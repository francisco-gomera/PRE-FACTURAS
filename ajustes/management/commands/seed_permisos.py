from django.core.management.base import BaseCommand

from ajustes.models import SegModulo, SegPermiso


class Command(BaseCommand):
    help = "Crea modulos y permisos base para el sistema."

    def handle(self, *args, **options):
        modulos = [
            ("prefacturas", "Prefacturas"),
            ("clientes", "Clientes"),
            ("inventario", "Inventario"),
            ("reportes", "Reportes"),
            ("etiquetas", "Etiquetas"),
            ("ajustes", "Ajustes"),
            ("cobros", "Cobros"),
            ("cartas", "Cartas"),
            ("factura", "Factura"),
            ("caja", "Caja"),
            ("chat_interno", "Chat Interno"),
            ("empleados", "Empleados y Nominas"),
            ("venta_pos", "Venta POS"),
        ]
        permisos_genericos = [
            ("ver", "Ver"),
            ("crear", "Crear"),
            ("editar", "Editar"),
            ("borrar", "Borrar"),
            ("imprimir", "Imprimir"),
            ("exportar", "Exportar"),
        ]
        permisos_submodulos = {
            "inventario": [
                ("ver_articulos", "Ver Articulos"),
                ("ver_entrada_articulos", "Ver Entrada de Articulos"),
                ("ver_salida_articulos", "Ver Salida de Articulos"),
                ("ver_grupos", "Ver Grupos de Articulos"),
                ("ver_stock", "Ver Stock"),
                ("ver_solicitudes_existencia", "Ver Solicitudes de Existencia"),
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
                ("ver_financiamientos_atraso", "Ver Financiamientos en Atraso"),
                ("ver_acuerdos", "Ver Acuerdos"),
                ("ver_cartas_enviadas", "Ver Cartas Enviadas"),
            ],
            "cartas": [
                ("ver_cartas_aviso", "Ver Cartas de Aviso"),
                ("ver_cartas_saldo", "Ver Cartas de Saldo"),
                ("ver_plantillas", "Ver Plantillas"),
            ],
            "factura": [
                ("ver_emision", "Ver Emision Factura Electronica"),
                ("ver_electronica", "Ver Facturacion Electronica"),
                ("ver_documentos", "Ver Facturacion"),
            ],
            "caja": [
                ("ver_cuentas_por_cobrar", "Ver Cuentas por Cobrar"),
                ("cxc_nuevo", "Nuevo en Cuentas por Cobrar"),
                ("cxc_buscar", "Buscar en Cuentas por Cobrar"),
                ("cxc_imprimir", "Imprimir en Cuentas por Cobrar"),
                ("cxc_cancelar", "Cancelar en Cuentas por Cobrar"),
                ("cxc_cerrar_cuenta", "Cerrar Cuenta en Cuentas por Cobrar"),
                ("cxc_modificar_medio_pago", "Modificar Medio de Pago en Cuentas por Cobrar"),
                ("cxc_corregir_monto_pago", "Corregir Monto Pagado en Cuentas por Cobrar"),
                ("cxc_modificar_fechas_pago", "Modificar Fechas de Pago en Cuentas por Cobrar"),
                ("ver_cuadre_caja", "Ver Cuadre de Caja"),
                ("ver_financiamiento", "Ver Financiamiento"),
            ],
            "venta_pos": [
                ("ver", "Ver Venta POS"),
            ],
            "chat_interno": [
                ("ver_usuarios", "Ver Usuarios de Chat"),
                ("crear_grupos", "Crear Grupos de Chat"),
                ("enviar_mensajes", "Enviar Mensajes de Chat"),
            ],
            "ajustes": [
                ("ver_parametros", "Ver Parametros"),
                ("ver_parametros_empresa", "Ver Datos de Empresa"),
                ("ver_parametros_sistema", "Ver Modo del Sistema y Formatos"),
                ("ver_sectores", "Ver Sectores"),
                ("ver_usuarios", "Ver Usuarios"),
                ("ver_integraciones", "Ver Integraciones"),
                ("ver_reportes_transunion", "Ver Reportes TransUnion"),
            ],
        }

        for codigo, nombre in modulos:
            modulo, _ = SegModulo.objects.get_or_create(
                codigo=codigo,
                defaults={"nombre": nombre},
            )
            for pcod, pnom in permisos_genericos:
                permiso, _ = SegPermiso.objects.get_or_create(
                    modulo=modulo,
                    codigo=pcod,
                    defaults={"nombre": f"{pnom} {nombre}"},
                )
                esperado = f"{pnom} {nombre}"
                if permiso.nombre != esperado:
                    permiso.nombre = esperado
                    permiso.save(update_fields=["nombre"])
            for pcod, pnom in permisos_submodulos.get(codigo, []):
                permiso, _ = SegPermiso.objects.get_or_create(
                    modulo=modulo,
                    codigo=pcod,
                    defaults={"nombre": pnom},
                )
                if permiso.nombre != pnom:
                    permiso.nombre = pnom
                    permiso.save(update_fields=["nombre"])

        # Permisos específicos solicitados
        especificos = {
            "prefacturas": [
                ("guardar", "Guardar Prefacturas"),
                ("cerrar", "Cerrar Prefacturas"),
                ("cancelar", "Cancelar Prefacturas"),
                ("imprimir", "Imprimir Prefacturas"),
            ],
            "clientes": [
                ("crear", "Crear Clientes"),
                ("editar", "Editar Clientes"),
                ("bloquear", "Bloquear Clientes"),
                ("ver_estado_cuenta", "Ver Estado de Cuenta"),
            ],
            "inventario": [
                ("grupos_guardar", "Guardar Grupos de Articulos"),
                ("grupos_buscar", "Buscar Grupos de Articulos"),
                ("stock_buscar", "Buscar Stock"),
                ("stock_imprimir", "Imprimir Stock"),
            ],
            "ajustes": [
                ("usuarios_crear", "Crear Usuarios"),
                ("usuarios_editar", "Editar Usuarios"),
                ("usuarios_inactivar", "Inactivar Usuarios"),
                ("sectores_crear", "Crear Sectores"),
                ("sectores_editar", "Editar Sectores"),
            ],
        }

        for modulo_codigo, permisos in especificos.items():
            try:
                modulo = SegModulo.objects.get(codigo=modulo_codigo)
            except SegModulo.DoesNotExist:
                continue
            for pcod, pnom in permisos:
                permiso, _ = SegPermiso.objects.get_or_create(
                    modulo=modulo,
                    codigo=pcod,
                    defaults={"nombre": pnom},
                )
                if permiso.nombre != pnom:
                    permiso.nombre = pnom
                    permiso.save(update_fields=["nombre"])

        ajustes_modulo = SegModulo.objects.filter(codigo="ajustes").first()
        if ajustes_modulo:
            SegPermiso.objects.filter(modulo=ajustes_modulo, codigo="sectores_borrar").delete()

        self.stdout.write(self.style.SUCCESS("Seed de modulos y permisos base completado."))
