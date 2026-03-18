import base64
from decimal import Decimal
from datetime import datetime

from django.db import connection, transaction
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from ajustes.permissions import has_perm
from core.views import _base_context, render_denied
from factura.ecf_runtime import build_ecf_runtime_report
from .models import (
    FacturacionElectronicaConfig,
    FacturacionElectronicaDocumento,
    FacturacionElectronicaEvento,
    FacturacionElectronicaSecuencia,
    SegModulo,
    SegPermiso,
    SegRol,
    SegRolPermiso,
    SegUsuarioPermiso,
    SegUsuarioRol,
)
from .user_signatures import get_users_with_signatures, save_user_signature


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


def index(request):
    ctx = _base_context(request, page_title="Ajustes", active_nav="ajustes")
    if not ctx:
        return redirect("login")
    if not has_perm(ctx["auth_payload"]["usuario_id"], "ajustes", "ver"):
        return render_denied(request, active_nav="ajustes")
    ctx["submodules"] = {
        "parametros": has_perm(ctx["auth_payload"]["usuario_id"], "ajustes", "ver_parametros"),
        "usuarios": has_perm(ctx["auth_payload"]["usuario_id"], "ajustes", "ver_usuarios"),
        "integraciones": has_perm(ctx["auth_payload"]["usuario_id"], "ajustes", "ver_integraciones"),
    }
    return render(request, "ajustes/index.html", ctx)


