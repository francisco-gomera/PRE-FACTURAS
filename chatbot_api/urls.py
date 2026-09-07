from django.urls import path
from .views import buscar_articulos_chatbot, consultar_inventario_chatbot, consultar_cliente_chatbot

app_name = "chatbot_api"

urlpatterns = [
    path("articulos/", buscar_articulos_chatbot, name="articulos"),
    path("inventario/", consultar_inventario_chatbot, name="inventario"),
    path("clientes/", consultar_cliente_chatbot, name="clientes"),
]
