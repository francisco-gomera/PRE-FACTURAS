import base64
import csv
import io
import json
from decimal import Decimal
from datetime import datetime

from django.db import connection, transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from ajustes.permissions import ensure_base_perms, has_perm
from cartas.whatsapp_cloud import get_runtime_settings as get_whatsapp_runtime_settings
from core.views import _base_context, _load_table_columns, render_denied
from factura.ecf_runtime import build_ecf_runtime_report
from prefacturas_app.models_existing import Usuario
from inventario.views import _load_departamento_rows
from prefacturas_app.views import _encode_delphi_clave, _get_auth_payload
from .models import (
    FacturacionElectronicaConfig,
    FacturacionElectronicaDocumento,
    FacturacionElectronicaEvento,
    FacturacionElectronicaSecuencia,
    FeriadoNacional,
    FormatoImpresionConfig,
    ImpresoraConfig,
    SegModulo,
    SegPermiso,
    SegRol,
    SegRolPermiso,
    SegUsuarioPermiso,
    SegUsuarioRol,
    UsuarioCajaPreferencia,
    WhatsAppCloudConfig,
)
from .user_signatures import get_users_with_signatures, save_user_signature
from .printer_utils import get_available_printers


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

PRINT_FORMAT_DOCUMENTS = [
    (FormatoImpresionConfig.DOCUMENTO_RECIBO_PAGO, "Recibo de pago"),
    (FormatoImpresionConfig.DOCUMENTO_FACTURA, "Factura"),
]
PRINT_FORMAT_VALUES = {
    FormatoImpresionConfig.FORMATO_A4,
    FormatoImpresionConfig.FORMATO_80MM,
    FormatoImpresionConfig.FORMATO_58MM,
}
PRINTER_DOCUMENT_TYPES = [
    ImpresoraConfig.TIPO_CUENTAS_COBRAR,
    ImpresoraConfig.TIPO_FACTURA,
    ImpresoraConfig.TIPO_FINANCIAMIENTO,
    ImpresoraConfig.TIPO_TICKET,
]


def _is_ajax_request(request):
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"

TRANSUNION_FIELDS = [
    ("tipo_entidad", "Tipo de Entidad"),
    ("codigo_cliente", "Codigo del cliente"),
    ("codigo_sucursal", "Codigo de la Sucursal"),
    ("relacion_cuenta", "Relacion del cliente con la cuenta"),
    ("nombre_completo", "Nombre completo"),
    ("cedula_nueva", "Cedula Nueva"),
    ("numero_pasaporte", "Numero de pasaporte"),
    ("razon_social", "Razon Social"),
    ("siglas", "Siglas"),
    ("rnc", "RNC"),
    ("telefono_residencia", "Telefono Residencia"),
    ("telefono_oficina", "Telefono Oficina/Empresa"),
    ("telefono_movil", "Telefono Movil"),
    ("fax", "Fax"),
    ("email", "Email"),
    ("otro", "otro"),
    ("calle_avenida", "Calle/Avenida"),
    ("esquina", "Esquina"),
    ("numero", "Numero"),
    ("edificio", "Edificio/Apartamento/Residencial"),
    ("urbanizacion", "Urbanizacion"),
    ("sector", "Sector"),
    ("ciudad", "Ciudad"),
    ("provincia_municipio", "Provincia/Municipio"),
    ("numero_cuenta", "Numero de Cuenta"),
    ("unidad_monetaria", "Unidad Monetaria"),
    ("tipo_cuenta", "Tipo de Cuenta"),
    ("fecha_apertura", "Fecha de Apertura"),
    ("fecha_vencimiento", "Fecha de Vencimiento"),
    ("limite_credito", "Limite de Credito"),
    ("credito_mas_alto", "Cedito mas alto Utilizado"),
    ("monto_cuota", "Monto de la Cuota"),
    ("cantidad_cuotas", "Cantidad de Cuotas"),
    ("fecha_ultimo_pago", "Fecha ultimo pago"),
    ("monto_ultimo_pago", "Monto ultimo pago"),
    ("balance_actual", "Balance Actual"),
    ("monto_atraso", "Monto en Atraso"),
    ("cuotas_atrasadas", "Cantidad de Cuotas atrasadas"),
    ("estatus_cuenta", "Estatus de la Cuenta"),
    ("estado_cuenta", "Estado de la cuenta"),
    ("saldo_vencido_1_30", "Saldo Vencido 1-30 dias"),
    ("saldo_vencido_31_60", "Saldo Vencido 31-60 dias"),
    ("saldo_vencido_61_90", "Saldo Vencido 61-90 dias"),
    ("saldo_vencido_91_120", "Saldo Vencido 91-120 dias"),
    ("saldo_vencido_121_150", "Saldo Vencido 121-150 dias"),
    ("saldo_vencido_151_180", "Saldo Vencido 151-180 dias"),
    ("saldo_vencido_181_mas", "Saldo Vencido 181 dias o mas"),
]


def index(request):
    ctx = _base_context(request, page_title="Ajustes", active_nav="ajustes")
    if not ctx:
        return redirect("login")
    if not has_perm(ctx["auth_payload"]["usuario_id"], "ajustes", "ver"):
        return render_denied(request, active_nav="ajustes")
    puede_ver_parametros = (
        has_perm(ctx["auth_payload"]["usuario_id"], "ajustes", "ver_parametros")
        or has_perm(ctx["auth_payload"]["usuario_id"], "ajustes", "ver_parametros_empresa")
        or has_perm(ctx["auth_payload"]["usuario_id"], "ajustes", "ver_parametros_sistema")
        or has_perm(ctx["auth_payload"]["usuario_id"], "ajustes", "ver_sectores")
    )
    ctx["submodules"] = {
        "parametros": puede_ver_parametros,
        "usuarios": has_perm(ctx["auth_payload"]["usuario_id"], "ajustes", "ver_usuarios"),
        "integraciones": (
            has_perm(ctx["auth_payload"]["usuario_id"], "ajustes", "ver_integraciones")
            or has_perm(ctx["auth_payload"]["usuario_id"], "ajustes", "ver_reportes_transunion")
        ),
    }
    return render(request, "ajustes/index.html", ctx)


