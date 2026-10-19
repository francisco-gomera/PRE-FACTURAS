from datetime import date, datetime
import json
from django.db import connection, transaction
from django.db.models import Q
from decimal import Decimal
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_GET, require_POST

from ajustes.permissions import has_perm
from ajustes.print_formats import get_print_format, get_print_format_label
from core.realtime import broadcast_notification_refresh
from core.views import _base_context, render_denied
from cartas.views import (
    _build_aviso_default_body_text,
    _build_empresa_payload,
    _load_firma_b64,
    _format_money_str,
)
from .models import CobroAcuerdo, CobroCartaEnviada, CobroCobrador, CobroCobradorCliente
from prefacturas_app.models_existing import MaestroSn, Usuario
from prefacturas_app.views import _get_open_ed_balance, _require_perm_or_denied
from caja.views import (
    _load_cxc_pendientes,
    _load_cxc_recibo_detalle,
    _build_cxc_recibo_print_payload,
    _build_cxc_receipt_template_context,
    _resolve_request_terminal,
    _pdf_render_receipt_page_ops,
    _build_pdf_document,
    _pdf_mm_to_pt,
    _to_decimal,
    _stringify_doc,
    _amount_to_spanish_words,
    _build_cxc_facturas_comment,
    _load_table_columns,
    _load_identity_columns,
    _pick_existing_column,
    _next_table_numeric_value,
    _load_cxc_active_payment_lookup,
    _load_prestamo_rows_by_doc,
    _resolve_factura_pending_for_payment,
    _normalize_result_row,
    _resolve_prestamo_balance,
    _pick_row_value,
    _assign_existing_values,
    _insert_dynamic_row,
    _calculate_det_recibo_sub_pdo,
    _create_cxc_ed_entries,
    _update_dynamic_row,
    _rebuild_det_prestamo_from_active_receipts,
    _sync_cab_prestamo_from_det,
    broadcast_cxc_document_status,
)


def _fmt_date(value):
    if not value:
        return ""
    if hasattr(value, "strftime"):
        return value.strftime("%d/%m/%Y")
    return str(value)


def _parse_date(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value).strip()[:10]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            pass
    return None


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


def _ensure_cobro_registro_pago_tables():
    with connection.cursor() as cursor:
        cursor.execute(
            """
            IF OBJECT_ID('COBRO_COBRADOR', 'U') IS NULL
            BEGIN
                CREATE TABLE COBRO_COBRADOR (
                    ID_COBRADOR INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
                    NOMBRE NVARCHAR(150) NOT NULL,
                    ID_SECTOR DECIMAL(18,0) NULL,
                    SECTOR_NOMBRE NVARCHAR(120) NOT NULL CONSTRAINT DF_COBRO_COBRADOR_SECTOR DEFAULT (''),
                    TELEFONO NVARCHAR(50) NOT NULL CONSTRAINT DF_COBRO_COBRADOR_TELEFONO DEFAULT (''),
                    ACTIVO BIT NOT NULL CONSTRAINT DF_COBRO_COBRADOR_ACTIVO DEFAULT (1),
                    CREADO_POR_ID BIGINT NULL,
                    FECHA_CREACION DATETIME2 NOT NULL CONSTRAINT DF_COBRO_COBRADOR_FECHA_CREACION DEFAULT (SYSDATETIME()),
                    FECHA_MODIFICACION DATETIME2 NOT NULL CONSTRAINT DF_COBRO_COBRADOR_FECHA_MODIFICACION DEFAULT (SYSDATETIME())
                );
            END

            IF OBJECT_ID('COBRO_COBRADOR_CLIENTE', 'U') IS NULL
            BEGIN
                CREATE TABLE COBRO_COBRADOR_CLIENTE (
                    ID_ASIGNACION INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
                    ID_COBRADOR INT NOT NULL CONSTRAINT FK_COBRO_COBRADOR_CLI FOREIGN KEY REFERENCES COBRO_COBRADOR(ID_COBRADOR) ON DELETE CASCADE,
                    ID_SN NVARCHAR(20) NOT NULL,
                    CLIENTE_NOMBRE NVARCHAR(200) NOT NULL CONSTRAINT DF_COBRO_COBRADOR_CLI_NOMBRE DEFAULT (''),
                    CEDULA NVARCHAR(30) NOT NULL CONSTRAINT DF_COBRO_COBRADOR_CLI_CEDULA DEFAULT (''),
                    APODO NVARCHAR(100) NOT NULL CONSTRAINT DF_COBRO_COBRADOR_CLI_APODO DEFAULT (''),
                    SECTOR NVARCHAR(120) NOT NULL CONSTRAINT DF_COBRO_COBRADOR_CLI_SECTOR DEFAULT (''),
                    ORDEN INT NOT NULL CONSTRAINT DF_COBRO_COBRADOR_CLI_ORDEN DEFAULT (0),
                    FECHA_ASIGNACION DATETIME2 NOT NULL CONSTRAINT DF_COBRO_COBRADOR_CLI_FECHA DEFAULT (SYSDATETIME()),
                    CONSTRAINT UQ_COBRO_COBRADOR_CLIENTE UNIQUE (ID_COBRADOR, ID_SN)
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


def _build_alertas_context(min_days, max_days=None, sector_id=None, vista_compacta=False, balance_min=None, balance_max=None, fecha_ult_pago=None):
    min_days = max(int(min_days or 0), 0)
    max_days = None if max_days in (None, "") else max(int(max_days), 0)
    if max_days is not None and max_days < min_days:
        min_days, max_days = max_days, min_days
    balance_min = None if balance_min in (None, "") else max(float(balance_min), 0.0)
    balance_max = None if balance_max in (None, "") else max(float(balance_max), 0.0)
    if balance_max is not None and balance_min is not None and balance_max < balance_min:
        balance_min, balance_max = balance_max, balance_min
    fecha_ult_pago_corte = _parse_date(fecha_ult_pago)
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
        if balance_min is not None:
            sql += " AND f.SALDO >= %s"
            params.append(balance_min)
        if balance_max is not None:
            sql += " AND f.SALDO <= %s"
            params.append(balance_max)
        sql += " ORDER BY ISNULL(t.DESCRIPCION, 'SIN SECTOR'), s.NOM_SOCIO, f.FECHA_DOC, f.ID_DOC"
        cursor.execute(sql, params)
        rows = cursor.fetchall()

    docs = [row[6] for row in rows if row[6] is not None]
    cuotas_by_doc = {}
    ult_pago_by_doc = {}
    ult_pago_date_by_doc = {}

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
                    ult_pago_date_by_doc[no_doc] = _parse_date(fecha_pago)

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
        if fecha_ult_pago_corte is not None:
            doc_ult_pago = ult_pago_date_by_doc.get(id_doc)
            if doc_ult_pago is not None and doc_ult_pago > fecha_ult_pago_corte:
                continue
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
        "balance_min": balance_min,
        "balance_max": balance_max,
        "has_balance_max": balance_max is not None,
        "fecha_ult_pago": fecha_ult_pago_corte,
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
        "registros_pagos": (
            has_perm(ctx["auth_payload"]["usuario_id"], "cobros", "ver_registros_pagos")
            or has_perm(ctx["auth_payload"]["usuario_id"], "cobros", "ver")
        ),
        "pagos_masivos": (
            has_perm(ctx["auth_payload"]["usuario_id"], "cobros", "ver_pagos_masivos")
            or has_perm(ctx["auth_payload"]["usuario_id"], "cobros", "ver_registros_pagos")
            or has_perm(ctx["auth_payload"]["usuario_id"], "cobros", "ver")
        ),
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
    balance_desde_raw = (request.GET.get("balance_desde") or "").strip()
    try:
        balance_desde = max(float(balance_desde_raw.replace(",", "")), 0.0) if balance_desde_raw else ""
    except ValueError:
        balance_desde = ""
    balance_hasta_raw = (request.GET.get("balance_hasta") or "").strip()
    try:
        balance_hasta = max(float(balance_hasta_raw.replace(",", "")), 0.0) if balance_hasta_raw else ""
    except ValueError:
        balance_hasta = ""
    fecha_ult_pago_raw = (request.GET.get("fecha_ult_pago") or "").strip()
    fecha_ult_pago_parsed = _parse_date(fecha_ult_pago_raw)
    fecha_ult_pago_default = fecha_ult_pago_parsed.strftime("%Y-%m-%d") if fecha_ult_pago_parsed else ""
    ctx["dias_desde_default"] = dias_desde
    ctx["dias_hasta_default"] = dias_hasta
    ctx["balance_desde_default"] = balance_desde
    ctx["balance_hasta_default"] = balance_hasta
    ctx["fecha_ult_pago_default"] = fecha_ult_pago_default
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

    balance_min_raw = (request.GET.get("balance_desde") or "").strip()
    try:
        balance_min = max(float(balance_min_raw.replace(",", "")), 0.0) if balance_min_raw else None
    except ValueError:
        balance_min = None

    balance_max_raw = (request.GET.get("balance_hasta") or "").strip()
    try:
        balance_max = max(float(balance_max_raw.replace(",", "")), 0.0) if balance_max_raw else None
    except ValueError:
        balance_max = None

    fecha_ult_pago_raw = (request.GET.get("fecha_ult_pago") or "").strip()
    fecha_ult_pago = _parse_date(fecha_ult_pago_raw)

    vista = (request.GET.get("vista") or "").strip().lower()
    vista_compacta = (vista == "compacta")

    embed = (request.GET.get("embed") or "").strip() == "1"
    ctx = _build_alertas_context(
        min_days,
        max_days,
        sector_id,
        vista_compacta=vista_compacta,
        balance_min=balance_min,
        balance_max=balance_max,
        fecha_ult_pago=fecha_ult_pago,
    )
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


def _load_cobrador_clientes(id_cobrador):
    _ensure_cobro_registro_pago_tables()
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT 
                c.ID_ASIGNACION,
                c.ID_SN,
                ISNULL(NULLIF(LTRIM(RTRIM(s.NOM_SOCIO)), ''), c.CLIENTE_NOMBRE) AS CLIENTE_NOMBRE,
                ISNULL(NULLIF(LTRIM(RTRIM(s.RNC_CED)), ''), c.CEDULA) AS CEDULA,
                ISNULL(NULLIF(LTRIM(RTRIM(s.CONTACTO)), ''), c.APODO) AS APODO,
                ISNULL(NULLIF(LTRIM(RTRIM(t.DESCRIPCION)), ''), c.SECTOR) AS SECTOR,
                c.ORDEN,
                c.FECHA_ASIGNACION
            FROM COBRO_COBRADOR_CLIENTE c
            LEFT JOIN MAESTRO_SN s ON s.ID_SN = c.ID_SN
            LEFT JOIN Territorio t ON t.ID_CODIGO = s.ID_SECTOR
            WHERE c.ID_COBRADOR = %s
            ORDER BY c.ORDEN ASC, s.NOM_SOCIO ASC
            """,
            [id_cobrador],
        )
        rows = cursor.fetchall()

    clientes = []
    for row in rows:
        clientes.append(
            {
                "id_asignacion": row[0],
                "id_sn": str(row[1] or "").strip(),
                "cliente_nombre": str(row[2] or "").strip(),
                "cedula": str(row[3] or "").strip() or str(row[1] or "").strip(),
                "apodo": str(row[4] or "").strip(),
                "sector": str(row[5] or "").strip(),
                "orden": row[6] or 0,
                "fecha_asignacion": row[7],
            }
        )
    return clientes


