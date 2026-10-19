import base64
import json
import logging
import re
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie

from .models import CartaPlantilla
from .whatsapp_cloud import (
    WhatsAppCloudError,
    get_missing_settings as get_whatsapp_missing_settings,
    get_verify_token as get_whatsapp_verify_token,
    is_configured as is_whatsapp_cloud_configured,
    send_text_and_document,
)
from ajustes.permissions import has_perm
from ajustes.user_signatures import get_user_signature_bytes
from core.views import _base_context, render_denied


logger = logging.getLogger(__name__)


def _fmt_date(value):
    if not value:
        return ""
    if hasattr(value, "strftime"):
        return value.strftime("%d/%m/%Y")
    return str(value)


def _norm_doc(value):
    if value is None:
        return ""
    val_str = str(value).strip()
    if "." in val_str:
        val_str = val_str.split(".")[0]
    return val_str


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


def _load_cliente_carta(id_sn):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT TOP 1
                m.NOM_SOCIO,
                m.RNC_CED,
                m.DIR_FACTURA,
                m.CONTACTO,
                m.COMENTARIO,
                m.TEL1,
                m.MORA,
                m.TARIFA_INT,
                m.ID_SECTOR,
                ISNULL(t.DESCRIPCION, '')
            FROM MAESTRO_SN m
            LEFT JOIN Territorio t ON t.ID_CODIGO = m.ID_SECTOR
            WHERE m.ID_SN = %s
            """,
            [id_sn],
        )
        row = cursor.fetchone()
    if not row:
        return None
    return {
        "id_sn": id_sn,
        "nombre": row[0] or "",
        "rnc_ced": row[1] or "",
        "direccion": row[2] or "",
        "contacto": row[3] or "",
        "comentario": row[4] or "",
        "telefono": row[5] or "",
        "mora": _to_float(row[6]),
        "tarifa_int": _to_float(row[7]),
        "id_sector": row[8],
        "sector": row[9] or "",
    }


def _load_firma_b64(usuario_id):
    firma_b64 = ""
    try:
        firma_bytes = get_user_signature_bytes(usuario_id)
        if firma_bytes:
            firma_b64 = base64.b64encode(firma_bytes).decode("ascii")
    except Exception:
        firma_b64 = ""
    return firma_b64


def _build_empresa_payload(ctx):
    empresa = (ctx or {}).get("empresa") or {}
    return {
        "nombre": empresa.get("nombre", ""),
        "direccion": empresa.get("direccion", ""),
        "tel1": empresa.get("tel1", ""),
        "tel2": empresa.get("tel2", ""),
        "email": empresa.get("email", ""),
        "rnc": empresa.get("rnc", ""),
        "logo_b64": empresa.get("logo_b64", ""),
        "logo_tipo": empresa.get("logo_tipo", ""),
        "sello_b64": empresa.get("sello_b64", ""),
    }


def _load_plantillas_activas():
    return list(
        CartaPlantilla.objects.filter(activa=True)
        .order_by("nombre")
        .values("id_plantilla", "nombre", "asunto", "cuerpo")
    )


class CartaDataError(Exception):
    def __init__(self, detail, status=400):
        super().__init__(detail)
        self.detail = detail
        self.status = status


def _format_money_str(value):
    return f"{_to_float(value):,.2f}"


def _sanitize_filename_fragment(value):
    text = str(value or "").strip()
    if text:
        text = unicodedata.normalize("NFD", text)
        text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = re.sub(r"[^A-Za-z0-9_-]+", "_", text)
    text = text.strip("_")
    return text or "cliente"


def _normalize_whatsapp_phone(value):
    digits = re.sub(r"\D+", "", str(value or ""))
    if digits.startswith("00"):
        return digits[2:]
    return digits


def _render_token_text(text, variables):
    rendered = str(text or "")
    for key, raw_value in (variables or {}).items():
        rendered = rendered.replace(f"{{{{{key}}}}}", str(raw_value or ""))
    return rendered


def _build_aviso_default_body_text():
    return (
        "Estimado Sr (a),\n\n"
        "Por medio de la presente le informamos que su cuenta se encuentra en ATRASO, "
        "le invitamos a ponerse al dia con nosotros a la brevedad posible, evite cargos por mora.\n\n"
        "El no cumplimiento de sus pagos nos da la autoridad de proceder con la recuperacion "
        "de los articulos y/o electrodomesticos relacionados a su cuenta en atraso. "
        "A continuacion detalle de deuda:"
    )


def _build_aviso_payload(ctx, id_sn):
    id_sn = str(id_sn or "").strip()
    if not id_sn:
        raise CartaDataError("id_sn requerido", status=400)

    try:
        cliente = _load_cliente_carta(id_sn)
    except Exception as exc:
        raise CartaDataError("No se pudo consultar el cliente.", status=500) from exc
    if not cliente:
        raise CartaDataError("Cliente no encontrado", status=404)

    try:
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
    except Exception as exc:
        raise CartaDataError("No se pudo consultar los balances pendientes.", status=500) from exc

    docs = [row[1] for row in rows if row[1] is not None]
    cuotas_by_doc = {}
    if docs:
        try:
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
        except Exception:
            cuotas_by_doc = {}

    facturas = []
    total_saldo = 0.0
    total_mora = 0.0
    tarifa_int = cliente.get("tarifa_int") or 0.0

    for row in rows:
        fecha_doc, id_doc, total_doc, saldo_doc, fecha_venc = row
        cuotas = cuotas_by_doc.get(id_doc, [])
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
                if dias <= 0:
                    continue
                monto = _to_float(cuota.get("cuota"))
                pagado = max(monto - saldo_val, 0.0)
                mora = (saldo_val * tarifa_int / 100.0) * dias if tarifa_int > 0 else 0.0
                facturas.append(
                    {
                        "id_doc": id_doc or "",
                        "no_cuota": cuota.get("no_cuota") or "Abierto",
                        "fecha_doc": _fmt_date(cuota.get("fecha") or fecha_doc),
                        "fecha_venc": _fmt_date(fecha_venc_cuota),
                        "monto": monto,
                        "pagado": pagado,
                        "saldo": saldo_val,
                        "dias": dias,
                        "mora": mora,
                    }
                )
                total_saldo += saldo_val
                total_mora += mora
        else:
            saldo_val = _to_float(saldo_doc)
            if saldo_val <= 0:
                continue
            dias = _days_overdue(fecha_venc)
            if dias <= 0:
                continue
            total_doc_val = _to_float(total_doc)
            pagado = max(total_doc_val - saldo_val, 0.0)
            mora = (saldo_val * tarifa_int / 100.0) * dias if tarifa_int > 0 else 0.0
            facturas.append(
                {
                    "id_doc": id_doc or "",
                    "no_cuota": "Abierto",
                    "fecha_doc": _fmt_date(fecha_doc),
                    "fecha_venc": _fmt_date(fecha_venc),
                    "monto": total_doc_val,
                    "pagado": pagado,
                    "saldo": saldo_val,
                    "dias": dias,
                    "mora": mora,
                }
            )
            total_saldo += saldo_val
            total_mora += mora

    overdue_doc_ids = list(set(f["id_doc"] for f in facturas))
    articulos_atraso = []
    if overdue_doc_ids:
        placeholders = ", ".join(["%s"] * len(overdue_doc_ids))
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT DISTINCT DESCRIP_ART
                    FROM DET_FACTURA
                    WHERE ID_DOC IN ({placeholders})
                    """,
                    overdue_doc_ids
                )
                art_rows = cursor.fetchall()
            articulos_atraso = [r[0].strip() for r in art_rows if r[0]]
        except Exception:
            pass

    return {
        "cliente": cliente,
        "facturas": facturas,
        "fecha": timezone.localdate().strftime("%d/%m/%Y"),
        "hora": timezone.localtime().strftime("%I:%M:%S %p").lstrip("0"),
        "firma_b64": _load_firma_b64(ctx["auth_payload"]["usuario_id"]),
        "empresa": _build_empresa_payload(ctx),
        "totales": {
            "saldo": total_saldo,
            "mora": total_mora,
        },
        "articulos_atraso": ", ".join(articulos_atraso),
    }


