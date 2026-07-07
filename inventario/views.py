import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import socket

from django.db import connection, transaction
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods

from ajustes.permissions import has_perm
from core.realtime import broadcast_inventario_solicitudes_refresh, broadcast_notification_refresh
from core.views import _base_context, render_denied
from inventario.models import SolicitudExistencia
from cobros.models import CobroAcuerdo
from prefacturas_app.models_existing import MaestroArticulo
from prefacturas_app.views import _get_auth_payload, _require_perm_json


def _fmt_date_input(value):
    if not value:
        return ""
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    text = str(value).strip()
    if not text:
        return ""
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(text[:26], fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return text[:10] if len(text) >= 10 else ""


def _fmt_date_flexible(value):
    if not value:
        return ""
    if hasattr(value, "strftime"):
        return value.strftime("%d/%m/%Y")
    text = str(value).strip()
    if not text:
        return ""
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(text[:26], fmt).strftime("%d/%m/%Y")
        except ValueError:
            continue
    return text


def _to_decimal(value, default=Decimal("0")):
    try:
        if value is None:
            return default
        text = str(value).strip().replace(",", "")
        if not text:
            return default
        return Decimal(text)
    except (InvalidOperation, TypeError, ValueError):
        return default


def _format_money(value):
    return f"{_to_decimal(value):,.2f}"


def _notification_sort_timestamp(value):
    if not value:
        return ""
    if hasattr(value, "hour") and hasattr(value, "minute"):
        try:
            return timezone.localtime(value).isoformat()
        except Exception:
            return value.isoformat() if hasattr(value, "isoformat") else str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value).strip()


def _notification_id_sort_value(value):
    text = str(value or "").strip()
    try:
        return (1, int(text))
    except (TypeError, ValueError):
        return (0, text)


def _stringify_doc(value):
    if value is None:
        return ""
    text = str(value).strip()
    if text.endswith(".0"):
        return text[:-2]
    return text


def _normalize_terminal_name(value):
    text = " ".join(str(value or "").split()).strip()
    if not text:
        return ""
    return text[:50]


def _resolve_request_terminal(request, payload=None):
    payload = payload if isinstance(payload, dict) else {}
    for candidate in (
        payload.get("terminal_cliente"),
        payload.get("terminal"),
        request.headers.get("X-Client-Terminal"),
        request.META.get("HTTP_X_CLIENT_TERMINAL"),
    ):
        terminal_name = _normalize_terminal_name(candidate)
        if terminal_name:
            return terminal_name
    return _normalize_terminal_name(socket.gethostname()) or "SERVIDOR"


def _to_int(value, default=0):
    try:
        return int(str(value or "").strip())
    except Exception:
        return default


def _parse_solicitud_existencia_items(raw_value):
    payload = raw_value
    if isinstance(raw_value, str):
        try:
            payload = json.loads(raw_value)
        except Exception:
            payload = []
    if isinstance(payload, dict):
        payload = payload.get("items") or payload.get("detalles") or []
    if not isinstance(payload, list):
        return []
    items = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        articulo_id = str(item.get("articulo_id") or item.get("id_articulo") or "").strip()
        descripcion = str(item.get("descripcion") or item.get("descrip_art") or "").strip()
        cantidad_faltante = _to_decimal(item.get("cantidad_faltante"), Decimal("0"))
        cantidad_solicitada = _to_decimal(item.get("cantidad_solicitada"), Decimal("0"))
        cantidad_disponible = _to_decimal(item.get("cantidad_disponible"), Decimal("0"))
        if cantidad_faltante <= 0:
            cantidad_faltante = cantidad_solicitada - cantidad_disponible
        if cantidad_faltante <= 0:
            continue
        items.append(
            {
                "articulo_id": articulo_id,
                "descripcion": descripcion,
                "cantidad_solicitada": cantidad_solicitada,
                "cantidad_disponible": cantidad_disponible,
                "cantidad_faltante": cantidad_faltante,
                "uom": str(item.get("uom") or item.get("um_inv") or "").strip(),
                "alm": _stringify_doc(item.get("alm") or item.get("alm_dft") or ""),
                "ceco": str(item.get("ceco") or "").strip(),
                "cta_aum_stock": str(item.get("cta_aum_stock") or item.get("cuenta_mayor") or "").strip(),
            }
        )
    return items


def _parse_solicitud_existencia_payload(raw_value):
    payload = raw_value
    if isinstance(raw_value, str):
        try:
            payload = json.loads(raw_value)
        except Exception:
            payload = {}
    return payload if isinstance(payload, dict) else {}


def _serialize_solicitud_existencia(solicitud):
    payload = _parse_solicitud_existencia_payload(solicitud.detalle_json)
    items = _parse_solicitud_existencia_items(payload)
    cliente_codigo = str(solicitud.cliente_codigo or "").strip()
    cliente_nombre = str(solicitud.cliente_nombre or "").strip()
    referencia = str(solicitud.origen_referencia or "").strip()
    resumen_items = ", ".join(
        [
            f"{item.get('articulo_id') or item.get('descripcion')}: {_format_money(item.get('cantidad_faltante'))}"
            for item in items[:3]
        ]
    )
    if len(items) > 3:
        resumen_items = f"{resumen_items} +{len(items) - 3} mas".strip()
    cliente_label = " - ".join([piece for piece in [cliente_codigo, cliente_nombre] if piece])
    return {
        "id": solicitud.id_solicitud,
        "notification_type": "stock_request",
        "delete_mode": "server",
        "titulo": "Pedido de existencia",
        "cliente": cliente_label or cliente_nombre or cliente_codigo,
        "customer_name": cliente_nombre or cliente_label or cliente_codigo,
        "customer_code": cliente_codigo,
        "referencia": referencia,
        "reference": referencia,
        "resumen": resumen_items,
        "items_count": len(items),
        "item_count": len(items),
        "creado_en": timezone.localtime(solicitud.creado_en).strftime("%d/%m/%Y %I:%M %p") if solicitud.creado_en else "",
        "created_at": timezone.localtime(solicitud.creado_en).strftime("%d/%m/%Y %I:%M %p") if solicitud.creado_en else "",
        "sort_timestamp": _notification_sort_timestamp(solicitud.creado_en),
        "created_by_name": str(solicitud.creada_por_nombre or "").strip(),
        "created_by_login": str(solicitud.creada_por_login or "").strip(),
        "origin_module": str(solicitud.origen_modulo or "").strip(),
        "comment": str(solicitud.comentario or "").strip(),
        "items": [
            {
                "articulo_id": item.get("articulo_id") or "",
                "descripcion": item.get("descripcion") or "",
                "cantidad_faltante": _format_money(item.get("cantidad_faltante")),
                "uom": item.get("uom") or "",
                "alm": item.get("alm") or "",
            }
            for item in items
        ],
        "url": reverse("inventario:entrada_articulos") + f"?solicitud={solicitud.id_solicitud}",
        "origin_terminal": _normalize_terminal_name(payload.get("origin_terminal") or ""),
    }


def _mark_solicitud_existencia_atendida(solicitud, *, usuario_id, usuario_nombre):
    solicitud.atendida = True
    solicitud.atendida_por_id = usuario_id
    solicitud.atendida_por_nombre = str(usuario_nombre or "").strip() or None
    solicitud.atendida_en = timezone.localtime()
    solicitud.save(
        update_fields=[
            "atendida",
            "atendida_por_id",
            "atendida_por_nombre",
            "atendida_en",
            "actualizado_en",
        ]
    )


def _ensure_cobro_acuerdo_table():
    with connection.cursor() as cursor:
        cursor.execute(
            """
            IF OBJECT_ID('COBRO_ACUERDO', 'U') IS NULL
            BEGIN
                CREATE TABLE COBRO_ACUERDO (
                    ID_ACUERDO INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
                    ID_SN NVARCHAR(20) NOT NULL,
                    CLIENTE_NOMBRE NVARCHAR(200) NOT NULL,
                    TELEFONO NVARCHAR(50) NOT NULL CONSTRAINT DF_COBRO_ACUERDO_TELEFONO DEFAULT (''),
                    SECTOR NVARCHAR(120) NOT NULL CONSTRAINT DF_COBRO_ACUERDO_SECTOR DEFAULT (''),
                    TIPO NVARCHAR(30) NOT NULL CONSTRAINT DF_COBRO_ACUERDO_TIPO DEFAULT ('PROMESA_PAGO'),
                    FECHA_COMPROMISO DATE NULL,
                    MONTO_COMPROMISO DECIMAL(19,2) NULL,
                    NOTA NVARCHAR(MAX) NOT NULL,
                    ESTADO NVARCHAR(20) NOT NULL CONSTRAINT DF_COBRO_ACUERDO_ESTADO DEFAULT ('PENDIENTE'),
                    CREADO_POR_ID BIGINT NOT NULL,
                    FECHA_CREACION DATETIME2 NOT NULL CONSTRAINT DF_COBRO_ACUERDO_FECHA_CREACION DEFAULT (SYSDATETIME()),
                    FECHA_MODIFICACION DATETIME2 NOT NULL CONSTRAINT DF_COBRO_ACUERDO_FECHA_MODIFICACION DEFAULT (SYSDATETIME())
                );
            END
            """
        )


def _serialize_acuerdo_notification(acuerdo, today):
    compromiso = getattr(acuerdo, "fecha_compromiso", None)
    if not compromiso:
        return None
    days_delta = (today - compromiso).days
    cliente_codigo = str(getattr(acuerdo, "id_sn", "") or "").strip()
    cliente_nombre = str(getattr(acuerdo, "cliente_nombre", "") or "").strip()
    telefono = str(getattr(acuerdo, "telefono", "") or "").strip()
    cliente_label = " - ".join([piece for piece in [cliente_codigo, cliente_nombre] if piece])
    estado_label = "Acuerdo vence hoy" if days_delta == 0 else f"Acuerdo vencido ({days_delta} dia(s))"
    resumen = str(getattr(acuerdo, "nota", "") or "").strip()
    if not resumen:
        resumen = "Seguimiento de acuerdo de pago pendiente."
    tipo = str(getattr(acuerdo, "tipo", "") or "").strip().replace("_", " ").title()
    referencia = tipo or "Cobros"
    if telefono:
        referencia = " - ".join([piece for piece in [referencia, telefono] if piece])
    return {
        "id": getattr(acuerdo, "id_acuerdo", ""),
        "notification_type": "payment_agreement",
        "delete_mode": "server",
        "titulo": estado_label,
        "cliente": cliente_label or cliente_nombre or cliente_codigo or "Sin cliente",
        "referencia": referencia,
        "resumen": resumen,
        "items_count": 1,
        "creado_en": compromiso.strftime("%d/%m/%Y"),
        "sort_timestamp": _notification_sort_timestamp(getattr(acuerdo, "fecha_creacion", None) or compromiso),
        "url": reverse("cobros:acuerdos") + f"?edit={getattr(acuerdo, 'id_acuerdo', '')}",
        "priority": 120 if days_delta > 0 else 110,
    }


def _load_acuerdo_notifications(limit=12):
    try:
        _ensure_cobro_acuerdo_table()
        today = timezone.localdate()
        acuerdos = list(
            CobroAcuerdo.objects.filter(estado="PENDIENTE", fecha_compromiso__isnull=False, fecha_compromiso__lte=today)
            .order_by("fecha_compromiso", "-fecha_creacion")[:limit]
        )
    except Exception:
        return []
    notifications = []
    for acuerdo in acuerdos:
        item = _serialize_acuerdo_notification(acuerdo, today)
        if item:
            notifications.append(item)
    return notifications


def _load_agreement_payment_notifications(limit=12):
    try:
        _ensure_cobro_acuerdo_table()
        _load_table_columns("WS_EVENT_QUEUE")
    except Exception:
        return []

    notifications = []
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT TOP (%s)
                    ID_EVENTO,
                    PAYLOAD_JSON,
                    FECHA_EVENTO
                FROM WS_EVENT_QUEUE
                WHERE CANAL = %s
                  AND TIPO_EVENTO = %s
                  AND ESTADO = 'COMPLETADO'
                ORDER BY FECHA_EVENTO DESC, ID_EVENTO DESC
                """,
                [max(1, min(int(limit or 12), 50)), "notifications", "agreement_payment_received"],
            )
            rows = cursor.fetchall()
    except Exception:
        return []

    for event_id, payload_json, fecha_evento in rows:
        payload = {}
        if payload_json:
            try:
                payload = json.loads(str(payload_json))
            except Exception:
                payload = {}
        acuerdo_id = str(payload.get("acuerdo_id") or "").strip()
        cliente_codigo = str(payload.get("id_sn") or payload.get("cliente_codigo") or "").strip()
        cliente_nombre = str(payload.get("cliente_nombre") or "").strip()
        cliente_label = " - ".join([piece for piece in [cliente_codigo, cliente_nombre] if piece])
        no_recibo = _stringify_doc(payload.get("no_recibo") or payload.get("document_id") or "")
        monto_pago = _to_decimal(payload.get("monto_pago"))
        tipo = str(payload.get("tipo_acuerdo") or "Acuerdo").strip().replace("_", " ").title()
        compromiso = _fmt_date_flexible(payload.get("fecha_compromiso"))
        resumen = f"Se registro un pago de RD$ {_format_money(monto_pago)}"
        if no_recibo:
            resumen = f"{resumen} en el recibo {no_recibo}"
        referencia = tipo
        if compromiso:
            referencia = f"{referencia} · Compromiso {compromiso}"
        notifications.append(
            {
                "id": str(event_id),
                "notification_type": "agreement_payment_received",
                "delete_mode": "server",
                "titulo": "Pago recibido con acuerdo pendiente",
                "cliente": cliente_label or cliente_nombre or cliente_codigo or "Sin cliente",
                "referencia": referencia,
                "resumen": resumen,
                "items_count": 1,
                "creado_en": _fmt_date_flexible(fecha_evento) if fecha_evento else "",
                "sort_timestamp": _notification_sort_timestamp(fecha_evento),
                "url": reverse("cobros:acuerdos") + (f"?edit={acuerdo_id}" if acuerdo_id else ""),
                "priority": 130,
            }
        )
    return notifications


def _ensure_notification_user_state_table():
    with connection.cursor() as cursor:
        cursor.execute(
            """
            IF OBJECT_ID('INV_NOTIFICATION_USER_STATE', 'U') IS NULL
            BEGIN
                CREATE TABLE INV_NOTIFICATION_USER_STATE (
                    ID_STATE INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
                    USUARIO_ID BIGINT NOT NULL,
                    NOTIFICATION_TYPE NVARCHAR(80) NOT NULL,
                    NOTIFICATION_ID NVARCHAR(120) NOT NULL,
                    LEIDA BIT NOT NULL CONSTRAINT DF_INV_NOTIFICATION_USER_STATE_LEIDA DEFAULT (0),
                    OCULTA BIT NOT NULL CONSTRAINT DF_INV_NOTIFICATION_USER_STATE_OCULTA DEFAULT (0),
                    ENTREGADA BIT NOT NULL CONSTRAINT DF_INV_NOTIFICATION_USER_STATE_ENTREGADA DEFAULT (0),
                    FECHA_CREACION DATETIME2 NOT NULL CONSTRAINT DF_INV_NOTIFICATION_USER_STATE_FECHA_CREACION DEFAULT (SYSDATETIME()),
                    FECHA_MODIFICACION DATETIME2 NOT NULL CONSTRAINT DF_INV_NOTIFICATION_USER_STATE_FECHA_MODIFICACION DEFAULT (SYSDATETIME())
                );
                CREATE UNIQUE INDEX UX_INV_NOTIFICATION_USER_STATE_USER_ITEM
                    ON INV_NOTIFICATION_USER_STATE (USUARIO_ID, NOTIFICATION_TYPE, NOTIFICATION_ID);
            END
            """
        )


def _normalize_notification_state_value(value, max_length=120):
    return str(value or "").strip()[:max_length]


def _load_notification_state_map(usuario_id, items):
    _ensure_notification_user_state_table()
    pairs = []
    seen = set()
    for item in items or []:
        notification_type = _normalize_notification_state_value(item.get("notification_type"), 80)
        notification_id = _normalize_notification_state_value(item.get("id"), 120)
        if not notification_type or not notification_id:
            continue
        key = (notification_type, notification_id)
        if key in seen:
            continue
        seen.add(key)
        pairs.append(key)
    if not pairs:
        return {}
    where_parts = []
    params = [int(usuario_id or 0)]
    for notification_type, notification_id in pairs:
        where_parts.append("(NOTIFICATION_TYPE = %s AND NOTIFICATION_ID = %s)")
        params.extend([notification_type, notification_id])
    sql = f"""
        SELECT NOTIFICATION_TYPE, NOTIFICATION_ID, LEIDA, OCULTA, ENTREGADA
        FROM INV_NOTIFICATION_USER_STATE
        WHERE USUARIO_ID = %s
          AND ({' OR '.join(where_parts)})
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
    return {
        (str(row[0] or "").strip(), str(row[1] or "").strip()): {
            "is_read": bool(row[2]),
            "is_hidden": bool(row[3]),
            "is_delivered": bool(row[4]),
        }
        for row in rows
    }


def _upsert_notification_user_state(usuario_id, notification_type, notification_id, *, is_read=None, is_hidden=None, is_delivered=None):
    _ensure_notification_user_state_table()
    notification_type = _normalize_notification_state_value(notification_type, 80)
    notification_id = _normalize_notification_state_value(notification_id, 120)
    if not usuario_id or not notification_type or not notification_id:
        return
    set_parts = ["FECHA_MODIFICACION = SYSDATETIME()"]
    insert_columns = ["USUARIO_ID", "NOTIFICATION_TYPE", "NOTIFICATION_ID"]
    insert_values = ["%s", "%s", "%s"]
    params = []
    insert_params = [int(usuario_id), notification_type, notification_id]
    if is_read is not None:
        set_parts.append("LEIDA = %s")
        params.append(1 if is_read else 0)
        insert_columns.append("LEIDA")
        insert_values.append("%s")
        insert_params.append(1 if is_read else 0)
    if is_hidden is not None:
        set_parts.append("OCULTA = %s")
        params.append(1 if is_hidden else 0)
        insert_columns.append("OCULTA")
        insert_values.append("%s")
        insert_params.append(1 if is_hidden else 0)
    if is_delivered is not None:
        set_parts.append("ENTREGADA = %s")
        params.append(1 if is_delivered else 0)
        insert_columns.append("ENTREGADA")
        insert_values.append("%s")
        insert_params.append(1 if is_delivered else 0)
    update_sql = f"""
        UPDATE INV_NOTIFICATION_USER_STATE
        SET {', '.join(set_parts)}
        WHERE USUARIO_ID = %s
          AND NOTIFICATION_TYPE = %s
          AND NOTIFICATION_ID = %s
    """
    with connection.cursor() as cursor:
        cursor.execute(update_sql, [*params, int(usuario_id), notification_type, notification_id])
        if cursor.rowcount:
            return
        cursor.execute(
            f"""
            INSERT INTO INV_NOTIFICATION_USER_STATE ({', '.join(insert_columns)})
            VALUES ({', '.join(insert_values)})
            """,
            insert_params,
        )


