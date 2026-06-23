from pathlib import Path

from django.conf import settings
from django.core import signing
from django.db import connection
from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse, HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone

from ajustes.permissions import has_perm, ensure_admin_role
from ajustes.models import SegUsuarioRol
from core.realtime import broadcast_prefacturas_refresh, broadcast_prefactura_document_status
from django.views.decorators.http import require_http_methods
from datetime import datetime, time
from functools import lru_cache
import json
import os
import socket
import uuid
from decimal import Decimal

from .models import EtiquetaFormatoUsuario
from .models_existing import DetPedido, MaestroSn, MaestroArticulo, Usuario

AUTH_COOKIE_NAME = "prefacturas_auth_v2"
LEGACY_AUTH_COOKIE_NAMES = ["prefacturas_auth"]
DELPHI_CLAVE_KEY = bytes([0x50, 0x53, 0x46, 0xE7, 0x42, 0x24, 0xBF, 0x44])

def _perm_denied_json():
    return JsonResponse({"detail": "Acceso denegado."}, status=403)


def _require_perm_json(request, modulo, permiso):
    auth_payload = _get_auth_payload(request)
    if not auth_payload:
        return JsonResponse({"detail": "No autenticado"}, status=401)
    if not has_perm(auth_payload.get("usuario_id"), modulo, permiso):
        return _perm_denied_json()
    return auth_payload


def _require_perm_or_denied(request, modulo, permiso):
    auth_payload = _get_auth_payload(request)
    if not auth_payload:
        return redirect("login")
    if not has_perm(auth_payload.get("usuario_id"), modulo, permiso):
        return HttpResponse("Acceso denegado.", status=403)
    return auth_payload


@lru_cache(maxsize=None)
def _load_table_columns_cached(table_name):
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
        return tuple(str(row[0]).strip().upper() for row in cursor.fetchall() if row and row[0])


def _pick_existing_table_column(columns, *candidates):
    available = {str(column).upper(): str(column).upper() for column in columns}
    for candidate in candidates:
        if not candidate:
            continue
        found = available.get(str(candidate).upper())
        if found:
            return found
    return None


def _table_column_is_identity(table_name, column_name):
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COLUMNPROPERTY(OBJECT_ID(%s), %s, 'IsIdentity')",
                [table_name, column_name],
            )
            row = cursor.fetchone()
        return bool(row and int(row[0] or 0) == 1)
    except Exception:
        return False


def _get_open_ed_balance(id_sn):
    id_sn = str(id_sn or "").strip()
    if not id_sn:
        return 0.0

    det_columns = _load_table_columns_cached("DET_ED")
    cab_columns = _load_table_columns_cached("CAB_ED")
    if not det_columns or not cab_columns:
        return 0.0

    det_client_col = _pick_existing_table_column(det_columns, "ID_SN", "CLIENTE", "COD_CLIENTE")
    det_debito_col = _pick_existing_table_column(det_columns, "DEBITO", "DEBE")
    det_credito_col = _pick_existing_table_column(det_columns, "CREDITO", "HABER")
    if not det_client_col or not det_debito_col or not det_credito_col:
        return 0.0

    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT
                COALESCE(SUM(COALESCE(d.[{det_debito_col}], 0)), 0) -
                COALESCE(SUM(COALESCE(d.[{det_credito_col}], 0)), 0)
            FROM DET_ED d
            WHERE d.[{det_client_col}] = %s
            """,
            [id_sn],
        )
        row = cursor.fetchone()
    return float(row[0]) if row and row[0] is not None else 0.0


def _normalize_bloqueado(value):
    return str(value or "").strip().upper()


def _articulos_bloqueados_info(cursor, article_ids):
    ids = sorted({str(x or "").strip() for x in article_ids if str(x or "").strip()})
    if not ids:
        return []
    placeholders = ", ".join(["%s"] * len(ids))
    cursor.execute(
        f"""
        SELECT ID_ARTICULO, ISNULL(DESCRIP_ART, ''), ISNULL(BLOQUEADO, 'N')
        FROM MAESTRO_ARTICULO
        WHERE ID_ARTICULO IN ({placeholders})
        """,
        ids,
    )
    blocked = []
    for row in cursor.fetchall():
        if _normalize_bloqueado(row[2]) == "Y":
            blocked.append(
                {
                    "id_articulo": str(row[0] or "").strip(),
                    "descrip_art": str(row[1] or "").strip(),
                }
            )
    return blocked


def _build_foto_url(foto_value):
    if not foto_value:
        return ""
    foto_str = str(foto_value).strip()
    if not foto_str:
        return ""
    if foto_str.startswith(("http://", "https://", "/media/")):
        return foto_str

    normalized = foto_str.replace("\\", "/")
    try:
        abs_path = Path(foto_str)
        if abs_path.is_absolute():
            rel = abs_path.resolve().relative_to(Path(settings.MEDIA_ROOT).resolve())
            return f"{settings.MEDIA_URL}{str(rel).replace('\\', '/')}"
    except Exception:
        pass

    return f"{settings.MEDIA_URL}{normalized.lstrip('/')}"


def _get_auth_payload(request):
    token = request.COOKIES.get(AUTH_COOKIE_NAME)
    if not token:
        return None
    try:
        return signing.loads(token, max_age=getattr(settings, "AUTH_COOKIE_MAX_AGE", 60 * 60 * 24 * 365 * 5))
    except signing.BadSignature:
        return None


def _delphi_clave_transform(value):
    raw = str(value or "")
    if not raw:
        return ""
    try:
        data = raw.encode("cp1252")
    except UnicodeEncodeError:
        data = raw.encode("latin-1")
    transformed = bytes(
        byte ^ DELPHI_CLAVE_KEY[index % len(DELPHI_CLAVE_KEY)]
        for index, byte in enumerate(data)
    )
    try:
        return transformed.decode("cp1252")
    except UnicodeDecodeError:
        return transformed.decode("latin-1")


def _delphi_clave_matches(password_value, clave_value):
    try:
        return _delphi_clave_transform(clave_value) == str(password_value or "")
    except Exception:
        return False


def _encode_delphi_clave(password_value):
    try:
        return _delphi_clave_transform(password_value)
    except Exception:
        return ""


def _get_usuario_activo(usuario_input):
    query = """
        SELECT TOP 1 ID_USUARIO, ISNULL(NOMBRE, USUARIO) AS NOMBRE, USUARIO, ISNULL([CLAVE], '')
        FROM USUARIO
        WHERE USUARIO = %s
          AND UPPER(ISNULL(ESTADO, '')) = 'ACTIVO'
    """
    with connection.cursor() as cursor:
        cursor.execute(query, [usuario_input])
        row = cursor.fetchone()
    if not row:
        return None
    return {
        "usuario_id": int(row[0]),
        "usuario_nombre": row[1],
        "usuario_login": row[2],
        "clave": str(row[3] or ""),
    }


def _has_any_usuario():
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT TOP 1 1 FROM USUARIO")
            return bool(cursor.fetchone())
    except Exception:
        return False


def _upsert_empresa(empresa_data):
    empresa_columns = set(_load_table_columns_cached("EMPRESA"))
    if not empresa_columns:
        return

    mapping = [
        ("NOMBRE", empresa_data.get("nombre", "")),
        ("DIR_EMP", empresa_data.get("direccion", "")),
        ("TEL1", empresa_data.get("tel1", "")),
        ("TEL2", empresa_data.get("tel2", "")),
        ("EMAIL", empresa_data.get("email", "")),
        ("RNC_CED", empresa_data.get("rnc", "")),
        ("HABILITAR_FACT_STOCK", 1 if empresa_data.get("habilitar_fact_stock") else 0),
    ]
    valid_columns = [(col, value) for col, value in mapping if col in empresa_columns]
    if not valid_columns:
        return

    with connection.cursor() as cursor:
        cursor.execute("SELECT TOP 1 ID_EMPRESA FROM EMPRESA")
        row = cursor.fetchone()
        if row:
            set_clause = ", ".join(f"[{col}] = %s" for col, _ in valid_columns)
            params = [value for _, value in valid_columns] + [row[0]]
            cursor.execute(f"UPDATE EMPRESA SET {set_clause} WHERE ID_EMPRESA = %s", params)
        else:
            cols = ", ".join(f"[{col}]" for col, _ in valid_columns)
            placeholders = ", ".join(["%s"] * len(valid_columns))
            params = [value for _, value in valid_columns]
            cursor.execute(f"INSERT INTO EMPRESA ({cols}) VALUES ({placeholders})", params)


def _insert_departamentos(departamentos):
    dept_columns = set(_load_table_columns_cached("DEPARTAMENTO"))
    if not dept_columns:
        return

    ceco_col = _pick_existing_table_column(dept_columns, "CECO", "CENTRO_COSTO", "ID_DEPTO", "DEPARTAMENTO", "CODIGO")
    descripcion_col = _pick_existing_table_column(dept_columns, "DESCRIPCION", "DESCRIP", "NOMBRE", "NOM_DEPTO")
    id_col = _pick_existing_table_column(dept_columns, "ID_CODIGO", "ID_DEPTO", "ID_DEPARTAMENTO")
    if not ceco_col or not descripcion_col:
        return

    id_is_identity = True
    if id_col:
        id_is_identity = _table_column_is_identity("DEPARTAMENTO", id_col)

    next_id = None
    if id_col and not id_is_identity:
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT COALESCE(MAX([{id_col}]), 0) + 1 FROM DEPARTAMENTO")
            next_id = cursor.fetchone()[0] or 1

    with connection.cursor() as cursor:
        for dep in departamentos:
            ceco = str(dep.get("ceco") or "").strip()
            descripcion = str(dep.get("descripcion") or "").strip()
            if not ceco and not descripcion:
                continue

            if id_col and not id_is_identity:
                cursor.execute(
                    f"INSERT INTO DEPARTAMENTO ([{id_col}], [{ceco_col}], [{descripcion_col}]) VALUES (%s, %s, %s)",
                    [next_id, ceco, descripcion],
                )
                next_id += 1
            else:
                cursor.execute(
                    f"INSERT INTO DEPARTAMENTO ([{ceco_col}], [{descripcion_col}]) VALUES (%s, %s)",
                    [ceco, descripcion],
                )


def _save_usuario_formato_preferencia(usuario_id, formatos):
    if not usuario_id or not isinstance(formatos, dict):
        return
    try:
        EtiquetaFormatoUsuario.objects.update_or_create(
            id_usuario=usuario_id,
            defaults={"formato_json": json.dumps(formatos, ensure_ascii=False)},
        )
    except Exception:
        pass


def _assign_admin_role_to_user(usuario_id):
    try:
        admin_role = ensure_admin_role()
        if not admin_role:
            return
        SegUsuarioRol.objects.get_or_create(id_usuario=usuario_id, rol=admin_role)
    except Exception:
        pass


def _build_login_response(payload):
    response = redirect("inicio")
    response.set_cookie(
        AUTH_COOKIE_NAME,
        signing.dumps(
            {
                "usuario_id": payload["usuario_id"],
                "usuario_nombre": payload["usuario_nombre"],
                "usuario_login": payload["usuario_login"],
            }
        ),
        max_age=getattr(settings, "AUTH_COOKIE_MAX_AGE", 60 * 60 * 24 * 365 * 5),
        httponly=True,
        samesite="Lax",
    )
    return response


@require_http_methods(["GET", "POST"])
def login_view(request):
    if _get_auth_payload(request):
        return redirect("inicio")

    error_message = None
    info_message = None
    setup_error_message = None
    setup_info_message = None
    setup_complete = False
    usuario_input = ""
    setup_mode = not _has_any_usuario()
    step = "setup" if setup_mode else "usuario"

    if request.method == "POST":
        action = (request.POST.get("action") or "identify").strip().lower()
        usuario_input = (request.POST.get("usuario") or "").strip()

        if setup_mode and action == "setup_complete":
            empresa_nombre = (request.POST.get("empresa_nombre") or "").strip()
            empresa_direccion = (request.POST.get("empresa_direccion") or "").strip()
            empresa_tel1 = (request.POST.get("empresa_tel1") or "").strip()
            empresa_tel2 = (request.POST.get("empresa_tel2") or "").strip()
            empresa_email = (request.POST.get("empresa_email") or "").strip()
            empresa_rnc = (request.POST.get("empresa_rnc") or "").strip()
            ui_theme_mode = (request.POST.get("ui_theme_mode") or "light").strip()
            formato_recibo_pago = (request.POST.get("formato_recibo_pago") or "a4").strip()
            formato_factura = (request.POST.get("formato_factura") or "a4").strip()
            habilitar_fact_stock = bool(request.POST.get("habilitar_fact_stock"))
            departments_json = request.POST.get("departments_data") or "[]"
            admin_usuario = (request.POST.get("admin_usuario") or "").strip()
            admin_nombre = (request.POST.get("admin_nombre") or "").strip()
            admin_password = request.POST.get("admin_password") or ""
            admin_password_confirm = request.POST.get("admin_password_confirm") or ""
            admin_departamento = (request.POST.get("admin_departamento") or "").strip()
            admin_departamento_ceco = (request.POST.get("admin_departamento_ceco") or "").strip()

            if not empresa_nombre:
                setup_error_message = "Debes ingresar el nombre de la empresa."
            elif not admin_usuario:
                setup_error_message = "Debes ingresar el usuario administrador."
            elif not admin_nombre:
                setup_error_message = "Debes ingresar el nombre del administrador."
            elif not admin_departamento:
                setup_error_message = "Debes seleccionar un departamento para el administrador."
            elif len(admin_password) < 4:
                setup_error_message = "La contraseña del administrador debe tener al menos 4 caracteres."
            elif len(admin_password) > 12:
                setup_error_message = "La contraseña no puede tener mas de 12 caracteres."
            elif admin_password != admin_password_confirm:
                setup_error_message = "Las contraseñas no coinciden."
            else:
                try:
                    departments = json.loads(departments_json)
                    if not isinstance(departments, list) or not departments:
                        raise ValueError("Debe ingresar al menos un departamento.")
                except Exception:
                    departments = []
                    setup_error_message = "Los datos de departamentos no son válidos."

            if not setup_error_message:
                if not departments:
                    setup_error_message = "Debe ingresar al menos un departamento."

            if not setup_error_message:
                try:
                    with transaction.atomic():
                        _upsert_empresa(
                            {
                                "nombre": empresa_nombre,
                                "direccion": empresa_direccion,
                                "tel1": empresa_tel1,
                                "tel2": empresa_tel2,
                                "email": empresa_email,
                                "rnc": empresa_rnc,
                                "habilitar_fact_stock": habilitar_fact_stock,
                            }
                        )
                        _insert_departamentos(departments)

                        encoded_password = _encode_delphi_clave(admin_password)
                        if not encoded_password:
                            raise ValueError("La contraseña contiene caracteres no permitidos.")

                        departamento_ceco = admin_departamento_ceco or str(departments[0].get("ceco") or "").strip()
                        departamento_depto = admin_departamento or str(departments[0].get("descripcion") or "").strip()
                        user = Usuario(
                            usuario=admin_usuario,
                            nombre=admin_nombre,
                            estado="ACTIVO",
                            ceco=departamento_ceco or None,
                            depto=departamento_depto or None,
                            nivel="Administrador",
                            porc_desc=Decimal("0.00"),
                            conectado="N",
                            id_empresa=1,
                            cambiar_clave="N",
                            id_caja=1,
                            pos="N",
                            preliminar="N",
                            terminal="",
                        )
                        user.clave = encoded_password
                        user.clave_nueva = encoded_password
                        user.save()
                        _save_usuario_formato_preferencia(
                            user.id_usuario,
                            {
                                "recibo_pago": formato_recibo_pago,
                                "factura": formato_factura,
                            },
                        )
                        _assign_admin_role_to_user(user.id_usuario)
                        setup_complete = True
                        setup_info_message = "¡Configuración inicial completada! Redirigiendo al login..."
                except ValueError as exc:
                    setup_error_message = str(exc)
                except Exception:
                    setup_error_message = "No fue posible guardar la configuración inicial. Intenta de nuevo."

            step = "setup"
        elif setup_mode:
            step = "setup"
        else:
            if not usuario_input:
                error_message = "Debes ingresar un usuario."
            else:
                payload = _get_usuario_activo(usuario_input)
                if not payload:
                    error_message = "Usuario no encontrado o inactivo."
                elif action == "identify":
                    if payload["clave"]:
                        step = "password_login"
                        info_message = "Usuario encontrado. Ingresa tu contraseña."
                    else:
                        step = "password_create"
                        info_message = "Este usuario no tiene contraseña. Crea una nueva."
                elif action == "login_password":
                    step = "password_login"
                    password_value = request.POST.get("password") or ""
                    if not password_value:
                        error_message = "Debes ingresar la contraseña."
                    else:
                        ok_password = bool(payload["clave"]) and _delphi_clave_matches(password_value, payload["clave"])
                        if ok_password:
                            return _build_login_response(payload)
                        error_message = "Contraseña incorrecta."
                elif action == "set_password":
                    step = "password_create"
                    if payload["clave"]:
                        step = "password_login"
                        info_message = "Este usuario ya tiene contraseña. Ingresa la contraseña actual."
                    else:
                        password_new = request.POST.get("password_new") or ""
                        password_confirm = request.POST.get("password_confirm") or ""
                        if len(password_new) < 4:
                            error_message = "La contraseña debe tener al menos 4 caracteres."
                        elif len(password_new) > 12:
                            error_message = "La contraseña no puede tener mas de 12 caracteres."
                        elif password_new != password_confirm:
                            error_message = "Las contraseñas no coinciden."
                        else:
                            clave_codificada = _encode_delphi_clave(password_new)
                            if not clave_codificada:
                                error_message = "La contraseña contiene caracteres no permitidos."
                            else:
                                with connection.cursor() as cursor:
                                    cursor.execute(
                                        """
                                        UPDATE USUARIO
                                        SET [CLAVE] = %s,
                                            [CLAVE_NUEVA] = %s
                                        WHERE ID_USUARIO = %s
                                        """,
                                        [clave_codificada, clave_codificada, int(payload["usuario_id"])],
                                    )
                                payload["clave"] = clave_codificada
                                return _build_login_response(payload)
                else:
                    error_message = "Accion no valida."

    return render(
        request,
        "prefacturas_app/login.html",
        {
            "error_message": error_message,
            "info_message": info_message,
            "step": step,
            "usuario_input": usuario_input,
            "setup_mode": setup_mode,
            "setup_error_message": setup_error_message,
            "setup_info_message": setup_info_message,
            "setup_complete": setup_complete,
        },
    )


def inicio_view(request):
    auth_payload = _get_auth_payload(request)
    if not auth_payload:
        return redirect("login")
    sectores = []
    empresa_nombre = "COMERCIAL ANITA SRL"
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
            sectores = [
                {"id_codigo": row[0], "descripcion": row[1]}
                for row in cursor.fetchall()
            ]
    except Exception:
        sectores = []
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT TOP 1 NOMBRE
                FROM EMPRESA
                WHERE NOMBRE IS NOT NULL AND LTRIM(RTRIM(NOMBRE)) <> ''
                """
            )
            row = cursor.fetchone()
            if row and row[0]:
                empresa_nombre = str(row[0]).strip()
    except Exception:
        empresa_nombre = "COMERCIAL ANITA SRL"

    return render(
        request,
        "prefacturas_app/inicio.html",
        {"auth_payload": auth_payload, "sectores": sectores, "empresa_nombre": empresa_nombre},
    )


