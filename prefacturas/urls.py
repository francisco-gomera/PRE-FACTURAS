"""
URL configuration for prefacturas project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from django.urls import include, path, re_path
from core.error_views import (
    bad_request_view,
    media_file_view,
    offline_view,
    page_not_found_view,
    permission_denied_view,
    service_worker_view,
)

handler400 = "core.error_views.bad_request_view"
handler403 = "core.error_views.permission_denied_view"
handler404 = "core.error_views.page_not_found_view"
handler500 = "core.error_views.server_error_view"

urlpatterns = [
    path("sw.js", service_worker_view, name="service_worker"),
    re_path(r"^media/(?P<path>.*)$", media_file_view, name="media_file"),
    path("app-offline/", offline_view, name="app_offline"),
    path('', include('prefacturas_app.urls')),
    path('app/', include('core.urls')),
    path('app/prefacturas/', include('prefacturas_mod.urls')),
    path('app/clientes/', include('clientes_mod.urls')),
    path('app/inventario/', include('inventario.urls')),
    path('app/reportes/', include('reportes.urls')),
    path('app/etiquetas/', include('etiquetas.urls')),
    path('app/ajustes/', include('ajustes.urls')),
    path('app/cobros/', include('cobros.urls')),
    path('app/cartas/', include('cartas.urls')),
    path('app/factura/', include('factura.urls')),
    path('app/caja/', include('caja.urls')),
    path('app/chat-interno/', include('chat_interno.urls')),
    path('app/empleados/', include('empleados.urls')),
    path('api/chatbot/', include('chatbot_api.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += staticfiles_urlpatterns()