@lru_cache(maxsize=64)
def _load_table_columns(table_name):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_NAME = %s
            ORDER BY ORDINAL_POSITION
            """,
            [table_name],
        )
        return [str(row[0]).strip().upper() for row in cursor.fetchall() if row and row[0]]


@lru_cache(maxsize=64)
def _load_table_string_limits(table_name):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT COLUMN_NAME, CHARACTER_MAXIMUM_LENGTH
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_NAME = %s
              AND DATA_TYPE IN ('char', 'nchar', 'varchar', 'nvarchar')
            """,
            [table_name],
        )
        return {
            str(row[0]).strip().upper(): int(row[1] or 0)
            for row in cursor.fetchall()
            if row and row[0] and row[1]
        }


@lru_cache(maxsize=64)
def _load_table_column_types(table_name):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT COLUMN_NAME, DATA_TYPE
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_NAME = %s
            """,
            [table_name],
        )
        return {
            str(row[0]).strip().upper(): str(row[1] or "").strip().lower()
            for row in cursor.fetchall()
            if row and row[0]
        }


@lru_cache(maxsize=64)
def _load_identity_columns(table_name):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_NAME = %s
              AND COLUMNPROPERTY(object_id(TABLE_SCHEMA + '.' + TABLE_NAME), COLUMN_NAME, 'IsIdentity') = 1
            """,
            [table_name],
        )
        return {str(row[0]).strip().upper() for row in cursor.fetchall() if row and row[0]}


def _pick_existing_column(columns, *candidates):
    available = {str(column).upper(): str(column).upper() for column in (columns or [])}
    for candidate in candidates:
        if not candidate:
            continue
        found = available.get(str(candidate).upper())
        if found:
            return found
    return None


def _build_doc_lookup_where(columns, lookup_value):
    lookup_text = str(lookup_value or "").strip()
    normalized_columns = []
    seen = set()
    for column in columns or []:
        if not column:
            continue
        column_name = str(column).upper()
        if column_name in seen:
            continue
        seen.add(column_name)
        normalized_columns.append(column_name)

    if not lookup_text or not normalized_columns:
        return "1 = 0", []

    lookup_numeric = _to_decimal(lookup_text, default=None)
    where_parts = []
    params = []
    for column_name in normalized_columns:
        where_parts.append(f"CAST([{column_name}] AS NVARCHAR(255)) = %s")
        params.append(lookup_text)
        if lookup_numeric is not None:
            where_parts.append(f"TRY_CAST([{column_name}] AS DECIMAL(38, 10)) = TRY_CAST(%s AS DECIMAL(38, 10))")
            params.append(lookup_text)
    return "(" + " OR ".join(where_parts) + ")", params


def _normalize_result_row(columns, raw_row):
    return {str(columns[idx]).upper(): raw_row[idx] for idx in range(len(columns))}


def _pick_row_value(row, *candidates, default=None, allow_blank=False):
    for candidate in candidates:
        if not candidate:
            continue
        key = str(candidate).upper()
        if key not in row:
            continue
        value = row.get(key)
        if value is None:
            continue
        if isinstance(value, str) and not allow_blank and not value.strip():
            continue
        return value
    return default


def _unique_preserve(*values):
    seen = set()
    result = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        upper = text.upper()
        if upper in seen:
            continue
        seen.add(upper)
        result.append(text)
    return result


def _build_multi_lookup_where(columns, lookup_values):
    parts = []
    params = []
    for value in _unique_preserve(*(lookup_values or [])):
        where_sql, where_params = _build_doc_lookup_where(columns, value)
        if where_params:
            parts.append(where_sql)
            params.extend(where_params)
    if not parts:
        return "1 = 0", []
    return "(" + " OR ".join(parts) + ")", params


def _assign_existing_values(target, columns, value, *candidates):
    if value is None:
        return
    available = {str(column).upper(): str(column).upper() for column in (columns or [])}
    for candidate in candidates:
        if not candidate:
            continue
        found = available.get(str(candidate).upper())
        if found:
            target[found] = value


def _sanitize_table_values(table_name, values_by_column):
    if not values_by_column:
        return {}
    string_limits = _load_table_string_limits(table_name)
    column_types = _load_table_column_types(table_name)
    string_types = {"char", "nchar", "varchar", "nvarchar", "text", "ntext"}
    sanitized = {}
    for column_name, raw_value in (values_by_column or {}).items():
        normalized_name = str(column_name).upper()
        value = raw_value
        column_type = column_types.get(normalized_name, "")
        if isinstance(value, str):
            if column_type not in string_types and not value.strip():
                value = None
        max_length = string_limits.get(normalized_name)
        if max_length and isinstance(value, str) and len(value) > max_length:
            value = value[:max_length]
        sanitized[normalized_name] = value
    return sanitized


def _insert_dynamic_row(cursor, table_name, table_columns, values_by_column, *, output_column=None, skip_columns=None):
    values_by_column = _sanitize_table_values(table_name, values_by_column)
    skip_columns = {str(column).upper() for column in (skip_columns or set())}
    insert_columns = [column for column in table_columns if column in values_by_column and column not in skip_columns]
    if not insert_columns:
        raise ValueError(f"No hay columnas para insertar en {table_name}")
    placeholders = ", ".join(["%s"] * len(insert_columns))
    output_sql = f" OUTPUT INSERTED.[{output_column}]" if output_column else ""
    sql = (
        f"INSERT INTO {table_name} ({', '.join(f'[{column}]' for column in insert_columns)})"
        f"{output_sql} VALUES ({placeholders})"
    )
    params = [values_by_column[column] for column in insert_columns]
    cursor.execute(sql, params)
    if output_column:
        row = cursor.fetchone()
        return row[0] if row else None
    return None


def _next_table_numeric_value(cursor, table_name, column_name):
    cursor.execute(
        f"""
        SELECT ISNULL(MAX(TRY_CAST([{column_name}] AS BIGINT)), 0) + 1
        FROM {table_name} WITH (UPDLOCK, HOLDLOCK)
        """
    )
    row = cursor.fetchone()
    next_value = int(row[0] or 0)
    return next_value if next_value > 0 else 1


def _to_date_or_none(value):
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except Exception:
        return None


@lru_cache(maxsize=8)
def _load_departamento_rows():
    dept_columns = _load_table_columns("DEPARTAMENTO")
    if not dept_columns:
        return []

    ceco_col = _pick_existing_column(dept_columns, "CECO", "CENTRO_COSTO", "ID_DEPTO", "DEPARTAMENTO", "CODIGO")
    descripcion_col = _pick_existing_column(dept_columns, "DESCRIPCION", "DESCRIP", "NOMBRE", "NOM_DEPTO")
    if not ceco_col and not descripcion_col:
        return []

    select_columns = [column for column in [ceco_col, descripcion_col] if column]
    sql = "SELECT " + ", ".join(f"[{column}]" for column in select_columns) + " FROM DEPARTAMENTO"
    if descripcion_col:
        sql += f" ORDER BY CAST([{descripcion_col}] AS NVARCHAR(255))"
    elif ceco_col:
        sql += f" ORDER BY CAST([{ceco_col}] AS NVARCHAR(255))"

    with connection.cursor() as cursor:
        cursor.execute(sql)
        columns = [col[0] for col in cursor.description]
        rows = cursor.fetchall()

    results = []
    for raw_row in rows:
        row = _normalize_result_row(columns, raw_row)
        ceco = str(_pick_row_value(row, ceco_col, default="", allow_blank=True) or "").strip()
        descripcion = str(_pick_row_value(row, descripcion_col, default="", allow_blank=True) or "").strip()
        if not ceco and not descripcion:
            continue
        results.append({"ceco": ceco, "descripcion": descripcion or ceco})
    return results


def _resolve_departamento_descripcion(ceco_value, fallback=""):
    ceco_text = str(ceco_value or "").strip()
    if ceco_text:
        for row in _load_departamento_rows():
            if str(row.get("ceco") or "").strip().upper() == ceco_text.upper():
                return str(row.get("descripcion") or "").strip()
    return str(fallback or "").strip()


def _get_default_departamento():
    departamentos = _load_departamento_rows()
    if not departamentos:
        return {"ceco": "", "descripcion": ""}
    for row in departamentos:
        descripcion = str(row.get("descripcion") or "").strip().upper()
        if descripcion == "ADMINISTRACION":
            return row
    return departamentos[0]


def _load_proveedor_search_rows(*, query=""):
    maestro_columns = _load_table_columns("MAESTRO_SN")
    if not maestro_columns:
        return []

    id_sn_col = _pick_existing_column(maestro_columns, "ID_SN", "COD_SN", "CODIGO", "ID")
    nom_socio_col = _pick_existing_column(maestro_columns, "NOM_SOCIO", "NOM_SN", "NOMBRE", "RAZON_SOCIAL")
    clase_col = _pick_existing_column(maestro_columns, "CLASE_SN", "CLASE", "TIPO_SN")
    rnc_col = _pick_existing_column(maestro_columns, "RNC_CED", "RNC", "CEDULA")
    telefono_col = _pick_existing_column(maestro_columns, "TELEFONO", "TEL", "MOVIL", "CELULAR")
    if not clase_col or not id_sn_col or not nom_socio_col:
        return []

    select_columns = [column for column in [id_sn_col, nom_socio_col, clase_col, rnc_col, telefono_col] if column]
    select_columns = list(dict.fromkeys(select_columns))
    sql = (
        "SELECT TOP 80 "
        + ", ".join(f"[{column}]" for column in select_columns)
        + f" FROM MAESTRO_SN WHERE UPPER(CAST([{clase_col}] AS NVARCHAR(255))) = %s"
    )
    params = ["PROVEEDOR"]
    query_text = str(query or "").strip()
    if query_text:
        search_columns = [column for column in [id_sn_col, nom_socio_col, rnc_col, telefono_col] if column]
        if search_columns:
            sql += " AND (" + " OR ".join(f"CAST([{column}] AS NVARCHAR(255)) LIKE %s" for column in search_columns) + ")"
            params.extend([f"%{query_text}%"] * len(search_columns))
    sql += f" ORDER BY CAST([{nom_socio_col}] AS NVARCHAR(255))"

    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        columns = [col[0] for col in cursor.description]
        rows = cursor.fetchall()

    results = []
    for raw_row in rows:
        row = _normalize_result_row(columns, raw_row)
        results.append(
            {
                "id_sn": str(_pick_row_value(row, id_sn_col, default="", allow_blank=True) or "").strip(),
                "nom_socio": str(_pick_row_value(row, nom_socio_col, default="", allow_blank=True) or "").strip(),
                "rnc_ced": str(_pick_row_value(row, rnc_col, default="", allow_blank=True) or "").strip(),
                "telefono": str(_pick_row_value(row, telefono_col, default="", allow_blank=True) or "").strip(),
            }
        )
    return results


def _load_entrada_articulos_articulo_rows(*, query="", filtro="descripcion"):
    qs = MaestroArticulo.objects.exclude(bloqueado__iexact="Y")
    query_text = str(query or "").strip()
    filtro_text = str(filtro or "descripcion").strip().lower()
    if query_text:
        if filtro_text == "codigo":
            qs = qs.filter(Q(referencia__icontains=query_text) | Q(id_articulo__icontains=query_text))
        else:
            qs = qs.filter(descrip_art__icontains=query_text)
    if filtro_text == "codigo":
        qs = qs.order_by("referencia", "id_articulo")
    else:
        qs = qs.order_by("id_articulo")
    values = list(qs.values(
        "id_articulo",
        "descrip_art",
        "referencia",
        "precio_det",
        "um_inv",
        "cta_aum_stock",
        "alm_dft",
        "ceco",
    )[:80])

    tarj_stock = {}
    if values:
        articulo_ids = [row.get("id_articulo") for row in values if row.get("id_articulo")]
        if articulo_ids:
            with connection.cursor() as cursor:
                placeholders = ", ".join(["%s"] * len(articulo_ids))
                cursor.execute(
                    f"SELECT ID_ARTICULO, COALESCE(SUM(CANTIDAD), 0) FROM TARJETERO WHERE ID_ARTICULO IN ({placeholders}) GROUP BY ID_ARTICULO",
                    articulo_ids
                )
                tarj_stock = {str(row[0] or "").strip(): float(row[1] or 0) for row in cursor.fetchall()}

    return [
        {
            "id_articulo": row.get("id_articulo") or "",
            "descrip_art": row.get("descrip_art") or "",
            "referencia": row.get("referencia") or "",
            "precio_det": float(_to_decimal(row.get("precio_det"))),
            "stock": tarj_stock.get(str(row.get("id_articulo") or "").strip(), 0.0),
            "um_inv": row.get("um_inv") or "",
            "porc_com": 0,
            "cta_aum_stock": row.get("cta_aum_stock") or "",
            "alm_dft": _stringify_doc(row.get("alm_dft")),
            "cebe": "",
            "ceco": row.get("ceco") or "",
        }
        for row in values
    ]


def _load_entrada_articulos_search_rows(*, query="", filtro="documento"):
    cab_columns = _load_table_columns("CAB_ENT_INV")
    if not cab_columns:
        return []

    doc_col = _pick_existing_column(cab_columns, "NO_DOC", "ID_DOC", "NO", "NO_ENTRADA", "ID_ENTRADA", "DOCUMENTO")
    no_col = _pick_existing_column(cab_columns, "NO", "ID_ENTRADA", "NO_ENTRADA", doc_col)
    codigo_col = _pick_existing_column(cab_columns, "ID_MOVIMIENTO", "CODIGO", "COD", "COD_DOC", "COD_TIPO", "TIPO_DOC")
    descripcion_col = _pick_existing_column(cab_columns, "DESCRIPCION", "DESCRIP", "DESCRIP_DOC", "DESCRIPCION_DOC")
    asunto_col = _pick_existing_column(cab_columns, "ASUNTO", "CONCEPTO", "REFERENCIA")
    proveedor_codigo_col = _pick_existing_column(cab_columns, "ID_SN", "ID_PROV", "COD_PROV", "ID_PROVEEDOR", "PROVEEDOR")
    proveedor_nombre_col = _pick_existing_column(cab_columns, "NOM_SOCIO", "NOM_SN", "NOM_PROV", "NOMBRE_PROV", "NOM_SUPLIDOR", "NOMBRE")
    estado_col = _pick_existing_column(cab_columns, "EST_DOC", "ESTATUS", "ESTADO")
    fecha_col = _pick_existing_column(cab_columns, "FECHA_DOC", "FECHA_CONT", "FECHA", "FECHA_APLIC")
    total_col = _pick_existing_column(cab_columns, "TOTAL_DOC", "TOTAL", "MONTO", "IMPORTE", "VALOR")

    select_columns = [col for col in [doc_col, no_col, codigo_col, descripcion_col, asunto_col, proveedor_codigo_col, proveedor_nombre_col, estado_col, fecha_col, total_col] if col]
    select_columns = list(dict.fromkeys(select_columns))
    if not select_columns:
        return []

    sql = "SELECT TOP 80 " + ", ".join(f"[{column}]" for column in select_columns) + " FROM CAB_ENT_INV"
    params = []
    query_text = str(query or "").strip()
    if query_text:
        filtro = str(filtro or "documento").strip().lower()
        if filtro == "documento":
            search_columns = [doc_col, no_col]
        elif filtro == "codigo":
            search_columns = [codigo_col, proveedor_codigo_col]
        elif filtro == "descripcion":
            search_columns = [descripcion_col, asunto_col]
        elif filtro == "proveedor":
            search_columns = [proveedor_codigo_col, proveedor_nombre_col]
        else:
            search_columns = [doc_col, no_col, codigo_col, descripcion_col, asunto_col, proveedor_codigo_col, proveedor_nombre_col]
        search_columns = [column for column in search_columns if column]
        if search_columns:
            sql += " WHERE (" + " OR ".join(f"CAST([{column}] AS NVARCHAR(255)) LIKE %s" for column in search_columns) + ")"
            params.extend([f"%{query_text}%"] * len(search_columns))

    if doc_col:
        sql += f" ORDER BY TRY_CAST([{doc_col}] AS BIGINT) DESC, CAST([{doc_col}] AS NVARCHAR(255)) DESC"
    elif no_col:
        sql += f" ORDER BY TRY_CAST([{no_col}] AS BIGINT) DESC, CAST([{no_col}] AS NVARCHAR(255)) DESC"

    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        columns = [col[0] for col in cursor.description]
        rows = cursor.fetchall()

    results = []
    for raw_row in rows:
        row = _normalize_result_row(columns, raw_row)
        no_doc = _stringify_doc(_pick_row_value(row, doc_col, no_col, default=""))
        codigo = str(_pick_row_value(row, codigo_col, default="", allow_blank=True) or "").strip()
        descripcion = str(_pick_row_value(row, descripcion_col, asunto_col, default="", allow_blank=True) or "").strip()
        proveedor_codigo = str(_pick_row_value(row, proveedor_codigo_col, default="", allow_blank=True) or "").strip()
        proveedor_nombre = str(_pick_row_value(row, proveedor_nombre_col, default="", allow_blank=True) or "").strip()
        estado = str(_pick_row_value(row, estado_col, default="", allow_blank=True) or "").strip()
        total_doc = _to_decimal(_pick_row_value(row, total_col, default=Decimal("0")))
        results.append(
            {
                "no_doc": no_doc,
                "codigo": codigo,
                "descripcion": descripcion,
                "proveedor_codigo": proveedor_codigo,
                "proveedor_nombre": proveedor_nombre,
                "fecha_doc": _fmt_date_flexible(_pick_row_value(row, fecha_col, default="")),
                "estado": estado,
                "total_doc": float(total_doc),
                "total_doc_fmt": _format_money(total_doc),
            }
        )
    return results


