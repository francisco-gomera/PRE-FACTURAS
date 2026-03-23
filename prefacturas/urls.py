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
from django.urls import include, path

urlpatterns = [
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
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
