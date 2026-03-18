from django.urls import path

from .views import (
    aprobar_ecf_view,
    cancelar_factura_view,
    emitir_factura_view,
    facturas_buscar_view,
    emision_prefactura_detalle_view,
    emision_prefacturas_view,
    electronica_view,
    factura_print_view,
    index,
    emision_view,
    recibir_ecf_view,
)

app_name = "factura"

urlpatterns = [
    path("", index, name="index"),
    path("emision/", emision_view, name="emision"),
    path("emision/prefacturas/", emision_prefacturas_view, name="emision_prefacturas"),
    path("emision/prefactura-detalle/", emision_prefactura_detalle_view, name="emision_prefactura_detalle"),
    path("emision/facturas/", facturas_buscar_view, name="buscar_facturas"),
    path("emision/facturas/cancelar/", cancelar_factura_view, name="cancelar_factura"),
    path("emision/emitir/", emitir_factura_view, name="emitir_factura"),
    path("impresion/", factura_print_view, name="factura_impresion"),
    path("electronica/", electronica_view, name="electronica"),
    path("ecf/recepcion/", recibir_ecf_view, name="ecf_recepcion"),
    path("ecf/aprobacion/", aprobar_ecf_view, name="ecf_aprobacion"),
]