def _load_entrada_articulos_record(lookup_value):
    lookup_text = str(lookup_value or "").strip()
    if not lookup_text:
        return None

    cab_columns = _load_table_columns("CAB_ENT_INV")
    det_columns = _load_table_columns("DET_ENT_INV")
    if not cab_columns:
        return None

    cab_doc_col = _pick_existing_column(cab_columns, "NO_DOC", "ID_DOC", "NO", "NO_ENTRADA", "ID_ENTRADA", "DOCUMENTO")
    cab_no_col = _pick_existing_column(cab_columns, "NO", "ID_ENTRADA", "NO_ENTRADA", cab_doc_col)
    if not cab_doc_col and not cab_no_col:
        return None

    where_sql, where_params = _build_doc_lookup_where([cab_doc_col, cab_no_col], lookup_text)
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT TOP 1 * FROM CAB_ENT_INV WHERE {where_sql}", where_params)
        raw_header = cursor.fetchone()
        if not raw_header:
            return None
        header_columns = [col[0] for col in cursor.description]
        header_row = _normalize_result_row(header_columns, raw_header)

    codigo_col = _pick_existing_column(cab_columns, "ID_MOVIMIENTO", "CODIGO", "COD", "COD_DOC", "COD_TIPO", "TIPO_DOC")
    descripcion_col = _pick_existing_column(cab_columns, "DESCRIPCION", "DESCRIP", "DESCRIP_DOC", "DESCRIPCION_DOC")
    asunto_col = _pick_existing_column(cab_columns, "ASUNTO", "CONCEPTO", "REFERENCIA")
    departamento_col = _pick_existing_column(cab_columns, "DEPARTAMENTO", "DEPTO", "DPTO")
    ceco_col = _pick_existing_column(cab_columns, "CECO", "CENTRO_COSTO")
    proveedor_codigo_col = _pick_existing_column(cab_columns, "ID_SN", "ID_PROV", "COD_PROV", "ID_PROVEEDOR", "PROVEEDOR")
    proveedor_nombre_col = _pick_existing_column(cab_columns, "NOM_SOCIO", "NOM_SN", "NOM_PROV", "NOMBRE_PROV", "NOM_SUPLIDOR", "NOMBRE")
    estado_col = _pick_existing_column(cab_columns, "EST_DOC", "ESTATUS", "ESTADO")
    fecha_cont_col = _pick_existing_column(cab_columns, "FECHA_CONT", "FECHA", "FECHA_APLIC", "F_CONT")
    fecha_venc_col = _pick_existing_column(cab_columns, "FECHA_VENC", "FECHA_VENCE", "VENCIMIENTO")
    fecha_doc_col = _pick_existing_column(cab_columns, "FECHA_DOC", "FECHA", "FECHA_CONT")
    comentario_col = _pick_existing_column(cab_columns, "COMENTARIO", "OBSERVACION", "NOTA")
    total_col = _pick_existing_column(cab_columns, "TOTAL_DOC", "TOTAL", "MONTO", "IMPORTE", "VALOR")

    document_values = _unique_preserve(
        _pick_row_value(header_row, cab_doc_col, default=""),
        _pick_row_value(header_row, cab_no_col, default=""),
        lookup_text,
    )

    detalles = []
    if det_columns:
        det_doc_col = _pick_existing_column(det_columns, "NO_DOC", "ID_DOC", "NO", "NO_ENTRADA", "ID_ENTRADA", "DOCUMENTO")
        det_no_col = _pick_existing_column(det_columns, "NO", "ID_ENTRADA", "NO_ENTRADA")
        det_line_col = _pick_existing_column(det_columns, "NO_LINEA", "LINEA", "NO_ITEM", "ORDEN", "ID_DETALLE")
        det_desc_col = _pick_existing_column(det_columns, "DESCRIP_ART", "DESCRIPCION", "DESCRIP", "DESCRIP_ART_SERV")
        det_art_col = _pick_existing_column(det_columns, "ID_ARTICULO", "ARTICULO", "COD_ART", "CODIGO")
        det_cant_emp_col = _pick_existing_column(det_columns, "PORC_COM", "CANT_EMP", "CANT_EMPAQUE", "CANT_UND", "CANT_UNIDADES")
        det_cantidad_col = _pick_existing_column(det_columns, "CANTIDAD", "CANT")
        det_uom_col = _pick_existing_column(det_columns, "MEDIDA", "UOM", "U_MED", "UNIDAD")
        det_alm_col = _pick_existing_column(det_columns, "ID_ALMACEN", "ALM", "ALMACEN", "ID_ALM")
        det_pedido_col = _pick_existing_column(det_columns, "ID_CLIENTE", "PEDIDO_CTE", "PEDIDO", "NO_PEDIDO")
        det_proyecto_col = _pick_existing_column(det_columns, "CEBE", "PROYECTO", "ID_PROYECTO")
        det_ceco_col = _pick_existing_column(det_columns, "CECO", "CENTRO_COSTO")
        det_costo_col = _pick_existing_column(det_columns, "PRECIO", "COSTO_UNIT", "COSTO", "COSTO_UNITARIO")
        det_valor_col = _pick_existing_column(det_columns, "TOTAL_PRECIO", "VALOR", "TOTAL_LINEA", "TOTAL", "IMPORTE")
        det_cta_col = _pick_existing_column(det_columns, "CTA_AUM_STOCK", "CTA_MAYOR", "CUENTA_MAYOR", "CTA_LM", "CUENTA")

        if det_doc_col or det_no_col:
            where_sql, where_params = _build_multi_lookup_where([det_doc_col, det_no_col], document_values)
            with connection.cursor() as cursor:
                cursor.execute(f"SELECT * FROM DET_ENT_INV WHERE {where_sql}", where_params)
                detail_columns = [col[0] for col in cursor.description]
                detail_rows = cursor.fetchall()
            parsed_rows = []
            for raw_row in detail_rows:
                row = _normalize_result_row(detail_columns, raw_row)
                parsed_rows.append(
                    {
                        "linea": _stringify_doc(_pick_row_value(row, det_line_col, default="")),
                        "descripcion": str(_pick_row_value(row, det_desc_col, default="", allow_blank=True) or "").strip(),
                        "articulo": str(_pick_row_value(row, det_art_col, default="", allow_blank=True) or "").strip(),
                        "cant_emp": str(_pick_row_value(row, det_cant_emp_col, default="", allow_blank=True) or "").strip(),
                        "cantidad": _format_money(_pick_row_value(row, det_cantidad_col, default=Decimal("0"))),
                        "uom": str(_pick_row_value(row, det_uom_col, default="", allow_blank=True) or "").strip(),
                        "alm": str(_pick_row_value(row, det_alm_col, default="", allow_blank=True) or "").strip(),
                        "pedido_cte": str(_pick_row_value(row, det_pedido_col, default="", allow_blank=True) or "").strip(),
                        "proyecto": str(_pick_row_value(row, det_proyecto_col, default="", allow_blank=True) or "").strip(),
                        "ceco": str(_pick_row_value(row, det_ceco_col, default="", allow_blank=True) or "").strip(),
                        "costo_unit": _format_money(_pick_row_value(row, det_costo_col, default=Decimal("0"))),
                        "valor": _format_money(_pick_row_value(row, det_valor_col, default=Decimal("0"))),
                        "cuenta_mayor": str(_pick_row_value(row, det_cta_col, default="", allow_blank=True) or "").strip(),
                    }
                )
            detalles = sorted(parsed_rows, key=lambda item: (Decimal(item["linea"]) if str(item["linea"]).replace(".", "", 1).isdigit() else Decimal("999999"), item["linea"]))

    total_doc = _to_decimal(_pick_row_value(header_row, total_col, default=Decimal("0")))
    if total_doc == Decimal("0") and detalles:
        total_doc = sum((_to_decimal(row.get("valor")) for row in detalles), Decimal("0"))

    departamento_ceco = str(_pick_row_value(header_row, ceco_col, default="", allow_blank=True) or "").strip()
    departamento_descripcion = _resolve_departamento_descripcion(
        departamento_ceco,
        fallback=_pick_row_value(header_row, departamento_col, default="", allow_blank=True) or "",
    )

    return {
        "entry": {
            "lookup": lookup_text,
            "no": _stringify_doc(_pick_row_value(header_row, cab_no_col, cab_doc_col, default="")),
            "no_doc": _stringify_doc(_pick_row_value(header_row, cab_doc_col, cab_no_col, default="")),
            "codigo": str(_pick_row_value(header_row, codigo_col, default="", allow_blank=True) or "").strip(),
            "descripcion": str(_pick_row_value(header_row, descripcion_col, default="", allow_blank=True) or "").strip(),
            "asunto": str(_pick_row_value(header_row, asunto_col, default="", allow_blank=True) or "").strip(),
            "departamento": departamento_descripcion,
            "departamento_ceco": departamento_ceco,
            "proveedor_codigo": str(_pick_row_value(header_row, proveedor_codigo_col, default="", allow_blank=True) or "").strip(),
            "proveedor_nombre": str(_pick_row_value(header_row, proveedor_nombre_col, default="", allow_blank=True) or "").strip(),
            "estado": str(_pick_row_value(header_row, estado_col, default="", allow_blank=True) or "").strip(),
            "fecha_cont": _fmt_date_input(_pick_row_value(header_row, fecha_cont_col, default="")),
            "fecha_venc": _fmt_date_input(_pick_row_value(header_row, fecha_venc_col, default="")),
            "fecha_doc": _fmt_date_input(_pick_row_value(header_row, fecha_doc_col, default="")),
            "comentario": str(_pick_row_value(header_row, comentario_col, default="", allow_blank=True) or "").strip(),
            "total_doc": _format_money(total_doc),
        },
        "detalles": detalles,
    }


def _create_entrada_ed_entries(
    cursor,
    *,
    origen_doc,
    fecha_cont,
    fecha_doc,
    fecha_venc,
    total_doc,
    comentario,
    usuario_id,
    usuario_nombre,
    terminal,
    departamento_ceco,
):
    cab_ed_columns = _load_table_columns("CAB_ED")
    det_ed_columns = _load_table_columns("DET_ED")
    if not cab_ed_columns or not det_ed_columns:
        raise ValueError("No se pudieron cargar las tablas CAB_ED/DET_ED.")

    cab_ed_identity_columns = _load_identity_columns("CAB_ED")
    det_ed_identity_columns = _load_identity_columns("DET_ED")
    cab_ed_key_col = _pick_existing_column(cab_ed_columns, "ID_DOC", "ID_ED", "NO_DOC", "NO_ED")
    cab_ed_no_col = _pick_existing_column(cab_ed_columns, "NO_DOC", "NO_ED", "ID_DOC", "ID_ED")
    if not cab_ed_key_col and not cab_ed_no_col:
        raise ValueError("No se pudo determinar la clave de CAB_ED.")

    next_ed_no = None
    if cab_ed_no_col and cab_ed_no_col not in cab_ed_identity_columns:
        next_ed_no = _next_table_numeric_value(cursor, "CAB_ED", cab_ed_no_col)
    elif cab_ed_key_col and cab_ed_key_col not in cab_ed_identity_columns:
        next_ed_no = _next_table_numeric_value(cursor, "CAB_ED", cab_ed_key_col)

    total_doc = _to_decimal(total_doc)
    comentario_ed = str(comentario or "").strip() or "Entrada de Mercancia"
    today = timezone.localdate()
    periodo_cont = str(today.month).zfill(2)
    ejercicio = today.year
    cab_ed_values = {}
    if cab_ed_key_col and cab_ed_key_col not in cab_ed_identity_columns:
        _assign_existing_values(cab_ed_values, cab_ed_columns, next_ed_no, cab_ed_key_col)
    if next_ed_no is not None:
        _assign_existing_values(cab_ed_values, cab_ed_columns, next_ed_no, "NO_DOC", "NO_ED")
    _assign_existing_values(cab_ed_values, cab_ed_columns, fecha_cont, "FECHA_CONT", "F_CONT")
    _assign_existing_values(cab_ed_values, cab_ed_columns, fecha_doc, "FECHA_DOC", "FECHA_APLIC")
    _assign_existing_values(cab_ed_values, cab_ed_columns, fecha_venc, "FECHA_VENC", "F_VENC")
    _assign_existing_values(cab_ed_values, cab_ed_columns, "EM", "TIPO_DOC", "TD", "CLASE_DOC", "TIPO")
    _assign_existing_values(cab_ed_values, cab_ed_columns, total_doc, "TOTAL_DOC", "MONTO", "IMPORTE")
    _assign_existing_values(cab_ed_values, cab_ed_columns, comentario_ed, "COMENTARIO", "OBSERVACION")
    _assign_existing_values(cab_ed_values, cab_ed_columns, "Cerrado", "EST_DOC", "ESTADO", "ESTATUS")
    _assign_existing_values(cab_ed_values, cab_ed_columns, origen_doc, "ORIGEN", "REFERENCIA", "NO_RECIBO")
    _assign_existing_values(cab_ed_values, cab_ed_columns, "RD$", "MON_DOC", "MONEDA")
    _assign_existing_values(cab_ed_values, cab_ed_columns, periodo_cont, "PERIODO_CONT")
    _assign_existing_values(cab_ed_values, cab_ed_columns, ejercicio, "EJERCICIO")
    _assign_existing_values(cab_ed_values, cab_ed_columns, usuario_id, "ID_USUARIO", "USUARIO_ID")
    _assign_existing_values(cab_ed_values, cab_ed_columns, usuario_nombre, "USUARIO", "USUARIO_NOMBRE")
    _assign_existing_values(cab_ed_values, cab_ed_columns, terminal, "TERMINAL")
    _assign_existing_values(cab_ed_values, cab_ed_columns, timezone.localdate(), "FECHA_CREACION")
    _assign_existing_values(cab_ed_values, cab_ed_columns, timezone.localtime(), "FECHA_ACT")

    inserted_ed_id = _insert_dynamic_row(
        cursor,
        "CAB_ED",
        cab_ed_columns,
        cab_ed_values,
        output_column=cab_ed_key_col or cab_ed_no_col,
        skip_columns=cab_ed_identity_columns,
    )
    ed_doc_id = _stringify_doc(inserted_ed_id or next_ed_no or "")
    ed_doc_no = _stringify_doc(next_ed_no or inserted_ed_id or ed_doc_id)

    det_line_col = _pick_existing_column(det_ed_columns, "NO_LINEA", "LINEA", "NO_ITEM", "ORDEN")

    def _build_det_ed_values(*, line_no, cuenta_num, cuenta_nombre, debito, credito):
        det_ed_values = {}
        _assign_existing_values(det_ed_values, det_ed_columns, ed_doc_id, "ID_DOC", "ID_ED")
        _assign_existing_values(det_ed_values, det_ed_columns, ed_doc_no, "NO_DOC", "NO_ED")
        if det_line_col and det_line_col not in det_ed_identity_columns:
            _assign_existing_values(det_ed_values, det_ed_columns, line_no, det_line_col)
        _assign_existing_values(det_ed_values, det_ed_columns, fecha_cont, "FECHA_CONT", "F_CONT")
        _assign_existing_values(det_ed_values, det_ed_columns, fecha_doc, "FECHA_DOC", "FECHA_APLIC")
        _assign_existing_values(det_ed_values, det_ed_columns, fecha_venc, "FECHA_VENC", "F_VENC")
        _assign_existing_values(det_ed_values, det_ed_columns, cuenta_num, "CTA_LM", "NUM_CTA", "CTA")
        _assign_existing_values(
            det_ed_values,
            det_ed_columns,
            cuenta_nombre,
            "NOM_CTA",
            "NOMBRE_CTA",
            "NOM_CUENTA",
            "NOMBRE_CUENTA",
            "NOMBRE",
        )
        _assign_existing_values(det_ed_values, det_ed_columns, "EM", "TIPO_DOC", "TD", "CLASE_DOC", "TIPO")
        _assign_existing_values(det_ed_values, det_ed_columns, origen_doc, "ORIGEN", "REFERENCIA", "NO_RECIBO")
        _assign_existing_values(det_ed_values, det_ed_columns, debito, "DEBITO", "DEBE")
        _assign_existing_values(det_ed_values, det_ed_columns, credito, "CREDITO", "HABER")
        _assign_existing_values(det_ed_values, det_ed_columns, comentario_ed, "COMENTARIO", "OBSERVACION")
        _assign_existing_values(det_ed_values, det_ed_columns, "Cerrado", "EST_DOC", "ESTADO", "ESTATUS")
        _assign_existing_values(det_ed_values, det_ed_columns, "RD$", "MON_DOC", "MONEDA")
        _assign_existing_values(det_ed_values, det_ed_columns, periodo_cont, "PERIODO_CONT")
        _assign_existing_values(det_ed_values, det_ed_columns, ejercicio, "EJERCICIO")
        _assign_existing_values(det_ed_values, det_ed_columns, usuario_id, "ID_USUARIO", "USUARIO_ID")
        _assign_existing_values(det_ed_values, det_ed_columns, usuario_nombre, "USUARIO", "USUARIO_NOMBRE")
        _assign_existing_values(det_ed_values, det_ed_columns, terminal, "TERMINAL")
        if str(cuenta_num or "").strip() == "51010102":
            _assign_existing_values(det_ed_values, det_ed_columns, departamento_ceco, "CECO")
            _assign_existing_values(det_ed_values, det_ed_columns, "C01", "CEBE")
        _assign_existing_values(det_ed_values, det_ed_columns, timezone.localdate(), "FECHA_CREACION")
        _assign_existing_values(det_ed_values, det_ed_columns, timezone.localtime(), "FECHA_ACT")
        return det_ed_values

    for line_no, cuenta_num, cuenta_nombre, debito, credito in (
        (1, "11030101", "Mercancia Disponible para la Venta", total_doc, Decimal("0")),
        (2, "51010102", "Costo de Mercancia Ajuste EM/SM", Decimal("0"), total_doc),
    ):
        _insert_dynamic_row(
            cursor,
            "DET_ED",
            det_ed_columns,
            _build_det_ed_values(
                line_no=line_no,
                cuenta_num=cuenta_num,
                cuenta_nombre=cuenta_nombre,
                debito=debito,
                credito=credito,
            ),
            skip_columns=det_ed_identity_columns,
        )

    return ed_doc_no or ed_doc_id


