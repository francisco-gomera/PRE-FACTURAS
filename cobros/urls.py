from django.urls import path

from .views import (
    acuerdos_view,
    alertas_print_view,
    alertas_view,
    cartas_enviadas_view,
    estado_cuenta_print_view,
    estado_cuenta_view,
    cuentas_sin_financiamiento_atraso_view,
    financiamientos_atraso_aviso_view,
    financiamientos_atraso_view,
    index,
    pagos_recuperados_impresion_view,
    pagos_recuperados_view,
)

app_name = "cobros"

urlpatterns = [
    path("", index, name="index"),
    path("acuerdos/", acuerdos_view, name="acuerdos"),
    path("pagos-recuperados/", pagos_recuperados_view, name="pagos_recuperados"),
    path("pagos-recuperados/impresion/", pagos_recuperados_impresion_view, name="pagos_recuperados_impresion"),
    path("alertas/", alertas_view, name="alertas"),
    path("alertas/impresion/", alertas_print_view, name="alertas_impresion"),
    path("cartas-enviadas/", cartas_enviadas_view, name="cartas_enviadas"),
    path("estado-cuenta/", estado_cuenta_view, name="estado_cuenta"),
    path("estado-cuenta/impresion/", estado_cuenta_print_view, name="estado_cuenta_impresion"),
    path("cuentas-sin-financiamiento-atraso/", cuentas_sin_financiamiento_atraso_view, name="cuentas_sin_financiamiento_atraso"),
    path("financiamientos-atraso/", financiamientos_atraso_view, name="financiamientos_atraso"),
    path("financiamientos-atraso/aviso/", financiamientos_atraso_aviso_view, name="financiamientos_atraso_aviso"),
]