def _fetch_usuarios():
    rows = []
    signed_user_ids = get_users_with_signatures()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT ID_USUARIO, USUARIO, NOMBRE, ESTADO
                FROM USUARIO
                ORDER BY USUARIO
                """
            )
            for r in cursor.fetchall():
                user_id = int(r[0]) if r[0] is not None else 0
                rows.append(
                    {
                        "id_usuario": user_id,
                        "usuario": r[1],
                        "nombre": r[2],
                        "estado": r[3],
                        "has_firma": user_id in signed_user_ids,
                    }
                )
    except Exception:
        rows = []
    return rows


def usuarios_view(request):
    ctx = _base_context(request, page_title="Usuarios y permisos", active_nav="ajustes")
    if not ctx:
        return redirect("login")
    if not has_perm(ctx["auth_payload"]["usuario_id"], "ajustes", "ver_usuarios"):
        return render_denied(request, active_nav="ajustes")
    ctx["usuarios"] = _fetch_usuarios()
    ctx["modulos"] = SegModulo.objects.all().order_by("nombre")
    ctx["permisos"] = SegPermiso.objects.select_related("modulo").order_by("modulo__nombre", "nombre")
    admin_role, _ = SegRol.objects.get_or_create(
        codigo="admin",
        defaults={"nombre": "Administrador", "descripcion": "Acceso total"},
    )
    permisos_ids = list(ctx["permisos"].values_list("id", flat=True))
    if permisos_ids:
        existing = set(
            SegRolPermiso.objects.filter(rol=admin_role, permiso_id__in=permisos_ids)
            .values_list("permiso_id", flat=True)
        )
        missing = [
            SegRolPermiso(rol=admin_role, permiso_id=pid)
            for pid in permisos_ids
            if pid not in existing
        ]
        if missing:
            SegRolPermiso.objects.bulk_create(missing)
    ctx["roles"] = SegRol.objects.all().order_by("nombre")
    ctx["roles_permisos"] = SegRolPermiso.objects.select_related("rol", "permiso").all()
    ctx["usuarios_roles"] = SegUsuarioRol.objects.select_related("rol").all()
    ctx["usuarios_permisos"] = SegUsuarioPermiso.objects.select_related("permiso").all()
    palette = [
        ("#1b4f91", "#5b88c7", "#e6effc"),
        ("#0f6d63", "#4ea79b", "#e2f5f1"),
        ("#7a4b12", "#c08b4a", "#faefe0"),
        ("#5b2c83", "#8d62b7", "#efe5fb"),
        ("#1f5a8a", "#5c92be", "#e7f1fb"),
        ("#7b1f3a", "#b4647a", "#f8e6ec"),
        ("#3a6d1f", "#75ad57", "#eaf6e3"),
        ("#6e3b1f", "#a56f52", "#f5e6dc"),
    ]
    module_colors = {}
    for idx, mod in enumerate(ctx["modulos"]):
        dark, mid, light = palette[idx % len(palette)]
        module_colors[mod.id] = {"module": dark, "submodule": mid, "perm": light}

    permisos_by_module = {}
    for perm in ctx["permisos"]:
        codigo = (perm.codigo or "").lower()
        if codigo == "ver":
            level = "module"
        elif codigo.startswith("ver_"):
            level = "submodule"
        else:
            level = "perm"
        color = module_colors.get(perm.modulo_id, {}).get(level, "#f7faff")
        permisos_by_module.setdefault(perm.modulo_id, []).append(
            {
                "id": perm.id,
                "modulo": perm.modulo.nombre if perm.modulo else "",
                "codigo": perm.codigo,
                "nombre": perm.nombre,
                "level": level,
                "color": color,
            }
        )

    level_order = {"module": 0, "submodule": 1, "perm": 2}
    permisos_ui = []
    for mod in ctx["modulos"]:
        items = permisos_by_module.get(mod.id, [])
        items.sort(key=lambda x: (level_order.get(x["level"], 9), x["nombre"], x["codigo"]))
        permisos_ui.extend(items)

    user_perm_map = {}
    for up in ctx["usuarios_permisos"]:
        user_perm_map.setdefault(up.id_usuario, {})[up.permiso_id] = bool(up.permitido)
    ctx["user_perm_map"] = user_perm_map
    ctx["permisos_ui"] = permisos_ui
    return render(request, "ajustes/usuarios.html", ctx)


@require_http_methods(["POST"])
def crear_modulo_view(request):
    codigo = (request.POST.get("codigo") or "").strip()
    nombre = (request.POST.get("nombre") or "").strip()
    descripcion = (request.POST.get("descripcion") or "").strip()
    if codigo and nombre:
        SegModulo.objects.get_or_create(
            codigo=codigo,
            defaults={"nombre": nombre, "descripcion": descripcion or None},
        )
    return redirect("ajustes:usuarios")


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


def _build_public_ecf_urls(request):
    return {
        "recepcion": request.build_absolute_uri(reverse("factura:ecf_recepcion")),
        "aprobacion": request.build_absolute_uri(reverse("factura:ecf_aprobacion")),
    }


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


@require_http_methods(["POST"])
def crear_permiso_view(request):
    modulo_id = request.POST.get("modulo_id")
    codigo = (request.POST.get("codigo") or "").strip()
    nombre = (request.POST.get("nombre") or "").strip()
    descripcion = (request.POST.get("descripcion") or "").strip()
    if modulo_id and codigo and nombre:
        try:
            modulo = SegModulo.objects.get(id=modulo_id)
            SegPermiso.objects.get_or_create(
                modulo=modulo,
                codigo=codigo,
                defaults={"nombre": nombre, "descripcion": descripcion or None},
            )
        except SegModulo.DoesNotExist:
            pass
    return redirect("ajustes:usuarios")


@require_http_methods(["POST"])
def crear_rol_view(request):
    codigo = (request.POST.get("codigo") or "").strip()
    nombre = (request.POST.get("nombre") or "").strip()
    descripcion = (request.POST.get("descripcion") or "").strip()
    if codigo and nombre:
        SegRol.objects.get_or_create(
            codigo=codigo,
            defaults={"nombre": nombre, "descripcion": descripcion or None},
        )
    return redirect("ajustes:usuarios")


@require_http_methods(["POST"])
def asignar_rol_view(request):
    id_usuario = request.POST.get("id_usuario")
    rol_id = request.POST.get("rol_id")
    if id_usuario and rol_id:
        try:
            SegUsuarioRol.objects.get_or_create(
                id_usuario=int(id_usuario),
                rol_id=int(rol_id),
            )
        except Exception:
            pass
    return redirect("ajustes:usuarios")


@require_http_methods(["POST"])
def asignar_permiso_rol_view(request):
    rol_id = request.POST.get("rol_id")
    permiso_id = request.POST.get("permiso_id")
    if rol_id and permiso_id:
        try:
            SegRolPermiso.objects.get_or_create(
                rol_id=int(rol_id),
                permiso_id=int(permiso_id),
            )
        except Exception:
            pass
    return redirect("ajustes:usuarios")


@require_http_methods(["POST"])
def asignar_permiso_usuario_view(request):
    id_usuario = request.POST.get("id_usuario")
    permiso_id = request.POST.get("permiso_id")
    permitido = request.POST.get("permitido") == "1"
    if id_usuario and permiso_id:
        with transaction.atomic():
            SegUsuarioPermiso.objects.update_or_create(
                id_usuario=int(id_usuario),
                permiso_id=int(permiso_id),
                defaults={"permitido": permitido},
            )
    return redirect("ajustes:usuarios")


@require_http_methods(["POST"])
def guardar_permisos_usuario_view(request):
    id_usuario = request.POST.get("id_usuario")
    if not id_usuario:
        return redirect("ajustes:usuarios")
    try:
        id_usuario = int(id_usuario)
    except (TypeError, ValueError):
        return redirect("ajustes:usuarios")

    selected_ids = set()
    for raw_id in request.POST.getlist("perm_ids"):
        try:
            selected_ids.add(int(raw_id))
        except (TypeError, ValueError):
            continue

    permisos_ids = list(SegPermiso.objects.values_list("id", flat=True))
    existing = {
        up.permiso_id: up
        for up in SegUsuarioPermiso.objects.filter(id_usuario=id_usuario)
    }
    to_update = []
    to_create = []
    for perm_id in permisos_ids:
        permitido = perm_id in selected_ids
        if perm_id in existing:
            up = existing[perm_id]
            if up.permitido != permitido:
                up.permitido = permitido
                to_update.append(up)
        else:
            to_create.append(
                SegUsuarioPermiso(
                    id_usuario=id_usuario,
                    permiso_id=perm_id,
                    permitido=permitido,
                )
            )
    with transaction.atomic():
        if to_update:
            SegUsuarioPermiso.objects.bulk_update(to_update, ["permitido"])
        if to_create:
            SegUsuarioPermiso.objects.bulk_create(to_create)
    return redirect("ajustes:usuarios")


@require_http_methods(["POST"])
def guardar_firma_usuario_view(request):
    ctx = _base_context(request, page_title="Usuarios y permisos", active_nav="ajustes")
    if not ctx:
        return redirect("login")
    if not has_perm(ctx["auth_payload"]["usuario_id"], "ajustes", "ver_usuarios"):
        return render_denied(request, active_nav="ajustes")
    id_usuario = request.POST.get("id_usuario")
    firma_file = request.FILES.get("firma_png")
    if not id_usuario or not firma_file:
        return redirect("ajustes:usuarios")
    try:
        id_usuario = int(id_usuario)
    except (TypeError, ValueError):
        return redirect("ajustes:usuarios")

    if (firma_file.content_type or "").lower() not in ("image/png", "image/x-png"):
        return redirect("ajustes:usuarios")
    if firma_file.size and firma_file.size > 2 * 1024 * 1024:
        return redirect("ajustes:usuarios")
    firma_bytes = firma_file.read()
    if not firma_bytes:
        return redirect("ajustes:usuarios")

    if not save_user_signature(id_usuario, firma_bytes):
        return redirect("ajustes:usuarios")
    return redirect("ajustes:usuarios")


def parametros_view(request):
    ctx = _base_context(request, page_title="Parametros", active_nav="ajustes")
    if not ctx:
        return redirect("login")
    if not has_perm(ctx["auth_payload"]["usuario_id"], "ajustes", "ver_parametros"):
        return render_denied(request, active_nav="ajustes")
    empresa = {
        "id_empresa": "",
        "nombre": "",
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
                    SELECT TOP 1 ID_EMPRESA, NOMBRE, DIR_EMP, TEL1, TEL2, EMAIL, RNC_CED, LOGO, LOGO_TIPO, SELLO
                    FROM EMPRESA
                    """
                )
                row = cursor.fetchone()
                if row:
                    empresa["id_empresa"] = row[0]
                    empresa["nombre"] = row[1] or ""
                    empresa["direccion"] = row[2] or ""
                    empresa["tel1"] = row[3] or ""
                    empresa["tel2"] = row[4] or ""
                    empresa["email"] = row[5] or ""
                    empresa["rnc"] = row[6] or ""
                    if row[7]:
                        empresa["logo_b64"] = base64.b64encode(row[7]).decode("ascii")
                    empresa["logo_tipo"] = row[8] or ""
                    if row[9]:
                        empresa["sello_b64"] = base64.b64encode(row[9]).decode("ascii")
            except Exception:
                cursor.execute(
                    """
                    SELECT TOP 1 ID_EMPRESA, NOMBRE, DIR_EMP, TEL1, TEL2, EMAIL, RNC_CED
                    FROM EMPRESA
                    """
                )
                row = cursor.fetchone()
                if row:
                    empresa["id_empresa"] = row[0]
                    empresa["nombre"] = row[1] or ""
                    empresa["direccion"] = row[2] or ""
                    empresa["tel1"] = row[3] or ""
                    empresa["tel2"] = row[4] or ""
                    empresa["email"] = row[5] or ""
                    empresa["rnc"] = row[6] or ""
    except Exception:
        pass
    ctx["empresa_data"] = empresa
    return render(request, "ajustes/parametros.html", ctx)