def _build_aviso_template_vars(aviso_payload, ciudad_impresion):
    cliente = (aviso_payload or {}).get("cliente") or {}
    empresa = (aviso_payload or {}).get("empresa") or {}
    totales = (aviso_payload or {}).get("totales") or {}
    facturas = (aviso_payload or {}).get("facturas") or []
    ciudad = str(ciudad_impresion or cliente.get("sector") or "").strip()
    return {
        "cliente_nombre": cliente.get("nombre", ""),
        "cliente_rnc": cliente.get("rnc_ced", ""),
        "cliente_direccion": cliente.get("direccion", ""),
        "cliente_ciudad": ciudad,
        "cliente_telefono": cliente.get("telefono", ""),
        "cliente_sector": cliente.get("sector", ""),
        "cliente_apodo": cliente.get("contacto", ""),
        "articulos_atraso": aviso_payload.get("articulos_atraso", ""),
        "fecha": aviso_payload.get("fecha", ""),
        "hora": aviso_payload.get("hora", ""),
        "empresa_nombre": empresa.get("nombre", ""),
        "ciudad_impresion": ciudad,
        "carta_titulo": "CARTA DE AVISO",
        "balance_atraso": _format_money_str(totales.get("saldo")),
        "total_mora": _format_money_str(totales.get("mora")),
        "facturas_count": len(facturas),
    }


