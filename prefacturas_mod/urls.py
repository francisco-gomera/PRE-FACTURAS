from django.urls import path

from prefacturas_app.views import prefactura_print_view
from .views import index

app_name = "prefacturas_mod"

urlpatterns = [
    path("", index, name="index"),
    path("impresion/", prefactura_print_view, name="impresion"),
]