def logout_view(request):
    response = redirect("login")
    response.delete_cookie(AUTH_COOKIE_NAME)
    for legacy_name in LEGACY_AUTH_COOKIE_NAMES:
        response.delete_cookie(legacy_name)
    return response


@require_http_methods(["POST"])
def cambiar_password_view(request):
    auth_payload = _get_auth_payload(request)
    if not auth_payload:
        return JsonResponse({"detail": "No autenticado"}, status=401)
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"detail": "JSON invalido"}, status=400)

    password_nueva = str(payload.get("password_nueva") or "")
    password_confirm = str(payload.get("password_confirm") or "")
    if len(password_nueva) < 4:
        return JsonResponse({"detail": "La nueva contraseña debe tener al menos 4 caracteres."}, status=400)
    if len(password_nueva) > 12:
        return JsonResponse({"detail": "La nueva contraseña no puede tener mas de 12 caracteres."}, status=400)
    if password_nueva != password_confirm:
        return JsonResponse({"detail": "La confirmación no coincide."}, status=400)

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT TOP 1 ID_USUARIO
            FROM USUARIO
            WHERE ID_USUARIO = %s
              AND UPPER(ISNULL(ESTADO, '')) = 'ACTIVO'
            """,
            [int(auth_payload["usuario_id"])],
        )
        row = cursor.fetchone()
        if not row:
            return JsonResponse({"detail": "Usuario no encontrado o inactivo."}, status=404)
        clave_nueva = _encode_delphi_clave(password_nueva)
        if not clave_nueva:
            return JsonResponse({"detail": "La nueva contraseña contiene caracteres no permitidos."}, status=400)

        cursor.execute(
            """
            UPDATE USUARIO
            SET [CLAVE] = %s,
                [CLAVE_NUEVA] = %s
            WHERE ID_USUARIO = %s
            """,
            [clave_nueva, clave_nueva, int(auth_payload["usuario_id"])],
        )

    return JsonResponse({"ok": True})


@require_http_methods(["GET"])
def estado_cuenta_print_view(request):
    auth_payload = _require_perm_or_denied(request, "clientes", "ver_estado_cuenta")
    if not isinstance(auth_payload, dict):
        return auth_payload

    id_sn = (request.GET.get("id_sn") or "").strip()
    cliente = None
    balance = 0.0
    facturas_abiertas = []

    if id_sn:
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
        if cliente:
            balance = _get_open_ed_balance(id_sn)

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

            facturas_abiertas = []
            for row in rows:
                fecha_doc, id_doc, total_doc, saldo_doc, fecha_venc = row
                cuotas = cuotas_by_doc.get(id_doc, [])
                if cuotas:
                    # Si existe financiamiento, se muestran las cuotas en lugar de la factura.
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
                    saldo_doc_val = _to_float(saldo_doc)
                    facturas_abiertas.append(
                        {
                            "fecha_doc": _fmt_date(fecha_doc),
                            "id_doc": id_doc,
                            "total_doc": _to_float(total_doc),
                            "saldo": saldo_doc_val,
                            "fecha_venc": _fmt_date(fecha_venc),
                            "dias": _days_overdue(fecha_venc),
                        }
                    )

    return render(
        request,
        "prefacturas_app/estado_cuenta_print.html",
        {
            "auth_payload": auth_payload,
            "cliente": cliente,
            "balance": balance,
            "facturas_abiertas": facturas_abiertas,
            "fecha_impresion": timezone.localdate(),
        },
    )


@require_http_methods(["GET"])
def buscar_stock_articulos_view(request):
    auth_payload = _require_perm_json(request, "inventario", "stock_buscar")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    q = (request.GET.get("q") or "").strip()
    stock_desde_raw = (request.GET.get("stock_desde") or "").strip()
    stock_hasta_raw = (request.GET.get("stock_hasta") or "").strip()

    def _to_dec_or_none(v):
        if v == "":
            return None
        try:
            return Decimal(str(v))
        except Exception:
            return None

    stock_desde = _to_dec_or_none(stock_desde_raw)
    stock_hasta = _to_dec_or_none(stock_hasta_raw)

    sql = """
        SELECT TOP 800 A.ID_ARTICULO, A.DESCRIP_ART, COALESCE(T.STOCK_TARJ, 0) AS STOCK, A.UM_INV, A.REFERENCIA
        FROM MAESTRO_ARTICULO A
        LEFT JOIN (
            SELECT ID_ARTICULO, SUM(CANTIDAD) AS STOCK_TARJ
            FROM TARJETERO
            GROUP BY ID_ARTICULO
        ) T ON T.ID_ARTICULO = A.ID_ARTICULO
        WHERE 1=1
    """
    params = []
    if q:
        sql += " AND (A.ID_ARTICULO LIKE %s OR A.DESCRIP_ART LIKE %s OR A.REFERENCIA LIKE %s)"
        like = f"%{q}%"
        params.extend([like, like, like])
    if stock_desde is not None:
        sql += " AND COALESCE(T.STOCK_TARJ, 0) >= %s"
        params.append(stock_desde)
    if stock_hasta is not None:
        sql += " AND COALESCE(T.STOCK_TARJ, 0) <= %s"
        params.append(stock_hasta)
    sql += " ORDER BY A.DESCRIP_ART, A.ID_ARTICULO"

    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()

    results = []
    for r in rows:
        try:
            stock = float(r[2] or 0)
        except Exception:
            stock = 0.0
        results.append(
            {
                "id_articulo": r[0] or "",
                "descrip_art": r[1] or "",
                "stock": stock,
                "uom": r[3] or "",
                "referencia": r[4] or "",
            }
        )
    return JsonResponse({"results": results})


@require_http_methods(["GET"])
def stock_articulos_print_view(request):
    auth_payload = _require_perm_or_denied(request, "inventario", "stock_imprimir")
    if not isinstance(auth_payload, dict):
        return auth_payload

    q = (request.GET.get("q") or "").strip()
    stock_desde_raw = (request.GET.get("stock_desde") or "").strip()
    stock_hasta_raw = (request.GET.get("stock_hasta") or "").strip()

    def _to_dec_or_none(v):
        if v == "":
            return None
        try:
            return Decimal(str(v))
        except Exception:
            return None

    stock_desde = _to_dec_or_none(stock_desde_raw)
    stock_hasta = _to_dec_or_none(stock_hasta_raw)

    sql = """
        SELECT A.ID_ARTICULO, A.DESCRIP_ART, COALESCE(T.STOCK_TARJ, 0) AS STOCK, A.UM_INV, A.REFERENCIA
        FROM MAESTRO_ARTICULO A
        LEFT JOIN (
            SELECT ID_ARTICULO, SUM(CANTIDAD) AS STOCK_TARJ
            FROM TARJETERO
            GROUP BY ID_ARTICULO
        ) T ON T.ID_ARTICULO = A.ID_ARTICULO
        WHERE 1=1
    """
    params = []
    if q:
        sql += " AND (A.ID_ARTICULO LIKE %s OR A.DESCRIP_ART LIKE %s OR A.REFERENCIA LIKE %s)"
        like = f"%{q}%"
        params.extend([like, like, like])
    if stock_desde is not None:
        sql += " AND COALESCE(T.STOCK_TARJ, 0) >= %s"
        params.append(stock_desde)
    if stock_hasta is not None:
        sql += " AND COALESCE(T.STOCK_TARJ, 0) <= %s"
        params.append(stock_hasta)
    sql += " ORDER BY A.DESCRIP_ART, A.ID_ARTICULO"

    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()

    articulos = []
    total_stock = 0.0
    for r in rows:
        try:
            stock = float(r[2] or 0)
        except Exception:
            stock = 0.0
        total_stock += stock
        articulos.append(
            {
                "id_articulo": r[0] or "",
                "descrip_art": r[1] or "",
                "stock": stock,
                "uom": r[3] or "",
                "referencia": r[4] or "",
            }
        )

    return render(
        request,
        "prefacturas_app/stock_articulos_print.html",
        {
            "articulos": articulos,
            "total_stock": total_stock,
            "q": q,
            "stock_desde": stock_desde_raw,
            "stock_hasta": stock_hasta_raw,
            "fecha_impresion": timezone.localdate(),
            "usuario_nombre": auth_payload.get("usuario_nombre", ""),
        },
    )


@require_http_methods(["GET"])
def buscar_clientes_view(request):
    auth_payload = _require_perm_json(request, "clientes", "ver")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    query = (request.GET.get("q") or "").strip()
    filtro = (request.GET.get("filtro") or "nombre").strip().lower()
    clientes = MaestroSn.objects.exclude(bloqueado__iexact="Y")

    if query:
        if filtro == "codigo":
            clientes = clientes.filter(id_sn__icontains=query)
        elif filtro == "apodo":
            clientes = clientes.filter(contacto__icontains=query)
        elif filtro == "rnc":
            clientes = clientes.filter(rnc_ced__icontains=query)
        elif filtro == "telefono":
            clientes = clientes.filter(Q(tel1__icontains=query) | Q(tel2__icontains=query))
        else:
            clientes = clientes.filter(nom_socio__icontains=query)

    if filtro == "codigo":
        clientes = clientes.order_by("id_sn")
    elif filtro == "apodo":
        clientes = clientes.order_by("contacto", "id_sn")
    elif filtro == "rnc":
        clientes = clientes.order_by("rnc_ced", "id_sn")
    elif filtro == "telefono":
        clientes = clientes.order_by("tel1", "tel2", "id_sn")
    else:
        clientes = clientes.order_by("nom_socio", "id_sn")

    clientes = clientes.values(
        "id_sn",
        "nom_socio",
        "rnc_ced",
        "contacto",
        "dir_factura",
        "tel1",
        "bloqueado",
    )[:50]

    return JsonResponse({"results": list(clientes)})


@require_http_methods(["GET"])
def buscar_prefacturas_view(request):
    auth_payload = _require_perm_json(request, "prefacturas", "ver")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    query = (request.GET.get("q") or "").strip()
    filtro = (request.GET.get("filtro") or "documento").strip().lower()
    sql = """
        SELECT TOP 50
            ID_DOC, ID_SN, NOM_SOCIO, RNC_CED, CONTACTO, ENT_FACTURA, ENT_MERCANCIA, EST_DOC, FECHA_CONT, FECHA_DOC, FECHA_VENC, COMENTARIO, TOTAL_DOC
        FROM CAB_PEDIDO
    """
    params = []
    if query:
        if filtro == "cliente":
            sql += " WHERE ID_SN LIKE %s"
            params.append(f"%{query}%")
        elif filtro == "nombre":
            sql += " WHERE NOM_SOCIO LIKE %s"
            params.append(f"%{query}%")
        else:
            sql += " WHERE CAST(ID_DOC AS VARCHAR(50)) LIKE %s"
            params.append(f"%{query}%")

    if filtro == "cliente":
        sql += " ORDER BY ID_SN, ID_DOC DESC"
    elif filtro == "nombre":
        sql += " ORDER BY NOM_SOCIO, ID_DOC DESC"
    else:
        sql += " ORDER BY TRY_CAST(ID_DOC AS BIGINT) DESC, ID_DOC DESC"

    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        pedidos = cursor.fetchall()

    def _fmt_date(value):
        if not value:
            return ""
        try:
            return value.strftime("%Y-%m-%d")
        except Exception:
            return ""

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
                "fecha_cont": _fmt_date(p[8]),
                "fecha_doc": _fmt_date(p[9]),
                "fecha_venc": _fmt_date(p[10]),
                "comentario": p[11] or "",
                "total_doc": float(p[12]) if p[12] is not None else 0.0,
            }
        )

    return JsonResponse({"results": results})


@require_http_methods(["GET"])
def buscar_articulos_view(request):
    auth_payload = _require_perm_json(request, "inventario", "stock_buscar")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    q = (request.GET.get("q") or "").strip()
    filtro = (request.GET.get("filtro") or "descripcion").strip().lower()
    qs = MaestroArticulo.objects.exclude(bloqueado__iexact="Y")

    if q:
        if filtro == "codigo":
            qs = qs.filter(referencia__icontains=q)
        else:
            qs = qs.filter(descrip_art__icontains=q)

    if filtro == "codigo":
        qs = qs.order_by("referencia", "id_articulo")
    else:
        qs = qs.order_by("id_articulo")

    values = list(qs.values(
        "id_articulo",
        "descrip_art",
        "referencia",
        "precio_det",
        "id_impto_vt",
        "bloqueado",
    )[:80])

    def _num(v):
        if v is None:
            return None
        try:
            return float(v)
        except Exception:
            return None

    results = []
    if values:
        articulo_ids = [row.get("id_articulo") for row in values if row.get("id_articulo")]
        tarj_stock = {}
        if articulo_ids:
            with connection.cursor() as cursor:
                placeholders = ", ".join(["%s"] * len(articulo_ids))
                cursor.execute(
                    f"SELECT ID_ARTICULO, COALESCE(SUM(CANTIDAD), 0) FROM TARJETERO WHERE ID_ARTICULO IN ({placeholders}) GROUP BY ID_ARTICULO",
                    articulo_ids
                )
                tarj_stock = {str(row[0] or "").strip(): float(row[1] or 0) for row in cursor.fetchall()}

        for r in values:
            art_id = r.get("id_articulo") or ""
            stock_val = tarj_stock.get(art_id, 0.0)
            results.append(
                {
                    "id_articulo": art_id,
                    "descrip_art": r.get("descrip_art") or "",
                    "referencia": r.get("referencia") or "",
                    "precio_det": _num(r.get("precio_det")),
                    "stock": stock_val,
                    "id_impto_vt": r.get("id_impto_vt"),
                    "bloqueado": (r.get("bloqueado") or "N"),
                }
            )
    return JsonResponse({"results": results})


@require_http_methods(["GET"])
def buscar_grupos_articulos_view(request):
    auth_payload = _require_perm_json(request, "inventario", "grupos_buscar")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    q = (request.GET.get("q") or "").strip()
    filtro = (request.GET.get("filtro") or "descripcion").strip().lower()
    sql = """
        SELECT TOP 120 ID_GRUPO, CODIGO, DESCRIPCION
        FROM GRUPO_ARTICULO_CAB
        WHERE ISNULL(ACTIVO, 'Y') <> 'N'
    """
    params = []
    if q:
        if filtro == "codigo":
            sql += " AND CODIGO LIKE %s"
            params.append(f"%{q}%")
        else:
            sql += " AND DESCRIPCION LIKE %s"
            params.append(f"%{q}%")
    if filtro == "codigo":
        sql += " ORDER BY CODIGO"
    else:
        sql += " ORDER BY DESCRIPCION, CODIGO"

    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()

    return JsonResponse(
        {
            "results": [
                {
                    "id_grupo": int(r[0]),
                    "codigo": r[1] or "",
                    "descripcion": r[2] or "",
                }
                for r in rows
            ]
        }
    )


@require_http_methods(["GET"])
def detalle_grupo_articulos_view(request):
    auth_payload = _require_perm_json(request, "inventario", "grupos_buscar")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    id_grupo_raw = (request.GET.get("id_grupo") or "").strip()
    if not id_grupo_raw:
        return JsonResponse({"detail": "id_grupo requerido"}, status=400)
    try:
        id_grupo = int(id_grupo_raw)
    except Exception:
        return JsonResponse({"detail": "id_grupo invalido"}, status=400)

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT ID_GRUPO, CODIGO, DESCRIPCION
            FROM GRUPO_ARTICULO_CAB
            WHERE ID_GRUPO = %s
            """,
            [id_grupo],
        )
        cab = cursor.fetchone()
        if not cab:
            return JsonResponse({"detail": "Grupo no encontrado"}, status=404)

        cursor.execute(
            """
            SELECT D.ID_DET, D.ID_ARTICULO, A.REFERENCIA, A.DESCRIP_ART, D.CANTIDAD
            FROM GRUPO_ARTICULO_DET D
            LEFT JOIN MAESTRO_ARTICULO A ON A.ID_ARTICULO = D.ID_ARTICULO
            WHERE D.ID_GRUPO = %s
            ORDER BY D.ORDEN, D.ID_DET
            """,
            [id_grupo],
        )
        det_rows = cursor.fetchall()

    def _num(v):
        try:
            return float(v)
        except Exception:
            return 0.0

    return JsonResponse(
        {
            "grupo": {
                "id_grupo": int(cab[0]),
                "codigo": cab[1] or "",
                "descripcion": cab[2] or "",
            },
            "detalles": [
                {
                    "id_det": int(r[0]),
                    "id_articulo": r[1] or "",
                    "referencia": r[2] or "",
                    "descrip_art": r[3] or "",
                    "cantidad": _num(r[4]),
                }
                for r in det_rows
            ],
        }
    )


