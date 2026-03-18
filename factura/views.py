import json
import socket
import hmac
from decimal import Decimal
from datetime import datetime

from django.db import connection, transaction
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from ajustes.models import (
    FacturacionElectronicaConfig,
    FacturacionElectronicaDocumento,
    FacturacionElectronicaEvento,
    FacturacionElectronicaSecuencia,
)
from ajustes.permissions import has_perm
from core.views import _base_context, _get_empresa_data, render_denied
from prefacturas_app.views import _require_perm_json
from .ecf_provider import submit_document, should_auto_dispatch
from .ecf_runtime import build_ecf_runtime_report, get_ecf_callback_api_key

ECF_TIPOS = [
    ("31", "Factura de Credito Fiscal Electronica"),
    ("32", "Factura de Consumo Electronica"),
    ("33", "Nota de Debito Electronica"),
    ("34", "Nota de Credito Electronica"),
    ("41", "Comprobante Electronico de Compras"),
    ("43", "Comprobante Electronico para Gastos Menores"),
    ("44", "Comprobante Electronico para Regimenes Especiales"),
    ("45", "Comprobante Electronico Gubernamental"),
    ("46", "Comprobante Electronico para Pagos al Exterior"),
    ("47", "Comprobante Electronico para Exportaciones"),
]
ECF_TIPOS_MAP = {codigo: nombre for codigo, nombre in ECF_TIPOS}
EMISION_TIPOS = [
    ("31", "Factura de Credito Fiscal Electronica"),
    ("32", "Factura de Consumo Electronica"),
    ("45", "Factura Gubernamental Electronica"),
    ("47", "Factura de Exportacion Electronica"),
]


def _bool_post(value):
    return str(value or "").strip().lower() in {"1", "true", "on", "si", "sí", "y", "yes"}


def _to_int(value, default=0):
    try:
        return int(str(value or "").strip())
    except Exception:
        return default


def _to_date_or_none(value):
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except Exception:
        return None


def _inferir_tipo_ecf(tipo_texto, id_ncf):
    normalized = str(tipo_texto or "").strip().upper()
    if id_ncf in (31, 32, 33, 34, 41, 43, 44, 45, 46, 47):
        return str(int(id_ncf))
    if "CREDITO FISCAL" in normalized:
        return "31"
    if "CONSUMO" in normalized:
        return "32"
    if "DEBITO" in normalized:
        return "33"
    if "CREDITO" in normalized and "NOTA" in normalized:
        return "34"
    if "COMPRA" in normalized:
        return "41"
    if "GASTO" in normalized:
        return "43"
    if "REGIMEN" in normalized:
        return "44"
    if "GUBERN" in normalized:
        return "45"
    if "EXTERIOR" in normalized:
        return "46"
    if "EXPORT" in normalized:
        return "47"
    return ""


def _build_qr_url(empresa_rnc, documento):
    if not empresa_rnc or not documento.encf or not documento.codigo_seguridad:
        return ""
    monto_total = f"{Decimal(documento.monto_total or 0):.2f}"
    if documento.tipo_ecf == "32" and Decimal(documento.monto_total or 0) < Decimal("250000"):
        return (
            "https://fc.dgii.gov.do/eCF/ConsultaTimbreFC"
            f"?RNCEmisor={empresa_rnc}&ENCF={documento.encf}"
            f"&MontoTotal={monto_total}&CodigoSeguridad={documento.codigo_seguridad}"
        )
    fecha_doc = timezone.localtime(documento.fecha_doc).strftime("%d-%m-%Y") if documento.fecha_doc else ""
    fecha_firma = timezone.localtime(documento.actualizado_en).strftime("%d-%m-%Y %H:%M:%S") if documento.actualizado_en else ""
    return (
        "https://ecf.dgii.gov.do/ecf/ConsultaTimbre"
        f"?RncEmisor={empresa_rnc}"
        f"&RncComprador={documento.cliente_rnc or ''}"
        f"&ENCF={documento.encf}"
        f"&FechaEmision={fecha_doc}"
        f"&MontoTotal={monto_total}"
        f"&FechaFirma={fecha_firma}"
        f"&CodigoSeguridad={documento.codigo_seguridad}"
    )


def _build_qr_image_url(qr_target_url):
    if not qr_target_url:
        return ""
    from urllib.parse import quote
    return f"https://api.qrserver.com/v1/create-qr-code/?size=150x150&data={quote(qr_target_url, safe='')}"


def _ensure_ecf_sequence(tipo_ecf):
    config = FacturacionElectronicaConfig.objects.filter(id_config=1).first()
    encf = ""
    if config and config.habilitado:
        secuencia = FacturacionElectronicaSecuencia.objects.select_for_update().filter(
            tipo_ecf=tipo_ecf,
            habilitada=True,
        ).first()
        if not secuencia:
            raise ValueError(f"No hay secuencia e-CF habilitada para el tipo {tipo_ecf}.")
        encf = f"E{tipo_ecf}{int(secuencia.secuencia_actual):010d}"
        secuencia.secuencia_actual = int(secuencia.secuencia_actual) + 1
        secuencia.save(update_fields=["secuencia_actual", "actualizado_en"])
    return config, encf


def _tipo_descripcion(tipo_ecf):
    return ECF_TIPOS_MAP.get(str(tipo_ecf or "").strip(), "Factura Electronica")


def _parse_request_payload(request):
    if request.content_type and "application/json" in request.content_type.lower():
        try:
            body = request.body.decode("utf-8") if request.body else "{}"
            parsed = json.loads(body or "{}")
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return None
    if request.POST:
        return request.POST.dict()
    return {}


def _find_ecf_document(payload):
    payload = payload or {}
    for field in ("id_doc", "encf", "track_id"):
        value = str(payload.get(field) or "").strip()
        if not value:
            continue
        documento = FacturacionElectronicaDocumento.objects.filter(**{field: value}).first()
        if documento:
            return documento
    return None


def _build_event_response(documento, evento_tipo):
    return {
        "ok": True,
        "evento": evento_tipo,
        "documento": {
            "id_doc": documento.id_doc,
            "encf": documento.encf,
            "track_id": documento.track_id,
            "estado": documento.estado,
            "cliente_rnc": documento.cliente_rnc,
            "cliente_nombre": documento.cliente_nombre,
        },
    }


def _build_public_ecf_urls(request):
    return {
        "recepcion": request.build_absolute_uri(reverse("factura:ecf_recepcion")),
        "aprobacion": request.build_absolute_uri(reverse("factura:ecf_aprobacion")),
    }


def _find_existing_credit_note(cursor, factura_id, encf_hint=""):
    cursor.execute(
        """
        SELECT TOP 1 ID_DOC, ISNULL(NCF, '')
        FROM CAB_FACTURA
        WHERE TRY_CAST(ID_DOC_BASE AS BIGINT) = %s
          AND TRY_CAST(ID_NCF AS BIGINT) = 34
          AND UPPER(ISNULL(CANCELADO, 'N')) <> 'Y'
        ORDER BY TRY_CAST(ID_DOC AS BIGINT) DESC, ID_DOC DESC
        """,
        [factura_id],
    )
    row = cursor.fetchone()
    if row:
        return {
            "id_doc": str(row[0] or "").strip(),
            "encf": str(row[1] or "").strip(),
        }

    encf_hint = str(encf_hint or "").strip()
    if not encf_hint:
        return None

    cursor.execute(
        """
        SELECT TOP 1 ID_DOC, ISNULL(NCF, '')
        FROM CAB_FACTURA
        WHERE TRY_CAST(ID_NCF AS BIGINT) = 34
          AND ISNULL(NCF, '') = %s
        ORDER BY TRY_CAST(ID_DOC AS BIGINT) DESC, ID_DOC DESC
        """,
        [encf_hint],
    )
    row = cursor.fetchone()
    if not row:
        return None
    return {
        "id_doc": str(row[0] or "").strip(),
        "encf": str(row[1] or "").strip(),
    }