def _persist_entrada_articulos_record(
    cursor,
    *,
    payload,
    usuario_id,
    usuario_nombre,
    terminal,
):
    cab_columns = _load_table_columns("CAB_ENT_INV")
    det_columns = _load_table_columns("DET_ENT_INV")
    if not cab_columns or not det_columns:
        raise ValueError("No se pudieron cargar las tablas CAB_ENT_INV/DET_ENT_INV.")

    cab_identity_columns = _load_identity_columns("CAB_ENT_INV")
    det_identity_columns = _load_identity_columns("DET_ENT_INV")

    detalles = [detalle for detalle in (payload.get("detalles") or []) if isinstance(detalle, dict)]
    if not detalles:
        raise ValueError("Debes agregar al menos una linea en el detalle.")

    departamento_ceco = str(payload.get("departamento_ceco") or "").strip()
    departamentos = _load_departamento_rows()
    if departamentos and not departamento_ceco:
        raise ValueError("Debes seleccionar un departamento.")
    departamento_descripcion = _resolve_departamento_descripcion(departamento_ceco)

    proveedor_codigo = str(payload.get("proveedor_codigo") or "").strip() or "-1"
    proveedor_nombre = str(payload.get("proveedor_nombre") or "").strip()

    fecha_cont = _to_date_or_none(payload.get("fecha_cont")) or timezone.localdate()
    fecha_doc = _to_date_or_none(payload.get("fecha_doc")) or fecha_cont
    fecha_venc = _to_date_or_none(payload.get("fecha_venc")) or fecha_doc
    asunto = str(payload.get("asunto") or "").strip()
    comentario = str(payload.get("comentario") or "").strip()

    cab_doc_col = _pick_existing_column(cab_columns, "NO_DOC", "ID_DOC", "NO", "NO_ENTRADA", "ID_ENTRADA", "DOCUMENTO")
    cab_no_col = _pick_existing_column(cab_columns, "NO", "ID_ENTRADA", "NO_ENTRADA", cab_doc_col)
    output_column = cab_doc_col or cab_no_col
    sequence_candidates = [
        column
        for column in [cab_no_col, cab_doc_col, "NO_DOC", "NO", "NO_ENTRADA", "ID_ENTRADA", "ID_DOC"]
        if column and column in cab_columns and column not in cab_identity_columns
    ]
    next_no_value = _next_table_numeric_value(cursor, "CAB_ENT_INV", sequence_candidates[0]) if sequence_candidates else None
    no_value_text = _stringify_doc(next_no_value) if next_no_value is not None else ""

    total_doc = Decimal("0")
    cleaned_details = []
    for index, detalle in enumerate(detalles, start=1):
        id_articulo = str(detalle.get("id_articulo") or "").strip()
        descripcion = str(detalle.get("descripcion") or "").strip()
        if not id_articulo and not descripcion:
            continue
        if not id_articulo:
            raise ValueError(f"La linea {index} no tiene articulo seleccionado.")
        cantidad = _to_decimal(detalle.get("cantidad"))
        if cantidad <= Decimal("0"):
            raise ValueError(f"La cantidad de la linea {index} debe ser mayor que cero.")
        costo_unit = _to_decimal(detalle.get("costo_unit"))
        valor = _to_decimal(detalle.get("valor"))
        if valor <= Decimal("0"):
            valor = cantidad * costo_unit
        total_doc += valor
        cleaned_details.append(
            {
                "linea": index,
                "id_articulo": id_articulo,
                "descripcion": descripcion,
                "cant_emp": _to_decimal(detalle.get("cant_emp")),
                "cantidad": cantidad,
                "uom": str(detalle.get("uom") or "").strip(),
                "alm": str(detalle.get("alm") or "").strip(),
                "pedido_cte": str(detalle.get("pedido_cte") or "").strip(),
                "proyecto": str(detalle.get("proyecto") or "").strip(),
                "ceco": str(detalle.get("ceco") or "").strip() or departamento_ceco,
                "costo_unit": costo_unit,
                "valor": valor,
                "cuenta_mayor": str(detalle.get("cuenta_mayor") or "").strip(),
            }
        )

    if not cleaned_details:
        raise ValueError("Debes agregar al menos una linea con articulo.")

    header_values = {}
    if next_no_value is not None:
        _assign_existing_values(header_values, cab_columns, next_no_value, "ID_DOC", "ID_ENTRADA")
        _assign_existing_values(header_values, cab_columns, next_no_value, "NO_DOC", "NO", "NO_ENTRADA", "DOCUMENTO")
    _assign_existing_values(header_values, cab_columns, 3, "ID_MOVIMIENTO", "CODIGO", "COD")
    _assign_existing_values(header_values, cab_columns, "Entrada de Mercancia", "DESCRIPCION", "DESCRIP", "DESCRIP_DOC", "DESCRIPCION_DOC")
    _assign_existing_values(header_values, cab_columns, asunto, "ASUNTO", "CONCEPTO")
    _assign_existing_values(header_values, cab_columns, departamento_ceco, "CECO", "CENTRO_COSTO")
    _assign_existing_values(header_values, cab_columns, departamento_descripcion, "DEPARTAMENTO")
    _assign_existing_values(header_values, cab_columns, proveedor_codigo, "ID_SN", "ID_PROV", "COD_PROV", "ID_PROVEEDOR", "PROVEEDOR")
    _assign_existing_values(header_values, cab_columns, proveedor_nombre, "NOM_SOCIO", "NOM_SN", "NOM_PROV", "NOMBRE_PROV", "NOM_SUPLIDOR", "NOMBRE")
    _assign_existing_values(header_values, cab_columns, fecha_cont, "FECHA_CONT", "FECHA", "FECHA_APLIC", "F_CONT")
    _assign_existing_values(header_values, cab_columns, fecha_venc, "FECHA_VENC", "FECHA_VENCE", "VENCIMIENTO")
    _assign_existing_values(header_values, cab_columns, fecha_doc, "FECHA_DOC")
    _assign_existing_values(header_values, cab_columns, comentario, "COMENTARIO", "OBSERVACION", "NOTA")
    _assign_existing_values(header_values, cab_columns, total_doc, "TOTAL_DOC", "TOTAL", "MONTO", "IMPORTE", "VALOR")
    _assign_existing_values(header_values, cab_columns, "Cerrado", "EST_DOC", "ESTATUS", "ESTADO")
    _assign_existing_values(header_values, cab_columns, usuario_id, "ID_USUARIO", "USUARIO_ID")
    _assign_existing_values(header_values, cab_columns, usuario_nombre, "USUARIO", "USUARIO_NOMBRE")
    _assign_existing_values(header_values, cab_columns, timezone.localdate(), "FECHA_CREACION")
    _assign_existing_values(header_values, cab_columns, timezone.localtime(), "FECHA_ACT")
    _assign_existing_values(header_values, cab_columns, str(fecha_cont.month).zfill(2), "PERIODO_CONT")
    _assign_existing_values(header_values, cab_columns, fecha_cont.year, "EJERCICIO")
    _assign_existing_values(header_values, cab_columns, "RD$", "MON_DOC", "MONEDA")
    _assign_existing_values(header_values, cab_columns, Decimal("1"), "TASAFACT", "TASA", "FACTOR", "TIPO_CAMBIO")
    _assign_existing_values(header_values, cab_columns, terminal, "TERMINAL")

    inserted_doc_value = _insert_dynamic_row(
        cursor,
        "CAB_ENT_INV",
        cab_columns,
        header_values,
        output_column=output_column,
        skip_columns=cab_identity_columns,
    )
    inserted_doc_text = _stringify_doc(inserted_doc_value)
    main_lookup_text = inserted_doc_text or no_value_text

    det_doc_col = _pick_existing_column(det_columns, "NO_DOC", "ID_DOC", "NO", "NO_ENTRADA", "ID_ENTRADA", "DOCUMENTO")
    det_line_col = _pick_existing_column(det_columns, "NO_LINEA", "LINEA", "NO_ITEM", "ORDEN", "ID_DETALLE")
    header_key_candidates = [
        column
        for column in [cab_doc_col, cab_no_col, "ID_DOC", "NO_DOC", "NO", "NO_ENTRADA", "ID_ENTRADA", "DOCUMENTO"]
        if column and column in cab_columns
    ]

    for detalle in cleaned_details:
        detail_values = {}
        if inserted_doc_text:
            _assign_existing_values(detail_values, det_columns, inserted_doc_text, "ID_DOC", "ID_ENTRADA")
        if main_lookup_text:
            _assign_existing_values(detail_values, det_columns, main_lookup_text, "NO_DOC", "NO", "NO_ENTRADA", "DOCUMENTO")
        if det_doc_col and det_doc_col not in detail_values and main_lookup_text:
            detail_values[det_doc_col] = main_lookup_text
        if det_line_col and det_line_col not in det_identity_columns:
            _assign_existing_values(detail_values, det_columns, detalle["linea"], det_line_col)
        _assign_existing_values(detail_values, det_columns, detalle["descripcion"], "DESCRIP_ART", "DESCRIPCION", "DESCRIP", "DESCRIP_ART_SERV")
        _assign_existing_values(detail_values, det_columns, detalle["id_articulo"], "ID_ARTICULO", "ARTICULO", "COD_ART", "CODIGO")
        _assign_existing_values(detail_values, det_columns, "Articulo", "CLASE_ART")
        _assign_existing_values(detail_values, det_columns, detalle["cant_emp"], "PORC_COM", "CANT_EMP", "CANT_EMPAQUE", "CANT_UND", "CANT_UNIDADES")
        _assign_existing_values(detail_values, det_columns, detalle["cantidad"], "CANTIDAD", "CANT")
        _assign_existing_values(detail_values, det_columns, detalle["uom"], "UOM", "U_MED", "UNIDAD")
        _assign_existing_values(detail_values, det_columns, detalle["uom"], "MEDIDA")
        _assign_existing_values(detail_values, det_columns, detalle["alm"], "ID_ALMACEN", "ALM", "ALMACEN", "ID_ALM")
        _assign_existing_values(detail_values, det_columns, detalle["pedido_cte"], "ID_CLIENTE", "PEDIDO_CTE", "PEDIDO", "NO_PEDIDO")
        _assign_existing_values(detail_values, det_columns, detalle["proyecto"], "CEBE", "PROYECTO", "ID_PROYECTO")
        _assign_existing_values(detail_values, det_columns, departamento_ceco, "CECO", "CENTRO_COSTO")
        _assign_existing_values(detail_values, det_columns, detalle["costo_unit"], "PRECIO", "COSTO_UNIT", "COSTO", "COSTO_UNITARIO")
        _assign_existing_values(detail_values, det_columns, detalle["valor"], "TOTAL_PRECIO", "VALOR", "TOTAL_LINEA", "TOTAL", "IMPORTE")
        _assign_existing_values(detail_values, det_columns, detalle["valor"], "TOTAL_PRECIO_NETO", "TOTAL_NETO")
        _assign_existing_values(detail_values, det_columns, detalle["cantidad"], "TOTAL_COSTO")
        _assign_existing_values(detail_values, det_columns, detalle["costo_unit"], "PRECIO_TRAS_DESC")
        _assign_existing_values(detail_values, det_columns, "11030101", "CTA_INV")
        _assign_existing_values(detail_values, det_columns, detalle["cuenta_mayor"], "CTA_AUM_STOCK", "CTA_MAYOR", "CUENTA_MAYOR", "CTA_LM", "CUENTA")
        _assign_existing_values(detail_values, det_columns, fecha_cont, "FECHA_CONT", "FECHA", "FECHA_APLIC")
        _assign_existing_values(detail_values, det_columns, fecha_doc, "FECHA_DOC")
        _assign_existing_values(detail_values, det_columns, fecha_venc, "FECHA_VENC", "FECHA_VENCE", "VENCIMIENTO")
        _assign_existing_values(detail_values, det_columns, proveedor_codigo, "ID_SN", "ID_PROV", "COD_PROV", "ID_PROVEEDOR", "PROVEEDOR")
        _assign_existing_values(detail_values, det_columns, proveedor_nombre, "NOM_SOCIO", "NOM_SN", "NOM_PROV", "NOMBRE_PROV", "NOM_SUPLIDOR", "NOMBRE")
        _assign_existing_values(detail_values, det_columns, "Cerrado", "EST_DOC", "ESTATUS", "ESTADO")
        _assign_existing_values(detail_values, det_columns, comentario, "COMENTARIO", "OBSERVACION")
        _assign_existing_values(detail_values, det_columns, usuario_id, "ID_USUARIO", "USUARIO_ID")
        _assign_existing_values(detail_values, det_columns, usuario_nombre, "USUARIO", "USUARIO_NOMBRE")
        _assign_existing_values(detail_values, det_columns, timezone.localdate(), "FECHA_CREACION")
        _assign_existing_values(detail_values, det_columns, timezone.localtime(), "FECHA_ACT")
        _assign_existing_values(detail_values, det_columns, str(fecha_cont.month).zfill(2), "PERIODO_CONT")
        _assign_existing_values(detail_values, det_columns, fecha_cont.year, "EJERCICIO")
        _insert_dynamic_row(
            cursor,
            "DET_ENT_INV",
            det_columns,
            detail_values,
            skip_columns=det_identity_columns,
        )

    stock_by_articulo = {}
    for detalle in cleaned_details:
        articulo_id = str(detalle.get("id_articulo") or "").strip()
        if not articulo_id:
            continue
        stock_by_articulo[articulo_id] = stock_by_articulo.get(articulo_id, Decimal("0")) + _to_decimal(detalle.get("cantidad"))

    for articulo_id, cantidad in stock_by_articulo.items():
        if cantidad.copy_abs() <= Decimal("0.0001"):
            continue
        cursor.execute(
            """
            UPDATE MAESTRO_ARTICULO
            SET STOCK = ISNULL(STOCK, 0) + %s,
                FECHA_ACT = GETDATE()
            WHERE ID_ARTICULO = %s
            """,
            [cantidad, articulo_id],
        )

    tarjetero_columns = _load_table_columns("TARJETERO")

    if tarjetero_columns:
        tarjetero_identity_columns = _load_identity_columns("TARJETERO")
        for detalle in cleaned_details:
            tarj_values = {}
            _assign_existing_values(tarj_values, tarjetero_columns, "EM", "TIPO_DOC", "TD", "CLASE_DOC", "TIPO")
            _assign_existing_values(tarj_values, tarjetero_columns, main_lookup_text, "ID_DOC", "NO_DOC", "NO", "DOCUMENTO")
            _assign_existing_values(tarj_values, tarjetero_columns, "3", "ID_SN", "ID_PROV", "COD_SN")
            _assign_existing_values(tarj_values, tarjetero_columns, "Entrada de Mercancia", "NOM_SN", "NOM_SOCIO", "NOMBRE_SN")
            _assign_existing_values(tarj_values, tarjetero_columns, detalle["id_articulo"], "ID_ARTICULO", "ARTICULO", "COD_ART")
            _assign_existing_values(tarj_values, tarjetero_columns, detalle["descripcion"], "DESCRIP_ART", "DESCRIPCION", "DESCRIP")
            _assign_existing_values(tarj_values, tarjetero_columns, detalle["cantidad"], "CANTIDAD", "CANT")
            _assign_existing_values(tarj_values, tarjetero_columns, detalle["costo_unit"], "COSTO", "COSTO_UNIT", "COSTO_UNITARIO")
            _assign_existing_values(tarj_values, tarjetero_columns, detalle["costo_unit"], "PRECIO", "PRECIO_UNIT", "PRECIO_UNITARIO")
            _assign_existing_values(tarj_values, tarjetero_columns, detalle["valor"], "TOTAL_COSTO", "TOTAL", "IMPORTE")
            _assign_existing_values(tarj_values, tarjetero_columns, detalle["valor"], "TOTAL_PRECIO", "TOTAL_NETO")
            _assign_existing_values(tarj_values, tarjetero_columns, "11030101", "CTA_INV", "CUENTA_INV")
            _assign_existing_values(tarj_values, tarjetero_columns, "No", "LOTE")
            _assign_existing_values(tarj_values, tarjetero_columns, fecha_cont, "FECHA_CONT", "F_CONT")
            _assign_existing_values(tarj_values, tarjetero_columns, fecha_venc, "FECHA_VENC", "F_VENC")
            _assign_existing_values(tarj_values, tarjetero_columns, fecha_doc, "FECHA_DOC", "F_DOC")
            _assign_existing_values(tarj_values, tarjetero_columns, timezone.localdate(), "FECHA_CREACION")
            _assign_existing_values(tarj_values, tarjetero_columns, "RD$", "MONEDA", "MON_DOC")
            _assign_existing_values(tarj_values, tarjetero_columns, 1, "ID_ALMACEN", "ALM", "ALMACEN")
            _assign_existing_values(tarj_values, tarjetero_columns, usuario_id, "ID_USUARIO", "USUARIO_ID")
            _assign_existing_values(tarj_values, tarjetero_columns, int(fecha_cont.month), "PERIODO_CONT")
            _assign_existing_values(tarj_values, tarjetero_columns, int(fecha_cont.year), "EJERCICIO")
            _insert_dynamic_row(
                cursor,
                "TARJETERO",
                tarjetero_columns,
                tarj_values,
                skip_columns=tarjetero_identity_columns,
            )

    no_ed_value = _create_entrada_ed_entries(
        cursor,
        origen_doc=main_lookup_text,
        fecha_cont=fecha_cont,
        fecha_doc=fecha_doc,
        fecha_venc=fecha_venc,
        total_doc=total_doc,
        comentario=comentario,
        usuario_id=usuario_id,
        usuario_nombre=usuario_nombre,
        terminal=terminal,
        departamento_ceco=departamento_ceco,
    )

    if no_ed_value:
        no_ed_col = _pick_existing_column(cab_columns, "NO_ED")
        header_key_col = next((column for column in header_key_candidates if column != no_ed_col), None)
        header_key_value = None
        if header_key_col in {"ID_DOC", "ID_ENTRADA"}:
            header_key_value = inserted_doc_text or main_lookup_text
        else:
            header_key_value = main_lookup_text or inserted_doc_text
        if no_ed_col and header_key_col and header_key_value:
            cursor.execute(
                f"UPDATE CAB_ENT_INV SET [{no_ed_col}] = %s WHERE [{header_key_col}] = %s",
                [no_ed_value, header_key_value],
            )

    mov_doc_columns = _load_table_columns("MOV_DOC")
    if mov_doc_columns:
        mov_doc_identity_columns = _load_identity_columns("MOV_DOC")
        mov_values = {}
        _assign_existing_values(mov_values, mov_doc_columns, "EM", "TIPO_DOC", "TD", "CLASE_DOC", "TIPO")
        _assign_existing_values(mov_values, mov_doc_columns, main_lookup_text, "ID_DOC", "NO_DOC", "NO", "DOCUMENTO")
        _assign_existing_values(mov_values, mov_doc_columns, "-1", "ID_SN", "ID_PROV", "COD_SN")
        _assign_existing_values(mov_values, mov_doc_columns, fecha_cont, "FECHA_CONT", "F_CONT")
        _assign_existing_values(mov_values, mov_doc_columns, fecha_venc, "FECHA_VENC", "F_VENC")
        _assign_existing_values(mov_values, mov_doc_columns, fecha_doc, "FECHA_DOC", "F_DOC")
        _assign_existing_values(mov_values, mov_doc_columns, "-1", "REF_DOC_BASE", "DOC_BASE", "ID_DOC_BASE")
        _assign_existing_values(mov_values, mov_doc_columns, Decimal("0"), "TOTAL_BASE", "BASE")
        _assign_existing_values(mov_values, mov_doc_columns, "Cerrado", "EST_DOC", "ESTATUS", "ESTADO")
        _assign_existing_values(mov_values, mov_doc_columns, total_doc, "TOTAL_DOC", "TOTAL", "MONTO", "IMPORTE", "VALOR")
        _assign_existing_values(mov_values, mov_doc_columns, "RD$", "MON_DOC", "MONEDA")
        _assign_existing_values(mov_values, mov_doc_columns, "-1", "NO_RECIBO", "ID_RECIBO")
        _assign_existing_values(mov_values, mov_doc_columns, datetime(1900, 1, 1), "FECHA_REC", "FECHA_RECIBO")
        _assign_existing_values(mov_values, mov_doc_columns, no_ed_value or "", "NO_ED", "ID_ED")
        _assign_existing_values(mov_values, mov_doc_columns, int(fecha_cont.month), "PERIODO_CONT")
        _assign_existing_values(mov_values, mov_doc_columns, int(fecha_cont.year), "EJERCICIO")
        _assign_existing_values(mov_values, mov_doc_columns, usuario_id, "ID_USUARIO", "USUARIO_ID")
        _assign_existing_values(mov_values, mov_doc_columns, "-1", "ID_VENDEDOR", "VENDEDOR")
        _assign_existing_values(mov_values, mov_doc_columns, None, "ID_CONDICION", "CONDICION")
        _assign_existing_values(mov_values, mov_doc_columns, "-1", "DIA")
        _assign_existing_values(mov_values, mov_doc_columns, "N", "CANCELADO", "ANULADO")
        _assign_existing_values(mov_values, mov_doc_columns, terminal, "TERMINAL")
        _assign_existing_values(mov_values, mov_doc_columns, "Entrada de Mercancia", "COMENTARIO", "OBSERVACION", "NOTA")
        _insert_dynamic_row(
            cursor,
            "MOV_DOC",
            mov_doc_columns,
            mov_values,
            skip_columns=mov_doc_identity_columns,
        )

    return main_lookup_text