def _build_aviso_document_context(aviso_payload, ciudad_impresion="", plantilla_id=""):
    cliente = (aviso_payload or {}).get("cliente") or {}
    empresa = (aviso_payload or {}).get("empresa") or {}
    totales = (aviso_payload or {}).get("totales") or {}
    facturas = (aviso_payload or {}).get("facturas") or []
    ciudad = str(ciudad_impresion or cliente.get("sector") or "").strip()
    variables = _build_aviso_template_vars(aviso_payload, ciudad)

    plantilla = None
    if str(plantilla_id or "").strip():
        plantilla = CartaPlantilla.objects.filter(id_plantilla=plantilla_id, activa=True).first()

    aviso_asunto = ""
    aviso_body_text = _build_aviso_default_body_text()
    if plantilla and str(plantilla.cuerpo or "").strip():
        aviso_asunto = _render_token_text(plantilla.asunto, variables).strip()
        aviso_body_text = _render_token_text(plantilla.cuerpo, variables).strip()

    logo_tipo = empresa.get("logo_tipo") or "image/png"
    logo_b64 = empresa.get("logo_b64") or ""
    sello_b64 = empresa.get("sello_b64") or ""
    firma_b64 = aviso_payload.get("firma_b64") or ""

    empresa_telefonos = "  ".join([value for value in [empresa.get("tel1", ""), empresa.get("tel2", "")] if value])

    return {
        "document_title": (
            f"carta_aviso_{_sanitize_filename_fragment(cliente.get('id_sn') or 'cliente')}"
            f"_{_sanitize_filename_fragment(cliente.get('nombre') or 'cliente')}"
        ),
        "empresa_nombre": empresa.get("nombre", "") or "COMERCIAL ANITA SRL",
        "empresa_direccion": empresa.get("direccion", ""),
        "empresa_telefonos": empresa_telefonos,
        "empresa_email": empresa.get("email", ""),
        "empresa_rnc": empresa.get("rnc", ""),
        "empresa_logo_src": f"data:{logo_tipo};base64,{logo_b64}" if logo_b64 else "",
        "empresa_sello_src": f"data:image/png;base64,{sello_b64}" if sello_b64 else "",
        "firma_src": f"data:image/png;base64,{firma_b64}" if firma_b64 else "",
        "cliente_nombre": cliente.get("nombre", ""),
        "cliente_rnc": cliente.get("rnc_ced", ""),
        "cliente_direccion": cliente.get("direccion", ""),
        "cliente_telefono": cliente.get("telefono", ""),
        "cliente_apodo": cliente.get("contacto", ""),
        "ciudad_impresion": ciudad,
        "articulos_atraso": aviso_payload.get("articulos_atraso", ""),
        "fecha": aviso_payload.get("fecha", ""),
        "hora": aviso_payload.get("hora", ""),
        "aviso_asunto": aviso_asunto,
        "aviso_body_text": aviso_body_text,
        "facturas": [
            {
                "id_doc": item.get("id_doc", ""),
                "no_cuota": item.get("no_cuota", "Abierto"),
                "fecha_venc": item.get("fecha_venc", ""),
                "monto_fmt": _format_money_str(item.get("monto")),
                "pagado_fmt": _format_money_str(item.get("pagado")),
                "saldo_fmt": _format_money_str(item.get("saldo")),
                "mora_fmt": _format_money_str(item.get("mora")),
                "dias": item.get("dias", 0),
            }
            for item in facturas
        ],
        "total_saldo_fmt": _format_money_str(totales.get("saldo")),
        "total_mora_fmt": _format_money_str(totales.get("mora")),
    }