def _boolish(value):
    return str(value or "").strip().lower() in {"1", "true", "on", "si", "sí", "yes", "y"}


def _ecf_sequence_error_response(tipo_ecf, exc):
    detail = str(exc or "").strip() or f"No hay secuencia e-CF habilitada para el tipo {tipo_ecf}."
    payload = {"detail": detail}
    if "No hay secuencia e-CF habilitada" in detail:
        payload.update(
            {
                "error_code": "missing_ecf_sequence",
                "tipo_ecf": str(tipo_ecf or "").strip(),
                "help_url": reverse("factura:electronica"),
                "help_label": "Configurar secuencia e-CF",
            }
        )
    return JsonResponse(payload, status=400)


def _require_ecf_callback_auth(request):
    expected = get_ecf_callback_api_key()
    if not expected:
        return None
    provided = str(request.headers.get("X-ECF-API-Key") or "").strip()
    if not provided:
        auth_header = str(request.headers.get("Authorization") or "").strip()
        if auth_header.lower().startswith("bearer "):
            provided = auth_header[7:].strip()
    if provided and hmac.compare_digest(provided, expected):
        return None
    return JsonResponse({"detail": "No autorizado para callback e-CF."}, status=401)


def _apply_demo_qr(documento, empresa_rnc):
    if not documento:
        return False
    base_code = f"DEMO{int(documento.id_doc):010d}"[-10:]
    documento.codigo_seguridad = f"QR{base_code}"
    documento.xml_generado = True
    documento.firmado = True
    documento.enviado_dgii = True
    if not str(documento.estado or "").strip():
        documento.estado = "DEMO_QR"
    documento.url_consulta_qr = _build_qr_url(empresa_rnc, documento)
    documento.save(
        update_fields=[
            "codigo_seguridad",
            "xml_generado",
            "firmado",
            "enviado_dgii",
            "estado",
            "url_consulta_qr",
            "actualizado_en",
        ]
    )
    FacturacionElectronicaEvento.objects.create(
        documento=documento,
        tipo_evento="QR_DEMO",
        detalle="Codigo de seguridad demo generado para validar visualmente el QR.",
    )
    return True


def _dispatch_document_if_needed(documento, request, config):
    dispatch_enabled = should_auto_dispatch(getattr(config, "modo_envio", "manual"))
    result = submit_document(documento, _build_public_ecf_urls(request), dispatch_enabled=dispatch_enabled)
    if not result.attempted:
        return result

    update_fields = ["estado", "respuesta_dgii", "actualizado_en"]
    documento.estado = "ENVIADO_INTEGRADOR" if result.ok else "ERROR_ENVIO_INTEGRADOR"
    documento.respuesta_dgii = result.raw_response or result.message
    if result.track_id:
        documento.track_id = result.track_id
        update_fields.append("track_id")
    documento.save(update_fields=update_fields)
    FacturacionElectronicaEvento.objects.create(
        documento=documento,
        tipo_evento="ENVIADO_INTEGRADOR" if result.ok else "ERROR_ENVIO_INTEGRADOR",
        detalle=result.message,
    )
    return result