@require_http_methods(["GET"])
def detalle_grupo_articulos_prefactura_view(request):
    auth_payload = _require_perm_json(request, "inventario", "grupos_buscar")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    id_grupo_raw = (request.GET.get("id_grupo") or "").strip()
    if not id_grupo_raw:
        return JsonResponse({"detail": "id_grupo requerido"}, status=400)
    try:
        id_grupo = int(id_grupo_raw)
    except Exception:
        return JsonResponse({"detail": "id_grupo invalido"}, status=400)

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT D.ID_ARTICULO, A.DESCRIP_ART, A.UM_INV, A.PRECIO_DET, A.ID_IMPTO_VT, D.CANTIDAD, ISNULL(A.BLOQUEADO, 'N')
            FROM GRUPO_ARTICULO_DET D
            INNER JOIN MAESTRO_ARTICULO A ON A.ID_ARTICULO = D.ID_ARTICULO
            WHERE D.ID_GRUPO = %s
            ORDER BY D.ORDEN, D.ID_DET
            """,
            [id_grupo],
        )
        rows = cursor.fetchall()

    blocked_items = [
        {
            "id_articulo": str(r[0] or "").strip(),
            "descrip_art": str(r[1] or "").strip(),
        }
        for r in rows
        if _normalize_bloqueado(r[6]) == "Y"
    ]
    if blocked_items:
        detalle = ", ".join(
            [
                f"{x['id_articulo']} - {x['descrip_art']}".strip(" -")
                for x in blocked_items
            ]
        )
        return JsonResponse(
            {
                "detail": f"No se puede cargar el grupo. Articulos bloqueados: {detalle}",
                "blocked_articulos": blocked_items,
            },
            status=400,
        )

    def _num(v):
        try:
            return float(v)
        except Exception:
            return 0.0

    return JsonResponse(
        {
            "results": [
                {
                    "id_articulo": r[0] or "",
                    "descrip_art": r[1] or "",
                    "um_inv": r[2] or "",
                    "precio_det": _num(r[3]),
                    "id_impto_vt": r[4],
                    "cantidad": _num(r[5]),
                }
                for r in rows
            ]
        }
    )


@require_http_methods(["POST"])
def guardar_grupo_articulos_view(request):
    auth_payload = _require_perm_json(request, "inventario", "grupos_guardar")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"detail": "JSON invalido"}, status=400)

    def _to_int_or_none(v):
        s = str(v or "").strip()
        if not s:
            return None
        try:
            return int(float(s))
        except Exception:
            return None

    def _clip_str(v, max_len):
        s = str(v or "")
        if max_len is None or max_len <= 0:
            return s
        return s if len(s) <= max_len else s[:max_len]

    def _to_dec(v, default=Decimal("0")):
        try:
            if v is None or str(v).strip() == "":
                return default
            return Decimal(str(v))
        except Exception:
            return default

    id_grupo = _to_int_or_none(payload.get("id_grupo"))
    descripcion = str(payload.get("descripcion") or "").strip()
    detalles = payload.get("detalles") or []
    if not descripcion:
        return JsonResponse({"detail": "Descripcion requerida"}, status=400)
    if not isinstance(detalles, list):
        return JsonResponse({"detail": "detalles invalido"}, status=400)

    clean_detalles = []
    orden = 0
    for d in detalles:
        if not isinstance(d, dict):
            continue
        id_articulo = str(d.get("id_articulo") or "").strip()
        if not id_articulo:
            continue
        orden += 1
        cantidad = _to_dec(d.get("cantidad"), Decimal("1"))
        if cantidad <= 0:
            cantidad = Decimal("1")
        clean_detalles.append(
            {
                "orden": orden,
                "id_articulo": id_articulo,
                "cantidad": cantidad,
            }
        )

    if not clean_detalles:
        return JsonResponse({"detail": "Debes agregar al menos un articulo al grupo."}, status=400)

    usuario_id = _to_int_or_none((auth_payload or {}).get("usuario_id"))
    now = datetime.combine(timezone.localdate(), time.min)

    with transaction.atomic():
        with connection.cursor() as cursor:
            blocked_items = _articulos_bloqueados_info(
                cursor, [d["id_articulo"] for d in clean_detalles]
            )
            if blocked_items:
                detalle = ", ".join(
                    [
                        f"{x['id_articulo']} - {x['descrip_art']}".strip(" -")
                        for x in blocked_items
                    ]
                )
                return JsonResponse(
                    {
                        "detail": f"No se puede guardar el grupo. Articulos bloqueados: {detalle}",
                        "blocked_articulos": blocked_items,
                    },
                    status=400,
                )
            if id_grupo:
                cursor.execute(
                    """
                    UPDATE GRUPO_ARTICULO_CAB
                    SET DESCRIPCION = %s,
                        FECHA_ACT = %s,
                        ID_USUARIO = %s
                    WHERE ID_GRUPO = %s
                    """,
                    [descripcion, now, usuario_id, id_grupo],
                )
                if (cursor.rowcount or 0) <= 0:
                    return JsonResponse({"detail": "Grupo no encontrado"}, status=404)
            else:
                temp_codigo = f"TMP{uuid.uuid4().hex[:17]}"
                cursor.execute(
                    """
                    INSERT INTO GRUPO_ARTICULO_CAB (CODIGO, DESCRIPCION, ACTIVO, FECHA_CREACION, FECHA_ACT, ID_USUARIO)
                    OUTPUT INSERTED.ID_GRUPO
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    [temp_codigo, descripcion, "Y", now, now, usuario_id],
                )
                row = cursor.fetchone()
                id_grupo = int((row[0] or 0))
                if id_grupo <= 0:
                    cursor.execute(
                        "SELECT TOP 1 ID_GRUPO FROM GRUPO_ARTICULO_CAB WHERE CODIGO = %s ORDER BY ID_GRUPO DESC",
                        [temp_codigo],
                    )
                    row2 = cursor.fetchone()
                    id_grupo = int((row2[0] if row2 else 0) or 0)
                if id_grupo <= 0:
                    return JsonResponse({"detail": "No se pudo crear la cabecera del grupo."}, status=500)
                codigo = f"GA{id_grupo:05d}"
                cursor.execute(
                    "UPDATE GRUPO_ARTICULO_CAB SET CODIGO = %s WHERE ID_GRUPO = %s",
                    [codigo, id_grupo],
                )

            cursor.execute("SELECT COUNT(1) FROM GRUPO_ARTICULO_CAB WHERE ID_GRUPO = %s", [id_grupo])
            exists_row = cursor.fetchone()
            if int((exists_row[0] if exists_row else 0) or 0) <= 0:
                return JsonResponse({"detail": "Grupo no disponible para guardar detalle."}, status=500)

            cursor.execute("DELETE FROM GRUPO_ARTICULO_DET WHERE ID_GRUPO = %s", [id_grupo])
            for d in clean_detalles:
                cursor.execute(
                    """
                    INSERT INTO GRUPO_ARTICULO_DET (ID_GRUPO, ID_ARTICULO, CANTIDAD, ORDEN)
                    VALUES (%s, %s, %s, %s)
                    """,
                    [id_grupo, d["id_articulo"], d["cantidad"], d["orden"]],
                )

            cursor.execute(
                "SELECT CODIGO FROM GRUPO_ARTICULO_CAB WHERE ID_GRUPO = %s",
                [id_grupo],
            )
            cab = cursor.fetchone()
            codigo = (cab[0] if cab else "") or ""

    return JsonResponse({"ok": True, "id_grupo": id_grupo, "codigo": codigo})


