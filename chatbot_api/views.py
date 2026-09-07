import os
from functools import wraps
from django.conf import settings
from django.db import connection
from django.db.models import Q
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
from prefacturas_app.models_existing import MaestroArticulo, MaestroSn


def chatbot_token_required(view_func):
    """
    Decorador para autenticar peticiones mediante token estático de API.
    Acepta el token en la cabecera 'Authorization: Bearer <token>' o
    en el parámetro GET/POST 'api_key'.
    """
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        # Obtener el token configurado
        expected_token = os.getenv("CHATBOT_API_KEY") or getattr(settings, "CHATBOT_API_KEY", None)
        
        if not expected_token:
            return JsonResponse(
                {"error": "La API del chatbot no está configurada en el servidor (falta CHATBOT_API_KEY)"},
                status=500
            )

        # Buscar el token en los headers o query params
        auth_header = request.headers.get("Authorization")
        provided_token = request.GET.get("api_key") or request.POST.get("api_key")

        if auth_header and auth_header.startswith("Bearer "):
            provided_token = auth_header.split(" ")[1]

        if not provided_token or provided_token.strip() != expected_token.strip():
            return JsonResponse({"error": "No autorizado. Token inválido o ausente."}, status=401)

        return view_func(request, *args, **kwargs)

    return _wrapped_view


def _float(val):
    if val is None:
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


@require_http_methods(["GET"])
@chatbot_token_required
def buscar_articulos_chatbot(request):
    """
    Busca artículos por descripción, código o referencia.
    Retorna detalles del artículo y su stock consolidado del tarjetero.
    URL: /api/chatbot/articulos/?q=<termino_busqueda>
    """
    q = (request.GET.get("q") or "").strip()
    
    # Excluir artículos bloqueados para la venta/inventario
    qs = MaestroArticulo.objects.exclude(bloqueado__iexact="Y")
    
    if q:
        qs = qs.filter(
            Q(descrip_art__icontains=q) | 
            Q(id_articulo__icontains=q) | 
            Q(referencia__icontains=q)
        )
    
    # Limitar a los primeros 25 resultados para no sobrecargar el chat de WhatsApp
    articulos_data = list(qs.values(
        "id_articulo",
        "descrip_art",
        "referencia",
        "precio_det",
        "bloqueado"
    )[:25])

    results = []
    if articulos_data:
        articulo_ids = [row["id_articulo"] for row in articulos_data if row.get("id_articulo")]
        
        # Consultar el stock consolidado desde la tabla TARJETERO (Kardex)
        tarj_stock = {}
        if articulo_ids:
            with connection.cursor() as cursor:
                placeholders = ", ".join(["%s"] * len(articulo_ids))
                cursor.execute(
                    f"SELECT ID_ARTICULO, COALESCE(SUM(CANTIDAD), 0) FROM TARJETERO WHERE ID_ARTICULO IN ({placeholders}) GROUP BY ID_ARTICULO",
                    articulo_ids
                )
                tarj_stock = {str(row[0] or "").strip(): _float(row[1]) for row in cursor.fetchall()}

        for r in articulos_data:
            art_id = r.get("id_articulo") or ""
            stock_val = tarj_stock.get(art_id, 0.0)
            results.append({
                "id_articulo": art_id.strip(),
                "descrip_art": (r.get("descrip_art") or "").strip(),
                "referencia": (r.get("referencia") or "").strip(),
                "precio_det": _float(r.get("precio_det")),
                "stock": stock_val,
                "bloqueado": r.get("bloqueado") or "N"
            })

    return JsonResponse({"results": results})