def _sync_ecf_documentos(limit=150):
    documentos = []
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT TOP {max(int(limit), 1)} ID_DOC, FECHA_DOC, NOM_SOCIO, RNC_CED, TOTAL_DOC,
                       ISNULL(NCF, ''), ISNULL(TIPO, ''), ISNULL(ID_NCF, 0), ISNULL(EST_DOC, '')
                FROM CAB_PEDIDO
                ORDER BY FECHA_DOC DESC, ID_DOC DESC
                """
            )
            documentos = list(cursor.fetchall())
    except Exception:
        return 0

    sincronizados = 0
    for row in documentos:
        id_doc = _to_int(row[0], 0)
        if id_doc <= 0:
            continue
        fecha_doc = row[1]
        cliente_nombre = str(row[2] or "").strip()
        cliente_rnc = str(row[3] or "").strip()
        monto_total = Decimal(row[4] or 0)
        encf = str(row[5] or "").strip()
        tipo_cab = str(row[6] or "").strip()
        id_ncf = _to_int(row[7], 0)
        est_doc = str(row[8] or "").strip().upper()
        tipo_ecf = _inferir_tipo_ecf(tipo_cab, id_ncf)
        estado = "PENDIENTE"
        if encf.startswith("E"):
            estado = "REGISTRADO"
        elif not tipo_ecf:
            estado = "TIPO_PENDIENTE"
        elif est_doc == "CERRADO":
            estado = "LISTO_PARA_XML"

        defaults = {
            "tipo_ecf": tipo_ecf or None,
            "encf": encf or None,
            "estado": estado,
            "cliente_rnc": cliente_rnc or None,
            "cliente_nombre": cliente_nombre or None,
            "fecha_doc": fecha_doc,
            "monto_total": monto_total,
            "observaciones": tipo_cab or None,
        }
        documento, created = FacturacionElectronicaDocumento.objects.update_or_create(
            id_doc=id_doc,
            defaults=defaults,
        )
        if created:
            FacturacionElectronicaEvento.objects.create(
                documento=documento,
                tipo_evento="SINCRONIZADO",
                detalle="Documento importado desde CAB_PEDIDO para preparacion e-CF.",
            )
        sincronizados += 1
    return sincronizados


def index(request):
    ctx = _base_context(request, page_title="Factura", active_nav="factura")
    if not ctx:
        return redirect("login")
    if not has_perm(ctx["auth_payload"]["usuario_id"], "factura", "ver"):
        return render_denied(request, active_nav="factura")
    ctx["submodules"] = {
        "emision": has_perm(ctx["auth_payload"]["usuario_id"], "factura", "ver_emision"),
        "electronica": has_perm(ctx["auth_payload"]["usuario_id"], "factura", "ver_electronica"),
        "documentos": has_perm(ctx["auth_payload"]["usuario_id"], "factura", "ver_documentos"),
    }
    return render(request, "factura/index.html", ctx)


def _fmt_date_iso(value):
    if not value:
        return ""
    try:
        return value.strftime("%Y-%m-%d")
    except Exception:
        return ""


def _num(v):
    if v is None:
        return None
    try:
        return float(v)
    except Exception:
        return None


@require_http_methods(["GET"])
def emision_view(request):
    ctx = _base_context(request, page_title="Factura - Emision", active_nav="factura")
    if not ctx:
        return redirect("login")
    if not has_perm(ctx["auth_payload"]["usuario_id"], "factura", "ver_emision"):
        return render_denied(request, active_nav="factura")
    ctx["tipos_emision"] = EMISION_TIPOS
    return render(request, "factura/emision.html", ctx)


@require_http_methods(["GET"])
def emision_prefacturas_view(request):
    auth_payload = _require_perm_json(request, "factura", "ver_emision")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    query = (request.GET.get("q") or "").strip()
    filtro = (request.GET.get("filtro") or "documento").strip().lower()
    sql = """
        SELECT TOP 80
            ID_DOC, ID_SN, NOM_SOCIO, RNC_CED, CONTACTO, ENT_FACTURA, ENT_MERCANCIA, EST_DOC,
            FECHA_CONT, FECHA_DOC, FECHA_VENC, COMENTARIO, TOTAL_DOC
        FROM CAB_PEDIDO
        WHERE UPPER(ISNULL(EST_DOC, '')) = 'ABIERTO'
    """
    params = []
    if query:
        if filtro == "cliente":
            sql += " AND ID_SN LIKE %s"
            params.append(f"%{query}%")
        elif filtro == "nombre":
            sql += " AND NOM_SOCIO LIKE %s"
            params.append(f"%{query}%")
        else:
            sql += " AND CAST(ID_DOC AS VARCHAR(50)) LIKE %s"
            params.append(f"%{query}%")
    if filtro == "cliente":
        sql += " ORDER BY ID_SN, TRY_CAST(ID_DOC AS BIGINT) DESC, ID_DOC DESC"
    elif filtro == "nombre":
        sql += " ORDER BY NOM_SOCIO, TRY_CAST(ID_DOC AS BIGINT) DESC, ID_DOC DESC"
    else:
        sql += " ORDER BY TRY_CAST(ID_DOC AS BIGINT) DESC, ID_DOC DESC"

    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        pedidos = cursor.fetchall()

    results = []
    for p in pedidos:
        results.append(
            {
                "id_doc": str(p[0] or ""),
                "id_sn": p[1] or "",
                "nom_socio": p[2] or "",
                "rnc_ced": p[3] or "",
                "contacto": p[4] or "",
                "ent_factura": p[5] or "",
                "ent_mercancia": p[6] or "",
                "est_doc": p[7] or "",
                "fecha_cont": _fmt_date_iso(p[8]),
                "fecha_doc": _fmt_date_iso(p[9]),
                "fecha_venc": _fmt_date_iso(p[10]),
                "comentario": p[11] or "",
                "total_doc": float(p[12]) if p[12] is not None else 0.0,
            }
        )
    return JsonResponse({"results": results})


@require_http_methods(["GET"])
def emision_prefactura_detalle_view(request):
    auth_payload = _require_perm_json(request, "factura", "ver_emision")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    id_doc = (request.GET.get("id_doc") or "").strip()
    if not id_doc:
        return JsonResponse({"detail": "Parametro id_doc requerido"}, status=400)

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT TOP 1
                ID_DOC, ID_SN, NOM_SOCIO, RNC_CED, CONTACTO, ENT_FACTURA, ENT_MERCANCIA, EST_DOC,
                FECHA_CONT, FECHA_DOC, FECHA_VENC, COMENTARIO, SUBTOTAL, TOTAL_DESC, TOTAL_ITBIS,
                TOTAL_DOC, ID_CONDICION, DIA, CONDICION, ID_PRECIO
            FROM CAB_PEDIDO
            WHERE CAST(ID_DOC AS VARCHAR(50)) = %s
            """,
            [id_doc],
        )
        cab = cursor.fetchone()

    if not cab:
        return JsonResponse({"detail": "Prefactura no encontrada"}, status=404)

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT ID_DETALLE, DESCRIP_ART, ID_ARTICULO, CANT_UND, CANTIDAD, CANT_ENT, MEDIDA,
                   OBSERVACION, ID_ALMACEN, CECO, CEBE, PRECIO, PRECIO_BRUTO, TOTAL_LINEA, PORC_DESC, ID_IMPTO
            FROM DET_PEDIDO
            WHERE CAST(ID_DOC AS VARCHAR(50)) = %s
            ORDER BY No_LINEA, ID_DETALLE
            """,
            [id_doc],
        )
        detalle_rows = cursor.fetchall()

    results = []
    for d in detalle_rows:
        results.append(
            {
                "id_detalle": d[0],
                "descrip_art": d[1] or "",
                "id_articulo": d[2] or "",
                "cant_emp": _num(d[3]),
                "cantidad": _num(d[4]),
                "entregado": _num(d[5]),
                "uom": d[6] or "",
                "observacion": d[7] or "",
                "alm": d[8],
                "cebe": d[9] or "",
                "proyecto": d[10] or "",
                "precio_unit": _num(d[11]),
                "precio_bruto": _num(d[12]),
                "valor": _num(d[13]),
                "porc_desc": _num(d[14]),
                "id_itbis": d[15],
            }
        )

    data = {
        "prefactura": {
            "id_doc": str(cab[0] or ""),
            "id_sn": cab[1] or "",
            "nom_socio": cab[2] or "",
            "rnc_ced": cab[3] or "",
            "contacto": cab[4] or "",
            "ent_factura": cab[5] or "",
            "ent_mercancia": cab[6] or "",
            "est_doc": cab[7] or "",
            "fecha_cont": _fmt_date_iso(cab[8]),
            "fecha_doc": _fmt_date_iso(cab[9]),
            "fecha_venc": _fmt_date_iso(cab[10]),
            "comentario": cab[11] or "",
            "subtotal": _num(cab[12]),
            "total_desc": _num(cab[13]),
            "impuesto": _num(cab[14]),
            "total_doc": _num(cab[15]),
            "id_condicion": cab[16],
            "dia": cab[17],
            "condicion": cab[18] or "",
            "id_precio": cab[19],
        },
        "detalles": results,
    }
    return JsonResponse(data)


@require_http_methods(["GET"])
def facturas_buscar_view(request):
    auth_payload = _require_perm_json(request, "factura", "ver_emision")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    query = (request.GET.get("q") or "").strip()
    filtro = (request.GET.get("filtro") or "documento").strip().lower()
    sql = """
        SELECT TOP 80
            ID_DOC, ID_DOC_PV, ID_SN, NOM_SOCIO, RNC_CED, FECHA_DOC, TOTAL_DOC, ISNULL(NCF, ''), ISNULL(TIPO, ''),
            UPPER(ISNULL(CANCELADO, 'N')), ISNULL(NCF_NC, '')
        FROM CAB_FACTURA
        WHERE (TRY_CAST(ID_NCF AS BIGINT) IS NULL OR TRY_CAST(ID_NCF AS BIGINT) <> 34)
    """
    params = []
    if query:
        if filtro == "cliente":
            sql += " AND ID_SN LIKE %s"
            params.append(f"%{query}%")
        elif filtro == "nombre":
            sql += " AND NOM_SOCIO LIKE %s"
            params.append(f"%{query}%")
        elif filtro == "encf":
            sql += " AND ISNULL(NCF, '') LIKE %s"
            params.append(f"%{query}%")
        else:
            sql += " AND CAST(ID_DOC AS VARCHAR(50)) LIKE %s"
            params.append(f"%{query}%")
    if filtro == "cliente":
        sql += " ORDER BY ID_SN, TRY_CAST(ID_DOC AS BIGINT) DESC, ID_DOC DESC"
    elif filtro == "nombre":
        sql += " ORDER BY NOM_SOCIO, TRY_CAST(ID_DOC AS BIGINT) DESC, ID_DOC DESC"
    else:
        sql += " ORDER BY TRY_CAST(ID_DOC AS BIGINT) DESC, ID_DOC DESC"

    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()

    results = []
    for row in rows:
        factura_id = str(row[0] or "")
        results.append(
            {
                "id_doc": factura_id,
                "id_doc_pv": str(row[1] or ""),
                "id_sn": str(row[2] or ""),
                "nom_socio": str(row[3] or ""),
                "rnc_ced": str(row[4] or ""),
                "fecha_doc": _fmt_date_iso(row[5]),
                "total_doc": float(row[6]) if row[6] is not None else 0.0,
                "encf": str(row[7] or ""),
                "tipo": str(row[8] or ""),
                "cancelado": str(row[9] or "") == "Y",
                "ncf_nc": str(row[10] or ""),
                "cancelable": not (str(row[9] or "") == "Y" or str(row[10] or "").strip()),
                "print_url": f"/app/factura/impresion/?id_doc={factura_id}",
            }
        )
    return JsonResponse({"results": results})


@require_http_methods(["POST"])
def cancelar_factura_view(request):
    auth_payload = _require_perm_json(request, "factura", "crear")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"detail": "JSON invalido"}, status=400)

    factura_id = _to_int(payload.get("factura_id"), 0)
    motivo = str(payload.get("motivo") or "").strip() or "Cancelacion mediante nota de credito"
    if factura_id <= 0:
        return JsonResponse({"detail": "factura_id requerido"}, status=400)

    usuario_id = _to_int((auth_payload or {}).get("usuario_id"), 0)
    terminal = socket.gethostname() or "FACTURA"
    today = timezone.localdate()

    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT TOP 1
                    ID_DOC, ID_DOC_PV, ID_SN, NOM_SOCIO, RNC_CED, CONTACTO, FECHA_CONT, FECHA_DOC, FECHA_VENC,
                    ENT_FACTURA, ENT_MERCANCIA, SUBTOTAL, TOTAL_DESC, TOTAL_ITBIS, TOTAL_DOC, MON_DOC,
                    COMENTARIO, ID_CONDICION, DIA, CONDICION, ID_VENDEDOR, CTA_ASOCIADA, ID_GASTO, ID_PRECIO,
                    UPPER(ISNULL(CANCELADO, 'N')), ISNULL(NCF, ''), ISNULL(NCF_NC, '')
                FROM CAB_FACTURA
                WHERE TRY_CAST(ID_DOC AS BIGINT) = %s
                """,
                [factura_id],
            )
            factura = cursor.fetchone()
            if not factura:
                return JsonResponse({"detail": "Factura no encontrada."}, status=404)
            if str(factura[24] or "").strip().upper() == "Y":
                return JsonResponse({"detail": "La factura ya se encuentra cancelada."}, status=400)
            existing_ncf = str(factura[26] or "").strip()
            if existing_ncf:
                existing_nc = _find_existing_credit_note(cursor, factura_id, existing_ncf)
                return JsonResponse(
                    {
                        "ok": True,
                        "already_cancelled": True,
                        "detail": "La factura ya tiene una nota de credito asociada.",
                        "nota_credito_id": (existing_nc or {}).get("id_doc", ""),
                        "encf": (existing_nc or {}).get("encf", existing_ncf),
                        "print_url": (
                            f"/app/factura/impresion/?id_doc={(existing_nc or {}).get('id_doc', '')}"
                            if (existing_nc or {}).get("id_doc")
                            else ""
                        ),
                    }
                )

            existing_nc = _find_existing_credit_note(cursor, factura_id)
            if existing_nc:
                return JsonResponse(
                    {
                        "ok": True,
                        "already_cancelled": True,
                        "detail": "La factura ya fue cancelada con una nota de credito.",
                        "nota_credito_id": existing_nc.get("id_doc", ""),
                        "encf": existing_nc.get("encf", ""),
                        "print_url": (
                            f"/app/factura/impresion/?id_doc={existing_nc.get('id_doc', '')}"
                            if existing_nc.get("id_doc")
                            else ""
                        ),
                    },
                )

            cursor.execute(
                """
                SELECT ISNULL(MAX(TRY_CAST(ID_DOC AS BIGINT)), 0) + 1
                FROM CAB_FACTURA WITH (UPDLOCK, HOLDLOCK)
                """
            )
            row = cursor.fetchone()
            nota_credito_id = int((row[0] or 0)) or 1

            try:
                config, encf_nc = _ensure_ecf_sequence("34")
            except ValueError as exc:
                return _ecf_sequence_error_response("34", exc)

            periodo_cont = str(today.month)
            ejercicio = int(today.year)
            comentario_original = str(factura[16] or "").strip()
            comentario_nc = f"NC por cancelacion de factura {factura_id}. {motivo}".strip()
            if comentario_original:
                comentario_nc = f"{comentario_nc} | Base: {comentario_original}"

            cursor.execute(
                """
                INSERT INTO CAB_FACTURA
                (ID_DOC, ID_DOC_PV, ID_DOC_BASE, TIPO_DOC_BASE, CANCELADO, IMPRESO, EST_DOC, TIPO_DOC,
                 CONTACTO, FECHA_CONT, FECHA_DOC, FECHA_VENC, ID_SN, NOM_SOCIO, RNC_CED, ENT_FACTURA,
                 ENT_MERCANCIA, SUBTOTAL, TOTAL_DESC, TOTAL_ITBIS, TOTAL_DOC, MON_DOC, ABONO, SALDO,
                 COMENTARIO, ID_CONDICION, DIA, CONDICION, ID_VENDEDOR, FECHA_CREACION, ID_NCF, NCF,
                 NCF_NC, TIPO, PERIODO_CONT, ID_USUARIO, TOTAL_BASE, CTA_ASOCIADA, EJERCICIO, ID_GASTO, TERMINAL,
                 ID_PRECIO, FINANCIADO, PRELIMINAR)
                VALUES
                (%s, %s, %s, %s, 'N', 'N', 'Abierto', 'NC',
                 %s, %s, %s, %s, %s, %s, %s, %s,
                 %s, %s, %s, %s, %s, %s, 0, %s,
                 %s, %s, %s, %s, %s, GETDATE(), 34, %s,
                 %s, %s, %s, %s, %s, %s, %s, %s, %s,
                 %s, 'N', 'N')
                """,
                [
                    nota_credito_id,
                    factura[1],
                    factura_id,
                    "FA",
                    factura[5],
                    factura[6],
                    factura[7],
                    factura[8],
                    factura[2],
                    factura[3],
                    factura[4],
                    factura[9],
                    factura[10],
                    -Decimal(factura[11] or 0),
                    -Decimal(factura[12] or 0),
                    -Decimal(factura[13] or 0),
                    -Decimal(factura[14] or 0),
                    str(factura[15] or "RD$"),
                    -Decimal(factura[14] or 0),
                    comentario_nc[:500],
                    factura[17],
                    factura[18],
                    factura[19],
                    factura[20],
                    encf_nc or None,
                    str(factura[25] or "").strip() or None,
                    _tipo_descripcion("34"),
                    periodo_cont,
                    usuario_id,
                    -Decimal(factura[11] or 0),
                    factura[21],
                    ejercicio,
                    factura[22],
                    terminal[:50],
                    factura[23],
                ],
            )

            cursor.execute(
                """
                INSERT INTO DET_FACTURA
                (ID_DOC, ID_DOC_PV, No_LINEA, CLASE_DOC_BASE, REF_DOC_BASE, ESTATUS_LINEA, CLASE_ART, ID_ARTICULO,
                 DESCRIP_ART, CANTIDAD, CANT_ENT, CANT_PEND, CANT_DESP, MEDIDA, COSTO, PRECIO, PRECIO_BRUTO, PORC_DESC,
                 ID_IMPTO, TOTAL_DESC, TOTAL_COSTO, TOTAL_PRECIO, TOTAL_PRECIO_NETO, TOTAL_LINEA, ID_ALMACEN, ID_VENDEDOR,
                 PORC_COM, CTA_INGRESO, CTA_GASTOS, CTA_COSTOS, CTA_INV, CTA_IMPTO, CTA_DEV_VENTA, PRECIO_TRAS_DESC,
                 FECHA_CONT, CEBE, CECO, PERIODO_CONT, EJERCICIO, REFERENCIA, OBSERVACION, CANT_UND, No_LINEA_BASE)
                SELECT
                    %s, ID_DOC_PV, No_LINEA, 'FA', %s, 'C', CLASE_ART, ID_ARTICULO,
                    DESCRIP_ART, -CANTIDAD, -CANT_ENT, 0, -ISNULL(CANT_DESP, CANTIDAD), MEDIDA, COSTO, PRECIO, PRECIO_BRUTO, PORC_DESC,
                    ID_IMPTO, -TOTAL_DESC, -TOTAL_COSTO, -TOTAL_PRECIO, -TOTAL_PRECIO_NETO, -TOTAL_LINEA, ID_ALMACEN, ID_VENDEDOR,
                    PORC_COM, CTA_INGRESO, CTA_GASTOS, CTA_COSTOS, CTA_INV, CTA_IMPTO, CTA_DEV_VENTA, PRECIO_TRAS_DESC,
                    FECHA_CONT, CEBE, CECO, %s, %s, REFERENCIA, OBSERVACION, -ISNULL(CANT_UND, CANTIDAD), No_LINEA
                FROM DET_FACTURA
                WHERE TRY_CAST(ID_DOC AS BIGINT) = %s
                """,
                [nota_credito_id, factura_id, periodo_cont, ejercicio, factura_id],
            )

            cursor.execute(
                """
                UPDATE CAB_FACTURA
                SET CANCELADO = 'Y',
                    EST_DOC = 'Cancelado',
                    NCF_NC = %s,
                    FECHA_ACT = CONVERT(VARCHAR(30), GETDATE(), 121)
                WHERE TRY_CAST(ID_DOC AS BIGINT) = %s
                """,
                [encf_nc or None, factura_id],
            )

        original_doc = FacturacionElectronicaDocumento.objects.filter(id_doc=factura_id).first()
        nc_estado = "REGISTRADO" if encf_nc else ("PENDIENTE_XML" if config and config.habilitado else "FACTURA_GENERADA")
        nc_doc, created = FacturacionElectronicaDocumento.objects.update_or_create(
            id_doc=nota_credito_id,
            defaults={
                "tipo_ecf": "34",
                "encf": encf_nc or None,
                "estado": nc_estado,
                "cliente_rnc": str(factura[4] or "").strip() or None,
                "cliente_nombre": str(factura[3] or "").strip() or None,
                "fecha_doc": factura[7],
                "monto_total": -Decimal(factura[14] or 0),
                "observaciones": f"Nota de credito por cancelacion de factura {factura_id}. {motivo}",
            },
        )
        FacturacionElectronicaEvento.objects.create(
            documento=nc_doc,
            tipo_evento="NOTA_CREDITO_EMITIDA",
            detalle=f"Nota de credito {nota_credito_id} generada para cancelar la factura {factura_id}.",
        )
        if encf_nc and created:
            FacturacionElectronicaEvento.objects.create(
                documento=nc_doc,
                tipo_evento="ENCF_ASIGNADO",
                detalle=f"e-NCF asignado: {encf_nc}",
            )
        if original_doc:
            original_doc.estado = "CANCELADO_NC"
            original_doc.observaciones = f"Cancelada con nota de credito {nota_credito_id}. {motivo}"
            original_doc.save(update_fields=["estado", "observaciones", "actualizado_en"])
            FacturacionElectronicaEvento.objects.create(
                documento=original_doc,
                tipo_evento="FACTURA_CANCELADA",
                detalle=f"Cancelada con nota de credito {nota_credito_id}.",
            )
        dispatch_result = _dispatch_document_if_needed(nc_doc, request, config)

    return JsonResponse(
        {
            "ok": True,
            "factura_id": factura_id,
            "nota_credito_id": nota_credito_id,
            "encf": encf_nc,
            "dispatch_message": dispatch_result.message,
            "print_url": f"/app/factura/impresion/?id_doc={nota_credito_id}",
        }
    )