@require_http_methods(["POST"])
def guardar_parametros_view(request):
    ctx = _base_context(request, page_title="Parametros", active_nav="ajustes")
    if not ctx:
        return redirect("login")
    if not has_perm(ctx["auth_payload"]["usuario_id"], "ajustes", "ver_parametros"):
        return render_denied(request, active_nav="ajustes")

    id_empresa = (request.POST.get("id_empresa") or "").strip()
    nombre = (request.POST.get("nombre") or "").strip()
    direccion = (request.POST.get("direccion") or "").strip()
    tel1 = (request.POST.get("tel1") or "").strip()
    tel2 = (request.POST.get("tel2") or "").strip()
    email = (request.POST.get("email") or "").strip()
    rnc = (request.POST.get("rnc") or "").strip()
    logo_file = request.FILES.get("logo_img")
    sello_file = request.FILES.get("sello_png")

    if not id_empresa:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT TOP 1 ID_EMPRESA FROM EMPRESA")
                row = cursor.fetchone()
                if row:
                    id_empresa = row[0]
        except Exception:
            id_empresa = ""

    if not id_empresa:
        return redirect("ajustes:parametros")

    logo_bytes = None
    logo_mime = ""
    if logo_file:
        logo_mime = (logo_file.content_type or "").lower()
        if not logo_mime.startswith("image/"):
            return redirect("ajustes:parametros")
        if logo_file.size and logo_file.size > 2 * 1024 * 1024:
            return redirect("ajustes:parametros")
        logo_bytes = logo_file.read() or None
    sello_bytes = None
    if sello_file:
        sello_mime = (sello_file.content_type or "").lower()
        if sello_mime not in ("image/png", "image/x-png"):
            return redirect("ajustes:parametros")
        if sello_file.size and sello_file.size > 2 * 1024 * 1024:
            return redirect("ajustes:parametros")
        sello_bytes = sello_file.read() or None

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE EMPRESA
                SET NOMBRE = %s,
                    DIR_EMP = %s,
                    TEL1 = %s,
                    TEL2 = %s,
                    EMAIL = %s,
                    RNC_CED = %s
                WHERE ID_EMPRESA = %s
                """,
                [nombre, direccion, tel1, tel2, email, rnc, id_empresa],
            )
            if logo_bytes is not None:
                try:
                    cursor.execute(
                        "UPDATE EMPRESA SET LOGO = %s WHERE ID_EMPRESA = %s",
                        [logo_bytes, id_empresa],
                    )
                    cursor.execute(
                        "UPDATE EMPRESA SET LOGO_TIPO = %s WHERE ID_EMPRESA = %s",
                        [logo_mime, id_empresa],
                    )
                except Exception:
                    pass
            if sello_bytes is not None:
                try:
                    cursor.execute(
                        "UPDATE EMPRESA SET SELLO = %s WHERE ID_EMPRESA = %s",
                        [sello_bytes, id_empresa],
                    )
                except Exception:
                    pass
    except Exception:
        return redirect("ajustes:parametros")

    return redirect("ajustes:parametros")


@require_http_methods(["GET", "POST"])
def facturacion_electronica_view(request):
    ctx = _base_context(request, page_title="Integraciones - Facturacion Electronica", active_nav="ajustes")
    if not ctx:
        return redirect("login")
    if not has_perm(ctx["auth_payload"]["usuario_id"], "ajustes", "ver_integraciones"):
        return render_denied(request, active_nav="ajustes")

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
        secuencias.append(
            {
                "codigo": codigo,
                "nombre": nombre,
                "obj": secuencia,
                "preview": preview,
            }
        )

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
    return render(request, "ajustes/facturacion_electronica.html", ctx)