def api_clientes_sector_view(request):
    ctx = _base_context(request, page_title="Clientes por Sector", active_nav="cobros")
    if not ctx:
        return JsonResponse({"error": "No autenticado"}, status=401)
    usuario_id = ctx["auth_payload"]["usuario_id"]
    if not (has_perm(usuario_id, "cobros", "ver_registros_pagos") or has_perm(usuario_id, "cobros", "ver")):
        return JsonResponse({"error": "Permiso denegado"}, status=403)

    _ensure_cobro_registro_pago_tables()
    sector_id = (request.GET.get("sector_id") or "").strip()
    id_cobrador = (request.GET.get("id_cobrador") or "").strip()

    if not sector_id:
        return JsonResponse({"results": []})

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT s.ID_SN, s.NOM_SOCIO, s.RNC_CED, s.CONTACTO, ISNULL(t.DESCRIPCION, '') AS SECTOR
            FROM MAESTRO_SN s
            LEFT JOIN Territorio t ON t.ID_CODIGO = s.ID_SECTOR
            WHERE s.ID_SECTOR = %s
            ORDER BY s.NOM_SOCIO ASC
            """,
            [sector_id],
        )
        rows = cursor.fetchall()

    assigned_ids = set()
    if id_cobrador:
        assigned_ids = set(
            CobroCobradorCliente.objects.filter(cobrador_id=id_cobrador).values_list("id_sn", flat=True)
        )

    results = []
    for r in rows:
        id_sn = str(r[0] or "").strip()
        results.append(
            {
                "id_sn": id_sn,
                "nom_socio": str(r[1] or "").strip(),
                "rnc_ced": str(r[2] or "").strip(),
                "contacto": str(r[3] or "").strip(),
                "sector": str(r[4] or "").strip(),
                "ya_asignado": id_sn in assigned_ids,
            }
        )

    return JsonResponse({"results": results})


def registros_pagos_view(request, id_cobrador=None):
    ctx = _base_context(request, page_title="Registros de Pagos", active_nav="cobros")
    if not ctx:
        return redirect("login")
    usuario_id = ctx["auth_payload"]["usuario_id"]
    if not (has_perm(usuario_id, "cobros", "ver_registros_pagos") or has_perm(usuario_id, "cobros", "ver")):
        return render_denied(request, active_nav="cobros")

    _ensure_cobro_registro_pago_tables()
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    error_message = ""
    success_message = ""

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip().lower()

        if action == "save_cobrador":
            cobrador_id = (request.POST.get("id_cobrador") or "").strip()
            nombre = (request.POST.get("nombre") or "").strip()
            id_sector_raw = (request.POST.get("id_sector") or "").strip()
            telefono = (request.POST.get("telefono") or "").strip()
            activo = request.POST.get("activo", "1").strip() in ("1", "true", "True", "on")

            if not nombre:
                if is_ajax:
                    return JsonResponse({"ok": False, "error": "El nombre del cobrador es obligatorio."}, status=400)
                error_message = "El nombre del cobrador es obligatorio."
            else:
                sector_nombre = ""
                id_sector = None
                if id_sector_raw:
                    try:
                        id_sector = int(id_sector_raw)
                        with connection.cursor() as cursor:
                            cursor.execute("SELECT DESCRIPCION FROM Territorio WHERE ID_CODIGO = %s", [id_sector])
                            row = cursor.fetchone()
                            if row and row[0]:
                                sector_nombre = str(row[0]).strip()
                    except (ValueError, TypeError):
                        id_sector = None

                if cobrador_id:
                    cobrador = CobroCobrador.objects.filter(id_cobrador=cobrador_id).first()
                    if cobrador:
                        cobrador.nombre = nombre
                        cobrador.id_sector = id_sector
                        cobrador.sector_nombre = sector_nombre
                        cobrador.telefono = telefono
                        cobrador.activo = activo
                        cobrador.save()
                        if is_ajax:
                            return JsonResponse({"ok": True, "id_cobrador": cobrador.id_cobrador, "message": "Cobrador actualizado exitosamente."})
                        return redirect("cobros:registros_pagos_cobrador", id_cobrador=cobrador.id_cobrador)
                else:
                    cobrador = CobroCobrador.objects.create(
                        nombre=nombre,
                        id_sector=id_sector,
                        sector_nombre=sector_nombre,
                        telefono=telefono,
                        activo=activo,
                        creado_por_id=usuario_id,
                    )
                    if is_ajax:
                        return JsonResponse({"ok": True, "id_cobrador": cobrador.id_cobrador, "message": "Cobrador registrado exitosamente."})
                    return redirect("cobros:registros_pagos_cobrador", id_cobrador=cobrador.id_cobrador)

        elif action == "delete_cobrador":
            cobrador_id = (request.POST.get("id_cobrador") or "").strip()
            if cobrador_id:
                CobroCobrador.objects.filter(id_cobrador=cobrador_id).delete()
                if is_ajax:
                    return JsonResponse({"ok": True, "message": "Cobrador eliminado exitosamente."})
                return redirect("cobros:registros_pagos")

        elif action == "add_cliente":
            cobrador_id = (request.POST.get("id_cobrador") or "").strip()
            id_sn = (request.POST.get("id_sn") or "").strip()
            if not cobrador_id or not id_sn:
                if is_ajax:
                    return JsonResponse({"ok": False, "error": "Datos incompletos."}, status=400)
                error_message = "Datos incompletos."
            else:
                cobrador = CobroCobrador.objects.filter(id_cobrador=cobrador_id).first()
                if not cobrador:
                    if is_ajax:
                        return JsonResponse({"ok": False, "error": "Cobrador no encontrado."}, status=404)
                    error_message = "Cobrador no encontrado."
                else:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            """
                            SELECT s.ID_SN, s.NOM_SOCIO, s.RNC_CED, s.CONTACTO, ISNULL(t.DESCRIPCION, '') AS SECTOR
                            FROM MAESTRO_SN s
                            LEFT JOIN Territorio t ON t.ID_CODIGO = s.ID_SECTOR
                            WHERE s.ID_SN = %s
                            """,
                            [id_sn],
                        )
                        row = cursor.fetchone()
                    if not row:
                        if is_ajax:
                            return JsonResponse({"ok": False, "error": "Cliente no encontrado en MAESTRO_SN."}, status=404)
                        error_message = "Cliente no encontrado."
                    else:
                        obj, created = CobroCobradorCliente.objects.update_or_create(
                            cobrador=cobrador,
                            id_sn=id_sn,
                            defaults={
                                "cliente_nombre": str(row[1] or "").strip(),
                                "cedula": str(row[2] or "").strip(),
                                "apodo": str(row[3] or "").strip(),
                                "sector": str(row[4] or "").strip(),
                            },
                        )
                        if is_ajax:
                            clientes = _load_cobrador_clientes(cobrador.id_cobrador)
                            return JsonResponse({"ok": True, "message": "Cliente agregado exitosamente.", "clientes": clientes, "total": len(clientes)})
                        return redirect("cobros:registros_pagos_cobrador", id_cobrador=cobrador.id_cobrador)

        elif action == "add_clientes_sector":
            cobrador_id = (request.POST.get("id_cobrador") or "").strip()
            sector_id = (request.POST.get("sector_id") or "").strip()
            selected_sn_list = request.POST.getlist("selected_sn")
            cobrador = CobroCobrador.objects.filter(id_cobrador=cobrador_id).first()
            if not cobrador:
                if is_ajax:
                    return JsonResponse({"ok": False, "error": "Cobrador no encontrado."}, status=404)
                error_message = "Cobrador no encontrado."
            else:
                if selected_sn_list:
                    placeholders = ", ".join(["%s"] * len(selected_sn_list))
                    sql = f"""
                        SELECT s.ID_SN, s.NOM_SOCIO, s.RNC_CED, s.CONTACTO, ISNULL(t.DESCRIPCION, '') AS SECTOR
                        FROM MAESTRO_SN s
                        LEFT JOIN Territorio t ON t.ID_CODIGO = s.ID_SECTOR
                        WHERE s.ID_SN IN ({placeholders})
                    """
                    params = selected_sn_list
                elif sector_id:
                    sql = """
                        SELECT s.ID_SN, s.NOM_SOCIO, s.RNC_CED, s.CONTACTO, ISNULL(t.DESCRIPCION, '') AS SECTOR
                        FROM MAESTRO_SN s
                        LEFT JOIN Territorio t ON t.ID_CODIGO = s.ID_SECTOR
                        WHERE s.ID_SECTOR = %s
                    """
                    params = [sector_id]
                else:
                    sql = ""
                    params = []

                agregados = 0
                if sql:
                    with connection.cursor() as cursor:
                        cursor.execute(sql, params)
                        rows = cursor.fetchall()
                    existing_sn = set(
                        CobroCobradorCliente.objects.filter(cobrador=cobrador).values_list("id_sn", flat=True)
                    )
                    to_create = []
                    for r in rows:
                        sn = str(r[0] or "").strip()
                        if sn not in existing_sn:
                            to_create.append(
                                CobroCobradorCliente(
                                    cobrador=cobrador,
                                    id_sn=sn,
                                    cliente_nombre=str(r[1] or "").strip(),
                                    cedula=str(r[2] or "").strip(),
                                    apodo=str(r[3] or "").strip(),
                                    sector=str(r[4] or "").strip(),
                                )
                            )
                            existing_sn.add(sn)
                    if to_create:
                        CobroCobradorCliente.objects.bulk_create(to_create)
                        agregados = len(to_create)

                if is_ajax:
                    clientes = _load_cobrador_clientes(cobrador.id_cobrador)
                    return JsonResponse({"ok": True, "message": f"Se agregaron {agregados} clientes.", "clientes": clientes, "total": len(clientes)})
                return redirect("cobros:registros_pagos_cobrador", id_cobrador=cobrador.id_cobrador)

        elif action == "remove_cliente":
            cobrador_id = (request.POST.get("id_cobrador") or "").strip()
            id_asignacion = (request.POST.get("id_asignacion") or "").strip()
            id_sn = (request.POST.get("id_sn") or "").strip()
            if id_asignacion:
                CobroCobradorCliente.objects.filter(id_asignacion=id_asignacion).delete()
            elif cobrador_id and id_sn:
                CobroCobradorCliente.objects.filter(cobrador_id=cobrador_id, id_sn=id_sn).delete()

            if is_ajax:
                clientes = _load_cobrador_clientes(cobrador_id) if cobrador_id else []
                return JsonResponse({"ok": True, "message": "Cliente eliminado de la asignación.", "clientes": clientes, "total": len(clientes)})
            if cobrador_id:
                return redirect("cobros:registros_pagos_cobrador", id_cobrador=cobrador_id)
            return redirect("cobros:registros_pagos")

        elif action == "clear_clientes":
            cobrador_id = (request.POST.get("id_cobrador") or "").strip()
            if cobrador_id:
                CobroCobradorCliente.objects.filter(cobrador_id=cobrador_id).delete()
            if is_ajax:
                return JsonResponse({"ok": True, "message": "Se han quitado todos los clientes.", "clientes": [], "total": 0})
            if cobrador_id:
                return redirect("cobros:registros_pagos_cobrador", id_cobrador=cobrador_id)
            return redirect("cobros:registros_pagos")

    # GET handling:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT 
                c.ID_COBRADOR,
                c.NOMBRE,
                c.ID_SECTOR,
                c.SECTOR_NOMBRE,
                c.TELEFONO,
                c.ACTIVO,
                COUNT(cl.ID_ASIGNACION) AS TOTAL_CLIENTES
            FROM COBRO_COBRADOR c
            LEFT JOIN COBRO_COBRADOR_CLIENTE cl ON cl.ID_COBRADOR = c.ID_COBRADOR
            GROUP BY c.ID_COBRADOR, c.NOMBRE, c.ID_SECTOR, c.SECTOR_NOMBRE, c.TELEFONO, c.ACTIVO
            ORDER BY c.ACTIVO DESC, c.NOMBRE ASC
            """
        )
        cob_rows = cursor.fetchall()

    cobradores = []
    for r in cob_rows:
        cobradores.append(
            {
                "id_cobrador": r[0],
                "nombre": str(r[1] or "").strip(),
                "id_sector": int(r[2]) if r[2] is not None else None,
                "sector_nombre": str(r[3] or "").strip(),
                "telefono": str(r[4] or "").strip(),
                "activo": bool(r[5]),
                "total_clientes": r[6] or 0,
            }
        )

    selected_cobrador = None
    target_id = id_cobrador or request.GET.get("cobrador_id")
    if target_id:
        try:
            target_id_int = int(target_id)
            selected_cobrador = next((c for c in cobradores if c["id_cobrador"] == target_id_int), None)
        except (ValueError, TypeError):
            selected_cobrador = None

    if not selected_cobrador and cobradores:
        selected_cobrador = cobradores[0]

    clientes_asignados = []
    if selected_cobrador:
        clientes_asignados = _load_cobrador_clientes(selected_cobrador["id_cobrador"])

    ctx["cobradores"] = cobradores
    ctx["selected_cobrador"] = selected_cobrador
    ctx["clientes_asignados"] = clientes_asignados
    ctx["total_clientes"] = len(clientes_asignados)
    ctx["sectores"] = _load_sectores()
    ctx["error_message"] = error_message
    ctx["success_message"] = success_message
    return render(request, "cobros/registros_pagos.html", ctx)