@require_http_methods(["POST"])
def actualizar_prefactura_view(request):
    auth_payload = _get_auth_payload(request)
    if not auth_payload:
        return JsonResponse({"detail": "No autenticado"}, status=401)
    if not has_perm(auth_payload.get("usuario_id"), "prefacturas", "guardar"):
        return JsonResponse({"detail": "Permiso denegado"}, status=403)

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"detail": "JSON invalido"}, status=400)

    id_doc = str(payload.get("id_doc") or "").strip()
    client_event_id = str(payload.get("event_id") or "").strip()
    if not id_doc:
        return JsonResponse({"detail": "id_doc requerido"}, status=400)

    def _parse_date(v):
        s = str(v or "").strip()
        if not s:
            return None
        try:
            return datetime.strptime(s, "%Y-%m-%d")
        except Exception:
            return None

    def _to_dec(v, default=Decimal("0")):
        try:
            if v is None or str(v).strip() == "":
                return default
            return Decimal(str(v))
        except Exception:
            return default

    def _to_int_or_none(v):
        s = str(v or "").strip()
        if not s:
            return None
        try:
            return int(float(s))
        except Exception:
            return None

    def _clip_str(v, max_len):
        s = str(v or "")
        if max_len is None or max_len <= 0:
            return s
        return s if len(s) <= max_len else s[:max_len]

    est_doc_raw = str(payload.get("est_doc") or "").strip()
    est_map = {
        "abierto": "Abierto",
        "cerrado": "Cerrado",
        "cancelado": "Cancelado",
    }
    est_doc = est_map.get(est_doc_raw.lower(), "")
    if est_doc not in {"Abierto", "Cerrado", "Cancelado"}:
        return JsonResponse({"detail": "est_doc invalido"}, status=400)

    detalles = payload.get("detalles") or []
    if not isinstance(detalles, list):
        return JsonResponse({"detail": "detalles invalido"}, status=400)

    local_date = timezone.localdate()
    periodo_cont = str(local_date.month)
    ejercicio = local_date.year
    usuario_id = _to_int_or_none((auth_payload or {}).get("usuario_id"))

    with transaction.atomic():
        with connection.cursor() as cursor:
            id_sn_payload = str(payload.get("id_sn") or "").strip()
            if id_sn_payload:
                cursor.execute(
                    """
                    SELECT TOP 1 ISNULL(BLOQUEADO, 'N')
                    FROM MAESTRO_SN
                    WHERE ID_SN = %s
                    """,
                    [id_sn_payload],
                )
                sn_row = cursor.fetchone()
                if sn_row and _normalize_bloqueado(sn_row[0]) == "Y":
                    return JsonResponse(
                        {"detail": "No se puede grabar la pre-factura: el cliente esta bloqueado."},
                        status=400,
                    )

            detalle_articulos = {
                str((d or {}).get("id_articulo") or "").strip()
                for d in detalles
                if isinstance(d, dict)
            }
            detalle_articulos.discard("")
            blocked_items = _articulos_bloqueados_info(cursor, detalle_articulos)
            if blocked_items:
                detalle = ", ".join(
                    [
                        f"{x['id_articulo']} - {x['descrip_art']}".strip(" -")
                        for x in blocked_items
                    ]
                )
                return JsonResponse(
                    {
                        "detail": f"No se puede grabar la pre-factura. Articulos bloqueados: {detalle}",
                        "blocked_articulos": blocked_items,
                    },
                    status=400,
                )
            referencia_map = {}
            if detalle_articulos:
                placeholders = ", ".join(["%s"] * len(detalle_articulos))
                cursor.execute(
                    f"""
                    SELECT ID_ARTICULO, REFERENCIA
                    FROM MAESTRO_ARTICULO
                    WHERE ID_ARTICULO IN ({placeholders})
                    """,
                    list(detalle_articulos),
                )
                for art_row in cursor.fetchall():
                    referencia_map[str(art_row[0] or "").strip()] = str(art_row[1] or "")

            cursor.execute(
                """
                UPDATE CAB_PEDIDO
                SET EST_DOC = %s,
                    FECHA_CONT = %s,
                    FECHA_VENC = %s,
                    FECHA_DOC = %s,
                    COMENTARIO = %s,
                    SUBTOTAL = %s,
                    TOTAL_DESC = %s,
                    TOTAL_DOC = %s,
                    TOTAL_ITBIS = %s,
                    ID_CONDICION = %s,
                    DIA = %s,
                    CONDICION = %s,
                    ID_PRECIO = %s
                WHERE CAST(ID_DOC AS VARCHAR(50)) = %s
                """,
                [
                    est_doc,
                    _parse_date(payload.get("fecha_cont")),
                    _parse_date(payload.get("fecha_venc")),
                    _parse_date(payload.get("fecha_doc")),
                    _clip_str(payload.get("comentario"), 500),
                    _to_dec(payload.get("subtotal")),
                    _to_dec(payload.get("total_desc")),
                    _to_dec(payload.get("total_doc")),
                    _to_dec(payload.get("impuesto")),
                    _to_int_or_none(payload.get("id_condicion")),
                    _to_int_or_none(payload.get("dia")),
                    _clip_str(payload.get("condicion"), 15),
                    _to_int_or_none(payload.get("id_precio")),
                    id_doc,
                ],
            )
            if (cursor.rowcount or 0) <= 0:
                return JsonResponse({"detail": "Pre-factura no encontrada"}, status=404)

            cursor.execute(
                "SELECT COALESCE(MAX(No_LINEA), 0) FROM DET_PEDIDO WHERE CAST(ID_DOC AS VARCHAR(50)) = %s",
                [id_doc],
            )
            row = cursor.fetchone()
            next_linea = int(row[0] or 0)

            for d in detalles:
                if not isinstance(d, dict):
                    continue
                id_detalle = _to_int_or_none(d.get("id_detalle"))
                descrip_art = _clip_str(d.get("descrip_art"), 500)
                id_articulo = _clip_str(d.get("id_articulo"), 20)
                medida = _clip_str(d.get("uom"), 50)
                cantidad = _to_dec(d.get("cantidad"))
                cant_und = _to_dec(d.get("cant_emp"), cantidad)
                cant_ent = Decimal("0")
                cant_pend = cantidad
                precio = _to_dec(d.get("precio_unit"))
                precio_bruto = _to_dec(d.get("precio_bruto"), precio)
                porc_desc = _to_dec(d.get("porc_desc"))
                id_impto = _to_int_or_none(d.get("id_itbis"))
                id_almacen = _to_int_or_none(d.get("alm"))
                # Mantener el mapeo actual de UI solicitado previamente.
                cebe = _clip_str(d.get("proyecto"), 50)
                ceco = _clip_str(d.get("cebe"), 12)
                referencia = _clip_str(referencia_map.get(id_articulo, ""), 15)
                total_precio = cantidad * precio
                total_desc_monto = total_precio * (porc_desc / Decimal("100"))
                total_precio_neto = total_precio - total_desc_monto
                total_linea = total_precio_neto
                precio_tras_desc = precio - (precio * (porc_desc / Decimal("100")))

                if id_detalle:
                    cursor.execute(
                        """
                        UPDATE DET_PEDIDO
                        SET DESCRIP_ART = %s,
                            ID_ARTICULO = %s,
                            MEDIDA = %s,
                            CANTIDAD = %s,
                            CANT_UND = %s,
                            CANT_ENT = %s,
                            CANT_PEND = %s,
                            PRECIO = %s,
                            PRECIO_BRUTO = %s,
                            TOTAL_LINEA = %s,
                            TOTAL_PRECIO = %s,
                            TOTAL_DESC = %s,
                            TOTAL_PRECIO_NETO = %s,
                            PRECIO_TRAS_DESC = %s,
                            PORC_DESC = %s,
                            ID_IMPTO = %s,
                            ID_ALMACEN = %s,
                            CECO = %s,
                            CEBE = %s,
                            CLASE_ART = %s,
                            LOTE = %s,
                            COSTO = %s,
                            TOTAL_COSTO = %s,
                            ID_VENDEDOR = %s,
                            PORC_COM = %s,
                            CTA_INGRESO = %s,
                            CTA_GASTOS = %s,
                            CTA_COSTOS = %s,
                            CTA_INV = %s,
                            CTA_IMPTO = %s,
                            CTA_DEV_VENTA = %s,
                            PERIODO_CONT = %s,
                            EJERCICIO = %s,
                            REFERENCIA = %s
                        WHERE ID_DETALLE = %s
                          AND CAST(ID_DOC AS VARCHAR(50)) = %s
                        """,
                        [
                            descrip_art,
                            id_articulo,
                            medida,
                            cantidad,
                            cant_und,
                            cant_ent,
                            cant_pend,
                            precio,
                            precio_bruto,
                            total_linea,
                            total_precio,
                            total_desc_monto,
                            total_precio_neto,
                            precio_tras_desc,
                            porc_desc,
                            id_impto,
                            id_almacen,
                            ceco,
                            cebe,
                            _clip_str("Articulo", 10),
                            _clip_str("No", 2),
                            Decimal("1"),
                            Decimal("1"),
                            usuario_id,
                            Decimal("1"),
                            _clip_str("41010101", 20),
                            _clip_str("11030102", 20),
                            _clip_str("51010101", 20),
                            _clip_str("11030101", 20),
                            _clip_str("21020301", 20),
                            _clip_str("41020201", 20),
                            periodo_cont,
                            ejercicio,
                            referencia,
                            id_detalle,
                            id_doc,
                        ],
                    )
                else:
                    next_linea += 1
                    cursor.execute(
                        """
                        INSERT INTO DET_PEDIDO
                        (ID_DOC, No_LINEA, DESCRIP_ART, ID_ARTICULO, MEDIDA, CANTIDAD, CANT_UND, CANT_ENT, CANT_PEND, PRECIO, PRECIO_BRUTO, TOTAL_LINEA, TOTAL_PRECIO, TOTAL_DESC, TOTAL_PRECIO_NETO, PRECIO_TRAS_DESC, PORC_DESC, ID_IMPTO, ID_ALMACEN, CECO, CEBE,
                         CLASE_ART, LOTE, COSTO, TOTAL_COSTO, ID_VENDEDOR, PORC_COM, CTA_INGRESO, CTA_GASTOS, CTA_COSTOS, CTA_INV, CTA_IMPTO, CTA_DEV_VENTA, PERIODO_CONT, EJERCICIO, REFERENCIA)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        [
                            id_doc,
                            next_linea,
                            descrip_art,
                            id_articulo,
                            medida,
                            cantidad,
                            cant_und,
                            cant_ent,
                            cant_pend,
                            precio,
                            precio_bruto,
                            total_linea,
                            total_precio,
                            total_desc_monto,
                            total_precio_neto,
                            precio_tras_desc,
                            porc_desc,
                            id_impto,
                            id_almacen,
                            ceco,
                            cebe,
                            _clip_str("Articulo", 10),
                            _clip_str("No", 2),
                            Decimal("1"),
                            Decimal("1"),
                            usuario_id,
                            Decimal("1"),
                            _clip_str("41010101", 20),
                            _clip_str("11030102", 20),
                            _clip_str("51010101", 20),
                            _clip_str("11030101", 20),
                            _clip_str("21020301", 20),
                            _clip_str("41020201", 20),
                            periodo_cont,
                            ejercicio,
                            referencia,
                        ],
                    )

            cursor.execute(
                """
                UPDATE DET_PEDIDO
                SET OBSERVACION = %s
                WHERE ID_DETALLE = (
                    SELECT TOP 1 ID_DETALLE
                    FROM DET_PEDIDO
                    WHERE CAST(ID_DOC AS VARCHAR(50)) = %s
                    ORDER BY No_LINEA, ID_DETALLE
                )
                """,
                [str(payload.get("comentario_linea") or ""), id_doc],
            )

    transaction.on_commit(
        lambda: broadcast_prefacturas_refresh(reason="prefactura-updated", event_id=client_event_id)
    )
    transaction.on_commit(
        lambda: broadcast_prefactura_document_status(
            document_id=id_doc,
            estado=est_doc,
            reason="prefactura-updated",
            event_id=client_event_id,
        )
    )
    return JsonResponse({"ok": True, "id_doc": id_doc})


@require_http_methods(["POST"])
def crear_prefactura_view(request):
    auth_payload = _get_auth_payload(request)
    if not auth_payload:
        return JsonResponse({"detail": "No autenticado"}, status=401)
    if not has_perm(auth_payload.get("usuario_id"), "prefacturas", "guardar"):
        return JsonResponse({"detail": "Permiso denegado"}, status=403)

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"detail": "JSON invalido"}, status=400)

    client_event_id = str(payload.get("event_id") or "").strip()
    id_sn = str(payload.get("id_sn") or "").strip()
    nom_socio = str(payload.get("nom_socio") or "").strip()
    if not id_sn:
        return JsonResponse({"detail": "Cliente requerido"}, status=400)
    cliente_bloqueado = (
        MaestroSn.objects.filter(id_sn=id_sn).values_list("bloqueado", flat=True).first()
    )
    if _normalize_bloqueado(cliente_bloqueado) == "Y":
        return JsonResponse(
            {"detail": "No se puede crear la pre-factura: el cliente esta bloqueado."},
            status=400,
        )

    def _parse_date(v):
        s = str(v or "").strip()
        if not s:
            return None
        try:
            return datetime.strptime(s, "%Y-%m-%d")
        except Exception:
            return None

    def _to_dec(v, default=Decimal("0")):
        try:
            if v is None or str(v).strip() == "":
                return default
            return Decimal(str(v))
        except Exception:
            return default

    def _to_int_or_none(v):
        s = str(v or "").strip()
        if not s:
            return None
        try:
            return int(float(s))
        except Exception:
            return None

    def _clip_str(v, max_len):
        s = str(v or "")
        if max_len is None or max_len <= 0:
            return s
        return s if len(s) <= max_len else s[:max_len]

    est_doc = "Abierto"
    detalles = payload.get("detalles") or []
    if not isinstance(detalles, list):
        return JsonResponse({"detail": "detalles invalido"}, status=400)
    detalles = [d for d in detalles if isinstance(d, dict) and str(d.get("id_articulo") or "").strip()]
    if not detalles:
        return JsonResponse({"detail": "Debes agregar al menos un articulo."}, status=400)

    fecha_cont = _parse_date(payload.get("fecha_cont"))
    fecha_venc = _parse_date(payload.get("fecha_venc"))
    fecha_doc = _parse_date(payload.get("fecha_doc"))

    now = timezone.localtime()
    local_date = timezone.localdate()
    today = datetime.combine(local_date, time.min)
    periodo_cont = str(local_date.month)
    ejercicio = local_date.year
    usuario_id = _to_int_or_none((auth_payload or {}).get("usuario_id"))
    terminal = socket.gethostname() or ""

    with transaction.atomic():
        with connection.cursor() as cursor:
            detalle_articulos = {
                str((d or {}).get("id_articulo") or "").strip()
                for d in detalles
                if isinstance(d, dict)
            }
            detalle_articulos.discard("")
            blocked_items = _articulos_bloqueados_info(cursor, detalle_articulos)
            if blocked_items:
                detalle = ", ".join(
                    [
                        f"{x['id_articulo']} - {x['descrip_art']}".strip(" -")
                        for x in blocked_items
                    ]
                )
                return JsonResponse(
                    {
                        "detail": f"No se puede crear la pre-factura. Articulos bloqueados: {detalle}",
                        "blocked_articulos": blocked_items,
                    },
                    status=400,
                )
            referencia_map = {}
            if detalle_articulos:
                placeholders = ", ".join(["%s"] * len(detalle_articulos))
                cursor.execute(
                    f"""
                    SELECT ID_ARTICULO, REFERENCIA
                    FROM MAESTRO_ARTICULO
                    WHERE ID_ARTICULO IN ({placeholders})
                    """,
                    list(detalle_articulos),
                )
                for art_row in cursor.fetchall():
                    referencia_map[str(art_row[0] or "").strip()] = str(art_row[1] or "")

            cursor.execute(
                """
                SELECT ISNULL(MAX(TRY_CAST(ID_DOC AS BIGINT)), 0) + 1
                FROM CAB_PEDIDO WITH (UPDLOCK, HOLDLOCK)
                """
            )
            row = cursor.fetchone()
            new_id_doc = int((row[0] or 0))
            if new_id_doc <= 0:
                new_id_doc = 1

            cursor.execute(
                """
                INSERT INTO CAB_PEDIDO
                (ID_DOC, EST_DOC, FECHA_CONT, FECHA_VENC, FECHA_DOC, FECHA_CREACION, FECHA_ACT,
                 ID_SN, NOM_SOCIO, RNC_CED, CONTACTO, ENT_FACTURA, ENT_MERCANCIA,
                 COMENTARIO, SUBTOTAL, TOTAL_DESC, TOTAL_DOC, TOTAL_ITBIS,
                 ABONO, SALDO, ID_CONDICION, DIA, CONDICION, ID_PRECIO, ID_VENDEDOR, ID_USUARIO, TERMINAL,
                 MON_DOC, ID_NCF, TIPO, PERIODO_CONT, EJERCICIO, CTA_ASOCIADA, ID_GASTO)
                VALUES
                (%s, %s, %s, %s, %s, %s, %s,
                 %s, %s, %s, %s, %s, %s,
                 %s, %s, %s, %s, %s,
                 %s, %s, %s, %s, %s, %s, %s, %s, %s,
                 %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    new_id_doc,
                    est_doc,
                    fecha_cont,
                    fecha_venc,
                    fecha_doc,
                    today,
                    None,
                    _clip_str(id_sn, 12),
                    _clip_str(nom_socio, 100),
                    _clip_str(payload.get("rnc_ced"), 13),
                    _clip_str(payload.get("contacto"), 50),
                    _clip_str(payload.get("ent_factura"), 200),
                    _clip_str(payload.get("ent_mercancia"), 200),
                    _clip_str(payload.get("comentario"), 500),
                    _to_dec(payload.get("subtotal")),
                    _to_dec(payload.get("total_desc")),
                    _to_dec(payload.get("total_doc")),
                    _to_dec(payload.get("impuesto")),
                    Decimal("0"),
                    Decimal("0"),
                    _to_int_or_none(payload.get("id_condicion")),
                    _to_int_or_none(payload.get("dia")),
                    _clip_str(payload.get("condicion"), 15),
                    _to_int_or_none(payload.get("id_precio")),
                    usuario_id,
                    usuario_id,
                    terminal[:50],
                    _clip_str("RD$", 50),
                    1,
                    "FACTURA DE CONSUMO",
                    periodo_cont,
                    ejercicio,
                    "11020101",
                    2,
                ],
            )

            next_linea = 0
            for d in detalles:
                next_linea += 1
                descrip_art = _clip_str(d.get("descrip_art"), 500)
                id_articulo = _clip_str(d.get("id_articulo"), 20)
                medida = _clip_str(d.get("uom"), 50)
                cantidad = _to_dec(d.get("cantidad"))
                cant_und = _to_dec(d.get("cant_emp"), cantidad)
                precio = _to_dec(d.get("precio_unit"))
                precio_bruto = _to_dec(d.get("precio_bruto"), precio)
                porc_desc = _to_dec(d.get("porc_desc"))
                id_impto = _to_int_or_none(d.get("id_itbis"))
                id_almacen = _to_int_or_none(d.get("alm"))
                cebe = _clip_str(d.get("proyecto"), 50)
                ceco = _clip_str(d.get("cebe"), 12)
                cant_ent = Decimal("0")
                cant_pend = cantidad
                referencia = _clip_str(referencia_map.get(id_articulo, ""), 15)
                total_precio = cantidad * precio
                total_desc_monto = total_precio * (porc_desc / Decimal("100"))
                total_precio_neto = total_precio - total_desc_monto
                total_linea = total_precio_neto
                precio_tras_desc = precio - (precio * (porc_desc / Decimal("100")))

                cursor.execute(
                    """
                    INSERT INTO DET_PEDIDO
                    (ID_DOC, No_LINEA, DESCRIP_ART, ID_ARTICULO, MEDIDA, CANTIDAD, CANT_UND, CANT_ENT, CANT_PEND,
                     PRECIO, PRECIO_BRUTO, TOTAL_LINEA, TOTAL_PRECIO, TOTAL_DESC, TOTAL_PRECIO_NETO, PRECIO_TRAS_DESC, PORC_DESC, ID_IMPTO, ID_ALMACEN, CECO, CEBE, FECHA_CONT,
                     CLASE_ART, LOTE, COSTO, TOTAL_COSTO, ID_VENDEDOR, PORC_COM, CTA_INGRESO, CTA_GASTOS, CTA_COSTOS,
                     CTA_INV, CTA_IMPTO, CTA_DEV_VENTA, PERIODO_CONT, EJERCICIO, REFERENCIA)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        new_id_doc,
                        next_linea,
                        descrip_art,
                        id_articulo,
                        medida,
                        cantidad,
                        cant_und,
                        cant_ent,
                        cant_pend,
                        precio,
                        precio_bruto,
                        total_linea,
                        total_precio,
                        total_desc_monto,
                        total_precio_neto,
                        precio_tras_desc,
                        porc_desc,
                        id_impto,
                        id_almacen,
                        ceco,
                        cebe,
                        fecha_cont or fecha_doc or today,
                        _clip_str("Articulo", 10),
                        _clip_str("No", 2),
                        Decimal("1"),
                        Decimal("1"),
                        usuario_id,
                        Decimal("1"),
                        _clip_str("41010101", 20),
                        _clip_str("11030102", 20),
                        _clip_str("51010101", 20),
                        _clip_str("11030101", 20),
                        _clip_str("21020301", 20),
                        _clip_str("41020201", 20),
                        periodo_cont,
                        ejercicio,
                        referencia,
                    ],
                )

            cursor.execute(
                """
                UPDATE DET_PEDIDO
                SET OBSERVACION = %s
                WHERE ID_DETALLE = (
                    SELECT TOP 1 ID_DETALLE
                    FROM DET_PEDIDO
                    WHERE CAST(ID_DOC AS VARCHAR(50)) = %s
                    ORDER BY No_LINEA, ID_DETALLE
                )
                """,
                [str(payload.get("comentario_linea") or ""), str(new_id_doc)],
            )

    transaction.on_commit(
        lambda: broadcast_prefacturas_refresh(reason="prefactura-created", event_id=client_event_id)
    )
    transaction.on_commit(
        lambda: broadcast_prefactura_document_status(
            document_id=str(new_id_doc),
            estado=est_doc,
            reason="prefactura-created",
            event_id=client_event_id,
        )
    )
    return JsonResponse({"ok": True, "id_doc": str(new_id_doc), "fecha_act": now.strftime("%Y-%m-%d")})


@require_http_methods(["POST"])
def actualizar_estado_prefactura_view(request):
    auth_payload = _get_auth_payload(request)
    if not auth_payload:
        return JsonResponse({"detail": "No autenticado"}, status=401)

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"detail": "JSON invalido"}, status=400)

    id_doc = str(payload.get("id_doc") or "").strip()
    client_event_id = str(payload.get("event_id") or "").strip()
    est_doc_raw = str(payload.get("est_doc") or "").strip()
    est_map = {
        "abierto": "Abierto",
        "cerrado": "Cerrado",
        "cancelado": "Cancelado",
    }
    est_doc = est_map.get(est_doc_raw.lower(), "")
    if not id_doc:
        return JsonResponse({"detail": "id_doc requerido"}, status=400)
    if est_doc not in {"Abierto", "Cerrado", "Cancelado"}:
        return JsonResponse({"detail": "est_doc invalido"}, status=400)

    if est_doc == "Cerrado" and not has_perm(auth_payload.get("usuario_id"), "prefacturas", "cerrar"):
        return JsonResponse({"detail": "Permiso denegado"}, status=403)
    if est_doc == "Cancelado" and not has_perm(auth_payload.get("usuario_id"), "prefacturas", "cancelar"):
        return JsonResponse({"detail": "Permiso denegado"}, status=403)

    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE CAB_PEDIDO
            SET EST_DOC = %s
            WHERE CAST(ID_DOC AS VARCHAR(50)) = %s
            """,
            [est_doc, id_doc],
        )
        updated = cursor.rowcount or 0

    if updated <= 0:
        return JsonResponse({"detail": "Pre-factura no encontrada"}, status=404)

    transaction.on_commit(
        lambda: broadcast_prefacturas_refresh(reason="prefactura-status-updated", event_id=client_event_id)
    )
    transaction.on_commit(
        lambda: broadcast_prefactura_document_status(
            document_id=id_doc,
            estado=est_doc,
            reason="prefactura-status-updated",
            event_id=client_event_id,
        )
    )
    return JsonResponse({"ok": True, "id_doc": id_doc, "est_doc": est_doc})