def _fetch_usuarios():
    rows = []
    signed_user_ids = get_users_with_signatures()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT ID_USUARIO, USUARIO, NOMBRE, CECO, DEPTO, NIVEL, ESTADO
                FROM USUARIO
                ORDER BY USUARIO
                """
            )
            for r in cursor.fetchall():
                user_id = int(r[0]) if r[0] is not None else 0
                nivel = str(r[5] or "").strip()
                estado = str(r[6] or "").strip().upper() or "INACTIVO"
                rows.append(
                    {
                        "id_usuario": user_id,
                        "usuario": str(r[1] or "").strip(),
                        "nombre": str(r[2] or "").strip(),
                        "ceco": str(r[3] or "").strip(),
                        "depto": str(r[4] or "").strip(),
                        "nivel": nivel,
                        "estado": estado,
                        "has_firma": user_id in signed_user_ids,
                    }
                )
    except Exception:
        rows = []
    return rows


def _user_can_manage_users(usuario_id):
    return (
        has_perm(usuario_id, "ajustes", "usuarios_crear")
        or has_perm(usuario_id, "ajustes", "usuarios_editar")
        or has_perm(usuario_id, "ajustes", "usuarios_inactivar")
    )


def _load_user_caja_pref_map():
    preferencias = {}
    try:
        for item in UsuarioCajaPreferencia.objects.only("id_usuario", "metodo_pago_default"):
            metodo = str(item.metodo_pago_default or "").strip()
            preferencias[int(item.id_usuario)] = metodo if metodo in {"Efectivo", "Transferencia"} else ""
    except Exception:
        return {}
    return preferencias


def usuarios_view(request):
    ctx = _base_context(request, page_title="Usuarios y permisos", active_nav="ajustes")
    if not ctx:
        return redirect("login")
    ensure_base_perms()
    if not has_perm(ctx["auth_payload"]["usuario_id"], "ajustes", "ver_usuarios"):
        return render_denied(request, active_nav="ajustes")
    ctx["usuarios"] = _fetch_usuarios()
    
    from django.conf import settings
    dev_user = getattr(settings, "DEVELOPER_USER", "fgomera")
    is_dev = ctx["auth_payload"]["usuario_login"].lower() == dev_user.lower()
    ctx["is_developer"] = is_dev

    if is_dev:
        ctx["modulos"] = SegModulo.objects.all().order_by("nombre")
        ctx["permisos"] = (
            SegPermiso.objects.select_related("modulo")
            .exclude(modulo__codigo="ajustes", codigo="sectores_borrar")
            .order_by("modulo__nombre", "nombre")
        )
    else:
        ctx["modulos"] = SegModulo.objects.filter(activo=True).order_by("nombre")
        ctx["permisos"] = (
            SegPermiso.objects.filter(activo=True, modulo__activo=True)
            .select_related("modulo")
            .exclude(modulo__codigo="ajustes", codigo="sectores_borrar")
            .order_by("modulo__nombre", "nombre")
        )

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
    ctx["user_caja_pref_map"] = _load_user_caja_pref_map()
    ctx["permisos_ui"] = permisos_ui
    ctx["usuarios_json"] = ctx["usuarios"]
    ctx["departamentos"] = _load_departamento_rows()
    ctx["usuarios_manage_permissions"] = {
        "crear": has_perm(ctx["auth_payload"]["usuario_id"], "ajustes", "usuarios_crear"),
        "editar": has_perm(ctx["auth_payload"]["usuario_id"], "ajustes", "usuarios_editar"),
        "inactivar": has_perm(ctx["auth_payload"]["usuario_id"], "ajustes", "usuarios_inactivar"),
        "gestionar": _user_can_manage_users(ctx["auth_payload"]["usuario_id"]),
    }
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


def _validate_user_password(password_value):
    password_value = str(password_value or "")
    if not password_value:
        return "", ""
    if len(password_value) < 4:
        return "", "La clave debe tener al menos 4 caracteres."
    if len(password_value) > 12:
        return "", "La clave no puede tener mas de 12 caracteres."
    encoded = _encode_delphi_clave(password_value)
    if not encoded:
        return "", "La clave contiene caracteres no permitidos."
    return encoded, ""


@require_http_methods(["POST"])
def guardar_usuario_view(request):
    ctx = _base_context(request, page_title="Usuarios y permisos", active_nav="ajustes")
    if not ctx:
        return redirect("login")
    current_user_id = ctx["auth_payload"]["usuario_id"]
    ensure_base_perms()
    if not has_perm(current_user_id, "ajustes", "ver_usuarios"):
        return render_denied(request, active_nav="ajustes")

    action = (request.POST.get("action") or "save").strip().lower()
    raw_id = (request.POST.get("id_usuario") or "").strip()
    usuario_login = (request.POST.get("usuario") or "").strip()
    nombre = (request.POST.get("nombre") or "").strip()
    estado = (request.POST.get("estado") or "ACTIVO").strip().upper()
    password = request.POST.get("password") or ""
    departamento_ceco = (request.POST.get("departamento_ceco") or "").strip()
    departamento_depto = (request.POST.get("depto") or "").strip()
    nivel = (request.POST.get("nivel") or "Normal").strip()
    firma_file = request.FILES.get("firma_png")

    if estado not in {"ACTIVO", "INACTIVO"}:
        estado = "ACTIVO"

    is_create = not raw_id
    is_state_change = action in {"inactivate", "activate"} or estado == "INACTIVO"
    if is_create:
        required_perm = "usuarios_crear"
    elif is_state_change:
        required_perm = "usuarios_inactivar"
    else:
        required_perm = "usuarios_editar"
    if not has_perm(current_user_id, "ajustes", required_perm):
        return render_denied(request, active_nav="ajustes")

    if not usuario_login or not nombre:
        return redirect("ajustes:usuarios")
    if len(usuario_login) > 20:
        usuario_login = usuario_login[:20]
    if len(nombre) > 100:
        nombre = nombre[:100]

    encoded_password, password_error = _validate_user_password(password)
    if password_error:
        return redirect("ajustes:usuarios")

    try:
        with transaction.atomic():
            if is_create:
                if Usuario.objects.filter(usuario__iexact=usuario_login).exists():
                    return redirect("ajustes:usuarios")
                user = Usuario(
                    usuario=usuario_login,
                    nombre=nombre,
                    estado=estado,
                    ceco=departamento_ceco or None,
                    depto=departamento_depto or None,
                    nivel=nivel or None,
                    porc_desc=Decimal("0.00"),
                    conectado="N",
                    id_empresa=1,
                    cambiar_clave="N",
                    id_caja=1,
                    pos="N",
                    preliminar="N",
                    terminal=""
                )
                if encoded_password:
                    user.clave = encoded_password
                    user.clave_nueva = encoded_password
                user.save()
                if firma_file:
                    if (firma_file.content_type or "").lower() not in ("image/png", "image/x-png"):
                        return redirect("ajustes:usuarios")
                    if firma_file.size and firma_file.size > 2 * 1024 * 1024:
                        return redirect("ajustes:usuarios")
                    firma_bytes = firma_file.read()
                    if not firma_bytes:
                        return redirect("ajustes:usuarios")
                    if not save_user_signature(user.id_usuario, firma_bytes):
                        return redirect("ajustes:usuarios")
            else:
                user = Usuario.objects.select_for_update().get(id_usuario=int(raw_id))
                if int(user.id_usuario) == int(current_user_id) and estado == "INACTIVO":
                    return redirect("ajustes:usuarios")
                duplicate = (
                    Usuario.objects.filter(usuario__iexact=usuario_login)
                    .exclude(id_usuario=user.id_usuario)
                    .exists()
                )
                if duplicate:
                    return redirect("ajustes:usuarios")
                user.usuario = usuario_login
                user.nombre = nombre
                user.ceco = departamento_ceco or None
                user.depto = departamento_depto or None
                user.nivel = nivel or None
                user.estado = estado
                update_fields = ["usuario", "nombre", "ceco", "depto", "nivel", "estado"]
                if encoded_password:
                    user.clave = encoded_password
                    user.clave_nueva = encoded_password
                    update_fields.extend(["clave", "clave_nueva"])
                user.save(update_fields=update_fields)
                if firma_file:
                    if (firma_file.content_type or "").lower() not in ("image/png", "image/x-png"):
                        return redirect("ajustes:usuarios")
                    if firma_file.size and firma_file.size > 2 * 1024 * 1024:
                        return redirect("ajustes:usuarios")
                    firma_bytes = firma_file.read()
                    if not firma_bytes:
                        return redirect("ajustes:usuarios")
                    if not save_user_signature(user.id_usuario, firma_bytes):
                        return redirect("ajustes:usuarios")
    except Exception:
        return redirect("ajustes:usuarios")

    return redirect("ajustes:usuarios")


def _build_public_ecf_urls(request):
    return {
        "recepcion": request.build_absolute_uri(reverse("factura:ecf_recepcion")),
        "aprobacion": request.build_absolute_uri(reverse("factura:ecf_aprobacion")),
    }


def _build_public_whatsapp_webhook_url(request):
    return request.build_absolute_uri(reverse("cartas:whatsapp_webhook"))


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
        from django.conf import settings
        auth_payload = _get_auth_payload(request)
        dev_user = getattr(settings, "DEVELOPER_USER", "fgomera")
        is_dev = auth_payload and auth_payload.get("usuario_login", "").lower() == dev_user.lower()
        if not is_dev:
            try:
                p = SegPermiso.objects.get(id=int(permiso_id))
                if not p.activo or not p.modulo.activo:
                    return redirect("ajustes:usuarios")
            except Exception:
                return redirect("ajustes:usuarios")
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
        from django.conf import settings
        auth_payload = _get_auth_payload(request)
        dev_user = getattr(settings, "DEVELOPER_USER", "fgomera")
        is_dev = auth_payload and auth_payload.get("usuario_login", "").lower() == dev_user.lower()
        if not is_dev:
            try:
                p = SegPermiso.objects.get(id=int(permiso_id))
                if not p.activo or not p.modulo.activo:
                    return redirect("ajustes:usuarios")
            except Exception:
                return redirect("ajustes:usuarios")
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

    from django.conf import settings
    auth_payload = _get_auth_payload(request)
    dev_user = getattr(settings, "DEVELOPER_USER", "fgomera")
    is_dev = auth_payload and auth_payload.get("usuario_login", "").lower() == dev_user.lower()

    if is_dev:
        permisos_ids = list(SegPermiso.objects.values_list("id", flat=True))
    else:
        permisos_ids = list(SegPermiso.objects.filter(activo=True, modulo__activo=True).values_list("id", flat=True))
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


@require_http_methods(["POST"])
def guardar_preferencia_caja_usuario_view(request):
    ctx = _base_context(request, page_title="Usuarios y permisos", active_nav="ajustes")
    if not ctx:
        return redirect("login")
    if not has_perm(ctx["auth_payload"]["usuario_id"], "ajustes", "ver_usuarios"):
        return redirect("ajustes:usuarios")

    id_usuario = request.POST.get("id_usuario")
    metodo_pago_default = str(request.POST.get("metodo_pago_default") or "").strip()
    if not id_usuario:
        return redirect("ajustes:usuarios")

    try:
        id_usuario = int(id_usuario)
    except Exception:
        return redirect("ajustes:usuarios")

    if metodo_pago_default not in {"", "Efectivo", "Transferencia"}:
        return redirect("ajustes:usuarios")

    if not metodo_pago_default:
        UsuarioCajaPreferencia.objects.filter(id_usuario=id_usuario).delete()
    else:
        UsuarioCajaPreferencia.objects.update_or_create(
            id_usuario=id_usuario,
            defaults={"metodo_pago_default": metodo_pago_default},
        )
    return redirect("ajustes:usuarios")


def parametros_view(request):
    ctx = _base_context(request, page_title="Parametros", active_nav="ajustes")
    if not ctx:
        return redirect("login")
    usuario_id = ctx["auth_payload"]["usuario_id"]
    puede_ver_parametros = has_perm(usuario_id, "ajustes", "ver_parametros")
    ctx["submodules"] = {
        "empresa": puede_ver_parametros or has_perm(usuario_id, "ajustes", "ver_parametros_empresa"),
        "sistema_impresion": puede_ver_parametros or has_perm(usuario_id, "ajustes", "ver_parametros_sistema"),
        "sectores": puede_ver_parametros or has_perm(usuario_id, "ajustes", "ver_sectores"),
        "feriados": puede_ver_parametros,
    }
    if not (puede_ver_parametros or any(ctx["submodules"].values())):
        return render_denied(request, active_nav="ajustes")
    return render(request, "ajustes/parametros.html", ctx)


def parametros_feriados_view(request):
    ctx = _base_context(request, page_title="Parametros - Feriados nacionales", active_nav="ajustes")
    if not ctx:
        return redirect("login")
    usuario_id = ctx["auth_payload"]["usuario_id"]
    if not has_perm(usuario_id, "ajustes", "ver_parametros"):
        return render_denied(request, active_nav="ajustes")
    year = _to_int(request.GET.get("year"), timezone.localdate().year)
    if year < 1900 or year > 2100:
        year = timezone.localdate().year
    ctx["year"] = year
    ctx["feriados"] = FeriadoNacional.objects.filter(fecha__year=year).order_by("fecha")
    ctx["status"] = request.GET.get("status", "")
    return render(request, "ajustes/parametros_feriados.html", ctx)


@require_http_methods(["POST"])
def guardar_feriado_view(request):
    ctx = _base_context(request, page_title="Parametros - Feriados nacionales", active_nav="ajustes")
    if not ctx:
        return redirect("login")
    usuario_id = ctx["auth_payload"]["usuario_id"]
    if not has_perm(usuario_id, "ajustes", "ver_parametros"):
        return render_denied(request, active_nav="ajustes")
    feriado_id = _to_int(request.POST.get("id_feriado"), 0)
    fecha_text = (request.POST.get("fecha") or "").strip()
    descripcion = (request.POST.get("descripcion") or "").strip()
    year = timezone.localdate().year
    try:
        fecha = datetime.strptime(fecha_text, "%Y-%m-%d").date()
        year = fecha.year
    except ValueError:
        return redirect(f"{reverse('ajustes:parametros_feriados')}?status=fecha")
    if not descripcion:
        return redirect(f"{reverse('ajustes:parametros_feriados')}?year={year}&status=descripcion")
    no_laborable = bool(request.POST.get("no_laborable"))
    activo = bool(request.POST.get("activo"))
    try:
        if feriado_id:
            feriado = FeriadoNacional.objects.filter(id_feriado=feriado_id).first()
            if not feriado:
                return redirect(f"{reverse('ajustes:parametros_feriados')}?year={year}&status=notfound")
            feriado.fecha = fecha
            feriado.descripcion = descripcion
            feriado.no_laborable = no_laborable
            feriado.activo = activo
            feriado.save()
        else:
            FeriadoNacional.objects.create(
                fecha=fecha,
                descripcion=descripcion,
                no_laborable=no_laborable,
                activo=activo,
            )
    except Exception:
        return redirect(f"{reverse('ajustes:parametros_feriados')}?year={year}&status=duplicado")
    return redirect(f"{reverse('ajustes:parametros_feriados')}?year={year}&status=ok")


def _load_formatos_impresion():
    formatos_impresion = {
        documento: FormatoImpresionConfig.FORMATO_A4
        for documento, _ in PRINT_FORMAT_DOCUMENTS
    }
    for documento, _ in PRINT_FORMAT_DOCUMENTS:
        try:
            config, _ = FormatoImpresionConfig.objects.get_or_create(
                documento=documento,
                defaults={"formato": FormatoImpresionConfig.FORMATO_A4},
            )
            formatos_impresion[documento] = (
                config.formato if config.formato in PRINT_FORMAT_VALUES else FormatoImpresionConfig.FORMATO_A4
            )
        except Exception:
            formatos_impresion[documento] = FormatoImpresionConfig.FORMATO_A4
    return formatos_impresion


def _load_impresoras_disponibles():
    """Carga la lista de impresoras disponibles en el sistema."""
    try:
        printers = get_available_printers()
        if not isinstance(printers, list):
            return []
        return sorted(printers, key=lambda x: (not x.get('es_predeterminada'), x.get('nombre', '')))
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Error cargando impresoras: {str(e)}")
        return []


def _normalize_terminal_name(value):
    terminal = " ".join(str(value or "").split()).strip()
    return terminal[:100] or "default"


def _get_preferencia_impresora_row(nombre_terminal, tipo_documento):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT TOP 1
                NOMBRE_IMPRESORA,
                PREDETERMINADA
            FROM AJUSTE_IMPRESORA_CONFIG
            WHERE TIPO_DOCUMENTO = %s
            """,
            [tipo_documento],
        )
        row = cursor.fetchone()
    if not row:
        return {"nombre_impresora": "", "predeterminada": False}
    return {
        "nombre_impresora": str(row[0] or "").strip(),
        "predeterminada": bool(row[1]),
    }


