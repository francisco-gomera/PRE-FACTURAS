from django.urls import path

from .views import (
    index,
    usuarios_view,
    parametros_view,
    facturacion_electronica_view,
    guardar_parametros_view,
    crear_modulo_view,
    crear_permiso_view,
    crear_rol_view,
    asignar_rol_view,
    asignar_permiso_rol_view,
    asignar_permiso_usuario_view,
    guardar_permisos_usuario_view,
    guardar_firma_usuario_view,
)

app_name = "ajustes"

urlpatterns = [
    path("", index, name="index"),
    path("usuarios/", usuarios_view, name="usuarios"),
    path("usuarios/modulos/crear/", crear_modulo_view, name="crear_modulo"),
    path("usuarios/permisos/crear/", crear_permiso_view, name="crear_permiso"),
    path("usuarios/roles/crear/", crear_rol_view, name="crear_rol"),
    path("usuarios/roles/asignar/", asignar_rol_view, name="asignar_rol"),
    path("usuarios/roles/permisos/asignar/", asignar_permiso_rol_view, name="asignar_permiso_rol"),
    path("usuarios/permisos/asignar/", asignar_permiso_usuario_view, name="asignar_permiso_usuario"),
    path("usuarios/permisos/guardar/", guardar_permisos_usuario_view, name="guardar_permisos_usuario"),
    path("usuarios/firma/guardar/", guardar_firma_usuario_view, name="guardar_firma_usuario"),
    path("parametros/", parametros_view, name="parametros"),
    path("parametros/guardar/", guardar_parametros_view, name="guardar_parametros"),
    path("integraciones/facturacion-electronica/", facturacion_electronica_view, name="facturacion_electronica"),
]