def _build_default_whatsapp_message(aviso_payload):
    cliente = (aviso_payload or {}).get("cliente") or {}
    totales = (aviso_payload or {}).get("totales") or {}
    facturas = (aviso_payload or {}).get("facturas") or []
    lines = [
        f"Hola {cliente.get('nombre') or 'cliente'},",
        f"Le compartimos su carta de aviso emitida el {aviso_payload.get('fecha', '')}.",
    ]
    if facturas:
        lines.append(f"Documentos en atraso: {len(facturas)}.")
    lines.append(f"Balance en atraso: {_format_money_str(totales.get('saldo'))}.")
    lines.append(f"Total mora: {_format_money_str(totales.get('mora'))}.")
    lines.append("Quedamos atentos para ayudarle con la regularizacion de su cuenta.")
    return "\n".join(lines)


def _generate_aviso_document(aviso_payload, ciudad_impresion="", plantilla_id=""):
    context = _build_aviso_document_context(
        aviso_payload,
        ciudad_impresion=ciudad_impresion,
        plantilla_id=plantilla_id,
    )
    html = render_to_string("cartas/aviso_document.html", context)

    cliente = (aviso_payload or {}).get("cliente") or {}
    timestamp = timezone.localtime().strftime("%Y%m%d%H%M%S")
    unique_suffix = uuid.uuid4().hex[:8]
    filename = (
        f"carta_aviso_{_sanitize_filename_fragment(cliente.get('id_sn') or 'cliente')}"
        f"_{_sanitize_filename_fragment(cliente.get('nombre') or 'cliente')}"
        f"_{timestamp}_{unique_suffix}.doc"
    )
    folder = Path(settings.MEDIA_ROOT) / "cartas_whatsapp"
    folder.mkdir(parents=True, exist_ok=True)
    file_path = folder / filename
    file_path.write_text("\ufeff" + html, encoding="utf-8")
    return file_path, filename


def index(request):
    ctx = _base_context(request, page_title="Cartas", active_nav="cartas")
    if not ctx:
        return redirect("login")
    if not has_perm(ctx["auth_payload"]["usuario_id"], "cartas", "ver"):
        return render_denied(request, active_nav="cartas")
    ctx["submodules"] = {
        "cartas_aviso": has_perm(ctx["auth_payload"]["usuario_id"], "cartas", "ver_cartas_aviso"),
        "cartas_saldo": has_perm(ctx["auth_payload"]["usuario_id"], "cartas", "ver_cartas_saldo"),
        "plantillas": has_perm(ctx["auth_payload"]["usuario_id"], "cartas", "ver_plantillas"),
    }
    return render(request, "cartas/index.html", ctx)


