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
    api_clientes_sector_view,
    registros_pagos_impresion_view,
    registros_pagos_view,
    pagos_masivos_view,
    api_pagos_masivos_procesar_view,
    pagos_masivos_impresion_lote_view,
    pagos_masivos_pdf_lote_view,
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
    path("registros-pagos/", registros_pagos_view, name="registros_pagos"),
    path("registros-pagos/<int:id_cobrador>/", registros_pagos_view, name="registros_pagos_cobrador"),
    path("registros-pagos/<int:id_cobrador>/impresion/", registros_pagos_impresion_view, name="registros_pagos_impresion"),
    path("registros-pagos/api/clientes-sector/", api_clientes_sector_view, name="registros_pagos_api_clientes_sector"),
    path("pagos-masivos/", pagos_masivos_view, name="pagos_masivos"),
    path("pagos-masivos/<int:id_cobrador>/", pagos_masivos_view, name="pagos_masivos_cobrador"),
    path("pagos-masivos/api/procesar/", api_pagos_masivos_procesar_view, name="pagos_masivos_procesar"),
    path("pagos-masivos/impresion-lote/", pagos_masivos_impresion_lote_view, name="pagos_masivos_impresion_lote"),
    path("pagos-masivos/pdf-lote/", pagos_masivos_pdf_lote_view, name="pagos_masivos_pdf_lote"),
]