@require_http_methods(["POST"])
def emitir_factura_view(request):
    auth_payload = _require_perm_json(request, "factura", "crear")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"detail": "JSON invalido"}, status=400)

    prefactura_id = str(payload.get("prefactura_id") or "").strip()
    tipo_ecf = str(payload.get("tipo_ecf") or "32").strip()
    if not prefactura_id:
        return JsonResponse({"detail": "prefactura_id requerido"}, status=400)
    if tipo_ecf not in {codigo for codigo, _ in EMISION_TIPOS}:
        return JsonResponse({"detail": "tipo_ecf invalido"}, status=400)

    usuario_id = _to_int((auth_payload or {}).get("usuario_id"), 0)
    terminal = socket.gethostname() or "FACTURA"
    today = timezone.localdate()

    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT TOP 1 ID_DOC, ID_SN, NOM_SOCIO, RNC_CED, EST_DOC, TOTAL_DOC, FECHA_DOC
                FROM CAB_PEDIDO
                WHERE CAST(ID_DOC AS VARCHAR(50)) = %s
                """,
                [prefactura_id],
            )
            pref = cursor.fetchone()
            if not pref:
                return JsonResponse({"detail": "Prefactura no encontrada."}, status=404)
            if str(pref[4] or "").strip().upper() != "ABIERTO":
                return JsonResponse({"detail": "La prefactura ya no esta abierta."}, status=400)

            if tipo_ecf == "31" and not str(pref[3] or "").strip():
                return JsonResponse({"detail": "La factura de credito fiscal requiere RNC/Ced. del cliente."}, status=400)

            cursor.execute(
                """
                SELECT TOP 1 ID_DOC, ISNULL(NCF, '')
                FROM CAB_FACTURA
                WHERE CAST(ID_DOC_PV AS VARCHAR(50)) = %s
                  AND UPPER(ISNULL(CANCELADO, 'N')) <> 'Y'
                ORDER BY TRY_CAST(ID_DOC AS BIGINT) DESC, ID_DOC DESC
                """,
                [prefactura_id],
            )
            existing = cursor.fetchone()
            if existing:
                return JsonResponse(
                    {
                        "detail": "Esta prefactura ya fue facturada.",
                        "factura_id": str(existing[0] or ""),
                        "encf": str(existing[1] or ""),
                    },
                    status=400,
                )

            cursor.execute(
                """
                SELECT ISNULL(MAX(TRY_CAST(ID_DOC AS BIGINT)), 0) + 1
                FROM CAB_FACTURA WITH (UPDLOCK, HOLDLOCK)
                """
            )
            row = cursor.fetchone()
            factura_id = int((row[0] or 0))
            if factura_id <= 0:
                factura_id = 1

            try:
                config, encf = _ensure_ecf_sequence(tipo_ecf)
            except ValueError as exc:
                return _ecf_sequence_error_response(tipo_ecf, exc)

            id_ncf = int(tipo_ecf)
            tipo_desc = _tipo_descripcion(tipo_ecf)
            periodo_cont = str(today.month)
            ejercicio = int(today.year)

            cursor.execute(
                """
                INSERT INTO CAB_FACTURA
                (ID_DOC, ID_DOC_PV, ID_DOC_BASE, TIPO_DOC_BASE, CANCELADO, IMPRESO, EST_DOC, TIPO_DOC,
                 CONTACTO, FECHA_CONT, FECHA_DOC, FECHA_VENC, ID_SN, NOM_SOCIO, RNC_CED, ENT_FACTURA,
                 ENT_MERCANCIA, SUBTOTAL, TOTAL_DESC, TOTAL_ITBIS, TOTAL_DOC, MON_DOC, ABONO, SALDO,
                 COMENTARIO, ID_CONDICION, DIA, CONDICION, ID_VENDEDOR, FECHA_CREACION, ID_NCF, NCF,
                 TIPO, PERIODO_CONT, ID_USUARIO, TOTAL_BASE, CTA_ASOCIADA, EJERCICIO, ID_GASTO, TERMINAL,
                 ID_PRECIO, FINANCIADO, PRELIMINAR)
                SELECT
                    %s, TRY_CAST(ID_DOC AS BIGINT), TRY_CAST(ID_DOC AS BIGINT), %s, 'N', 'N', 'Abierto', 'FA',
                    CONTACTO, FECHA_CONT, FECHA_DOC, FECHA_VENC, ID_SN, NOM_SOCIO, RNC_CED, ENT_FACTURA,
                    ENT_MERCANCIA, SUBTOTAL, TOTAL_DESC, TOTAL_ITBIS, TOTAL_DOC, ISNULL(MON_DOC, 'RD$'), 0, TOTAL_DOC,
                    COMENTARIO, ID_CONDICION, DIA, CONDICION, ID_VENDEDOR, GETDATE(), %s, %s,
                    %s, %s, %s, SUBTOTAL, CTA_ASOCIADA, %s, ID_GASTO, %s,
                    ID_PRECIO, 'N', 'N'
                FROM CAB_PEDIDO
                WHERE CAST(ID_DOC AS VARCHAR(50)) = %s
                """,
                [
                    factura_id,
                    "PV",
                    id_ncf,
                    encf or None,
                    tipo_desc,
                    periodo_cont,
                    usuario_id,
                    ejercicio,
                    terminal[:50],
                    prefactura_id,
                ],
            )

            cursor.execute(
                """
                INSERT INTO DET_FACTURA
                (ID_DOC, ID_DOC_PV, No_LINEA, CLASE_DOC_BASE, REF_DOC_BASE, ESTATUS_LINEA, CLASE_ART, ID_ARTICULO,
                 DESCRIP_ART, CANTIDAD, CANT_ENT, CANT_PEND, CANT_DESP, MEDIDA, COSTO, PRECIO, PRECIO_BRUTO, PORC_DESC,
                 ID_IMPTO, TOTAL_DESC, TOTAL_COSTO, TOTAL_PRECIO, TOTAL_PRECIO_NETO, TOTAL_LINEA, ID_ALMACEN, ID_VENDEDOR,
                 PORC_COM, CTA_INGRESO, CTA_GASTOS, CTA_COSTOS, CTA_INV, CTA_IMPTO, CTA_DEV_VENTA, PRECIO_TRAS_DESC,
                 FECHA_CONT, CEBE, CECO, PERIODO_CONT, EJERCICIO, REFERENCIA, OBSERVACION, CANT_UND, No_LINEA_BASE)
                SELECT
                    %s, TRY_CAST(ID_DOC AS BIGINT), No_LINEA, 'PV', TRY_CAST(ID_DOC AS BIGINT), 'C', CLASE_ART, ID_ARTICULO,
                    DESCRIP_ART, CANTIDAD, CANTIDAD, 0, CANTIDAD, MEDIDA, COSTO, PRECIO, PRECIO_BRUTO, PORC_DESC,
                    ID_IMPTO, TOTAL_DESC, TOTAL_COSTO, TOTAL_PRECIO, TOTAL_PRECIO_NETO, TOTAL_LINEA, ID_ALMACEN, ID_VENDEDOR,
                    PORC_COM, CTA_INGRESO, CTA_GASTOS, CTA_COSTOS, CTA_INV, CTA_IMPTO, CTA_DEV_VENTA, PRECIO_TRAS_DESC,
                    FECHA_CONT, CEBE, CECO, %s, %s, REFERENCIA, OBSERVACION, CANT_UND, No_LINEA
                FROM DET_PEDIDO
                WHERE CAST(ID_DOC AS VARCHAR(50)) = %s
                """,
                [factura_id, periodo_cont, ejercicio, prefactura_id],
            )

            cursor.execute(
                """
                UPDATE CAB_PEDIDO
                SET EST_DOC = 'Cerrado',
                    FECHA_ACT = CONVERT(VARCHAR(30), GETDATE(), 121)
                WHERE CAST(ID_DOC AS VARCHAR(50)) = %s
                """,
                [prefactura_id],
            )

        doc_estado = "REGISTRADO" if encf else ("PENDIENTE_XML" if config and config.habilitado else "FACTURA_GENERADA")
        doc, created = FacturacionElectronicaDocumento.objects.update_or_create(
            id_doc=factura_id,
            defaults={
                "tipo_ecf": tipo_ecf,
                "encf": encf or None,
                "estado": doc_estado,
                "cliente_rnc": str(pref[3] or "").strip() or None,
                "cliente_nombre": str(pref[2] or "").strip() or None,
                "fecha_doc": pref[6],
                "monto_total": Decimal(pref[5] or 0),
                "observaciones": f"Factura emitida desde prefactura {prefactura_id}",
            },
        )
        FacturacionElectronicaEvento.objects.create(
            documento=doc,
            tipo_evento="FACTURA_EMITIDA",
            detalle=f"Factura {factura_id} generada desde prefactura {prefactura_id}.",
        )
        if encf and created:
            FacturacionElectronicaEvento.objects.create(
                documento=doc,
                tipo_evento="ENCF_ASIGNADO",
                detalle=f"e-NCF asignado: {encf}",
            )
        dispatch_result = _dispatch_document_if_needed(doc, request, config)

    return JsonResponse(
        {
            "ok": True,
            "factura_id": factura_id,
            "prefactura_id": prefactura_id,
            "encf": encf,
            "estado_ecf": doc_estado,
            "dispatch_message": dispatch_result.message,
            "print_url": f"/app/factura/impresion/?id_doc={factura_id}",
        }
    )


@require_http_methods(["GET"])
def factura_print_view(request):
    auth_payload = _require_perm_json(request, "factura", "ver_emision")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    id_doc = (request.GET.get("id_doc") or "").strip()
    if not id_doc:
        return JsonResponse({"detail": "Parametro id_doc requerido"}, status=400)

    empresa = _get_empresa_data()
    factura = None
    detalles = []

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT TOP 1
                ID_DOC, ID_DOC_PV, ID_SN, NOM_SOCIO, RNC_CED, CONTACTO, FECHA_DOC, FECHA_VENC,
                ENT_FACTURA, ENT_MERCANCIA, COMENTARIO, SUBTOTAL, TOTAL_DESC, TOTAL_ITBIS,
                TOTAL_DOC, NCF, ID_NCF, TIPO, CONDICION, DIA, UPPER(ISNULL(CANCELADO, 'N')), ISNULL(NCF_NC, '')
            FROM CAB_FACTURA
            WHERE CAST(ID_DOC AS VARCHAR(50)) = %s
            """,
            [id_doc],
        )
        row = cursor.fetchone()

        if row:
            factura = {
                "id_doc": str(row[0] or ""),
                "id_doc_pv": str(row[1] or ""),
                "id_sn": str(row[2] or ""),
                "nom_socio": str(row[3] or ""),
                "rnc_ced": str(row[4] or ""),
                "contacto": str(row[5] or ""),
                "fecha_doc": row[6],
                "fecha_venc": row[7],
                "ent_factura": str(row[8] or ""),
                "ent_mercancia": str(row[9] or ""),
                "comentario": str(row[10] or ""),
                "subtotal": Decimal(row[11] or 0),
                "total_desc": Decimal(row[12] or 0),
                "total_itbis": Decimal(row[13] or 0),
                "total_doc": Decimal(row[14] or 0),
                "encf": str(row[15] or ""),
                "id_ncf": _to_int(row[16], 0),
                "tipo": str(row[17] or ""),
                "condicion": str(row[18] or ""),
                "dia": _to_int(row[19], 0),
                "cancelado": str(row[20] or "").strip().upper() == "Y",
                "ncf_nc": str(row[21] or ""),
            }

            cursor.execute(
                """
                SELECT No_LINEA, ID_ARTICULO, DESCRIP_ART, CANTIDAD, MEDIDA, PRECIO,
                       PORC_DESC, TOTAL_ITBIS, TOTAL_LINEA
                FROM DET_FACTURA
                WHERE CAST(ID_DOC AS VARCHAR(50)) = %s
                ORDER BY No_LINEA, ID_DETALLE
                """,
                [id_doc],
            )
            for d in cursor.fetchall():
                detalles.append(
                    {
                        "linea": _to_int(d[0], 0),
                        "id_articulo": str(d[1] or ""),
                        "descrip_art": str(d[2] or ""),
                        "cantidad": Decimal(d[3] or 0),
                        "medida": str(d[4] or ""),
                        "precio": Decimal(d[5] or 0),
                        "porc_desc": Decimal(d[6] or 0),
                        "total_itbis": Decimal(d[7] or 0),
                        "total_linea": Decimal(d[8] or 0),
                    }
                )

    if not factura:
        return JsonResponse({"detail": "Factura no encontrada"}, status=404)

    documento_ecf = FacturacionElectronicaDocumento.objects.filter(id_doc=_to_int(id_doc, 0)).first()
    qr_target_url = ""
    qr_image_url = ""
    codigo_seguridad = ""
    estado_ecf = ""
    if documento_ecf:
        codigo_seguridad = str(documento_ecf.codigo_seguridad or "")
        estado_ecf = str(documento_ecf.estado or "")
        qr_target_url = _build_qr_url(empresa.get("rnc", ""), documento_ecf)
        qr_image_url = _build_qr_image_url(qr_target_url)

    return render(
        request,
        "factura/factura_print.html",
        {
            "auth_payload": auth_payload,
            "empresa": empresa,
            "factura": factura,
            "detalles": detalles,
            "documento_ecf": documento_ecf,
            "codigo_seguridad": codigo_seguridad,
            "estado_ecf": estado_ecf,
            "qr_target_url": qr_target_url,
            "qr_image_url": qr_image_url,
            "fecha_impresion": timezone.localtime(),
            "doc_label": "NOTA DE CREDITO / RI e-CF" if factura.get("id_ncf") == 34 else "FACTURA / RI e-CF",
            "doc_numero_label": "No. Nota de credito" if factura.get("id_ncf") == 34 else "No. Factura",
            "detalle_label": "Detalle de Nota de Credito" if factura.get("id_ncf") == 34 else "Detalle de Factura",
        },
    )


