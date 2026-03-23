from django.db import connection

from .models import SegModulo, SegPermiso, SegRol, SegRolPermiso, SegUsuarioPermiso, SegUsuarioRol


def _tables_exist():
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM INFORMATION_SCHEMA.TABLES
                WHERE TABLE_NAME IN ('SEG_MODULO','SEG_PERMISO','SEG_ROL','SEG_ROL_PERMISO','SEG_USUARIO_ROL','SEG_USUARIO_PERMISO')
                """
            )
            count = cursor.fetchone()[0] or 0
        return count >= 6
    except Exception:
        return False


def ensure_base_perms():
    if not _tables_exist():
        return
    permisos_genericos = [
        ("ver", "Ver"),
        ("crear", "Crear"),
        ("editar", "Editar"),
        ("borrar", "Borrar"),
        ("imprimir", "Imprimir"),
        ("exportar", "Exportar"),
    ]
    modulos = {
        "inventario": "Inventario",
        "reportes": "Reportes",
        "etiquetas": "Etiquetas",
        "cobros": "Cobros",
        "cartas": "Cartas",
        "factura": "Factura",
        "caja": "Caja",
        "ajustes": "Ajustes",
    }
    sub_perms = {
        "inventario": [
            ("ver_articulos", "Ver Articulos"),
            ("ver_grupos", "Ver Grupos de Articulos"),
            ("ver_stock", "Ver Stock"),
        ],
        "reportes": [
            ("ver_ventas", "Ver Reportes de Ventas"),
            ("ver_clientes", "Ver Reportes de Clientes"),
            ("ver_inventario", "Ver Reportes de Inventario"),
        ],
        "etiquetas": [
            ("ver_formatos", "Ver Formatos"),
            ("ver_impresion", "Ver Impresion"),
            ("ver_historial", "Ver Historial"),
        ],
        "cobros": [
            ("ver_estado_cuenta", "Ver Estado de Cuenta"),
            ("ver_alertas", "Ver Alertas"),
            ("ver_acuerdos", "Ver Acuerdos"),
        ],
        "cartas": [
            ("ver_cartas_aviso", "Ver Cartas de Aviso"),
            ("ver_cartas_saldo", "Ver Cartas de Saldo"),
            ("ver_plantillas", "Ver Plantillas"),
        ],
        "factura": [
            ("ver_emision", "Ver Emision de Facturas"),
            ("ver_electronica", "Ver Facturacion Electronica"),
            ("ver_documentos", "Ver Documentos de Factura"),
        ],
        "caja": [
            ("ver_cuentas_por_cobrar", "Ver Cuentas por Cobrar"),
            ("cxc_nuevo", "Nuevo en Cuentas por Cobrar"),
            ("cxc_buscar", "Buscar en Cuentas por Cobrar"),
            ("cxc_imprimir", "Imprimir en Cuentas por Cobrar"),
            ("cxc_cancelar", "Cancelar en Cuentas por Cobrar"),
            ("cxc_cerrar_cuenta", "Cerrar Cuenta en Cuentas por Cobrar"),
            ("ver_cuadre_caja", "Ver Cuadre de Caja"),
            ("ver_financiamiento", "Ver Financiamiento"),
        ],
        "ajustes": [
            ("ver_parametros", "Ver Parametros"),
            ("ver_usuarios", "Ver Usuarios"),
            ("ver_integraciones", "Ver Integraciones"),
        ],
    }

    for codigo, nombre in modulos.items():
        modulo, _ = SegModulo.objects.get_or_create(
            codigo=codigo,
            defaults={"nombre": nombre},
        )
        for perm_code, perm_name in permisos_genericos:
            SegPermiso.objects.get_or_create(
                modulo=modulo,
                codigo=perm_code,
                defaults={"nombre": f"{perm_name} {nombre}"},
            )
        for perm_code, perm_name in sub_perms.get(codigo, []):
            SegPermiso.objects.get_or_create(
                modulo=modulo,
                codigo=perm_code,
                defaults={"nombre": perm_name},
            )


def ensure_admin_role():
    if not _tables_exist():
        return None
    ensure_base_perms()
    admin_role, _ = SegRol.objects.get_or_create(
        codigo="admin",
        defaults={"nombre": "Administrador", "descripcion": "Acceso total"},
    )
    permisos_ids = list(SegPermiso.objects.values_list("id", flat=True))
    if not permisos_ids:
        return admin_role
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
    return admin_role


def get_user_roles(id_usuario):
    if not _tables_exist():
        return []
    return list(SegUsuarioRol.objects.filter(id_usuario=id_usuario).select_related("rol"))


def get_user_permissions(id_usuario):
    if not _tables_exist():
        return []
    return list(SegUsuarioPermiso.objects.filter(id_usuario=id_usuario).select_related("permiso"))


def has_perm(id_usuario, modulo_codigo, permiso_codigo):
    if not _tables_exist():
        return True
    try:
        modulo = SegModulo.objects.get(codigo=modulo_codigo)
        permiso = SegPermiso.objects.get(modulo=modulo, codigo=permiso_codigo)
    except Exception:
        return False

    # Permiso directo
    direct = SegUsuarioPermiso.objects.filter(id_usuario=id_usuario, permiso=permiso).first()
    if direct is not None:
        return bool(direct.permitido)

    # Permiso por rol
    roles = SegUsuarioRol.objects.filter(id_usuario=id_usuario).select_related("rol")
    if not roles:
        return False
    if any(getattr(r.rol, "codigo", "") == "admin" for r in roles):
        return True
    rol_ids = [r.rol_id for r in roles]
    return SegRolPermiso.objects.filter(rol_id__in=rol_ids, permiso=permiso).exists()
