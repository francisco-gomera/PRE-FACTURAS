import json
from django.db import connection, transaction
from django.db.models import Q
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.clickjacking import xframe_options_sameorigin

from ajustes.permissions import has_perm
from core.realtime import broadcast_notification_refresh
from core.views import _base_context, render_denied
from cartas.views import (
    _build_aviso_default_body_text,
    _build_empresa_payload,
    _load_firma_b64,
    _format_money_str,
)
from .models import CobroAcuerdo, CobroCartaEnviada
from prefacturas_app.models_existing import MaestroSn, Usuario
from prefacturas_app.views import _get_open_ed_balance, _require_perm_or_denied


def _fmt_date(value):
    if not value:
        return ""
    if hasattr(value, "strftime"):
        return value.strftime("%d/%m/%Y")
    return str(value)


def _to_float(value):
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _days_overdue(value):
    if not value:
        return 0
    try:
        d = value.date() if hasattr(value, "date") else value
        return max((timezone.localdate() - d).days, 0)
    except Exception:
        return 0


def _chunked(sequence, size):
    for idx in range(0, len(sequence), size):
        yield sequence[idx:idx + size]


def _load_sectores():
    sectores = []
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT ID_CODIGO, DESCRIPCION
                FROM Territorio
                WHERE DESCRIPCION IS NOT NULL AND LTRIM(RTRIM(DESCRIPCION)) <> ''
                ORDER BY DESCRIPCION
                """
            )
            sectores = [{"id_codigo": row[0], "descripcion": row[1]} for row in cursor.fetchall()]
    except Exception:
        sectores = []
    return sectores


def _ensure_cobro_acuerdo_table():
    with connection.cursor() as cursor:
        cursor.execute(
            """
            IF OBJECT_ID('COBRO_ACUERDO', 'U') IS NULL
            BEGIN
                CREATE TABLE COBRO_ACUERDO (
                    ID_ACUERDO INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
                    ID_SN NVARCHAR(20) NOT NULL,
                    CLIENTE_NOMBRE NVARCHAR(200) NOT NULL,
                    TELEFONO NVARCHAR(50) NOT NULL CONSTRAINT DF_COBRO_ACUERDO_TELEFONO DEFAULT (''),
                    SECTOR NVARCHAR(120) NOT NULL CONSTRAINT DF_COBRO_ACUERDO_SECTOR DEFAULT (''),
                    TIPO NVARCHAR(30) NOT NULL CONSTRAINT DF_COBRO_ACUERDO_TIPO DEFAULT ('PROMESA_PAGO'),
                    FECHA_COMPROMISO DATE NULL,
                    MONTO_COMPROMISO DECIMAL(19,2) NULL,
                    NOTA NVARCHAR(MAX) NOT NULL,
                    ESTADO NVARCHAR(20) NOT NULL CONSTRAINT DF_COBRO_ACUERDO_ESTADO DEFAULT ('PENDIENTE'),
                    CREADO_POR_ID BIGINT NOT NULL,
                    FECHA_CREACION DATETIME2 NOT NULL CONSTRAINT DF_COBRO_ACUERDO_FECHA_CREACION DEFAULT (SYSDATETIME()),
                    FECHA_MODIFICACION DATETIME2 NOT NULL CONSTRAINT DF_COBRO_ACUERDO_FECHA_MODIFICACION DEFAULT (SYSDATETIME())
                );
            END
            """
        )


def _ensure_cobro_carta_enviada_table():
    with connection.cursor() as cursor:
        cursor.execute(
            """
            IF OBJECT_ID('COBRO_CARTA_ENVIADA', 'U') IS NULL
            BEGIN
                CREATE TABLE COBRO_CARTA_ENVIADA (
                    ID_CARTA INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
                    ID_SN NVARCHAR(20) NOT NULL,
                    CLIENTE_NOMBRE NVARCHAR(200) NOT NULL,
                    TELEFONO NVARCHAR(50) NOT NULL CONSTRAINT DF_COBRO_CARTA_TELEFONO DEFAULT (''),
                    SECTOR NVARCHAR(120) NOT NULL CONSTRAINT DF_COBRO_CARTA_SECTOR DEFAULT (''),
                    TIPO NVARCHAR(30) NOT NULL CONSTRAINT DF_COBRO_CARTA_TIPO DEFAULT ('AVISO'),
                    MEDIO_ENVIO NVARCHAR(30) NOT NULL CONSTRAINT DF_COBRO_CARTA_MEDIO DEFAULT ('IMPRESA'),
                    FECHA_ENVIO DATE NOT NULL,
                    ESTADO NVARCHAR(20) NOT NULL CONSTRAINT DF_COBRO_CARTA_ESTADO DEFAULT ('ENVIADA'),
                    FECHA_SEGUIMIENTO DATE NULL,
                    NOTA NVARCHAR(MAX) NOT NULL,
                    CREADO_POR_ID BIGINT NOT NULL,
                    FECHA_CREACION DATETIME2 NOT NULL CONSTRAINT DF_COBRO_CARTA_FECHA_CREACION DEFAULT (SYSDATETIME()),
                    FECHA_MODIFICACION DATETIME2 NOT NULL CONSTRAINT DF_COBRO_CARTA_FECHA_MODIFICACION DEFAULT (SYSDATETIME())
                );
            END
            """
        )


def _build_estado_cuenta_context(id_sn):
    cliente = None
    balance = 0.0
    facturas_abiertas = []

    if not id_sn:
        return {
            "cliente": cliente,
            "balance": balance,
            "facturas_abiertas": facturas_abiertas,
            "fecha_impresion": timezone.localdate(),
        }

    cliente = (
        MaestroSn.objects.filter(id_sn=id_sn)
        .values(
            "id_sn",
            "nom_socio",
            "rnc_ced",
            "dir_factura",
            "tel1",
        )
        .first()
    )

    if not cliente:
        return {
            "cliente": None,
            "balance": 0.0,
            "facturas_abiertas": [],
            "fecha_impresion": timezone.localdate(),
        }

    balance = _to_float(_get_open_ed_balance(id_sn))

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT FECHA_DOC, ID_DOC, TOTAL_DOC, SALDO, FECHA_VENC
            FROM CAB_FACTURA
            WHERE ID_SN = %s
              AND UPPER(ISNULL(EST_DOC, '')) = 'ABIERTO'
            ORDER BY FECHA_DOC, ID_DOC
            """,
            [id_sn],
        )
        rows = cursor.fetchall()

    docs = [row[1] for row in rows if row[1] is not None]
    cuotas_by_doc = {}
    if docs:
        placeholders = ", ".join(["%s"] * len(docs))
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT NO_DOC, NO_CUOTA, FECHA, FECHA_VENC, CUOTA, BALANCE, SALDO_INSOLUTO
                FROM DET_PRESTAMO
                WHERE NO_DOC IN ({placeholders})
                ORDER BY NO_DOC, NO_CUOTA
                """,
                docs,
            )
            cuotas_rows = cursor.fetchall()
        for c in cuotas_rows:
            no_doc = c[0]
            cuotas_by_doc.setdefault(no_doc, []).append(
                {
                    "no_cuota": c[1],
                    "fecha": c[2],
                    "fecha_venc": c[3],
                    "cuota": c[4],
                    "balance": c[5],
                    "saldo_insoluto": c[6],
                }
            )

    for row in rows:
        fecha_doc, id_doc, total_doc, saldo_doc, fecha_venc = row
        cuotas = cuotas_by_doc.get(id_doc, [])
        if cuotas:
            for cuota in cuotas:
                no_cuota = cuota.get("no_cuota")
                id_doc_label = f"{id_doc}-{no_cuota}" if no_cuota is not None else id_doc
                saldo_cuota = cuota.get("balance")
                if saldo_cuota is None:
                    saldo_cuota = cuota.get("saldo_insoluto")
                saldo_cuota_val = _to_float(saldo_cuota)
                if saldo_cuota_val <= 0:
                    continue
                fecha_venc_cuota = cuota.get("fecha_venc") or fecha_venc
                facturas_abiertas.append(
                    {
                        "fecha_doc": _fmt_date(cuota.get("fecha") or fecha_doc),
                        "id_doc": id_doc_label,
                        "total_doc": _to_float(cuota.get("cuota")),
                        "saldo": saldo_cuota_val,
                        "fecha_venc": _fmt_date(fecha_venc_cuota),
                        "dias": _days_overdue(fecha_venc_cuota),
                    }
                )
        else:
            facturas_abiertas.append(
                {
                    "fecha_doc": _fmt_date(fecha_doc),
                    "id_doc": id_doc,
                    "total_doc": _to_float(total_doc),
                    "saldo": _to_float(saldo_doc),
                    "fecha_venc": _fmt_date(fecha_venc),
                    "dias": _days_overdue(fecha_venc),
                }
            )

    return {
        "cliente": cliente,
        "balance": balance,
        "facturas_abiertas": facturas_abiertas,
        "fecha_impresion": timezone.localdate(),
    }


def _build_alertas_context(min_days, max_days=None, sector_id=None, vista_compacta=False):
    min_days = max(int(min_days or 0), 0)
    max_days = None if max_days in (None, "") else max(int(max_days), 0)
    if max_days is not None and max_days < min_days:
        min_days, max_days = max_days, min_days
    sector_id = None if sector_id in (None, "") else int(sector_id)
    grupos_map = {}

    with connection.cursor() as cursor:
        sql = """
            SELECT
                f.ID_SN,
                s.NOM_SOCIO,
                s.TEL1,
                s.ID_SECTOR,
                ISNULL(t.DESCRIPCION, 'SIN SECTOR') AS SECTOR,
                f.FECHA_DOC,
                f.ID_DOC,
                f.TOTAL_DOC,
                f.SALDO,
                f.FECHA_VENC,
                s.CONTACTO AS APODO,
                f.MONICA,
                s.DIR_FACTURA
            FROM CAB_FACTURA f
            INNER JOIN MAESTRO_SN s ON s.ID_SN = f.ID_SN
            LEFT JOIN Territorio t ON t.ID_CODIGO = s.ID_SECTOR
            WHERE UPPER(ISNULL(f.EST_DOC, '')) = 'ABIERTO'
        """
        params = []
        if sector_id is not None:
            sql += " AND s.ID_SECTOR = %s"
            params.append(sector_id)
        sql += " ORDER BY ISNULL(t.DESCRIPCION, 'SIN SECTOR'), s.NOM_SOCIO, f.FECHA_DOC, f.ID_DOC"
        cursor.execute(sql, params)
        rows = cursor.fetchall()

    docs = [row[6] for row in rows if row[6] is not None]
    cuotas_by_doc = {}
    ult_pago_by_doc = {}

    if docs:
        unique_docs = list(dict.fromkeys(docs))
        for docs_chunk in _chunked(unique_docs, 300):
            placeholders = ", ".join(["%s"] * len(docs_chunk))
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT NO_DOC, NO_CUOTA, FECHA, FECHA_VENC, CUOTA, BALANCE, SALDO_INSOLUTO
                    FROM DET_PRESTAMO
                    WHERE NO_DOC IN ({placeholders})
                    ORDER BY NO_DOC, NO_CUOTA
                    """,
                    docs_chunk,
                )
                cuotas_rows = cursor.fetchall()
            for c in cuotas_rows:
                cuotas_by_doc.setdefault(c[0], []).append(
                    {
                        "no_cuota": c[1],
                        "fecha": c[2],
                        "fecha_venc": c[3],
                        "cuota": c[4],
                        "balance": c[5],
                        "saldo_insoluto": c[6],
                    }
                )

            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT d.NO_DOC, MAX(COALESCE(c.FECHA_DOC, c.FECHA_CONT, d.FECHA_CONT, c.FECHA_CREACION))
                    FROM DET_RECIBO_INGRESO d
                    LEFT JOIN CAB_RECIBO_INGRESO c ON c.ID_RECIBO = d.ID_RECIBO
                    WHERE d.NO_DOC IN ({placeholders})
                      AND ISNULL(c.CANCELADO, 'N') <> 'Y'
                      AND UPPER(ISNULL(c.EST_DOC, '')) <> 'CANCELADO'
                    GROUP BY d.NO_DOC
                    """,
                    docs_chunk,
                )
                for no_doc, fecha_pago in cursor.fetchall():
                    ult_pago_by_doc[no_doc] = _fmt_date(fecha_pago)

    articles_by_doc = {}
    if vista_compacta and docs:
        unique_docs = list(dict.fromkeys(docs))
        for docs_chunk in _chunked(unique_docs, 300):
            placeholders = ", ".join(["%s"] * len(docs_chunk))
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT ID_DOC, DESCRIP_ART
                    FROM DET_FACTURA
                    WHERE ID_DOC IN ({placeholders})
                    """,
                    docs_chunk,
                )
                for doc_id, descrip_art in cursor.fetchall():
                    art_clean = str(descrip_art or "").strip()
                    if art_clean:
                        articles_by_doc.setdefault(str(doc_id).strip(), []).append(art_clean)

    for row in rows:
        id_sn, nom_socio, tel1, _, sector, fecha_doc, id_doc, total_doc, saldo_doc, fecha_venc, apodo, monica_val, direccion = row
        cuotas = cuotas_by_doc.get(id_doc, [])
        cliente_key = (sector, id_sn)

        def _ensure_cliente():
            grupo = grupos_map.setdefault(
                sector or "SIN SECTOR",
                {"sector": sector or "SIN SECTOR", "clientes_map": {}},
            )
            clientes_map = grupo["clientes_map"]
            cliente = clientes_map.get(cliente_key)
            if not cliente:
                cliente = {
                    "id_sn": id_sn,
                    "nombre": nom_socio or id_sn or "",
                    "telefono": tel1 or "",
                    "apodo": apodo or "",
                    "direccion": direccion or "",
                    "items": [],
                }
                clientes_map[cliente_key] = cliente
            return cliente

        if vista_compacta:
            is_overdue = False
            max_dias = 0
            if cuotas:
                for cuota in cuotas:
                    saldo_cuota = cuota.get("balance")
                    if saldo_cuota is None:
                        saldo_cuota = cuota.get("saldo_insoluto")
                    saldo_val = _to_float(saldo_cuota)
                    if saldo_val <= 0:
                        continue
                    fecha_venc_cuota = cuota.get("fecha_venc") or fecha_venc
                    dias = _days_overdue(fecha_venc_cuota)
                    if dias >= min_days and (max_days is None or dias <= max_days):
                        is_overdue = True
                        if dias > max_dias:
                            max_dias = dias
            else:
                saldo_val = _to_float(saldo_doc)
                if saldo_val > 0:
                    dias = _days_overdue(fecha_venc)
                    if dias >= min_days and (max_days is None or dias <= max_days):
                        is_overdue = True
                        max_dias = dias

            if is_overdue:
                cliente = _ensure_cliente()
                m_apertura = _to_float(monica_val) if (monica_val is not None and _to_float(monica_val) > 0) else _to_float(total_doc)
                arts_list = list(dict.fromkeys(articles_by_doc.get(str(id_doc).strip(), [])))
                articulos_str = ", ".join(arts_list)
                cliente["items"].append(
                    {
                        "id_doc": id_doc,
                        "fecha_factura": _fmt_date(fecha_doc),
                        "monto_total": m_apertura,
                        "monto_pendiente": _to_float(saldo_doc),
                        "fecha_ultimo_pago": ult_pago_by_doc.get(id_doc, ""),
                        "dias_atraso": max_dias,
                        "articulos": articulos_str,
                    }
                )
        else:
            if cuotas:
                for cuota in cuotas:
                    saldo_cuota = cuota.get("balance")
                    if saldo_cuota is None:
                        saldo_cuota = cuota.get("saldo_insoluto")
                    saldo_val = _to_float(saldo_cuota)
                    if saldo_val <= 0:
                        continue
                    fecha_venc_cuota = cuota.get("fecha_venc") or fecha_venc
                    dias = _days_overdue(fecha_venc_cuota)
                    if dias < min_days or (max_days is not None and dias > max_days):
                        continue
                    cliente = _ensure_cliente()
                    cliente["items"].append(
                        {
                            "id_doc": f"{id_doc}-{cuota.get('no_cuota')}" if cuota.get("no_cuota") is not None else id_doc,
                            "monto_total": _to_float(cuota.get("cuota")),
                            "fecha_factura": _fmt_date(cuota.get("fecha") or fecha_doc),
                            "fecha_ultimo_pago": ult_pago_by_doc.get(id_doc, ""),
                            "monto_pendiente": saldo_val,
                            "dias_atraso": dias,
                        }
                    )
            else:
                saldo_val = _to_float(saldo_doc)
                if saldo_val <= 0:
                    continue
                dias = _days_overdue(fecha_venc)
                if dias < min_days or (max_days is not None and dias > max_days):
                    continue
                cliente = _ensure_cliente()
                cliente["items"].append(
                    {
                        "id_doc": id_doc,
                        "monto_total": _to_float(total_doc),
                        "fecha_factura": _fmt_date(fecha_doc),
                        "fecha_ultimo_pago": ult_pago_by_doc.get(id_doc, ""),
                        "monto_pendiente": saldo_val,
                        "dias_atraso": dias,
                    }
                )

    grupos = []
    total_items = 0
    for sector_name in sorted(grupos_map.keys(), key=lambda value: (value or "").upper()):
        clientes_map = grupos_map[sector_name]["clientes_map"]
        clientes = []
        for _, cliente in sorted(clientes_map.items(), key=lambda item: (item[1]["nombre"] or "").upper()):
            if not cliente["items"]:
                continue
            cliente["items"] = sorted(cliente["items"], key=lambda item: (item["dias_atraso"] * -1, item["fecha_factura"], item["id_doc"]))
            total_items += len(cliente["items"])
            clientes.append(cliente)
        if clientes:
            grupos.append({"sector": sector_name, "clientes": clientes})

    return {
        "min_days": min_days,
        "max_days": max_days,
        "has_max_days": max_days is not None,
        "sector_id": sector_id,
        "grupos": grupos,
        "total_items": total_items,
        "fecha_impresion": timezone.localdate(),
    }