@csrf_exempt
@require_http_methods(["POST"])
def recibir_ecf_view(request):
    unauthorized = _require_ecf_callback_auth(request)
    if unauthorized:
        return unauthorized
    payload = _parse_request_payload(request)
    if payload is None:
        return JsonResponse({"detail": "JSON invalido"}, status=400)

    documento = _find_ecf_document(payload)
    if not documento:
        return JsonResponse({"detail": "Documento e-CF no encontrado. Envie id_doc, encf o track_id."}, status=404)

    estado = str(payload.get("estado") or payload.get("estatus") or "RECIBIDO").strip().upper() or "RECIBIDO"
    detalle = str(payload.get("detalle") or payload.get("observacion") or payload.get("mensaje") or "").strip()
    track_id = str(payload.get("track_id") or "").strip()
    codigo_seguridad = str(payload.get("codigo_seguridad") or payload.get("security_code") or "").strip()
    url_consulta_qr = str(payload.get("url_consulta_qr") or payload.get("qr_url") or "").strip()
    empresa = _get_empresa_data()

    update_fields = ["estado", "actualizado_en"]
    documento.estado = estado
    if track_id and not documento.track_id:
        documento.track_id = track_id
        update_fields.append("track_id")
    if codigo_seguridad:
        documento.codigo_seguridad = codigo_seguridad
        update_fields.append("codigo_seguridad")
    if "xml_generado" in payload:
        documento.xml_generado = _boolish(payload.get("xml_generado"))
        update_fields.append("xml_generado")
    if "firmado" in payload:
        documento.firmado = _boolish(payload.get("firmado"))
        update_fields.append("firmado")
    if "enviado_dgii" in payload:
        documento.enviado_dgii = _boolish(payload.get("enviado_dgii"))
        update_fields.append("enviado_dgii")
    if detalle:
        documento.respuesta_dgii = detalle
        update_fields.append("respuesta_dgii")
    if not url_consulta_qr and codigo_seguridad:
        url_consulta_qr = _build_qr_url(empresa.get("rnc", ""), documento)
    if url_consulta_qr:
        documento.url_consulta_qr = url_consulta_qr
        update_fields.append("url_consulta_qr")
    documento.save(update_fields=update_fields)

    FacturacionElectronicaEvento.objects.create(
        documento=documento,
        tipo_evento="RECEPCION_ECF",
        detalle=detalle or f"Recepcion registrada con estado {estado}.",
    )
    return JsonResponse(_build_event_response(documento, "RECEPCION_ECF"))


