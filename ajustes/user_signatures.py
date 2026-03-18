from django.db import connection, transaction

from .models import UsuarioFirma


def get_user_signature_bytes(id_usuario):
    try:
        id_usuario = int(id_usuario)
    except (TypeError, ValueError):
        return b""

    try:
        registro = UsuarioFirma.objects.filter(id_usuario=id_usuario).only("firma").first()
        if registro and registro.firma:
            return bytes(registro.firma)
    except Exception:
        return b""
    return b""


def get_users_with_signatures():
    try:
        return {
            int(item)
            for item in UsuarioFirma.objects.exclude(firma__isnull=True).values_list("id_usuario", flat=True)
        }
    except Exception:
        return set()


def save_user_signature(id_usuario, firma_bytes):
    try:
        id_usuario = int(id_usuario)
    except (TypeError, ValueError):
        return None

    if not firma_bytes:
        return None

    try:
        with transaction.atomic():
            registro, _ = UsuarioFirma.objects.update_or_create(
                id_usuario=id_usuario,
                defaults={"firma": firma_bytes},
            )
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE USUARIO SET ID_FIRMA = %s WHERE ID_USUARIO = %s",
                    [int(registro.id_firma), id_usuario],
                )
            return registro
    except Exception:
        return None
