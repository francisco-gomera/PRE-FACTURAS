from http.cookies import SimpleCookie

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.conf import settings
from django.core import signing
from django.db import IntegrityError
from django.utils import timezone

from ajustes.permissions import has_perm
from core.chat_presence import (
    get_online_user_ids,
    mark_user_connected,
    mark_user_disconnected,
    touch_user_connection,
)
from chat_interno.models import ChatMensaje, ChatMensajeLectura, ChatSalaMiembro
from prefacturas_app.views import AUTH_COOKIE_NAME

from .realtime import (
    CHAT_USER_GROUP_PREFIX,
    CXC_GROUP_NAME,
    FINANCIAMIENTO_GROUP_NAME,
    INVENTARIO_SOLICITUDES_GROUP_NAME,
    NOTIFICATION_GROUP_NAME,
    PREFACTURA_GROUP_NAME,
)


class NotificationConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        auth_payload = await self._load_auth_payload()
        if not auth_payload:
            await self.close(code=4401)
            return

        usuario_id = auth_payload.get("usuario_id")
        can_view_notifications = await self._can_view_notifications(usuario_id)
        self.notification_group_name = NOTIFICATION_GROUP_NAME if can_view_notifications else ""
        if self.notification_group_name:
            await self.channel_layer.group_add(self.notification_group_name, self.channel_name)
        await self.accept()
        await self.send_json({"type": "notification.ready"})

    async def disconnect(self, close_code):
        group_name = getattr(self, "notification_group_name", "")
        if group_name:
            await self.channel_layer.group_discard(group_name, self.channel_name)

    async def receive_json(self, content, **kwargs):
        action = str((content or {}).get("action") or "").strip().lower()
        if action == "ping":
            await self.send_json({"type": "notification.pong"})

    async def notification_refresh(self, event):
        await self.send_json(
            {
                "type": "notification.refresh",
                "reason": str((event or {}).get("reason") or "updated").strip() or "updated",
                "event_id": str((event or {}).get("event_id") or "").strip(),
            }
        )

    async def _load_auth_payload(self):
        cookie_header = ""
        for key, value in self.scope.get("headers", []):
            if key == b"cookie":
                cookie_header = value.decode("utf-8", errors="ignore")
                break
        if not cookie_header:
            return None
        cookies = SimpleCookie()
        try:
            cookies.load(cookie_header)
        except Exception:
            return None
        token = cookies.get(AUTH_COOKIE_NAME)
        if not token or not token.value:
            return None
        return await self._decode_token(token.value)

    @database_sync_to_async
    def _decode_token(self, token):
        try:
            return signing.loads(
                token,
                max_age=getattr(settings, "AUTH_COOKIE_MAX_AGE", 60 * 60 * 24 * 365 * 5),
            )
        except signing.BadSignature:
            return None

    @database_sync_to_async
    def _can_view_notifications(self, usuario_id):
        return bool(
            has_perm(usuario_id, "inventario", "ver_solicitudes_existencia")
            or has_perm(usuario_id, "cobros", "ver_acuerdos")
        )


class InventarioSolicitudesConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        auth_payload = await self._load_auth_payload()
        if not auth_payload:
            await self.close(code=4401)
            return

        usuario_id = auth_payload.get("usuario_id")
        can_view = await self._can_view_inventario_solicitudes(usuario_id)
        self.inventory_group_name = INVENTARIO_SOLICITUDES_GROUP_NAME if can_view else ""
        if self.inventory_group_name:
            await self.channel_layer.group_add(self.inventory_group_name, self.channel_name)
        await self.accept()
        await self.send_json({"type": "inventario.solicitudes.ready"})

    async def disconnect(self, close_code):
        group_name = getattr(self, "inventory_group_name", "")
        if group_name:
            await self.channel_layer.group_discard(group_name, self.channel_name)

    async def receive_json(self, content, **kwargs):
        action = str((content or {}).get("action") or "").strip().lower()
        if action == "ping":
            await self.send_json({"type": "inventario.solicitudes.pong"})

    async def inventario_solicitudes_refresh(self, event):
        await self.send_json(
            {
                "type": "inventario.solicitudes.refresh",
                "reason": str((event or {}).get("reason") or "updated").strip() or "updated",
            }
        )

    async def _load_auth_payload(self):
        cookie_header = ""
        for key, value in self.scope.get("headers", []):
            if key == b"cookie":
                cookie_header = value.decode("utf-8", errors="ignore")
                break
        if not cookie_header:
            return None
        cookies = SimpleCookie()
        try:
            cookies.load(cookie_header)
        except Exception:
            return None
        token = cookies.get(AUTH_COOKIE_NAME)
        if not token or not token.value:
            return None
        return await self._decode_token(token.value)

    @database_sync_to_async
    def _decode_token(self, token):
        try:
            return signing.loads(
                token,
                max_age=getattr(settings, "AUTH_COOKIE_MAX_AGE", 60 * 60 * 24 * 365 * 5),
            )
        except signing.BadSignature:
            return None

    @database_sync_to_async
    def _can_view_inventario_solicitudes(self, usuario_id):
        return bool(has_perm(usuario_id, "inventario", "ver_solicitudes_existencia"))


class PrefacturaConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        auth_payload = await self._load_auth_payload()
        if not auth_payload:
            await self.close(code=4401)
            return

        usuario_id = auth_payload.get("usuario_id")
        can_view_prefacturas = await self._can_view_prefacturas(usuario_id)
        self.prefactura_group_name = PREFACTURA_GROUP_NAME if can_view_prefacturas else ""
        if self.prefactura_group_name:
            await self.channel_layer.group_add(self.prefactura_group_name, self.channel_name)
        await self.accept()
        await self.send_json({"type": "prefactura.ready"})

    async def disconnect(self, close_code):
        group_name = getattr(self, "prefactura_group_name", "")
        if group_name:
            await self.channel_layer.group_discard(group_name, self.channel_name)

    async def receive_json(self, content, **kwargs):
        action = str((content or {}).get("action") or "").strip().lower()
        if action == "ping":
            await self.send_json({"type": "prefactura.pong"})

    async def prefactura_refresh(self, event):
        await self.send_json(
            {
                "type": "prefactura.refresh",
                "reason": str((event or {}).get("reason") or "updated").strip() or "updated",
                "event_id": str((event or {}).get("event_id") or "").strip(),
            }
        )

    async def prefactura_document_status(self, event):
        await self.send_json(
            {
                "type": "prefactura.document_status",
                "document_id": str((event or {}).get("document_id") or "").strip(),
                "estado": str((event or {}).get("estado") or "").strip(),
                "reason": str((event or {}).get("reason") or "updated").strip() or "updated",
                "event_id": str((event or {}).get("event_id") or "").strip(),
            }
        )

    async def factura_document_status(self, event):
        await self.send_json(
            {
                "type": "factura.document_status",
                "document_id": str((event or {}).get("document_id") or "").strip(),
                "estado": str((event or {}).get("estado") or "").strip(),
                "reason": str((event or {}).get("reason") or "updated").strip() or "updated",
                "event_id": str((event or {}).get("event_id") or "").strip(),
            }
        )

    async def _load_auth_payload(self):
        cookie_header = ""
        for key, value in self.scope.get("headers", []):
            if key == b"cookie":
                cookie_header = value.decode("utf-8", errors="ignore")
                break
        if not cookie_header:
            return None
        cookies = SimpleCookie()
        try:
            cookies.load(cookie_header)
        except Exception:
            return None
        token = cookies.get(AUTH_COOKIE_NAME)
        if not token or not token.value:
            return None
        return await self._decode_token(token.value)

    @database_sync_to_async
    def _decode_token(self, token):
        try:
            return signing.loads(
                token,
                max_age=getattr(settings, "AUTH_COOKIE_MAX_AGE", 60 * 60 * 24 * 365 * 5),
            )
        except signing.BadSignature:
            return None

    @database_sync_to_async
    def _can_view_prefacturas(self, usuario_id):
        return bool(
            has_perm(usuario_id, "factura", "ver_documentos")
            or has_perm(usuario_id, "factura", "ver_emision")
            or has_perm(usuario_id, "prefacturas", "ver")
            or has_perm(usuario_id, "prefacturas", "guardar")
            or has_perm(usuario_id, "prefacturas", "cerrar")
            or has_perm(usuario_id, "prefacturas", "cancelar")
        )


class CxcConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        auth_payload = await self._load_auth_payload()
        if not auth_payload:
            await self.close(code=4401)
            return

        usuario_id = auth_payload.get("usuario_id")
        can_view_cxc = await self._can_view_cxc(usuario_id)
        self.cxc_group_name = CXC_GROUP_NAME if can_view_cxc else ""
        if self.cxc_group_name:
            await self.channel_layer.group_add(self.cxc_group_name, self.channel_name)
        await self.accept()
        await self.send_json({"type": "cxc.ready"})

    async def disconnect(self, close_code):
        group_name = getattr(self, "cxc_group_name", "")
        if group_name:
            await self.channel_layer.group_discard(group_name, self.channel_name)

    async def receive_json(self, content, **kwargs):
        action = str((content or {}).get("action") or "").strip().lower()
        if action == "ping":
            await self.send_json({"type": "cxc.pong"})

    async def cxc_document_status(self, event):
        await self.send_json(
            {
                "type": "cxc.document_status",
                "document_id": str((event or {}).get("document_id") or "").strip(),
                "no_recibo": str((event or {}).get("no_recibo") or "").strip(),
                "estado": str((event or {}).get("estado") or "").strip(),
                "reason": str((event or {}).get("reason") or "updated").strip() or "updated",
                "event_id": str((event or {}).get("event_id") or "").strip(),
            }
        )

    async def _load_auth_payload(self):
        cookie_header = ""
        for key, value in self.scope.get("headers", []):
            if key == b"cookie":
                cookie_header = value.decode("utf-8", errors="ignore")
                break
        if not cookie_header:
            return None
        cookies = SimpleCookie()
        try:
            cookies.load(cookie_header)
        except Exception:
            return None
        token = cookies.get(AUTH_COOKIE_NAME)
        if not token or not token.value:
            return None
        return await self._decode_token(token.value)

    @database_sync_to_async
    def _decode_token(self, token):
        try:
            return signing.loads(
                token,
                max_age=getattr(settings, "AUTH_COOKIE_MAX_AGE", 60 * 60 * 24 * 365 * 5),
            )
        except signing.BadSignature:
            return None

    @database_sync_to_async
    def _can_view_cxc(self, usuario_id):
        return bool(has_perm(usuario_id, "caja", "ver_cuentas_por_cobrar"))


class FinanciamientoConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        auth_payload = await self._load_auth_payload()
        if not auth_payload:
            await self.close(code=4401)
            return

        usuario_id = auth_payload.get("usuario_id")
        can_view_financiamiento = await self._can_view_financiamiento(usuario_id)
        self.financiamiento_group_name = FINANCIAMIENTO_GROUP_NAME if can_view_financiamiento else ""
        if self.financiamiento_group_name:
            await self.channel_layer.group_add(self.financiamiento_group_name, self.channel_name)
        await self.accept()
        await self.send_json({"type": "financiamiento.ready"})

    async def disconnect(self, close_code):
        group_name = getattr(self, "financiamiento_group_name", "")
        if group_name:
            await self.channel_layer.group_discard(group_name, self.channel_name)

    async def receive_json(self, content, **kwargs):
        action = str((content or {}).get("action") or "").strip().lower()
        if action == "ping":
            await self.send_json({"type": "financiamiento.pong"})

    async def financiamiento_document_status(self, event):
        await self.send_json(
            {
                "type": "financiamiento.document_status",
                "document_id": str((event or {}).get("document_id") or "").strip(),
                "factura_no": str((event or {}).get("factura_no") or "").strip(),
                "estado": str((event or {}).get("estado") or "").strip(),
                "reason": str((event or {}).get("reason") or "updated").strip() or "updated",
                "event_id": str((event or {}).get("event_id") or "").strip(),
            }
        )

    async def _load_auth_payload(self):
        cookie_header = ""
        for key, value in self.scope.get("headers", []):
            if key == b"cookie":
                cookie_header = value.decode("utf-8", errors="ignore")
                break
        if not cookie_header:
            return None
        cookies = SimpleCookie()
        try:
            cookies.load(cookie_header)
        except Exception:
            return None
        token = cookies.get(AUTH_COOKIE_NAME)
        if not token or not token.value:
            return None
        return await self._decode_token(token.value)

    @database_sync_to_async
    def _decode_token(self, token):
        try:
            return signing.loads(
                token,
                max_age=getattr(settings, "AUTH_COOKIE_MAX_AGE", 60 * 60 * 24 * 365 * 5),
            )
        except signing.BadSignature:
            return None

    @database_sync_to_async
    def _can_view_financiamiento(self, usuario_id):
        return bool(has_perm(usuario_id, "caja", "ver_financiamiento"))


class ChatInternoConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        auth_payload = await self._load_auth_payload()
        if not auth_payload:
            await self.close(code=4401)
            return

        usuario_id = auth_payload.get("usuario_id")
        self.chat_user_id = int(usuario_id or 0) if str(usuario_id or "").strip() else 0
        self.chat_user_name = str(auth_payload.get("usuario_nombre") or "").strip()
        can_view_chat = await self._can_view_chat(usuario_id)
        user_key = str(usuario_id or "").strip()
        self.chat_group_name = f"{CHAT_USER_GROUP_PREFIX}.{user_key}" if can_view_chat and user_key else ""
        if self.chat_group_name:
            await self.channel_layer.group_add(self.chat_group_name, self.channel_name)
        await self.accept()
        if self.chat_user_id > 0:
            mark_user_connected(self.chat_user_id, self.channel_name)
        await self.send_json({"type": "chat.ready"})
        await self.send_json({"type": "chat.presence_snapshot", "online_user_ids": get_online_user_ids()})
        await self._broadcast_presence_change(True)

    async def disconnect(self, close_code):
        group_name = getattr(self, "chat_group_name", "")
        user_id = int(getattr(self, "chat_user_id", 0) or 0)
        remaining = 0
        if user_id > 0:
            remaining = mark_user_disconnected(user_id, self.channel_name)
        if user_id > 0 and remaining <= 0:
            await self._broadcast_presence_change(False)
        if group_name:
            await self.channel_layer.group_discard(group_name, self.channel_name)

    async def receive_json(self, content, **kwargs):
        action = str((content or {}).get("action") or "").strip().lower()
        if action == "ping":
            if self.chat_user_id > 0:
                touch_user_connection(self.chat_user_id, self.channel_name)
            await self.send_json({"type": "chat.pong"})
            return
        if action == "typing":
            room_id = int((content or {}).get("room_id") or 0)
            is_typing = bool((content or {}).get("is_typing"))
            member_ids = await self._get_chat_room_member_ids(room_id, self.chat_user_id)
            if not member_ids:
                return
            event_payload = {
                "type": "chat.typing",
                "room_id": room_id,
                "id_usuario": int(self.chat_user_id or 0),
                "usuario_nombre": str(self.chat_user_name or "").strip(),
                "is_typing": is_typing,
            }
            for member_id in member_ids:
                if int(member_id or 0) == int(self.chat_user_id or 0):
                    continue
                await self.channel_layer.group_send(
                    f"{CHAT_USER_GROUP_PREFIX}.{member_id}",
                    event_payload,
                )
            return
        if action == "read":
            room_id = int((content or {}).get("room_id") or 0)
            message_id = int((content or {}).get("message_id") or 0)
            if room_id <= 0 or message_id <= 0:
                return
            read_result = await self._mark_room_messages_read(
                room_id=room_id,
                reader_id=self.chat_user_id,
                until_message_id=message_id,
            )
            member_ids = read_result.get("member_ids") or []
            max_read_message_id = int(read_result.get("max_read_message_id") or 0)
            if not member_ids or max_read_message_id <= 0:
                return
            event_payload = {
                "type": "chat.read",
                "room_id": room_id,
                "id_usuario": int(self.chat_user_id or 0),
                "usuario_nombre": str(self.chat_user_name or "").strip(),
                "message_id": max_read_message_id,
            }
            for member_id in member_ids:
                if int(member_id or 0) == int(self.chat_user_id or 0):
                    continue
                await self.channel_layer.group_send(
                    f"{CHAT_USER_GROUP_PREFIX}.{member_id}",
                    event_payload,
                )
            return

    async def chat_message(self, event):
        await self.send_json(
            {
                "type": "chat.message",
                "message": (event or {}).get("message") or {},
                "room": (event or {}).get("room") or {},
            }
        )

    async def chat_room(self, event):
        await self.send_json(
            {
                "type": "chat.room",
                "room": (event or {}).get("room") or {},
            }
        )

    async def chat_typing(self, event):
        await self.send_json(
            {
                "type": "chat.typing",
                "room_id": int((event or {}).get("room_id") or 0),
                "id_usuario": int((event or {}).get("id_usuario") or 0),
                "usuario_nombre": str((event or {}).get("usuario_nombre") or "").strip(),
                "is_typing": bool((event or {}).get("is_typing")),
            }
        )

    async def chat_read(self, event):
        await self.send_json(
            {
                "type": "chat.read",
                "room_id": int((event or {}).get("room_id") or 0),
                "id_usuario": int((event or {}).get("id_usuario") or 0),
                "usuario_nombre": str((event or {}).get("usuario_nombre") or "").strip(),
                "message_id": int((event or {}).get("message_id") or 0),
            }
        )

    async def chat_presence(self, event):
        await self.send_json(
            {
                "type": "chat.presence",
                "id_usuario": int((event or {}).get("id_usuario") or 0),
                "is_online": bool((event or {}).get("is_online")),
            }
        )

    async def _load_auth_payload(self):
        cookie_header = ""
        for key, value in self.scope.get("headers", []):
            if key == b"cookie":
                cookie_header = value.decode("utf-8", errors="ignore")
                break
        if not cookie_header:
            return None
        cookies = SimpleCookie()
        try:
            cookies.load(cookie_header)
        except Exception:
            return None
        token = cookies.get(AUTH_COOKIE_NAME)
        if not token or not token.value:
            return None
        return await self._decode_token(token.value)

    @database_sync_to_async
    def _decode_token(self, token):
        try:
            return signing.loads(
                token,
                max_age=getattr(settings, "AUTH_COOKIE_MAX_AGE", 60 * 60 * 24 * 365 * 5),
            )
        except signing.BadSignature:
            return None

    @database_sync_to_async
    def _can_view_chat(self, usuario_id):
        return bool(has_perm(usuario_id, "chat_interno", "ver"))

    @database_sync_to_async
    def _get_chat_room_member_ids(self, room_id, user_id):
        rid = int(room_id or 0)
        uid = int(user_id or 0)
        if rid <= 0 or uid <= 0:
            return []
        is_member = ChatSalaMiembro.objects.filter(sala_id=rid, id_usuario=uid, activo=True).exists()
        if not is_member:
            return []
        return list(
            ChatSalaMiembro.objects.filter(sala_id=rid, activo=True).values_list("id_usuario", flat=True)
        )

    @database_sync_to_async
    def _get_related_chat_user_ids(self, user_id):
        uid = int(user_id or 0)
        if uid <= 0:
            return []
        room_ids = list(
            ChatSalaMiembro.objects.filter(id_usuario=uid, activo=True).values_list("sala_id", flat=True)
        )
        if not room_ids:
            return []
        return list(
            ChatSalaMiembro.objects.filter(sala_id__in=room_ids, activo=True)
            .exclude(id_usuario=uid)
            .values_list("id_usuario", flat=True)
            .distinct()
        )

    @database_sync_to_async
    def _mark_room_messages_read(self, *, room_id, reader_id, until_message_id):
        rid = int(room_id or 0)
        uid = int(reader_id or 0)
        mid = int(until_message_id or 0)
        if rid <= 0 or uid <= 0 or mid <= 0:
            return {"member_ids": [], "max_read_message_id": 0}
        is_member = ChatSalaMiembro.objects.filter(sala_id=rid, id_usuario=uid, activo=True).exists()
        if not is_member:
            return {"member_ids": [], "max_read_message_id": 0}

        target_ids = list(
            ChatMensaje.objects.filter(sala_id=rid, id_mensaje__lte=mid)
            .exclude(id_usuario=uid)
            .values_list("id_mensaje", flat=True)
        )
        if not target_ids:
            member_ids = list(ChatSalaMiembro.objects.filter(sala_id=rid, activo=True).values_list("id_usuario", flat=True))
            return {"member_ids": member_ids, "max_read_message_id": mid}

        existing_ids = set(
            ChatMensajeLectura.objects.filter(mensaje_id__in=target_ids, id_usuario=uid).values_list("mensaje_id", flat=True)
        )
        pending = [
            ChatMensajeLectura(
                mensaje_id=msg_id,
                sala_id=rid,
                id_usuario=uid,
                leido_en=timezone.now(),
            )
            for msg_id in target_ids
            if msg_id not in existing_ids
        ]
        if pending:
            for lectura in pending:
                try:
                    ChatMensajeLectura.objects.create(
                        mensaje_id=lectura.mensaje_id,
                        sala_id=lectura.sala_id,
                        id_usuario=lectura.id_usuario,
                        leido_en=timezone.now(),
                    )
                except IntegrityError:
                    # Otra terminal pudo registrar la misma lectura casi al mismo tiempo.
                    pass
        member_ids = list(ChatSalaMiembro.objects.filter(sala_id=rid, activo=True).values_list("id_usuario", flat=True))
        return {"member_ids": member_ids, "max_read_message_id": max(target_ids)}

    async def _broadcast_presence_change(self, is_online):
        user_id = int(getattr(self, "chat_user_id", 0) or 0)
        if user_id <= 0:
            return
        member_ids = await self._get_related_chat_user_ids(user_id)
        if not member_ids:
            return
        event_payload = {
            "type": "chat.presence",
            "id_usuario": user_id,
            "is_online": bool(is_online),
        }
        for member_id in member_ids:
            await self.channel_layer.group_send(
                f"{CHAT_USER_GROUP_PREFIX}.{member_id}",
                event_payload,
            )