@csrf_exempt
@require_http_methods(["POST"])
def aprobar_ecf_view(request):
    unauthorized = _require_ecf_callback_auth(request)
    if unauthorized:
        return unauthorized
    payload = _parse_request_payload(request)
    if payload is None:
        return JsonResponse({"detail": "JSON invalido"}, status=400)

    documento = _find_ecf_document(payload)
    if not documento:
        return JsonResponse({"detail": "Documento e-CF no encontrado. Envie id_doc, encf o track_id."}, status=404)

    decision = str(payload.get("decision") or payload.get("aprobacion") or payload.get("estado") or "").strip().lower()
    decision_map = {
        "aprobado": "APROBADO_COMERCIAL",
        "aprobada": "APROBADO_COMERCIAL",
        "approved": "APROBADO_COMERCIAL",
        "aceptado": "APROBADO_COMERCIAL",
        "aceptada": "APROBADO_COMERCIAL",
        "rechazado": "RECHAZADO_COMERCIAL",
        "rechazada": "RECHAZADO_COMERCIAL",
        "rejected": "RECHAZADO_COMERCIAL",
        "devuelto": "RECHAZADO_COMERCIAL",
    }
    estado = decision_map.get(decision, "APROBACION_REGISTRADA")
    detalle = str(payload.get("detalle") or payload.get("observacion") or payload.get("mensaje") or "").strip()
    codigo_seguridad = str(payload.get("codigo_seguridad") or payload.get("security_code") or "").strip()
    url_consulta_qr = str(payload.get("url_consulta_qr") or payload.get("qr_url") or "").strip()
    empresa = _get_empresa_data()

    update_fields = ["estado", "actualizado_en"]
    documento.estado = estado
    if codigo_seguridad:
        documento.codigo_seguridad = codigo_seguridad
        update_fields.append("codigo_seguridad")
    if "xml_generado" in payload:
        documento.xml_generado = _boolish(payload.get("xml_generado"))
        update_fields.append("xml_generado")
    if "firmado" in payload:
        documento.firmado = _boolish(payload.get("firmado"))
        update_fields.append("firmado")
    if "enviado_dgii" in payload:
        documento.enviado_dgii = _boolish(payload.get("enviado_dgii"))
        update_fields.append("enviado_dgii")
    if detalle:
        documento.respuesta_dgii = detalle
        update_fields.append("respuesta_dgii")
    if not url_consulta_qr and codigo_seguridad:
        url_consulta_qr = _build_qr_url(empresa.get("rnc", ""), documento)
    if url_consulta_qr:
        documento.url_consulta_qr = url_consulta_qr
        update_fields.append("url_consulta_qr")
    documento.save(update_fields=update_fields)

    FacturacionElectronicaEvento.objects.create(
        documento=documento,
        tipo_evento="APROBACION_ECF",
        detalle=detalle or f"Aprobacion comercial registrada con estado {estado}.",
    )
    return JsonResponse(_build_event_response(documento, "APROBACION_ECF"))