SALIDA_CAB_TABLE_CANDIDATES = ("CAB_SAL_INV",)
SALIDA_DET_TABLE_CANDIDATES = ("DET_SAL_INV",)


def _resolve_existing_table_name(*candidates):
    for candidate in candidates:
        table_name = str(candidate or "").strip().upper()
        if not table_name:
            continue
        if _load_table_columns(table_name):
            return table_name
    for candidate in candidates:
        table_name = str(candidate or "").strip().upper()
        if table_name:
            return table_name
    return ""


def _get_salida_inventory_tables():
    return (
        _resolve_existing_table_name(*SALIDA_CAB_TABLE_CANDIDATES),
        _resolve_existing_table_name(*SALIDA_DET_TABLE_CANDIDATES),
    )


def _load_inventory_articulos_search_rows(cab_table, *, query="", filtro="documento"):
    cab_columns = _load_table_columns(cab_table)
    if not cab_columns:
        return []

    doc_col = _pick_existing_column(cab_columns, "NO_DOC", "ID_DOC", "NO", "NO_ENTRADA", "ID_ENTRADA", "NO_SALIDA", "ID_SALIDA", "DOCUMENTO")
    no_col = _pick_existing_column(cab_columns, "NO", "ID_ENTRADA", "NO_ENTRADA", "ID_SALIDA", "NO_SALIDA", doc_col)
    codigo_col = _pick_existing_column(cab_columns, "ID_MOVIMIENTO", "CODIGO", "COD", "COD_DOC", "COD_TIPO", "TIPO_DOC")
    descripcion_col = _pick_existing_column(cab_columns, "DESCRIPCION", "DESCRIP", "DESCRIP_DOC", "DESCRIPCION_DOC")
    asunto_col = _pick_existing_column(cab_columns, "ASUNTO", "CONCEPTO", "REFERENCIA")
    proveedor_codigo_col = _pick_existing_column(cab_columns, "ID_SN", "ID_PROV", "COD_PROV", "ID_PROVEEDOR", "PROVEEDOR")
    proveedor_nombre_col = _pick_existing_column(cab_columns, "NOM_SOCIO", "NOM_SN", "NOM_PROV", "NOMBRE_PROV", "NOM_SUPLIDOR", "NOMBRE")
    estado_col = _pick_existing_column(cab_columns, "EST_DOC", "ESTATUS", "ESTADO")
    fecha_col = _pick_existing_column(cab_columns, "FECHA_DOC", "FECHA_CONT", "FECHA", "FECHA_APLIC")
    total_col = _pick_existing_column(cab_columns, "TOTAL_DOC", "TOTAL", "MONTO", "IMPORTE", "VALOR")

    select_columns = [
        col
        for col in [doc_col, no_col, codigo_col, descripcion_col, asunto_col, proveedor_codigo_col, proveedor_nombre_col, estado_col, fecha_col, total_col]
        if col
    ]
    select_columns = list(dict.fromkeys(select_columns))
    if not select_columns:
        return []

    sql = "SELECT TOP 80 " + ", ".join(f"[{column}]" for column in select_columns) + f" FROM {cab_table}"
    params = []
    query_text = str(query or "").strip()
    if query_text:
        filtro = str(filtro or "documento").strip().lower()
        if filtro == "documento":
            search_columns = [doc_col, no_col]
        elif filtro == "codigo":
            search_columns = [codigo_col, proveedor_codigo_col]
        elif filtro == "descripcion":
            search_columns = [descripcion_col, asunto_col]
        elif filtro == "proveedor":
            search_columns = [proveedor_codigo_col, proveedor_nombre_col]
        else:
            search_columns = [doc_col, no_col, codigo_col, descripcion_col, asunto_col, proveedor_codigo_col, proveedor_nombre_col]
        search_columns = [column for column in search_columns if column]
        if search_columns:
            sql += " WHERE (" + " OR ".join(f"CAST([{column}] AS NVARCHAR(255)) LIKE %s" for column in search_columns) + ")"
            params.extend([f"%{query_text}%"] * len(search_columns))

    if doc_col:
        sql += f" ORDER BY TRY_CAST([{doc_col}] AS BIGINT) DESC, CAST([{doc_col}] AS NVARCHAR(255)) DESC"
    elif no_col:
        sql += f" ORDER BY TRY_CAST([{no_col}] AS BIGINT) DESC, CAST([{no_col}] AS NVARCHAR(255)) DESC"

    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        columns = [col[0] for col in cursor.description]
        rows = cursor.fetchall()

    results = []
    for raw_row in rows:
        row = _normalize_result_row(columns, raw_row)
        no_doc = _stringify_doc(_pick_row_value(row, doc_col, no_col, default=""))
        codigo = str(_pick_row_value(row, codigo_col, default="", allow_blank=True) or "").strip()
        descripcion = str(_pick_row_value(row, descripcion_col, asunto_col, default="", allow_blank=True) or "").strip()
        proveedor_codigo = str(_pick_row_value(row, proveedor_codigo_col, default="", allow_blank=True) or "").strip()
        proveedor_nombre = str(_pick_row_value(row, proveedor_nombre_col, default="", allow_blank=True) or "").strip()
        estado = str(_pick_row_value(row, estado_col, default="", allow_blank=True) or "").strip()
        total_doc = _to_decimal(_pick_row_value(row, total_col, default=Decimal("0")))
        results.append(
            {
                "no_doc": no_doc,
                "codigo": codigo,
                "descripcion": descripcion,
                "proveedor_codigo": proveedor_codigo,
                "proveedor_nombre": proveedor_nombre,
                "fecha_doc": _fmt_date_flexible(_pick_row_value(row, fecha_col, default="")),
                "estado": estado,
                "total_doc": float(total_doc),
                "total_doc_fmt": _format_money(total_doc),
            }
        )
    return results


def _load_inventory_articulos_record(cab_table, det_table, lookup_value):
    lookup_text = str(lookup_value or "").strip()
    if not lookup_text:
        return None

    cab_columns = _load_table_columns(cab_table)
    det_columns = _load_table_columns(det_table)
    if not cab_columns:
        return None

    cab_doc_col = _pick_existing_column(cab_columns, "NO_DOC", "ID_DOC", "NO", "NO_ENTRADA", "ID_ENTRADA", "NO_SALIDA", "ID_SALIDA", "DOCUMENTO")
    cab_no_col = _pick_existing_column(cab_columns, "NO", "ID_ENTRADA", "NO_ENTRADA", "ID_SALIDA", "NO_SALIDA", cab_doc_col)
    if not cab_doc_col and not cab_no_col:
        return None

    where_sql, where_params = _build_doc_lookup_where([cab_doc_col, cab_no_col], lookup_text)
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT TOP 1 * FROM {cab_table} WHERE {where_sql}", where_params)
        raw_header = cursor.fetchone()
        if not raw_header:
            return None
        header_columns = [col[0] for col in cursor.description]
        header_row = _normalize_result_row(header_columns, raw_header)

    codigo_col = _pick_existing_column(cab_columns, "ID_MOVIMIENTO", "CODIGO", "COD", "COD_DOC", "COD_TIPO", "TIPO_DOC")
    descripcion_col = _pick_existing_column(cab_columns, "DESCRIPCION", "DESCRIP", "DESCRIP_DOC", "DESCRIPCION_DOC")
    asunto_col = _pick_existing_column(cab_columns, "ASUNTO", "CONCEPTO", "REFERENCIA")
    departamento_col = _pick_existing_column(cab_columns, "DEPARTAMENTO", "DEPTO", "DPTO")
    ceco_col = _pick_existing_column(cab_columns, "CECO", "CENTRO_COSTO")
    proveedor_codigo_col = _pick_existing_column(cab_columns, "ID_SN", "ID_PROV", "COD_PROV", "ID_PROVEEDOR", "PROVEEDOR")
    proveedor_nombre_col = _pick_existing_column(cab_columns, "NOM_SOCIO", "NOM_SN", "NOM_PROV", "NOMBRE_PROV", "NOM_SUPLIDOR", "NOMBRE")
    estado_col = _pick_existing_column(cab_columns, "EST_DOC", "ESTATUS", "ESTADO")
    fecha_cont_col = _pick_existing_column(cab_columns, "FECHA_CONT", "FECHA", "FECHA_APLIC", "F_CONT")
    fecha_venc_col = _pick_existing_column(cab_columns, "FECHA_VENC", "FECHA_VENCE", "VENCIMIENTO")
    fecha_doc_col = _pick_existing_column(cab_columns, "FECHA_DOC", "FECHA", "FECHA_CONT")
    comentario_col = _pick_existing_column(cab_columns, "COMENTARIO", "OBSERVACION", "NOTA")
    total_col = _pick_existing_column(cab_columns, "TOTAL_DOC", "TOTAL", "MONTO", "IMPORTE", "VALOR")

    document_values = _unique_preserve(
        _pick_row_value(header_row, cab_doc_col, default=""),
        _pick_row_value(header_row, cab_no_col, default=""),
        lookup_text,
    )

    detalles = []
    if det_columns:
        det_doc_col = _pick_existing_column(det_columns, "NO_DOC", "ID_DOC", "NO", "NO_ENTRADA", "ID_ENTRADA", "NO_SALIDA", "ID_SALIDA", "DOCUMENTO")
        det_no_col = _pick_existing_column(det_columns, "NO", "ID_ENTRADA", "NO_ENTRADA", "ID_SALIDA", "NO_SALIDA")
        det_line_col = _pick_existing_column(det_columns, "NO_LINEA", "LINEA", "NO_ITEM", "ORDEN", "ID_DETALLE")
        det_desc_col = _pick_existing_column(det_columns, "DESCRIP_ART", "DESCRIPCION", "DESCRIP", "DESCRIP_ART_SERV")
        det_art_col = _pick_existing_column(det_columns, "ID_ARTICULO", "ARTICULO", "COD_ART", "CODIGO")
        det_cant_emp_col = _pick_existing_column(det_columns, "PORC_COM", "CANT_EMP", "CANT_EMPAQUE", "CANT_UND", "CANT_UNIDADES")
        det_cantidad_col = _pick_existing_column(det_columns, "CANTIDAD", "CANT")
        det_uom_col = _pick_existing_column(det_columns, "MEDIDA", "UOM", "U_MED", "UNIDAD")
        det_alm_col = _pick_existing_column(det_columns, "ID_ALMACEN", "ALM", "ALMACEN", "ID_ALM")
        det_pedido_col = _pick_existing_column(det_columns, "ID_CLIENTE", "PEDIDO_CTE", "PEDIDO", "NO_PEDIDO")
        det_proyecto_col = _pick_existing_column(det_columns, "CEBE", "PROYECTO", "ID_PROYECTO")
        det_ceco_col = _pick_existing_column(det_columns, "CECO", "CENTRO_COSTO")
        det_costo_col = _pick_existing_column(det_columns, "PRECIO", "COSTO_UNIT", "COSTO", "COSTO_UNITARIO")
        det_valor_col = _pick_existing_column(det_columns, "TOTAL_PRECIO", "VALOR", "TOTAL_LINEA", "TOTAL", "IMPORTE")
        det_cta_col = _pick_existing_column(det_columns, "CTA_DISM_STOCK", "CTA_AUM_STOCK", "CTA_MAYOR", "CUENTA_MAYOR", "CTA_LM", "CUENTA")

        if det_doc_col or det_no_col:
            where_sql, where_params = _build_multi_lookup_where([det_doc_col, det_no_col], document_values)
            with connection.cursor() as cursor:
                cursor.execute(f"SELECT * FROM {det_table} WHERE {where_sql}", where_params)
                detail_columns = [col[0] for col in cursor.description]
                detail_rows = cursor.fetchall()
            parsed_rows = []
            for raw_row in detail_rows:
                row = _normalize_result_row(detail_columns, raw_row)
                parsed_rows.append(
                    {
                        "linea": _stringify_doc(_pick_row_value(row, det_line_col, default="")),
                        "descripcion": str(_pick_row_value(row, det_desc_col, default="", allow_blank=True) or "").strip(),
                        "articulo": str(_pick_row_value(row, det_art_col, default="", allow_blank=True) or "").strip(),
                        "cant_emp": str(_pick_row_value(row, det_cant_emp_col, default="", allow_blank=True) or "").strip(),
                        "cantidad": _format_money(_pick_row_value(row, det_cantidad_col, default=Decimal("0"))),
                        "uom": str(_pick_row_value(row, det_uom_col, default="", allow_blank=True) or "").strip(),
                        "alm": str(_pick_row_value(row, det_alm_col, default="", allow_blank=True) or "").strip(),
                        "pedido_cte": str(_pick_row_value(row, det_pedido_col, default="", allow_blank=True) or "").strip(),
                        "proyecto": str(_pick_row_value(row, det_proyecto_col, default="", allow_blank=True) or "").strip(),
                        "ceco": str(_pick_row_value(row, det_ceco_col, default="", allow_blank=True) or "").strip(),
                        "costo_unit": _format_money(_pick_row_value(row, det_costo_col, default=Decimal("0"))),
                        "valor": _format_money(_pick_row_value(row, det_valor_col, default=Decimal("0"))),
                        "cuenta_mayor": str(_pick_row_value(row, det_cta_col, default="", allow_blank=True) or "").strip(),
                    }
                )
            detalles = sorted(
                parsed_rows,
                key=lambda item: (
                    Decimal(item["linea"]) if str(item["linea"]).replace(".", "", 1).isdigit() else Decimal("999999"),
                    item["linea"],
                ),
            )

    total_doc = _to_decimal(_pick_row_value(header_row, total_col, default=Decimal("0")))
    if total_doc == Decimal("0") and detalles:
        total_doc = sum((_to_decimal(row.get("valor")) for row in detalles), Decimal("0"))

    departamento_ceco = str(_pick_row_value(header_row, ceco_col, default="", allow_blank=True) or "").strip()
    departamento_descripcion = _resolve_departamento_descripcion(
        departamento_ceco,
        fallback=_pick_row_value(header_row, departamento_col, default="", allow_blank=True) or "",
    )

    return {
        "entry": {
            "lookup": lookup_text,
            "no": _stringify_doc(_pick_row_value(header_row, cab_no_col, cab_doc_col, default="")),
            "no_doc": _stringify_doc(_pick_row_value(header_row, cab_doc_col, cab_no_col, default="")),
            "codigo": str(_pick_row_value(header_row, codigo_col, default="", allow_blank=True) or "").strip(),
            "descripcion": str(_pick_row_value(header_row, descripcion_col, default="", allow_blank=True) or "").strip(),
            "asunto": str(_pick_row_value(header_row, asunto_col, default="", allow_blank=True) or "").strip(),
            "departamento": departamento_descripcion,
            "departamento_ceco": departamento_ceco,
            "proveedor_codigo": str(_pick_row_value(header_row, proveedor_codigo_col, default="", allow_blank=True) or "").strip(),
            "proveedor_nombre": str(_pick_row_value(header_row, proveedor_nombre_col, default="", allow_blank=True) or "").strip(),
            "estado": str(_pick_row_value(header_row, estado_col, default="", allow_blank=True) or "").strip(),
            "fecha_cont": _fmt_date_input(_pick_row_value(header_row, fecha_cont_col, default="")),
            "fecha_venc": _fmt_date_input(_pick_row_value(header_row, fecha_venc_col, default="")),
            "fecha_doc": _fmt_date_input(_pick_row_value(header_row, fecha_doc_col, default="")),
            "comentario": str(_pick_row_value(header_row, comentario_col, default="", allow_blank=True) or "").strip(),
            "total_doc": _format_money(total_doc),
        },
        "detalles": detalles,
    }