def saldo_view(request):
    ctx = _base_context(request, page_title="Cartas - Saldo", active_nav="cartas")
    if not ctx:
        return redirect("login")
    if not has_perm(ctx["auth_payload"]["usuario_id"], "cartas", "ver_cartas_saldo"):
        return render_denied(request, active_nav="cartas")
    ctx["plantillas_activas"] = _load_plantillas_activas()
    return render(request, "cartas/saldo.html", ctx)


@ensure_csrf_cookie
def aviso_view(request):
    ctx = _base_context(request, page_title="Cartas - Aviso", active_nav="cartas")
    if not ctx:
        return redirect("login")
    if not has_perm(ctx["auth_payload"]["usuario_id"], "cartas", "ver_cartas_aviso"):
        return render_denied(request, active_nav="cartas")
    ctx["plantillas_activas"] = _load_plantillas_activas()
    ctx["whatsapp_cloud_enabled"] = is_whatsapp_cloud_configured()
    ctx["whatsapp_cloud_missing"] = ", ".join(get_whatsapp_missing_settings())
    return render(request, "cartas/aviso.html", ctx)


def plantillas_view(request):
    ctx = _base_context(request, page_title="Cartas - Plantillas", active_nav="cartas")
    if not ctx:
        return redirect("login")
    if not has_perm(ctx["auth_payload"]["usuario_id"], "cartas", "ver_plantillas"):
        return render_denied(request, active_nav="cartas")

    usuario_id = ctx["auth_payload"]["usuario_id"]
    q = (request.GET.get("q") or "").strip()
    edit_id = (request.GET.get("edit") or "").strip()
    status = (request.GET.get("status") or "").strip().lower()
    selected = None
    error_message = ""

    if request.method == "POST":
        action = (request.POST.get("action") or "save").strip().lower()
        plantilla_id = (request.POST.get("id_plantilla") or "").strip()

        if action == "toggle" and plantilla_id:
            plantilla = CartaPlantilla.objects.filter(id_plantilla=plantilla_id).first()
            if not plantilla:
                return redirect("cartas:plantillas")
            plantilla.activa = not bool(plantilla.activa)
            plantilla.save(update_fields=["activa", "fecha_modificacion"])
            return redirect("cartas:plantillas")

        nombre = (request.POST.get("nombre") or "").strip()
        asunto = (request.POST.get("asunto") or "").strip()
        cuerpo = (request.POST.get("cuerpo") or "").strip()
        activa = (request.POST.get("activa") or "").strip() == "1"

        if not nombre:
            error_message = "El nombre es obligatorio."
        elif not asunto:
            error_message = "El asunto es obligatorio."
        elif not cuerpo:
            error_message = "El cuerpo es obligatorio."
        else:
            duplicate_qs = CartaPlantilla.objects.filter(nombre__iexact=nombre)
            if plantilla_id:
                duplicate_qs = duplicate_qs.exclude(id_plantilla=plantilla_id)
            if duplicate_qs.exists():
                error_message = "Ya existe una plantilla con ese nombre."

        if not error_message:
            if plantilla_id:
                plantilla = CartaPlantilla.objects.filter(id_plantilla=plantilla_id).first()
                if not plantilla:
                    error_message = "La plantilla seleccionada no existe."
                else:
                    plantilla.nombre = nombre
                    plantilla.asunto = asunto
                    plantilla.cuerpo = cuerpo
                    plantilla.activa = activa
                    plantilla.save()
                    return redirect("cartas:plantillas")
            else:
                CartaPlantilla.objects.create(
                    nombre=nombre,
                    asunto=asunto,
                    cuerpo=cuerpo,
                    activa=activa,
                    creado_por_id=int(usuario_id),
                )
                return redirect(f"{reverse('cartas:plantillas')}?status=created")

        selected = {
            "id_plantilla": plantilla_id,
            "nombre": nombre,
            "asunto": asunto,
            "cuerpo": cuerpo,
            "activa": activa,
        }
    elif edit_id:
        selected = CartaPlantilla.objects.filter(id_plantilla=edit_id).first()

    plantillas = CartaPlantilla.objects.all().order_by("-activa", "nombre")
    if q:
        plantillas = plantillas.filter(Q(nombre__icontains=q) | Q(asunto__icontains=q) | Q(cuerpo__icontains=q))

    ctx["plantillas"] = plantillas
    ctx["selected_plantilla"] = selected
    ctx["plantilla_query"] = q
    ctx["plantilla_status"] = status
    ctx["plantilla_error"] = error_message
    ctx["placeholders"] = [
        "{{cliente_nombre}}",
        "{{cliente_rnc}}",
        "{{cliente_direccion}}",
        "{{cliente_ciudad}}",
        "{{cliente_telefono}}",
        "{{cliente_sector}}",
        "{{fecha}}",
        "{{hora}}",
        "{{empresa_nombre}}",
        "{{ciudad_impresion}}",
        "{{carta_titulo}}",
        "{{saldo_total}}",
        "{{facturas_count}}",
        "{{balance_atraso}}",
        "{{total_mora}}",
        "{{articulos}}",
    ]
    return render(request, "cartas/plantillas.html", ctx)