@require_http_methods(["GET", "POST"])
def electronica_view(request):
    ctx = _base_context(request, page_title="Factura - Facturacion Electronica", active_nav="factura")
    if not ctx:
        return redirect("login")
    if not has_perm(ctx["auth_payload"]["usuario_id"], "factura", "ver_electronica"):
        return render_denied(request, active_nav="factura")

    config, _ = FacturacionElectronicaConfig.objects.get_or_create(
        id_config=1,
        defaults={"habilitado": False, "ambiente": "precertificacion", "modo_envio": "manual"},
    )
    status_message = ""

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip().lower()
        if action == "save_config":
            public_urls = _build_public_ecf_urls(request)
            config.habilitado = _bool_post(request.POST.get("habilitado"))
            config.ambiente = (request.POST.get("ambiente") or "precertificacion").strip() or "precertificacion"
            config.modo_envio = (request.POST.get("modo_envio") or "manual").strip() or "manual"
            config.certificado_ruta = (request.POST.get("certificado_ruta") or "").strip() or None
            config.certificado_clave = (request.POST.get("certificado_clave") or "").strip() or None
            config.url_recepcion_emisor = (request.POST.get("url_recepcion_emisor") or "").strip() or public_urls["recepcion"]
            config.url_aprobacion_emisor = (request.POST.get("url_aprobacion_emisor") or "").strip() or public_urls["aprobacion"]
            config.observaciones = (request.POST.get("observaciones") or "").strip() or None
            config.save()
            status_message = "Configuracion e-CF guardada."
        elif action == "save_sequence":
            tipo_ecf = (request.POST.get("tipo_ecf") or "").strip()
            if tipo_ecf in ECF_TIPOS_MAP:
                secuencia, _ = FacturacionElectronicaSecuencia.objects.get_or_create(
                    tipo_ecf=tipo_ecf,
                    defaults={"descripcion": ECF_TIPOS_MAP[tipo_ecf]},
                )
                secuencia.descripcion = ECF_TIPOS_MAP[tipo_ecf]
                secuencia.habilitada = _bool_post(request.POST.get("habilitada"))
                secuencia.secuencia_actual = max(_to_int(request.POST.get("secuencia_actual"), 1), 1)
                secuencia.secuencia_desde = max(_to_int(request.POST.get("secuencia_desde"), 1), 1)
                secuencia.secuencia_hasta = max(_to_int(request.POST.get("secuencia_hasta"), 0), 0)
                secuencia.vencimiento_secuencia = _to_date_or_none(request.POST.get("vencimiento_secuencia"))
                secuencia.save()
                status_message = f"Secuencia e-CF {tipo_ecf} actualizada."
        elif action == "sync_docs":
            total = _sync_ecf_documentos(limit=200)
            status_message = f"Sincronizados {total} documentos desde CAB_PEDIDO."
        elif action == "demo_qr":
            documento_id = _to_int(request.POST.get("id_doc"), 0)
            empresa = ctx.get("empresa") or {}
            documento = FacturacionElectronicaDocumento.objects.filter(id_doc=documento_id).first()
            if documento and documento.encf:
                _apply_demo_qr(documento, empresa.get("rnc", ""))
                status_message = f"QR demo generado para el documento {documento_id}."
            elif documento:
                status_message = f"El documento {documento_id} aun no tiene e-NCF; no se pudo generar QR demo."
            else:
                status_message = "Documento no encontrado para la prueba QR."

    if not FacturacionElectronicaDocumento.objects.exists():
        _sync_ecf_documentos(limit=80)

    empresa = ctx.get("empresa") or {}
    seq_map = {item.tipo_ecf: item for item in FacturacionElectronicaSecuencia.objects.all()}
    secuencias = []
    for codigo, nombre in ECF_TIPOS:
        secuencia = seq_map.get(codigo)
        preview = ""
        if secuencia:
            preview = f"E{codigo}{int(secuencia.secuencia_actual):010d}"
        secuencias.append({"codigo": codigo, "nombre": nombre, "obj": secuencia, "preview": preview})

    documentos = list(FacturacionElectronicaDocumento.objects.all()[:60])
    for documento in documentos:
        documento.tipo_ecf_nombre = ECF_TIPOS_MAP.get(documento.tipo_ecf or "", "Sin definir")
        documento.url_consulta_qr = _build_qr_url(empresa.get("rnc", ""), documento)

    readiness = {
        "empresa_rnc": bool(str(empresa.get("rnc") or "").strip()),
        "config_activa": bool(config.habilitado),
        "certificado": bool(str(config.certificado_ruta or "").strip()),
        "urls_receptor": bool(str(config.url_recepcion_emisor or "").strip()) and bool(str(config.url_aprobacion_emisor or "").strip()),
        "secuencias": FacturacionElectronicaSecuencia.objects.filter(habilitada=True).exists(),
    }
    readiness["completo"] = all(readiness.values())
    resumen = {
        "total": FacturacionElectronicaDocumento.objects.count(),
        "listos": FacturacionElectronicaDocumento.objects.filter(estado="LISTO_PARA_XML").count(),
        "registrados": FacturacionElectronicaDocumento.objects.filter(estado="REGISTRADO").count(),
        "pendientes_tipo": FacturacionElectronicaDocumento.objects.filter(estado="TIPO_PENDIENTE").count(),
    }
    ecf_public_urls = _build_public_ecf_urls(request)
    runtime_report = build_ecf_runtime_report()

    ctx.update(
        {
            "status_message": status_message,
            "ecf_config": config,
            "ecf_secuencias": secuencias,
            "ecf_documentos": documentos,
            "ecf_resumen": resumen,
            "ecf_readiness": readiness,
            "ecf_public_urls": ecf_public_urls,
            "ecf_runtime": runtime_report,
            "dgii_fuentes": [
                {
                    "title": "Documentacion sobre e-CF",
                    "url": "https://dgii.gov.do/cicloContribuyente/facturacion/comprobantesFiscalesElectronicosE-CF/Paginas/documentacionSobreE-CF.aspx",
                },
                {
                    "title": "Descripcion tecnica de facturacion electronica",
                    "url": "https://dgii.gov.do/cicloContribuyente/facturacion/comprobantesFiscalesElectronicosE-CF/Documentacin%20sobre%20eCF/Informe%20y%20Descripci%C3%B3n%20T%C3%A9cnica/Descripcion-tecnica-de-facturacion-electronica.pdf",
                },
                {
                    "title": "Preguntas Tecnicas e-CF",
                    "url": "https://dgii.gov.do/cicloContribuyente/facturacion/comprobantesFiscalesElectronicosE-CF/Preguntas%20frecuentes/T%C3%A9cnicas/Preguntas%20T%C3%A9cnicas%20e-CF.pdf",
                },
                {
                    "title": "Calendario oficial de implementacion",
                    "url": "https://dgii.gov.do/cicloContribuyente/facturacion/comprobantesFiscalesElectronicosE-CF/Paginas/Listados-contribuyentes-obligados-implementar-facturacion-electronica.aspx",
                },
            ],
        }
    )
    return render(request, "factura/electronica.html", ctx)