def _persist_inventory_articulos_record(
    cursor,
    *,
    payload,
    usuario_id,
    usuario_nombre,
    terminal,
    cab_table,
    det_table,
    default_description,
    movement_code,
    stock_multiplier,
):
    cab_columns = _load_table_columns(cab_table)
    det_columns = _load_table_columns(det_table)
    if not cab_columns or not det_columns:
        raise ValueError(f"No se pudieron cargar las tablas {cab_table}/{det_table}.")

    cab_identity_columns = _load_identity_columns(cab_table)
    det_identity_columns = _load_identity_columns(det_table)

    detalles = [detalle for detalle in (payload.get("detalles") or []) if isinstance(detalle, dict)]
    if not detalles:
        raise ValueError("Debes agregar al menos una linea en el detalle.")

    departamento_ceco = str(payload.get("departamento_ceco") or "").strip()
    departamentos = _load_departamento_rows()
    if departamentos and not departamento_ceco:
        raise ValueError("Debes seleccionar un departamento.")
    departamento_descripcion = _resolve_departamento_descripcion(departamento_ceco)

    proveedor_codigo = str(payload.get("proveedor_codigo") or "").strip() or "-1"
    proveedor_nombre = str(payload.get("proveedor_nombre") or "").strip()

    fecha_cont = _to_date_or_none(payload.get("fecha_cont")) or timezone.localdate()
    fecha_doc = _to_date_or_none(payload.get("fecha_doc")) or fecha_cont
    fecha_venc = _to_date_or_none(payload.get("fecha_venc")) or fecha_doc
    asunto = str(payload.get("asunto") or "").strip()
    comentario = str(payload.get("comentario") or "").strip() or default_description

    cab_doc_col = _pick_existing_column(cab_columns, "NO_DOC", "ID_DOC", "NO", "NO_ENTRADA", "ID_ENTRADA", "NO_SALIDA", "ID_SALIDA", "DOCUMENTO")
    cab_no_col = _pick_existing_column(cab_columns, "NO", "ID_ENTRADA", "NO_ENTRADA", "ID_SALIDA", "NO_SALIDA", cab_doc_col)
    output_column = cab_doc_col or cab_no_col
    sequence_candidates = [
        column
        for column in [cab_no_col, cab_doc_col, "NO_DOC", "NO", "NO_ENTRADA", "ID_ENTRADA", "NO_SALIDA", "ID_SALIDA", "ID_DOC"]
        if column and column in cab_columns and column not in cab_identity_columns
    ]
    next_no_value = _next_table_numeric_value(cursor, cab_table, sequence_candidates[0]) if sequence_candidates else None
    no_value_text = _stringify_doc(next_no_value) if next_no_value is not None else ""

    total_doc = Decimal("0")
    cleaned_details = []
    for index, detalle in enumerate(detalles, start=1):
        id_articulo = str(detalle.get("id_articulo") or "").strip()
        descripcion = str(detalle.get("descripcion") or "").strip()
        if not id_articulo and not descripcion:
            continue
        if not id_articulo:
            raise ValueError(f"La linea {index} no tiene articulo seleccionado.")
        cantidad = _to_decimal(detalle.get("cantidad"))
        if cantidad <= Decimal("0"):
            raise ValueError(f"La cantidad de la linea {index} debe ser mayor que cero.")
        costo_unit = _to_decimal(detalle.get("costo_unit"))
        valor = _to_decimal(detalle.get("valor"))
        if valor <= Decimal("0"):
            valor = cantidad * costo_unit
        total_doc += valor
        cleaned_details.append(
            {
                "linea": index,
                "id_articulo": id_articulo,
                "descripcion": descripcion,
                "cant_emp": _to_decimal(detalle.get("cant_emp")),
                "cantidad": cantidad,
                "uom": str(detalle.get("uom") or "").strip(),
                "alm": str(detalle.get("alm") or "").strip(),
                "pedido_cte": str(detalle.get("pedido_cte") or "").strip(),
                "proyecto": str(detalle.get("proyecto") or "").strip(),
                "ceco": str(detalle.get("ceco") or "").strip() or departamento_ceco,
                "costo_unit": costo_unit,
                "valor": valor,
                "cuenta_mayor": str(detalle.get("cuenta_mayor") or "").strip(),
            }
        )

    if not cleaned_details:
        raise ValueError("Debes agregar al menos una linea con articulo.")

    header_values = {}
    if next_no_value is not None:
        _assign_existing_values(header_values, cab_columns, next_no_value, "ID_DOC", "ID_ENTRADA", "ID_SALIDA")
        _assign_existing_values(header_values, cab_columns, next_no_value, "NO_DOC", "NO", "NO_ENTRADA", "NO_SALIDA", "DOCUMENTO")
    _assign_existing_values(header_values, cab_columns, movement_code, "ID_MOVIMIENTO", "CODIGO", "COD")
    _assign_existing_values(header_values, cab_columns, default_description, "DESCRIPCION", "DESCRIP", "DESCRIP_DOC", "DESCRIPCION_DOC")
    _assign_existing_values(header_values, cab_columns, asunto, "ASUNTO", "CONCEPTO")
    _assign_existing_values(header_values, cab_columns, departamento_ceco, "CECO", "CENTRO_COSTO")
    _assign_existing_values(header_values, cab_columns, departamento_descripcion, "DEPARTAMENTO")
    _assign_existing_values(header_values, cab_columns, proveedor_codigo, "ID_SN", "ID_PROV", "COD_PROV", "ID_PROVEEDOR", "PROVEEDOR")
    _assign_existing_values(header_values, cab_columns, proveedor_nombre, "NOM_SOCIO", "NOM_SN", "NOM_PROV", "NOMBRE_PROV", "NOM_SUPLIDOR", "NOMBRE")
    _assign_existing_values(header_values, cab_columns, fecha_cont, "FECHA_CONT", "FECHA", "FECHA_APLIC", "F_CONT")
    _assign_existing_values(header_values, cab_columns, fecha_venc, "FECHA_VENC", "FECHA_VENCE", "VENCIMIENTO")
    _assign_existing_values(header_values, cab_columns, fecha_doc, "FECHA_DOC")
    _assign_existing_values(header_values, cab_columns, comentario, "COMENTARIO", "OBSERVACION", "NOTA")
    _assign_existing_values(header_values, cab_columns, total_doc, "TOTAL_DOC", "TOTAL", "MONTO", "IMPORTE", "VALOR")
    _assign_existing_values(header_values, cab_columns, "Cerrado", "EST_DOC", "ESTATUS", "ESTADO")
    _assign_existing_values(header_values, cab_columns, usuario_id, "ID_USUARIO", "USUARIO_ID")
    _assign_existing_values(header_values, cab_columns, usuario_nombre, "USUARIO", "USUARIO_NOMBRE")
    _assign_existing_values(header_values, cab_columns, timezone.localdate(), "FECHA_CREACION")
    _assign_existing_values(header_values, cab_columns, timezone.localtime(), "FECHA_ACT")
    _assign_existing_values(header_values, cab_columns, str(fecha_cont.month).zfill(2), "PERIODO_CONT")
    _assign_existing_values(header_values, cab_columns, fecha_cont.year, "EJERCICIO")
    _assign_existing_values(header_values, cab_columns, "RD$", "MON_DOC", "MONEDA")
    _assign_existing_values(header_values, cab_columns, Decimal("1"), "TASAFACT", "TASA", "FACTOR", "TIPO_CAMBIO")
    _assign_existing_values(header_values, cab_columns, terminal, "TERMINAL")

    inserted_doc_value = _insert_dynamic_row(
        cursor,
        cab_table,
        cab_columns,
        header_values,
        output_column=output_column,
        skip_columns=cab_identity_columns,
    )
    inserted_doc_text = _stringify_doc(inserted_doc_value)
    main_lookup_text = inserted_doc_text or no_value_text

    det_doc_col = _pick_existing_column(det_columns, "NO_DOC", "ID_DOC", "NO", "NO_ENTRADA", "ID_ENTRADA", "NO_SALIDA", "ID_SALIDA", "DOCUMENTO")
    det_line_col = _pick_existing_column(det_columns, "NO_LINEA", "LINEA", "NO_ITEM", "ORDEN", "ID_DETALLE")
    for detalle in cleaned_details:
        detail_values = {}
        if inserted_doc_text:
            _assign_existing_values(detail_values, det_columns, inserted_doc_text, "ID_DOC", "ID_ENTRADA", "ID_SALIDA")
        if main_lookup_text:
            _assign_existing_values(detail_values, det_columns, main_lookup_text, "NO_DOC", "NO", "NO_ENTRADA", "NO_SALIDA", "DOCUMENTO")
        if det_doc_col and det_doc_col not in detail_values and main_lookup_text:
            detail_values[det_doc_col] = main_lookup_text
        if det_line_col and det_line_col not in det_identity_columns:
            _assign_existing_values(detail_values, det_columns, detalle["linea"], det_line_col)
        _assign_existing_values(detail_values, det_columns, detalle["descripcion"], "DESCRIP_ART", "DESCRIPCION", "DESCRIP", "DESCRIP_ART_SERV")
        _assign_existing_values(detail_values, det_columns, detalle["id_articulo"], "ID_ARTICULO", "ARTICULO", "COD_ART", "CODIGO")
        _assign_existing_values(detail_values, det_columns, "Articulo", "CLASE_ART")
        _assign_existing_values(detail_values, det_columns, detalle["cant_emp"], "PORC_COM", "CANT_EMP", "CANT_EMPAQUE", "CANT_UND", "CANT_UNIDADES")
        _assign_existing_values(detail_values, det_columns, detalle["cantidad"], "CANTIDAD", "CANT")
        _assign_existing_values(detail_values, det_columns, detalle["uom"], "UOM", "U_MED", "UNIDAD")
        _assign_existing_values(detail_values, det_columns, detalle["uom"], "MEDIDA")
        _assign_existing_values(detail_values, det_columns, detalle["alm"], "ID_ALMACEN", "ALM", "ALMACEN", "ID_ALM")
        _assign_existing_values(detail_values, det_columns, detalle["pedido_cte"], "ID_CLIENTE", "PEDIDO_CTE", "PEDIDO", "NO_PEDIDO")
        _assign_existing_values(detail_values, det_columns, detalle["proyecto"], "CEBE", "PROYECTO", "ID_PROYECTO")
        _assign_existing_values(detail_values, det_columns, detalle["ceco"], "CECO", "CENTRO_COSTO")
        _assign_existing_values(detail_values, det_columns, detalle["costo_unit"], "PRECIO", "COSTO_UNIT", "COSTO", "COSTO_UNITARIO")
        _assign_existing_values(detail_values, det_columns, detalle["valor"], "TOTAL_PRECIO", "VALOR", "TOTAL_LINEA", "TOTAL", "IMPORTE")
        _assign_existing_values(detail_values, det_columns, "11030101", "CTA_INV")
        _assign_existing_values(detail_values, det_columns, detalle["cuenta_mayor"], "CTA_DISM_STOCK", "CTA_AUM_STOCK", "CTA_MAYOR", "CUENTA_MAYOR", "CTA_LM", "CUENTA")
        _assign_existing_values(detail_values, det_columns, fecha_cont, "FECHA_CONT", "FECHA", "FECHA_APLIC")
        _assign_existing_values(detail_values, det_columns, fecha_doc, "FECHA_DOC")
        _assign_existing_values(detail_values, det_columns, fecha_venc, "FECHA_VENC", "FECHA_VENCE", "VENCIMIENTO")
        _assign_existing_values(detail_values, det_columns, proveedor_codigo, "ID_SN", "ID_PROV", "COD_PROV", "ID_PROVEEDOR", "PROVEEDOR")
        _assign_existing_values(detail_values, det_columns, proveedor_nombre, "NOM_SOCIO", "NOM_SN", "NOM_PROV", "NOMBRE_PROV", "NOM_SUPLIDOR", "NOMBRE")
        _assign_existing_values(detail_values, det_columns, "Cerrado", "EST_DOC", "ESTATUS", "ESTADO")
        _assign_existing_values(detail_values, det_columns, comentario, "COMENTARIO", "OBSERVACION")
        _assign_existing_values(detail_values, det_columns, usuario_id, "ID_USUARIO", "USUARIO_ID")
        _assign_existing_values(detail_values, det_columns, usuario_nombre, "USUARIO", "USUARIO_NOMBRE")
        _assign_existing_values(detail_values, det_columns, timezone.localdate(), "FECHA_CREACION")
        _assign_existing_values(detail_values, det_columns, timezone.localtime(), "FECHA_ACT")
        _assign_existing_values(detail_values, det_columns, str(fecha_cont.month).zfill(2), "PERIODO_CONT")
        _assign_existing_values(detail_values, det_columns, fecha_cont.year, "EJERCICIO")
        _insert_dynamic_row(
            cursor,
            det_table,
            det_columns,
            detail_values,
            skip_columns=det_identity_columns,
        )

    stock_by_articulo = {}
    for detalle in cleaned_details:
        articulo_id = str(detalle.get("id_articulo") or "").strip()
        if not articulo_id:
            continue
        stock_by_articulo[articulo_id] = stock_by_articulo.get(articulo_id, Decimal("0")) + _to_decimal(detalle.get("cantidad"))

    for articulo_id, cantidad in stock_by_articulo.items():
        if cantidad.copy_abs() <= Decimal("0.0001"):
            continue
        cursor.execute(
            """
            UPDATE MAESTRO_ARTICULO
            SET STOCK = ISNULL(STOCK, 0) + %s,
                FECHA_ACT = GETDATE()
            WHERE ID_ARTICULO = %s
            """,
            [cantidad * Decimal(str(stock_multiplier)), articulo_id],
        )

    if det_table.upper() == "DET_SAL_INV":
        tarjetero_columns = _load_table_columns("TARJETERO")
        if tarjetero_columns:
            tarjetero_identity_columns = _load_identity_columns("TARJETERO")
            for detalle in cleaned_details:
                cantidad_neg = -_to_decimal(detalle.get("cantidad"))
                total_costo_t = cantidad_neg
                costo_t = _to_decimal(detalle.get("costo_unit"))
                total_precio_t = total_costo_t * costo_t
                tarj_values = {}
                _assign_existing_values(tarj_values, tarjetero_columns, "SM", "TIPO_DOC", "TD", "CLASE_DOC", "TIPO")
                _assign_existing_values(tarj_values, tarjetero_columns, main_lookup_text, "ID_DOC", "NO_DOC", "NO", "DOCUMENTO")
                _assign_existing_values(tarj_values, tarjetero_columns, "4", "ID_SN", "ID_PROV", "COD_SN")
                _assign_existing_values(tarj_values, tarjetero_columns, "Salida de Mercancia", "NOM_SN", "NOM_SOCIO", "NOMBRE_SN")
                _assign_existing_values(tarj_values, tarjetero_columns, detalle["id_articulo"], "ID_ARTICULO", "ARTICULO", "COD_ART")
                _assign_existing_values(tarj_values, tarjetero_columns, detalle["descripcion"], "DESCRIP_ART", "DESCRIPCION", "DESCRIP")
                _assign_existing_values(tarj_values, tarjetero_columns, cantidad_neg, "CANTIDAD", "CANT")
                _assign_existing_values(tarj_values, tarjetero_columns, total_costo_t, "TOTAL_COSTO", "TOTAL", "IMPORTE")
                _assign_existing_values(tarj_values, tarjetero_columns, costo_t, "COSTO", "COSTO_UNIT", "COSTO_UNITARIO")
                _assign_existing_values(tarj_values, tarjetero_columns, costo_t, "PRECIO", "PRECIO_UNIT", "PRECIO_UNITARIO")
                _assign_existing_values(tarj_values, tarjetero_columns, total_precio_t, "TOTAL_PRECIO", "TOTAL_NETO")
                _assign_existing_values(tarj_values, tarjetero_columns, "11030101", "CTA_INV", "CUENTA_INV")
                _assign_existing_values(tarj_values, tarjetero_columns, "No", "LOTE")
                _assign_existing_values(tarj_values, tarjetero_columns, fecha_cont, "FECHA_CONT", "F_CONT")
                _assign_existing_values(tarj_values, tarjetero_columns, fecha_venc, "FECHA_VENC", "F_VENC")
                _assign_existing_values(tarj_values, tarjetero_columns, fecha_doc, "FECHA_DOC", "F_DOC")
                _assign_existing_values(tarj_values, tarjetero_columns, timezone.localdate(), "FECHA_CREACION")
                _assign_existing_values(tarj_values, tarjetero_columns, "RD$", "MONEDA", "MON_DOC")
                _assign_existing_values(tarj_values, tarjetero_columns, 1, "ID_ALMACEN", "ALM", "ALMACEN")
                _assign_existing_values(tarj_values, tarjetero_columns, usuario_id, "ID_USUARIO", "USUARIO_ID")
                _assign_existing_values(tarj_values, tarjetero_columns, int(fecha_cont.month), "PERIODO_CONT")
                _assign_existing_values(tarj_values, tarjetero_columns, int(fecha_cont.year), "EJERCICIO")
                _insert_dynamic_row(
                    cursor,
                    "TARJETERO",
                    tarjetero_columns,
                    tarj_values,
                    skip_columns=tarjetero_identity_columns,
                )

    no_ed_value = _create_entrada_ed_entries(
        cursor,
        origen_doc=main_lookup_text,
        fecha_cont=fecha_cont,
        fecha_doc=fecha_doc,
        fecha_venc=fecha_venc,
        total_doc=total_doc,
        comentario=comentario,
        usuario_id=usuario_id,
        usuario_nombre=usuario_nombre,
        terminal=terminal,
        departamento_ceco=departamento_ceco,
    )

    if det_table.upper() == "DET_SAL_INV":
        mov_doc_columns = _load_table_columns("MOV_DOC")
        if mov_doc_columns:
            mov_doc_identity_columns = _load_identity_columns("MOV_DOC")
            mov_values = {}
            _assign_existing_values(mov_values, mov_doc_columns, "SM", "TIPO_DOC", "TD", "CLASE_DOC", "TIPO")
            _assign_existing_values(mov_values, mov_doc_columns, main_lookup_text, "ID_DOC", "NO_DOC", "NO", "DOCUMENTO")
            _assign_existing_values(mov_values, mov_doc_columns, "-1", "ID_SN", "ID_PROV", "COD_SN")
            _assign_existing_values(mov_values, mov_doc_columns, fecha_cont, "FECHA_CONT", "F_CONT")
            _assign_existing_values(mov_values, mov_doc_columns, fecha_venc, "FECHA_VENC", "F_VENC")
            _assign_existing_values(mov_values, mov_doc_columns, fecha_doc, "FECHA_DOC", "F_DOC")
            _assign_existing_values(mov_values, mov_doc_columns, "-1", "REF_DOC_BASE", "DOC_BASE", "ID_DOC_BASE")
            _assign_existing_values(mov_values, mov_doc_columns, Decimal("0"), "TOTAL_BASE", "BASE")
            _assign_existing_values(mov_values, mov_doc_columns, "Cerrado", "EST_DOC", "ESTATUS", "ESTADO")
            _assign_existing_values(mov_values, mov_doc_columns, total_doc, "TOTAL_DOC", "TOTAL", "MONTO", "IMPORTE", "VALOR")
            _assign_existing_values(mov_values, mov_doc_columns, "RD$", "MON_DOC", "MONEDA")
            _assign_existing_values(mov_values, mov_doc_columns, "-1", "NO_RECIBO", "ID_RECIBO")
            _assign_existing_values(mov_values, mov_doc_columns, datetime(1900, 1, 1), "FECHA_REC", "FECHA_RECIBO")
            _assign_existing_values(mov_values, mov_doc_columns, no_ed_value or "", "NO_ED", "ID_ED")
            _assign_existing_values(mov_values, mov_doc_columns, int(fecha_cont.month), "PERIODO_CONT")
            _assign_existing_values(mov_values, mov_doc_columns, int(fecha_cont.year), "EJERCICIO")
            _assign_existing_values(mov_values, mov_doc_columns, usuario_id, "ID_USUARIO", "USUARIO_ID")
            _assign_existing_values(mov_values, mov_doc_columns, "-1", "ID_VENDEDOR", "VENDEDOR")
            _assign_existing_values(mov_values, mov_doc_columns, None, "ID_CONDICION", "CONDICION_ID", "CONDICION")
            _assign_existing_values(mov_values, mov_doc_columns, "-1", "DIA")
            _assign_existing_values(mov_values, mov_doc_columns, None, "CANCELADO", "ANULADO")
            _assign_existing_values(mov_values, mov_doc_columns, terminal, "TERMINAL")
            _assign_existing_values(mov_values, mov_doc_columns, comentario, "COMENTARIO", "OBSERVACION", "NOTA")
            _insert_dynamic_row(
                cursor,
                "MOV_DOC",
                mov_doc_columns,
                mov_values,
                skip_columns=mov_doc_identity_columns,
            )

    return main_lookup_text