@require_http_methods(["GET"])
@chatbot_token_required
def consultar_inventario_chatbot(request):
    """
    Consulta el inventario / stock específico para un código de artículo.
    URL: /api/chatbot/inventario/?id_articulo=<codigo_articulo>
    """
    id_articulo = (request.GET.get("id_articulo") or "").strip()
    
    if not id_articulo:
        return JsonResponse({"error": "Debe proporcionar el parámetro 'id_articulo'"}, status=400)

    try:
        articulo = MaestroArticulo.objects.get(id_articulo=id_articulo)
    except MaestroArticulo.DoesNotExist:
        # Intentar buscar por referencia
        articulo = MaestroArticulo.objects.filter(referencia=id_articulo).first()
        if not articulo:
            return JsonResponse({"error": f"Artículo con ID o referencia '{id_articulo}' no encontrado"}, status=404)

    # Consultar existencias consolidado en TARJETERO
    stock_val = 0.0
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT COALESCE(SUM(CANTIDAD), 0) FROM TARJETERO WHERE ID_ARTICULO = %s",
            [articulo.id_articulo]
        )
        row = cursor.fetchone()
        if row:
            stock_val = _float(row[0])

    return JsonResponse({
        "id_articulo": articulo.id_articulo.strip(),
        "descrip_art": (articulo.descrip_art or "").strip(),
        "referencia": (articulo.referencia or "").strip(),
        "precio_det": _float(articulo.precio_det),
        "stock": stock_val,
        "bloqueado": articulo.bloqueado or "N",
        "marca": (articulo.marca or "").strip(),
        "modelo": (articulo.modelo or "").strip()
    })


@require_http_methods(["GET"])
@chatbot_token_required
def consultar_cliente_chatbot(request):
    """
    Consulta los datos y estado de cuenta (saldo) de un cliente.
    Se puede buscar por:
    - telefono: Número de WhatsApp (búsqueda flexible por últimos dígitos)
    - rnc_ced: RNC o Cédula
    - id_sn: Código de cliente interno
    URL: /api/chatbot/clientes/?telefono=18091234567
    """
    telefono = (request.GET.get("telefono") or "").strip()
    rnc_ced = (request.GET.get("rnc_ced") or "").strip()
    id_sn = (request.GET.get("id_sn") or "").strip()

    if not any([telefono, rnc_ced, id_sn]):
        return JsonResponse(
            {"error": "Debe proporcionar al menos un criterio de búsqueda: 'telefono', 'rnc_ced' o 'id_sn'"},
            status=400
        )

    clientes = MaestroSn.objects.exclude(bloqueado__iexact="Y")

    if id_sn:
        clientes = clientes.filter(id_sn=id_sn)
    elif rnc_ced:
        clientes = clientes.filter(rnc_ced__icontains=rnc_ced)
    elif telefono:
        # Normalizar teléfono de búsqueda (dejar solo dígitos)
        telefono_limpio = "".join([c for c in telefono if c.isdigit()])
        if len(telefono_limpio) >= 7:
            # Buscar coincidencia flexible por los últimos 7 dígitos para evitar problemas de código de país
            ultimos_digitos = telefono_limpio[-7:]
            clientes = clientes.filter(
                Q(tel1__icontains=ultimos_digitos) | 
                Q(tel2__icontains=ultimos_digitos) | 
                Q(celular__icontains=ultimos_digitos)
            )
        else:
            # Si el teléfono ingresado es muy corto, buscar coincidencia directa
            clientes = clientes.filter(
                Q(tel1__icontains=telefono) | 
                Q(tel2__icontains=telefono) | 
                Q(celular__icontains=telefono)
            )

    # Limitar a los primeros 5 resultados por si hay homónimos o números compartidos
    resultados_clientes = list(clientes.values(
        "id_sn",
        "nom_socio",
        "rnc_ced",
        "tel1",
        "tel2",
        "celular",
        "email",
        "saldo",
        "lim_credito",
        "comentario",
        "bloqueado"
    )[:5])

    if not resultados_clientes:
        return JsonResponse({"error": "Cliente no encontrado con los criterios provistos"}, status=404)

    # Limpiar strings de la base de datos (quitar espacios sobrantes que SQL Server suele rellenar)
    for c in resultados_clientes:
        c["id_sn"] = (c["id_sn"] or "").strip()
        c["nom_socio"] = (c["nom_socio"] or "").strip()
        c["rnc_ced"] = (c["rnc_ced"] or "").strip()
        c["tel1"] = (c["tel1"] or "").strip()
        c["tel2"] = (c["tel2"] or "").strip()
        c["celular"] = (c["celular"] or "").strip()
        c["email"] = (c["email"] or "").strip()
        c["comentario"] = (c["comentario"] or "").strip()
        c["saldo"] = _float(c["saldo"])
        c["lim_credito"] = _float(c["lim_credito"])

    return JsonResponse({"results": resultados_clientes})
