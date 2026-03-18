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
            "ajustes": [
                ("ver_parametros", "Ver Parametros"),
                ("ver_usuarios", "Ver Usuarios"),
                ("ver_integraciones", "Ver Integraciones"),
            ],
        }

        for codigo, nombre in modulos:
            modulo, _ = SegModulo.objects.get_or_create(
                codigo=codigo,
                defaults={"nombre": nombre},
            )
            for pcod, pnom in permisos_genericos:
                SegPermiso.objects.get_or_create(
                    modulo=modulo,
                    codigo=pcod,
                    defaults={"nombre": f"{pnom} {nombre}"},
                )
            for pcod, pnom in permisos_submodulos.get(codigo, []):
                SegPermiso.objects.get_or_create(
                    modulo=modulo,
                    codigo=pcod,
                    defaults={"nombre": pnom},
                )

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
            ],
        }

        for modulo_codigo, permisos in especificos.items():
            try:
                modulo = SegModulo.objects.get(codigo=modulo_codigo)
            except SegModulo.DoesNotExist:
                continue
            for pcod, pnom in permisos:
                SegPermiso.objects.get_or_create(
                    modulo=modulo,
                    codigo=pcod,
                    defaults={"nombre": pnom},
                )

        self.stdout.write(self.style.SUCCESS("Seed de modulos y permisos base completado."))