def index(request):
    ctx = _base_context(request, page_title="Inventario", active_nav="inventario")
    if not ctx:
        return redirect("login")
    if not has_perm(ctx["auth_payload"]["usuario_id"], "inventario", "ver"):
        return render_denied(request, active_nav="inventario")
    ctx["submodules"] = {
        "articulos": has_perm(ctx["auth_payload"]["usuario_id"], "inventario", "ver_articulos"),
        "entrada_articulos": has_perm(ctx["auth_payload"]["usuario_id"], "inventario", "ver_entrada_articulos"),
        "salida_articulos": has_perm(ctx["auth_payload"]["usuario_id"], "inventario", "ver_salida_articulos"),
        "grupos": has_perm(ctx["auth_payload"]["usuario_id"], "inventario", "ver_grupos"),
        "stock": has_perm(ctx["auth_payload"]["usuario_id"], "inventario", "ver_stock"),
        "solicitudes_existencia_operativas": has_perm(ctx["auth_payload"]["usuario_id"], "inventario", "ver_solicitudes_existencia"),
    }
    return render(request, "inventario/index.html", ctx)


def articulos_view(request):
    ctx = _base_context(request, page_title="Articulos", active_nav="inventario")
    if not ctx:
        return redirect("login")
    if not has_perm(ctx["auth_payload"]["usuario_id"], "inventario", "ver_articulos"):
        return render_denied(request, active_nav="inventario")
    return render(request, "inventario/articulos.html", ctx)


def grupos_view(request):
    ctx = _base_context(request, page_title="Grupos de articulos", active_nav="inventario")
    if not ctx:
        return redirect("login")
    if not has_perm(ctx["auth_payload"]["usuario_id"], "inventario", "ver_grupos"):
        return render_denied(request, active_nav="inventario")
    return render(request, "inventario/grupos.html", ctx)


def stock_view(request):
    ctx = _base_context(request, page_title="Stock de articulos", active_nav="inventario")
    if not ctx:
        return redirect("login")
    if not has_perm(ctx["auth_payload"]["usuario_id"], "inventario", "ver_stock"):
        return render_denied(request, active_nav="inventario")
    return render(request, "inventario/stock.html", ctx)


def entrada_articulos_view(request):
    ctx = _base_context(request, page_title="Entrada de articulos", active_nav="inventario")
    if not ctx:
        return redirect("login")
    if not has_perm(ctx["auth_payload"]["usuario_id"], "inventario", "ver_entrada_articulos"):
        return render_denied(request, active_nav="inventario")
    usuario_id = ctx["auth_payload"]["usuario_id"]
    default_departamento = _get_default_departamento()
    ctx["server_today_iso"] = timezone.localdate().strftime("%Y-%m-%d")
    ctx["entrada_defaults"] = {
        "codigo": "3",
        "descripcion": "Entrada de Mercancia",
        "departamento_ceco": default_departamento.get("ceco") or "",
        "departamento_descripcion": default_departamento.get("descripcion") or "",
        "proveedor_codigo": "-1",
    }
    ctx["entrada_shortcuts"] = {
        "articulos": has_perm(usuario_id, "inventario", "ver_articulos"),
        "salida_articulos": has_perm(usuario_id, "inventario", "ver_salida_articulos"),
        "facturacion": has_perm(usuario_id, "factura", "ver_documentos"),
        "cuentas_por_cobrar": has_perm(usuario_id, "caja", "ver_cuentas_por_cobrar"),
    }
    ctx["entrada_departamentos"] = _load_departamento_rows()
    return render(request, "inventario/entrada_articulos.html", ctx)


@require_GET
def solicitudes_existencia_resumen_view(request):
    auth_payload = _get_auth_payload(request)
    if not auth_payload:
        return JsonResponse({"detail": "Sesion expirada."}, status=401)
    usuario_id = auth_payload.get("usuario_id")
    can_view_stock = has_perm(usuario_id, "inventario", "ver_solicitudes_existencia")
    can_view_acuerdos = has_perm(usuario_id, "cobros", "ver_acuerdos")
    can_view = can_view_stock or can_view_acuerdos
    if not can_view:
        return JsonResponse({"allowed": False, "count": 0, "results": []})
    results = []
    if can_view_stock:
        solicitudes = list(SolicitudExistencia.objects.filter(atendida=False).order_by("-creado_en", "-id_solicitud")[:12])
        results.extend(_serialize_solicitud_existencia(item) for item in solicitudes)
    if can_view_acuerdos:
        results.extend(_load_acuerdo_notifications(limit=12))
        results.extend(_load_agreement_payment_notifications(limit=12))
    state_map = _load_notification_state_map(usuario_id, results)
    visible_results = []
    for item in results:
        key = (
            _normalize_notification_state_value(item.get("notification_type"), 80),
            _normalize_notification_state_value(item.get("id"), 120),
        )
        state = state_map.get(key) or {}
        if state.get("is_hidden"):
            continue
        item = dict(item)
        item["is_read"] = bool(state.get("is_read"))
        item["is_delivered"] = bool(state.get("is_delivered"))
        visible_results.append(item)
    results = sorted(
        visible_results,
        key=lambda item: (
            str(item.get("sort_timestamp") or ""),
            int(item.get("priority") or (100 if item.get("notification_type") == "stock_request" else 0)),
            _notification_id_sort_value(item.get("id")),
        ),
        reverse=True,
    )[:12]
    return JsonResponse(
        {
            "allowed": True,
            "count": len(results),
            "results": results,
        }
    )


@require_GET
def solicitudes_existencia_lista_view(request):
    auth_payload = _require_perm_json(request, "inventario", "ver_solicitudes_existencia")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload
    solicitudes = list(SolicitudExistencia.objects.filter(atendida=False).order_by("-creado_en", "-id_solicitud")[:100])
    return JsonResponse(
        {
            "count": len(solicitudes),
            "results": [_serialize_solicitud_existencia(item) for item in solicitudes],
        }
    )


def solicitudes_existencia_operativas_view(request):
    ctx = _base_context(request, page_title="Solicitudes de existencia", active_nav="inventario")
    if not ctx:
        return redirect("login")
    if not has_perm(ctx["auth_payload"]["usuario_id"], "inventario", "ver_solicitudes_existencia"):
        return render_denied(request, active_nav="inventario")
    return render(request, "inventario/solicitudes_existencia_operativas.html", ctx)


@require_http_methods(["POST"])
def solicitudes_existencia_crear_view(request):
    auth_payload = _require_perm_json(request, "factura", "ver_documentos")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        return JsonResponse({"detail": "No se pudo leer la solicitud de existencia."}, status=400)
    detalles = _parse_solicitud_existencia_items((payload or {}).get("detalles"))
    if not detalles:
        return JsonResponse({"detail": "No hay articulos pendientes para solicitar existencia."}, status=400)

    origen_modulo = str((payload or {}).get("origen_modulo") or "FACTURA").strip().upper() or "FACTURA"
    origen_referencia = str((payload or {}).get("origen_referencia") or "").strip() or None

    if origen_modulo == "FACTURA" and origen_referencia:
        ref_upper = origen_referencia.upper()
        if (ref_upper.startswith("FACTURA ") or ref_upper.startswith("PREFACTURA ")) and ref_upper not in ("FACTURA", "PREFACTURA"):
            if SolicitudExistencia.objects.filter(origen_modulo="FACTURA", origen_referencia__iexact=origen_referencia).exists():
                doc_type_es = "prefactura" if ref_upper.startswith("PREFACTURA ") else "factura"
                return JsonResponse(
                    {"detail": f"Ya existe un pedido de existencia para esta {doc_type_es}."},
                    status=400
                )

    origin_terminal = _resolve_request_terminal(request, payload if isinstance(payload, dict) else {})
    solicitud = SolicitudExistencia.objects.create(
        origen_modulo=origen_modulo,
        origen_referencia=origen_referencia,
        cliente_codigo=str((payload or {}).get("cliente_codigo") or "").strip() or None,
        cliente_nombre=str((payload or {}).get("cliente_nombre") or "").strip() or None,
        comentario=str((payload or {}).get("comentario") or "").strip() or None,
        detalle_json=json.dumps({"items": detalles, "origin_terminal": origin_terminal}, ensure_ascii=False, default=str),
        creada_por_id=auth_payload.get("usuario_id"),
        creada_por_login=str(auth_payload.get("usuario_login") or "").strip() or None,
        creada_por_nombre=str(auth_payload.get("usuario_nombre") or "").strip() or None,
    )
    transaction.on_commit(
        lambda: broadcast_notification_refresh(reason="stock-request-created")
    )
    transaction.on_commit(
        lambda: broadcast_inventario_solicitudes_refresh(reason="stock-request-created")
    )
    return JsonResponse(
        {
            "ok": True,
            "solicitud_id": solicitud.id_solicitud,
            "notification": _serialize_solicitud_existencia(solicitud),
            "detail": "Pedido de existencia enviado correctamente.",
        }
    )


@require_GET
def solicitudes_existencia_detalle_view(request, solicitud_id):
    auth_payload = _require_perm_json(request, "inventario", "ver_solicitudes_existencia")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload
    solicitud = SolicitudExistencia.objects.filter(id_solicitud=solicitud_id).first()
    if not solicitud:
        return JsonResponse({"detail": "La solicitud de existencia no fue encontrada."}, status=404)
    if solicitud.atendida:
        return JsonResponse({"detail": "La solicitud de existencia ya fue atendida."}, status=409)
    detalles = []
    for item in _parse_solicitud_existencia_items(solicitud.detalle_json):
        cantidad = item.get("cantidad_faltante") or Decimal("0")
        detalles.append(
            {
                "id_articulo": item.get("articulo_id") or "",
                "descripcion": item.get("descripcion") or "",
                "cant_emp": cantidad,
                "cantidad": cantidad,
                "uom": item.get("uom") or "",
                "alm": item.get("alm") or "",
                "pedido_cte": "-1",
                "proyecto": "P01",
                "ceco": item.get("ceco") or "",
                "costo_unit": Decimal("1"),
                "valor": cantidad,
                "cuenta_mayor": item.get("cta_aum_stock") or "11030101",
            }
        )
    return JsonResponse(
        {
            "id": solicitud.id_solicitud,
            "cliente_codigo": solicitud.cliente_codigo or "",
            "cliente_nombre": solicitud.cliente_nombre or "",
            "referencia": solicitud.origen_referencia or "",
            "comentario": solicitud.comentario or "",
            "detalles": detalles,
        }
    )


@require_http_methods(["POST"])
def solicitudes_existencia_marcar_leida_view(request, solicitud_id):
    auth_payload = _require_perm_json(request, "inventario", "ver_solicitudes_existencia")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload
    solicitud = SolicitudExistencia.objects.filter(id_solicitud=solicitud_id).first()
    if not solicitud:
        return JsonResponse({"detail": "La notificacion no fue encontrada."}, status=404)
    if not solicitud.atendida:
        _mark_solicitud_existencia_atendida(
            solicitud,
            usuario_id=auth_payload.get("usuario_id"),
            usuario_nombre=auth_payload.get("usuario_nombre"),
        )
        transaction.on_commit(
            lambda: broadcast_notification_refresh(reason="stock-request-read")
        )
        transaction.on_commit(
            lambda: broadcast_inventario_solicitudes_refresh(reason="stock-request-read")
        )
    return JsonResponse({"ok": True})


def _parse_notification_state_payload(request):
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return {
        "notification_type": _normalize_notification_state_value(payload.get("notification_type"), 80),
        "notification_id": _normalize_notification_state_value(payload.get("notification_id"), 120),
    }


@require_http_methods(["POST"])
def notification_state_mark_read_view(request, notification_id):
    auth_payload = _get_auth_payload(request)
    if not auth_payload:
        return JsonResponse({"detail": "Sesion expirada."}, status=401)
    payload = _parse_notification_state_payload(request)
    notification_type = payload.get("notification_type")
    resolved_id = payload.get("notification_id") or _normalize_notification_state_value(notification_id, 120)
    if not notification_type or not resolved_id:
        return JsonResponse({"detail": "Notificacion invalida."}, status=400)
    _upsert_notification_user_state(
        auth_payload.get("usuario_id"),
        notification_type,
        resolved_id,
        is_read=True,
    )
    transaction.on_commit(lambda: broadcast_notification_refresh(reason="notification-read"))
    return JsonResponse({"ok": True})


@require_http_methods(["POST"])
def notification_state_mark_read_bulk_view(request):
    auth_payload = _get_auth_payload(request)
    if not auth_payload:
        return JsonResponse({"detail": "Sesion expirada."}, status=401)
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        payload = {}
    items = payload.get("items") if isinstance(payload, dict) else []
    if not isinstance(items, list):
        items = []
    changed = False
    for item in items[:50]:
        if not isinstance(item, dict):
            continue
        notification_type = _normalize_notification_state_value(item.get("notification_type"), 80)
        notification_id = _normalize_notification_state_value(item.get("notification_id"), 120)
        if not notification_type or not notification_id:
            continue
        _upsert_notification_user_state(
            auth_payload.get("usuario_id"),
            notification_type,
            notification_id,
            is_read=True,
        )
        changed = True
    if changed:
        transaction.on_commit(lambda: broadcast_notification_refresh(reason="notification-read-bulk"))
    return JsonResponse({"ok": True})


@require_http_methods(["POST"])
def notification_state_hide_view(request, notification_id):
    auth_payload = _get_auth_payload(request)
    if not auth_payload:
        return JsonResponse({"detail": "Sesion expirada."}, status=401)
    payload = _parse_notification_state_payload(request)
    notification_type = payload.get("notification_type")
    resolved_id = payload.get("notification_id") or _normalize_notification_state_value(notification_id, 120)
    if not notification_type or not resolved_id:
        return JsonResponse({"detail": "Notificacion invalida."}, status=400)
    _upsert_notification_user_state(
        auth_payload.get("usuario_id"),
        notification_type,
        resolved_id,
        is_hidden=True,
        is_read=True,
    )
    transaction.on_commit(lambda: broadcast_notification_refresh(reason="notification-hidden"))
    return JsonResponse({"ok": True})


@require_http_methods(["POST"])
def notification_state_delivered_view(request):
    auth_payload = _get_auth_payload(request)
    if not auth_payload:
        return JsonResponse({"detail": "Sesion expirada."}, status=401)
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        payload = {}
    items = payload.get("items") if isinstance(payload, dict) else []
    if not isinstance(items, list):
        items = []
    for item in items[:50]:
        if not isinstance(item, dict):
            continue
        notification_type = _normalize_notification_state_value(item.get("notification_type"), 80)
        notification_id = _normalize_notification_state_value(item.get("notification_id"), 120)
        if not notification_type or not notification_id:
            continue
        _upsert_notification_user_state(
            auth_payload.get("usuario_id"),
            notification_type,
            notification_id,
            is_delivered=True,
        )
    return JsonResponse({"ok": True})


