import base64
import json
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.db import connection
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from ajustes.permissions import ensure_admin_role, has_perm

from prefacturas_app.views import _get_auth_payload


def _load_table_columns(table_name):
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COLUMN_NAME
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_NAME = %s
                ORDER BY ORDINAL_POSITION
                """,
                [table_name],
            )
            return [str(row[0]).strip().upper() for row in cursor.fetchall() if row and row[0]]
    except Exception:
        return []


def _bool_db(value):
    return str(value or "").strip().lower() in {"1", "true", "on", "si", "sÃ­", "y", "yes"}


def _get_empresa_data():
    empresa = {
        "nombre": "COMERCIAL ANITA SRL",
        "direccion": "",
        "tel1": "",
        "tel2": "",
        "email": "",
        "rnc": "",
        "logo_b64": "",
        "logo_tipo": "",
        "sello_b64": "",
        "habilitar_fact_stock": False,
    }
    try:
        columns = set(_load_table_columns("EMPRESA"))
        if not columns:
            return empresa
        select_columns = ["NOMBRE", "DIR_EMP", "TEL1", "TEL2", "EMAIL", "RNC_CED"]
        for optional_column in ("LOGO", "LOGO_TIPO", "SELLO", "HABILITAR_FACT_STOCK"):
            if optional_column in columns:
                select_columns.append(optional_column)
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT TOP 1 {', '.join(select_columns)} FROM EMPRESA")
            row = cursor.fetchone()
            if not row:
                return empresa
            data = {select_columns[index]: row[index] for index in range(min(len(select_columns), len(row)))}
            if data.get("NOMBRE"):
                empresa["nombre"] = str(data.get("NOMBRE")).strip()
            empresa["direccion"] = str(data.get("DIR_EMP") or "").strip()
            empresa["tel1"] = str(data.get("TEL1") or "").strip()
            empresa["tel2"] = str(data.get("TEL2") or "").strip()
            empresa["email"] = str(data.get("EMAIL") or "").strip()
            empresa["rnc"] = str(data.get("RNC_CED") or "").strip()
            if data.get("LOGO"):
                empresa["logo_b64"] = base64.b64encode(data.get("LOGO")).decode("ascii")
            empresa["logo_tipo"] = str(data.get("LOGO_TIPO") or "").strip()
            if data.get("SELLO"):
                empresa["sello_b64"] = base64.b64encode(data.get("SELLO")).decode("ascii")
            empresa["habilitar_fact_stock"] = _bool_db(data.get("HABILITAR_FACT_STOCK"))
    except Exception:
        return empresa
    return empresa


def _base_context(request, *, page_title, active_nav):
    auth_payload = _get_auth_payload(request)
    if not auth_payload:
        return None
    ensure_admin_role()
    usuario_id = auth_payload.get("usuario_id")
    visible_modules = {
        "prefacturas": has_perm(usuario_id, "prefacturas", "ver"),
        "clientes": has_perm(usuario_id, "clientes", "ver"),
        "inventario": has_perm(usuario_id, "inventario", "ver"),
        "reportes": has_perm(usuario_id, "reportes", "ver"),
        "etiquetas": has_perm(usuario_id, "etiquetas", "ver"),
        "cobros": has_perm(usuario_id, "cobros", "ver"),
        "cartas": has_perm(usuario_id, "cartas", "ver"),
        "factura": has_perm(usuario_id, "factura", "ver"),
        "caja": has_perm(usuario_id, "caja", "ver"),
        "chat_interno": has_perm(usuario_id, "chat_interno", "ver"),
        "empleados": has_perm(usuario_id, "empleados", "ver"),
        "ajustes": has_perm(usuario_id, "ajustes", "ver"),
        "venta_pos": has_perm(usuario_id, "venta_pos", "ver"),
    }
    stock_request_notifications_enabled = has_perm(usuario_id, "inventario", "ver_solicitudes_existencia")
    empresa = _get_empresa_data()
    return {
        "auth_payload": auth_payload,
        "empresa_nombre": empresa.get("nombre", "COMERCIAL ANITA SRL"),
        "empresa": empresa,
        "page_title": page_title,
        "active_nav": active_nav,
        "visible_modules": visible_modules,
        "stock_request_notifications_enabled": stock_request_notifications_enabled,
    }


def render_denied(request, *, page_title="Acceso denegado", active_nav="dashboard"):
    ctx = _base_context(request, page_title=page_title, active_nav=active_nav)
    if not ctx:
        return redirect("login")
    ctx["denied_message"] = "Acceso denegado. No tienes permiso para ver este modulo."
    return render(request, "core/access_denied.html", ctx, status=403)


def dashboard_view(request):
    ctx = _base_context(request, page_title="Panel general", active_nav="dashboard")
    if not ctx:
        return redirect("login")
    return render(request, "core/dashboard.html", ctx)


def _require_authenticated_ajax(request):
    if not _get_auth_payload(request):
        return JsonResponse({"detail": "Sesion no valida."}, status=401)
    if request.headers.get("X-Requested-With") != "XMLHttpRequest":
        return JsonResponse({"detail": "Solicitud no permitida."}, status=400)
    return None


def _read_qz_file(path_setting):
    path = Path(path_setting)
    if not path.exists():
        raise FileNotFoundError(str(path))
    return path.read_bytes()


@require_GET
def qz_certificate_view(request):
    denied = _require_authenticated_ajax(request)
    if denied:
        return denied
    try:
        certificate = _read_qz_file(settings.QZ_CERTIFICATE_PATH).decode("utf-8")
    except FileNotFoundError:
        return JsonResponse({"detail": "No se encontro el certificado de QZ Tray."}, status=503)
    return HttpResponse(certificate, content_type="text/plain; charset=utf-8")


@require_POST
def qz_sign_view(request):
    denied = _require_authenticated_ajax(request)
    if denied:
        return denied
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"detail": "Solicitud de firma invalida."}, status=400)
    request_data = str(payload.get("request") or "")
    if not request_data:
        return JsonResponse({"detail": "No hay datos para firmar."}, status=400)
    try:
        private_key = serialization.load_pem_private_key(
            _read_qz_file(settings.QZ_PRIVATE_KEY_PATH),
            password=None,
        )
    except FileNotFoundError:
        return JsonResponse({"detail": "No se encontro la llave privada de QZ Tray."}, status=503)
    signature = private_key.sign(
        request_data.encode("utf-8"),
        padding.PKCS1v15(),
        hashes.SHA512(),
    )
    return HttpResponse(base64.b64encode(signature).decode("ascii"), content_type="text/plain; charset=utf-8")