@xframe_options_sameorigin
def registros_pagos_impresion_view(request, id_cobrador):
    ctx = _base_context(request, page_title="Registro Pago Clientes", active_nav="cobros")
    if not ctx:
        return redirect("login")
    usuario_id = ctx["auth_payload"]["usuario_id"]
    if not (has_perm(usuario_id, "cobros", "ver_registros_pagos") or has_perm(usuario_id, "cobros", "ver")):
        return render_denied(request, active_nav="cobros")

    _ensure_cobro_registro_pago_tables()
    cobrador = CobroCobrador.objects.filter(id_cobrador=id_cobrador).first()
    if not cobrador:
        return redirect("cobros:registros_pagos")

    clientes = _load_cobrador_clientes(id_cobrador)
    empresa = _build_empresa_payload(ctx)

    sector_display = ""
    if cobrador.sector_nombre and cobrador.sector_nombre.strip():
        sector_display = cobrador.sector_nombre.strip().upper()
    elif clientes and clientes[0].get("sector"):
        sector_display = clientes[0]["sector"].strip().upper()
    else:
        sector_display = "TODOS LOS SECTORES"

    ctx["cobrador"] = cobrador
    ctx["clientes"] = clientes
    ctx["total_clientes"] = len(clientes)
    ctx["sector_display"] = sector_display
    ctx["empresa"] = empresa
    ctx["fecha_impresion"] = timezone.localdate()
    return render(request, "cobros/registros_pagos_print.html", ctx)