def _save_preferencia_impresora_row(nombre_terminal, tipo_documento, nombre_impresora):
    predeterminada = 1 if nombre_impresora else 0
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE AJUSTE_IMPRESORA_CONFIG
            SET NOMBRE_IMPRESORA = %s,
                PREDETERMINADA = %s,
                ACTUALIZADO_EN = GETDATE()
            WHERE TIPO_DOCUMENTO = %s
            """,
            [nombre_impresora, predeterminada, tipo_documento],
        )
        if cursor.rowcount:
            return
        cursor.execute(
            """
            INSERT INTO AJUSTE_IMPRESORA_CONFIG
                (TIPO_DOCUMENTO, NOMBRE_IMPRESORA, PREDETERMINADA, CREADO_EN, ACTUALIZADO_EN)
            VALUES
                (%s, %s, %s, GETDATE(), GETDATE())
            """,
            [tipo_documento, nombre_impresora, predeterminada],
        )


def _load_preferencias_impresora(nombre_terminal="default"):
    """Carga las preferencias de impresora configuradas."""
    nombre_terminal = _normalize_terminal_name(nombre_terminal)
    preferencias = {}
    
    for tipo_doc in PRINTER_DOCUMENT_TYPES:
        try:
            preferencias[tipo_doc] = _get_preferencia_impresora_row(nombre_terminal, tipo_doc)
        except Exception:
            preferencias[tipo_doc] = {
                "nombre_impresora": "",
                "predeterminada": False,
            }
    
    return preferencias


def _preferencias_impresora_payload(nombre_terminal):
    nombre_terminal = _normalize_terminal_name(nombre_terminal)
    return {
        "terminal": nombre_terminal,
        "preferencias": _load_preferencias_impresora(nombre_terminal),
    }


def _load_empresa_parametros():
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
        "habilitar_fact_stock": False,
    }
    try:
        empresa_columns = set(_load_table_columns("EMPRESA"))
        if not empresa_columns:
            return empresa
        select_columns = ["ID_EMPRESA", "NOMBRE", "DIR_EMP", "TEL1", "TEL2", "EMAIL", "RNC_CED"]
        for optional_column in ("LOGO", "LOGO_TIPO", "SELLO", "HABILITAR_FACT_STOCK"):
            if optional_column in empresa_columns:
                select_columns.append(optional_column)
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT TOP 1 {', '.join(select_columns)} FROM EMPRESA")
            row = cursor.fetchone()
            if row:
                data = {select_columns[index]: row[index] for index in range(min(len(select_columns), len(row)))}
                empresa["id_empresa"] = data.get("ID_EMPRESA") or ""
                empresa["nombre"] = data.get("NOMBRE") or ""
                empresa["direccion"] = data.get("DIR_EMP") or ""
                empresa["tel1"] = data.get("TEL1") or ""
                empresa["tel2"] = data.get("TEL2") or ""
                empresa["email"] = data.get("EMAIL") or ""
                empresa["rnc"] = data.get("RNC_CED") or ""
                if data.get("LOGO"):
                    empresa["logo_b64"] = base64.b64encode(data.get("LOGO")).decode("ascii")
                empresa["logo_tipo"] = data.get("LOGO_TIPO") or ""
                if data.get("SELLO"):
                    empresa["sello_b64"] = base64.b64encode(data.get("SELLO")).decode("ascii")
                empresa["habilitar_fact_stock"] = _bool_post(data.get("HABILITAR_FACT_STOCK"))
    except Exception:
        pass
    return empresa


def parametros_empresa_view(request):
    ctx = _base_context(request, page_title="Parametros - Empresa", active_nav="ajustes")
    if not ctx:
        return redirect("login")
    usuario_id = ctx["auth_payload"]["usuario_id"]
    if not (
        has_perm(usuario_id, "ajustes", "ver_parametros")
        or has_perm(usuario_id, "ajustes", "ver_parametros_empresa")
    ):
        return render_denied(request, active_nav="ajustes")
    ctx["empresa_data"] = _load_empresa_parametros()
    return render(request, "ajustes/parametros_empresa.html", ctx)


def parametros_sistema_impresion_view(request):
    ctx = _base_context(request, page_title="Parametros - Sistema e impresion", active_nav="ajustes")
    if not ctx:
        return redirect("login")
    usuario_id = ctx["auth_payload"]["usuario_id"]
    if not (
        has_perm(usuario_id, "ajustes", "ver_parametros")
        or has_perm(usuario_id, "ajustes", "ver_parametros_sistema")
    ):
        return render_denied(request, active_nav="ajustes")
    from prefacturas_app.models import CodigoVariable

    ctx["formatos_impresion"] = _load_formatos_impresion()
    ctx["empresa_data"] = _load_empresa_parametros()
    ctx["impresoras_disponibles"] = _load_impresoras_disponibles()
    ctx["preferencias_impresora"] = _load_preferencias_impresora()
    ctx["codigos_variables"] = CodigoVariable.objects.all().order_by("prefijo")
    ctx["tipos_documento_impresora"] = [
        (ImpresoraConfig.TIPO_CUENTAS_COBRAR, "Cuentas por Cobrar"),
        (ImpresoraConfig.TIPO_FACTURA, "Factura"),
        (ImpresoraConfig.TIPO_FINANCIAMIENTO, "Financiamiento"),
        (ImpresoraConfig.TIPO_TICKET, "Venta POS"),
    ]
    return render(request, "ajustes/parametros_sistema_impresion.html", ctx)


def preferencias_impresora_view(request):
    ctx = _base_context(request, page_title="Parametros - Sistema e impresion", active_nav="ajustes")
    if not ctx:
        return JsonResponse({"detail": "Sesion requerida."}, status=401)
    usuario_id = ctx["auth_payload"]["usuario_id"]
    if not (
        has_perm(usuario_id, "ajustes", "ver_parametros")
        or has_perm(usuario_id, "ajustes", "ver_parametros_sistema")
    ):
        return JsonResponse({"detail": "No tienes permiso para ver estas preferencias."}, status=403)
    terminal = request.GET.get("terminal") or request.POST.get("terminal_nombre") or ""
    return JsonResponse(_preferencias_impresora_payload(terminal))


def _load_sectores_parametros():
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    t.ID_CODIGO,
                    t.DESCRIPCION,
                    COUNT(m.ID_SN) AS CLIENTES
                FROM Territorio t
                LEFT JOIN MAESTRO_SN m ON m.ID_SECTOR = t.ID_CODIGO
                WHERE t.DESCRIPCION IS NOT NULL AND LTRIM(RTRIM(t.DESCRIPCION)) <> ''
                GROUP BY t.ID_CODIGO, t.DESCRIPCION
                ORDER BY t.DESCRIPCION
                """
            )
            return [
                {"id_codigo": row[0], "descripcion": row[1], "clientes": int(row[2] or 0)}
                for row in cursor.fetchall()
            ]
    except Exception:
        return []


def parametros_sectores_view(request):
    ctx = _base_context(request, page_title="Parametros - Sectores", active_nav="ajustes")
    if not ctx:
        return redirect("login")
    usuario_id = ctx["auth_payload"]["usuario_id"]
    if not (has_perm(usuario_id, "ajustes", "ver_parametros") or has_perm(usuario_id, "ajustes", "ver_sectores")):
        return render_denied(request, active_nav="ajustes")
    ctx["sectores"] = _load_sectores_parametros()
    ctx["sector_perms"] = {
        "crear": has_perm(usuario_id, "ajustes", "sectores_crear"),
        "editar": has_perm(usuario_id, "ajustes", "sectores_editar"),
    }
    ctx["status"] = request.GET.get("status", "")
    return render(request, "ajustes/parametros_sectores.html", ctx)