@require_http_methods(["GET"])
def detalle_prefactura_view(request):
    auth_payload = _require_perm_json(request, "prefacturas", "ver")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    id_doc = (request.GET.get("id_doc") or "").strip()
    if not id_doc:
        return JsonResponse({"detail": "Parametro id_doc requerido"}, status=400)

    detalles = (
        DetPedido.objects.filter(id_doc=id_doc)
        .order_by("no_linea", "id_detalle")
        .values(
            "id_detalle",
            "descrip_art",
            "id_articulo",
            "cant_und",
            "cantidad",
            "cant_ent",
            "medida",
            "observacion",
            "id_almacen",
            "ceco",
            "cebe",
            "precio",
            "precio_bruto",
            "total_linea",
            "porc_desc",
            "id_impto",
        )
    )

    def _num(v):
        if v is None:
            return None
        try:
            return float(v)
        except Exception:
            return None

    results = []
    for d in detalles:
        results.append(
            {
                "id_detalle": d.get("id_detalle"),
                "descrip_art": d.get("descrip_art") or "",
                "id_articulo": d.get("id_articulo") or "",
                "cant_emp": _num(d.get("cant_und")),
                "cantidad": _num(d.get("cantidad")),
                "entregado": _num(d.get("cant_ent")),
                "uom": d.get("medida") or "",
                "observacion": d.get("observacion") or "",
                "alm": d.get("id_almacen"),
                "proyecto": d.get("cebe") or "",
                "cebe": d.get("ceco") or "",
                "precio_unit": _num(d.get("precio")),
                "precio_bruto": _num(d.get("precio_bruto")),
                "valor": _num(d.get("total_linea")),
                "porc_desc": _num(d.get("porc_desc")),
                "id_itbis": d.get("id_impto"),
            }
        )

    return JsonResponse({"results": results})