def _load_cobrador_clientes_with_pendientes(id_cobrador):
    clientes = _load_cobrador_clientes(id_cobrador)
    result = []
    for cl in clientes:
        id_sn = str(cl.get("id_sn") or "").strip()
        pendientes = []
        if id_sn:
            try:
                pendientes = _load_cxc_pendientes(id_sn)
            except Exception:
                pendientes = []
        
        total_pendiente = sum(_to_float(item.get("balance_doc")) for item in pendientes)
        result.append({
            **cl,
            "cuentas_pendientes": pendientes,
            "total_pendiente": total_pendiente,
            "total_cuentas": len(pendientes),
            "tiene_pendientes": len(pendientes) > 0,
        })
    return result


def _save_pago_masivo_cliente(
    cursor,
    *,
    id_sn,
    items,
    usuario_id,
    usuario_nombre,
    cobrador_nombre="",
    terminal="PAGOS-MASIVOS",
    now=None,
    local_date=None,
):
    valid_items = []
    for item in items:
        monto_pago = _to_decimal(item.get("monto_pago"))
        if monto_pago > Decimal("0.001"):
            valid_items.append({
                **item,
                "monto_pago": monto_pago,
            })
    if not valid_items:
        return None

    if now is None:
        now = timezone.localtime()
    if local_date is None:
        local_date = timezone.localdate()

    periodo_cont = str(local_date.month)
    ejercicio = local_date.year

    # Fecha de pago requerida para venc, cont y aplic
    raw_fecha_pago = valid_items[0].get("fecha_pago")
    fecha_pago_val = _parse_date(raw_fecha_pago) or local_date

    efectivo = Decimal("0")
    transferencia = Decimal("0")
    refs = []
    for item in valid_items:
        monto = item["monto_pago"]
        metodo = str(item.get("metodo_pago") or "Efectivo").strip().title()
        if metodo == "Transferencia":
            transferencia += monto
            ref = str(item.get("no_transferencia") or "").strip()
            if ref:
                refs.append(ref)
        else:
            efectivo += monto

    cash_total = efectivo + transferencia
    no_transferencia_str = ", ".join(dict.fromkeys(refs)) if refs else ""

    cursor.execute(
        """
        SELECT TOP 1
            ISNULL(BLOQUEADO, 'N'),
            ISNULL(NOM_SOCIO, ''),
            ISNULL(RNC_CED, ''),
            ISNULL(CTA_ASOCIADA, ''),
            ISNULL(CONTACTO, '')
        FROM MAESTRO_SN
        WHERE ID_SN = %s
        """,
        [id_sn],
    )
    cl_row = cursor.fetchone()
    if not cl_row:
        raise ValueError(f"No se encontró el socio de negocio con código {id_sn}.")
    if str(cl_row[0] or "").strip().upper() == "Y":
        raise ValueError(f"El cliente {id_sn} está bloqueado.")

    nombre_cliente = str(cl_row[1] or "").strip()
    rnc_ced = str(cl_row[2] or "").strip()
    cta_asociada = str(cl_row[3] or "").strip() or "11020101"
    apodo = str(cl_row[4] or "").strip()
    balance_cliente = _to_decimal(_get_open_ed_balance(id_sn))

    cab_columns = _load_table_columns("CAB_RECIBO_INGRESO")
    det_columns = _load_table_columns("DET_RECIBO_INGRESO")
    cab_identity_columns = _load_identity_columns("CAB_RECIBO_INGRESO")
    cab_key_col = _pick_existing_column(cab_columns, "ID_RECIBO", "ID_DOC", "NO_RECIBO", "NO_DOC")
    cab_no_recibo_col = _pick_existing_column(cab_columns, "NO_RECIBO", "NO_DOC")
    if not cab_key_col and not cab_no_recibo_col:
        raise ValueError("No se pudo determinar la clave de CAB_RECIBO_INGRESO.")

    next_no_recibo = None
    if cab_no_recibo_col and cab_no_recibo_col not in cab_identity_columns:
        next_no_recibo = _next_table_numeric_value(cursor, "CAB_RECIBO_INGRESO", cab_no_recibo_col)
    elif cab_key_col and cab_key_col not in cab_identity_columns:
        next_no_recibo = _next_table_numeric_value(cursor, "CAB_RECIBO_INGRESO", cab_key_col)

    detail_by_doc = {}
    detail_balance_hint_by_doc = {}
    for item in valid_items:
        doc_key = str(item.get("no_doc") or "").strip()
        detail_by_doc[doc_key] = detail_by_doc.get(doc_key, Decimal("0")) + item["monto_pago"]
        balance_hint = max(
            _to_decimal(item.get("balance_total_factura")),
            _to_decimal(item.get("balance_doc")),
        )
        current_hint = detail_balance_hint_by_doc.get(doc_key, Decimal("0"))
        detail_balance_hint_by_doc[doc_key] = max(current_hint, balance_hint)

    doc_numbers = list(detail_by_doc.keys())
    placeholders = ", ".join(["%s"] * len(doc_numbers))
    cursor.execute(
        f"""
        SELECT ID_DOC, SALDO, TOTAL_DOC, EST_DOC, ABONO, ISNULL(COMENTARIO, '')
        FROM CAB_FACTURA WITH (UPDLOCK, HOLDLOCK)
        WHERE CAST(ID_DOC AS VARCHAR(50)) IN ({placeholders})
        """,
        doc_numbers,
    )
    factura_lookup = {
        _stringify_doc(row[0]): {
            "saldo": _to_decimal(row[1]),
            "total_doc": _to_decimal(row[2]),
            "est_doc": str(row[3] or "").strip(),
            "abono": _to_decimal(row[4]),
            "comentario": str(row[5] or "").strip(),
        }
        for row in cursor.fetchall()
    }
    pagos_lookup = _load_cxc_active_payment_lookup(doc_numbers)
    pagos_por_doc = pagos_lookup.get("by_doc") or {}
    prestamo_rows_by_doc = _load_prestamo_rows_by_doc(doc_numbers)

    for doc_number, applied_amount in detail_by_doc.items():
        factura_row = factura_lookup.get(doc_number)
        if not factura_row:
            raise ValueError(f"No se encontró la factura {doc_number} del cliente {id_sn}.")
        pending_actual = _resolve_factura_pending_for_payment(
            factura_row,
            pagos_doc=pagos_por_doc.get(doc_number),
            balance_hint=detail_balance_hint_by_doc.get(doc_number),
            cuotas_rows=prestamo_rows_by_doc.get(doc_number),
        )
        factura_row["pending_actual"] = pending_actual
        factura_row["abono_base"] = max(
            factura_row.get("abono", Decimal("0")),
            max(factura_row.get("total_doc", Decimal("0")) - pending_actual, Decimal("0")),
        )
        if applied_amount > pending_actual + Decimal("0.01"):
            raise ValueError(f"El monto asignado ({applied_amount}) a la factura {doc_number} excede su saldo pendiente ({pending_actual}).")

    prestamo_columns = _load_table_columns("DET_PRESTAMO")
    prestamo_has_balance = "BALANCE" in prestamo_columns
    prestamo_abono_cuota_col = _pick_existing_column(
        prestamo_columns,
        "ABONO_CUOTA",
        "ABONOCUOTA",
        "ABONO_CUENTA",
        "ABONOCUENTA",
    )
    prestamo_no_recibo_col = _pick_existing_column(prestamo_columns, "NORECIBO", "NO_RECIBO")

    for item in valid_items:
        if not bool(item.get("tiene_financiamiento")):
            continue
        no_doc = str(item.get("no_doc") or "").strip()
        no_cuota = str(item.get("no_cuota") or "").strip()
        if not no_doc or not no_cuota:
            continue
        cursor.execute(
            """
            SELECT TOP 1 *
            FROM DET_PRESTAMO WITH (UPDLOCK, HOLDLOCK)
            WHERE CAST(NO_DOC AS VARCHAR(50)) = %s
              AND CAST(NO_CUOTA AS VARCHAR(50)) = %s
            """,
            [no_doc, no_cuota],
        )
        raw_prestamo_row = cursor.fetchone()
        if not raw_prestamo_row:
            raise ValueError(f"No se encontró la cuota {no_cuota} de la factura {no_doc}.")
        prestamo_row = _normalize_result_row([col[0] for col in cursor.description], raw_prestamo_row)
        current_balance = _resolve_prestamo_balance(
            _pick_row_value(prestamo_row, "BALANCE"),
            cuota=_pick_row_value(prestamo_row, "CUOTA", "MONTO_CUOTA", "VALOR_CUOTA"),
            abono_cuota=_pick_row_value(
                prestamo_row,
                prestamo_abono_cuota_col,
                "ABONO_CUOTA",
                "ABONOCUOTA",
                "ABONO_CUENTA",
                "ABONOCUENTA",
            ),
        )
        applied_amount = item["monto_pago"]
        if applied_amount > current_balance + Decimal("0.01"):
            raise ValueError(f"El monto aplicado ({applied_amount}) a la cuota {no_cuota} de la factura {no_doc} excede el saldo pendiente ({current_balance}).")

    total_letra = _amount_to_spanish_words(cash_total)
    comentario_pago = f"Pago masivo cobrador: {cobrador_nombre}".strip() if cobrador_nombre else "Pago masivo cobrador"
    comentario_ed = _build_cxc_facturas_comment(
        [
            {
                "no_doc": item.get("no_doc"),
                "no_cuota": item.get("no_cuota"),
                "pago_abono": item.get("monto_pago"),
            }
            for item in valid_items
        ],
        close_account=False,
    )

    header_values = {}
    if cab_key_col and cab_key_col not in cab_identity_columns:
        _assign_existing_values(header_values, cab_columns, next_no_recibo, cab_key_col)
    if next_no_recibo is not None:
        _assign_existing_values(header_values, cab_columns, next_no_recibo, "NO_RECIBO", "NO_DOC")

    # Requisito explícito: la fecha de pago debe ponerse tanto en fecha venc. fecha cont y fecha aplic.
    _assign_existing_values(header_values, cab_columns, fecha_pago_val, "FECHA_CONT", "F_CONT")
    _assign_existing_values(header_values, cab_columns, fecha_pago_val, "FECHA_VENC", "F_VENC")
    _assign_existing_values(header_values, cab_columns, fecha_pago_val, "FECHA_DOC", "FECHA_APLIC")
    _assign_existing_values(header_values, cab_columns, fecha_pago_val, "FECHA_PAGO")

    _assign_existing_values(header_values, cab_columns, id_sn, "ID_SN", "CLIENTE", "COD_CLIENTE")
    _assign_existing_values(header_values, cab_columns, nombre_cliente, "NOM_SN", "NOM_SOCIO", "NOMBRE", "NOM_CLIENTE")
    _assign_existing_values(header_values, cab_columns, apodo, "CONTACTO", "APODO")
    _assign_existing_values(header_values, cab_columns, rnc_ced, "RNC_CED", "RNC", "CEDULA")
    _assign_existing_values(header_values, cab_columns, "RD$", "MONEDA", "MON_DOC", "MONEDA_PAGO")
    _assign_existing_values(header_values, cab_columns, "RD$", "MONPAGO", "MON_PAGO")
    _assign_existing_values(header_values, cab_columns, Decimal("1.0000"), "TASA_PAGO", "TASA")
    _assign_existing_values(header_values, cab_columns, efectivo, "IMP_EFECTIVO", "EFECTIVO", "MONTO_EFECTIVO", "PAGO_EFECTIVO")
    _assign_existing_values(header_values, cab_columns, transferencia, "IMP_TRANSF", "TRANSFERENCIA", "MONTO_TRANSFERENCIA", "PAGO_TRANSFERENCIA")
    _assign_existing_values(header_values, cab_columns, cash_total, "TOTAL_COBRO", "TOTAL_DOC", "IMPORTE", "MONTO")
    _assign_existing_values(header_values, cab_columns, Decimal("0"), "TOTAL_MORA", "MORA", "CARGO")
    _assign_existing_values(header_values, cab_columns, Decimal("0"), "TOTAL_DESCTO", "DESC_AVANCE", "DESCUENTO", "AVANCE")
    _assign_existing_values(header_values, cab_columns, Decimal("0"), "TOTAL_RET", "RETENCION", "RET")
    _assign_existing_values(header_values, cab_columns, "Y", "IMPRESO")
    _assign_existing_values(header_values, cab_columns, "41020102", "CTA_DESCTO", "CTA_DESC_RET", "CTA_DESC")
    _assign_existing_values(header_values, cab_columns, "DESCUENTOS EN CXC", "CTA_DESC_RET_DESC", "DESC_CTA_DESC_RET", "NOM_CTA_DESC")
    _assign_existing_values(header_values, cab_columns, balance_cliente, "BALANCE")
    _assign_existing_values(header_values, cab_columns, total_letra, "TOTAL_LETRA")
    _assign_existing_values(header_values, cab_columns, "11010101", "CTA_CAJA", "CTA_BANCO_CAJA", "CTA_COBRO", "CTA_INGRESO")
    _assign_existing_values(header_values, cab_columns, "Caja General", "CTA_CAJA_DESC", "DESC_CTA_CAJA", "NOM_CTA_CAJA")
    _assign_existing_values(header_values, cab_columns, "11010101", "CTA_EFECTIVO", "CTA_CHEQUE", "CTA_TARJETA")
    _assign_existing_values(header_values, cab_columns, "Caja General", "NOM_CTA", "NOM_CTA3")
    _assign_existing_values(header_values, cab_columns, "11010101", "CTA_TRANSF")
    _assign_existing_values(header_values, cab_columns, "Caja General", "NOM_CTA2")
    _assign_existing_values(header_values, cab_columns, no_transferencia_str, "NO_TRANSF", "NO_TRANSFERENCIA", "REFERENCIA_TRANSF", "NO_REF", "REFERENCIA")
    _assign_existing_values(header_values, cab_columns, cta_asociada, "CTA_ASOCIADA", "CTA_ANTICIPO")
    _assign_existing_values(header_values, cab_columns, "41040120", "CTA_MORA")
    _assign_existing_values(header_values, cab_columns, "41040105", "CTA_DIF_CAMBIO")
    _assign_existing_values(header_values, cab_columns, "11010202P", "CTA_PRIMAT", "CTA_PRIMAE", "CTA_PRIMAC", "CTA_PRIMAJ")
    _assign_existing_values(header_values, cab_columns, comentario_pago, "COMENTARIO", "OBSERVACION")
    _assign_existing_values(header_values, cab_columns, "Abierto", "ESTATUS", "EST_DOC", "ESTADO")
    _assign_existing_values(header_values, cab_columns, usuario_id, "ID_USUARIO", "USUARIO_ID")
    _assign_existing_values(header_values, cab_columns, usuario_nombre, "USUARIO", "USUARIO_NOMBRE")
    _assign_existing_values(header_values, cab_columns, terminal, "TERMINAL")
    _assign_existing_values(header_values, cab_columns, local_date, "FECHA_CREACION")
    _assign_existing_values(header_values, cab_columns, now, "FECHA_ACT")
    _assign_existing_values(header_values, cab_columns, periodo_cont, "PERIODO_CONT")
    _assign_existing_values(header_values, cab_columns, ejercicio, "EJERCICIO")

    inserted_recibo_id = _insert_dynamic_row(
        cursor,
        "CAB_RECIBO_INGRESO",
        cab_columns,
        header_values,
        output_column=cab_key_col or cab_no_recibo_col,
        skip_columns=cab_identity_columns,
    )
    inserted_recibo_id = _stringify_doc(inserted_recibo_id)
    next_no_recibo_text = _stringify_doc(next_no_recibo)
    recibo_id = inserted_recibo_id or next_no_recibo_text
    no_recibo = next_no_recibo_text or inserted_recibo_id

    det_line_col = _pick_existing_column(det_columns, "NO_LINEA", "LINEA", "NO_ITEM", "ORDEN")
    next_det_line = 0
    if det_line_col:
        cursor.execute(
            f"SELECT ISNULL(MAX(TRY_CAST([{det_line_col}] AS BIGINT)), 0) FROM DET_RECIBO_INGRESO WITH (UPDLOCK, HOLDLOCK)"
        )
        row = cursor.fetchone()
        next_det_line = int(row[0] or 0)

    for item in valid_items:
        next_det_line += 1
        monto_total_factura = _to_decimal(item.get("monto_doc"))
        cuota_linea = _to_decimal(item.get("cuota"))
        balance_doc_linea = _to_decimal(item.get("balance_doc"))
        monto_pagado_linea = item["monto_pago"]
        total_recibo_linea = max(balance_doc_linea - monto_pagado_linea, Decimal("0"))
        saldo_venc_linea = monto_pagado_linea + total_recibo_linea
        sub_pdo_linea = _calculate_det_recibo_sub_pdo(
            monto_pagado_linea,
            monto_total_factura,
            cuota_linea,
        )
        vencida = "*" if int(_to_float(item.get("dias"))) > 0 else ""

        detail_values = {}
        _assign_existing_values(detail_values, det_columns, recibo_id, "ID_RECIBO")
        _assign_existing_values(detail_values, det_columns, no_recibo, "NO_RECIBO")
        if det_line_col:
            _assign_existing_values(detail_values, det_columns, next_det_line, det_line_col)
        _assign_existing_values(detail_values, det_columns, str(item.get("no_doc") or "").strip(), "NO_DOC", "ID_DOC", "DOCUMENTO", "FACTURA")
        _assign_existing_values(detail_values, det_columns, str(item.get("td") or "").strip(), "TIPO_DOC", "TD", "CLASE_DOC", "TIPO")
        _assign_existing_values(detail_values, det_columns, fecha_pago_val, "FECHA_CONT", "F_CONT", "FECHA_DOC", "FECHA")
        _assign_existing_values(detail_values, det_columns, fecha_pago_val, "FECHA_FACT")
        _assign_existing_values(detail_values, det_columns, monto_total_factura, "MONTO_DOC", "TOTAL_DOC", "MONTO")
        _assign_existing_values(detail_values, det_columns, vencida, "VENCIDA")
        _assign_existing_values(detail_values, det_columns, monto_total_factura, "SUBTOTAL", "TOTAL_FACT")
        _assign_existing_values(detail_values, det_columns, str(item.get("no_cuota") or "").strip() or "1", "NO_CUOTA", "CUOTA_NUM", "NUM_CUOTA")
        _assign_existing_values(detail_values, det_columns, cuota_linea, "CUOTA", "MONTO_CUOTA", "VALOR_CUOTA")
        _assign_existing_values(detail_values, det_columns, balance_doc_linea, "BALANCE_DOC", "BALANCE", "SALDO_DOC", "SALDO")
        _assign_existing_values(detail_values, det_columns, _to_decimal(item.get("balance_total_factura")), "BALANCE_TOTAL_FACTURA", "SALDO_FACTURA")
        _assign_existing_values(detail_values, det_columns, _parse_date(item.get("fecha_venc")), "FECHA_VENC", "F_VENC", "VENCIMIENTO")
        _assign_existing_values(detail_values, det_columns, int(_to_float(item.get("dias"))), "DIAS", "DIAS_VENC", "ATRASO")
        _assign_existing_values(detail_values, det_columns, Decimal("0"), "CARGO", "MORA", "TOTAL_MORA")
        _assign_existing_values(detail_values, det_columns, Decimal("0"), "PORC_DESC", "PORCENTAJE_DESC", "PCT_DESC")
        _assign_existing_values(detail_values, det_columns, Decimal("0"), "DESC_AVANCE", "DESCUENTO", "AVANCE")
        _assign_existing_values(
            detail_values,
            det_columns,
            monto_pagado_linea,
            "PAGO_ABONO",
            "IMP_ABONO",
            "IMP_PAGADO",
            "IMP_PAGO",
            "IMP_COBRADO",
            "IMP_APLICADO",
            "MONTO_APLICADO",
            "ABONO_APLICADO",
            "MONTO_ABONO",
            "MONTO_PAGO",
            "ABONO",
            "PAGADO",
            "PAGO",
            "COBRO",
            "IMPORTE",
        )
        _assign_existing_values(detail_values, det_columns, monto_pagado_linea, "TOTAL_PAGO", "TOTAL_PAGO2")
        _assign_existing_values(detail_values, det_columns, sub_pdo_linea, "SUB_PDO", "SUBPDO")
        _assign_existing_values(detail_values, det_columns, saldo_venc_linea, "SALDO_VENC")
        _assign_existing_values(detail_values, det_columns, Decimal("1.0000"), "TASAFACT")
        _assign_existing_values(detail_values, det_columns, total_recibo_linea, "TOTAL_RECIBO", "TOTAL_RECIBO2")
        _assign_existing_values(detail_values, det_columns, "Y", "SELECCION")
        _assign_existing_values(detail_values, det_columns, total_recibo_linea, "BALANCE_PEND", "SALDO_PEND", "PENDIENTE")
        _assign_existing_values(detail_values, det_columns, Decimal("0"), "TOTAL_RET", "RETENCION", "RET")
        _assign_existing_values(detail_values, det_columns, id_sn, "ID_SN")
        _assign_existing_values(detail_values, det_columns, usuario_id, "ID_USUARIO", "USUARIO_ID")
        _assign_existing_values(detail_values, det_columns, periodo_cont, "PERIODO_CONT")
        _assign_existing_values(detail_values, det_columns, ejercicio, "EJERCICIO")
        factura_data = factura_lookup.get(str(item.get("no_doc") or "").strip()) or {}
        comentario_factura = str(factura_data.get("comentario") or "").strip()
        _assign_existing_values(detail_values, det_columns, comentario_factura, "COMENTARIO", "OBSERVACION", "CONCEPTO")
        _insert_dynamic_row(cursor, "DET_RECIBO_INGRESO", det_columns, detail_values)

    _create_cxc_ed_entries(
        cursor,
        no_recibo=no_recibo,
        id_sn=id_sn,
        nombre_cliente=nombre_cliente,
        rnc_ced=rnc_ced,
        fecha_cont=fecha_pago_val,
        fecha_doc=fecha_pago_val,
        fecha_venc=fecha_pago_val,
        total_recibo=cash_total,
        comentario=comentario_ed,
        periodo_cont=periodo_cont,
        ejercicio=ejercicio,
        usuario_id=usuario_id,
        usuario_nombre=usuario_nombre,
        terminal=terminal,
        cta_asociada=cta_asociada,
        cuenta_medio_pago="11010101",
        cuenta_medio_pago_desc="Caja General",
    )

    factura_columns = _load_table_columns("CAB_FACTURA")
    factura_has_abono = "ABONO" in factura_columns
    factura_has_fecha_act = "FECHA_ACT" in factura_columns
    for doc_number, applied_amount in detail_by_doc.items():
        factura_row = factura_lookup.get(doc_number) or {}
        pending_actual = max(_to_decimal(factura_row.get("pending_actual")), Decimal("0"))
        new_saldo = max(pending_actual - applied_amount, Decimal("0"))
        factura_updates = {
            "SALDO": new_saldo,
            "EST_DOC": "CERRADO" if new_saldo <= Decimal("0.01") else "ABIERTO",
        }
        if factura_has_abono:
            total_doc_actual = max(_to_decimal(factura_row.get("total_doc")), Decimal("0"))
            new_abono = max(_to_decimal(factura_row.get("abono_base")) + applied_amount, Decimal("0"))
            if total_doc_actual > Decimal("0"):
                new_abono = min(new_abono, total_doc_actual)
            factura_updates["ABONO"] = new_abono
        if factura_has_fecha_act:
            factura_updates["FECHA_ACT"] = now
        _update_dynamic_row(
            cursor,
            "CAB_FACTURA",
            factura_updates,
            "CAST(ID_DOC AS VARCHAR(50)) = %s",
            [doc_number],
        )

    prestamo_docs_actualizados = set()
    for item in valid_items:
        if not bool(item.get("tiene_financiamiento")):
            continue
        no_doc = str(item.get("no_doc") or "").strip()
        no_cuota = str(item.get("no_cuota") or "").strip()
        if not no_doc or not no_cuota:
            continue
        balance_anterior = _to_decimal(item.get("balance_doc"))
        monto_pagado = item["monto_pago"]
        new_balance = max(balance_anterior - monto_pagado, Decimal("0"))
        cuota_original = _to_decimal(item.get("cuota"))
        new_abono_cuota = max(cuota_original - new_balance, Decimal("0"))
        prestamo_updates = {}
        if prestamo_has_balance:
            prestamo_updates["BALANCE"] = new_balance
        if prestamo_abono_cuota_col:
            prestamo_updates[prestamo_abono_cuota_col] = new_abono_cuota
        if prestamo_no_recibo_col:
            prestamo_updates[prestamo_no_recibo_col] = no_recibo
        _update_dynamic_row(
            cursor,
            "DET_PRESTAMO",
            prestamo_updates,
            "CAST(NO_DOC AS VARCHAR(50)) = %s AND CAST(NO_CUOTA AS VARCHAR(50)) = %s",
            [no_doc, no_cuota],
        )
        prestamo_docs_actualizados.add(no_doc)

    for prestamo_doc in prestamo_docs_actualizados:
        _rebuild_det_prestamo_from_active_receipts(cursor, prestamo_doc)
        _sync_cab_prestamo_from_det(cursor, prestamo_doc, now=now)

    transaction.on_commit(
        lambda rid=recibo_id, nro=no_recibo: broadcast_cxc_document_status(
            document_id=rid,
            no_recibo=nro,
            estado="Abierto",
            reason="recibo-saved",
        )
    )

    return {
        "recibo_id": recibo_id,
        "no_recibo": no_recibo,
        "id_sn": id_sn,
        "cliente_nombre": nombre_cliente,
        "monto_total": float(cash_total),
        "efectivo": float(efectivo),
        "transferencia": float(transferencia),
        "cuentas_pagadas": len(valid_items),
    }


