from django.db import connection
from django.db.models import Q
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.clickjacking import xframe_options_sameorigin

from ajustes.permissions import has_perm
from core.views import _base_context, render_denied
from .models import CobroAcuerdo
from prefacturas_app.models_existing import MaestroSn
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


def _build_alertas_context(min_days, max_days=None, sector_id=None):
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
                f.FECHA_VENC
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
                    SELECT d.NO_DOC, MAX(d.FECHA_CONT)
                    FROM DET_RECIBO_INGRESO d
                    WHERE d.NO_DOC IN ({placeholders})
                    GROUP BY d.NO_DOC
                    """,
                    docs_chunk,
                )
                for no_doc, fecha_pago in cursor.fetchall():
                    ult_pago_by_doc[no_doc] = _fmt_date(fecha_pago)

    for row in rows:
        id_sn, nom_socio, tel1, _, sector, fecha_doc, id_doc, total_doc, saldo_doc, fecha_venc = row
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
                    "items": [],
                }
                clientes_map[cliente_key] = cliente
            return cliente

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


def index(request):
    ctx = _base_context(request, page_title="Gestion de cobros", active_nav="cobros")
    if not ctx:
        return redirect("login")
    if not has_perm(ctx["auth_payload"]["usuario_id"], "cobros", "ver"):
        return render_denied(request, active_nav="cobros")
    ctx["submodules"] = {
        "estado_cuenta": has_perm(ctx["auth_payload"]["usuario_id"], "cobros", "ver_estado_cuenta"),
        "alertas": has_perm(ctx["auth_payload"]["usuario_id"], "cobros", "ver_alertas"),
        "acuerdos": has_perm(ctx["auth_payload"]["usuario_id"], "cobros", "ver_acuerdos"),
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
                    return redirect("cobros:acuerdos")
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
                success_message = "Acuerdo guardado correctamente."

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
    if estado_filtro:
        acuerdos = acuerdos.filter(estado=estado_filtro)

    hoy = timezone.localdate()
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
    ctx["estado_options"] = [
        ("PENDIENTE", "Pendiente"),
        ("CUMPLIDO", "Cumplido"),
        ("CANCELADO", "Cancelado"),
    ]
    return render(request, "cobros/acuerdos.html", ctx)


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

    embed = (request.GET.get("embed") or "").strip() == "1"
    ctx = _build_alertas_context(min_days, max_days, sector_id)
    ctx["auth_payload"] = auth_payload
    ctx["embed"] = embed
    return render(request, "cobros/alertas_print.html", ctx)
