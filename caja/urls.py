from django.urls import path

from .views import (
    catalogo_cuentas_detalle_view,
    catalogo_cuentas_financ_view,
    financiamiento_buscar_view,
    financiamiento_guardar_view,
    financiamiento_detalle_view,
    financiamiento_facturas_disponibles_view,
    cuentas_por_cobrar_buscar_view,
    cuentas_por_cobrar_cancelar_view,
    cuentas_por_cobrar_cobros_anteriores_view,
    cuentas_por_cobrar_historial_pago_view,
    cuentas_por_cobrar_corregir_monto_view,
    cuentas_por_cobrar_detalle_view,
    cuentas_por_cobrar_guardar_view,
    cuentas_por_cobrar_marcar_impreso_view,
    cuentas_por_cobrar_medio_pago_view,
    cuentas_por_cobrar_pdf_view,
    cuentas_por_cobrar_print_data_view,
    cuentas_por_cobrar_pendientes_view,
    cuentas_por_cobrar_view,
    cuadre_caja_view,
    financiamiento_view,
    index,
)

app_name = "caja"

urlpatterns = [
    path("", index, name="index"),
    path("cuentas-por-cobrar/", cuentas_por_cobrar_view, name="cuentas_por_cobrar"),
    path(
        "cuentas-por-cobrar/pendientes/",
        cuentas_por_cobrar_pendientes_view,
        name="cuentas_por_cobrar_pendientes",
    ),
    path(
        "cuentas-por-cobrar/buscar/",
        cuentas_por_cobrar_buscar_view,
        name="cuentas_por_cobrar_buscar",
    ),
    path(
        "cuentas-por-cobrar/detalle/",
        cuentas_por_cobrar_detalle_view,
        name="cuentas_por_cobrar_detalle",
    ),
    path(
        "cuentas-por-cobrar/cobros-anteriores/",
        cuentas_por_cobrar_cobros_anteriores_view,
        name="cuentas_por_cobrar_cobros_anteriores",
    ),
    path(
        "cuentas-por-cobrar/historial-pago/",
        cuentas_por_cobrar_historial_pago_view,
        name="cuentas_por_cobrar_historial_pago",
    ),
    path(
        "cuentas-por-cobrar/print-data/",
        cuentas_por_cobrar_print_data_view,
        name="cuentas_por_cobrar_print_data",
    ),
    path(
        "cuentas-por-cobrar/pdf/",
        cuentas_por_cobrar_pdf_view,
        name="cuentas_por_cobrar_pdf",
    ),
    path(
        "cuentas-por-cobrar/guardar/",
        cuentas_por_cobrar_guardar_view,
        name="cuentas_por_cobrar_guardar",
    ),
    path(
        "cuentas-por-cobrar/marcar-impreso/",
        cuentas_por_cobrar_marcar_impreso_view,
        name="cuentas_por_cobrar_marcar_impreso",
    ),
    path(
        "cuentas-por-cobrar/medio-pago/",
        cuentas_por_cobrar_medio_pago_view,
        name="cuentas_por_cobrar_medio_pago",
    ),
    path(
        "cuentas-por-cobrar/corregir-monto/",
        cuentas_por_cobrar_corregir_monto_view,
        name="cuentas_por_cobrar_corregir_monto",
    ),
    path(
        "cuentas-por-cobrar/cancelar/",
        cuentas_por_cobrar_cancelar_view,
        name="cuentas_por_cobrar_cancelar",
    ),
    path(
        "catalogo/cuentas-detalle/",
        catalogo_cuentas_detalle_view,
        name="catalogo_cuentas_detalle",
    ),
    path(
        "catalogo/cuentas-financ/",
        catalogo_cuentas_financ_view,
        name="catalogo_cuentas_financ",
    ),
    path("cuadre-caja/", cuadre_caja_view, name="cuadre_caja"),
    path("financiamiento/", financiamiento_view, name="financiamiento"),
    path("financiamiento/buscar/", financiamiento_buscar_view, name="financiamiento_buscar"),
    path(
        "financiamiento/facturas-disponibles/",
        financiamiento_facturas_disponibles_view,
        name="financiamiento_facturas_disponibles",
    ),
    path("financiamiento/detalle/", financiamiento_detalle_view, name="financiamiento_detalle"),
    path("financiamiento/guardar/", financiamiento_guardar_view, name="financiamiento_guardar"),
]