@require_http_methods(["POST"])
def guardar_comentario_linea_prefactura_view(request):
    if not _get_auth_payload(request):
        return JsonResponse({"detail": "No autenticado"}, status=401)

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"detail": "JSON invalido"}, status=400)

    id_doc = str(payload.get("id_doc") or "").strip()
    observacion = str(payload.get("observacion") or "")
    if not id_doc:
        return JsonResponse({"detail": "id_doc requerido"}, status=400)

    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE DET_PEDIDO
            SET OBSERVACION = %s
            WHERE ID_DETALLE = (
                SELECT TOP 1 ID_DETALLE
                FROM DET_PEDIDO
                WHERE CAST(ID_DOC AS VARCHAR(50)) = %s
                ORDER BY No_LINEA, ID_DETALLE
            )
            """,
            [observacion, id_doc],
        )
        updated = cursor.rowcount or 0

    if updated <= 0:
        return JsonResponse({"detail": "No se encontro primer detalle para el documento"}, status=404)

    return JsonResponse({"ok": True, "id_doc": id_doc})


@require_http_methods(["GET"])
def buscar_unidad_medida_view(request):
    auth_payload = _require_perm_json(request, "prefacturas", "ver")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT TOP 200 * FROM Unidad_medida ORDER BY 1")
            rows = cursor.fetchall()
            columns = [c[0].lower() for c in (cursor.description or [])]
    except Exception:
        return JsonResponse({"results": []})

    preferred = [
        "medida",
        "descripcion",
        "descrip",
        "uom",
        "unidad",
        "nombre",
        "id",
        "codigo",
    ]
    idx = None
    for name in preferred:
        if name in columns:
            idx = columns.index(name)
            break
    if idx is None:
        idx = 0 if columns else None

    values = []
    seen = set()
    if idx is not None:
        for row in rows:
            raw = row[idx]
            val = str(raw or "").strip()
            if not val:
                continue
            key = val.lower()
            if key in seen:
                continue
            seen.add(key)
            values.append(val)

    return JsonResponse({"results": values})


@require_http_methods(["GET"])
def buscar_proyectos_view(request):
    auth_payload = _require_perm_json(request, "prefacturas", "ver")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    query = (request.GET.get("q") or "").strip()
    sql = """
        SELECT TOP 200 CODIGO, DESCRIPCION
        FROM PROYECTO
        WHERE CODIGO IS NOT NULL AND LTRIM(RTRIM(CODIGO)) <> ''
    """
    params = []
    if query:
        sql += " AND (CODIGO LIKE %s OR DESCRIPCION LIKE %s)"
        like = f"%{query}%"
        params.extend([like, like])
    sql += " ORDER BY CODIGO"

    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()

    results = [
        {
            "codigo": str(r[0]).strip(),
            "descripcion": str(r[1] or "").strip(),
        }
        for r in rows
        if r and r[0] is not None and str(r[0]).strip()
    ]
    return JsonResponse({"results": results})


@require_http_methods(["GET"])
def buscar_cebes_view(request):
    auth_payload = _require_perm_json(request, "prefacturas", "ver")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    query = (request.GET.get("q") or "").strip()
    sql = """
        SELECT TOP 200 CECO, DESCRIPCION, ID_CODIGO
        FROM DEPARTAMENTO
        WHERE CECO IS NOT NULL AND LTRIM(RTRIM(CECO)) <> ''
    """
    params = []
    if query:
        sql += " AND (CECO LIKE %s OR DESCRIPCION LIKE %s)"
        like = f"%{query}%"
        params.extend([like, like])
    sql += " ORDER BY ID_CODIGO"

    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()

    results = [
        {
            "codigo": str(r[0]).strip(),
            "descripcion": str(r[1] or "").strip(),
        }
        for r in rows
        if r and r[0] is not None and str(r[0]).strip()
    ]
    return JsonResponse({"results": results})


@require_http_methods(["GET"])
def buscar_grupo_cliente_view(request):
    auth_payload = _require_perm_json(request, "clientes", "ver")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    query = (request.GET.get("q") or "").strip()

    sql = """
        SELECT TOP 200 ID_GRUPO, DESCRIPCION
        FROM GRUPO_CLIENTE
    """
    params = []
    if query:
        sql += """
            WHERE CAST(ID_GRUPO AS VARCHAR(50)) LIKE %s
               OR DESCRIPCION LIKE %s
        """
        like = f"%{query}%"
        params.extend([like, like])
    sql += " ORDER BY ID_GRUPO"

    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()

    results = [
        {
            "id_grupo": row[0],
            "descripcion": row[1],
        }
        for row in rows
    ]

    return JsonResponse({"results": results})


@require_http_methods(["GET"])
def buscar_vend_comp_view(request):
    auth_payload = _require_perm_json(request, "clientes", "ver")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    query = (request.GET.get("q") or "").strip()

    sql = """
        SELECT TOP 200 ID_CODIGO, NOMBRE
        FROM VEND_COMP
    """
    params = []
    if query:
        sql += """
            WHERE CAST(ID_CODIGO AS VARCHAR(50)) LIKE %s
               OR NOMBRE LIKE %s
        """
        like = f"%{query}%"
        params.extend([like, like])
    sql += " ORDER BY ID_CODIGO"

    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()

    results = [
        {
            "id_codigo": row[0],
            "nombre": row[1],
        }
        for row in rows
    ]

    return JsonResponse({"results": results})


@require_http_methods(["GET"])
def buscar_condicion_pago_view(request):
    auth_payload = _require_perm_json(request, "prefacturas", "ver")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    query = (request.GET.get("q") or "").strip()

    sql = """
        SELECT TOP 200 ID_CONDICION, DESCRIPCION, DIA
        FROM CONDICION_PAGO
    """
    params = []
    if query:
        sql += """
            WHERE CAST(ID_CONDICION AS VARCHAR(50)) LIKE %s
               OR DESCRIPCION LIKE %s
        """
        like = f"%{query}%"
        params.extend([like, like])
    sql += " ORDER BY ID_CONDICION"

    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()

    results = [
        {
            "id_condicion": row[0],
            "descripcion": row[1],
            "dia": row[2],
        }
        for row in rows
    ]
    return JsonResponse({"results": results})


@require_http_methods(["GET"])
def buscar_lista_precio_view(request):
    auth_payload = _require_perm_json(request, "prefacturas", "ver")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    query = (request.GET.get("q") or "").strip()

    sql = """
        SELECT TOP 200 ID_PRECIO, DESCRIPCION, FACTOR
        FROM LISTA_PRECIO
    """
    params = []
    if query:
        sql += """
            WHERE CAST(ID_PRECIO AS VARCHAR(50)) LIKE %s
               OR DESCRIPCION LIKE %s
        """
        like = f"%{query}%"
        params.extend([like, like])
    sql += " ORDER BY ID_PRECIO"

    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()

    def _factor_to_int(value):
        if value is None:
            return None
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None

    results = [
        {
            "id_precio": row[0],
            "descripcion": row[1],
            "factor": _factor_to_int(row[2]),
        }
        for row in rows
    ]
    return JsonResponse({"results": results})


@require_http_methods(["GET"])
def detalle_cliente_view(request):
    auth_payload = _require_perm_json(request, "clientes", "ver")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    id_sn = (request.GET.get("id_sn") or "").strip()
    if not id_sn:
        return JsonResponse({"detail": "Parametro id_sn requerido"}, status=400)

    cliente = (
        MaestroSn.objects.filter(id_sn=id_sn)
        .values(
            "id_sn",
            "nom_socio",
            "contacto",
            "rnc_ced",
            "dir_factura",
            "dir_mercancia",
            "nomref1",
            "telref1",
            "parentref1",
            "nomref2",
            "telref2",
            "parentref2",
            "tel1",
            "tel2",
            "fax",
            "email",
            "id_sector",
            "comentario",
            "id_grupo",
            "descripcion",
            "tipo_sn",
            "id_vendedor",
            "nom_vend",
            "bloqueado",
            "id_condicion",
            "condicion",
            "dia",
            "tarifa_int",
            "lim_credito",
            "id_precio",
            "saldo",
            "cobro_elect",
            "foto",
        )
        .first()
    )

    if not cliente:
        return JsonResponse({"detail": "Cliente no encontrado"}, status=404)

    # Balance dinamico: suma el libro DET_ED completo; los CAB_ED cancelados reversan movimientos.
    cliente["saldo"] = _get_open_ed_balance(id_sn)
    cliente["foto_url"] = _build_foto_url(cliente.get("foto"))
    return JsonResponse({"cliente": cliente})


@require_http_methods(["POST"])
def subir_foto_cliente_view(request):
    auth_payload = _require_perm_json(request, "clientes", "editar")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    id_sn = (request.POST.get("id_sn") or "").strip()
    foto_file = request.FILES.get("foto")
    if not id_sn:
        return JsonResponse({"detail": "id_sn requerido"}, status=400)
    if not foto_file:
        return JsonResponse({"detail": "Archivo de foto requerido"}, status=400)
    if not str(getattr(foto_file, "content_type", "")).startswith("image/"):
        return JsonResponse({"detail": "Solo se permiten imagenes"}, status=400)

    qs = MaestroSn.objects.filter(id_sn=id_sn)
    if not qs.exists():
        return JsonResponse({"detail": "Cliente no encontrado"}, status=404)

    ext = os.path.splitext(foto_file.name or "")[1].lower() or ".jpg"
    file_name = f"{id_sn}_{timezone.now().strftime('%Y%m%d%H%M%S%f')}{ext}"
    rel_dir = Path("clientes_fotos")
    abs_dir = Path(settings.MEDIA_ROOT) / rel_dir
    abs_dir.mkdir(parents=True, exist_ok=True)
    abs_path = abs_dir / file_name

    with abs_path.open("wb+") as destination:
        for chunk in foto_file.chunks():
            destination.write(chunk)

    abs_path_str = str(abs_path)
    qs.update(foto=abs_path_str, terminal=socket.gethostname())
    return JsonResponse({"ok": True, "foto": abs_path_str, "foto_url": _build_foto_url(abs_path_str)})


@require_http_methods(["POST"])
def actualizar_cliente_view(request):
    auth_payload = _require_perm_json(request, "clientes", "editar")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"detail": "JSON invalido"}, status=400)

    if "bloqueado" in payload and not has_perm(auth_payload.get("usuario_id"), "clientes", "bloquear"):
        return _perm_denied_json()

    id_sn = (payload.get("id_sn") or "").strip()
    if not id_sn:
        return JsonResponse({"detail": "id_sn requerido"}, status=400)

    nom_socio = (payload.get("nom_socio") or "").strip()
    if not nom_socio:
        return JsonResponse({"detail": "El campo Nombre es obligatorio"}, status=400)

    rnc_ced = (payload.get("rnc_ced") or "").strip()
    if not rnc_ced:
        return JsonResponse({"detail": "El campo RNC/CED es obligatorio"}, status=400)

    id_sector_raw = payload.get("id_sector")
    if id_sector_raw is None or str(id_sector_raw).strip() == "":
        return JsonResponse({"detail": "El campo Sector es obligatorio"}, status=400)

    dir_factura = (payload.get("dir_factura") or "").strip()
    if not dir_factura:
        return JsonResponse({"detail": "El campo Direccion de cliente es obligatorio"}, status=400)

    qs = MaestroSn.objects.filter(id_sn=id_sn)
    if not qs.exists():
        return JsonResponse({"detail": "Cliente no encontrado"}, status=404)

    def _clean(v):
        if v is None:
            return None
        if isinstance(v, str):
            v = v.strip()
            return v or None
        return v

    fecha_local = datetime.combine(timezone.localdate(), time.min)
    update_data = {
        "nom_socio": _clean(payload.get("nom_socio")),
        "contacto": _clean(payload.get("contacto")),
        "rnc_ced": _clean(payload.get("rnc_ced")),
        "tel1": _clean(payload.get("tel1")),
        "tel2": _clean(payload.get("tel2")),
        "fax": _clean(payload.get("fax")),
        "email": _clean(payload.get("email")),
        "comentario": _clean(payload.get("comentario")),
        "nom_vend": _clean(payload.get("nom_vend")),
        "dir_factura": _clean(payload.get("dir_factura")),
        "dir_mercancia": _clean(payload.get("dir_mercancia")),
        "nomref1": _clean(payload.get("nomref1")),
        "telref1": _clean(payload.get("telref1")),
        "parentref1": _clean(payload.get("parentref1")),
        "nomref2": _clean(payload.get("nomref2")),
        "telref2": _clean(payload.get("telref2")),
        "parentref2": _clean(payload.get("parentref2")),
        "cobro_elect": "N",
        "bloqueado": "Y" if bool(payload.get("bloqueado")) else "N",
        "terminal": socket.gethostname(),
        # Guardar solo fecha (hora 00:00:00) para FECHA_ACT.
        "fecha_act": fecha_local,
    }
    if "foto" in payload:
        update_data["foto"] = _clean(payload.get("foto"))

    id_vendedor = _clean(payload.get("id_vendedor"))
    if id_vendedor is not None:
        try:
            update_data["id_vendedor"] = int(str(id_vendedor))
        except ValueError:
            return JsonResponse({"detail": "id_vendedor invalido"}, status=400)
    update_data["id_usuario"] = int(auth_payload["usuario_id"])

    id_sector = _clean(payload.get("id_sector"))
    if id_sector is not None:
        try:
            update_data["id_sector"] = int(str(id_sector))
        except ValueError:
            return JsonResponse({"detail": "id_sector invalido"}, status=400)

    # Optional fields already present in the form.
    id_grupo = _clean(payload.get("id_grupo"))
    grupo_sync_data = None
    if id_grupo is not None:
        try:
            update_data["id_grupo"] = int(str(id_grupo))
        except ValueError:
            return JsonResponse({"detail": "id_grupo invalido"}, status=400)
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT TOP 1
                    ISNULL(SERIE, ''),
                    ISNULL(PREFIJO, ''),
                    ISNULL(TIPO, ''),
                    ISNULL(CTA_ASOCIADA, ''),
                    ISNULL(CTA_ANTICIPO, ''),
                    ISNULL(CTA_COMP_ANT, ''),
                    ISNULL(DESCRIPCION, '')
                FROM GRUPO_CLIENTE
                WHERE ID_GRUPO = %s
                """,
                [update_data["id_grupo"]],
            )
            row = cursor.fetchone()
        if not row:
            return JsonResponse({"detail": "Grupo SN no encontrado"}, status=400)
        grupo_sync_data = {
            "serie": str(row[0] or "").strip(),
            "prefijo": str(row[1] or "").strip(),
            "tipo": str(row[2] or "").strip(),
            "cta_asociada": str(row[3] or "").strip(),
            "cta_anticipo": str(row[4] or "").strip(),
            "cta_comp_ant": str(row[5] or "").strip(),
            "descripcion": str(row[6] or "").strip(),
        }
        update_data["secuencia"] = grupo_sync_data["serie"] or None
        update_data["clase_sn"] = grupo_sync_data["tipo"] or None
        update_data["cta_asociada"] = grupo_sync_data["cta_asociada"] or None
        update_data["cta_anticipo"] = grupo_sync_data["cta_anticipo"] or None
        update_data["cta_comp_ant"] = grupo_sync_data["cta_comp_ant"] or None
        update_data["descripcion"] = grupo_sync_data["descripcion"] or _clean(payload.get("descripcion"))
    else:
        update_data["descripcion"] = _clean(payload.get("descripcion"))

    id_condicion = _clean(payload.get("id_condicion"))
    if id_condicion is not None:
        try:
            update_data["id_condicion"] = int(str(id_condicion))
        except ValueError:
            return JsonResponse({"detail": "id_condicion invalido"}, status=400)

    id_precio = _clean(payload.get("id_precio"))
    if id_precio is not None:
        try:
            update_data["id_precio"] = int(str(id_precio))
        except ValueError:
            return JsonResponse({"detail": "id_precio invalido"}, status=400)

    dia = _clean(payload.get("dia"))
    if dia is not None:
        try:
            update_data["dia"] = int(str(dia))
        except ValueError:
            return JsonResponse({"detail": "dia invalido"}, status=400)

    tarifa_int = _clean(payload.get("tarifa_int"))
    if tarifa_int is not None:
        try:
            update_data["tarifa_int"] = float(str(tarifa_int))
        except ValueError:
            return JsonResponse({"detail": "tarifa_int invalido"}, status=400)
    else:
        update_data["tarifa_int"] = 0.0

    lim_credito = _clean(payload.get("lim_credito"))
    if lim_credito is not None:
        try:
            update_data["lim_credito"] = float(str(lim_credito))
        except ValueError:
            return JsonResponse({"detail": "lim_credito invalido"}, status=400)
    else:
        update_data["lim_credito"] = 0.0

    qs.update(**update_data)
    return JsonResponse({"ok": True})