def _next_sector_id():
    with connection.cursor() as cursor:
        cursor.execute("SELECT ISNULL(MAX(ID_CODIGO), 0) + 1 FROM Territorio")
        row = cursor.fetchone()
    return int(row[0] or 1)


@require_http_methods(["POST"])
def crear_sector_view(request):
    ctx = _base_context(request, page_title="Parametros - Sectores", active_nav="ajustes")
    if not ctx:
        return redirect("login")
    if not has_perm(ctx["auth_payload"]["usuario_id"], "ajustes", "sectores_crear"):
        return render_denied(request, active_nav="ajustes")
    descripcion = (request.POST.get("descripcion") or "").strip()
    if descripcion:
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO Territorio (ID_CODIGO, DESCRIPCION) VALUES (%s, %s)",
                    [_next_sector_id(), descripcion],
                )
        except Exception:
            try:
                with connection.cursor() as cursor:
                    cursor.execute("INSERT INTO Territorio (DESCRIPCION) VALUES (%s)", [descripcion])
            except Exception:
                return redirect(f"{reverse('ajustes:parametros_sectores')}?status=error")
    return redirect("ajustes:parametros_sectores")


@require_http_methods(["POST"])
def editar_sector_view(request):
    ctx = _base_context(request, page_title="Parametros - Sectores", active_nav="ajustes")
    if not ctx:
        return redirect("login")
    if not has_perm(ctx["auth_payload"]["usuario_id"], "ajustes", "sectores_editar"):
        return render_denied(request, active_nav="ajustes")
    id_codigo = _to_int(request.POST.get("id_codigo"), 0)
    descripcion = (request.POST.get("descripcion") or "").strip()
    if id_codigo > 0 and descripcion:
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE Territorio SET DESCRIPCION = %s WHERE ID_CODIGO = %s",
                    [descripcion, id_codigo],
                )
        except Exception:
            return redirect(f"{reverse('ajustes:parametros_sectores')}?status=error")
    return redirect("ajustes:parametros_sectores")


def integraciones_view(request):
    ctx = _base_context(request, page_title="Integraciones", active_nav="ajustes")
    if not ctx:
        return redirect("login")
    puede_ver_integraciones = has_perm(ctx["auth_payload"]["usuario_id"], "ajustes", "ver_integraciones")
    puede_ver_transunion = has_perm(ctx["auth_payload"]["usuario_id"], "ajustes", "ver_reportes_transunion")
    if not (puede_ver_integraciones or puede_ver_transunion):
        return render_denied(request, active_nav="ajustes")
    ctx["submodules"] = {
        "facturacion_electronica": puede_ver_integraciones,
        "whatsapp": puede_ver_integraciones,
        "reportes_transunion": puede_ver_transunion,
    }
    return render(request, "ajustes/integraciones.html", ctx)


@require_http_methods(["POST"])
def guardar_parametros_view(request):
    ctx = _base_context(request, page_title="Parametros", active_nav="ajustes")
    if not ctx:
        return redirect("login")
    usuario_id = ctx["auth_payload"]["usuario_id"]
    if not (
        has_perm(usuario_id, "ajustes", "ver_parametros")
        or has_perm(usuario_id, "ajustes", "ver_parametros_empresa")
    ):
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
        return redirect("ajustes:parametros_empresa")

    logo_bytes = None
    logo_mime = ""
    if logo_file:
        logo_mime = (logo_file.content_type or "").lower()
        if not logo_mime.startswith("image/"):
            return redirect("ajustes:parametros_empresa")
        if logo_file.size and logo_file.size > 2 * 1024 * 1024:
            return redirect("ajustes:parametros_empresa")
        logo_bytes = logo_file.read() or None
    sello_bytes = None
    if sello_file:
        sello_mime = (sello_file.content_type or "").lower()
        if sello_mime not in ("image/png", "image/x-png"):
            return redirect("ajustes:parametros_empresa")
        if sello_file.size and sello_file.size > 2 * 1024 * 1024:
            return redirect("ajustes:parametros_empresa")
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
        return redirect("ajustes:parametros_empresa")

    return redirect("ajustes:parametros_empresa")


@require_http_methods(["POST"])
def guardar_formatos_impresion_view(request):
    ctx = _base_context(request, page_title="Parametros - Sistema e impresion", active_nav="ajustes")
    if not ctx:
        return redirect("login")
    usuario_id = ctx["auth_payload"]["usuario_id"]
    if not (
        has_perm(usuario_id, "ajustes", "ver_parametros")
        or has_perm(usuario_id, "ajustes", "ver_parametros_sistema")
    ):
        return render_denied(request, active_nav="ajustes")

    formato_recibo_pago = (request.POST.get("formato_recibo_pago") or FormatoImpresionConfig.FORMATO_A4).strip()
    formato_factura = (request.POST.get("formato_factura") or FormatoImpresionConfig.FORMATO_A4).strip()
    if formato_recibo_pago not in PRINT_FORMAT_VALUES:
        formato_recibo_pago = FormatoImpresionConfig.FORMATO_A4
    if formato_factura not in PRINT_FORMAT_VALUES:
        formato_factura = FormatoImpresionConfig.FORMATO_A4
    try:
        FormatoImpresionConfig.objects.update_or_create(
            documento=FormatoImpresionConfig.DOCUMENTO_RECIBO_PAGO,
            defaults={"formato": formato_recibo_pago},
        )
        FormatoImpresionConfig.objects.update_or_create(
            documento=FormatoImpresionConfig.DOCUMENTO_FACTURA,
            defaults={"formato": formato_factura},
        )
    except Exception:
        pass
    return redirect("ajustes:parametros_sistema_impresion")


@require_http_methods(["POST"])
def guardar_preferencias_impresora_view(request):
    ctx = _base_context(request, page_title="Parametros - Sistema e impresion", active_nav="ajustes")
    if not ctx:
        return redirect("login")
    usuario_id = ctx["auth_payload"]["usuario_id"]
    if not (
        has_perm(usuario_id, "ajustes", "ver_parametros")
        or has_perm(usuario_id, "ajustes", "ver_parametros_sistema")
    ):
        return render_denied(request, active_nav="ajustes")

    terminal = _normalize_terminal_name(request.POST.get("terminal_nombre") or request.POST.get("terminal") or "")
    
    try:
        with transaction.atomic():
            for tipo_doc in PRINTER_DOCUMENT_TYPES:
                nombre_impresora = (request.POST.get(f"impresora_{tipo_doc}") or "").strip()
                _save_preferencia_impresora_row(terminal, tipo_doc, nombre_impresora)
    except Exception as exc:
        detail = f"No se pudo guardar la configuracion de impresoras: {exc}"
        if _is_ajax_request(request):
            return JsonResponse({"detail": detail}, status=500)
        return redirect(f"{reverse('ajustes:parametros_sistema_impresion')}?printer_status=error")

    if _is_ajax_request(request):
        payload = _preferencias_impresora_payload(terminal)
        payload["detail"] = "Preferencias de impresora guardadas."
        return JsonResponse(payload)
    
    return redirect(f"{reverse('ajustes:parametros_sistema_impresion')}?printer_status=ok")


@require_http_methods(["POST"])
def guardar_fact_stock_view(request):
    ctx = _base_context(request, page_title="Parametros - Sistema e impresion", active_nav="ajustes")
    if not ctx:
        return redirect("login")
    usuario_id = ctx["auth_payload"]["usuario_id"]
    if not (
        has_perm(usuario_id, "ajustes", "ver_parametros")
        or has_perm(usuario_id, "ajustes", "ver_parametros_sistema")
    ):
        return render_denied(request, active_nav="ajustes")

    habilitar_fact_stock = _bool_post(request.POST.get("habilitar_fact_stock"))
    try:
        empresa_columns = set(_load_table_columns("EMPRESA"))
        if "HABILITAR_FACT_STOCK" in empresa_columns:
            id_empresa = (request.POST.get("id_empresa") or "").strip()
            if not id_empresa:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT TOP 1 ID_EMPRESA FROM EMPRESA")
                    row = cursor.fetchone()
                    if row:
                        id_empresa = row[0]
            if id_empresa:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE EMPRESA SET HABILITAR_FACT_STOCK = %s WHERE ID_EMPRESA = %s",
                        [habilitar_fact_stock, id_empresa],
                    )
    except Exception:
        pass
    return redirect("ajustes:parametros_sistema_impresion")


@require_http_methods(["POST"])
def guardar_codigo_variable_view(request):
    ctx = _base_context(request, page_title="Parametros - Sistema e impresion", active_nav="ajustes")
    if not ctx:
        return redirect("login")
    usuario_id = ctx["auth_payload"]["usuario_id"]
    if not (
        has_perm(usuario_id, "ajustes", "ver_parametros")
        or has_perm(usuario_id, "ajustes", "ver_parametros_sistema")
    ):
        return render_denied(request, active_nav="ajustes")

    from prefacturas_app.models import CodigoVariable

    id_config = (request.POST.get("id_config") or "").strip()
    prefijo = (request.POST.get("prefijo") or "").strip()
    try:
        pos_producto = int(request.POST.get("pos_producto") or 2)
    except (TypeError, ValueError):
        pos_producto = 2
    try:
        len_producto = int(request.POST.get("len_producto") or 5)
    except (TypeError, ValueError):
        len_producto = 5
    try:
        pos_valor = int(request.POST.get("pos_valor") or 7)
    except (TypeError, ValueError):
        pos_valor = 7
    try:
        len_valor = int(request.POST.get("len_valor") or 5)
    except (TypeError, ValueError):
        len_valor = 5
    try:
        divisor_valor = Decimal(request.POST.get("divisor_valor") or "1000")
    except Exception:
        divisor_valor = Decimal("1000")
    tipo = (request.POST.get("tipo") or "peso").strip()
    activo = (request.POST.get("activo") or "Y").strip()

    if not prefijo:
        return redirect("ajustes:parametros_sistema_impresion")

    try:
        if id_config:
            # Edit
            config = CodigoVariable.objects.get(id_config=id_config)
            config.prefijo = prefijo
            config.pos_producto = pos_producto
            config.len_producto = len_producto
            config.pos_valor = pos_valor
            config.len_valor = len_valor
            config.divisor_valor = divisor_valor
            config.tipo = tipo
            config.activo = activo
            config.save()
        else:
            # Create - validate prefix uniqueness
            if CodigoVariable.objects.filter(prefijo=prefijo).exists():
                from django.urls import reverse
                return redirect(f"{reverse('ajustes:parametros_sistema_impresion')}?error=prefix_exists")
            CodigoVariable.objects.create(
                prefijo=prefijo,
                pos_producto=pos_producto,
                len_producto=len_producto,
                pos_valor=pos_valor,
                len_valor=len_valor,
                divisor_valor=divisor_valor,
                tipo=tipo,
                activo=activo
            )
    except Exception:
        pass
    return redirect("ajustes:parametros_sistema_impresion")