@require_http_methods(["POST"])
def solicitudes_existencia_eliminar_view(request, solicitud_id):
    auth_payload = _require_perm_json(request, "inventario", "ver_solicitudes_existencia")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload
    solicitud = SolicitudExistencia.objects.filter(id_solicitud=solicitud_id).first()
    if not solicitud:
        return JsonResponse({"detail": "La notificacion no fue encontrada."}, status=404)
    solicitud.delete()
    transaction.on_commit(
        lambda: broadcast_notification_refresh(reason="stock-request-deleted")
    )
    transaction.on_commit(
        lambda: broadcast_inventario_solicitudes_refresh(reason="stock-request-deleted")
    )
    return JsonResponse({"ok": True})


@require_GET
def entrada_articulos_buscar_view(request):
    auth_payload = _require_perm_json(request, "inventario", "ver_entrada_articulos")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload
    query = (request.GET.get("q") or "").strip()
    filtro = (request.GET.get("filtro") or "documento").strip().lower()
    try:
        return JsonResponse({"results": _load_entrada_articulos_search_rows(query=query, filtro=filtro)})
    except Exception:
        return JsonResponse({"results": []})


@require_GET
def entrada_articulos_detalle_view(request):
    auth_payload = _require_perm_json(request, "inventario", "ver_entrada_articulos")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload
    no_doc = (request.GET.get("no_doc") or "").strip()
    if not no_doc:
        return JsonResponse({"detail": "Parametro no_doc requerido"}, status=400)
    try:
        record = _load_entrada_articulos_record(no_doc)
    except Exception:
        record = None
    if not record:
        return JsonResponse({"detail": "Entrada de articulos no encontrada."}, status=404)
    return JsonResponse(record)


@require_GET
def entrada_articulos_proveedores_view(request):
    auth_payload = _require_perm_json(request, "inventario", "ver_entrada_articulos")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload
    query = (request.GET.get("q") or "").strip()
    try:
        return JsonResponse({"results": _load_proveedor_search_rows(query=query)})
    except Exception:
        return JsonResponse({"results": []})


@require_GET
def entrada_articulos_articulos_view(request):
    auth_payload = _require_perm_json(request, "inventario", "ver_entrada_articulos")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload
    query = (request.GET.get("q") or "").strip()
    filtro = (request.GET.get("filtro") or "descripcion").strip().lower()
    try:
        return JsonResponse({"results": _load_entrada_articulos_articulo_rows(query=query, filtro=filtro)})
    except Exception as exc:
        return JsonResponse({"detail": f"No se pudieron cargar articulos: {exc}"}, status=500)


@require_http_methods(["POST"])
def entrada_articulos_guardar_view(request):
    auth_payload = _require_perm_json(request, "inventario", "ver_entrada_articulos")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        return JsonResponse({"detail": "No se pudo leer el documento a guardar."}, status=400)
    try:
        terminal = _resolve_request_terminal(request, payload if isinstance(payload, dict) else {})
        with transaction.atomic():
            solicitud_existencia = None
            solicitud_existencia_id = _to_int((payload or {}).get("solicitud_existencia_id"), 0)
            if solicitud_existencia_id > 0:
                solicitud_existencia = (
                    SolicitudExistencia.objects.select_for_update().filter(id_solicitud=solicitud_existencia_id).first()
                )
                if not solicitud_existencia:
                    raise ValueError("La solicitud de existencia ya no esta disponible.")
                if solicitud_existencia.atendida:
                    raise ValueError("La solicitud de existencia ya fue atendida.")
            with connection.cursor() as cursor:
                lookup = _persist_entrada_articulos_record(
                    cursor,
                    payload=payload if isinstance(payload, dict) else {},
                    usuario_id=auth_payload.get("usuario_id"),
                    usuario_nombre=auth_payload.get("usuario_nombre"),
                    terminal=terminal,
                )
            if solicitud_existencia:
                _mark_solicitud_existencia_atendida(
                    solicitud_existencia,
                    usuario_id=auth_payload.get("usuario_id"),
                    usuario_nombre=auth_payload.get("usuario_nombre"),
                )
                transaction.on_commit(
                    lambda: broadcast_notification_refresh(reason="stock-request-completed")
                )
                transaction.on_commit(
                    lambda: broadcast_inventario_solicitudes_refresh(reason="stock-request-completed")
                )
        record = _load_entrada_articulos_record(lookup)
        return JsonResponse({"ok": True, "lookup": lookup, "record": record})
    except ValueError as exc:
        return JsonResponse({"detail": str(exc)}, status=400)
    except Exception as exc:
        return JsonResponse({"detail": f"No se pudo guardar la entrada: {exc}"}, status=500)


def salida_articulos_view(request):
    ctx = _base_context(request, page_title="Salida de articulos", active_nav="inventario")
    if not ctx:
        return redirect("login")
    if not has_perm(ctx["auth_payload"]["usuario_id"], "inventario", "ver_salida_articulos"):
        return render_denied(request, active_nav="inventario")
    usuario_id = ctx["auth_payload"]["usuario_id"]
    default_departamento = _get_default_departamento()
    ctx["server_today_iso"] = timezone.localdate().strftime("%Y-%m-%d")
    ctx["entrada_defaults"] = {
        "codigo": "4",
        "descripcion": "Salida de Mercancia",
        "departamento_ceco": default_departamento.get("ceco") or "",
        "departamento_descripcion": default_departamento.get("descripcion") or "",
        "proveedor_codigo": "-1",
    }
    ctx["entrada_shortcuts"] = {
        "articulos": has_perm(usuario_id, "inventario", "ver_articulos"),
        "salida_articulos": has_perm(usuario_id, "inventario", "ver_entrada_articulos"),
        "facturacion": has_perm(usuario_id, "factura", "ver_documentos"),
        "cuentas_por_cobrar": has_perm(usuario_id, "caja", "ver_cuentas_por_cobrar"),
    }
    ctx["entrada_departamentos"] = _load_departamento_rows()
    return render(request, "inventario/salida_articulos.html", ctx)


@require_GET
def salida_articulos_buscar_view(request):
    auth_payload = _require_perm_json(request, "inventario", "ver_salida_articulos")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload
    query = (request.GET.get("q") or "").strip()
    filtro = (request.GET.get("filtro") or "documento").strip().lower()
    cab_table, _ = _get_salida_inventory_tables()
    try:
        return JsonResponse({"results": _load_inventory_articulos_search_rows(cab_table, query=query, filtro=filtro)})
    except Exception:
        return JsonResponse({"results": []})


@require_GET
def salida_articulos_detalle_view(request):
    auth_payload = _require_perm_json(request, "inventario", "ver_salida_articulos")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload
    no_doc = (request.GET.get("no_doc") or "").strip()
    if not no_doc:
        return JsonResponse({"detail": "Parametro no_doc requerido"}, status=400)
    cab_table, det_table = _get_salida_inventory_tables()
    try:
        record = _load_inventory_articulos_record(cab_table, det_table, no_doc)
    except Exception:
        record = None
    if not record:
        return JsonResponse({"detail": "Salida de articulos no encontrada."}, status=404)
    return JsonResponse(record)


@require_GET
def salida_articulos_proveedores_view(request):
    auth_payload = _require_perm_json(request, "inventario", "ver_salida_articulos")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload
    query = (request.GET.get("q") or "").strip()
    try:
        return JsonResponse({"results": _load_proveedor_search_rows(query=query)})
    except Exception:
        return JsonResponse({"results": []})


@require_GET
def salida_articulos_articulos_view(request):
    auth_payload = _require_perm_json(request, "inventario", "ver_salida_articulos")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload
    query = (request.GET.get("q") or "").strip()
    filtro = (request.GET.get("filtro") or "descripcion").strip().lower()
    try:
        return JsonResponse({"results": _load_entrada_articulos_articulo_rows(query=query, filtro=filtro)})
    except Exception as exc:
        return JsonResponse({"detail": f"No se pudieron cargar articulos: {exc}"}, status=500)


@require_http_methods(["POST"])
def salida_articulos_guardar_view(request):
    auth_payload = _require_perm_json(request, "inventario", "ver_salida_articulos")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        return JsonResponse({"detail": "No se pudo leer el documento a guardar."}, status=400)
    try:
        terminal = _resolve_request_terminal(request, payload if isinstance(payload, dict) else {})
        cab_table, det_table = _get_salida_inventory_tables()
        with transaction.atomic():
            with connection.cursor() as cursor:
                lookup = _persist_inventory_articulos_record(
                    cursor,
                    payload=payload if isinstance(payload, dict) else {},
                    usuario_id=auth_payload.get("usuario_id"),
                    usuario_nombre=auth_payload.get("usuario_nombre"),
                    terminal=terminal,
                    cab_table=cab_table,
                    det_table=det_table,
                    default_description="Salida de Mercancia",
                    movement_code=4,
                    stock_multiplier=-1,
                )
        record = _load_inventory_articulos_record(cab_table, det_table, lookup)
        return JsonResponse({"ok": True, "lookup": lookup, "record": record})
    except ValueError as exc:
        return JsonResponse({"detail": str(exc)}, status=400)
    except Exception as exc:
        return JsonResponse({"detail": f"No se pudo guardar la salida: {exc}"}, status=500)


@require_GET
def articulos_list_view(request):
    auth_payload = _require_perm_json(request, "inventario", "ver_articulos")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload
        
    q = (request.GET.get("q") or "").strip()
    page_num = int(request.GET.get("page") or 1)
    page_size = 50
    
    from django.db.models import Q
    from prefacturas_app.models_existing import MaestroArticulo
    
    qs = MaestroArticulo.objects.all()
    if q:
        qs = qs.filter(
            Q(id_articulo__icontains=q) |
            Q(descrip_art__icontains=q) |
            Q(referencia__icontains=q) |
            Q(cod_barra__icontains=q)
        )
        
    qs = qs.order_by("-id_articulo")
    
    total_count = qs.count()
    
    start = (page_num - 1) * page_size
    end = start + page_size
    
    articles_slice = qs[start:end]
    
    results = []
    for a in articles_slice:
        results.append({
            "id_articulo": a.id_articulo.strip() if a.id_articulo else "",
            "descrip_art": a.descrip_art.strip() if a.descrip_art else "",
            "referencia": a.referencia.strip() if a.referencia else "",
            "cod_barra": a.cod_barra.strip() if a.cod_barra else "",
            "precio_det": float(a.precio_det or 0),
            "costo": float(a.costo or 0),
            "tarifa_vt": float(a.tarifa_vt or 0),
            "um_venta": a.um_venta.strip() if a.um_venta else "UND",
            "bloqueado": (a.bloqueado or "N").strip() == "Y"
        })
        
    # Load conversions for the page slice
    from prefacturas_app.models import ArticuloConversion
    article_ids = [r["id_articulo"] for r in results]
    conversions = {
        c.id_articulo: {
            "id_articulo_base": c.id_articulo_base,
            "factor": float(c.factor)
        }
        for c in ArticuloConversion.objects.filter(id_articulo__in=article_ids)
    }
    for r in results:
        conv = conversions.get(r["id_articulo"])
        if conv:
            r["id_articulo_base"] = conv["id_articulo_base"]
            r["factor_conversion"] = conv["factor"]
            r["has_conversion"] = True
        else:
            r["id_articulo_base"] = ""
            r["factor_conversion"] = 1.0
            r["has_conversion"] = False
        
    import math
    total_pages = math.ceil(total_count / page_size) if total_count > 0 else 1
    
    return JsonResponse({
        "ok": True,
        "results": results,
        "total_count": total_count,
        "total_pages": total_pages,
        "current_page": page_num
    })


@require_http_methods(["POST"])
def articulo_save_view(request):
    auth_payload = _require_perm_json(request, "inventario", "ver_articulos")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload
        
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
        is_edit = bool(payload.get("is_edit"))
        id_articulo = str(payload.get("id_articulo") or "").strip()
        descrip_art = str(payload.get("descrip_art") or "").strip()
        referencia = str(payload.get("referencia") or "").strip()
        cod_barra = str(payload.get("cod_barra") or "").strip()
        costo = float(payload.get("costo") or 0)
        precio_det = float(payload.get("precio_det") or 0)
        raw_tarifa = payload.get("tarifa_vt")
        tarifa_vt = float(raw_tarifa) if raw_tarifa is not None else 18.0
        um_venta = str(payload.get("um_venta") or "UND").strip().upper()
        bloqueado_val = "Y" if bool(payload.get("bloqueado")) else "N"
        
        id_articulo_base = str(payload.get("id_articulo_base") or "").strip()
        try:
            factor_conversion = float(payload.get("factor_conversion") or 1.0)
        except (TypeError, ValueError):
            factor_conversion = 1.0
    except Exception as exc:
        return JsonResponse({"detail": f"Datos invalidos: {exc}"}, status=400)
        
    if not descrip_art:
        return JsonResponse({"detail": "La descripcion del articulo es requerida."}, status=400)
        
    from prefacturas_app.models_existing import MaestroArticulo
    from prefacturas_app.models import ArticuloConversion
    
    uom_map = {
        "UND": "Unidad",
        "LB": "Libra",
        "KG": "Kilogramo",
        "GL": "Galón",
        "PAQ": "Paquete",
        "CJA": "Caja",
    }
    um_inv_val = uom_map.get(um_venta, "Unidad")
    terminal_name = _resolve_request_terminal(request, payload)
    
    try:
        with transaction.atomic():
            if is_edit:
                if not id_articulo:
                    return JsonResponse({"detail": "Codigo de articulo requerido para editar."}, status=400)
                try:
                    articulo = MaestroArticulo.objects.get(id_articulo=id_articulo)
                except MaestroArticulo.DoesNotExist:
                    return JsonResponse({"detail": f"Articulo con codigo {id_articulo} no existe."}, status=404)
                    
                articulo.descrip_art = descrip_art
                articulo.referencia = referencia
                articulo.cod_barra = cod_barra if cod_barra else id_articulo
                articulo.costo = costo
                articulo.precio_det = precio_det
                articulo.tarifa_vt = tarifa_vt
                articulo.id_impto_vt = 1 if tarifa_vt > 0 else 2
                articulo.cod_impto_vt = "ITBIS" if tarifa_vt > 0 else "EXENTO"
                articulo.um_venta = um_venta
                articulo.um_inv = um_inv_val
                articulo.bloqueado = bloqueado_val
                articulo.fecha_act = timezone.localtime()
                articulo.terminal = terminal_name
                articulo.save(update_fields=[
                    'descrip_art',
                    'referencia',
                    'cod_barra',
                    'costo',
                    'precio_det',
                    'tarifa_vt',
                    'id_impto_vt',
                    'cod_impto_vt',
                    'um_venta',
                    'um_inv',
                    'bloqueado',
                    'fecha_act',
                    'terminal'
                ])
            else:
                # Creacion
                if id_articulo:
                    # Validar unicidad
                    if MaestroArticulo.objects.filter(id_articulo=id_articulo).exists():
                        return JsonResponse({"detail": f"Ya existe un articulo con el codigo '{id_articulo}'."}, status=400)
                else:
                    # Auto-generar codigo
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT ISNULL(MAX(TRY_CAST(ID_ARTICULO AS BIGINT)), 0) + 1 FROM MAESTRO_ARTICULO")
                        next_id = int(cursor.fetchone()[0] or 1)
                    id_articulo = str(next_id)
                    
                with connection.cursor() as cursor:
                    cursor.execute("""
                        INSERT INTO MAESTRO_ARTICULO (
                            ID_ARTICULO, DESCRIP_ART, REFERENCIA, COD_BARRA, COSTO, PRECIO_DET, TARIFA_VT,
                            ID_IMPTO_VT, COD_IMPTO_VT, UM_VENTA, BLOQUEADO, FECHA_CREACION, FECHA_ACT,
                            ALM_DFT, MONEDA, STOCK, ART_COMPRA, ART_VENTA, ART_INV, LOTE,
                            ID_GRUPO, DESCRIP_GRUPO, SECUENCIA, CTA_INGRESO, NOM_1, ID_USUARIO,
                            ID_PRECIO, DESCRIP_PRECIO, TIPO_PRECIO, CTA_GASTO, NOM_2,
                            CTA_COSTO, NOM_3, CLASE_ART, UM_INV, TERMINAL,
                            IDSUBGRUPO, NOMSUBGRUPO, IDCATEGORIA, NOMCATEGORIA
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s,
                            %s, %s, %s, %s
                        )
                    """, [
                        id_articulo,
                        descrip_art,
                        referencia,
                        cod_barra if cod_barra else id_articulo,
                        costo,
                        precio_det,
                        tarifa_vt,
                        1 if tarifa_vt > 0 else 2,
                        "ITBIS" if tarifa_vt > 0 else "EXENTO",
                        um_venta,
                        bloqueado_val,
                        timezone.localtime(),
                        timezone.localtime(),
                        1,
                        None,
                        0.0,
                        "T",
                        "T",
                        "T",
                        "No",
                        1,
                        "MERCANCIAS",
                        "1000",
                        "41010101",
                        "Ingresos p",
                        auth_payload.get("usuario_id"),
                        1,
                        "Lista de Precio",
                        "Fijo",
                        "11030102",
                        "Mercancias en Tránsito",
                        "51010101",
                        "Costo de Ventas de Mercancias",
                        "Articulo",
                        um_inv_val,
                        terminal_name,
                        1,
                        "Generico",
                        1,
                        "Generico"
                    ])
                
            # Save or delete ArticuloConversion
            if id_articulo_base:
                if not MaestroArticulo.objects.filter(id_articulo=id_articulo_base).exists():
                    raise ValueError(f"El articulo base '{id_articulo_base}' no existe.")
                if id_articulo == id_articulo_base:
                    raise ValueError("Un articulo no puede ser convertido a si mismo.")
                ArticuloConversion.objects.update_or_create(
                    id_articulo=id_articulo,
                    defaults={
                        "id_articulo_base": id_articulo_base,
                        "factor": factor_conversion
                    }
                )
            else:
                ArticuloConversion.objects.filter(id_articulo=id_articulo).delete()

        return JsonResponse({"ok": True, "id_articulo": id_articulo})
    except ValueError as exc:
        return JsonResponse({"detail": str(exc)}, status=400)
    except Exception as exc:
        return JsonResponse({"detail": f"Error al guardar el articulo: {exc}"}, status=500)