def _parse_positive_int(value, default=None):
    raw = str(value or "").strip()
    if not raw:
        return default
    try:
        return max(int(raw), 0)
    except (TypeError, ValueError):
        return default


def _build_financiamientos_atraso_context(cuotas_desde=None, cuotas_hasta=None, sector_id=None, q=""):
    cuotas_desde = _parse_positive_int(cuotas_desde, None)
    cuotas_hasta = _parse_positive_int(cuotas_hasta, None)
    if cuotas_desde is not None and cuotas_hasta is not None and cuotas_hasta < cuotas_desde:
        cuotas_desde, cuotas_hasta = cuotas_hasta, cuotas_desde
    try:
        sector_id = None if sector_id in (None, "") else int(sector_id)
    except (TypeError, ValueError):
        sector_id = None
    q = str(q or "").strip()

    sql = """
        SELECT
            f.ID_SN,
            s.NOM_SOCIO,
            s.RNC_CED,
            s.TEL1,
            s.DIR_FACTURA,
            s.ID_SECTOR,
            ISNULL(t.DESCRIPCION, 'SIN SECTOR') AS SECTOR,
            s.CONTACTO,
            f.ID_DOC,
            f.FECHA_DOC,
            f.TOTAL_DOC,
            f.SALDO,
            f.FECHA_VENC,
            p.NO_CUOTA,
            p.FECHA,
            p.FECHA_VENC,
            p.CUOTA,
            p.BALANCE,
            p.SALDO_INSOLUTO
        FROM CAB_FACTURA f
        INNER JOIN DET_PRESTAMO p ON p.NO_DOC = f.ID_DOC
        INNER JOIN MAESTRO_SN s ON s.ID_SN = f.ID_SN
        LEFT JOIN Territorio t ON t.ID_CODIGO = s.ID_SECTOR
        WHERE UPPER(ISNULL(f.EST_DOC, '')) = 'ABIERTO'
    """
    params = []
    if sector_id is not None:
        sql += " AND s.ID_SECTOR = %s"
        params.append(sector_id)
    if q:
        sql += """
            AND (
                CAST(f.ID_DOC AS NVARCHAR(255)) LIKE %s OR
                CAST(f.ID_SN AS NVARCHAR(255)) LIKE %s OR
                s.NOM_SOCIO LIKE %s OR
                s.RNC_CED LIKE %s OR
                s.TEL1 LIKE %s
            )
        """
        like = f"%{q}%"
        params.extend([like, like, like, like, like])
    sql += " ORDER BY f.ID_DOC, p.NO_CUOTA"

    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()

    records_map = {}
    overdue_docs = []
    for row in rows:
        (
            id_sn,
            nom_socio,
            rnc_ced,
            tel1,
            dir_factura,
            _id_sector,
            sector,
            contacto,
            id_doc,
            fecha_doc,
            total_doc,
            saldo_doc,
            fecha_venc_doc,
            no_cuota,
            fecha_cuota,
            fecha_venc_cuota,
            cuota_monto,
            balance,
            saldo_insoluto,
        ) = row

        saldo_cuota = balance if balance is not None else saldo_insoluto
        saldo_val = _to_float(saldo_cuota)
        if saldo_val <= 0:
            continue
        vencimiento = fecha_venc_cuota or fecha_venc_doc
        dias = _days_overdue(vencimiento)
        if dias <= 0:
            continue

        if id_doc not in records_map:
            records_map[id_doc] = {
                "id_sn": id_sn,
                "cliente": nom_socio or id_sn or "",
                "rnc_ced": rnc_ced or "",
                "telefono": tel1 or "",
                "direccion": dir_factura or "",
                "sector": sector or "SIN SECTOR",
                "apodo": contacto or "",
                "id_doc": id_doc,
                "fecha_doc": _fmt_date(fecha_doc),
                "fecha_venc": _fmt_date(fecha_venc_doc),
                "total_doc": _to_float(total_doc),
                "saldo_doc": _to_float(saldo_doc),
                "cuotas_atrasadas": 0,
                "balance_atraso": 0.0,
                "dias_mayor_atraso": 0,
                "primera_vencida": "",
                "ultima_vencida": "",
                "cuotas": [],
            }
            overdue_docs.append(id_doc)

        record = records_map[id_doc]
        cuota_val = _to_float(cuota_monto)
        record["cuotas_atrasadas"] += 1
        record["balance_atraso"] += saldo_val
        record["dias_mayor_atraso"] = max(record["dias_mayor_atraso"], dias)
        record["cuotas"].append(
            {
                "no_cuota": no_cuota or "",
                "fecha": _fmt_date(fecha_cuota or fecha_doc),
                "fecha_venc": _fmt_date(vencimiento),
                "monto": cuota_val,
                "pagado": max(cuota_val - saldo_val, 0.0),
                "saldo": saldo_val,
                "dias": dias,
            }
        )

    ult_pago_by_doc = {}
    unique_docs = list(dict.fromkeys(overdue_docs))
    if unique_docs:
        for docs_chunk in _chunked(unique_docs, 300):
            placeholders = ", ".join(["%s"] * len(docs_chunk))
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT d.NO_DOC, MAX(COALESCE(c.FECHA_DOC, c.FECHA_CONT, d.FECHA_CONT, c.FECHA_CREACION))
                    FROM DET_RECIBO_INGRESO d
                    LEFT JOIN CAB_RECIBO_INGRESO c ON c.ID_RECIBO = d.ID_RECIBO
                    WHERE d.NO_DOC IN ({placeholders})
                      AND ISNULL(c.CANCELADO, 'N') <> 'Y'
                      AND UPPER(ISNULL(c.EST_DOC, '')) <> 'CANCELADO'
                    GROUP BY d.NO_DOC
                    """,
                    docs_chunk,
                )
                for no_doc, fecha_pago in cursor.fetchall():
                    ult_pago_by_doc[no_doc] = _fmt_date(fecha_pago)

    records = []
    for record in records_map.values():
        if cuotas_desde is not None and record["cuotas_atrasadas"] < cuotas_desde:
            continue
        if cuotas_hasta is not None and record["cuotas_atrasadas"] > cuotas_hasta:
            continue
        record["cuotas"] = sorted(record["cuotas"], key=lambda item: (-item["dias"], item["no_cuota"]))
        record["primera_vencida"] = record["cuotas"][0]["fecha_venc"] if record["cuotas"] else ""
        record["ultima_vencida"] = record["cuotas"][-1]["fecha_venc"] if record["cuotas"] else ""
        record["fecha_ultimo_pago"] = ult_pago_by_doc.get(record["id_doc"], "")
        records.append(record)

    records = sorted(
        records,
        key=lambda item: (-item["cuotas_atrasadas"], -item["dias_mayor_atraso"], (item["cliente"] or "").upper(), item["id_doc"]),
    )
    return {
        "records": records,
        "total_records": len(records),
        "total_cuotas": sum(item["cuotas_atrasadas"] for item in records),
        "total_balance": sum(item["balance_atraso"] for item in records),
        "cuotas_desde": cuotas_desde if cuotas_desde is not None else "",
        "cuotas_hasta": cuotas_hasta if cuotas_hasta is not None else "",
        "sector_id": sector_id if sector_id is not None else "",
        "q": q,
        "fecha_impresion": timezone.localdate(),
    }


def _build_cuentas_sin_financiamiento_atraso_context(dias_desde=None, dias_hasta=None, sector_id=None, q="", criterio="emision"):
    dias_desde = _parse_positive_int(dias_desde, 1)
    dias_hasta = _parse_positive_int(dias_hasta, None)
    if dias_desde is not None and dias_hasta is not None and dias_hasta < dias_desde:
        dias_desde, dias_hasta = dias_hasta, dias_desde
    try:
        sector_id = None if sector_id in (None, "") else int(sector_id)
    except (TypeError, ValueError):
        sector_id = None
    q = str(q or "").strip()
    criterio = str(criterio or "emision").strip().lower()
    if criterio not in {"emision", "vencimiento"}:
        criterio = "emision"

    sql = """
        SELECT
            f.ID_SN,
            s.NOM_SOCIO,
            s.RNC_CED,
            s.TEL1,
            s.DIR_FACTURA,
            s.ID_SECTOR,
            ISNULL(t.DESCRIPCION, 'SIN SECTOR') AS SECTOR,
            f.ID_DOC,
            f.FECHA_DOC,
            f.TOTAL_DOC,
            f.SALDO,
            f.FECHA_VENC
        FROM CAB_FACTURA f
        INNER JOIN MAESTRO_SN s ON s.ID_SN = f.ID_SN
        LEFT JOIN Territorio t ON t.ID_CODIGO = s.ID_SECTOR
        WHERE UPPER(ISNULL(f.EST_DOC, '')) = 'ABIERTO'
          AND UPPER(ISNULL(f.CANCELADO, 'N')) <> 'Y'
          AND ISNULL(f.SALDO, 0) > 0
          AND NOT EXISTS (
                SELECT 1
                FROM DET_PRESTAMO p
                WHERE CAST(p.NO_DOC AS NVARCHAR(255)) = CAST(f.ID_DOC AS NVARCHAR(255))
          )
    """
    params = []
    if sector_id is not None:
        sql += " AND s.ID_SECTOR = %s"
        params.append(sector_id)
    if q:
        sql += """
            AND (
                CAST(f.ID_DOC AS NVARCHAR(255)) LIKE %s OR
                CAST(f.ID_SN AS NVARCHAR(255)) LIKE %s OR
                s.NOM_SOCIO LIKE %s OR
                s.RNC_CED LIKE %s OR
                s.TEL1 LIKE %s
            )
        """
        like = f"%{q}%"
        params.extend([like, like, like, like, like])
    sql += " ORDER BY f.FECHA_DOC, f.ID_DOC"

    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()

    docs = [row[7] for row in rows if row[7] is not None]
    ult_pago_by_doc = {}
    if docs:
        for docs_chunk in _chunked(list(dict.fromkeys(docs)), 300):
            placeholders = ", ".join(["%s"] * len(docs_chunk))
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT d.NO_DOC, MAX(COALESCE(c.FECHA_DOC, c.FECHA_CONT, d.FECHA_CONT, c.FECHA_CREACION))
                    FROM DET_RECIBO_INGRESO d
                    LEFT JOIN CAB_RECIBO_INGRESO c ON c.ID_RECIBO = d.ID_RECIBO
                    WHERE d.NO_DOC IN ({placeholders})
                      AND ISNULL(c.CANCELADO, 'N') <> 'Y'
                      AND UPPER(ISNULL(c.EST_DOC, '')) <> 'CANCELADO'
                    GROUP BY d.NO_DOC
                    """,
                    docs_chunk,
                )
                for no_doc, fecha_pago in cursor.fetchall():
                    ult_pago_by_doc[no_doc] = _fmt_date(fecha_pago)

    records = []
    for row in rows:
        (
            id_sn,
            nom_socio,
            rnc_ced,
            tel1,
            dir_factura,
            _id_sector,
            sector,
            id_doc,
            fecha_doc,
            total_doc,
            saldo_doc,
            fecha_venc,
        ) = row
        fecha_base = fecha_venc if criterio == "vencimiento" else fecha_doc
        dias = _days_overdue(fecha_base)
        if dias <= 0:
            continue
        if dias_desde is not None and dias < dias_desde:
            continue
        if dias_hasta is not None and dias > dias_hasta:
            continue

        records.append(
            {
                "id_sn": id_sn,
                "cliente": nom_socio or id_sn or "",
                "rnc_ced": rnc_ced or "",
                "telefono": tel1 or "",
                "direccion": dir_factura or "",
                "sector": sector or "SIN SECTOR",
                "id_doc": id_doc,
                "fecha_doc": _fmt_date(fecha_doc),
                "fecha_venc": _fmt_date(fecha_venc),
                "total_doc": _to_float(total_doc),
                "saldo_doc": _to_float(saldo_doc),
                "dias_atraso": dias,
                "fecha_ultimo_pago": ult_pago_by_doc.get(id_doc, ""),
            }
        )

    records = sorted(
        records,
        key=lambda item: (-item["dias_atraso"], (item["cliente"] or "").upper(), str(item["id_doc"])),
    )
    return {
        "records": records,
        "total_records": len(records),
        "total_balance": sum(item["saldo_doc"] for item in records),
        "dias_mayor_atraso": max([item["dias_atraso"] for item in records] or [0]),
        "dias_desde": dias_desde if dias_desde is not None else "",
        "dias_hasta": dias_hasta if dias_hasta is not None else "",
        "sector_id": sector_id if sector_id is not None else "",
        "q": q,
        "criterio": criterio,
        "fecha_impresion": timezone.localdate(),
    }