@require_http_methods(["POST"])
def eliminar_codigo_variable_view(request):
    ctx = _base_context(request, page_title="Parametros - Sistema e impresion", active_nav="ajustes")
    if not ctx:
        return redirect("login")
    usuario_id = ctx["auth_payload"]["usuario_id"]
    if not (
        has_perm(usuario_id, "ajustes", "ver_parametros")
        or has_perm(usuario_id, "ajustes", "ver_parametros_sistema")
    ):
        return render_denied(request, active_nav="ajustes")

    from prefacturas_app.models import CodigoVariable

    id_config = (request.POST.get("id_config") or "").strip()
    if id_config:
        try:
            CodigoVariable.objects.filter(id_config=id_config).delete()
        except Exception:
            pass
    return redirect("ajustes:parametros_sistema_impresion")


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


@require_http_methods(["GET", "POST"])
def whatsapp_view(request):
    ctx = _base_context(request, page_title="Integraciones - WhatsApp", active_nav="ajustes")
    if not ctx:
        return redirect("login")
    if not has_perm(ctx["auth_payload"]["usuario_id"], "ajustes", "ver_integraciones"):
        return render_denied(request, active_nav="ajustes")

    whatsapp_config, _ = WhatsAppCloudConfig.objects.get_or_create(
        id_config=1,
        defaults={"habilitado": False, "api_version": "v23.0"},
    )
    status_message = ""

    if request.method == "POST":
        whatsapp_config.habilitado = _bool_post(request.POST.get("whatsapp_habilitado"))
        whatsapp_config.api_version = (request.POST.get("whatsapp_api_version") or "v23.0").strip() or "v23.0"
        whatsapp_config.phone_number_id = (request.POST.get("whatsapp_phone_number_id") or "").strip() or None
        whatsapp_config.waba_id = (request.POST.get("whatsapp_waba_id") or "").strip() or None
        whatsapp_config.verify_token = (request.POST.get("whatsapp_verify_token") or "").strip() or None
        whatsapp_config.observaciones = (request.POST.get("whatsapp_observaciones") or "").strip() or None

        new_token = (request.POST.get("whatsapp_access_token") or "").strip()
        clear_token = _bool_post(request.POST.get("whatsapp_clear_access_token"))
        if clear_token:
            whatsapp_config.access_token = None
        elif new_token:
            whatsapp_config.access_token = new_token

        whatsapp_config.save()
        status_message = "Configuracion de WhatsApp Cloud API guardada."

    whatsapp_webhook_url = _build_public_whatsapp_webhook_url(request)
    whatsapp_runtime = get_whatsapp_runtime_settings()
    whatsapp_missing = []
    if not whatsapp_runtime.get("enabled"):
        whatsapp_missing.append("Integracion deshabilitada")
    if not whatsapp_runtime.get("access_token"):
        whatsapp_missing.append("Access Token")
    if not whatsapp_runtime.get("phone_number_id"):
        whatsapp_missing.append("Phone Number ID")
    if not whatsapp_runtime.get("verify_token"):
        whatsapp_missing.append("Verify Token")
    if not whatsapp_runtime.get("waba_id"):
        whatsapp_missing.append("WABA ID")
    whatsapp_readiness = {
        "habilitado": bool(whatsapp_runtime.get("enabled")),
        "api_version": bool(str(whatsapp_runtime.get("api_version") or "").strip()),
        "access_token": bool(str(whatsapp_runtime.get("access_token") or "").strip()),
        "phone_number_id": bool(str(whatsapp_runtime.get("phone_number_id") or "").strip()),
        "waba_id": bool(str(whatsapp_runtime.get("waba_id") or "").strip()),
        "verify_token": bool(str(whatsapp_runtime.get("verify_token") or "").strip()),
        "webhook_url": bool(str(whatsapp_webhook_url or "").strip()),
    }
    whatsapp_readiness["completo"] = all(whatsapp_readiness.values())

    ctx.update(
        {
            "status_message": status_message,
            "whatsapp_config": whatsapp_config,
            "whatsapp_runtime": whatsapp_runtime,
            "whatsapp_missing": whatsapp_missing,
            "whatsapp_readiness": whatsapp_readiness,
            "whatsapp_webhook_url": whatsapp_webhook_url,
        }
    )
    return render(request, "ajustes/whatsapp.html", ctx)


def _tu_clean(value):
    return str(value or "").replace("\r", " ").replace("\n", " ").strip()


def _tu_digits(value):
    return "".join(ch for ch in str(value or "") if ch.isdigit())


NUMERIC_FIELDS = {
    "limite_credito",
    "credito_mas_alto",
    "monto_cuota",
    "monto_ultimo_pago",
    "balance_actual",
    "monto_atraso",
    "saldo_vencido_1_30",
    "saldo_vencido_31_60",
    "saldo_vencido_61_90",
    "saldo_vencido_91_120",
    "saldo_vencido_121_150",
    "saldo_vencido_151_180",
    "saldo_vencido_181_mas",
}


def _tu_money(value):
    try:
        clean_val = str(value or "0").replace(",", "").strip()
        amount = Decimal(clean_val)
    except Exception:
        amount = Decimal("0")
    return f"{amount.quantize(Decimal('1'), rounding='ROUND_HALF_UP')}"


def _tu_date(value):
    if not value:
        return ""
    if hasattr(value, "strftime"):
        return value.strftime("%Y%m%d")
    raw = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y%m%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y%m%d")
        except Exception:
            continue
    return raw


def _tu_days_overdue(value, corte=None):
    if not value:
        return 0
    try:
        venc = value.date() if hasattr(value, "date") else datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
        corte = corte or timezone.localdate()
        return max((corte - venc).days, 0)
    except Exception:
        return 0


def _tu_bucket(days, saldo):
    saldo = Decimal(str(saldo or "0"))
    buckets = {
        "saldo_vencido_1_30": Decimal("0"),
        "saldo_vencido_31_60": Decimal("0"),
        "saldo_vencido_61_90": Decimal("0"),
        "saldo_vencido_91_120": Decimal("0"),
        "saldo_vencido_121_150": Decimal("0"),
        "saldo_vencido_151_180": Decimal("0"),
        "saldo_vencido_181_mas": Decimal("0"),
    }
    if days <= 0 or saldo <= 0:
        return buckets
    if days <= 30:
        buckets["saldo_vencido_1_30"] = saldo
    elif days <= 60:
        buckets["saldo_vencido_31_60"] = saldo
    elif days <= 90:
        buckets["saldo_vencido_61_90"] = saldo
    elif days <= 120:
        buckets["saldo_vencido_91_120"] = saldo
    elif days <= 150:
        buckets["saldo_vencido_121_150"] = saldo
    elif days <= 180:
        buckets["saldo_vencido_151_180"] = saldo
    else:
        buckets["saldo_vencido_181_mas"] = saldo
    return buckets


def _tu_pick_existing_column(columns, *candidates):
    available = {str(column).upper(): str(column).upper() for column in columns or []}
    for candidate in candidates:
        found = available.get(str(candidate or "").upper())
        if found:
            return found
    return None


def _tu_decimal(value):
    try:
        return Decimal(str(value or "0"))
    except Exception:
        return Decimal("0")


def _tu_prestamo_pending(row):
    cuota = _tu_decimal(row.get("cuota"))
    balance = _tu_decimal(row.get("balance"))
    saldo_insoluto = _tu_decimal(row.get("saldo_insoluto"))
    abono_cuota = _tu_decimal(row.get("abono_cuota"))
    if balance > Decimal("0.01"):
        return balance
    if cuota > Decimal("0"):
        rebuilt = max(cuota - abono_cuota, Decimal("0"))
        if rebuilt > Decimal("0.01") or abono_cuota > Decimal("0.01"):
            return rebuilt
    if saldo_insoluto > Decimal("0.01"):
        return saldo_insoluto
    return Decimal("0")