def saldo_detalle_view(request):
    ctx = _base_context(request, page_title="Cartas - Saldo", active_nav="cartas")
    if not ctx:
        return JsonResponse({"detail": "No autenticado"}, status=401)
    if not has_perm(ctx["auth_payload"]["usuario_id"], "cartas", "ver_cartas_saldo"):
        return JsonResponse({"detail": "Acceso denegado"}, status=403)

    id_sn = (request.GET.get("id_sn") or "").strip()
    if not id_sn:
        return JsonResponse({"detail": "id_sn requerido"}, status=400)

    try:
        cliente = _load_cliente_carta(id_sn)
    except Exception:
        return JsonResponse({"detail": "No se pudo consultar el cliente."}, status=500)
    if not cliente:
        return JsonResponse({"detail": "Cliente no encontrado"}, status=404)

    facturas = []
    cuotas_meta_by_doc = {}
    articulos_by_doc = {}
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT TOP 200 ID_DOC, FECHA_DOC, TOTAL_DOC, SALDO, MONICA
                FROM CAB_FACTURA
                WHERE ID_SN = %s
                  AND ABS(ISNULL(SALDO, 0)) <= 0.0001
                  AND UPPER(ISNULL(EST_DOC, '')) = 'CERRADO'
                ORDER BY FECHA_DOC DESC, ID_DOC DESC
                """,
                [id_sn],
            )
            rows = cursor.fetchall()

        docs = [row[0] for row in rows if row and row[0]]
        if docs:
            placeholders = ", ".join(["%s"] * len(docs))
            with connection.cursor() as cursor:
                try:
                    cursor.execute(
                        f"""
                        SELECT NO_DOC, COUNT(*), MAX(NO_CUOTA)
                        FROM DET_PRESTAMO
                        WHERE NO_DOC IN ({placeholders})
                        GROUP BY NO_DOC
                        """,
                        docs,
                    )
                    for no_doc, total_cuotas, max_no_cuota in cursor.fetchall():
                        cuotas_meta_by_doc[no_doc] = {
                            "total_cuotas": int(total_cuotas or 0),
                            "no_cuota": max_no_cuota,
                        }
                except Exception:
                    pass

                try:
                    cursor.execute(
                        f"""
                        SELECT ID_DOC, ID_ARTICULO, DESCRIP_ART, CANTIDAD, PRECIO, TOTAL_LINEA
                        FROM DET_FACTURA
                        WHERE ID_DOC IN ({placeholders})
                        ORDER BY ID_DOC, No_LINEA, ID_DETALLE
                        """,
                        docs,
                    )
                    for doc_id, id_articulo, descrip_art, cantidad, precio, total_linea in cursor.fetchall():
                        key = _norm_doc(doc_id)
                        articulos_by_doc.setdefault(key, []).append(
                            {
                                "id_articulo": str(id_articulo or "").strip(),
                                "descripcion": str(descrip_art or "").strip(),
                                "cantidad": _to_float(cantidad),
                                "precio": _to_float(precio),
                                "total": _to_float(total_linea),
                            }
                        )
                except Exception:
                    pass
    except Exception:
        return JsonResponse({"detail": "No se pudo consultar las facturas."}, status=500)

    for r in rows:
        id_doc = r[0] or ""
        fecha_doc = r[1]
        fecha_str = ""
        if isinstance(fecha_doc, datetime):
            fecha_str = fecha_doc.strftime("%d/%m/%Y")
        elif fecha_doc:
            try:
                fecha_str = fecha_doc.strftime("%d/%m/%Y")
            except Exception:
                fecha_str = str(fecha_doc)

        total_doc = _to_float(r[2])
        saldo_doc = _to_float(r[3])
        monica_raw = r[4]

        if monica_raw is not None:
            monica_val = _to_float(monica_raw)
            total_doc = monica_val
            pagado = monica_val
        else:
            pagado = total_doc - saldo_doc

        fecha_pago_str = ""
        try:
            with connection.cursor() as cursor2:
                cursor2.execute(
                    """
                    SELECT MAX(COALESCE(c.FECHA_DOC, c.FECHA_CONT, d.FECHA_CONT, c.FECHA_CREACION))
                    FROM CAB_RECIBO_INGRESO c
                    INNER JOIN DET_RECIBO_INGRESO d ON c.ID_RECIBO = d.ID_RECIBO
                    WHERE c.ID_SN = %s AND d.NO_DOC = %s
                      AND ISNULL(c.CANCELADO, 'N') <> 'Y'
                      AND UPPER(ISNULL(c.EST_DOC, '')) <> 'CANCELADO'
                    """,
                    [id_sn, id_doc],
                )
                row_pago = cursor2.fetchone()
                if row_pago and row_pago[0]:
                    fecha_pago_dt = row_pago[0]
                    if isinstance(fecha_pago_dt, datetime):
                        fecha_pago_str = fecha_pago_dt.strftime("%d/%m/%Y")
        except Exception as e:
            fecha_pago_str = f"Error: {e}"

        norm_key = _norm_doc(id_doc)
        doc_articulos = articulos_by_doc.get(norm_key, [])
        articulos_str = ", ".join([a["descripcion"] for a in doc_articulos if a.get("descripcion")])

        facturas.append(
            {
                "id_doc": id_doc,
                "no_cuota": str(cuotas_meta_by_doc.get(id_doc, {}).get("no_cuota") or "Abierto"),
                "fecha_doc": fecha_str,
                "total_doc": total_doc,
                "pagado": pagado,
                "saldo": saldo_doc,
                "fecha_pago": fecha_pago_str,
                "articulos": doc_articulos,
                "articulos_str": articulos_str,
            }
        )

    firma_b64 = _load_firma_b64(ctx["auth_payload"]["usuario_id"])

    return JsonResponse(
        {
            "cliente": cliente,
            "facturas": facturas,
            "fecha": timezone.localdate().strftime("%d/%m/%Y"),
            "hora": timezone.localtime().strftime("%I:%M:%S %p").lstrip("0"),
            "firma_b64": firma_b64,
            "empresa": _build_empresa_payload(ctx),
        }
    )


def aviso_detalle_view(request):
    ctx = _base_context(request, page_title="Cartas - Aviso", active_nav="cartas")
    if not ctx:
        return JsonResponse({"detail": "No autenticado"}, status=401)
    if not has_perm(ctx["auth_payload"]["usuario_id"], "cartas", "ver_cartas_aviso"):
        return JsonResponse({"detail": "Acceso denegado"}, status=403)

    try:
        payload = _build_aviso_payload(ctx, request.GET.get("id_sn"))
    except CartaDataError as exc:
        return JsonResponse({"detail": exc.detail}, status=exc.status)
    return JsonResponse(payload)


def aviso_whatsapp_send_view(request):
    if request.method != "POST":
        return JsonResponse({"detail": "Metodo no permitido"}, status=405)

    ctx = _base_context(request, page_title="Cartas - Aviso", active_nav="cartas")
    if not ctx:
        return JsonResponse({"detail": "No autenticado"}, status=401)
    if not has_perm(ctx["auth_payload"]["usuario_id"], "cartas", "ver_cartas_aviso"):
        return JsonResponse({"detail": "Acceso denegado"}, status=403)
    if not is_whatsapp_cloud_configured():
        return JsonResponse(
            {
                "detail": "WhatsApp Cloud API no esta configurada.",
                "missing": get_whatsapp_missing_settings(),
            },
            status=503,
        )

    try:
        payload = json.loads((request.body or b"{}").decode("utf-8"))
    except json.JSONDecodeError:
        return JsonResponse({"detail": "JSON invalido."}, status=400)

    id_sn = str(payload.get("id_sn") or "").strip()
    telefono = _normalize_whatsapp_phone(payload.get("telefono"))
    ciudad_impresion = str(payload.get("ciudad") or "").strip()
    plantilla_id = str(payload.get("plantilla_id") or "").strip()
    mensaje = str(payload.get("mensaje") or "").strip()

    if not id_sn:
        return JsonResponse({"detail": "id_sn requerido."}, status=400)
    if len(telefono) < 8:
        return JsonResponse({"detail": "Telefono de WhatsApp invalido."}, status=400)

    try:
        aviso_payload = _build_aviso_payload(ctx, id_sn)
    except CartaDataError as exc:
        return JsonResponse({"detail": exc.detail}, status=exc.status)

    if not mensaje:
        mensaje = _build_default_whatsapp_message(aviso_payload)

    try:
        file_path, filename = _generate_aviso_document(
            aviso_payload,
            ciudad_impresion=ciudad_impresion,
            plantilla_id=plantilla_id,
        )
        provider_response = send_text_and_document(
            telefono,
            mensaje,
            file_path,
            mime_type="application/msword",
        )
    except WhatsAppCloudError as exc:
        provider_status = exc.status_code or 502
        response_status = 400 if 400 <= provider_status < 500 else 502
        return JsonResponse(
            {
                "detail": exc.detail,
                "provider_status": provider_status,
                "provider": exc.payload,
            },
            status=response_status,
        )
    except Exception as exc:
        logger.exception("No se pudo enviar la carta de aviso por WhatsApp.")
        return JsonResponse(
            {
                "detail": "No se pudo preparar o enviar la carta por WhatsApp.",
                "error": str(exc),
            },
            status=500,
        )

    document_message_id = ""
    text_message_id = ""
    try:
        document_message_id = (
            ((provider_response.get("document") or {}).get("messages") or [{}])[0].get("id") or ""
        )
        text_message_id = (
            ((provider_response.get("text") or {}).get("messages") or [{}])[0].get("id") or ""
        )
    except Exception:
        document_message_id = ""
        text_message_id = ""

    return JsonResponse(
        {
            "detail": "Carta enviada por WhatsApp correctamente.",
            "document_filename": filename,
            "document_message_id": document_message_id,
            "text_message_id": text_message_id,
            "provider": provider_response,
        }
    )


@csrf_exempt
def whatsapp_webhook_view(request):
    if request.method == "GET":
        verify_token = get_whatsapp_verify_token()
        mode = str(request.GET.get("hub.mode") or "").strip()
        token = str(request.GET.get("hub.verify_token") or "").strip()
        challenge = str(request.GET.get("hub.challenge") or "")
        if not verify_token:
            return JsonResponse({"detail": "WHATSAPP_VERIFY_TOKEN no configurado."}, status=503)
        if mode == "subscribe" and token == verify_token:
            return HttpResponse(challenge)
        return JsonResponse({"detail": "Verificacion invalida."}, status=403)

    if request.method == "POST":
        try:
            payload = json.loads((request.body or b"{}").decode("utf-8"))
        except json.JSONDecodeError:
            return JsonResponse({"detail": "JSON invalido."}, status=400)
        logger.info("WhatsApp webhook payload: %s", json.dumps(payload, ensure_ascii=True))
        return JsonResponse({"received": True})

    return JsonResponse({"detail": "Metodo no permitido"}, status=405)
