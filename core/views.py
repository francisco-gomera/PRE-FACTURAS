import base64
from django.db import connection
from django.shortcuts import redirect, render

from ajustes.permissions import ensure_admin_role, has_perm

from prefacturas_app.views import _get_auth_payload


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
    }
    try:
        with connection.cursor() as cursor:
            try:
                cursor.execute(
                    """
                    SELECT TOP 1 NOMBRE, DIR_EMP, TEL1, TEL2, EMAIL, RNC_CED, LOGO, LOGO_TIPO, SELLO
                    FROM EMPRESA
                    """
                )
                row = cursor.fetchone()
                if row:
                    if row[0]:
                        empresa["nombre"] = str(row[0]).strip()
                    empresa["direccion"] = str(row[1]).strip() if row[1] else ""
                    empresa["tel1"] = str(row[2]).strip() if row[2] else ""
                    empresa["tel2"] = str(row[3]).strip() if row[3] else ""
                    empresa["email"] = str(row[4]).strip() if row[4] else ""
                    empresa["rnc"] = str(row[5]).strip() if row[5] else ""
                    if row[6]:
                        empresa["logo_b64"] = base64.b64encode(row[6]).decode("ascii")
                    empresa["logo_tipo"] = str(row[7]).strip() if row[7] else ""
                    if row[8]:
                        empresa["sello_b64"] = base64.b64encode(row[8]).decode("ascii")
            except Exception:
                cursor.execute(
                    """
                    SELECT TOP 1 NOMBRE, DIR_EMP, TEL1, TEL2, EMAIL, RNC_CED
                    FROM EMPRESA
                    """
                )
                row = cursor.fetchone()
                if row:
                    if row[0]:
                        empresa["nombre"] = str(row[0]).strip()
                    empresa["direccion"] = str(row[1]).strip() if row[1] else ""
                    empresa["tel1"] = str(row[2]).strip() if row[2] else ""
                    empresa["tel2"] = str(row[3]).strip() if row[3] else ""
                    empresa["email"] = str(row[4]).strip() if row[4] else ""
                    empresa["rnc"] = str(row[5]).strip() if row[5] else ""
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
        "ajustes": has_perm(usuario_id, "ajustes", "ver"),
    }
    empresa = _get_empresa_data()
    return {
        "auth_payload": auth_payload,
        "empresa_nombre": empresa.get("nombre", "COMERCIAL ANITA SRL"),
        "empresa": empresa,
        "page_title": page_title,
        "active_nav": active_nav,
        "visible_modules": visible_modules,
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