def _tu_load_prestamo_summary(doc_numbers, corte=None):
    docs = [str(doc or "").strip() for doc in doc_numbers if str(doc or "").strip()]
    if not docs:
        return {}
    corte = corte or timezone.localdate()
    try:
        columns = _load_table_columns("DET_PRESTAMO")
    except Exception:
        return {}
    doc_col = _tu_pick_existing_column(columns, "NO_DOC", "ID_DOC", "DOCUMENTO", "FACTURA")
    cuota_num_col = _tu_pick_existing_column(columns, "NO_CUOTA", "CUOTA_NUM", "NUM_CUOTA")
    cuota_col = _tu_pick_existing_column(columns, "CUOTA", "MONTO_CUOTA", "VALOR_CUOTA")
    balance_col = _tu_pick_existing_column(columns, "BALANCE")
    saldo_insoluto_col = _tu_pick_existing_column(columns, "SALDO_INSOLUTO")
    fecha_venc_col = _tu_pick_existing_column(columns, "FECHA_VENC", "F_VENC", "VENCIMIENTO")
    abono_cuota_col = _tu_pick_existing_column(columns, "ABONO_CUOTA", "ABONOCUOTA", "ABONO_CUENTA", "ABONOCUENTA")
    if not doc_col:
        return {}

    selected = [
        col
        for col in (doc_col, cuota_num_col, cuota_col, balance_col, saldo_insoluto_col, fecha_venc_col, abono_cuota_col)
        if col
    ]
    selected = list(dict.fromkeys(selected))
    placeholders = ", ".join(["%s"] * len(docs))
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT {', '.join(f'[{col}]' for col in selected)}
                FROM DET_PRESTAMO
                WHERE CAST([{doc_col}] AS NVARCHAR(255)) IN ({placeholders})
                ORDER BY [{doc_col}], [{cuota_num_col or doc_col}]
                """,
                docs,
            )
            fetched = cursor.fetchall()
            descriptions = [col[0].upper() for col in cursor.description]
    except Exception:
        return {}

    summaries = {}
    for raw_row in fetched:
        data = {descriptions[idx]: raw_row[idx] for idx in range(len(descriptions))}
        doc = str(data.get(doc_col) or "").strip()
        if not doc:
            continue
        cuota_value = _tu_decimal(data.get(cuota_col))
        item = {
            "cuota": cuota_value,
            "balance": data.get(balance_col),
            "saldo_insoluto": data.get(saldo_insoluto_col),
            "fecha_venc": data.get(fecha_venc_col),
            "abono_cuota": data.get(abono_cuota_col),
        }
        pending = _tu_prestamo_pending(item)
        days = _tu_days_overdue(item.get("fecha_venc"), corte)
        summary = summaries.setdefault(
            doc,
            {
                "cantidad_cuotas": 0,
                "cuotas_pendientes": 0,
                "cuotas_atrasadas": 0,
                "balance_actual": Decimal("0"),
                "monto_atraso": Decimal("0"),
                "monto_cuota": Decimal("0"),
                "fecha_vencimiento": None,
                "buckets": {
                    "saldo_vencido_1_30": Decimal("0"),
                    "saldo_vencido_31_60": Decimal("0"),
                    "saldo_vencido_61_90": Decimal("0"),
                    "saldo_vencido_91_120": Decimal("0"),
                    "saldo_vencido_121_150": Decimal("0"),
                    "saldo_vencido_151_180": Decimal("0"),
                    "saldo_vencido_181_mas": Decimal("0"),
                },
            },
        )
        summary["cantidad_cuotas"] += 1
        if pending > Decimal("0.01"):
            summary["cuotas_pendientes"] += 1
            summary["balance_actual"] += pending
            if summary["monto_cuota"] <= Decimal("0") and cuota_value > Decimal("0"):
                summary["monto_cuota"] = cuota_value
            if days > 0:
                summary["cuotas_atrasadas"] += 1
                summary["monto_atraso"] += pending
                for key, value in _tu_bucket(days, pending).items():
                    summary["buckets"][key] += value
        fecha_venc = item.get("fecha_venc")
        if fecha_venc and (not summary["fecha_vencimiento"] or fecha_venc > summary["fecha_vencimiento"]):
            summary["fecha_vencimiento"] = fecha_venc
    return summaries


def _tu_split_address(address):
    parts = [part.strip() for part in str(address or "").replace(",", "|").split("|") if part.strip()]
    return {
        "calle_avenida": parts[0] if parts else str(address or "").strip(),
        "sector": parts[1] if len(parts) > 1 else "",
        "ciudad": parts[2] if len(parts) > 2 else "",
        "provincia_municipio": parts[3] if len(parts) > 3 else "",
    }


def _tu_load_last_payments(doc_numbers):
    """Query DET_RECIBO_INGRESO joined with CAB_RECIBO_INGRESO for the last payment date and amount per document."""
    docs = [str(doc or "").strip() for doc in doc_numbers if str(doc or "").strip()]
    if not docs:
        return {}

    try:
        det_columns = _load_table_columns("DET_RECIBO_INGRESO")
        cab_columns = _load_table_columns("CAB_RECIBO_INGRESO")
    except Exception:
        return {}
    if not det_columns:
        return {}

    det_doc_col = _tu_pick_existing_column(det_columns, "NO_DOC", "ID_DOC", "DOCUMENTO", "FACTURA")
    if not det_doc_col:
        return {}

    det_amount_col = _tu_pick_existing_column(
        det_columns,
        "TOTAL_PAGO", "TOTAL_PAGO2", "PAGO_ABONO", "IMP_ABONO",
        "IMP_PAGADO", "IMP_PAGO", "IMP_COBRADO", "IMP_APLICADO",
        "MONTO_APLICADO", "ABONO_APLICADO", "MONTO_ABONO", "MONTO_PAGO",
        "ABONO", "PAGADO", "PAGO", "COBRO", "IMPORTE", "SALDO_VENC",
    )
    det_recibo_col = _tu_pick_existing_column(det_columns, "ID_RECIBO", "NO_RECIBO")
    det_line_col = _tu_pick_existing_column(det_columns, "NO_LINEA", "LINEA", "NO_ITEM", "ORDEN", "ID_LINEA")

    amount_expr = f"ISNULL(d.[{det_amount_col}], 0)" if det_amount_col else "0"

    use_join = bool(cab_columns and det_recibo_col)
    cab_fecha_pago_col = None
    cab_recibo_col = None
    if use_join:
        cab_fecha_pago_col = _tu_pick_existing_column(cab_columns, "FECHA_PAGO", "FECHA_DOC", "FECHA_CONT", "F_CONT", "FECHA")
        cab_recibo_col = _tu_pick_existing_column(cab_columns, "ID_RECIBO", "NO_RECIBO", "ID_DOC", "NO_DOC")
        if not cab_fecha_pago_col or not cab_recibo_col:
            use_join = False

    extra_filters = []
    if use_join:
        fecha_expr = f"c.[{cab_fecha_pago_col}]"
        join_clause = f"INNER JOIN CAB_RECIBO_INGRESO c ON CAST(c.[{cab_recibo_col}] AS NVARCHAR(255)) = CAST(d.[{det_recibo_col}] AS NVARCHAR(255))"
        order_parts = [f"c.[{cab_fecha_pago_col}] DESC", f"c.[{cab_recibo_col}] DESC"]
        if det_line_col:
            order_parts.append(f"d.[{det_line_col}] DESC")

        cab_cancel_col = _tu_pick_existing_column(cab_columns, "CANCELADO", "ANULADO")
        if cab_cancel_col:
            extra_filters.append(f"ISNULL(c.[{cab_cancel_col}], 'N') != 'Y'")
        cab_est_doc_col = _tu_pick_existing_column(cab_columns, "EST_DOC", "ESTATUS", "ESTADO")
        if cab_est_doc_col:
            extra_filters.append(f"UPPER(ISNULL(c.[{cab_est_doc_col}], '')) != 'CANCELADO'")
    else:
        det_fecha_col = _tu_pick_existing_column(det_columns, "FECHA_CONT", "F_CONT", "FECHA", "FECHA_PAGO")
        if not det_fecha_col:
            return {}
        fecha_expr = f"d.[{det_fecha_col}]"
        join_clause = ""
        order_parts = [f"d.[{det_fecha_col}] DESC"]
        if det_recibo_col:
            order_parts.append(f"d.[{det_recibo_col}] DESC")
        if det_line_col:
            order_parts.append(f"d.[{det_line_col}] DESC")

    if det_amount_col:
        extra_filters.append(f"ISNULL(d.[{det_amount_col}], 0) > 0")

    extra_filter_sql = ""
    if extra_filters:
        extra_filter_sql = " AND " + " AND ".join(extra_filters)

    det_recibo_expr = f"d.[{det_recibo_col}]" if det_recibo_col else "1"
    det_amount_expr = f"d.[{det_amount_col}]" if det_amount_col else "0"

    result = {}
    unique_docs = list(dict.fromkeys(docs))
    chunk_size = 300
    for idx in range(0, len(unique_docs), chunk_size):
        chunk = unique_docs[idx:idx + chunk_size]
        placeholders = ", ".join(["%s"] * len(chunk))
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    ;WITH LastReceipts AS (
                        SELECT
                            d.[{det_doc_col}] AS DOC_KEY,
                            {det_recibo_expr} AS REC_KEY,
                            {fecha_expr} AS FECHA_PAGO,
                            ROW_NUMBER() OVER (
                                PARTITION BY d.[{det_doc_col}]
                                ORDER BY {', '.join(order_parts)}
                            ) AS rn
                        FROM DET_RECIBO_INGRESO d
                        {join_clause}
                        WHERE CAST(d.[{det_doc_col}] AS NVARCHAR(50)) IN ({placeholders}){extra_filter_sql}
                    )
                    SELECT
                        lr.DOC_KEY,
                        lr.FECHA_PAGO,
                        SUM(ISNULL({det_amount_expr}, 0)) AS MONTO_PAGO
                    FROM LastReceipts lr
                    INNER JOIN DET_RECIBO_INGRESO d ON CAST({det_recibo_expr} AS NVARCHAR(255)) = CAST(lr.REC_KEY AS NVARCHAR(255))
                                                   AND CAST(d.[{det_doc_col}] AS NVARCHAR(255)) = CAST(lr.DOC_KEY AS NVARCHAR(255))
                    WHERE lr.rn = 1
                    GROUP BY lr.DOC_KEY, lr.FECHA_PAGO
                    """,
                    chunk,
                )
                for no_doc, fecha_pago, monto_pago in cursor.fetchall():
                    doc_key = str(no_doc or "").strip()
                    if doc_key:
                        result[doc_key] = {
                            "fecha": fecha_pago,
                            "monto": Decimal(str(monto_pago or "0")),
                        }
        except Exception:
            pass
    return result


def _tu_estatus_cuenta(dias, cuotas_atrasadas=0):
    """Determine account status based on overdue state."""
    if dias > 0 or (cuotas_atrasadas is not None and int(cuotas_atrasadas or 0) > 0):
        return "CASTIGADA"
    return "NORMAL"