def pagos_masivos_view(request, id_cobrador=None):
    ctx = _base_context(request, page_title="Cobros - Pagos Masivos por Cobrador", active_nav="cobros")
    if not ctx:
        return redirect("login")
    usuario_id = ctx["auth_payload"]["usuario_id"]
    if not (
        has_perm(usuario_id, "cobros", "ver_pagos_masivos")
        or has_perm(usuario_id, "cobros", "ver_registros_pagos")
        or has_perm(usuario_id, "cobros", "ver")
    ):
        return render_denied(request, active_nav="cobros")

    _ensure_cobro_registro_pago_tables()

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT 
                c.ID_COBRADOR,
                c.NOMBRE,
                c.ID_SECTOR,
                c.SECTOR_NOMBRE,
                c.TELEFONO,
                c.ACTIVO,
                COUNT(cl.ID_ASIGNACION) AS TOTAL_CLIENTES
            FROM COBRO_COBRADOR c
            LEFT JOIN COBRO_COBRADOR_CLIENTE cl ON cl.ID_COBRADOR = c.ID_COBRADOR
            WHERE c.ACTIVO = 1
            GROUP BY c.ID_COBRADOR, c.NOMBRE, c.ID_SECTOR, c.SECTOR_NOMBRE, c.TELEFONO, c.ACTIVO
            ORDER BY c.NOMBRE ASC
            """
        )
        cob_rows = cursor.fetchall()

    cobradores = [
        {
            "id_cobrador": r[0],
            "nombre": str(r[1] or "").strip(),
            "id_sector": int(r[2]) if r[2] is not None else None,
            "sector_nombre": str(r[3] or "").strip(),
            "telefono": str(r[4] or "").strip(),
            "activo": bool(r[5]),
            "total_clientes": r[6] or 0,
        }
        for r in cob_rows
    ]

    selected_cobrador = None
    target_id = id_cobrador or request.GET.get("cobrador_id")
    if target_id:
        try:
            target_id_int = int(target_id)
            selected_cobrador = next((c for c in cobradores if c["id_cobrador"] == target_id_int), None)
        except (ValueError, TypeError):
            selected_cobrador = None

    if not selected_cobrador and cobradores:
        selected_cobrador = cobradores[0]

    clientes_con_cuentas = []
    stats = {
        "total_clientes": 0,
        "clientes_con_saldo": 0,
        "total_cuentas": 0,
        "total_saldo_general": 0.0,
    }

    if selected_cobrador:
        clientes_con_cuentas = _load_cobrador_clientes_with_pendientes(selected_cobrador["id_cobrador"])
        stats["total_clientes"] = len(clientes_con_cuentas)
        stats["clientes_con_saldo"] = sum(1 for c in clientes_con_cuentas if c.get("tiene_pendientes"))
        stats["total_cuentas"] = sum(c.get("total_cuentas", 0) for c in clientes_con_cuentas)
        stats["total_saldo_general"] = sum(c.get("total_pendiente", 0.0) for c in clientes_con_cuentas)

    today = timezone.localdate()
    ctx["cobradores"] = cobradores
    ctx["selected_cobrador"] = selected_cobrador
    ctx["clientes_con_cuentas"] = clientes_con_cuentas
    ctx["stats"] = stats
    ctx["fecha_hoy"] = today.strftime("%Y-%m-%d")
    return render(request, "cobros/pagos_masivos.html", ctx)


@require_POST
def api_pagos_masivos_procesar_view(request):
    ctx = _base_context(request, page_title="Cobros - Procesar Pagos Masivos", active_nav="cobros")
    if not ctx:
        return JsonResponse({"ok": False, "error": "Sesión inválida"}, status=401)
    usuario_id = ctx["auth_payload"]["usuario_id"]
    usuario_nombre = str(ctx["auth_payload"].get("usuario_nombre") or "").strip()
    if not (
        has_perm(usuario_id, "cobros", "ver_pagos_masivos")
        or has_perm(usuario_id, "cobros", "ver_registros_pagos")
        or has_perm(usuario_id, "caja", "cxc_nuevo")
        or has_perm(usuario_id, "cobros", "ver")
    ):
        return JsonResponse({"ok": False, "error": "No tiene permisos para registrar pagos."}, status=403)

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"ok": False, "error": "Payload JSON inválido."}, status=400)

    id_cobrador = payload.get("id_cobrador")
    copias = max(1, min(int(payload.get("copias") or 2), 10))
    raw_pagos = payload.get("pagos") or []

    cobrador = CobroCobrador.objects.filter(id_cobrador=id_cobrador).first()
    cobrador_nombre = cobrador.nombre if cobrador else ""

    # Filtrar solo clientes con items que tengan monto_pago > 0
    clientes_para_procesar = []
    for cl in raw_pagos:
        id_sn = str(cl.get("id_sn") or "").strip()
        items = cl.get("items") or []
        valid_items = [it for it in items if _to_decimal(it.get("monto_pago")) > Decimal("0.001")]
        if id_sn and valid_items:
            clientes_para_procesar.append({
                "id_sn": id_sn,
                "cliente_nombre": cl.get("cliente_nombre", ""),
                "items": valid_items,
            })

    if not clientes_para_procesar:
        return JsonResponse({"ok": False, "error": "No se enviaron pagos con monto mayor a cero para procesar."}, status=400)

    now = timezone.localtime()
    local_date = timezone.localdate()
    terminal = _resolve_request_terminal(request, payload)
    recibos_creados = []

    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                for cl in clientes_para_procesar:
                    res = _save_pago_masivo_cliente(
                        cursor,
                        id_sn=cl["id_sn"],
                        items=cl["items"],
                        usuario_id=usuario_id,
                        usuario_nombre=usuario_nombre,
                        cobrador_nombre=cobrador_nombre,
                        terminal=terminal,
                        now=now,
                        local_date=local_date,
                    )
                    if res:
                        recibos_creados.append(res)
    except Exception as exc:
        return JsonResponse({"ok": False, "error": f"Error al procesar lote de pagos: {str(exc)}"}, status=500)

    total_efectivo = sum(r["efectivo"] for r in recibos_creados)
    total_transferencia = sum(r["transferencia"] for r in recibos_creados)
    total_general = sum(r["monto_total"] for r in recibos_creados)
    recibo_ids = [str(r["recibo_id"]) for r in recibos_creados]
    ids_param = ",".join(recibo_ids)

    print_url = reverse("cobros:pagos_masivos_impresion_lote") + f"?recibos={ids_param}&copies={copias}"
    pdf_url = reverse("cobros:pagos_masivos_pdf_lote") + f"?recibos={ids_param}&copies={copias}"

    return JsonResponse({
        "ok": True,
        "message": f"Se procesaron con éxito {len(recibos_creados)} recibos de pago.",
        "recibos": recibos_creados,
        "total_recibos": len(recibos_creados),
        "total_efectivo": total_efectivo,
        "total_transferencia": total_transferencia,
        "total_general": total_general,
        "copias": copias,
        "print_url": print_url,
        "pdf_url": pdf_url,
    })


@xframe_options_sameorigin
def pagos_masivos_impresion_lote_view(request):
    ctx = _base_context(request, page_title="Impresión de Recibos en Lote", active_nav="cobros")
    if not ctx:
        return redirect("login")
    usuario_id = ctx["auth_payload"]["usuario_id"]
    if not (
        has_perm(usuario_id, "cobros", "ver_pagos_masivos")
        or has_perm(usuario_id, "cobros", "ver_registros_pagos")
        or has_perm(usuario_id, "caja", "cxc_imprimir")
        or has_perm(usuario_id, "cobros", "ver")
    ):
        return render_denied(request, active_nav="cobros")

    recibos_param = (request.GET.get("recibos") or "").strip()
    if not recibos_param:
        return redirect("cobros:pagos_masivos")

    try:
        copies = max(1, min(int(request.GET.get("copies") or 2), 10))
    except (ValueError, TypeError):
        copies = 2

    raw_ids = [r.strip() for r in recibos_param.split(",") if r.strip()]
    receipts_contexts = []

    for rid in raw_ids:
        try:
            record = _load_cxc_recibo_detalle(rid)
            if not record:
                continue
            print_payload = _build_cxc_recibo_print_payload(record, ctx["auth_payload"])
            receipt_ctx = _build_cxc_receipt_template_context(print_payload, copies=copies)
            receipts_contexts.append(receipt_ctx)
        except Exception:
            continue

    if not receipts_contexts:
        return redirect("cobros:pagos_masivos")

    ctx["receipts_contexts"] = receipts_contexts
    ctx["total_recibos"] = len(receipts_contexts)
    ctx["copies"] = copies
    ctx["recibos_param"] = recibos_param
    ctx["formato_impresion"] = get_print_format("recibo_pago")
    return render(request, "cobros/recibos_lote_print.html", ctx)


def pagos_masivos_pdf_lote_view(request):
    ctx = _base_context(request, page_title="Recibos PDF", active_nav="cobros")
    if not ctx:
        return redirect("login")
    usuario_id = ctx["auth_payload"]["usuario_id"]
    if not (
        has_perm(usuario_id, "cobros", "ver_pagos_masivos")
        or has_perm(usuario_id, "cobros", "ver_registros_pagos")
        or has_perm(usuario_id, "caja", "cxc_imprimir")
        or has_perm(usuario_id, "cobros", "ver")
    ):
        return render_denied(request, active_nav="cobros")

    recibos_param = (request.GET.get("recibos") or "").strip()
    if not recibos_param:
        return HttpResponse("No se indicaron recibos", status=400)

    try:
        copies = max(1, min(int(request.GET.get("copies") or 2), 10))
    except (ValueError, TypeError):
        copies = 2

    raw_ids = [r.strip() for r in recibos_param.split(",") if r.strip()]
    all_streams = []

    for rid in raw_ids:
        try:
            record = _load_cxc_recibo_detalle(rid)
            if not record:
                continue
            print_payload = _build_cxc_recibo_print_payload(record, ctx["auth_payload"])
            stream = _pdf_render_receipt_page_ops(print_payload)
            for _ in range(copies):
                all_streams.append(stream)
        except Exception:
            continue

    if not all_streams:
        return HttpResponse("No se pudieron cargar los recibos para el PDF", status=404)

    page_width = _pdf_mm_to_pt(210)
    page_height = _pdf_mm_to_pt(148.5)
    pdf_bytes = _build_pdf_document(page_width, page_height, all_streams)
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = 'inline; filename="Recibos_Lote_Cobros.pdf"'
    return response

