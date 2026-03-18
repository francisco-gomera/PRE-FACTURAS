from django.urls import path

from .views import acuerdos_view, alertas_print_view, alertas_view, estado_cuenta_print_view, estado_cuenta_view, index

app_name = "cobros"

urlpatterns = [
    path("", index, name="index"),
    path("acuerdos/", acuerdos_view, name="acuerdos"),
    path("alertas/", alertas_view, name="alertas"),
    path("alertas/impresion/", alertas_print_view, name="alertas_impresion"),
    path("estado-cuenta/", estado_cuenta_view, name="estado_cuenta"),
    path("estado-cuenta/impresion/", estado_cuenta_print_view, name="estado_cuenta_impresion"),
]