def _tu_build_row(raw, corte=None):
    corte = corte or timezone.localdate()
    id_sn = _tu_clean(raw.get("id_sn"))
    rnc_ced = _tu_digits(raw.get("rnc_ced"))
    saldo = Decimal(str(raw.get("saldo") or "0"))
    total_doc = Decimal(str(raw.get("total_doc") or saldo or "0"))
    dias = _tu_days_overdue(raw.get("fecha_venc"), corte)
    buckets = raw.get("buckets") or _tu_bucket(dias, saldo)
    address = _tu_split_address(raw.get("dir_factura") or raw.get("ent_factura"))
    is_company = len(rnc_ced) == 9
    last_payment_amount = Decimal(str(raw.get("monto_ultimo_pago") or "0"))
    monto_atraso = _tu_decimal(raw.get("monto_atraso")) if raw.get("monto_atraso") is not None else (saldo if dias > 0 else Decimal("0"))
    cuotas_atrasadas = raw.get("cuotas_atrasadas")
    if cuotas_atrasadas is None:
        cuotas_atrasadas = 1 if dias > 0 and saldo > 0 else 0
    est_doc = str(raw.get("est_doc") or "ABIERTO").strip().upper()
    estado_cuenta = "ABIERTA" if est_doc == "ABIERTO" else "CERRADA"
    row = {
        "tipo_entidad": "E" if is_company else "I",
        "codigo_cliente": id_sn,
        "codigo_sucursal": "1",
        "relacion_cuenta": "D",
        "nombre_completo": "" if is_company else _tu_clean(raw.get("nom_socio")),
        "cedula_nueva": "" if is_company else rnc_ced,
        "numero_pasaporte": "",
        "razon_social": _tu_clean(raw.get("nom_socio")) if is_company else "",
        "siglas": "",
        "rnc": rnc_ced if is_company else "",
        "telefono_residencia": _tu_clean(raw.get("tel2")),
        "telefono_oficina": _tu_clean(raw.get("celular") or raw.get("fax")),
        "telefono_movil": _tu_clean(raw.get("tel1")),
        "fax": "",
        "email": _tu_clean(raw.get("email")),
        "otro": "",
        "calle_avenida": address["calle_avenida"],
        "esquina": "",
        "numero": "",
        "edificio": "",
        "urbanizacion": "",
        "sector": _tu_clean(raw.get("sector") or address["sector"]),
        "ciudad": "",
        "provincia_municipio": _tu_clean(raw.get("provincia_municipio") or address["provincia_municipio"]),
        "numero_cuenta": _tu_clean(raw.get("numero_cuenta")),
        "unidad_monetaria": "R",
        "tipo_cuenta": "CREDITO_COMERCIAL",
        "fecha_apertura": _tu_date(raw.get("fecha_doc")),
        "fecha_vencimiento": _tu_date(raw.get("fecha_venc")),
        "limite_credito": _tu_money(max(total_doc, saldo)),
        "credito_mas_alto": _tu_money(max(total_doc, saldo)),
        "monto_cuota": _tu_money(raw.get("monto_cuota") or saldo),
        "cantidad_cuotas": _tu_clean(raw.get("cantidad_cuotas") or "1"),
        "fecha_ultimo_pago": _tu_date(raw.get("fecha_ultimo_pago")),
        "monto_ultimo_pago": _tu_money(last_payment_amount),
        "balance_actual": _tu_money(saldo),
        "monto_atraso": _tu_money(monto_atraso),
        "cuotas_atrasadas": str(cuotas_atrasadas),
        "estatus_cuenta": _tu_estatus_cuenta(dias, cuotas_atrasadas),
        "estado_cuenta": estado_cuenta,
    }
    for key, value in buckets.items():
        row[key] = _tu_money(value)
    return row