@require_http_methods(["POST"])
def crear_cliente_view(request):
    auth_payload = _require_perm_json(request, "clientes", "crear")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"detail": "JSON invalido"}, status=400)

    if "bloqueado" in payload and not has_perm(auth_payload.get("usuario_id"), "clientes", "bloquear"):
        return _perm_denied_json()

    nom_socio = (payload.get("nom_socio") or "").strip()
    if not nom_socio:
        return JsonResponse({"detail": "El campo Nombre es obligatorio"}, status=400)

    rnc_ced = (payload.get("rnc_ced") or "").strip()
    if not rnc_ced:
        return JsonResponse({"detail": "El campo RNC/CED es obligatorio"}, status=400)

    id_sector_raw = payload.get("id_sector")
    if id_sector_raw is None or str(id_sector_raw).strip() == "":
        return JsonResponse({"detail": "El campo Sector es obligatorio"}, status=400)

    dir_factura = (payload.get("dir_factura") or "").strip()
    if not dir_factura:
        return JsonResponse({"detail": "El campo Direccion de cliente es obligatorio"}, status=400)

    def _clean(v):
        if v is None:
            return None
        if isinstance(v, str):
            v = v.strip()
            return v or None
        return v

    def _int_or_none(value, field_name):
        value = _clean(value)
        if value is None:
            return None
        try:
            return int(str(value))
        except ValueError:
            raise ValueError(f"{field_name} invalido")

    fecha_registro = datetime.combine(timezone.localdate(), time.min)

    try:
        id_sector = _int_or_none(payload.get("id_sector"), "id_sector")
        id_vendedor = _int_or_none(payload.get("id_vendedor"), "id_vendedor")
        id_grupo = _int_or_none(payload.get("id_grupo"), "id_grupo")
    except ValueError as exc:
        return JsonResponse({"detail": str(exc)}, status=400)
    if id_grupo is None:
        return JsonResponse({"detail": "Debes seleccionar un Grupo SN"}, status=400)

    tarifa_int_raw = _clean(payload.get("tarifa_int"))
    lim_credito_raw = _clean(payload.get("lim_credito"))
    try:
        tarifa_int_val = float(str(tarifa_int_raw)) if tarifa_int_raw is not None else 0.0
    except ValueError:
        return JsonResponse({"detail": "tarifa_int invalido"}, status=400)
    try:
        lim_credito_val = float(str(lim_credito_raw)) if lim_credito_raw is not None else 0.0
    except ValueError:
        return JsonResponse({"detail": "lim_credito invalido"}, status=400)

    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT TOP 1
                    ISNULL(SERIE, ''),
                    ISNULL(PREFIJO, ''),
                    ISNULL(TIPO, ''),
                    ISNULL(CTA_ASOCIADA, ''),
                    ISNULL(CTA_ANTICIPO, ''),
                    ISNULL(CTA_COMP_ANT, ''),
                    ISNULL(DESCRIPCION, '')
                FROM GRUPO_CLIENTE WITH (UPDLOCK, HOLDLOCK)
                WHERE ID_GRUPO = %s
                """,
                [id_grupo],
            )
            grupo_row = cursor.fetchone()
            if not grupo_row:
                return JsonResponse({"detail": "Grupo SN no encontrado"}, status=400)

            serie_raw = str(grupo_row[0] or "").strip()
            prefijo = str(grupo_row[1] or "").strip()
            clase_sn = str(grupo_row[2] or "").strip() or "Cliente"
            cta_asociada = str(grupo_row[3] or "").strip() or "11020101"
            cta_anticipo = str(grupo_row[4] or "").strip() or "21010204"
            cta_comp_ant = str(grupo_row[5] or "").strip() or "21207000"
            grupo_descripcion = str(grupo_row[6] or "").strip() or "CLIENTES"

            if not serie_raw:
                return JsonResponse({"detail": "El grupo seleccionado no tiene SERIE configurada"}, status=400)
            if not prefijo:
                return JsonResponse({"detail": "El grupo seleccionado no tiene PREFIJO configurado"}, status=400)

            try:
                next_seq_int = int(str(serie_raw))
            except Exception:
                return JsonResponse({"detail": "SERIE invalida en el grupo seleccionado"}, status=400)
            next_seq = str(next_seq_int)
            if not next_seq.startswith("1") or len(next_seq) < 2:
                return JsonResponse({"detail": "La SERIE del grupo debe iniciar con 1 y tener al menos 2 digitos"}, status=400)

            id_sn = f"{prefijo}{next_seq[1:]}"

            # Salvaguarda por si existen duplicados inesperados.
            if MaestroSn.objects.filter(id_sn=id_sn).exists():
                return JsonResponse({"detail": "El ID_SN generado ya existe. Revisa la SERIE del grupo."}, status=400)

            cursor.execute(
                """
                UPDATE GRUPO_CLIENTE
                SET SERIE = %s
                WHERE ID_GRUPO = %s
                """,
                [str(next_seq_int + 1), id_grupo],
            )

            insert_columns = [
                "ID_SN",
                "CLASE_SN",
                "SECUENCIA",
                "NOM_SOCIO",
                "CONTACTO",
                "RNC_CED",
                "TEL1",
                "TEL2",
                "FAX",
                "EMAIL",
                "ID_SECTOR",
                "COMENTARIO",
                "NOM_VEND",
                "ID_VENDEDOR",
                "DIR_FACTURA",
                "DIR_MERCANCIA",
                "NOMREF1",
                "TELREF1",
                "PARENTREF1",
                "NOMREF2",
                "TELREF2",
                "PARENTREF2",
                "ID_GRUPO",
                "TIPO_SN",
                "BLOQUEADO",
                "TERMINAL",
                "ID_USUARIO",
                "FOTO",
                "DESCRIPCION",
                "CTA_ASOCIADA",
                "CTA_ANTICIPO",
                "CTA_COMP_ANT",
                "COBRO_ELECT",
                "SALDO",
                "SALDO_ANT",
                "MORA",
                "ID_CONDICION",
                "DIA",
                "CONDICION",
                "LIM_CREDITO",
                "TARIFA_INT",
                "ID_PRECIO",
                "FACTOR",
                "MONEDA",
                "FECHA_CREACION",
                "FECHA_ACT",
                "ID_NCF",
                "TIPO_NCF",
                "MOVIMIENTO",
                "TIPO_MOV",
                "RET2",
                "RETIT",
            ]
            insert_values = [
                id_sn,
                clase_sn,
                next_seq,
                _clean(payload.get("nom_socio")),
                _clean(payload.get("contacto")),
                _clean(payload.get("rnc_ced")),
                _clean(payload.get("tel1")),
                _clean(payload.get("tel2")),
                _clean(payload.get("fax")),
                _clean(payload.get("email")),
                id_sector,
                _clean(payload.get("comentario")),
                _clean(payload.get("nom_vend")),
                id_vendedor,
                _clean(payload.get("dir_factura")),
                _clean(payload.get("dir_mercancia")),
                _clean(payload.get("nomref1")),
                _clean(payload.get("telref1")),
                _clean(payload.get("parentref1")),
                _clean(payload.get("nomref2")),
                _clean(payload.get("telref2")),
                _clean(payload.get("parentref2")),
                id_grupo,
                _clean(payload.get("tipo_sn")),
                "Y" if bool(payload.get("bloqueado")) else "N",
                socket.gethostname(),
                int(auth_payload["usuario_id"]),
                _clean(payload.get("foto")),
                grupo_descripcion,
                cta_asociada,
                cta_anticipo,
                cta_comp_ant,
                "N",
                0,
                0,
                0,
                1,
                0,
                "CONTADO",
                lim_credito_val,
                tarifa_int_val,
                1,
                1,
                "RD$",
                fecha_registro,
                fecha_registro,
                2,
                "FACTURA DE CONSUMO",
                "N",
                "C",
                "N",
                "N",
            ]
            placeholders = ", ".join(["%s"] * len(insert_columns))
            cursor.execute(
                f"INSERT INTO MAESTRO_SN ({', '.join(insert_columns)}) VALUES ({placeholders})",
                insert_values,
            )

    return JsonResponse({"ok": True, "id_sn": id_sn, "secuencia": next_seq})


@require_http_methods(["GET"])
def obtener_formato_etiquetas_view(request):
    auth_payload = _require_perm_json(request, "etiquetas", "ver_formatos")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload
    usuario_id = int(auth_payload["usuario_id"])
    registro = EtiquetaFormatoUsuario.objects.filter(id_usuario=usuario_id).first()
    formato = {}
    if registro and registro.formato_json:
        try:
            formato = json.loads(registro.formato_json)
        except Exception:
            formato = {}
    return JsonResponse({"ok": True, "formato": formato})


@require_http_methods(["POST"])
def guardar_formato_etiquetas_view(request):
    auth_payload = _require_perm_json(request, "etiquetas", "ver_formatos")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"detail": "JSON invalido"}, status=400)
    formato = payload.get("formato")
    if not isinstance(formato, dict):
        return JsonResponse({"detail": "formato invalido"}, status=400)

    usuario_id = int(auth_payload["usuario_id"])
    EtiquetaFormatoUsuario.objects.update_or_create(
        id_usuario=usuario_id,
        defaults={"formato_json": json.dumps(formato, ensure_ascii=False)},
    )
    return JsonResponse({"ok": True})