def _build_financiamiento_aviso_context(request, no_doc, auth_payload, embed=False):
    no_doc = str(no_doc or "").strip()
    data = _build_financiamientos_atraso_context(q=no_doc)
    record = next((item for item in data["records"] if str(item.get("id_doc") or "") == no_doc), None)
    if not record:
        record = {
            "id_doc": no_doc,
            "cliente": "",
            "rnc_ced": "",
            "direccion": "",
            "telefono": "",
            "sector": "",
            "apodo": "",
            "cuotas": [],
            "balance_atraso": 0.0,
        }

    empresa = _build_empresa_payload(_base_context(request, page_title="", active_nav="cobros") or {})
    logo_tipo = empresa.get("logo_tipo") or "image/png"
    logo_b64 = empresa.get("logo_b64") or ""
    sello_b64 = empresa.get("sello_b64") or ""
    firma_b64 = _load_firma_b64(auth_payload["usuario_id"])
    total_mora = 0.0

    articulos_list = []
    if no_doc:
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT DISTINCT DESCRIP_ART
                    FROM DET_FACTURA
                    WHERE ID_DOC = %s
                    """,
                    [no_doc]
                )
                art_rows = cursor.fetchall()
            articulos_list = [r[0].strip() for r in art_rows if r[0]]
        except Exception:
            pass

    return {
        "embed": embed,
        "document_title": f"aviso_financiamiento_{no_doc}",
        "empresa_nombre": empresa.get("nombre", "") or "COMERCIAL ANITA SRL",
        "empresa_direccion": empresa.get("direccion", ""),
        "empresa_telefonos": "  ".join([value for value in [empresa.get("tel1", ""), empresa.get("tel2", "")] if value]),
        "empresa_email": empresa.get("email", ""),
        "empresa_rnc": empresa.get("rnc", ""),
        "empresa_logo_src": f"data:{logo_tipo};base64,{logo_b64}" if logo_b64 else "",
        "empresa_sello_src": f"data:image/png;base64,{sello_b64}" if sello_b64 else "",
        "firma_src": f"data:image/png;base64,{firma_b64}" if firma_b64 else "",
        "cliente_nombre": record.get("cliente", ""),
        "cliente_rnc": record.get("rnc_ced", ""),
        "cliente_direccion": record.get("direccion", ""),
        "cliente_telefono": record.get("telefono", ""),
        "cliente_apodo": record.get("apodo", ""),
        "ciudad_impresion": record.get("sector", ""),
        "articulos_atraso": ", ".join(articulos_list),
        "fecha": timezone.localdate().strftime("%d/%m/%Y"),
        "hora": timezone.localtime().strftime("%I:%M:%S %p").lstrip("0"),
        "aviso_asunto": f"Financiamiento {no_doc} con cuotas en atraso",
        "aviso_body_text": _build_aviso_default_body_text(),
        "facturas": [
            {
                "id_doc": record.get("id_doc", ""),
                "no_cuota": item.get("no_cuota") or "Abierto",
                "fecha_venc": item.get("fecha_venc", ""),
                "monto_fmt": _format_money_str(item.get("monto")),
                "pagado_fmt": _format_money_str(item.get("pagado")),
                "saldo_fmt": _format_money_str(item.get("saldo")),
                "mora_fmt": _format_money_str(0),
                "dias": item.get("dias", 0),
            }
            for item in record.get("cuotas", [])
        ],
        "total_saldo_fmt": _format_money_str(record.get("balance_atraso")),
        "total_mora_fmt": _format_money_str(total_mora),
    }


def _can_view_financiamientos_atraso(usuario_id):
    return (
        has_perm(usuario_id, "cobros", "ver_financiamientos_atraso")
        or has_perm(usuario_id, "cobros", "ver_alertas")
    )


def _can_view_cuentas_sin_financiamiento_atraso(usuario_id):
    return _can_view_financiamientos_atraso(usuario_id)


def index(request):
    ctx = _base_context(request, page_title="Gestion de cobros", active_nav="cobros")
    if not ctx:
        return redirect("login")
    if not has_perm(ctx["auth_payload"]["usuario_id"], "cobros", "ver"):
        return render_denied(request, active_nav="cobros")
    ctx["submodules"] = {
        "estado_cuenta": has_perm(ctx["auth_payload"]["usuario_id"], "cobros", "ver_estado_cuenta"),
        "alertas": has_perm(ctx["auth_payload"]["usuario_id"], "cobros", "ver_alertas"),
        "financiamientos_atraso": _can_view_financiamientos_atraso(ctx["auth_payload"]["usuario_id"]),
        "cuentas_sin_financiamiento_atraso": _can_view_cuentas_sin_financiamiento_atraso(ctx["auth_payload"]["usuario_id"]),
        "acuerdos": has_perm(ctx["auth_payload"]["usuario_id"], "cobros", "ver_acuerdos"),
        "pagos_recuperados": (
            has_perm(ctx["auth_payload"]["usuario_id"], "cobros", "ver_pagos_recuperados")
            or has_perm(ctx["auth_payload"]["usuario_id"], "cobros", "ver_acuerdos")
        ),
        "cartas_enviadas": has_perm(ctx["auth_payload"]["usuario_id"], "cobros", "ver_cartas_enviadas"),
    }
    return render(request, "cobros/index.html", ctx)


def estado_cuenta_view(request):
    ctx = _base_context(request, page_title="Cobros - Estado de Cuenta", active_nav="cobros")
    if not ctx:
        return redirect("login")
    if not has_perm(ctx["auth_payload"]["usuario_id"], "cobros", "ver_estado_cuenta"):
        return render_denied(request, active_nav="cobros")
    return render(request, "cobros/estado_cuenta.html", ctx)


def alertas_view(request):
    ctx = _base_context(request, page_title="Cobros - Alertas", active_nav="cobros")
    if not ctx:
        return redirect("login")
    if not has_perm(ctx["auth_payload"]["usuario_id"], "cobros", "ver_alertas"):
        return render_denied(request, active_nav="cobros")
    try:
        dias_desde = max(int((request.GET.get("dias_desde") or "30").strip() or "30"), 0)
    except ValueError:
        dias_desde = 30
    try:
        dias_hasta_raw = (request.GET.get("dias_hasta") or "").strip()
        dias_hasta = max(int(dias_hasta_raw), 0) if dias_hasta_raw else ""
    except ValueError:
        dias_hasta = ""
    try:
        sector_default_raw = (request.GET.get("sector") or "").strip()
        sector_default = int(sector_default_raw) if sector_default_raw else ""
    except ValueError:
        sector_default = ""
    ctx["dias_desde_default"] = dias_desde
    ctx["dias_hasta_default"] = dias_hasta
    ctx["sector_default"] = sector_default
    ctx["sectores"] = _load_sectores()
    return render(request, "cobros/alertas.html", ctx)


def financiamientos_atraso_view(request):
    ctx = _base_context(request, page_title="Cobros - Financiamientos en atraso", active_nav="cobros")
    if not ctx:
        return redirect("login")
    if not _can_view_financiamientos_atraso(ctx["auth_payload"]["usuario_id"]):
        return render_denied(request, active_nav="cobros")

    cuotas_desde = request.GET.get("cuotas_desde", "1")
    cuotas_hasta = request.GET.get("cuotas_hasta", "")
    sector_id = request.GET.get("sector", "")
    q = request.GET.get("q", "")
    ctx.update(_build_financiamientos_atraso_context(cuotas_desde, cuotas_hasta, sector_id, q))
    ctx["sectores"] = _load_sectores()
    return render(request, "cobros/financiamientos_atraso.html", ctx)


def cuentas_sin_financiamiento_atraso_view(request):
    ctx = _base_context(request, page_title="Cobros - Cuentas sin financiamiento en atraso", active_nav="cobros")
    if not ctx:
        return redirect("login")
    if not _can_view_cuentas_sin_financiamiento_atraso(ctx["auth_payload"]["usuario_id"]):
        return render_denied(request, active_nav="cobros")

    dias_desde = request.GET.get("dias_desde", "1")
    dias_hasta = request.GET.get("dias_hasta", "")
    sector_id = request.GET.get("sector", "")
    q = request.GET.get("q", "")
    criterio = request.GET.get("criterio", "emision")
    ctx.update(_build_cuentas_sin_financiamiento_atraso_context(dias_desde, dias_hasta, sector_id, q, criterio))
    ctx["sectores"] = _load_sectores()
    return render(request, "cobros/cuentas_sin_financiamiento_atraso.html", ctx)


def acuerdos_view(request):
    ctx = _base_context(request, page_title="Cobros - Acuerdos", active_nav="cobros")
    if not ctx:
        return redirect("login")
    if not has_perm(ctx["auth_payload"]["usuario_id"], "cobros", "ver_acuerdos"):
        return render_denied(request, active_nav="cobros")

    _ensure_cobro_acuerdo_table()

    usuario_id = int(ctx["auth_payload"]["usuario_id"])
    q = (request.GET.get("q") or "").strip()
    estado_filtro = (request.GET.get("estado") or "").strip().upper()
    edit_id = (request.GET.get("edit") or "").strip()
    hoy = timezone.localdate()
    selected = None
    error_message = ""
    success_message = ""

    if request.method == "POST":
        action = (request.POST.get("action") or "save").strip().lower()
        acuerdo_id = (request.POST.get("id_acuerdo") or "").strip()

        if action in {"complete", "cancel", "reopen"} and acuerdo_id:
            acuerdo = CobroAcuerdo.objects.filter(id_acuerdo=acuerdo_id).first()
            if acuerdo:
                if action == "complete":
                    acuerdo.estado = "CUMPLIDO"
                elif action == "cancel":
                    acuerdo.estado = "CANCELADO"
                else:
                    acuerdo.estado = "PENDIENTE"
                acuerdo.save(update_fields=["estado", "fecha_modificacion"])
                transaction.on_commit(
                    lambda: broadcast_notification_refresh(reason="payment-agreement-updated")
                )
            return redirect("cobros:acuerdos")

        id_sn = (request.POST.get("id_sn") or "").strip()
        cliente_nombre = (request.POST.get("cliente_nombre") or "").strip()
        telefono = (request.POST.get("telefono") or "").strip()
        sector = (request.POST.get("sector") or "").strip()
        tipo = (request.POST.get("tipo") or "PROMESA_PAGO").strip().upper()
        fecha_compromiso_raw = (request.POST.get("fecha_compromiso") or "").strip()
        monto_compromiso_raw = (request.POST.get("monto_compromiso") or "").strip()
        nota = (request.POST.get("nota") or "").strip()
        estado = (request.POST.get("estado") or "PENDIENTE").strip().upper()

        fecha_compromiso = None
        monto_compromiso = None
        if not id_sn:
            error_message = "Debes seleccionar un cliente."
        elif not cliente_nombre:
            error_message = "El nombre del cliente es obligatorio."
        elif not nota:
            error_message = "La nota o detalle es obligatorio."
        else:
            if fecha_compromiso_raw:
                try:
                    fecha_compromiso = timezone.datetime.strptime(fecha_compromiso_raw, "%Y-%m-%d").date()
                except ValueError:
                    error_message = "La fecha compromiso no es válida."
            if not error_message and monto_compromiso_raw:
                try:
                    monto_compromiso = float(monto_compromiso_raw.replace(",", ""))
                except ValueError:
                    error_message = "El monto compromiso no es válido."

        if not error_message:
            if acuerdo_id:
                acuerdo = CobroAcuerdo.objects.filter(id_acuerdo=acuerdo_id).first()
                if not acuerdo:
                    error_message = "El acuerdo seleccionado no existe."
                else:
                    acuerdo.id_sn = id_sn
                    acuerdo.cliente_nombre = cliente_nombre
                    acuerdo.telefono = telefono
                    acuerdo.sector = sector
                    acuerdo.tipo = tipo
                    acuerdo.fecha_compromiso = fecha_compromiso
                    acuerdo.monto_compromiso = monto_compromiso
                    acuerdo.nota = nota
                    acuerdo.estado = estado
                    acuerdo.save()
                    transaction.on_commit(
                        lambda: broadcast_notification_refresh(reason="payment-agreement-updated")
                    )
                    success_message = "Acuerdo guardado correctamente."
                    selected = None
            else:
                CobroAcuerdo.objects.create(
                    id_sn=id_sn,
                    cliente_nombre=cliente_nombre,
                    telefono=telefono,
                    sector=sector,
                    tipo=tipo,
                    fecha_compromiso=fecha_compromiso,
                    monto_compromiso=monto_compromiso,
                    nota=nota,
                    estado=estado,
                    creado_por_id=usuario_id,
                )
                transaction.on_commit(
                    lambda: broadcast_notification_refresh(reason="payment-agreement-created")
                )
                success_message = "Acuerdo guardado correctamente."
                selected = None

        if error_message:
            selected = {
                "id_acuerdo": acuerdo_id,
                "id_sn": id_sn,
                "cliente_nombre": cliente_nombre,
                "telefono": telefono,
                "sector": sector,
                "tipo": tipo,
                "fecha_compromiso": fecha_compromiso_raw,
                "monto_compromiso": monto_compromiso_raw,
                "nota": nota,
                "estado": estado,
            }
    elif edit_id:
        selected = CobroAcuerdo.objects.filter(id_acuerdo=edit_id).first()

    acuerdos = CobroAcuerdo.objects.all().order_by("estado", "-fecha_compromiso", "-fecha_creacion")
    if q:
        acuerdos = acuerdos.filter(
            Q(cliente_nombre__icontains=q)
            | Q(id_sn__icontains=q)
            | Q(telefono__icontains=q)
            | Q(sector__icontains=q)
            | Q(nota__icontains=q)
        )
    if estado_filtro == "VENCIDOS":
        acuerdos = acuerdos.filter(estado="PENDIENTE", fecha_compromiso__lt=hoy)
    elif estado_filtro:
        acuerdos = acuerdos.filter(estado=estado_filtro)

    acuerdos_hoy = CobroAcuerdo.objects.filter(estado="PENDIENTE", fecha_compromiso=hoy).order_by("cliente_nombre", "fecha_creacion")

    ctx["acuerdos"] = acuerdos
    ctx["acuerdos_hoy"] = acuerdos_hoy
    ctx["fecha_hoy"] = hoy
    ctx["selected_acuerdo"] = selected
    ctx["acuerdo_error"] = error_message
    ctx["acuerdo_success"] = success_message
    ctx["acuerdo_query"] = q
    ctx["acuerdo_estado"] = estado_filtro
    ctx["tipo_options"] = [
        ("PROMESA_PAGO", "Promesa de pago"),
        ("RECORDATORIO", "Recordatorio"),
        ("SEGUIMIENTO", "Seguimiento"),
        ("VISITA", "Visita"),
    ]
    ctx["estado_filtro_options"] = [
        ("PENDIENTE", "Pendiente"),
        ("VENCIDOS", "Pendientes vencidos"),
        ("CUMPLIDO", "Cumplido"),
        ("CANCELADO", "Cancelado"),
    ]
    ctx["estado_options"] = [
        ("PENDIENTE", "Pendiente"),
        ("CUMPLIDO", "Cumplido"),
        ("CANCELADO", "Cancelado"),
    ]
    return render(request, "cobros/acuerdos.html", ctx)


def _load_pagos_recuperados(fecha_desde=None, fecha_hasta=None, q="", event_ids=None):
    _ensure_cobro_acuerdo_table()
    where_clauses = ["q.TIPO_EVENTO = 'agreement_payment_received'"]
    params = []

    if event_ids:
        safe_ids = []
        for raw_id in event_ids:
            try:
                safe_ids.append(int(raw_id))
            except (TypeError, ValueError):
                continue
        if safe_ids:
            placeholders = ", ".join(["%s"] * len(safe_ids))
            where_clauses.append(f"q.ID_EVENTO IN ({placeholders})")
            params.extend(safe_ids)

    if fecha_desde:
        where_clauses.append("q.FECHA_EVENTO >= %s")
        params.append(f"{fecha_desde} 00:00:00")

    if fecha_hasta:
        where_clauses.append("q.FECHA_EVENTO <= %s")
        params.append(f"{fecha_hasta} 23:59:59")

    where_sql = " AND ".join(where_clauses)

    sql = f"""
        SELECT
            q.ID_EVENTO,
            q.FECHA_EVENTO,
            q.DOCUMENT_ID,
            q.PAYLOAD_JSON,
            a.ID_ACUERDO,
            a.ESTADO,
            a.TELEFONO,
            a.SECTOR,
            a.NOTA,
            a.MONTO_COMPROMISO
        FROM WS_EVENT_QUEUE q
        LEFT JOIN COBRO_ACUERDO a
            ON TRY_CAST(JSON_VALUE(q.PAYLOAD_JSON, '$.acuerdo_id') AS INT) = a.ID_ACUERDO
        WHERE {where_sql}
        ORDER BY q.FECHA_EVENTO DESC, q.ID_EVENTO DESC
    """

    records = []
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            rows = cursor.fetchall()
    except Exception:
        rows = []

    q_lower = str(q or "").strip().lower()

    for row in rows:
        event_id, fecha_evento, doc_id, payload_raw, ac_id, ac_estado, ac_tel, ac_sec, ac_nota, ac_monto_comp = row
        payload = {}
        if payload_raw:
            try:
                payload = json.loads(str(payload_raw))
            except Exception:
                payload = {}

        no_recibo = str(payload.get("no_recibo") or payload.get("document_id") or doc_id or "").strip()
        id_sn = str(payload.get("id_sn") or payload.get("cliente_codigo") or "").strip()
        cliente_nombre = str(payload.get("cliente_nombre") or "").strip()
        tipo_acuerdo = str(payload.get("tipo_acuerdo") or "PROMESA_PAGO").strip()
        fecha_compromiso = str(payload.get("fecha_compromiso") or "").strip()
        monto_pago = _to_float(payload.get("monto_pago"))
        acuerdo_id = str(payload.get("acuerdo_id") or ac_id or "").strip()
        telefono = str(ac_tel or "").strip()
        sector = str(ac_sec or "").strip()
        estado = str(ac_estado or "PENDIENTE").strip().upper()
        nota = str(ac_nota or "").strip()
        monto_compromiso = _to_float(ac_monto_comp) if ac_monto_comp is not None else None

        if q_lower:
            searchable = " ".join([
                cliente_nombre,
                id_sn,
                no_recibo,
                telefono,
                sector,
                tipo_acuerdo,
                acuerdo_id,
            ]).lower()
            if q_lower not in searchable:
                continue

        records.append({
            "id_evento": event_id,
            "fecha_evento": fecha_evento,
            "no_recibo": no_recibo,
            "id_sn": id_sn,
            "cliente_nombre": cliente_nombre,
            "tipo_acuerdo": tipo_acuerdo,
            "tipo_label": tipo_acuerdo.replace("_", " ").title(),
            "fecha_compromiso": fecha_compromiso,
            "monto_pago": monto_pago,
            "acuerdo_id": acuerdo_id,
            "telefono": telefono,
            "sector": sector,
            "estado": estado,
            "nota": nota,
            "monto_compromiso": monto_compromiso,
        })

    return records


def pagos_recuperados_view(request):
    ctx = _base_context(request, page_title="Cobros - Pagos Recuperados", active_nav="cobros")
    if not ctx:
        return redirect("login")
    usuario_id = ctx["auth_payload"]["usuario_id"]
    if not (has_perm(usuario_id, "cobros", "ver_pagos_recuperados") or has_perm(usuario_id, "cobros", "ver_acuerdos")):
        return render_denied(request, active_nav="cobros")

    today = timezone.localdate()
    default_desde = today.replace(day=1).strftime("%Y-%m-%d")
    default_hasta = today.strftime("%Y-%m-%d")

    fecha_desde = request.GET.get("fecha_desde")
    if fecha_desde is None:
        fecha_desde = default_desde
    else:
        fecha_desde = fecha_desde.strip()

    fecha_hasta = request.GET.get("fecha_hasta")
    if fecha_hasta is None:
        fecha_hasta = default_hasta
    else:
        fecha_hasta = fecha_hasta.strip()

    q = (request.GET.get("q") or "").strip()

    records = _load_pagos_recuperados(fecha_desde=fecha_desde, fecha_hasta=fecha_hasta, q=q)
    total_monto = sum(r["monto_pago"] for r in records)
    total_records = len(records)

    ctx["records"] = records
    ctx["total_records"] = total_records
    ctx["total_monto"] = total_monto
    ctx["fecha_desde"] = fecha_desde
    ctx["fecha_hasta"] = fecha_hasta
    ctx["q"] = q
    ctx["hoy"] = today
    return render(request, "cobros/pagos_recuperados.html", ctx)


@xframe_options_sameorigin
def pagos_recuperados_impresion_view(request):
    ctx = _base_context(request, page_title="Registro de Pagos Recuperados", active_nav="cobros")
    if not ctx:
        return redirect("login")
    usuario_id = ctx["auth_payload"]["usuario_id"]
    if not (has_perm(usuario_id, "cobros", "ver_pagos_recuperados") or has_perm(usuario_id, "cobros", "ver_acuerdos")):
        return render_denied(request, active_nav="cobros")

    if request.method == "POST":
        selected_ids = request.POST.getlist("selected_ids")
        fecha_desde = (request.POST.get("fecha_desde") or "").strip()
        fecha_hasta = (request.POST.get("fecha_hasta") or "").strip()
        q = (request.POST.get("q") or "").strip()
    else:
        selected_ids = request.GET.getlist("selected_ids")
        fecha_desde = (request.GET.get("fecha_desde") or "").strip()
        fecha_hasta = (request.GET.get("fecha_hasta") or "").strip()
        q = (request.GET.get("q") or "").strip()

    records = _load_pagos_recuperados(
        fecha_desde=fecha_desde if not selected_ids else None,
        fecha_hasta=fecha_hasta if not selected_ids else None,
        q=q if not selected_ids else "",
        event_ids=selected_ids if selected_ids else None,
    )

    total_monto = sum(r["monto_pago"] for r in records)
    empresa = _build_empresa_payload(ctx)

    ctx["records"] = records
    ctx["total_records"] = len(records)
    ctx["total_monto"] = total_monto
    ctx["fecha_desde"] = fecha_desde
    ctx["fecha_hasta"] = fecha_hasta
    ctx["fecha_impresion"] = timezone.localtime()
    ctx["usuario_nombre"] = ctx["auth_payload"].get("nombre") or ctx["auth_payload"].get("usuario") or ""
    ctx["empresa"] = empresa
    return render(request, "cobros/pagos_recuperados_print.html", ctx)



def cartas_enviadas_view(request):
    ctx = _base_context(request, page_title="Cobros - Cartas enviadas", active_nav="cobros")
    if not ctx:
        return redirect("login")
    if not has_perm(ctx["auth_payload"]["usuario_id"], "cobros", "ver_cartas_enviadas"):
        return render_denied(request, active_nav="cobros")

    _ensure_cobro_carta_enviada_table()

    usuario_id = int(ctx["auth_payload"]["usuario_id"])
    q = (request.GET.get("q") or "").strip()
    estado_filtro = (request.GET.get("estado") or "").strip().upper()
    edit_id = (request.GET.get("edit") or "").strip()
    hoy = timezone.localdate()
    selected = None
    error_message = ""
    success_message = ""

    if request.method == "POST":
        action = (request.POST.get("action") or "save").strip().lower()
        carta_id = (request.POST.get("id_carta") or "").strip()

        if action in {"sent", "complete", "promise", "cancel", "reopen"} and carta_id:
            carta = CobroCartaEnviada.objects.filter(id_carta=carta_id).first()
            if carta:
                if action == "sent":
                    carta.estado = "ENVIADA"
                elif action == "complete":
                    carta.estado = "CUMPLIDA"
                elif action == "promise":
                    carta.estado = "PROMESA"
                elif action == "cancel":
                    carta.estado = "CANCELADA"
                else:
                    carta.estado = "ENVIADA"
                carta.save(update_fields=["estado", "fecha_modificacion"])
                transaction.on_commit(
                    lambda: broadcast_notification_refresh(reason="sent-letter-updated")
                )
            return redirect("cobros:cartas_enviadas")

        id_sn = (request.POST.get("id_sn") or "").strip()
        cliente_nombre = (request.POST.get("cliente_nombre") or "").strip()
        telefono = (request.POST.get("telefono") or "").strip()
        sector = (request.POST.get("sector") or "").strip()
        tipo = (request.POST.get("tipo") or "AVISO").strip().upper()
        medio_envio = (request.POST.get("medio_envio") or "IMPRESA").strip().upper()
        fecha_envio_raw = (request.POST.get("fecha_envio") or "").strip()
        estado = (request.POST.get("estado") or "ENVIADA").strip().upper()
        fecha_seguimiento_raw = (request.POST.get("fecha_seguimiento") or "").strip()
        nota = (request.POST.get("nota") or "").strip()

        fecha_envio = hoy
        fecha_seguimiento = None
        if not id_sn:
            error_message = "Debes seleccionar un cliente."
        elif not cliente_nombre:
            error_message = "El nombre del cliente es obligatorio."
        elif not nota:
            error_message = "La nota o detalle es obligatorio."
        else:
            if fecha_envio_raw:
                try:
                    fecha_envio = timezone.datetime.strptime(fecha_envio_raw, "%Y-%m-%d").date()
                except ValueError:
                    error_message = "La fecha de envio no es valida."
            if not error_message and fecha_seguimiento_raw:
                try:
                    fecha_seguimiento = timezone.datetime.strptime(fecha_seguimiento_raw, "%Y-%m-%d").date()
                except ValueError:
                    error_message = "La fecha de seguimiento no es valida."

        if not error_message:
            if carta_id:
                carta = CobroCartaEnviada.objects.filter(id_carta=carta_id).first()
                if not carta:
                    error_message = "La carta seleccionada no existe."
                else:
                    carta.id_sn = id_sn
                    carta.cliente_nombre = cliente_nombre
                    carta.telefono = telefono
                    carta.sector = sector
                    carta.tipo = tipo
                    carta.medio_envio = medio_envio
                    carta.fecha_envio = fecha_envio
                    carta.estado = estado
                    carta.fecha_seguimiento = fecha_seguimiento
                    carta.nota = nota
                    carta.save()
                    transaction.on_commit(
                        lambda: broadcast_notification_refresh(reason="sent-letter-updated")
                    )
                    return redirect("cobros:cartas_enviadas")
            else:
                CobroCartaEnviada.objects.create(
                    id_sn=id_sn,
                    cliente_nombre=cliente_nombre,
                    telefono=telefono,
                    sector=sector,
                    tipo=tipo,
                    medio_envio=medio_envio,
                    fecha_envio=fecha_envio,
                    estado=estado,
                    fecha_seguimiento=fecha_seguimiento,
                    nota=nota,
                    creado_por_id=usuario_id,
                )
                transaction.on_commit(
                    lambda: broadcast_notification_refresh(reason="sent-letter-created")
                )
                success_message = "Carta enviada guardada correctamente."

        selected = {
            "id_carta": carta_id,
            "id_sn": id_sn,
            "cliente_nombre": cliente_nombre,
            "telefono": telefono,
            "sector": sector,
            "tipo": tipo,
            "medio_envio": medio_envio,
            "fecha_envio": fecha_envio_raw,
            "estado": estado,
            "fecha_seguimiento": fecha_seguimiento_raw,
            "nota": nota,
        }
    elif edit_id:
        selected = CobroCartaEnviada.objects.filter(id_carta=edit_id).first()

    cartas = CobroCartaEnviada.objects.all().order_by("estado", "-fecha_envio", "-fecha_creacion")
    if q:
        cartas = cartas.filter(
            Q(cliente_nombre__icontains=q)
            | Q(id_sn__icontains=q)
            | Q(telefono__icontains=q)
            | Q(sector__icontains=q)
            | Q(tipo__icontains=q)
            | Q(medio_envio__icontains=q)
            | Q(nota__icontains=q)
        )
    if estado_filtro:
        cartas = cartas.filter(estado=estado_filtro)

    cartas = list(cartas)
    usuario_ids = [carta.creado_por_id for carta in cartas if carta.creado_por_id]
    usuarios_lookup = {}
    if usuario_ids:
        usuarios_lookup = {
            row["id_usuario"]: (row.get("nombre") or row.get("usuario") or str(row["id_usuario"]))
            for row in Usuario.objects.filter(id_usuario__in=list(dict.fromkeys(usuario_ids))).values(
                "id_usuario",
                "usuario",
                "nombre",
            )
        }
    for carta in cartas:
        carta.registrado_por = usuarios_lookup.get(carta.creado_por_id, str(carta.creado_por_id or ""))

    seguimientos_hoy = CobroCartaEnviada.objects.filter(
        estado__in=["ENVIADA", "PROMESA"],
        fecha_seguimiento=hoy,
    ).order_by("cliente_nombre", "fecha_creacion")

    ctx["cartas"] = cartas
    ctx["seguimientos_hoy"] = seguimientos_hoy
    ctx["fecha_hoy"] = hoy
    ctx["selected_carta"] = selected
    ctx["carta_error"] = error_message
    ctx["carta_success"] = success_message
    ctx["carta_query"] = q
    ctx["carta_estado"] = estado_filtro
    ctx["tipo_options"] = [
        ("AVISO", "Carta de aviso"),
        ("SALDO", "Carta de saldo"),
        ("INTIMACION", "Intimacion"),
        ("RECORDATORIO", "Recordatorio"),
        ("OTRA", "Otra"),
    ]
    ctx["medio_options"] = [
        ("IMPRESA", "Impresa"),
        ("WHATSAPP", "WhatsApp"),
        ("EMAIL", "Email"),
        ("MENSAJERO", "Mensajero"),
        ("OTRO", "Otro"),
    ]
    ctx["estado_filtro_options"] = [
        ("ENVIADA", "Enviada"),
        ("PROMESA", "Promesa"),
        ("CUMPLIDA", "Cumplida"),
        ("CANCELADA", "Cancelada"),
    ]
    ctx["estado_options"] = ctx["estado_filtro_options"]
    return render(request, "cobros/cartas_enviadas.html", ctx)


@xframe_options_sameorigin
def estado_cuenta_print_view(request):
    auth_payload = _require_perm_or_denied(request, "cobros", "ver_estado_cuenta")
    if not isinstance(auth_payload, dict):
        return auth_payload

    id_sn = (request.GET.get("id_sn") or "").strip()
    embed = (request.GET.get("embed") or "").strip() == "1"
    ctx = _build_estado_cuenta_context(id_sn)
    ctx["auth_payload"] = auth_payload
    ctx["embed"] = embed
    return render(request, "prefacturas_app/estado_cuenta_print.html", ctx)


@xframe_options_sameorigin
def alertas_print_view(request):
    auth_payload = _require_perm_or_denied(request, "cobros", "ver_alertas")
    if not isinstance(auth_payload, dict):
        return auth_payload

    try:
        min_days = int((request.GET.get("dias_desde") or "0").strip() or "0")
    except ValueError:
        min_days = 0
    try:
        max_days_raw = (request.GET.get("dias_hasta") or "").strip()
        max_days = int(max_days_raw) if max_days_raw else None
    except ValueError:
        max_days = None
    try:
        sector_raw = (request.GET.get("sector") or "").strip()
        sector_id = int(sector_raw) if sector_raw else None
    except ValueError:
        sector_id = None

    vista = (request.GET.get("vista") or "").strip().lower()
    vista_compacta = (vista == "compacta")

    embed = (request.GET.get("embed") or "").strip() == "1"
    ctx = _build_alertas_context(min_days, max_days, sector_id, vista_compacta=vista_compacta)
    ctx["auth_payload"] = auth_payload
    ctx["embed"] = embed
    ctx["vista_compacta"] = vista_compacta
    return render(request, "cobros/alertas_print.html", ctx)


@xframe_options_sameorigin
def financiamientos_atraso_aviso_view(request):
    base = _base_context(request, page_title="Aviso de financiamiento", active_nav="cobros")
    if not base:
        return redirect("login")
    auth_payload = base["auth_payload"]
    if not _can_view_financiamientos_atraso(auth_payload["usuario_id"]):
        return render_denied(request, active_nav="cobros")

    no_doc = (request.GET.get("no_doc") or "").strip()
    embed = (request.GET.get("embed") or "").strip() == "1"
    ctx = _build_financiamiento_aviso_context(request, no_doc, auth_payload, embed=embed)
    ctx["auth_payload"] = auth_payload
    return render(request, "cobros/financiamiento_aviso_print.html", ctx)