def _tu_load_accounts(query="", limit=100, corte=None):
    query = str(query or "").strip()
    corte = corte or timezone.localdate()
    params = []
    where = "WHERE UPPER(ISNULL(f.EST_DOC, '')) = 'ABIERTO' AND COALESCE(f.SALDO, 0) > 0"
    if query:
        where += """
          AND (
            CAST(f.ID_DOC AS NVARCHAR(50)) LIKE %s OR
            CAST(f.ID_SN AS NVARCHAR(50)) LIKE %s OR
            f.NOM_SOCIO LIKE %s OR
            f.RNC_CED LIKE %s
          )
        """
        like = f"%{query}%"
        params.extend([like, like, like, like])

    has_territorio = False
    prov_col = None
    desc_col = None
    try:
        territorio_cols = _load_table_columns("TERRITORIO")
        if territorio_cols:
            has_territorio = True
            prov_col = _tu_pick_existing_column(territorio_cols, "PROV")
            desc_col = _tu_pick_existing_column(territorio_cols, "DESCRIPCION", "DESC")
    except Exception:
        pass

    select_fields = [
        "f.ID_DOC", "f.ID_SN", "f.NOM_SOCIO", "f.RNC_CED", "f.FECHA_DOC", "f.FECHA_VENC",
        "f.TOTAL_DOC", "f.SALDO", "f.ABONO", "f.MON_DOC", "f.ENT_FACTURA",
        "s.DIR_FACTURA", "s.TEL1", "s.TEL2", "s.CELULAR", "s.FAX", "s.EMAIL", "s.COMENTARIO",
        "s.LIM_CREDITO", "s.MONEDA"
    ]
    
    join_clause = "LEFT JOIN MAESTRO_SN s ON CAST(s.ID_SN AS NVARCHAR(50)) = CAST(f.ID_SN AS NVARCHAR(50))"
    
    if has_territorio:
        if desc_col:
            select_fields.append(f"t.[{desc_col}] AS SECTOR_NAME")
        else:
            select_fields.append("NULL AS SECTOR_NAME")
            
        if prov_col:
            select_fields.append(f"t.[{prov_col}] AS SECTOR_PROV")
        else:
            select_fields.append("NULL AS SECTOR_PROV")
            
        join_clause += "\nLEFT JOIN TERRITORIO t ON CAST(t.ID_CODIGO AS NVARCHAR(50)) = CAST(s.ID_SECTOR AS NVARCHAR(50))"
    else:
        select_fields.append("NULL AS SECTOR_NAME")
        select_fields.append("NULL AS SECTOR_PROV")

    rows = []
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT TOP {max(int(limit), 1)}
                    {', '.join(select_fields)}
                FROM CAB_FACTURA f
                {join_clause}
                {where}
                ORDER BY f.FECHA_VENC, f.ID_SN, f.ID_DOC
                """,
                params,
            )
            rows = cursor.fetchall()
    except Exception:
        rows = []

    doc_ids = [row[0] for row in rows]
    prestamo_summary = _tu_load_prestamo_summary(doc_ids, corte)
    last_payments = _tu_load_last_payments(doc_ids)
    results = []
    for row in rows:
        doc_key = _tu_clean(row[0])
        loan = prestamo_summary.get(doc_key) or {}
        loan_balance = loan.get("balance_actual")
        loan_has_balance = loan_balance is not None and loan_balance > Decimal("0.01")
        last_pmt = last_payments.get(doc_key) or {}
        raw = {
            "id_doc": row[0],
            "id_sn": row[1],
            "nom_socio": row[2],
            "rnc_ced": row[3],
            "fecha_doc": row[4],
            "fecha_venc": loan.get("fecha_vencimiento") or row[5],
            "total_doc": row[6],
            "saldo": loan_balance if loan_has_balance else row[7],
            "fecha_ultimo_pago": last_pmt.get("fecha"),
            "monto_ultimo_pago": last_pmt.get("monto", Decimal("0")),
            "est_doc": "ABIERTO",
            "moneda": row[9] or row[19],
            "ent_factura": row[10],
            "dir_factura": row[11],
            "tel1": row[12],
            "tel2": row[13],
            "celular": row[14],
            "fax": row[15],
            "email": row[16],
            "sector": row[20] if (len(row) > 20 and row[20] is not None) else row[17],
            "provincia_municipio": row[21] if (len(row) > 21 and row[21] is not None) else "",
            "lim_credito": row[18],
            "numero_cuenta": row[0],
        }
        if loan:
            raw.update(
                {
                    "monto_cuota": loan.get("monto_cuota") or raw.get("saldo"),
                    "cantidad_cuotas": loan.get("cantidad_cuotas"),
                    "monto_atraso": loan.get("monto_atraso"),
                    "cuotas_atrasadas": loan.get("cuotas_atrasadas"),
                    "buckets": loan.get("buckets"),
                }
            )
        results.append(
            {
                "source": {
                    "id_doc": doc_key,
                    "id_sn": _tu_clean(row[1]),
                    "cliente": _tu_clean(row[2]),
                    "rnc_ced": _tu_clean(row[3]),
                    "fecha_venc": _tu_date(raw.get("fecha_venc")),
                    "saldo": _tu_money(raw.get("saldo")),
                    "dias_atraso": _tu_days_overdue(raw.get("fecha_venc"), corte),
                    "cuotas": str(loan.get("cantidad_cuotas") or ""),
                    "cuotas_atrasadas": str(loan.get("cuotas_atrasadas") or ""),
                    "monto_atraso": _tu_money(raw.get("monto_atraso")),
                },
                "fields": _tu_build_row(raw, corte),
            }
        )
    return results


def reportes_transunion_view(request):
    ctx = _base_context(request, page_title="Integraciones - Reportes TransUnion", active_nav="ajustes")
    if not ctx:
        return redirect("login")
    if not has_perm(ctx["auth_payload"]["usuario_id"], "ajustes", "ver_reportes_transunion"):
        return render_denied(request, active_nav="ajustes")
    ctx["transunion_fields"] = [{"key": key, "label": label} for key, label in TRANSUNION_FIELDS]
    ctx["transunion_default_filename"] = f"C0542.{timezone.localdate().strftime('%y%m%d')}"
    ctx["fecha_hoy"] = timezone.localdate()
    return render(request, "ajustes/reportes_transunion.html", ctx)


@require_http_methods(["GET"])
def reportes_transunion_cuentas_view(request):
    ctx = _base_context(request, page_title="Integraciones - Reportes TransUnion", active_nav="ajustes")
    if not ctx:
        return JsonResponse({"detail": "No autenticado"}, status=401)
    if not has_perm(ctx["auth_payload"]["usuario_id"], "ajustes", "ver_reportes_transunion"):
        return JsonResponse({"detail": "Acceso denegado."}, status=403)
    corte = _to_date_or_none(request.GET.get("corte")) or timezone.localdate()
    rows = _tu_load_accounts(
        query=request.GET.get("q") or "",
        limit=_to_int(request.GET.get("limit"), 100),
        corte=corte,
    )
    return JsonResponse(
        {
            "fields": [{"key": key, "label": label} for key, label in TRANSUNION_FIELDS],
            "results": rows,
        }
    )


@require_http_methods(["POST"])
def reportes_transunion_actualizar_cuentas_view(request):
    ctx = _base_context(request, page_title="Integraciones - Reportes TransUnion", active_nav="ajustes")
    if not ctx:
        return JsonResponse({"detail": "No autenticado"}, status=401)
    if not has_perm(ctx["auth_payload"]["usuario_id"], "ajustes", "ver_reportes_transunion"):
        return JsonResponse({"detail": "Acceso denegado."}, status=403)
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"detail": "JSON invalido."}, status=400)

    id_docs = payload.get("id_docs") or []
    if not isinstance(id_docs, list) or not id_docs:
        return JsonResponse({"detail": "No hay cuentas para actualizar."}, status=400)

    corte_str = payload.get("corte")
    corte = _to_date_or_none(corte_str) or timezone.localdate()

    id_docs_clean = [str(doc or "").strip() for doc in id_docs if str(doc or "").strip()]
    if not id_docs_clean:
        return JsonResponse({"detail": "No hay cuentas validas para actualizar."}, status=400)

    has_territorio = False
    prov_col = None
    desc_col = None
    try:
        territorio_cols = _load_table_columns("TERRITORIO")
        if territorio_cols:
            has_territorio = True
            prov_col = _tu_pick_existing_column(territorio_cols, "PROV")
            desc_col = _tu_pick_existing_column(territorio_cols, "DESCRIPCION", "DESC")
    except Exception:
        pass

    select_fields = [
        "f.ID_DOC", "f.ID_SN", "f.NOM_SOCIO", "f.RNC_CED", "f.FECHA_DOC", "f.FECHA_VENC",
        "f.TOTAL_DOC", "f.SALDO", "f.ABONO", "f.MON_DOC", "f.ENT_FACTURA",
        "s.DIR_FACTURA", "s.TEL1", "s.TEL2", "s.CELULAR", "s.FAX", "s.EMAIL", "s.COMENTARIO",
        "s.LIM_CREDITO", "s.MONEDA",
        "UPPER(ISNULL(f.EST_DOC, '')) AS EST_DOC_UPPER"
    ]
    
    join_clause = "LEFT JOIN MAESTRO_SN s ON CAST(s.ID_SN AS NVARCHAR(50)) = CAST(f.ID_SN AS NVARCHAR(50))"
    
    if has_territorio:
        if desc_col:
            select_fields.append(f"t.[{desc_col}] AS SECTOR_NAME")
        else:
            select_fields.append("NULL AS SECTOR_NAME")
            
        if prov_col:
            select_fields.append(f"t.[{prov_col}] AS SECTOR_PROV")
        else:
            select_fields.append("NULL AS SECTOR_PROV")
            
        join_clause += "\nLEFT JOIN TERRITORIO t ON CAST(t.ID_CODIGO AS NVARCHAR(50)) = CAST(s.ID_SECTOR AS NVARCHAR(50))"
    else:
        select_fields.append("NULL AS SECTOR_NAME")
        select_fields.append("NULL AS SECTOR_PROV")

    placeholders = ", ".join(["%s"] * len(id_docs_clean))
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT
                    {', '.join(select_fields)}
                FROM CAB_FACTURA f
                {join_clause}
                WHERE CAST(f.ID_DOC AS NVARCHAR(50)) IN ({placeholders})
                ORDER BY f.ID_DOC
                """,
                id_docs_clean,
            )
            rows = cursor.fetchall()
    except Exception:
        return JsonResponse({"detail": "Error al consultar las cuentas."}, status=500)

    found_docs = {}
    for row in rows:
        doc_key = _tu_clean(row[0])
        est_doc = str(row[20] or "").strip()
        saldo_val = Decimal(str(row[7] or "0"))
        is_closed = est_doc != "ABIERTO" or saldo_val <= Decimal("0")
        found_docs[doc_key] = {
            "row": row,
            "is_closed": is_closed,
            "est_doc": est_doc,
            "saldo": saldo_val,
        }

    prestamo_doc_ids = [doc for doc, info in found_docs.items() if not info["is_closed"]]
    prestamo_summary = _tu_load_prestamo_summary(prestamo_doc_ids, corte) if prestamo_doc_ids else {}
    last_payments = _tu_load_last_payments(prestamo_doc_ids) if prestamo_doc_ids else {}

    updated = []
    removed = []
    not_found = []

    for doc_id in id_docs_clean:
        info = found_docs.get(doc_id)
        if not info:
            not_found.append(doc_id)
            removed.append({"id_doc": doc_id, "reason": "No encontrada en la base de datos"})
            continue

        if info["is_closed"]:
            reason = "Cuenta saldada (saldo 0)" if info["saldo"] <= Decimal("0") else f"Cuenta cerrada (estado: {info['est_doc']})"
            removed.append({"id_doc": doc_id, "reason": reason})
            continue

        row = info["row"]
        loan = prestamo_summary.get(doc_id) or {}
        loan_balance = loan.get("balance_actual")
        loan_has_balance = loan_balance is not None and loan_balance > Decimal("0.01")
        last_pmt = last_payments.get(doc_id) or {}
        raw = {
            "id_doc": row[0],
            "id_sn": row[1],
            "nom_socio": row[2],
            "rnc_ced": row[3],
            "fecha_doc": row[4],
            "fecha_venc": loan.get("fecha_vencimiento") or row[5],
            "total_doc": row[6],
            "saldo": loan_balance if loan_has_balance else row[7],
            "fecha_ultimo_pago": last_pmt.get("fecha"),
            "monto_ultimo_pago": last_pmt.get("monto", Decimal("0")),
            "est_doc": info["est_doc"],
            "moneda": row[9] or row[19],
            "ent_factura": row[10],
            "dir_factura": row[11],
            "tel1": row[12],
            "tel2": row[13],
            "celular": row[14],
            "fax": row[15],
            "email": row[16],
            "sector": row[21] if (len(row) > 21 and row[21] is not None) else row[17],
            "provincia_municipio": row[22] if (len(row) > 22 and row[22] is not None) else "",
            "lim_credito": row[18],
            "numero_cuenta": row[0],
        }
        if loan:
            raw.update(
                {
                    "monto_cuota": loan.get("monto_cuota") or raw.get("saldo"),
                    "cantidad_cuotas": loan.get("cantidad_cuotas"),
                    "monto_atraso": loan.get("monto_atraso"),
                    "cuotas_atrasadas": loan.get("cuotas_atrasadas"),
                    "buckets": loan.get("buckets"),
                }
            )
        updated.append(
            {
                "source": {
                    "id_doc": doc_id,
                    "id_sn": _tu_clean(row[1]),
                    "cliente": _tu_clean(row[2]),
                    "rnc_ced": _tu_clean(row[3]),
                    "fecha_venc": _tu_date(raw.get("fecha_venc")),
                    "saldo": _tu_money(raw.get("saldo")),
                    "dias_atraso": _tu_days_overdue(raw.get("fecha_venc"), corte),
                    "cuotas": str(loan.get("cantidad_cuotas") or ""),
                    "cuotas_atrasadas": str(loan.get("cuotas_atrasadas") or ""),
                    "monto_atraso": _tu_money(raw.get("monto_atraso")),
                },
                "fields": _tu_build_row(raw, corte),
            }
        )

    return JsonResponse({
        "updated": updated,
        "removed": removed,
        "summary": {
            "total": len(id_docs_clean),
            "actualizadas": len(updated),
            "eliminadas": len(removed),
        },
    })


@require_http_methods(["POST"])
def reportes_transunion_generar_view(request):
    ctx = _base_context(request, page_title="Integraciones - Reportes TransUnion", active_nav="ajustes")
    if not ctx:
        return JsonResponse({"detail": "No autenticado"}, status=401)
    if not has_perm(ctx["auth_payload"]["usuario_id"], "ajustes", "ver_reportes_transunion"):
        return JsonResponse({"detail": "Acceso denegado."}, status=403)
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"detail": "JSON invalido."}, status=400)

    rows = payload.get("rows") or []
    if not isinstance(rows, list) or not rows:
        return JsonResponse({"detail": "Selecciona al menos una cuenta."}, status=400)

    fmt = str(payload.get("format") or "csv").strip().lower()
    delimiter = "|" if fmt == "txt" else ","
    extension = "txt" if fmt == "txt" else "csv"
    codigo_suscriptor = _tu_clean(payload.get("codigo_suscriptor")) or "C0542"
    corte = _to_date_or_none(payload.get("corte")) or timezone.localdate()
    filename = f"{codigo_suscriptor}.{corte.strftime('%y%m%d')}.{extension}"

    output = io.StringIO()
    writer = csv.writer(output, delimiter=delimiter, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    if payload.get("include_header", True):
        writer.writerow([label for _, label in TRANSUNION_FIELDS])
    for row in rows:
        if not isinstance(row, dict):
            continue
        cleaned_row = []
        for key, _ in TRANSUNION_FIELDS:
            val = row.get(key)
            if key in NUMERIC_FIELDS:
                val = _tu_money(val)
            cleaned_row.append(_tu_clean(val))
        writer.writerow(cleaned_row)

    response = HttpResponse(output.getvalue(), content_type="text/plain; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@require_http_methods(["POST"])
def toggle_permiso_activo_view(request):
    auth_payload = _get_auth_payload(request)
    if not auth_payload:
        return JsonResponse({"detail": "No autenticado"}, status=401)
    
    from django.conf import settings
    dev_user = getattr(settings, "DEVELOPER_USER", "fgomera")
    if auth_payload.get("usuario_login", "").lower() != dev_user.lower():
        return JsonResponse({"detail": "Acceso denegado"}, status=403)
    
    try:
        data = json.loads(request.body.decode("utf-8"))
        permiso_id = data.get("permiso_id")
        activo = bool(data.get("activo"))
        
        permiso = SegPermiso.objects.get(id=permiso_id)
        permiso.activo = activo
        permiso.save()
        
        # If this is the main module viewing permission, toggle module active state as well
        if permiso.codigo == "ver":
            modulo = permiso.modulo
            modulo.activo = activo
            modulo.save()
            
        return JsonResponse({"ok": True, "activo": activo})
    except Exception as e:
        return JsonResponse({"detail": str(e)}, status=400)
