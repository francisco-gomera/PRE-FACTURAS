import base64
import json
import socket
import hmac
from decimal import Decimal
from datetime import datetime

from django.core.cache import cache
from django.db import connection, transaction
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from ajustes.models import (
    FacturacionElectronicaConfig,
    FacturacionElectronicaDocumento,
    FacturacionElectronicaEvento,
    FacturacionElectronicaSecuencia,
)
from ajustes.permissions import has_perm
from ajustes.print_formats import get_print_format, get_print_format_label
from ajustes.user_signatures import get_user_signature_bytes
from core.realtime import (
    broadcast_factura_document_status,
    broadcast_prefactura_document_status,
    broadcast_prefacturas_refresh,
)
from core.views import _base_context, _get_empresa_data, render_denied
from prefacturas_app.models_existing import MaestroArticulo, MaestroSn
from prefacturas_app.views import _get_auth_payload, _require_perm_json
from .ecf_provider import submit_document, should_auto_dispatch
from .ecf_runtime import build_ecf_runtime_report, get_ecf_callback_api_key

ECF_TIPOS = [
    ("31", "Factura de Credito Fiscal Electronica"),
    ("32", "Factura de Consumo Electronica"),
    ("33", "Nota de Debito Electronica"),
    ("34", "Nota de Credito Electronica"),
    ("41", "Comprobante Electronico de Compras"),
    ("43", "Comprobante Electronico para Gastos Menores"),
    ("44", "Comprobante Electronico para Regimenes Especiales"),
    ("45", "Comprobante Electronico Gubernamental"),
    ("46", "Comprobante Electronico para Pagos al Exterior"),
    ("47", "Comprobante Electronico para Exportaciones"),
]
ECF_TIPOS_MAP = {codigo: nombre for codigo, nombre in ECF_TIPOS}
ECF_ID_NCF_CODES = {int(codigo) for codigo, _ in ECF_TIPOS}
EMISION_TIPOS = [
    ("31", "Factura de Credito Fiscal Electronica"),
    ("32", "Factura de Consumo Electronica"),
    ("45", "Factura Gubernamental Electronica"),
    ("47", "Factura de Exportacion Electronica"),
]

PREFACTURA_LOCK_TTL_SECONDS = 60 * 15
PREFACTURA_LOCK_CACHE_PREFIX = "factura.prefactura.lock"


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
    return _normalize_terminal_name(socket.gethostname()) or "FACTURA"


def _bool_post(value):
    return str(value or "").strip().lower() in {"1", "true", "on", "si", "sí", "y", "yes"}


def _to_int(value, default=0):
    try:
        return int(str(value or "").strip())
    except Exception:
        return default


def _to_date_or_none(value):
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except Exception:
        return None


def _to_decimal(value, default=Decimal("0")):
    try:
        if value is None or str(value).strip() == "":
            return default
        return Decimal(str(value))
    except Exception:
        return default


def _to_int_or_none(value):
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return int(float(raw))
    except Exception:
        return None


def _prefactura_lock_cache_key(prefactura_id):
    return f"{PREFACTURA_LOCK_CACHE_PREFIX}.{str(prefactura_id or '').strip()}"


def _prefactura_lock_get(prefactura_id):
    pref_id = str(prefactura_id or "").strip()
    if not pref_id:
        return None
    key = _prefactura_lock_cache_key(pref_id)
    lock = cache.get(key)
    if not isinstance(lock, dict):
        return None
    owner_id = str(lock.get("owner_id") or "").strip()
    touched_at = float(lock.get("touched_at") or 0)
    if not owner_id or touched_at <= 0:
        cache.delete(key)
        return None
    now_ts = timezone.now().timestamp()
    if (now_ts - touched_at) > PREFACTURA_LOCK_TTL_SECONDS:
        cache.delete(key)
        return None
    return lock


def _prefactura_lock_set(prefactura_id, *, owner_id, usuario_id, usuario_nombre, terminal):
    pref_id = str(prefactura_id or "").strip()
    owner_key = str(owner_id or "").strip()
    if not pref_id or not owner_key:
        return None
    now_ts = timezone.now().timestamp()
    lock = {
        "prefactura_id": pref_id,
        "owner_id": owner_key,
        "usuario_id": int(usuario_id or 0),
        "usuario_nombre": str(usuario_nombre or "").strip(),
        "terminal": str(terminal or "").strip(),
        "touched_at": now_ts,
    }
    cache.set(_prefactura_lock_cache_key(pref_id), lock, timeout=PREFACTURA_LOCK_TTL_SECONDS)
    return lock


def _prefactura_lock_acquire(prefactura_id, *, owner_id, usuario_id, usuario_nombre, terminal):
    pref_id = str(prefactura_id or "").strip()
    owner_key = str(owner_id or "").strip()
    if not pref_id or not owner_key:
        return {"ok": False, "lock": None}

    existing_lock = _prefactura_lock_get(pref_id)
    if existing_lock:
        existing_owner = str(existing_lock.get("owner_id") or "").strip()
        if existing_owner == owner_key:
            lock = _prefactura_lock_set(
                pref_id,
                owner_id=owner_key,
                usuario_id=usuario_id,
                usuario_nombre=usuario_nombre,
                terminal=terminal,
            )
            return {"ok": True, "lock": lock}
        return {"ok": False, "lock": existing_lock}

    now_ts = timezone.now().timestamp()
    lock = {
        "prefactura_id": pref_id,
        "owner_id": owner_key,
        "usuario_id": int(usuario_id or 0),
        "usuario_nombre": str(usuario_nombre or "").strip(),
        "terminal": str(terminal or "").strip(),
        "touched_at": now_ts,
    }
    created = cache.add(_prefactura_lock_cache_key(pref_id), lock, timeout=PREFACTURA_LOCK_TTL_SECONDS)
    if created:
        return {"ok": True, "lock": lock}

    existing_lock = _prefactura_lock_get(pref_id)
    if existing_lock and str(existing_lock.get("owner_id") or "").strip() == owner_key:
        return {"ok": True, "lock": existing_lock}
    return {"ok": False, "lock": existing_lock}


def _prefactura_lock_release(prefactura_id, *, owner_id):
    lock = _prefactura_lock_get(prefactura_id)
    owner_key = str(owner_id or "").strip()
    if not lock or not owner_key:
        return False
    if str(lock.get("owner_id") or "").strip() != owner_key:
        return False
    cache.delete(_prefactura_lock_cache_key(prefactura_id))
    return True


def _prefactura_lock_is_valid_for_owner(prefactura_id, owner_id):
    lock = _prefactura_lock_get(prefactura_id)
    if not lock:
        return False
    return str(lock.get("owner_id") or "").strip() == str(owner_id or "").strip()


def _clip_str(value, max_len):
    text = str(value or "")
    if not max_len or max_len <= 0:
        return text
    return text if len(text) <= max_len else text[:max_len]


def _format_decimal_display(value):
    decimal_value = _to_decimal(value)
    text = format(decimal_value.normalize(), "f") if decimal_value != 0 else "0"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _load_table_columns(cursor, table_name):
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


def _pick_existing_column(columns, *candidates):
    available = {str(column).upper(): str(column).upper() for column in (columns or [])}
    for candidate in candidates:
        if not candidate:
            continue
        found = available.get(str(candidate).upper())
        if found:
            return found
    return None


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


def _load_identity_columns(cursor, table_name):
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


def _insert_dynamic_row(cursor, table_name, table_columns, values_by_column, *, output_column=None, skip_columns=None):
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
    cursor.execute(sql, [values_by_column[column] for column in insert_columns])
    if output_column:
        row = cursor.fetchone()
        return row[0] if row else None
    return None


def _update_dynamic_row(cursor, table_name, set_values, where_sql, where_params):
    if not set_values:
        return 0
    columns = list(set_values.keys())
    sql = f"UPDATE {table_name} SET " + ", ".join(f"[{column}] = %s" for column in columns) + f" WHERE {where_sql}"
    cursor.execute(sql, [set_values[column] for column in columns] + list(where_params))
    return cursor.rowcount or 0


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


def _update_existing_columns(cursor, table_name, key_column, key_value, assignments):
    table_columns = _load_table_columns(cursor, table_name)
    update_clauses = []
    params = []
    for candidates, value in assignments:
        column = _pick_existing_column(table_columns, *(candidates or ()))
        if not column:
            continue
        update_clauses.append(f"[{column}] = %s")
        params.append(value)
    if not update_clauses:
        return
    params.append(key_value)
    cursor.execute(
        f"UPDATE {table_name} SET {', '.join(update_clauses)} WHERE [{key_column}] = %s",
        params,
    )


def _update_existing_columns_where(cursor, table_name, where_sql, where_params, assignments):
    table_columns = _load_table_columns(cursor, table_name)
    update_clauses = []
    params = []
    for candidates, value in assignments:
        column = _pick_existing_column(table_columns, *(candidates or ()))
        if not column:
            continue
        update_clauses.append(f"[{column}] = %s")
        params.append(value)
    if not update_clauses:
        return
    cursor.execute(
        f"UPDATE {table_name} SET {', '.join(update_clauses)} WHERE {where_sql}",
        params + list(where_params),
    )


def _stringify_doc(value):
    if value is None:
        return ""
    text = str(value).strip()
    return text[:-2] if text.endswith(".0") else text


def _to_float(value, default=0.0):
    try:
        if value is None:
            return float(default)
        text = str(value).strip()
        if not text:
            return float(default)
        return float(text.replace(",", ""))
    except Exception:
        return float(default)


def _chunked(sequence, size):
    for idx in range(0, len(sequence), size):
        yield sequence[idx:idx + size]


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


def _pick_row_text(row, *candidates, default=""):
    value = _pick_row_value(row, *candidates, default=None)
    if value is None:
        return default
    return str(value).strip()


def _pick_amount_value(row, *candidates, default=0.0):
    present_values = []
    for candidate in candidates:
        if not candidate:
            continue
        key = str(candidate).upper()
        if key not in row:
            continue
        raw_value = row.get(key)
        if raw_value is None:
            continue
        if isinstance(raw_value, str) and not raw_value.strip():
            continue
        amount = _to_float(raw_value)
        present_values.append(amount)
        if abs(amount) > 0.0001:
            return amount
    if present_values:
        return present_values[0]
    return _to_float(default)


def _get_det_recibo_payment_amount(row):
    return _to_decimal(
        _pick_amount_value(
            row,
            "TOTAL_PAGO",
            "TOTAL_PAGO2",
            "SALDO_VENC",
            "PAGO_ABONO",
            "IMP_ABONO",
            "IMP_PAGADO",
            "IMP_PAGO",
            "IMP_COBRADO",
            "IMP_APLICADO",
            "MONTO_APLICADO",
            "ABONO_APLICADO",
            "MONTO_ABONO",
            "MONTO_PAGO",
            "ABONO",
            "PAGADO",
            "PAGO",
            "COBRO",
            "IMPORTE",
            default=0,
        )
    )


def _get_det_recibo_discount_amount(row):
    return _to_decimal(
        _pick_amount_value(
            row,
            "DESCUENTO",
            "DESC_AVANCE",
            "AVANCE",
            "DESC",
            default=0,
        )
    )


def _get_det_recibo_applied_amount(row):
    return _get_det_recibo_payment_amount(row) + _get_det_recibo_discount_amount(row)


def _load_factura_active_payment_total(doc_number):
    no_doc = _stringify_doc(doc_number)
    if not no_doc:
        return Decimal("0")

    with connection.cursor() as cursor:
        det_columns = _load_table_columns(cursor, "DET_RECIBO_INGRESO")
        cab_columns = _load_table_columns(cursor, "CAB_RECIBO_INGRESO")
        det_doc_col = _pick_existing_column(det_columns, "NO_DOC", "ID_DOC", "DOCUMENTO", "FACTURA")
        det_recibo_col = _pick_existing_column(det_columns, "ID_RECIBO", "NO_RECIBO", "ID_DOC", "NO_DOC")
        cab_id_col = _pick_existing_column(cab_columns, "ID_RECIBO", "NO_RECIBO", "ID_DOC", "NO_DOC")
        cab_no_col = _pick_existing_column(cab_columns, "NO_RECIBO", "ID_RECIBO", "ID_DOC", "NO_DOC")
        if not det_doc_col or not det_recibo_col or not cab_id_col:
            return Decimal("0")

        cursor.execute(
            f"SELECT * FROM DET_RECIBO_INGRESO WHERE CAST([{det_doc_col}] AS NVARCHAR(255)) = %s",
            [no_doc],
        )
        raw_columns = [col[0] for col in cursor.description]
        detail_rows = [_normalize_result_row(raw_columns, raw_row) for raw_row in cursor.fetchall()]
        if not detail_rows:
            return Decimal("0")

        recibo_refs = {
            _stringify_doc(_pick_row_value(row, det_recibo_col, "ID_RECIBO", "NO_RECIBO"))
            for row in detail_rows
        }
        recibo_refs.discard("")

        recibos_activos = set()
        if recibo_refs:
            refs = list(dict.fromkeys(recibo_refs))
            for refs_chunk in _chunked(refs, 300):
                where_parts = []
                params = []
                placeholders = ", ".join(["%s"] * len(refs_chunk))
                if cab_id_col:
                    where_parts.append(f"CAST([{cab_id_col}] AS NVARCHAR(255)) IN ({placeholders})")
                    params.extend(refs_chunk)
                if cab_no_col and cab_no_col != cab_id_col:
                    where_parts.append(f"CAST([{cab_no_col}] AS NVARCHAR(255)) IN ({placeholders})")
                    params.extend(refs_chunk)
                if not where_parts:
                    continue
                cursor.execute(
                    f"SELECT * FROM CAB_RECIBO_INGRESO WHERE {' OR '.join(f'({part})' for part in where_parts)}",
                    params,
                )
                cab_raw_columns = [col[0] for col in cursor.description]
                for raw_row in cursor.fetchall():
                    row = _normalize_result_row(cab_raw_columns, raw_row)
                    estado = _pick_row_text(row, "ESTATUS", "EST_DOC", "ESTADO").upper()
                    cancelado = _pick_row_text(row, "CANCELADO").upper()
                    if estado == "CANCELADO" or cancelado == "Y":
                        continue
                    recibo_id = _stringify_doc(_pick_row_value(row, cab_id_col, cab_no_col))
                    recibo_no = _stringify_doc(_pick_row_value(row, cab_no_col, cab_id_col))
                    if recibo_id:
                        recibos_activos.add(recibo_id)
                    if recibo_no:
                        recibos_activos.add(recibo_no)

        total_pagado = Decimal("0")
        for row in detail_rows:
            recibo_ref = _stringify_doc(_pick_row_value(row, det_recibo_col, "ID_RECIBO", "NO_RECIBO"))
            if recibo_ref and recibo_ref not in recibos_activos:
                continue
            total_pagado += _get_det_recibo_applied_amount(row)
        return total_pagado


def _factura_manual_editable(*, total_doc, saldo, abono, est_doc, cancelado, ncf_nc="", active_payment_total=Decimal("0")):
    estado = str(est_doc or "").strip().upper()
    cancelado_val = str(cancelado or "").strip().upper()
    nota_credito = str(ncf_nc or "").strip()
    total_doc_dec = _to_decimal(total_doc)
    saldo_dec = _to_decimal(saldo)
    abono_dec = _to_decimal(abono)
    active_payment_total = _to_decimal(active_payment_total)

    if cancelado_val == "Y" or estado == "CANCELADO" or nota_credito:
        return False
    if active_payment_total > Decimal("0.01"):
        return False
    if abono_dec.copy_abs() > Decimal("0.01"):
        return False
    if (saldo_dec - total_doc_dec).copy_abs() > Decimal("0.01"):
        return False
    if estado and estado not in {"ABIERTO", "FACTURADA"}:
        return False
    return True


def _append_cancelled_comment(comment):
    marker = "(Documento Cancelado)"
    base = str(comment or "").strip()
    if marker.lower() in base.lower():
        return base
    return f"{base} {marker}".strip() if base else marker


def _create_factura_ed_entries(
    cursor,
    *,
    factura_id,
    prefactura_id,
    id_sn,
    nombre_cliente,
    rnc_ced,
    fecha_cont,
    fecha_doc,
    fecha_venc,
    total_factura,
    comentario,
    periodo_cont,
    ejercicio,
    usuario_id,
    usuario_nombre,
    terminal,
    cta_asociada,
    total_cantidad,
):
    cab_ed_columns = _load_table_columns(cursor, "CAB_ED")
    det_ed_columns = _load_table_columns(cursor, "DET_ED")
    if not cab_ed_columns or not det_ed_columns:
        raise ValueError("No se pudieron cargar las tablas CAB_ED/DET_ED.")

    cab_ed_identity_columns = _load_identity_columns(cursor, "CAB_ED")
    det_ed_identity_columns = _load_identity_columns(cursor, "DET_ED")
    cab_ed_key_col = _pick_existing_column(cab_ed_columns, "ID_DOC", "ID_ED", "NO_DOC", "NO_ED")
    cab_ed_no_col = _pick_existing_column(cab_ed_columns, "NO_DOC", "NO_ED", "ID_DOC", "ID_ED")
    if not cab_ed_key_col and not cab_ed_no_col:
        raise ValueError("No se pudo determinar la clave de CAB_ED.")

    next_ed_no = None
    if cab_ed_no_col and cab_ed_no_col not in cab_ed_identity_columns:
        next_ed_no = _next_table_numeric_value(cursor, "CAB_ED", cab_ed_no_col)
    elif cab_ed_key_col and cab_ed_key_col not in cab_ed_identity_columns:
        next_ed_no = _next_table_numeric_value(cursor, "CAB_ED", cab_ed_key_col)

    total_factura = _to_decimal(total_factura)
    comentario_ed = str(comentario or "").strip() or f"Factura {factura_id}"
    cab_ed_values = {}
    if cab_ed_key_col and cab_ed_key_col not in cab_ed_identity_columns:
        _assign_existing_values(cab_ed_values, cab_ed_columns, next_ed_no, cab_ed_key_col)
    if next_ed_no is not None:
        _assign_existing_values(cab_ed_values, cab_ed_columns, next_ed_no, "NO_DOC", "NO_ED")
    _assign_existing_values(cab_ed_values, cab_ed_columns, fecha_cont, "FECHA_CONT", "F_CONT")
    _assign_existing_values(cab_ed_values, cab_ed_columns, fecha_doc, "FECHA_DOC", "FECHA_APLIC")
    _assign_existing_values(cab_ed_values, cab_ed_columns, fecha_venc, "FECHA_VENC", "F_VENC")
    _assign_existing_values(cab_ed_values, cab_ed_columns, id_sn, "ID_SN", "CLIENTE", "COD_CLIENTE")
    _assign_existing_values(cab_ed_values, cab_ed_columns, nombre_cliente, "NOM_SN", "NOM_SOCIO", "NOMBRE", "NOM_CLIENTE")
    _assign_existing_values(cab_ed_values, cab_ed_columns, rnc_ced, "RNC_CED", "RNC", "CEDULA")
    _assign_existing_values(cab_ed_values, cab_ed_columns, "FC", "TIPO_DOC", "TD", "CLASE_DOC", "TIPO")
    _assign_existing_values(cab_ed_values, cab_ed_columns, total_factura, "TOTAL_DOC", "MONTO", "IMPORTE")
    _assign_existing_values(cab_ed_values, cab_ed_columns, Decimal("0"), "ABONO")
    _assign_existing_values(cab_ed_values, cab_ed_columns, total_factura, "SALDO")
    _assign_existing_values(cab_ed_values, cab_ed_columns, "RD$", "MON_DOC", "MONEDA")
    _assign_existing_values(cab_ed_values, cab_ed_columns, comentario_ed, "COMENTARIO", "OBSERVACION")
    _assign_existing_values(cab_ed_values, cab_ed_columns, "Abierto", "EST_DOC", "ESTADO", "ESTATUS")
    _assign_existing_values(cab_ed_values, cab_ed_columns, factura_id, "ORIGEN", "REFERENCIA", "NO_RECIBO")
    _assign_existing_values(cab_ed_values, cab_ed_columns, prefactura_id or None, "REFERENCIA1")
    _assign_existing_values(cab_ed_values, cab_ed_columns, usuario_id, "ID_USUARIO", "USUARIO_ID")
    _assign_existing_values(cab_ed_values, cab_ed_columns, usuario_nombre, "USUARIO", "USUARIO_NOMBRE")
    _assign_existing_values(cab_ed_values, cab_ed_columns, terminal, "TERMINAL")
    _assign_existing_values(cab_ed_values, cab_ed_columns, timezone.localdate(), "FECHA_CREACION")
    _assign_existing_values(cab_ed_values, cab_ed_columns, timezone.localtime(), "FECHA_ACT")
    _assign_existing_values(cab_ed_values, cab_ed_columns, periodo_cont, "PERIODO_CONT")
    _assign_existing_values(cab_ed_values, cab_ed_columns, ejercicio, "EJERCICIO")
    _assign_existing_values(cab_ed_values, cab_ed_columns, cta_asociada, "CTA_ASOCIADA")

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
    det_cliente_col = _pick_existing_column(det_ed_columns, "ID_SN", "CLIENTE", "COD_CLIENTE")
    cuenta_cliente_num = _clip_str(cta_asociada or "11020101", 20) or "11020101"
    cuenta_cliente_nombre = "Cuentas por Cobrar Clientes"
    cuenta_ingreso_num = "41010101"
    cuenta_ingreso_nombre = "Ingresos por Ventas de Mercancias"
    cuenta_costo_num = "51010101"
    cuenta_costo_nombre = "Costo de Ventas de Mercancias"
    cuenta_inventario_num = "11030101"
    cuenta_inventario_nombre = "Mercancia Disponible para la Venta"
    total_cantidad = _to_decimal(total_cantidad)

    def _build_det_ed_values(*, line_no, id_sn_value, cuenta_num, cuenta_nombre, debito, credito):
        det_ed_values = {}
        _assign_existing_values(det_ed_values, det_ed_columns, ed_doc_id, "ID_DOC", "ID_ED")
        _assign_existing_values(det_ed_values, det_ed_columns, ed_doc_no, "NO_DOC", "NO_ED")
        if det_line_col and det_line_col not in det_ed_identity_columns:
            _assign_existing_values(det_ed_values, det_ed_columns, line_no, det_line_col)
        _assign_existing_values(det_ed_values, det_ed_columns, fecha_cont, "FECHA_CONT", "F_CONT")
        _assign_existing_values(det_ed_values, det_ed_columns, fecha_doc, "FECHA_DOC", "FECHA_APLIC")
        _assign_existing_values(det_ed_values, det_ed_columns, fecha_venc, "FECHA_VENC", "F_VENC")
        if id_sn_value is None and det_cliente_col:
            det_ed_values[det_cliente_col] = None
        else:
            _assign_existing_values(det_ed_values, det_ed_columns, id_sn_value, "ID_SN", "CLIENTE", "COD_CLIENTE")
        _assign_existing_values(det_ed_values, det_ed_columns, nombre_cliente, "NOM_SN", "NOM_SOCIO", "NOMBRE", "NOM_CLIENTE")
        _assign_existing_values(det_ed_values, det_ed_columns, rnc_ced, "RNC_CED", "RNC", "CEDULA")
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
        _assign_existing_values(det_ed_values, det_ed_columns, "FC", "TIPO_DOC", "TD", "CLASE_DOC", "TIPO")
        _assign_existing_values(det_ed_values, det_ed_columns, factura_id, "ORIGEN", "REFERENCIA", "NO_RECIBO")
        _assign_existing_values(det_ed_values, det_ed_columns, prefactura_id or None, "REFERENCIA1")
        _assign_existing_values(det_ed_values, det_ed_columns, debito, "DEBITO", "DEBE")
        _assign_existing_values(det_ed_values, det_ed_columns, credito, "CREDITO", "HABER")
        _assign_existing_values(det_ed_values, det_ed_columns, "RD$", "MON_DOC", "MONEDA")
        _assign_existing_values(det_ed_values, det_ed_columns, comentario_ed, "COMENTARIO", "OBSERVACION")
        _assign_existing_values(det_ed_values, det_ed_columns, cta_asociada, "CTA_ASOCIADA")
        _assign_existing_values(det_ed_values, det_ed_columns, usuario_id, "ID_USUARIO", "USUARIO_ID")
        _assign_existing_values(det_ed_values, det_ed_columns, usuario_nombre, "USUARIO", "USUARIO_NOMBRE")
        _assign_existing_values(det_ed_values, det_ed_columns, terminal, "TERMINAL")
        _assign_existing_values(det_ed_values, det_ed_columns, periodo_cont, "PERIODO_CONT")
        _assign_existing_values(det_ed_values, det_ed_columns, ejercicio, "EJERCICIO")
        if str(cuenta_num or "").strip() in {"41010101", "51010101"}:
            _assign_existing_values(det_ed_values, det_ed_columns, "C01", "CECO")
            _assign_existing_values(det_ed_values, det_ed_columns, "P01", "CEBE")
        _assign_existing_values(det_ed_values, det_ed_columns, timezone.localdate(), "FECHA_CREACION")
        _assign_existing_values(det_ed_values, det_ed_columns, timezone.localtime(), "FECHA_ACT")
        return det_ed_values

    for line_no, line_id_sn, cuenta_num, cuenta_nombre, debito, credito in (
        (1, id_sn, cuenta_cliente_num, cuenta_cliente_nombre, total_factura, Decimal("0")),
        (2, None, cuenta_ingreso_num, cuenta_ingreso_nombre, Decimal("0"), total_factura),
        (3, None, cuenta_costo_num, cuenta_costo_nombre, total_cantidad, Decimal("0")),
        (4, None, cuenta_inventario_num, cuenta_inventario_nombre, Decimal("0"), total_cantidad),
    ):
        _insert_dynamic_row(
            cursor,
            "DET_ED",
            det_ed_columns,
            _build_det_ed_values(
                line_no=line_no,
                id_sn_value=line_id_sn,
                cuenta_num=cuenta_num,
                cuenta_nombre=cuenta_nombre,
                debito=debito,
                credito=credito,
            ),
            skip_columns=det_ed_identity_columns,
        )

    return ed_doc_no or ed_doc_id


def _sync_factura_ed_entries(
    cursor,
    *,
    factura_id,
    prefactura_id,
    id_sn,
    nombre_cliente,
    rnc_ced,
    fecha_cont,
    fecha_doc,
    fecha_venc,
    total_factura,
    comentario,
    periodo_cont,
    ejercicio,
    usuario_id,
    usuario_nombre,
    terminal,
    cta_asociada,
    total_cantidad,
    existing_ed_no,
):
    existing_ed_no = _stringify_doc(existing_ed_no)
    if not existing_ed_no:
        return _create_factura_ed_entries(
            cursor,
            factura_id=factura_id,
            prefactura_id=prefactura_id,
            id_sn=id_sn,
            nombre_cliente=nombre_cliente,
            rnc_ced=rnc_ced,
            fecha_cont=fecha_cont,
            fecha_doc=fecha_doc,
            fecha_venc=fecha_venc,
            total_factura=total_factura,
            comentario=comentario,
            periodo_cont=periodo_cont,
            ejercicio=ejercicio,
            usuario_id=usuario_id,
            usuario_nombre=usuario_nombre,
            terminal=terminal,
            cta_asociada=cta_asociada,
            total_cantidad=total_cantidad,
        )

    cab_ed_columns = _load_table_columns(cursor, "CAB_ED")
    det_ed_columns = _load_table_columns(cursor, "DET_ED")
    if not cab_ed_columns or not det_ed_columns:
        raise ValueError("No se pudieron cargar las tablas CAB_ED/DET_ED.")

    det_ed_identity_columns = _load_identity_columns(cursor, "DET_ED")
    cab_ed_key_col = _pick_existing_column(cab_ed_columns, "ID_DOC", "ID_ED", "NO_DOC", "NO_ED")
    cab_ed_no_col = _pick_existing_column(cab_ed_columns, "NO_DOC", "NO_ED", "ID_DOC", "ID_ED")
    if not cab_ed_key_col and not cab_ed_no_col:
        raise ValueError("No se pudo determinar la clave de CAB_ED.")

    select_columns = []
    if cab_ed_key_col:
        select_columns.append(f"[{cab_ed_key_col}]")
    if cab_ed_no_col and cab_ed_no_col != cab_ed_key_col:
        select_columns.append(f"[{cab_ed_no_col}]")

    where_parts = []
    where_params = []
    if cab_ed_no_col:
        where_parts.append(f"CAST([{cab_ed_no_col}] AS NVARCHAR(255)) = %s")
        where_params.append(existing_ed_no)
    if cab_ed_key_col and cab_ed_key_col != cab_ed_no_col:
        where_parts.append(f"CAST([{cab_ed_key_col}] AS NVARCHAR(255)) = %s")
        where_params.append(existing_ed_no)

    cursor.execute(
        f"SELECT TOP 1 {', '.join(select_columns)} FROM CAB_ED WITH (UPDLOCK, HOLDLOCK) WHERE {' OR '.join(f'({part})' for part in where_parts)}",
        where_params,
    )
    cab_ed_row = cursor.fetchone()
    if not cab_ed_row:
        return _create_factura_ed_entries(
            cursor,
            factura_id=factura_id,
            prefactura_id=prefactura_id,
            id_sn=id_sn,
            nombre_cliente=nombre_cliente,
            rnc_ced=rnc_ced,
            fecha_cont=fecha_cont,
            fecha_doc=fecha_doc,
            fecha_venc=fecha_venc,
            total_factura=total_factura,
            comentario=comentario,
            periodo_cont=periodo_cont,
            ejercicio=ejercicio,
            usuario_id=usuario_id,
            usuario_nombre=usuario_nombre,
            terminal=terminal,
            cta_asociada=cta_asociada,
            total_cantidad=total_cantidad,
        )

    ed_doc_id = _stringify_doc(cab_ed_row[0] if cab_ed_row else existing_ed_no)
    ed_doc_no = _stringify_doc(cab_ed_row[1] if len(cab_ed_row) > 1 else ed_doc_id or existing_ed_no)
    total_factura = _to_decimal(total_factura)
    total_cantidad = _to_decimal(total_cantidad)
    comentario_ed = str(comentario or "").strip() or f"Factura {factura_id}"

    cab_ed_updates = {}
    _assign_existing_values(cab_ed_updates, cab_ed_columns, fecha_cont, "FECHA_CONT", "F_CONT")
    _assign_existing_values(cab_ed_updates, cab_ed_columns, fecha_doc, "FECHA_DOC", "FECHA_APLIC")
    _assign_existing_values(cab_ed_updates, cab_ed_columns, fecha_venc, "FECHA_VENC", "F_VENC")
    _assign_existing_values(cab_ed_updates, cab_ed_columns, id_sn, "ID_SN", "CLIENTE", "COD_CLIENTE")
    _assign_existing_values(cab_ed_updates, cab_ed_columns, nombre_cliente, "NOM_SN", "NOM_SOCIO", "NOMBRE", "NOM_CLIENTE")
    _assign_existing_values(cab_ed_updates, cab_ed_columns, rnc_ced, "RNC_CED", "RNC", "CEDULA")
    _assign_existing_values(cab_ed_updates, cab_ed_columns, "FC", "TIPO_DOC", "TD", "CLASE_DOC", "TIPO")
    _assign_existing_values(cab_ed_updates, cab_ed_columns, total_factura, "TOTAL_DOC", "MONTO", "IMPORTE")
    _assign_existing_values(cab_ed_updates, cab_ed_columns, Decimal("0"), "ABONO")
    _assign_existing_values(cab_ed_updates, cab_ed_columns, total_factura, "SALDO")
    _assign_existing_values(cab_ed_updates, cab_ed_columns, "RD$", "MON_DOC", "MONEDA")
    _assign_existing_values(cab_ed_updates, cab_ed_columns, comentario_ed, "COMENTARIO", "OBSERVACION")
    _assign_existing_values(cab_ed_updates, cab_ed_columns, "Abierto", "EST_DOC", "ESTADO", "ESTATUS")
    _assign_existing_values(cab_ed_updates, cab_ed_columns, factura_id, "ORIGEN", "REFERENCIA", "NO_RECIBO")
    _assign_existing_values(cab_ed_updates, cab_ed_columns, prefactura_id or None, "REFERENCIA1")
    _assign_existing_values(cab_ed_updates, cab_ed_columns, usuario_id, "ID_USUARIO", "USUARIO_ID")
    _assign_existing_values(cab_ed_updates, cab_ed_columns, usuario_nombre, "USUARIO", "USUARIO_NOMBRE")
    _assign_existing_values(cab_ed_updates, cab_ed_columns, terminal, "TERMINAL")
    _assign_existing_values(cab_ed_updates, cab_ed_columns, timezone.localdate(), "FECHA_CREACION")
    _assign_existing_values(cab_ed_updates, cab_ed_columns, timezone.localtime(), "FECHA_ACT")
    _assign_existing_values(cab_ed_updates, cab_ed_columns, periodo_cont, "PERIODO_CONT")
    _assign_existing_values(cab_ed_updates, cab_ed_columns, ejercicio, "EJERCICIO")
    _assign_existing_values(cab_ed_updates, cab_ed_columns, cta_asociada, "CTA_ASOCIADA")

    _update_dynamic_row(
        cursor,
        "CAB_ED",
        cab_ed_updates,
        " OR ".join(f"({part})" for part in where_parts),
        where_params,
    )

    det_doc_col = _pick_existing_column(det_ed_columns, "ID_DOC", "ID_ED")
    det_no_col = _pick_existing_column(det_ed_columns, "NO_DOC", "NO_ED")
    det_where_parts = []
    det_where_params = []
    if det_doc_col and ed_doc_id:
        det_where_parts.append(f"CAST([{det_doc_col}] AS NVARCHAR(255)) = %s")
        det_where_params.append(ed_doc_id)
    if det_no_col and ed_doc_no:
        det_where_parts.append(f"CAST([{det_no_col}] AS NVARCHAR(255)) = %s")
        det_where_params.append(ed_doc_no)
    if det_where_parts:
        cursor.execute(
            f"DELETE FROM DET_ED WHERE {' OR '.join(f'({part})' for part in det_where_parts)}",
            det_where_params,
        )

    det_line_col = _pick_existing_column(det_ed_columns, "NO_LINEA", "LINEA", "NO_ITEM", "ORDEN")
    det_cliente_col = _pick_existing_column(det_ed_columns, "ID_SN", "CLIENTE", "COD_CLIENTE")
    cuenta_cliente_num = _clip_str(cta_asociada or "11020101", 20) or "11020101"
    cuenta_cliente_nombre = "Cuentas por Cobrar Clientes"
    cuenta_ingreso_num = "41010101"
    cuenta_ingreso_nombre = "Ingresos por Ventas de Mercancias"
    cuenta_costo_num = "51010101"
    cuenta_costo_nombre = "Costo de Ventas de Mercancias"
    cuenta_inventario_num = "11030101"
    cuenta_inventario_nombre = "Mercancia Disponible para la Venta"

    def _build_det_ed_values(*, line_no, id_sn_value, cuenta_num, cuenta_nombre, debito, credito):
        det_ed_values = {}
        _assign_existing_values(det_ed_values, det_ed_columns, ed_doc_id, "ID_DOC", "ID_ED")
        _assign_existing_values(det_ed_values, det_ed_columns, ed_doc_no, "NO_DOC", "NO_ED")
        if det_line_col and det_line_col not in det_ed_identity_columns:
            _assign_existing_values(det_ed_values, det_ed_columns, line_no, det_line_col)
        _assign_existing_values(det_ed_values, det_ed_columns, fecha_cont, "FECHA_CONT", "F_CONT")
        _assign_existing_values(det_ed_values, det_ed_columns, fecha_doc, "FECHA_DOC", "FECHA_APLIC")
        _assign_existing_values(det_ed_values, det_ed_columns, fecha_venc, "FECHA_VENC", "F_VENC")
        if id_sn_value is None and det_cliente_col:
            det_ed_values[det_cliente_col] = None
        else:
            _assign_existing_values(det_ed_values, det_ed_columns, id_sn_value, "ID_SN", "CLIENTE", "COD_CLIENTE")
        _assign_existing_values(det_ed_values, det_ed_columns, nombre_cliente, "NOM_SN", "NOM_SOCIO", "NOMBRE", "NOM_CLIENTE")
        _assign_existing_values(det_ed_values, det_ed_columns, rnc_ced, "RNC_CED", "RNC", "CEDULA")
        _assign_existing_values(det_ed_values, det_ed_columns, cuenta_num, "CTA_LM", "NUM_CTA", "CTA")
        _assign_existing_values(det_ed_values, det_ed_columns, cuenta_nombre, "NOM_CTA", "NOMBRE_CTA", "NOM_CUENTA", "NOMBRE_CUENTA", "NOMBRE")
        _assign_existing_values(det_ed_values, det_ed_columns, "FC", "TIPO_DOC", "TD", "CLASE_DOC", "TIPO")
        _assign_existing_values(det_ed_values, det_ed_columns, factura_id, "ORIGEN", "REFERENCIA", "NO_RECIBO")
        _assign_existing_values(det_ed_values, det_ed_columns, prefactura_id or None, "REFERENCIA1")
        _assign_existing_values(det_ed_values, det_ed_columns, debito, "DEBITO", "DEBE")
        _assign_existing_values(det_ed_values, det_ed_columns, credito, "CREDITO", "HABER")
        _assign_existing_values(det_ed_values, det_ed_columns, "RD$", "MON_DOC", "MONEDA")
        _assign_existing_values(det_ed_values, det_ed_columns, comentario_ed, "COMENTARIO", "OBSERVACION")
        _assign_existing_values(det_ed_values, det_ed_columns, cta_asociada, "CTA_ASOCIADA")
        _assign_existing_values(det_ed_values, det_ed_columns, usuario_id, "ID_USUARIO", "USUARIO_ID")
        _assign_existing_values(det_ed_values, det_ed_columns, usuario_nombre, "USUARIO", "USUARIO_NOMBRE")
        _assign_existing_values(det_ed_values, det_ed_columns, terminal, "TERMINAL")
        _assign_existing_values(det_ed_values, det_ed_columns, periodo_cont, "PERIODO_CONT")
        _assign_existing_values(det_ed_values, det_ed_columns, ejercicio, "EJERCICIO")
        if str(cuenta_num or "").strip() in {"41010101", "51010101"}:
            _assign_existing_values(det_ed_values, det_ed_columns, "C01", "CECO")
            _assign_existing_values(det_ed_values, det_ed_columns, "P01", "CEBE")
        _assign_existing_values(det_ed_values, det_ed_columns, timezone.localdate(), "FECHA_CREACION")
        _assign_existing_values(det_ed_values, det_ed_columns, timezone.localtime(), "FECHA_ACT")
        return det_ed_values

    for line_no, line_id_sn, cuenta_num, cuenta_nombre, debito, credito in (
        (1, id_sn, cuenta_cliente_num, cuenta_cliente_nombre, total_factura, Decimal("0")),
        (2, None, cuenta_ingreso_num, cuenta_ingreso_nombre, Decimal("0"), total_factura),
        (3, None, cuenta_costo_num, cuenta_costo_nombre, total_cantidad, Decimal("0")),
        (4, None, cuenta_inventario_num, cuenta_inventario_nombre, Decimal("0"), total_cantidad),
    ):
        _insert_dynamic_row(
            cursor,
            "DET_ED",
            det_ed_columns,
            _build_det_ed_values(
                line_no=line_no,
                id_sn_value=line_id_sn,
                cuenta_num=cuenta_num,
                cuenta_nombre=cuenta_nombre,
                debito=debito,
                credito=credito,
            ),
            skip_columns=det_ed_identity_columns,
        )

    return ed_doc_no or ed_doc_id or existing_ed_no


def _load_factura_detail_quantities(cursor, factura_id):
    cursor.execute(
        """
        SELECT ID_ARTICULO, ISNULL(SUM(CANTIDAD), 0)
        FROM DET_FACTURA
        WHERE TRY_CAST(ID_DOC AS BIGINT) = %s
          AND NULLIF(LTRIM(RTRIM(ISNULL(ID_ARTICULO, ''))), '') IS NOT NULL
        GROUP BY ID_ARTICULO
        """,
        [factura_id],
    )
    quantities = {}
    for articulo_id, cantidad in cursor.fetchall():
        codigo = str(articulo_id or "").strip()
        if not codigo:
            continue
        quantities[codigo] = _to_decimal(cantidad)
    return quantities


def _restore_factura_stock(cursor, factura_id):
    # Fetch invoice header details
    cursor.execute(
        """
        SELECT ID_SN, NOM_SOCIO, ID_VENDEDOR, ID_USUARIO, TERMINAL, FECHA_CONT, FECHA_VENC, FECHA_DOC
        FROM CAB_FACTURA
        WHERE TRY_CAST(ID_DOC AS BIGINT) = %s
        """,
        [factura_id],
    )
    cab = cursor.fetchone()
    if not cab:
        return
    id_sn, nom_socio, id_vendedor, id_usuario, terminal, fecha_cont, fecha_venc, fecha_doc = cab

    # Fetch invoice lines details
    cursor.execute(
        """
        SELECT ID_ARTICULO, DESCRIP_ART, CANTIDAD, PRECIO, ID_ALMACEN
        FROM DET_FACTURA
        WHERE TRY_CAST(ID_DOC AS BIGINT) = %s
        """,
        [factura_id],
    )
    detalles = cursor.fetchall()

    tarjetero_columns = _load_table_columns(cursor, "TARJETERO")
    if tarjetero_columns:
        tarjetero_identity_columns = (
            _load_identity_columns(cursor, "TARJETERO") if tarjetero_columns else set()
        )
        for det in detalles:
            id_articulo, descrip_art, cantidad, precio, id_almacen = det
            if not id_articulo or cantidad is None or cantidad <= 0:
                continue

            cantidad_pos = Decimal(str(cantidad))
            total_costo_t = cantidad_pos
            costo_t = Decimal("1")
            total_precio_t = total_costo_t * Decimal(str(precio or 0))

            # Safe extraction of month/year
            month_val = int(timezone.localdate().month)
            year_val = int(timezone.localdate().year)
            if fecha_cont:
                if hasattr(fecha_cont, "month"):
                    month_val = int(fecha_cont.month)
                elif isinstance(fecha_cont, str):
                    try:
                        parsed_dt = datetime.strptime(fecha_cont[:10], "%Y-%m-%d")
                        month_val = parsed_dt.month
                    except Exception:
                        pass
                if hasattr(fecha_cont, "year"):
                    year_val = int(fecha_cont.year)
                elif isinstance(fecha_cont, str):
                    try:
                        parsed_dt = datetime.strptime(fecha_cont[:10], "%Y-%m-%d")
                        year_val = parsed_dt.year
                    except Exception:
                        pass

            tarj_values = {}
            _assign_existing_values(tarj_values, tarjetero_columns, "NC", "TIPO_DOC", "TD", "CLASE_DOC", "TIPO")
            _assign_existing_values(tarj_values, tarjetero_columns, factura_id, "ID_DOC", "NO_DOC", "NO", "DOCUMENTO")
            _assign_existing_values(tarj_values, tarjetero_columns, id_sn, "ID_SN", "ID_CLIENTE")
            _assign_existing_values(tarj_values, tarjetero_columns, nom_socio, "NOM_SN", "NOM_SOCIO", "NOMBRE_SN")
            _assign_existing_values(tarj_values, tarjetero_columns, _clip_str("11020101", 20), "CTA_ASOCIADA")
            _assign_existing_values(tarj_values, tarjetero_columns, id_articulo, "ID_ARTICULO", "ARTICULO", "COD_ART")
            _assign_existing_values(tarj_values, tarjetero_columns, descrip_art, "DESCRIP_ART", "DESCRIPCION", "DESCRIP")
            _assign_existing_values(tarj_values, tarjetero_columns, cantidad_pos, "CANTIDAD", "CANT")
            _assign_existing_values(tarj_values, tarjetero_columns, total_costo_t, "TOTAL_COSTO")
            _assign_existing_values(tarj_values, tarjetero_columns, costo_t, "COSTO", "COSTO_UNIT", "COSTO_UNITARIO")
            _assign_existing_values(tarj_values, tarjetero_columns, precio, "PRECIO", "PRECIO_UNIT", "PRECIO_UNITARIO")
            _assign_existing_values(tarj_values, tarjetero_columns, total_precio_t, "TOTAL_PRECIO", "TOTAL_NETO")
            _assign_existing_values(tarj_values, tarjetero_columns, "No", "LOTE")
            _assign_existing_values(tarj_values, tarjetero_columns, fecha_cont, "FECHA_CONT", "F_CONT")
            _assign_existing_values(tarj_values, tarjetero_columns, fecha_venc, "FECHA_VENC", "F_VENC")
            _assign_existing_values(tarj_values, tarjetero_columns, fecha_doc, "FECHA_DOC", "F_DOC")
            _assign_existing_values(tarj_values, tarjetero_columns, "RD$", "MONEDA", "MON_DOC")
            _assign_existing_values(tarj_values, tarjetero_columns, timezone.localdate(), "FECHA_CREACION")
            _assign_existing_values(tarj_values, tarjetero_columns, month_val, "PERIODO_CONT")
            _assign_existing_values(tarj_values, tarjetero_columns, year_val, "EJERCICIO")
            _assign_existing_values(tarj_values, tarjetero_columns, id_almacen or 1, "ID_ALMACEN", "ALM", "ALMACEN")
            _assign_existing_values(tarj_values, tarjetero_columns, id_vendedor, "ID_VENDEDOR", "VENDEDOR")
            _assign_existing_values(tarj_values, tarjetero_columns, id_usuario, "ID_USUARIO", "USUARIO_ID")
            _assign_existing_values(tarj_values, tarjetero_columns, _clip_str("41010101", 20), "CTA_INGRESO")
            _assign_existing_values(tarj_values, tarjetero_columns, _clip_str("21020301", 20), "CTA_IMPTO_VT", "CTA_IMPTO")
            _insert_dynamic_row(
                cursor,
                "TARJETERO",
                tarjetero_columns,
                tarj_values,
                skip_columns=tarjetero_identity_columns,
            )


def _create_factura_cancel_ed_entries(
    cursor,
    *,
    factura_id,
    no_ed="",
    usuario_id,
    usuario_nombre,
    terminal,
):
    cab_ed_columns = _load_table_columns(cursor, "CAB_ED")
    det_ed_columns = _load_table_columns(cursor, "DET_ED")
    if not cab_ed_columns or not det_ed_columns:
        raise ValueError("No se pudieron cargar las tablas CAB_ED/DET_ED.")

    cab_ed_identity_columns = _load_identity_columns(cursor, "CAB_ED")
    det_ed_identity_columns = _load_identity_columns(cursor, "DET_ED")
    cab_ed_key_col = _pick_existing_column(cab_ed_columns, "ID_DOC", "ID_ED", "NO_DOC", "NO_ED")
    cab_ed_no_col = _pick_existing_column(cab_ed_columns, "NO_DOC", "NO_ED", "ID_DOC", "ID_ED")
    cab_ed_tipo_col = _pick_existing_column(cab_ed_columns, "TIPO_DOC", "TD", "CLASE_DOC", "TIPO")
    cab_ed_origen_col = _pick_existing_column(cab_ed_columns, "ORIGEN", "REFERENCIA", "NO_RECIBO")
    cab_ed_status_col = _pick_existing_column(cab_ed_columns, "EST_DOC", "ESTADO", "ESTATUS")
    if not cab_ed_key_col and not cab_ed_no_col:
        raise ValueError("No se pudo determinar la clave de CAB_ED.")

    where_sql_parts = []
    where_params = []
    factura_ref = _stringify_doc(factura_id)
    no_ed = _stringify_doc(no_ed)
    if cab_ed_origen_col and factura_ref:
        where_sql_parts.append(f"CAST([{cab_ed_origen_col}] AS NVARCHAR(255)) = %s")
        where_params.append(factura_ref)
    if cab_ed_no_col and no_ed:
        where_sql_parts.append(f"CAST([{cab_ed_no_col}] AS NVARCHAR(255)) = %s")
        where_params.append(no_ed)
    if cab_ed_key_col and no_ed and cab_ed_key_col != cab_ed_no_col:
        where_sql_parts.append(f"CAST([{cab_ed_key_col}] AS NVARCHAR(255)) = %s")
        where_params.append(no_ed)
    if not where_sql_parts:
        raise ValueError("No se pudo identificar el CAB_ED de la factura.")

    extra_filters = [f"({' OR '.join(f'({part})' for part in where_sql_parts)})"]
    if cab_ed_tipo_col:
        extra_filters.append(f"UPPER(LTRIM(RTRIM(ISNULL([{cab_ed_tipo_col}], '')))) = 'FC'")
    if cab_ed_status_col:
        extra_filters.append(f"UPPER(LTRIM(RTRIM(ISNULL([{cab_ed_status_col}], '')))) <> 'CANCELADO'")

    order_column = cab_ed_no_col or cab_ed_key_col
    cursor.execute(
        f"""
        SELECT TOP 1 *
        FROM CAB_ED WITH (UPDLOCK, HOLDLOCK)
        WHERE {" AND ".join(extra_filters)}
        ORDER BY [{order_column}] DESC
        """,
        where_params,
    )
    raw_cab_ed = cursor.fetchone()
    if not raw_cab_ed:
        raise ValueError("No se encontro el CAB_ED abierto asociado a la factura.")
    raw_cab_ed_columns = [col[0] for col in cursor.description]
    original_cab_ed = _normalize_result_row(raw_cab_ed_columns, raw_cab_ed)

    original_cab_ed_id = _stringify_doc(_pick_row_value(original_cab_ed, cab_ed_key_col, cab_ed_no_col))
    original_cab_ed_no = _stringify_doc(_pick_row_value(original_cab_ed, cab_ed_no_col, cab_ed_key_col))
    if not original_cab_ed_id and not original_cab_ed_no:
        raise ValueError("No se pudo identificar el CAB_ED de la factura.")

    cancel_comment = _append_cancelled_comment(_pick_row_text(original_cab_ed, "COMENTARIO", "OBSERVACION"))
    original_cab_ed_updates = {}
    _assign_existing_values(original_cab_ed_updates, cab_ed_columns, "Cancelado", "EST_DOC", "ESTADO", "ESTATUS")
    _assign_existing_values(original_cab_ed_updates, cab_ed_columns, cancel_comment, "COMENTARIO", "OBSERVACION")
    _assign_existing_values(original_cab_ed_updates, cab_ed_columns, timezone.localtime(), "FECHA_ACT")
    original_cab_ed_lookup_col = cab_ed_key_col if original_cab_ed_id else cab_ed_no_col
    original_cab_ed_lookup_value = original_cab_ed_id if original_cab_ed_id else original_cab_ed_no
    _update_dynamic_row(
        cursor,
        "CAB_ED",
        original_cab_ed_updates,
        f"CAST([{original_cab_ed_lookup_col}] AS NVARCHAR(255)) = %s",
        [original_cab_ed_lookup_value],
    )

    next_ed_no = None
    if cab_ed_no_col and cab_ed_no_col not in cab_ed_identity_columns:
        next_ed_no = _next_table_numeric_value(cursor, "CAB_ED", cab_ed_no_col)
    elif cab_ed_key_col and cab_ed_key_col not in cab_ed_identity_columns:
        next_ed_no = _next_table_numeric_value(cursor, "CAB_ED", cab_ed_key_col)

    cancel_cab_ed_values = {
        column: original_cab_ed.get(column)
        for column in cab_ed_columns
        if column in original_cab_ed and column not in cab_ed_identity_columns
    }
    if cab_ed_key_col and cab_ed_key_col not in cab_ed_identity_columns:
        cancel_cab_ed_values.pop(cab_ed_key_col, None)
        _assign_existing_values(cancel_cab_ed_values, cab_ed_columns, next_ed_no, cab_ed_key_col)
    if next_ed_no is not None:
        cancel_cab_ed_values.pop("NO_DOC", None)
        cancel_cab_ed_values.pop("NO_ED", None)
        _assign_existing_values(cancel_cab_ed_values, cab_ed_columns, next_ed_no, "NO_DOC", "NO_ED")
    _assign_existing_values(cancel_cab_ed_values, cab_ed_columns, "Cancelado", "EST_DOC", "ESTADO", "ESTATUS")
    _assign_existing_values(cancel_cab_ed_values, cab_ed_columns, cancel_comment, "COMENTARIO", "OBSERVACION")
    _assign_existing_values(cancel_cab_ed_values, cab_ed_columns, usuario_id, "ID_USUARIO", "USUARIO_ID")
    _assign_existing_values(cancel_cab_ed_values, cab_ed_columns, usuario_nombre, "USUARIO", "USUARIO_NOMBRE")
    _assign_existing_values(cancel_cab_ed_values, cab_ed_columns, terminal, "TERMINAL")
    _assign_existing_values(cancel_cab_ed_values, cab_ed_columns, timezone.localdate(), "FECHA_CREACION")
    _assign_existing_values(cancel_cab_ed_values, cab_ed_columns, timezone.localtime(), "FECHA_ACT")

    inserted_cancel_ed_id = _insert_dynamic_row(
        cursor,
        "CAB_ED",
        cab_ed_columns,
        cancel_cab_ed_values,
        output_column=cab_ed_key_col or cab_ed_no_col,
        skip_columns=cab_ed_identity_columns,
    )
    cancel_ed_doc_id = _stringify_doc(inserted_cancel_ed_id or next_ed_no or "")
    cancel_ed_doc_no = _stringify_doc(next_ed_no or inserted_cancel_ed_id or cancel_ed_doc_id)

    det_ed_doc_key_col = _pick_existing_column(det_ed_columns, "ID_DOC", "ID_ED")
    det_ed_doc_no_col = _pick_existing_column(det_ed_columns, "NO_DOC", "NO_ED")
    det_ed_line_col = _pick_existing_column(det_ed_columns, "NO_LINEA", "LINEA", "NO_ITEM", "ORDEN")
    if not det_ed_doc_key_col and not det_ed_doc_no_col:
        raise ValueError("No se pudo determinar la relacion de DET_ED con CAB_ED.")

    det_ed_where_parts = []
    det_ed_where_params = []
    original_det_ed_doc_key_value = (
        original_cab_ed_no
        if det_ed_doc_key_col and det_ed_doc_no_col and det_ed_doc_key_col == det_ed_doc_no_col and original_cab_ed_no
        else original_cab_ed_id or original_cab_ed_no
    )
    if det_ed_doc_key_col and original_det_ed_doc_key_value:
        det_ed_where_parts.append(f"CAST([{det_ed_doc_key_col}] AS NVARCHAR(255)) = %s")
        det_ed_where_params.append(original_det_ed_doc_key_value)
    if det_ed_doc_no_col and original_cab_ed_no and (not original_cab_ed_id or det_ed_doc_no_col != det_ed_doc_key_col):
        det_ed_where_parts.append(f"CAST([{det_ed_doc_no_col}] AS NVARCHAR(255)) = %s")
        det_ed_where_params.append(original_cab_ed_no)
    if not det_ed_where_parts:
        raise ValueError("No se pudo identificar el detalle DET_ED de la factura.")

    det_ed_sql = f"SELECT * FROM DET_ED WITH (UPDLOCK, HOLDLOCK) WHERE {' OR '.join(f'({part})' for part in det_ed_where_parts)}"
    if det_ed_line_col:
        det_ed_sql += f" ORDER BY [{det_ed_line_col}]"
    cursor.execute(det_ed_sql, det_ed_where_params)
    raw_det_ed_columns = [col[0] for col in cursor.description]
    original_det_ed_rows = [_normalize_result_row(raw_det_ed_columns, raw_row) for raw_row in cursor.fetchall()]
    if not original_det_ed_rows:
        raise ValueError("No se encontraron registros en DET_ED para la factura.")

    for line_no, original_det_ed in enumerate(original_det_ed_rows, start=1):
        debito_original = _to_decimal(_pick_row_value(original_det_ed, "DEBITO", "DEBE", default=0))
        credito_original = _to_decimal(_pick_row_value(original_det_ed, "CREDITO", "HABER", default=0))
        cancel_det_ed_values = {
            column: original_det_ed.get(column)
            for column in det_ed_columns
            if column in original_det_ed and column not in det_ed_identity_columns
        }
        _assign_existing_values(cancel_det_ed_values, det_ed_columns, cancel_ed_doc_id, "ID_DOC", "ID_ED")
        _assign_existing_values(cancel_det_ed_values, det_ed_columns, cancel_ed_doc_no, "NO_DOC", "NO_ED")
        if det_ed_line_col and det_ed_line_col not in det_ed_identity_columns:
            cancel_det_ed_values.pop(det_ed_line_col, None)
            _assign_existing_values(cancel_det_ed_values, det_ed_columns, line_no, det_ed_line_col)
        _assign_existing_values(cancel_det_ed_values, det_ed_columns, credito_original, "DEBITO", "DEBE")
        _assign_existing_values(cancel_det_ed_values, det_ed_columns, debito_original, "CREDITO", "HABER")
        _assign_existing_values(cancel_det_ed_values, det_ed_columns, cancel_comment, "COMENTARIO", "OBSERVACION")
        _assign_existing_values(cancel_det_ed_values, det_ed_columns, usuario_id, "ID_USUARIO", "USUARIO_ID")
        _assign_existing_values(cancel_det_ed_values, det_ed_columns, usuario_nombre, "USUARIO", "USUARIO_NOMBRE")
        _assign_existing_values(cancel_det_ed_values, det_ed_columns, terminal, "TERMINAL")
        _assign_existing_values(cancel_det_ed_values, det_ed_columns, timezone.localdate(), "FECHA_CREACION")
        _assign_existing_values(cancel_det_ed_values, det_ed_columns, timezone.localtime(), "FECHA_ACT")
        _insert_dynamic_row(
            cursor,
            "DET_ED",
            det_ed_columns,
            cancel_det_ed_values,
            skip_columns=det_ed_identity_columns,
        )

    return cancel_ed_doc_no or cancel_ed_doc_id


def _inferir_tipo_ecf(tipo_texto, id_ncf):
    normalized = str(tipo_texto or "").strip().upper()
    if id_ncf in (31, 32, 33, 34, 41, 43, 44, 45, 46, 47):
        return str(int(id_ncf))
    if "CREDITO FISCAL" in normalized:
        return "31"
    if "CONSUMO" in normalized:
        return "32"
    if "DEBITO" in normalized:
        return "33"
    if "CREDITO" in normalized and "NOTA" in normalized:
        return "34"
    if "COMPRA" in normalized:
        return "41"
    if "GASTO" in normalized:
        return "43"
    if "REGIMEN" in normalized:
        return "44"
    if "GUBERN" in normalized:
        return "45"
    if "EXTERIOR" in normalized:
        return "46"
    if "EXPORT" in normalized:
        return "47"
    return ""


def _build_qr_url(empresa_rnc, documento):
    if not empresa_rnc or not documento.encf or not documento.codigo_seguridad:
        return ""
    monto_total = f"{Decimal(documento.monto_total or 0):.2f}"
    if documento.tipo_ecf == "32" and Decimal(documento.monto_total or 0) < Decimal("250000"):
        return (
            "https://fc.dgii.gov.do/eCF/ConsultaTimbreFC"
            f"?RNCEmisor={empresa_rnc}&ENCF={documento.encf}"
            f"&MontoTotal={monto_total}&CodigoSeguridad={documento.codigo_seguridad}"
        )
    fecha_doc = timezone.localtime(documento.fecha_doc).strftime("%d-%m-%Y") if documento.fecha_doc else ""
    fecha_firma = timezone.localtime(documento.actualizado_en).strftime("%d-%m-%Y %H:%M:%S") if documento.actualizado_en else ""
    return (
        "https://ecf.dgii.gov.do/ecf/ConsultaTimbre"
        f"?RncEmisor={empresa_rnc}"
        f"&RncComprador={documento.cliente_rnc or ''}"
        f"&ENCF={documento.encf}"
        f"&FechaEmision={fecha_doc}"
        f"&MontoTotal={monto_total}"
        f"&FechaFirma={fecha_firma}"
        f"&CodigoSeguridad={documento.codigo_seguridad}"
    )


def _build_qr_image_url(qr_target_url):
    if not qr_target_url:
        return ""
    from urllib.parse import quote
    return f"https://api.qrserver.com/v1/create-qr-code/?size=150x150&data={quote(qr_target_url, safe='')}"


def _ensure_ecf_sequence(tipo_ecf):
    config = FacturacionElectronicaConfig.objects.filter(id_config=1).first()
    encf = ""
    if config and config.habilitado:
        secuencia = FacturacionElectronicaSecuencia.objects.select_for_update().filter(
            tipo_ecf=tipo_ecf,
            habilitada=True,
        ).first()
        if not secuencia:
            raise ValueError(f"No hay secuencia e-CF habilitada para el tipo {tipo_ecf}.")
        encf = f"E{tipo_ecf}{int(secuencia.secuencia_actual):010d}"
        secuencia.secuencia_actual = int(secuencia.secuencia_actual) + 1
        secuencia.save(update_fields=["secuencia_actual", "actualizado_en"])
    return config, encf


def _tipo_descripcion(tipo_ecf):
    return ECF_TIPOS_MAP.get(str(tipo_ecf or "").strip(), "Factura Electronica")


def _parse_request_payload(request):
    if request.content_type and "application/json" in request.content_type.lower():
        try:
            body = request.body.decode("utf-8") if request.body else "{}"
            parsed = json.loads(body or "{}")
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return None
    if request.POST:
        return request.POST.dict()
    return {}


def _require_any_factura_perm_json(request, *perm_codes):
    auth_payload = _get_auth_payload(request)
    if not auth_payload:
        return JsonResponse({"detail": "No autenticado"}, status=401)
    usuario_id = auth_payload.get("usuario_id")
    if any(has_perm(usuario_id, "factura", perm_code) for perm_code in perm_codes if perm_code):
        return auth_payload
    return JsonResponse({"detail": "Acceso denegado."}, status=403)


def _build_factura_screen_context(mode):
    is_manual = str(mode or "").strip().lower() == "manual"
    return {
        "factura_mode": "manual" if is_manual else "electronica",
        "factura_screen_title": "Facturacion" if is_manual else "Emision Factura Electronica",
        "factura_screen_subtitle": (
            "Facturacion normal separada de prefacturas, con carga desde prefacturas activas y sin e-CF."
            if is_manual
            else "Pantalla independiente para emitir facturas electronicas a partir de prefacturas activas."
        ),
        "factura_pref_card_title": "Prefacturas Activas",
        "factura_pref_card_subtitle": (
            "Selecciona una prefactura activa para cargarla a la factura."
            if is_manual
            else "Haz doble click sobre una prefactura para cargarla en la factura electronica."
        ),
        "factura_form_title": "Factura" if is_manual else "Factura Electronica",
        "factura_form_subtitle": (
            "Los datos se autocompletan desde la prefactura seleccionada. Este flujo no genera e-CF."
            if is_manual
            else "Los datos se autocompletan desde la prefactura seleccionada."
        ),
        "factura_emit_button_label": "Emitir factura" if is_manual else "Emitir factura electronica",
        "factura_emit_loading_label": "Generando factura..." if is_manual else "Generando factura electronica...",
        "factura_emit_error_label": "No se pudo emitir la factura." if is_manual else "No se pudo emitir la factura electronica.",
        "factura_emit_success_label": "Factura" if is_manual else "Factura electronica",
        "factura_allow_type_selector": not is_manual,
        "factura_allow_cancel": not is_manual,
        "factura_allow_encf_history": not is_manual,
        "factura_history_title": "Facturas Emitidas" if is_manual else "Facturas Electronicas Emitidas",
        "factura_history_subtitle": (
            "Buscador para reimprimir facturas normales emitidas desde este modulo."
            if is_manual
            else "Buscador para reimprimir facturas electronicas emitidas desde este modulo."
        ),
        "factura_history_placeholder": (
            "No. factura, cliente o nombre"
            if is_manual
            else "No. factura, cliente, nombre o e-NCF"
        ),
    }


def _find_ecf_document(payload):
    payload = payload or {}
    for field in ("id_doc", "encf", "track_id"):
        value = str(payload.get(field) or "").strip()
        if not value:
            continue
        documento = FacturacionElectronicaDocumento.objects.filter(**{field: value}).first()
        if documento:
            return documento
    return None


def _build_event_response(documento, evento_tipo):
    return {
        "ok": True,
        "evento": evento_tipo,
        "documento": {
            "id_doc": documento.id_doc,
            "encf": documento.encf,
            "track_id": documento.track_id,
            "estado": documento.estado,
            "cliente_rnc": documento.cliente_rnc,
            "cliente_nombre": documento.cliente_nombre,
        },
    }


def _build_public_ecf_urls(request):
    return {
        "recepcion": request.build_absolute_uri(reverse("factura:ecf_recepcion")),
        "aprobacion": request.build_absolute_uri(reverse("factura:ecf_aprobacion")),
    }


def _find_existing_credit_note(cursor, factura_id, encf_hint=""):
    cursor.execute(
        """
        SELECT TOP 1 ID_DOC, ISNULL(NCF, '')
        FROM CAB_FACTURA
        WHERE TRY_CAST(ID_DOC_BASE AS BIGINT) = %s
          AND TRY_CAST(ID_NCF AS BIGINT) = 34
          AND UPPER(ISNULL(CANCELADO, 'N')) <> 'Y'
        ORDER BY TRY_CAST(ID_DOC AS BIGINT) DESC, ID_DOC DESC
        """,
        [factura_id],
    )
    row = cursor.fetchone()
    if row:
        return {
            "id_doc": str(row[0] or "").strip(),
            "encf": str(row[1] or "").strip(),
        }

    encf_hint = str(encf_hint or "").strip()
    if not encf_hint:
        return None

    cursor.execute(
        """
        SELECT TOP 1 ID_DOC, ISNULL(NCF, '')
        FROM CAB_FACTURA
        WHERE TRY_CAST(ID_NCF AS BIGINT) = 34
          AND ISNULL(NCF, '') = %s
        ORDER BY TRY_CAST(ID_DOC AS BIGINT) DESC, ID_DOC DESC
        """,
        [encf_hint],
    )
    row = cursor.fetchone()
    if not row:
        return None
    return {
        "id_doc": str(row[0] or "").strip(),
        "encf": str(row[1] or "").strip(),
    }


def _boolish(value):
    return str(value or "").strip().lower() in {"1", "true", "on", "si", "sí", "yes", "y"}


def _build_inline_image_src(base64_value, image_type):
    encoded = str(base64_value or "").strip()
    if not encoded:
        return ""
    normalized_type = str(image_type or "").strip().lower()
    if normalized_type.startswith("image/"):
        return f"data:{normalized_type};base64,{encoded}"
    if normalized_type:
        return f"data:image/{normalized_type};base64,{encoded}"
    return f"data:image/png;base64,{encoded}"


def _ecf_sequence_error_response(tipo_ecf, exc):
    detail = str(exc or "").strip() or f"No hay secuencia e-CF habilitada para el tipo {tipo_ecf}."
    payload = {"detail": detail}
    if "No hay secuencia e-CF habilitada" in detail:
        payload.update(
            {
                "error_code": "missing_ecf_sequence",
                "tipo_ecf": str(tipo_ecf or "").strip(),
                "help_url": reverse("factura:electronica"),
                "help_label": "Configurar secuencia e-CF",
            }
        )
    return JsonResponse(payload, status=400)


def _require_ecf_callback_auth(request):
    expected = get_ecf_callback_api_key()
    if not expected:
        return None
    provided = str(request.headers.get("X-ECF-API-Key") or "").strip()
    if not provided:
        auth_header = str(request.headers.get("Authorization") or "").strip()
        if auth_header.lower().startswith("bearer "):
            provided = auth_header[7:].strip()
    if provided and hmac.compare_digest(provided, expected):
        return None
    return JsonResponse({"detail": "No autorizado para callback e-CF."}, status=401)


def _apply_demo_qr(documento, empresa_rnc):
    if not documento:
        return False
    base_code = f"DEMO{int(documento.id_doc):010d}"[-10:]
    documento.codigo_seguridad = f"QR{base_code}"
    documento.xml_generado = True
    documento.firmado = True
    documento.enviado_dgii = True
    if not str(documento.estado or "").strip():
        documento.estado = "DEMO_QR"
    documento.url_consulta_qr = _build_qr_url(empresa_rnc, documento)
    documento.save(
        update_fields=[
            "codigo_seguridad",
            "xml_generado",
            "firmado",
            "enviado_dgii",
            "estado",
            "url_consulta_qr",
            "actualizado_en",
        ]
    )
    FacturacionElectronicaEvento.objects.create(
        documento=documento,
        tipo_evento="QR_DEMO",
        detalle="Codigo de seguridad demo generado para validar visualmente el QR.",
    )
    return True


def _dispatch_document_if_needed(documento, request, config):
    dispatch_enabled = should_auto_dispatch(getattr(config, "modo_envio", "manual"))
    result = submit_document(documento, _build_public_ecf_urls(request), dispatch_enabled=dispatch_enabled)
    if not result.attempted:
        return result

    update_fields = ["estado", "respuesta_dgii", "actualizado_en"]
    documento.estado = "ENVIADO_INTEGRADOR" if result.ok else "ERROR_ENVIO_INTEGRADOR"
    documento.respuesta_dgii = result.raw_response or result.message
    if result.track_id:
        documento.track_id = result.track_id
        update_fields.append("track_id")
    documento.save(update_fields=update_fields)
    FacturacionElectronicaEvento.objects.create(
        documento=documento,
        tipo_evento="ENVIADO_INTEGRADOR" if result.ok else "ERROR_ENVIO_INTEGRADOR",
        detalle=result.message,
    )
    return result


def _sync_ecf_documentos(limit=150):
    documentos = []
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT TOP {max(int(limit), 1)} ID_DOC, FECHA_DOC, NOM_SOCIO, RNC_CED, TOTAL_DOC,
                       ISNULL(NCF, ''), ISNULL(TIPO, ''), ISNULL(ID_NCF, 0), ISNULL(EST_DOC, '')
                FROM CAB_PEDIDO
                ORDER BY FECHA_DOC DESC, ID_DOC DESC
                """
            )
            documentos = list(cursor.fetchall())
    except Exception:
        return 0

    sincronizados = 0
    for row in documentos:
        id_doc = _to_int(row[0], 0)
        if id_doc <= 0:
            continue
        fecha_doc = row[1]
        cliente_nombre = str(row[2] or "").strip()
        cliente_rnc = str(row[3] or "").strip()
        monto_total = Decimal(row[4] or 0)
        encf = str(row[5] or "").strip()
        tipo_cab = str(row[6] or "").strip()
        id_ncf = _to_int(row[7], 0)
        est_doc = str(row[8] or "").strip().upper()
        tipo_ecf = _inferir_tipo_ecf(tipo_cab, id_ncf)
        estado = "PENDIENTE"
        if encf.startswith("E"):
            estado = "REGISTRADO"
        elif not tipo_ecf:
            estado = "TIPO_PENDIENTE"
        elif est_doc == "CERRADO":
            estado = "LISTO_PARA_XML"

        defaults = {
            "tipo_ecf": tipo_ecf or None,
            "encf": encf or None,
            "estado": estado,
            "cliente_rnc": cliente_rnc or None,
            "cliente_nombre": cliente_nombre or None,
            "fecha_doc": fecha_doc,
            "monto_total": monto_total,
            "observaciones": tipo_cab or None,
        }
        documento, created = FacturacionElectronicaDocumento.objects.update_or_create(
            id_doc=id_doc,
            defaults=defaults,
        )
        if created:
            FacturacionElectronicaEvento.objects.create(
                documento=documento,
                tipo_evento="SINCRONIZADO",
                detalle="Documento importado desde CAB_PEDIDO para preparacion e-CF.",
            )
        sincronizados += 1
    return sincronizados


def index(request):
    ctx = _base_context(request, page_title="Factura", active_nav="factura")
    if not ctx:
        return redirect("login")
    if not has_perm(ctx["auth_payload"]["usuario_id"], "factura", "ver"):
        return render_denied(request, active_nav="factura")
    ctx["submodules"] = {
        "facturacion": has_perm(ctx["auth_payload"]["usuario_id"], "factura", "ver_documentos"),
        "emision": has_perm(ctx["auth_payload"]["usuario_id"], "factura", "ver_emision"),
        "electronica": has_perm(ctx["auth_payload"]["usuario_id"], "factura", "ver_electronica"),
    }
    return render(request, "factura/index.html", ctx)


def _fmt_date_iso(value):
    if not value:
        return ""
    try:
        return value.strftime("%Y-%m-%d")
    except Exception:
        return ""


def _num(v):
    if v is None:
        return None
    try:
        return float(v)
    except Exception:
        return None


@require_http_methods(["GET"])
def facturacion_view(request):
    ctx = _base_context(request, page_title="Factura - Facturacion", active_nav="factura")
    if not ctx:
        return redirect("login")
    if not has_perm(ctx["auth_payload"]["usuario_id"], "factura", "ver_documentos"):
        return render_denied(request, active_nav="factura")
    usuario_id = ctx["auth_payload"]["usuario_id"]
    ctx["facturacion_shortcuts"] = {
        "cuentas_por_cobrar": has_perm(usuario_id, "caja", "ver_cuentas_por_cobrar"),
        "financiamiento": has_perm(usuario_id, "caja", "ver_financiamiento"),
        "prefactura": has_perm(usuario_id, "prefacturas", "ver"),
    }
    factura_print_format = get_print_format("factura")
    ctx["factura_print_format"] = factura_print_format
    ctx["factura_print_format_label"] = get_print_format_label(factura_print_format)
    return render(request, "factura/facturacion.html", ctx)


@require_http_methods(["GET"])
def emision_view(request):
    ctx = _base_context(request, page_title="Factura - Emision Factura Electronica", active_nav="factura")
    if not ctx:
        return redirect("login")
    if not has_perm(ctx["auth_payload"]["usuario_id"], "factura", "ver_emision"):
        return render_denied(request, active_nav="factura")
    ctx["tipos_emision"] = EMISION_TIPOS
    ctx.update(_build_factura_screen_context("electronica"))
    return render(request, "factura/emision.html", ctx)


@require_http_methods(["GET"])
def emision_prefacturas_view(request):
    auth_payload = _require_any_factura_perm_json(request, "ver_emision", "ver_documentos")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    query = (request.GET.get("q") or "").strip()
    filtro = (request.GET.get("filtro") or "documento").strip().lower()
    lock_owner = str(request.GET.get("lock_owner") or "").strip()
    sql = """
        SELECT TOP 80
            ID_DOC, ID_SN, NOM_SOCIO, RNC_CED, CONTACTO, ENT_FACTURA, ENT_MERCANCIA, EST_DOC,
            FECHA_CONT, FECHA_DOC, FECHA_VENC, COMENTARIO, TOTAL_DOC
        FROM CAB_PEDIDO
        WHERE UPPER(ISNULL(EST_DOC, '')) = 'ABIERTO'
          AND NOT EXISTS (
                SELECT 1
                FROM CAB_FACTURA f
                WHERE (
                        CAST(f.ID_DOC_PV AS VARCHAR(50)) = CAST(CAB_PEDIDO.ID_DOC AS VARCHAR(50))
                     OR (
                            CAST(f.ID_DOC_BASE AS VARCHAR(50)) = CAST(CAB_PEDIDO.ID_DOC AS VARCHAR(50))
                        AND UPPER(ISNULL(f.TIPO_DOC_BASE, '')) IN ('PV', 'PC')
                     )
                     OR (
                            ISNULL(f.REFERENCIA, '') = CAST(CAB_PEDIDO.ID_DOC AS VARCHAR(50))
                        AND UPPER(ISNULL(f.TIPO_DOC_BASE, '')) = 'PC'
                     )
                )
          )
    """
    params = []
    if query:
        if filtro == "cliente":
            sql += " AND ID_SN LIKE %s"
            params.append(f"%{query}%")
        elif filtro == "nombre":
            sql += " AND NOM_SOCIO LIKE %s"
            params.append(f"%{query}%")
        else:
            sql += " AND CAST(ID_DOC AS VARCHAR(50)) LIKE %s"
            params.append(f"%{query}%")
    if filtro == "cliente":
        sql += " ORDER BY ID_SN, TRY_CAST(ID_DOC AS BIGINT) DESC, ID_DOC DESC"
    elif filtro == "nombre":
        sql += " ORDER BY NOM_SOCIO, TRY_CAST(ID_DOC AS BIGINT) DESC, ID_DOC DESC"
    else:
        sql += " ORDER BY TRY_CAST(ID_DOC AS BIGINT) DESC, ID_DOC DESC"

    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        pedidos = cursor.fetchall()

    results = []
    for p in pedidos:
        pref_id = str(p[0] or "")
        lock = _prefactura_lock_get(pref_id)
        lock_owner_id = str((lock or {}).get("owner_id") or "").strip()
        locked_by_me = bool(lock_owner and lock_owner_id and lock_owner_id == lock_owner)
        locked_by_other = bool(lock and not locked_by_me)
        lock_user = str((lock or {}).get("usuario_nombre") or "").strip()
        lock_terminal = str((lock or {}).get("terminal") or "").strip()
        results.append(
            {
                "id_doc": pref_id,
                "id_sn": p[1] or "",
                "nom_socio": p[2] or "",
                "rnc_ced": p[3] or "",
                "contacto": p[4] or "",
                "ent_factura": p[5] or "",
                "ent_mercancia": p[6] or "",
                "est_doc": p[7] or "",
                "fecha_cont": _fmt_date_iso(p[8]),
                "fecha_doc": _fmt_date_iso(p[9]),
                "fecha_venc": _fmt_date_iso(p[10]),
                "comentario": p[11] or "",
                "total_doc": float(p[12]) if p[12] is not None else 0.0,
                "locked_by_me": locked_by_me,
                "locked_by_other": locked_by_other,
                "lock_user": lock_user,
                "lock_terminal": lock_terminal,
            }
        )
    return JsonResponse({"results": results})


@require_http_methods(["GET"])
def emision_prefacturas_status_view(request):
    auth_payload = _require_any_factura_perm_json(request, "ver_emision", "ver_documentos")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    sql = """
        SELECT
            COUNT(*) AS total,
            MAX(TRY_CAST(ID_DOC AS BIGINT)) AS max_id,
            MAX(COALESCE(FECHA_ACT, FECHA_DOC)) AS max_fecha
        FROM CAB_PEDIDO
        WHERE UPPER(ISNULL(EST_DOC, '')) = 'ABIERTO'
          AND NOT EXISTS (
                SELECT 1
                FROM CAB_FACTURA f
                WHERE (
                        CAST(f.ID_DOC_PV AS VARCHAR(50)) = CAST(CAB_PEDIDO.ID_DOC AS VARCHAR(50))
                     OR (
                            CAST(f.ID_DOC_BASE AS VARCHAR(50)) = CAST(CAB_PEDIDO.ID_DOC AS VARCHAR(50))
                        AND UPPER(ISNULL(f.TIPO_DOC_BASE, '')) IN ('PV', 'PC')
                     )
                     OR (
                            ISNULL(f.REFERENCIA, '') = CAST(CAB_PEDIDO.ID_DOC AS VARCHAR(50))
                        AND UPPER(ISNULL(f.TIPO_DOC_BASE, '')) = 'PC'
                     )
                )
          )
    """
    with connection.cursor() as cursor:
        cursor.execute(sql)
        row = cursor.fetchone() or []
    total = int(row[0] or 0)
    max_id = int(row[1] or 0)
    max_fecha = row[2]
    max_fecha_text = max_fecha.isoformat() if hasattr(max_fecha, "isoformat") else str(max_fecha or "")
    return JsonResponse({"stamp": f"{total}|{max_id}|{max_fecha_text}", "total": total})


@require_http_methods(["POST"])
def emision_prefactura_lock_view(request):
    auth_payload = _require_any_factura_perm_json(request, "ver_emision", "ver_documentos")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"detail": "JSON invalido"}, status=400)

    prefactura_id = str(payload.get("prefactura_id") or "").strip()
    owner_id = str(payload.get("lock_owner") or "").strip()
    action = str(payload.get("action") or "acquire").strip().lower()
    if not prefactura_id:
        return JsonResponse({"detail": "prefactura_id requerido"}, status=400)
    if not owner_id:
        return JsonResponse({"detail": "lock_owner requerido"}, status=400)
    if action not in {"acquire", "release"}:
        return JsonResponse({"detail": "action invalido"}, status=400)

    usuario_id = _to_int((auth_payload or {}).get("usuario_id"), 0)
    usuario_nombre = str((auth_payload or {}).get("usuario_nombre") or "").strip()
    terminal = _resolve_request_terminal(request, payload)

    if action == "release":
        changed = _prefactura_lock_release(prefactura_id, owner_id=owner_id)
        if changed:
            transaction.on_commit(lambda: broadcast_prefacturas_refresh(reason="prefactura-lock-released"))
        return JsonResponse({"ok": True, "released": changed})

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT TOP 1 UPPER(ISNULL(EST_DOC, ''))
            FROM CAB_PEDIDO
            WHERE CAST(ID_DOC AS VARCHAR(50)) = %s
            """,
            [prefactura_id],
        )
        pref = cursor.fetchone()
    if not pref:
        return JsonResponse({"detail": "Prefactura no encontrada."}, status=404)
    if str(pref[0] or "").strip() != "ABIERTO":
        return JsonResponse({"detail": "La prefactura ya no esta abierta."}, status=409)

    acquire_result = _prefactura_lock_acquire(
        prefactura_id,
        owner_id=owner_id,
        usuario_id=usuario_id,
        usuario_nombre=usuario_nombre,
        terminal=terminal[:50],
    )
    if not acquire_result.get("ok"):
        existing_lock = acquire_result.get("lock") or {}
        return JsonResponse(
            {
                "detail": "La prefactura ya esta en uso en otra terminal.",
                "lock_user": str(existing_lock.get("usuario_nombre") or "").strip(),
                "lock_terminal": str(existing_lock.get("terminal") or "").strip(),
            },
            status=409,
        )
    transaction.on_commit(lambda: broadcast_prefacturas_refresh(reason="prefactura-lock-acquired"))
    return JsonResponse({"ok": True, "locked_by_me": True})


@require_http_methods(["GET"])
def emision_prefactura_detalle_view(request):
    auth_payload = _require_any_factura_perm_json(request, "ver_emision", "ver_documentos")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    id_doc = (request.GET.get("id_doc") or "").strip()
    if not id_doc:
        return JsonResponse({"detail": "Parametro id_doc requerido"}, status=400)

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT TOP 1
                ID_DOC, ID_SN, NOM_SOCIO, RNC_CED, CONTACTO, ENT_FACTURA, ENT_MERCANCIA, EST_DOC,
                FECHA_CONT, FECHA_DOC, FECHA_VENC, COMENTARIO, SUBTOTAL, TOTAL_DESC, TOTAL_ITBIS,
                TOTAL_DOC, ID_CONDICION, DIA, CONDICION, ID_PRECIO
            FROM CAB_PEDIDO
            WHERE CAST(ID_DOC AS VARCHAR(50)) = %s
            """,
            [id_doc],
        )
        cab = cursor.fetchone()

    if not cab:
        return JsonResponse({"detail": "Prefactura no encontrada"}, status=404)

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT ID_DETALLE, DESCRIP_ART, ID_ARTICULO, CANT_UND, CANTIDAD, CANT_ENT, MEDIDA,
                   OBSERVACION, ID_ALMACEN, CECO, CEBE, PRECIO, PRECIO_BRUTO, TOTAL_LINEA, PORC_DESC, ID_IMPTO
            FROM DET_PEDIDO
            WHERE CAST(ID_DOC AS VARCHAR(50)) = %s
            ORDER BY No_LINEA, ID_DETALLE
            """,
            [id_doc],
        )
        detalle_rows = cursor.fetchall()

    results = []
    for d in detalle_rows:
        results.append(
            {
                "id_detalle": d[0],
                "descrip_art": d[1] or "",
                "id_articulo": d[2] or "",
                "cant_emp": _num(d[3]),
                "cantidad": _num(d[4]),
                "entregado": _num(d[5]),
                "uom": d[6] or "",
                "observacion": d[7] or "",
                "alm": d[8],
                "cebe": d[9] or "",
                "proyecto": d[10] or "",
                "precio_unit": _num(d[11]),
                "precio_bruto": _num(d[12]),
                "valor": _num(d[13]),
                "porc_desc": _num(d[14]),
                "id_itbis": d[15],
            }
        )

    data = {
        "prefactura": {
            "id_doc": str(cab[0] or ""),
            "id_sn": cab[1] or "",
            "nom_socio": cab[2] or "",
            "rnc_ced": cab[3] or "",
            "contacto": cab[4] or "",
            "ent_factura": cab[5] or "",
            "ent_mercancia": cab[6] or "",
            "est_doc": cab[7] or "",
            "fecha_cont": _fmt_date_iso(cab[8]),
            "fecha_doc": _fmt_date_iso(cab[9]),
            "fecha_venc": _fmt_date_iso(cab[10]),
            "comentario": cab[11] or "",
            "subtotal": _num(cab[12]),
            "total_desc": _num(cab[13]),
            "impuesto": _num(cab[14]),
            "total_doc": _num(cab[15]),
            "id_condicion": cab[16],
            "dia": cab[17],
            "condicion": cab[18] or "",
            "id_precio": cab[19],
        },
        "detalles": results,
    }
    return JsonResponse(data)


@require_http_methods(["GET"])
def facturacion_clientes_buscar_view(request):
    auth_payload = _require_any_factura_perm_json(request, "ver_documentos", "ver_emision")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    query = (request.GET.get("q") or "").strip()
    filtro = (request.GET.get("filtro") or "nombre").strip().lower()
    clientes = MaestroSn.objects.exclude(bloqueado__iexact="Y")

    if query:
        if filtro == "codigo":
            clientes = clientes.filter(id_sn__icontains=query)
        elif filtro == "apodo":
            clientes = clientes.filter(contacto__icontains=query)
        elif filtro == "rnc":
            clientes = clientes.filter(rnc_ced__icontains=query)
        elif filtro == "telefono":
            clientes = clientes.filter(Q(tel1__icontains=query) | Q(tel2__icontains=query))
        else:
            clientes = clientes.filter(nom_socio__icontains=query)

    if filtro == "codigo":
        clientes = clientes.order_by("id_sn")
    elif filtro == "apodo":
        clientes = clientes.order_by("contacto", "id_sn")
    elif filtro == "rnc":
        clientes = clientes.order_by("rnc_ced", "id_sn")
    elif filtro == "telefono":
        clientes = clientes.order_by("tel1", "tel2", "id_sn")
    else:
        clientes = clientes.order_by("nom_socio", "id_sn")

    results = list(
        clientes.values(
            "id_sn",
            "nom_socio",
            "rnc_ced",
            "contacto",
            "dir_factura",
            "tel1",
            "bloqueado",
        )[:50]
    )
    return JsonResponse({"results": results})


@require_http_methods(["GET"])
def facturacion_cliente_detalle_view(request):
    auth_payload = _require_any_factura_perm_json(request, "ver_documentos", "ver_emision")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    id_sn = (request.GET.get("id_sn") or "").strip()
    if not id_sn:
        return JsonResponse({"detail": "Parametro id_sn requerido"}, status=400)

    cliente = (
        MaestroSn.objects.filter(id_sn=id_sn)
        .values(
            "id_sn",
            "nom_socio",
            "contacto",
            "rnc_ced",
            "dir_factura",
            "dir_mercancia",
            "tel1",
            "tel2",
            "id_sector",
            "comentario",
            "descripcion",
            "id_vendedor",
            "nom_vend",
            "bloqueado",
            "id_condicion",
            "condicion",
            "dia",
            "tarifa_int",
            "lim_credito",
            "id_precio",
        )
        .first()
    )
    if not cliente:
        return JsonResponse({"detail": "Cliente no encontrado"}, status=404)
    return JsonResponse({"cliente": cliente})


@require_http_methods(["GET"])
def facturacion_articulos_buscar_view(request):
    auth_payload = _require_any_factura_perm_json(request, "ver_documentos", "ver_emision")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    query = (request.GET.get("q") or "").strip()
    filtro = (request.GET.get("filtro") or "descripcion").strip().lower()
    qs = MaestroArticulo.objects.exclude(bloqueado__iexact="Y")

    if query:
        if filtro == "codigo":
            qs = qs.filter(Q(referencia__icontains=query) | Q(id_articulo__icontains=query))
        else:
            qs = qs.filter(descrip_art__icontains=query)

    if filtro == "codigo":
        qs = qs.order_by("referencia", "id_articulo")
    else:
        qs = qs.order_by("id_articulo")

    values = list(qs.values(
        "id_articulo",
        "descrip_art",
        "referencia",
        "precio_det",
        "id_impto_vt",
        "um_inv",
        "bloqueado",
    )[:80])

    results = []
    if values:
        articulo_ids = [row.get("id_articulo") for row in values if row.get("id_articulo")]
        tarj_stock = {}
        if articulo_ids:
            with connection.cursor() as cursor:
                placeholders = ", ".join(["%s"] * len(articulo_ids))
                cursor.execute(
                    f"SELECT ID_ARTICULO, COALESCE(SUM(CANTIDAD), 0) FROM TARJETERO WHERE ID_ARTICULO IN ({placeholders}) GROUP BY ID_ARTICULO",
                    articulo_ids
                )
                tarj_stock = {str(row[0] or "").strip(): float(row[1] or 0) for row in cursor.fetchall()}

        for row in values:
            art_id = row.get("id_articulo") or ""
            stock_val = tarj_stock.get(art_id, 0.0)
            results.append(
                {
                    "id_articulo": art_id,
                    "descrip_art": row.get("descrip_art") or "",
                    "referencia": row.get("referencia") or "",
                    "precio_det": _num(row.get("precio_det")),
                    "stock": stock_val,
                    "id_impto_vt": row.get("id_impto_vt"),
                    "um_inv": row.get("um_inv") or "",
                    "bloqueado": row.get("bloqueado") or "N",
                }
            )
    return JsonResponse({"results": results})


@require_http_methods(["GET"])
def facturacion_unidad_medida_buscar_view(request):
    auth_payload = _require_any_factura_perm_json(request, "ver_documentos", "ver_emision")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT TOP 200 * FROM Unidad_medida ORDER BY 1")
            rows = cursor.fetchall()
            columns = [c[0].lower() for c in (cursor.description or [])]
    except Exception:
        return JsonResponse({"results": []})

    preferred = [
        "medida",
        "descripcion",
        "descrip",
        "uom",
        "unidad",
        "nombre",
        "id",
        "codigo",
    ]
    idx = None
    for name in preferred:
        if name in columns:
            idx = columns.index(name)
            break
    if idx is None:
        idx = 0 if columns else None

    results = []
    seen = set()
    if idx is not None:
        for row in rows:
            value = str(row[idx] or "").strip()
            if not value:
                continue
            key = value.lower()
            if key in seen:
                continue
            seen.add(key)
            results.append(value)

    return JsonResponse({"results": results})


@require_http_methods(["GET"])
def facturacion_proyectos_buscar_view(request):
    auth_payload = _require_any_factura_perm_json(request, "ver_documentos", "ver_emision")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    query = (request.GET.get("q") or "").strip()
    sql = """
        SELECT TOP 200 CODIGO, DESCRIPCION
        FROM PROYECTO
        WHERE CODIGO IS NOT NULL AND LTRIM(RTRIM(CODIGO)) <> ''
    """
    params = []
    if query:
        sql += " AND (CODIGO LIKE %s OR DESCRIPCION LIKE %s)"
        like = f"%{query}%"
        params.extend([like, like])
    sql += " ORDER BY CODIGO"

    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()

    results = [
        {
            "codigo": str(row[0]).strip(),
            "descripcion": str(row[1] or "").strip(),
        }
        for row in rows
        if row and row[0] is not None and str(row[0]).strip()
    ]
    return JsonResponse({"results": results})


@require_http_methods(["GET"])
def facturacion_cebes_buscar_view(request):
    auth_payload = _require_any_factura_perm_json(request, "ver_documentos", "ver_emision")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    query = (request.GET.get("q") or "").strip()
    sql = """
        SELECT TOP 200 CECO, DESCRIPCION, ID_CODIGO
        FROM DEPARTAMENTO
        WHERE CECO IS NOT NULL AND LTRIM(RTRIM(CECO)) <> ''
    """
    params = []
    if query:
        sql += " AND (CECO LIKE %s OR DESCRIPCION LIKE %s)"
        like = f"%{query}%"
        params.extend([like, like])
    sql += " ORDER BY ID_CODIGO"

    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()

    results = [
        {
            "codigo": str(row[0]).strip(),
            "descripcion": str(row[1] or "").strip(),
        }
        for row in rows
        if row and row[0] is not None and str(row[0]).strip()
    ]
    return JsonResponse({"results": results})


@require_http_methods(["GET"])
def facturas_buscar_view(request):
    auth_payload = _require_any_factura_perm_json(request, "ver_emision", "ver_documentos")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    query = (request.GET.get("q") or "").strip()
    filtro = (request.GET.get("filtro") or "documento").strip().lower()
    modo = (request.GET.get("modo") or "todos").strip().lower()
    sql = """
        SELECT TOP 80
            ID_DOC,
            COALESCE(
                CASE
                    WHEN UPPER(ISNULL(TIPO_DOC_BASE, '')) = 'PC'
                         AND NULLIF(LTRIM(RTRIM(ISNULL(REFERENCIA, ''))), '') IS NOT NULL
                    THEN NULLIF(LTRIM(RTRIM(ISNULL(REFERENCIA, ''))), '')
                    ELSE NULL
                END,
                CASE
                    WHEN UPPER(ISNULL(TIPO_DOC_BASE, '')) IN ('PV', 'PC')
                         AND TRY_CAST(ID_DOC_BASE AS BIGINT) IS NOT NULL
                         AND TRY_CAST(ID_DOC_BASE AS BIGINT) >= 0
                    THEN CAST(TRY_CAST(ID_DOC_BASE AS BIGINT) AS VARCHAR(50))
                    ELSE NULL
                END,
                CAST(ID_DOC_PV AS VARCHAR(50)),
                ''
            ),
            ID_SN, NOM_SOCIO, RNC_CED, FECHA_DOC, TOTAL_DOC, ISNULL(NCF, ''), ISNULL(TIPO, ''),
            UPPER(ISNULL(CANCELADO, 'N')), ISNULL(NCF_NC, ''), ISNULL(ID_NCF, 0)
        FROM CAB_FACTURA
        WHERE (TRY_CAST(ID_NCF AS BIGINT) IS NULL OR TRY_CAST(ID_NCF AS BIGINT) <> 34)
    """
    params = []
    if modo == "manual":
        sql += " AND (TRY_CAST(ID_NCF AS BIGINT) IS NULL OR TRY_CAST(ID_NCF AS BIGINT) IN (0, 2))"
    elif modo == "electronica":
        placeholders = ", ".join(["%s"] * len(ECF_ID_NCF_CODES))
        sql += f" AND TRY_CAST(ID_NCF AS BIGINT) IN ({placeholders})"
        params.extend(sorted(ECF_ID_NCF_CODES))
    if query:
        if filtro == "cliente":
            sql += " AND ID_SN LIKE %s"
            params.append(f"%{query}%")
        elif filtro == "nombre":
            sql += " AND NOM_SOCIO LIKE %s"
            params.append(f"%{query}%")
        elif filtro == "encf":
            sql += " AND ISNULL(NCF, '') LIKE %s"
            params.append(f"%{query}%")
        else:
            sql += " AND CAST(ID_DOC AS VARCHAR(50)) LIKE %s"
            params.append(f"%{query}%")
    if filtro == "cliente":
        sql += " ORDER BY ID_SN, TRY_CAST(ID_DOC AS BIGINT) DESC, ID_DOC DESC"
    elif filtro == "nombre":
        sql += " ORDER BY NOM_SOCIO, TRY_CAST(ID_DOC AS BIGINT) DESC, ID_DOC DESC"
    else:
        sql += " ORDER BY TRY_CAST(ID_DOC AS BIGINT) DESC, ID_DOC DESC"

    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()

    results = []
    for row in rows:
        factura_id = str(row[0] or "")
        results.append(
            {
                "id_doc": factura_id,
                "id_doc_pv": str(row[1] or ""),
                "id_sn": str(row[2] or ""),
                "nom_socio": str(row[3] or ""),
                "rnc_ced": str(row[4] or ""),
                "fecha_doc": _fmt_date_iso(row[5]),
                "total_doc": float(row[6]) if row[6] is not None else 0.0,
                "encf": str(row[7] or ""),
                "tipo": str(row[8] or ""),
                "cancelado": str(row[9] or "") == "Y",
                "ncf_nc": str(row[10] or ""),
                "cancelable": not (str(row[9] or "") == "Y" or str(row[10] or "").strip()),
                "es_electronica": _to_int(row[11], 0) in ECF_ID_NCF_CODES or str(row[7] or "").strip().upper().startswith("E"),
                "print_url": f"/app/factura/impresion/?id_doc={factura_id}",
            }
        )
    return JsonResponse({"results": results})


@require_http_methods(["GET"])
def facturacion_factura_detalle_view(request):
    auth_payload = _require_any_factura_perm_json(request, "ver_emision", "ver_documentos")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    id_doc = (request.GET.get("id_doc") or "").strip()
    if not id_doc:
        return JsonResponse({"detail": "Parametro id_doc requerido"}, status=400)

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT TOP 1
                ID_DOC,
                COALESCE(
                    CASE
                        WHEN UPPER(ISNULL(TIPO_DOC_BASE, '')) = 'PC'
                             AND NULLIF(LTRIM(RTRIM(ISNULL(REFERENCIA, ''))), '') IS NOT NULL
                        THEN NULLIF(LTRIM(RTRIM(ISNULL(REFERENCIA, ''))), '')
                        ELSE NULL
                    END,
                    CASE
                        WHEN UPPER(ISNULL(TIPO_DOC_BASE, '')) IN ('PV', 'PC')
                             AND TRY_CAST(ID_DOC_BASE AS BIGINT) IS NOT NULL
                             AND TRY_CAST(ID_DOC_BASE AS BIGINT) >= 0
                        THEN CAST(TRY_CAST(ID_DOC_BASE AS BIGINT) AS VARCHAR(50))
                        ELSE NULL
                    END,
                    CAST(ID_DOC_PV AS VARCHAR(50)),
                    ''
                ) AS PREF_ORIGEN,
                UPPER(ISNULL(EST_DOC, '')),
                UPPER(ISNULL(CANCELADO, 'N')),
                ID_SN, NOM_SOCIO, RNC_CED, CONTACTO,
                FECHA_CONT, FECHA_DOC, FECHA_VENC,
                ENT_FACTURA, ENT_MERCANCIA, COMENTARIO,
                SUBTOTAL, TOTAL_DESC, TOTAL_ITBIS, TOTAL_DOC, ABONO, SALDO,
                ID_CONDICION, DIA, CONDICION, ID_PRECIO, ISNULL(NCF_NC, '')
            FROM CAB_FACTURA
            WHERE CAST(ID_DOC AS VARCHAR(50)) = %s
            """,
            [id_doc],
        )
        cab = cursor.fetchone()

        if not cab:
            return JsonResponse({"detail": "Factura no encontrada."}, status=404)

        cursor.execute(
            """
            SELECT
                ID_DETALLE, DESCRIP_ART, ID_ARTICULO, CANT_UND, CANTIDAD, CANT_ENT, MEDIDA,
                OBSERVACION, ID_ALMACEN, CECO, CEBE, PRECIO, PRECIO_BRUTO, TOTAL_LINEA, PORC_DESC, ID_IMPTO
            FROM DET_FACTURA
            WHERE CAST(ID_DOC AS VARCHAR(50)) = %s
            ORDER BY No_LINEA, ID_DETALLE
            """,
            [id_doc],
        )
        detalle_rows = cursor.fetchall()

    estatus_doc = "Cancelado" if str(cab[3] or "").strip().upper() == "Y" else (str(cab[2] or "").strip().title() or "Facturada")
    active_payment_total = _load_factura_active_payment_total(cab[0])
    editable = _factura_manual_editable(
        total_doc=cab[17],
        saldo=cab[19],
        abono=cab[18],
        est_doc=cab[2],
        cancelado=cab[3],
        ncf_nc=cab[24],
        active_payment_total=active_payment_total,
    )
    factura = {
        "id_doc": str(cab[0] or ""),
        "id_doc_pv": str(cab[1] or ""),
        "est_doc": estatus_doc,
        "id_sn": str(cab[4] or ""),
        "nom_socio": str(cab[5] or ""),
        "rnc_ced": str(cab[6] or ""),
        "contacto": str(cab[7] or ""),
        "fecha_cont": _fmt_date_iso(cab[8]),
        "fecha_doc": _fmt_date_iso(cab[9]),
        "fecha_venc": _fmt_date_iso(cab[10]),
        "ent_factura": str(cab[11] or ""),
        "ent_mercancia": str(cab[12] or ""),
        "comentario": str(cab[13] or ""),
        "subtotal": _num(cab[14]),
        "total_desc": _num(cab[15]),
        "impuesto": _num(cab[16]),
        "total_doc": _num(cab[17]),
        "pagado": _num(cab[18]),
        "balance": _num(cab[19]),
        "id_condicion": cab[20],
        "dia": cab[21],
        "condicion": str(cab[22] or ""),
        "id_precio": cab[23],
        "editable": editable,
        "print_url": f"/app/factura/impresion/?id_doc={str(cab[0] or '').strip()}",
    }
    detalles = []
    for d in detalle_rows:
        detalles.append(
            {
                "id_detalle": d[0],
                "descrip_art": d[1] or "",
                "id_articulo": d[2] or "",
                "cant_emp": _num(d[3]),
                "cantidad": _num(d[4]),
                "entregado": _num(d[5]),
                "uom": d[6] or "",
                "observacion": d[7] or "",
                "alm": d[8],
                "cebe": d[9] or "",
                "proyecto": d[10] or "",
                "precio_unit": _num(d[11]),
                "precio_bruto": _num(d[12]),
                "valor": _num(d[13]),
                "porc_desc": _num(d[14]),
                "id_itbis": d[15],
            }
        )

    return JsonResponse({"factura": factura, "detalles": detalles})


@require_http_methods(["POST"])
def facturacion_cancelar_factura_view(request):
    auth_payload = _require_any_factura_perm_json(request, "ver_documentos", "ver_emision")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"detail": "JSON invalido"}, status=400)

    factura_id = _to_int(payload.get("factura_id"), 0)
    if factura_id <= 0:
        return JsonResponse({"detail": "factura_id requerido"}, status=400)
    client_event_id = str(payload.get("event_id") or "").strip()

    usuario_id = _to_int((auth_payload or {}).get("usuario_id"), 0)
    usuario_nombre = _clip_str((auth_payload or {}).get("usuario_nombre"), 100)
    terminal = _resolve_request_terminal(request, payload)

    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT TOP 1
                    ID_DOC,
                    COALESCE(
                        CASE
                            WHEN UPPER(ISNULL(TIPO_DOC_BASE, '')) = 'PC'
                                 AND NULLIF(LTRIM(RTRIM(ISNULL(REFERENCIA, ''))), '') IS NOT NULL
                            THEN NULLIF(LTRIM(RTRIM(ISNULL(REFERENCIA, ''))), '')
                            ELSE NULL
                        END,
                        CASE
                            WHEN UPPER(ISNULL(TIPO_DOC_BASE, '')) IN ('PV', 'PC')
                                 AND TRY_CAST(ID_DOC_BASE AS BIGINT) IS NOT NULL
                                 AND TRY_CAST(ID_DOC_BASE AS BIGINT) >= 0
                            THEN CAST(TRY_CAST(ID_DOC_BASE AS BIGINT) AS VARCHAR(50))
                            ELSE NULL
                        END,
                        CAST(ID_DOC_PV AS VARCHAR(50)),
                        ''
                    ) AS PREF_ORIGEN,
                    UPPER(ISNULL(CANCELADO, 'N')),
                    UPPER(ISNULL(EST_DOC, '')),
                    COALESCE(CAST(NO_ED AS VARCHAR(50)), ''),
                    ISNULL(ID_NCF, 0),
                    ISNULL(NCF_NC, ''),
                    ISNULL(TOTAL_DOC, 0),
                    ISNULL(SALDO, 0),
                    ISNULL(ABONO, 0)
                FROM CAB_FACTURA
                WHERE TRY_CAST(ID_DOC AS BIGINT) = %s
                """,
                [factura_id],
            )
            factura = cursor.fetchone()
            if not factura:
                return JsonResponse({"detail": "Factura no encontrada."}, status=404)

            cancelado = str(factura[2] or "").strip().upper()
            estado = str(factura[3] or "").strip().upper()
            no_ed = _stringify_doc(factura[4])
            id_ncf = _to_int(factura[5], 0)
            ncf_nc = str(factura[6] or "").strip()
            prefactura_origen = _stringify_doc(factura[1])

            if cancelado == "Y" or estado == "CANCELADO":
                return JsonResponse({"detail": "La factura ya se encuentra cancelada."}, status=400)
            if ncf_nc:
                return JsonResponse(
                    {"detail": "La factura ya tiene una cancelacion asociada y no se puede cancelar otra vez."},
                    status=400,
                )
            if id_ncf not in {0, 2}:
                return JsonResponse(
                    {"detail": "Las facturas electronicas deben cancelarse desde Emision Factura Electronica."},
                    status=400,
                )

            active_payment_total = _load_factura_active_payment_total(factura[0])
            if active_payment_total > Decimal("0.01"):
                return JsonResponse(
                    {"detail": "La factura tiene pagos activos y no se puede cancelar."},
                    status=400,
                )

            try:
                cancel_no_ed = _create_factura_cancel_ed_entries(
                    cursor,
                    factura_id=factura_id,
                    no_ed=no_ed,
                    usuario_id=usuario_id,
                    usuario_nombre=usuario_nombre,
                    terminal=terminal,
                )
            except ValueError as exc:
                return JsonResponse({"detail": str(exc)}, status=400)

            _restore_factura_stock(cursor, factura_id)

            _update_existing_columns(
                cursor,
                "CAB_FACTURA",
                "ID_DOC",
                factura_id,
                [
                    (("CANCELADO",), "Y"),
                    (("EST_DOC",), "Cancelado"),
                    (("FECHA_ACT",), timezone.localtime()),
                ],
            )
            _update_existing_columns_where(
                cursor,
                "DET_FACTURA",
                "TRY_CAST([ID_DOC] AS BIGINT) = %s",
                [factura_id],
                [
                    (("ESTATUS_LINEA",), "C"),
                    (("FECHA_ACT",), timezone.localtime()),
                ],
            )

            if prefactura_origen:
                cursor.execute(
                    """
                    UPDATE CAB_PEDIDO
                    SET EST_DOC = 'Abierto',
                        FECHA_ACT = CONVERT(VARCHAR(30), GETDATE(), 121)
                    WHERE CAST(ID_DOC AS VARCHAR(50)) = %s
                    """,
                    [prefactura_origen],
                )
        if prefactura_origen:
            transaction.on_commit(
                lambda: broadcast_prefacturas_refresh(
                    reason="prefactura-reopened",
                    event_id=client_event_id,
                )
            )
            transaction.on_commit(
                lambda: broadcast_prefactura_document_status(
                    document_id=prefactura_origen,
                    estado="Abierto",
                    reason="prefactura-reopened",
                    event_id=client_event_id,
                )
            )
        transaction.on_commit(
            lambda: broadcast_factura_document_status(
                document_id=factura_id,
                estado="Cancelado",
                reason="factura-cancelled",
                event_id=client_event_id,
            )
        )

    return JsonResponse(
        {
            "ok": True,
            "factura_id": factura_id,
            "estado": "Cancelado",
            "no_ed": cancel_no_ed,
            "prefactura_reabierta": bool(prefactura_origen),
            "detail": f"La factura {factura_id} fue cancelada correctamente.",
        }
    )


@require_http_methods(["POST"])
def cancelar_factura_view(request):
    auth_payload = _require_perm_json(request, "factura", "crear")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"detail": "JSON invalido"}, status=400)

    factura_id = _to_int(payload.get("factura_id"), 0)
    client_event_id = str(payload.get("event_id") or "").strip()
    motivo = str(payload.get("motivo") or "").strip() or "Cancelacion mediante nota de credito"
    if factura_id <= 0:
        return JsonResponse({"detail": "factura_id requerido"}, status=400)

    usuario_id = _to_int((auth_payload or {}).get("usuario_id"), 0)
    terminal = _resolve_request_terminal(request, payload)
    today = timezone.localdate()

    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT TOP 1
                    ID_DOC, ID_DOC_PV, ID_SN, NOM_SOCIO, RNC_CED, CONTACTO, FECHA_CONT, FECHA_DOC, FECHA_VENC,
                    ENT_FACTURA, ENT_MERCANCIA, SUBTOTAL, TOTAL_DESC, TOTAL_ITBIS, TOTAL_DOC, MON_DOC,
                    COMENTARIO, ID_CONDICION, DIA, CONDICION, ID_VENDEDOR, CTA_ASOCIADA, ID_GASTO, ID_PRECIO,
                    UPPER(ISNULL(CANCELADO, 'N')), ISNULL(NCF, ''), ISNULL(NCF_NC, '')
                FROM CAB_FACTURA
                WHERE TRY_CAST(ID_DOC AS BIGINT) = %s
                """,
                [factura_id],
            )
            factura = cursor.fetchone()
            if not factura:
                return JsonResponse({"detail": "Factura no encontrada."}, status=404)
            if str(factura[24] or "").strip().upper() == "Y":
                return JsonResponse({"detail": "La factura ya se encuentra cancelada."}, status=400)
            existing_ncf = str(factura[26] or "").strip()
            if existing_ncf:
                existing_nc = _find_existing_credit_note(cursor, factura_id, existing_ncf)
                return JsonResponse(
                    {
                        "ok": True,
                        "already_cancelled": True,
                        "detail": "La factura ya tiene una nota de credito asociada.",
                        "nota_credito_id": (existing_nc or {}).get("id_doc", ""),
                        "encf": (existing_nc or {}).get("encf", existing_ncf),
                        "print_url": (
                            f"/app/factura/impresion/?id_doc={(existing_nc or {}).get('id_doc', '')}"
                            if (existing_nc or {}).get("id_doc")
                            else ""
                        ),
                    }
                )

            existing_nc = _find_existing_credit_note(cursor, factura_id)
            if existing_nc:
                return JsonResponse(
                    {
                        "ok": True,
                        "already_cancelled": True,
                        "detail": "La factura ya fue cancelada con una nota de credito.",
                        "nota_credito_id": existing_nc.get("id_doc", ""),
                        "encf": existing_nc.get("encf", ""),
                        "print_url": (
                            f"/app/factura/impresion/?id_doc={existing_nc.get('id_doc', '')}"
                            if existing_nc.get("id_doc")
                            else ""
                        ),
                    },
                )

            cursor.execute(
                """
                SELECT ISNULL(MAX(TRY_CAST(ID_DOC AS BIGINT)), 0) + 1
                FROM CAB_FACTURA WITH (UPDLOCK, HOLDLOCK)
                """
            )
            row = cursor.fetchone()
            nota_credito_id = int((row[0] or 0)) or 1

            try:
                config, encf_nc = _ensure_ecf_sequence("34")
            except ValueError as exc:
                return _ecf_sequence_error_response("34", exc)

            periodo_cont = str(today.month)
            ejercicio = int(today.year)
            comentario_original = str(factura[16] or "").strip()
            comentario_nc = f"NC por cancelacion de factura {factura_id}. {motivo}".strip()
            if comentario_original:
                comentario_nc = f"{comentario_nc} | Base: {comentario_original}"

            cursor.execute(
                """
                INSERT INTO CAB_FACTURA
                (ID_DOC, ID_DOC_PV, ID_DOC_BASE, TIPO_DOC_BASE, CANCELADO, IMPRESO, EST_DOC, TIPO_DOC,
                 CONTACTO, FECHA_CONT, FECHA_DOC, FECHA_VENC, ID_SN, NOM_SOCIO, RNC_CED, ENT_FACTURA,
                 ENT_MERCANCIA, SUBTOTAL, TOTAL_DESC, TOTAL_ITBIS, TOTAL_DOC, MON_DOC, ABONO, SALDO,
                 COMENTARIO, ID_CONDICION, DIA, CONDICION, ID_VENDEDOR, FECHA_CREACION, ID_NCF, NCF,
                 NCF_NC, TIPO, PERIODO_CONT, ID_USUARIO, TOTAL_BASE, CTA_ASOCIADA, EJERCICIO, ID_GASTO, TERMINAL,
                 ID_PRECIO, FINANCIADO, PRELIMINAR)
                VALUES
                (%s, %s, %s, %s, 'N', 'N', 'Abierto', 'NC',
                 %s, %s, %s, %s, %s, %s, %s, %s,
                 %s, %s, %s, %s, %s, %s, 0, %s,
                 %s, %s, %s, %s, %s, GETDATE(), 34, %s,
                 %s, %s, %s, %s, %s, %s, %s, %s, %s,
                 %s, 'N', 'N')
                """,
                [
                    nota_credito_id,
                    factura[1],
                    factura_id,
                    "FC",
                    factura[5],
                    factura[6],
                    factura[7],
                    factura[8],
                    factura[2],
                    factura[3],
                    factura[4],
                    factura[9],
                    factura[10],
                    -Decimal(factura[11] or 0),
                    -Decimal(factura[12] or 0),
                    -Decimal(factura[13] or 0),
                    -Decimal(factura[14] or 0),
                    str(factura[15] or "RD$"),
                    -Decimal(factura[14] or 0),
                    comentario_nc[:500],
                    factura[17],
                    factura[18],
                    factura[19],
                    factura[20],
                    encf_nc or None,
                    str(factura[25] or "").strip() or None,
                    _tipo_descripcion("34"),
                    periodo_cont,
                    usuario_id,
                    -Decimal(factura[11] or 0),
                    factura[21],
                    ejercicio,
                    factura[22],
                    terminal[:50],
                    factura[23],
                ],
            )

            cursor.execute(
                """
                INSERT INTO DET_FACTURA
                (ID_DOC, ID_DOC_PV, No_LINEA, CLASE_DOC_BASE, REF_DOC_BASE, ESTATUS_LINEA, CLASE_ART, ID_ARTICULO,
                 DESCRIP_ART, CANTIDAD, CANT_ENT, CANT_PEND, CANT_DESP, MEDIDA, COSTO, PRECIO, PRECIO_BRUTO, PORC_DESC,
                 ID_IMPTO, TOTAL_DESC, TOTAL_COSTO, TOTAL_PRECIO, TOTAL_PRECIO_NETO, TOTAL_LINEA, ID_ALMACEN, ID_VENDEDOR,
                 PORC_COM, CTA_INGRESO, CTA_GASTOS, CTA_COSTOS, CTA_INV, CTA_IMPTO, CTA_DEV_VENTA, PRECIO_TRAS_DESC,
                 FECHA_CONT, CEBE, CECO, PERIODO_CONT, EJERCICIO, REFERENCIA, OBSERVACION, CANT_UND, No_LINEA_BASE)
                SELECT
                    %s, ID_DOC_PV, No_LINEA, 'FC', %s, 'C', CLASE_ART, ID_ARTICULO,
                    DESCRIP_ART, -CANTIDAD, -CANT_ENT, 0, -ISNULL(CANT_DESP, CANTIDAD), MEDIDA, COSTO, PRECIO, PRECIO_BRUTO, PORC_DESC,
                    ID_IMPTO, -TOTAL_DESC, -TOTAL_COSTO, -TOTAL_PRECIO, -TOTAL_PRECIO_NETO, -TOTAL_LINEA, ID_ALMACEN, ID_VENDEDOR,
                    PORC_COM, CTA_INGRESO, CTA_GASTOS, CTA_COSTOS, CTA_INV, CTA_IMPTO, CTA_DEV_VENTA, PRECIO_TRAS_DESC,
                    FECHA_CONT, CEBE, CECO, %s, %s, REFERENCIA, OBSERVACION, -ISNULL(CANT_UND, CANTIDAD), No_LINEA
                FROM DET_FACTURA
                WHERE TRY_CAST(ID_DOC AS BIGINT) = %s
                """,
                [nota_credito_id, factura_id, periodo_cont, ejercicio, factura_id],
            )

            cursor.execute(
                """
                UPDATE CAB_FACTURA
                SET CANCELADO = 'Y',
                    EST_DOC = 'Cancelado',
                    NCF_NC = %s,
                    FECHA_ACT = CONVERT(VARCHAR(30), GETDATE(), 121)
                WHERE TRY_CAST(ID_DOC AS BIGINT) = %s
                """,
                [encf_nc or None, factura_id],
            )

        original_doc = FacturacionElectronicaDocumento.objects.filter(id_doc=factura_id).first()
        nc_estado = "REGISTRADO" if encf_nc else ("PENDIENTE_XML" if config and config.habilitado else "FACTURA_GENERADA")
        nc_doc, created = FacturacionElectronicaDocumento.objects.update_or_create(
            id_doc=nota_credito_id,
            defaults={
                "tipo_ecf": "34",
                "encf": encf_nc or None,
                "estado": nc_estado,
                "cliente_rnc": str(factura[4] or "").strip() or None,
                "cliente_nombre": str(factura[3] or "").strip() or None,
                "fecha_doc": factura[7],
                "monto_total": -Decimal(factura[14] or 0),
                "observaciones": f"Nota de credito por cancelacion de factura {factura_id}. {motivo}",
            },
        )
        FacturacionElectronicaEvento.objects.create(
            documento=nc_doc,
            tipo_evento="NOTA_CREDITO_EMITIDA",
            detalle=f"Nota de credito {nota_credito_id} generada para cancelar la factura {factura_id}.",
        )
        if encf_nc and created:
            FacturacionElectronicaEvento.objects.create(
                documento=nc_doc,
                tipo_evento="ENCF_ASIGNADO",
                detalle=f"e-NCF asignado: {encf_nc}",
            )
        if original_doc:
            original_doc.estado = "CANCELADO_NC"
            original_doc.observaciones = f"Cancelada con nota de credito {nota_credito_id}. {motivo}"
            original_doc.save(update_fields=["estado", "observaciones", "actualizado_en"])
            FacturacionElectronicaEvento.objects.create(
                documento=original_doc,
                tipo_evento="FACTURA_CANCELADA",
                detalle=f"Cancelada con nota de credito {nota_credito_id}.",
            )
        dispatch_result = _dispatch_document_if_needed(nc_doc, request, config)

    transaction.on_commit(
        lambda: broadcast_factura_document_status(
            document_id=factura_id,
            estado="Cancelado",
            reason="factura-cancelled",
            event_id=client_event_id,
        )
    )
    return JsonResponse(
        {
            "ok": True,
            "factura_id": factura_id,
            "nota_credito_id": nota_credito_id,
            "encf": encf_nc,
            "dispatch_message": dispatch_result.message,
            "print_url": f"/app/factura/impresion/?id_doc={nota_credito_id}",
        }
    )


def _emitir_factura_desde_prefactura(
    *,
    request,
    auth_payload,
    prefactura_id,
    tipo_ecf="",
    lock_owner="",
    event_id="",
    terminal_cliente="",
):
    prefactura_id = str(prefactura_id or "").strip()
    tipo_ecf = str(tipo_ecf or "").strip()
    lock_owner = str(lock_owner or "").strip()
    client_event_id = str(event_id or "").strip()
    es_electronica = bool(tipo_ecf)
    if not prefactura_id:
        return None, JsonResponse({"detail": "prefactura_id requerido"}, status=400)
    if es_electronica and tipo_ecf not in {codigo for codigo, _ in EMISION_TIPOS}:
        return None, JsonResponse({"detail": "tipo_ecf invalido"}, status=400)

    usuario_id = _to_int((auth_payload or {}).get("usuario_id"), 0)
    usuario_nombre = str((auth_payload or {}).get("usuario_nombre") or "").strip()
    terminal = _resolve_request_terminal(request, {"terminal_cliente": terminal_cliente})
    if not lock_owner:
        return None, JsonResponse({"detail": "lock_owner requerido para guardar la prefactura."}, status=400)

    acquire_result = _prefactura_lock_acquire(
        prefactura_id,
        owner_id=lock_owner,
        usuario_id=usuario_id,
        usuario_nombre=usuario_nombre,
        terminal=terminal[:50],
    )
    if not acquire_result.get("ok"):
        existing_lock = acquire_result.get("lock") or {}
        return None, JsonResponse(
            {
                "detail": "La prefactura esta en uso en otra terminal.",
                "lock_user": str(existing_lock.get("usuario_nombre") or "").strip(),
                "lock_terminal": str(existing_lock.get("terminal") or "").strip(),
            },
            status=409,
        )

    today = timezone.localdate()
    encf = ""
    doc_estado = ""
    dispatch_message = ""
    config = None

    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT TOP 1 ID_DOC, ID_SN, NOM_SOCIO, RNC_CED, EST_DOC, TOTAL_DOC, FECHA_DOC
                    FROM CAB_PEDIDO
                    WHERE CAST(ID_DOC AS VARCHAR(50)) = %s
                    """,
                    [prefactura_id],
                )
                pref = cursor.fetchone()
                if not pref:
                    return None, JsonResponse({"detail": "Prefactura no encontrada."}, status=404)
                if str(pref[4] or "").strip().upper() != "ABIERTO":
                    return None, JsonResponse({"detail": "La prefactura ya no esta abierta."}, status=400)
                if es_electronica and tipo_ecf == "31" and not str(pref[3] or "").strip():
                    return None, JsonResponse({"detail": "La factura de credito fiscal requiere RNC/Ced. del cliente."}, status=400)

                cursor.execute(
                    """
                    SELECT TOP 1 ID_DOC, ISNULL(NCF, '')
                    FROM CAB_FACTURA
                    WHERE CAST(ID_DOC_PV AS VARCHAR(50)) = %s
                      AND UPPER(ISNULL(CANCELADO, 'N')) <> 'Y'
                    ORDER BY TRY_CAST(ID_DOC AS BIGINT) DESC, ID_DOC DESC
                    """,
                    [prefactura_id],
                )
                existing = cursor.fetchone()
                if existing:
                    return (
                        None,
                        JsonResponse(
                            {
                                "detail": "Esta prefactura ya fue facturada.",
                                "factura_id": str(existing[0] or ""),
                                "encf": str(existing[1] or ""),
                                "print_url": f"/app/factura/impresion/?id_doc={str(existing[0] or '').strip()}",
                            },
                            status=400,
                        ),
                    )

                cursor.execute(
                    """
                    SELECT ISNULL(MAX(TRY_CAST(ID_DOC AS BIGINT)), 0) + 1
                    FROM CAB_FACTURA WITH (UPDLOCK, HOLDLOCK)
                    """
                )
                row = cursor.fetchone()
                factura_id = int((row[0] or 0))
                if factura_id <= 0:
                    factura_id = 1

                if es_electronica:
                    try:
                        config, encf = _ensure_ecf_sequence(tipo_ecf)
                    except ValueError as exc:
                        return None, _ecf_sequence_error_response(tipo_ecf, exc)
                    id_ncf = int(tipo_ecf)
                    tipo_desc = _tipo_descripcion(tipo_ecf)
                else:
                    id_ncf = None
                    tipo_desc = "Factura"

                periodo_cont = str(today.month)
                ejercicio = int(today.year)

                cursor.execute(
                    """
                    INSERT INTO CAB_FACTURA
                    (ID_DOC, ID_DOC_PV, ID_DOC_BASE, TIPO_DOC_BASE, CANCELADO, IMPRESO, EST_DOC, TIPO_DOC,
                     CONTACTO, FECHA_CONT, FECHA_DOC, FECHA_VENC, ID_SN, NOM_SOCIO, RNC_CED, ENT_FACTURA,
                     ENT_MERCANCIA, SUBTOTAL, TOTAL_DESC, TOTAL_ITBIS, TOTAL_DOC, MON_DOC, ABONO, SALDO,
                     COMENTARIO, ID_CONDICION, DIA, CONDICION, ID_VENDEDOR, FECHA_CREACION, ID_NCF, NCF,
                     TIPO, PERIODO_CONT, ID_USUARIO, TOTAL_BASE, CTA_ASOCIADA, EJERCICIO, ID_GASTO, TERMINAL,
                     ID_PRECIO, FINANCIADO, PRELIMINAR)
                    SELECT
                        %s, TRY_CAST(ID_DOC AS BIGINT), TRY_CAST(ID_DOC AS BIGINT), %s, 'N', 'N', 'Abierto', 'FC',
                        CONTACTO, FECHA_CONT, FECHA_DOC, FECHA_VENC, ID_SN, NOM_SOCIO, RNC_CED, ENT_FACTURA,
                        ENT_MERCANCIA, SUBTOTAL, TOTAL_DESC, TOTAL_ITBIS, TOTAL_DOC, ISNULL(MON_DOC, 'RD$'), 0, TOTAL_DOC,
                        COMENTARIO, ID_CONDICION, DIA, CONDICION, ID_VENDEDOR, GETDATE(), %s, %s,
                        %s, %s, %s, SUBTOTAL, CTA_ASOCIADA, %s, ID_GASTO, %s,
                        ID_PRECIO, 'N', 'N'
                    FROM CAB_PEDIDO
                    WHERE CAST(ID_DOC AS VARCHAR(50)) = %s
                    """,
                    [
                        factura_id,
                        "PV",
                        id_ncf,
                        encf or None,
                        tipo_desc,
                        periodo_cont,
                        usuario_id,
                        ejercicio,
                        terminal[:50],
                        prefactura_id,
                    ],
                )

                cursor.execute(
                    """
                    INSERT INTO DET_FACTURA
                    (ID_DOC, ID_DOC_PV, No_LINEA, CLASE_DOC_BASE, REF_DOC_BASE, ESTATUS_LINEA, CLASE_ART, ID_ARTICULO,
                     DESCRIP_ART, CANTIDAD, CANT_ENT, CANT_PEND, CANT_DESP, MEDIDA, COSTO, PRECIO, PRECIO_BRUTO, PORC_DESC,
                     ID_IMPTO, TOTAL_DESC, TOTAL_COSTO, TOTAL_PRECIO, TOTAL_PRECIO_NETO, TOTAL_LINEA, ID_ALMACEN, ID_VENDEDOR,
                     PORC_COM, CTA_INGRESO, CTA_GASTOS, CTA_COSTOS, CTA_INV, CTA_IMPTO, CTA_DEV_VENTA, PRECIO_TRAS_DESC,
                     FECHA_CONT, CEBE, CECO, PERIODO_CONT, EJERCICIO, REFERENCIA, OBSERVACION, CANT_UND, No_LINEA_BASE)
                    SELECT
                        %s, TRY_CAST(ID_DOC AS BIGINT), No_LINEA, 'PV', TRY_CAST(ID_DOC AS BIGINT), 'C', CLASE_ART, ID_ARTICULO,
                        DESCRIP_ART, CANTIDAD, CANTIDAD, 0, CANTIDAD, MEDIDA, COSTO, PRECIO, PRECIO_BRUTO, PORC_DESC,
                        ID_IMPTO, TOTAL_DESC, TOTAL_COSTO, TOTAL_PRECIO, TOTAL_PRECIO_NETO, TOTAL_LINEA, ID_ALMACEN, ID_VENDEDOR,
                        PORC_COM, CTA_INGRESO, CTA_GASTOS, CTA_COSTOS, CTA_INV, CTA_IMPTO, CTA_DEV_VENTA, PRECIO_TRAS_DESC,
                        FECHA_CONT, CEBE, CECO, %s, %s, REFERENCIA, OBSERVACION, CANT_UND, No_LINEA
                    FROM DET_PEDIDO
                    WHERE CAST(ID_DOC AS VARCHAR(50)) = %s
                    """,
                    [factura_id, periodo_cont, ejercicio, prefactura_id],
                )

                cursor.execute(
                    """
                    UPDATE CAB_PEDIDO
                    SET EST_DOC = 'Cerrado',
                        FECHA_ACT = CONVERT(VARCHAR(30), GETDATE(), 121)
                    WHERE CAST(ID_DOC AS VARCHAR(50)) = %s
                    """,
                    [prefactura_id],
                )

            transaction.on_commit(
                lambda: broadcast_prefacturas_refresh(
                    reason="prefactura-invoiced",
                    event_id=client_event_id,
                )
            )
            transaction.on_commit(
                lambda: broadcast_prefactura_document_status(
                    document_id=prefactura_id,
                    estado="Cerrado",
                    reason="prefactura-invoiced",
                    event_id=client_event_id,
                )
            )

            if es_electronica:
                doc_estado = "REGISTRADO" if encf else ("PENDIENTE_XML" if config and config.habilitado else "FACTURA_GENERADA")
                doc, created = FacturacionElectronicaDocumento.objects.update_or_create(
                    id_doc=factura_id,
                    defaults={
                        "tipo_ecf": tipo_ecf,
                        "encf": encf or None,
                        "estado": doc_estado,
                        "cliente_rnc": str(pref[3] or "").strip() or None,
                        "cliente_nombre": str(pref[2] or "").strip() or None,
                        "fecha_doc": pref[6],
                        "monto_total": Decimal(pref[5] or 0),
                        "observaciones": f"Factura emitida desde prefactura {prefactura_id}",
                    },
                )
                FacturacionElectronicaEvento.objects.create(
                    documento=doc,
                    tipo_evento="FACTURA_EMITIDA",
                    detalle=f"Factura {factura_id} generada desde prefactura {prefactura_id}.",
                )
                if encf and created:
                    FacturacionElectronicaEvento.objects.create(
                        documento=doc,
                        tipo_evento="ENCF_ASIGNADO",
                        detalle=f"e-NCF asignado: {encf}",
                    )
                dispatch_result = _dispatch_document_if_needed(doc, request, config)
                dispatch_message = dispatch_result.message
    finally:
        _prefactura_lock_release(prefactura_id, owner_id=lock_owner)

    result = {
        "ok": True,
        "factura_id": factura_id,
        "prefactura_id": prefactura_id,
        "encf": encf,
        "print_url": f"/app/factura/impresion/?id_doc={factura_id}",
    }
    if es_electronica:
        result["estado_ecf"] = doc_estado
        result["dispatch_message"] = dispatch_message
    return result, None


def _emitir_factura_manual_desde_payload(*, request, auth_payload, payload):
    prefactura_id = str(payload.get("prefactura_id") or "").strip()
    client_event_id = str(payload.get("event_id") or "").strip()
    lock_owner = str(payload.get("lock_owner") or "").strip()
    factura_id_payload = _to_int(payload.get("factura_id"), 0)
    id_sn = _clip_str(payload.get("id_sn"), 12).strip()
    nom_socio = _clip_str(payload.get("nom_socio"), 100).strip()
    detalles = payload.get("detalles") or []
    if not id_sn:
        return None, JsonResponse({"detail": "Cliente requerido."}, status=400)
    if not nom_socio:
        return None, JsonResponse({"detail": "Nombre del cliente requerido."}, status=400)
    if not isinstance(detalles, list):
        return None, JsonResponse({"detail": "detalles invalido"}, status=400)
    detalles = [
        detalle
        for detalle in detalles
        if isinstance(detalle, dict) and str(detalle.get("id_articulo") or "").strip()
    ]
    if not detalles:
        return None, JsonResponse({"detail": "Debes agregar al menos un articulo."}, status=400)

    cliente_bloqueado = (
        MaestroSn.objects.filter(id_sn=id_sn).values_list("bloqueado", flat=True).first()
    )
    if str(cliente_bloqueado or "").strip().upper() in {"Y", "S", "1"}:
        return None, JsonResponse({"detail": "El cliente seleccionado esta bloqueado."}, status=400)

    fecha_cont = _to_date_or_none(payload.get("fecha_cont")) or timezone.localdate()
    fecha_doc = _to_date_or_none(payload.get("fecha_doc")) or fecha_cont
    fecha_venc = _to_date_or_none(payload.get("fecha_venc")) or fecha_doc
    subtotal = _to_decimal(payload.get("subtotal"))
    total_desc = _to_decimal(payload.get("total_desc"))
    impuesto = _to_decimal(payload.get("impuesto"))
    total_doc = _to_decimal(payload.get("total_doc"))
    usuario_id = _to_int_or_none((auth_payload or {}).get("usuario_id")) or 0
    usuario_nombre = str((auth_payload or {}).get("usuario_nombre") or "").strip()
    terminal = _resolve_request_terminal(request, payload)
    if prefactura_id and not lock_owner:
        return None, JsonResponse({"detail": "lock_owner requerido para guardar la prefactura."}, status=400)
    lock_acquired = False
    if prefactura_id:
        acquire_result = _prefactura_lock_acquire(
            prefactura_id,
            owner_id=lock_owner,
            usuario_id=usuario_id,
            usuario_nombre=usuario_nombre,
            terminal=terminal,
        )
        if not acquire_result.get("ok"):
            existing_lock = acquire_result.get("lock") or {}
            return None, JsonResponse(
                {
                    "detail": "La prefactura esta en uso en otra terminal.",
                    "lock_user": str(existing_lock.get("usuario_nombre") or "").strip(),
                    "lock_terminal": str(existing_lock.get("terminal") or "").strip(),
                },
                status=409,
            )
        lock_acquired = True
    periodo_cont = str(fecha_doc.month)
    ejercicio = int(fecha_doc.year)
    manual_id_ncf = 2
    manual_tipo = "FACTURA DE CONSUMO"
    manual_id_gasto = -1
    manual_forma_pago = 1
    manual_id_ingreso = "01"
    manual_cta_cobro = "11010101"
    manual_cta_transferencia = "11010202"
    total_cantidad_ed = Decimal("0")

    detalle_articulos = {
        str((detalle or {}).get("id_articulo") or "").strip()
        for detalle in detalles
        if isinstance(detalle, dict)
    }
    detalle_articulos.discard("")
    requested_qty_by_articulo = {}
    for detalle in detalles:
        articulo_id = str((detalle or {}).get("id_articulo") or "").strip()
        if not articulo_id:
            continue
        cantidad_solicitada = _to_decimal((detalle or {}).get("cantidad"), Decimal("0"))
        if cantidad_solicitada <= 0:
            cantidad_solicitada = Decimal("0")
        requested_qty_by_articulo[articulo_id] = requested_qty_by_articulo.get(articulo_id, Decimal("0")) + cantidad_solicitada

    referencia_map = {}
    blocked_items = []
    stock_items = {}
    if detalle_articulos:
        articulos = list(MaestroArticulo.objects.filter(id_articulo__in=list(detalle_articulos)).values(
            "id_articulo",
            "descrip_art",
            "referencia",
            "bloqueado",
            "um_inv",
            "alm_dft",
            "ceco",
            "cta_aum_stock",
        ))
        articulo_ids = [str(a.get("id_articulo") or "").strip() for a in articulos if a.get("id_articulo")]
        tarj_stock = {}
        if articulo_ids:
            with connection.cursor() as cursor:
                placeholders = ", ".join(["%s"] * len(articulo_ids))
                cursor.execute(
                    f"SELECT ID_ARTICULO, COALESCE(SUM(CANTIDAD), 0) FROM TARJETERO WHERE ID_ARTICULO IN ({placeholders}) GROUP BY ID_ARTICULO",
                    articulo_ids
                )
                tarj_stock = {str(row[0] or "").strip(): Decimal(str(row[1] or 0)) for row in cursor.fetchall()}

        for articulo in articulos:
            articulo_id = str(articulo.get("id_articulo") or "").strip()
            if not articulo_id:
                continue
            referencia_map[articulo_id] = str(articulo.get("referencia") or "")
            stock_val = tarj_stock.get(articulo_id, Decimal("0"))
            stock_items[articulo_id] = {
                "descrip_art": str(articulo.get("descrip_art") or "").strip(),
                "stock": stock_val,
                "uom": str(articulo.get("um_inv") or "").strip(),
                "alm_dft": _stringify_doc(articulo.get("alm_dft")),
                "ceco": str(articulo.get("ceco") or "").strip(),
                "cta_aum_stock": str(articulo.get("cta_aum_stock") or "").strip(),
            }
            if str(articulo.get("bloqueado") or "").strip().upper() in {"Y", "S", "1"}:
                blocked_items.append(
                    {
                        "id_articulo": articulo_id,
                        "descrip_art": str(articulo.get("descrip_art") or "").strip(),
                    }
                )
    if blocked_items:
        detalle = ", ".join(
            [f"{item['id_articulo']} - {item['descrip_art']}".strip(" -") for item in blocked_items]
        )
        return None, JsonResponse(
            {"detail": f"No se puede grabar la factura. Articulos bloqueados: {detalle}"},
            status=400,
        )
    empresa = _get_empresa_data()
    empresa["logo_src"] = _build_inline_image_src(empresa.get("logo_b64"), empresa.get("logo_tipo"))
    if empresa.get("habilitar_fact_stock"):
        stock_errors = []
        stock_request_items = []
        for articulo_id, cantidad_solicitada in requested_qty_by_articulo.items():
            articulo_stock = stock_items.get(articulo_id) or {}
            cantidad_disponible = _to_decimal(articulo_stock.get("stock"), Decimal("0"))
            if cantidad_solicitada > cantidad_disponible:
                descripcion = str(articulo_stock.get("descrip_art") or "").strip()
                etiqueta = f"{articulo_id} - {descripcion}".strip(" -")
                cantidad_faltante = cantidad_solicitada - cantidad_disponible
                stock_errors.append(
                    f"- {etiqueta} | seleccionado: {_format_decimal_display(cantidad_solicitada)} | disponible: {_format_decimal_display(cantidad_disponible)}"
                )
                stock_request_items.append(
                    {
                        "articulo_id": articulo_id,
                        "descripcion": descripcion,
                        "cantidad_solicitada": _format_decimal_display(cantidad_solicitada),
                        "cantidad_disponible": _format_decimal_display(cantidad_disponible),
                        "cantidad_faltante": _format_decimal_display(cantidad_faltante),
                        "uom": str(articulo_stock.get("uom") or "").strip(),
                        "alm_dft": _stringify_doc(articulo_stock.get("alm_dft")),
                        "ceco": str(articulo_stock.get("ceco") or "").strip(),
                        "cta_aum_stock": str(articulo_stock.get("cta_aum_stock") or "").strip(),
                    }
                )
        if stock_errors:
            return None, JsonResponse(
                {
                    "detail": "No hay inventario suficiente para facturar:\n"
                    + "\n".join(stock_errors),
                    "allow_request_existence": True,
                    "stock_request_items": stock_request_items,
                },
                status=400,
            )

    for detalle in detalles:
        cantidad_ed = _to_decimal((detalle or {}).get("cantidad"), Decimal("1"))
        if cantidad_ed <= 0:
            cantidad_ed = Decimal("1")
        total_cantidad_ed += cantidad_ed

    with transaction.atomic():
        with connection.cursor() as cursor:
            existing_factura_prefactura = ""
            existing_no_ed = ""
            pref_base_id = 0
            cab_id_doc_base = -1
            cab_tipo_doc_base = None
            cab_referencia = None
            detalle_doc_pv = 0
            detalle_tipo_doc_base = ""
            detalle_ref_doc_base = -1
            cta_asociada = "11020101"
            if factura_id_payload > 0:
                cursor.execute(
                    """
                    SELECT TOP 1
                        ID_DOC,
                        COALESCE(
                            CASE
                                WHEN UPPER(ISNULL(TIPO_DOC_BASE, '')) = 'PC'
                                     AND NULLIF(LTRIM(RTRIM(ISNULL(REFERENCIA, ''))), '') IS NOT NULL
                                THEN NULLIF(LTRIM(RTRIM(ISNULL(REFERENCIA, ''))), '')
                                ELSE NULL
                            END,
                            CASE
                                WHEN UPPER(ISNULL(TIPO_DOC_BASE, '')) IN ('PV', 'PC')
                                     AND TRY_CAST(ID_DOC_BASE AS BIGINT) IS NOT NULL
                                     AND TRY_CAST(ID_DOC_BASE AS BIGINT) >= 0
                                THEN CAST(TRY_CAST(ID_DOC_BASE AS BIGINT) AS VARCHAR(50))
                                ELSE NULL
                            END,
                            CAST(ID_DOC_PV AS VARCHAR(50)),
                            ''
                        ) AS PREF_ORIGEN,
                        UPPER(ISNULL(CANCELADO, 'N')),
                        UPPER(ISNULL(EST_DOC, '')),
                        ISNULL(ABONO, 0),
                        ISNULL(SALDO, 0),
                        ISNULL(TOTAL_DOC, 0),
                        ISNULL(NCF_NC, ''),
                        COALESCE(CAST(NO_ED AS VARCHAR(50)), ''),
                        ISNULL(CTA_ASOCIADA, ''),
                        ISNULL(ID_NCF, 0)
                    FROM CAB_FACTURA WITH (UPDLOCK, HOLDLOCK)
                    WHERE TRY_CAST(ID_DOC AS BIGINT) = %s
                    """,
                    [factura_id_payload],
                )
                existing_factura = cursor.fetchone()
                if not existing_factura:
                    return None, JsonResponse({"detail": "La factura a modificar no existe."}, status=404)
                if _to_int(existing_factura[10], 0) not in {0, 2}:
                    return None, JsonResponse({"detail": "Solo se pueden modificar facturas del modulo Facturacion."}, status=400)
                active_payment_total = _load_factura_active_payment_total(existing_factura[0])
                if not _factura_manual_editable(
                    total_doc=existing_factura[6],
                    saldo=existing_factura[5],
                    abono=existing_factura[4],
                    est_doc=existing_factura[3],
                    cancelado=existing_factura[2],
                    ncf_nc=existing_factura[7],
                    active_payment_total=active_payment_total,
                ):
                    return None, JsonResponse(
                        {"detail": "La factura solo se puede editar si no tiene pagos activos y conserva su estado inicial."},
                        status=400,
                    )
                existing_factura_prefactura = str(existing_factura[1] or "").strip()
                existing_no_ed = _stringify_doc(existing_factura[8])
                cta_asociada = _clip_str(existing_factura[9] or "11020101", 20) or "11020101"
            if prefactura_id:
                cursor.execute(
                    """
                    SELECT TOP 1 ID_DOC, UPPER(ISNULL(EST_DOC, '')), ISNULL(CTA_ASOCIADA, ''), ISNULL(ID_GASTO, 0)
                    FROM CAB_PEDIDO
                    WHERE CAST(ID_DOC AS VARCHAR(50)) = %s
                    """,
                    [prefactura_id],
                )
                pref_row = cursor.fetchone()
                if not pref_row:
                    return None, JsonResponse({"detail": "Prefactura no encontrada."}, status=404)
                same_existing_pref = factura_id_payload > 0 and prefactura_id == existing_factura_prefactura
                if str(pref_row[1] or "").strip().upper() != "ABIERTO" and not same_existing_pref:
                    return None, JsonResponse({"detail": "La prefactura ya no esta abierta."}, status=400)

                duplicate_sql = """
                    SELECT TOP 1 ID_DOC, ISNULL(NCF, '')
                    FROM CAB_FACTURA
                    WHERE (
                            CAST(ID_DOC_PV AS VARCHAR(50)) = %s
                         OR (
                                CAST(ID_DOC_BASE AS VARCHAR(50)) = %s
                            AND UPPER(ISNULL(TIPO_DOC_BASE, '')) IN ('PV', 'PC')
                         )
                         OR (
                                ISNULL(REFERENCIA, '') = %s
                            AND UPPER(ISNULL(TIPO_DOC_BASE, '')) = 'PC'
                         )
                    )
                      AND UPPER(ISNULL(CANCELADO, 'N')) <> 'Y'
                """
                duplicate_params = [prefactura_id, prefactura_id, prefactura_id]
                if factura_id_payload > 0:
                    duplicate_sql += " AND TRY_CAST(ID_DOC AS BIGINT) <> %s"
                    duplicate_params.append(factura_id_payload)
                duplicate_sql += " ORDER BY TRY_CAST(ID_DOC AS BIGINT) DESC, ID_DOC DESC"
                cursor.execute(duplicate_sql, duplicate_params)
                existing = cursor.fetchone()
                if existing:
                    return (
                        None,
                        JsonResponse(
                            {
                                "detail": "Esta prefactura ya fue facturada.",
                                "factura_id": str(existing[0] or ""),
                                "encf": str(existing[1] or ""),
                                "print_url": f"/app/factura/impresion/?id_doc={str(existing[0] or '').strip()}",
                            },
                            status=400,
                        ),
                    )

                pref_base_id = _to_int(pref_row[0], 0)
                cab_id_doc_base = pref_base_id if pref_base_id > 0 else -1
                cab_tipo_doc_base = "PC"
                cab_referencia = _clip_str(prefactura_id, 30) or None
                detalle_doc_pv = 0
                detalle_tipo_doc_base = "PC"
                detalle_ref_doc_base = cab_id_doc_base
                cta_asociada = _clip_str(pref_row[2] or "11020101", 20) or "11020101"

            if factura_id_payload > 0:
                factura_id = factura_id_payload
                cursor.execute(
                    """
                    UPDATE CAB_FACTURA
                    SET ID_DOC_PV = %s,
                        ID_DOC_BASE = %s,
                        TIPO_DOC_BASE = %s,
                        CANCELADO = 'N',
                        IMPRESO = 'N',
                        EST_DOC = 'Abierto',
                        TIPO_DOC = 'FC',
                        CONTACTO = %s,
                        FECHA_CONT = %s,
                        FECHA_DOC = %s,
                        FECHA_VENC = %s,
                        ID_SN = %s,
                        NOM_SOCIO = %s,
                        RNC_CED = %s,
                        ENT_FACTURA = %s,
                        ENT_MERCANCIA = %s,
                        SUBTOTAL = %s,
                        TOTAL_DESC = %s,
                        TOTAL_ITBIS = %s,
                        TOTAL_DOC = %s,
                        MON_DOC = %s,
                        ABONO = 0,
                        SALDO = %s,
                        COMENTARIO = %s,
                        ID_CONDICION = %s,
                        DIA = %s,
                        CONDICION = %s,
                        ID_VENDEDOR = %s,
                        ID_NCF = %s,
                        NCF = NULL,
                        TIPO = %s,
                        PERIODO_CONT = %s,
                        ID_USUARIO = %s,
                        TOTAL_BASE = %s,
                        CTA_ASOCIADA = %s,
                        EJERCICIO = %s,
                        ID_GASTO = %s,
                        TERMINAL = %s,
                        ID_PRECIO = %s,
                        FINANCIADO = 'N',
                        PRELIMINAR = 'N'
                    WHERE TRY_CAST(ID_DOC AS BIGINT) = %s
                    """,
                    [
                        None,
                        cab_id_doc_base,
                        cab_tipo_doc_base,
                        _clip_str(payload.get("contacto"), 50),
                        fecha_cont,
                        fecha_doc,
                        fecha_venc,
                        id_sn,
                        nom_socio,
                        _clip_str(payload.get("rnc_ced"), 13),
                        _clip_str(payload.get("ent_factura"), 200),
                        _clip_str(payload.get("ent_mercancia"), 200),
                        subtotal,
                        total_desc,
                        impuesto,
                        total_doc,
                        _clip_str("RD$", 50),
                        total_doc,
                        _clip_str(payload.get("comentario"), 500),
                        _to_int_or_none(payload.get("id_condicion")),
                        _to_int_or_none(payload.get("dia")),
                        _clip_str(payload.get("condicion"), 15),
                        usuario_id,
                        manual_id_ncf,
                        manual_tipo,
                        periodo_cont,
                        usuario_id,
                        subtotal,
                        _clip_str(cta_asociada, 20),
                        ejercicio,
                        manual_id_gasto,
                        terminal,
                        _to_int_or_none(payload.get("id_precio")),
                        factura_id,
                    ],
                )
            else:
                cursor.execute(
                    """
                    SELECT ISNULL(MAX(TRY_CAST(ID_DOC AS BIGINT)), 0) + 1
                    FROM CAB_FACTURA WITH (UPDLOCK, HOLDLOCK)
                    """
                )
                row = cursor.fetchone()
                factura_id = int((row[0] or 0))
                if factura_id <= 0:
                    factura_id = 1

                cursor.execute(
                    """
                    INSERT INTO CAB_FACTURA
                    (ID_DOC, ID_DOC_PV, ID_DOC_BASE, TIPO_DOC_BASE, CANCELADO, IMPRESO, EST_DOC, TIPO_DOC,
                     CONTACTO, FECHA_CONT, FECHA_DOC, FECHA_VENC, ID_SN, NOM_SOCIO, RNC_CED, ENT_FACTURA,
                     ENT_MERCANCIA, SUBTOTAL, TOTAL_DESC, TOTAL_ITBIS, TOTAL_DOC, MON_DOC, ABONO, SALDO,
                     COMENTARIO, ID_CONDICION, DIA, CONDICION, ID_VENDEDOR, FECHA_CREACION, ID_NCF, NCF,
                     TIPO, PERIODO_CONT, ID_USUARIO, TOTAL_BASE, CTA_ASOCIADA, EJERCICIO, ID_GASTO, TERMINAL,
                     ID_PRECIO, FINANCIADO, PRELIMINAR)
                    VALUES
                    (%s, %s, %s, %s, 'N', 'N', 'Abierto', 'FC',
                     %s, %s, %s, %s, %s, %s, %s, %s,
                     %s, %s, %s, %s, %s, %s, 0, %s,
                     %s, %s, %s, %s, %s, GETDATE(), %s, NULL,
                     %s, %s, %s, %s, %s, %s, %s, %s,
                     %s, 'N', 'N')
                    """,
                    [
                        factura_id,
                        None,
                        cab_id_doc_base,
                        cab_tipo_doc_base,
                        _clip_str(payload.get("contacto"), 50),
                        fecha_cont,
                        fecha_doc,
                        fecha_venc,
                        id_sn,
                        nom_socio,
                        _clip_str(payload.get("rnc_ced"), 13),
                        _clip_str(payload.get("ent_factura"), 200),
                        _clip_str(payload.get("ent_mercancia"), 200),
                        subtotal,
                        total_desc,
                        impuesto,
                        total_doc,
                        _clip_str("RD$", 50),
                        total_doc,
                        _clip_str(payload.get("comentario"), 500),
                        _to_int_or_none(payload.get("id_condicion")),
                        _to_int_or_none(payload.get("dia")),
                        _clip_str(payload.get("condicion"), 15),
                        usuario_id,
                        manual_id_ncf,
                        manual_tipo,
                        periodo_cont,
                        usuario_id,
                        subtotal,
                        _clip_str(cta_asociada, 20),
                        ejercicio,
                        manual_id_gasto,
                        terminal,
                        _to_int_or_none(payload.get("id_precio")),
                    ],
                )

            _update_existing_columns(
                cursor,
                "CAB_FACTURA",
                "ID_DOC",
                factura_id,
                [
                    (("REFERENCIA",), cab_referencia),
                    (("FECHA_RECIBO",), fecha_doc),
                    (("FECHA_ACT",), ""),
                    (("FECHA_ENT",), ""),
                    (("FECHA_DEV",), ""),
                    (("FORMAPAGO", "FORMA_PAGO"), manual_forma_pago),
                    (("IDINGRESO", "ID_INGRESO"), manual_id_ingreso),
                    (("CTA_EFECTIVO",), manual_cta_cobro),
                    (("CTA_TARJETA",), manual_cta_cobro),
                    (("CTA_CHEQUE",), manual_cta_cobro),
                    (("CTA_TRANSFERENCIA", "CTA_TRANSF"), manual_cta_transferencia),
                    (("CTA_ABONO",), ""),
                    (("NCF_NC",), None),
                ],
            )

            try:
                if factura_id_payload > 0 and existing_no_ed:
                    no_ed = _sync_factura_ed_entries(
                        cursor,
                        factura_id=factura_id,
                        prefactura_id=prefactura_id or None,
                        id_sn=id_sn,
                        nombre_cliente=nom_socio,
                        rnc_ced=_clip_str(payload.get("rnc_ced"), 13),
                        fecha_cont=fecha_cont,
                        fecha_doc=fecha_doc,
                        fecha_venc=fecha_venc,
                        total_factura=total_doc,
                        comentario=_clip_str(payload.get("comentario"), 500),
                        periodo_cont=periodo_cont,
                        ejercicio=ejercicio,
                        usuario_id=usuario_id,
                        usuario_nombre=_clip_str((auth_payload or {}).get("usuario_nombre"), 100),
                        terminal=terminal,
                        cta_asociada=_clip_str(cta_asociada, 20),
                        total_cantidad=total_cantidad_ed,
                        existing_ed_no=existing_no_ed,
                    )
                else:
                    no_ed = _create_factura_ed_entries(
                        cursor,
                        factura_id=factura_id,
                        prefactura_id=prefactura_id or None,
                        id_sn=id_sn,
                        nombre_cliente=nom_socio,
                        rnc_ced=_clip_str(payload.get("rnc_ced"), 13),
                        fecha_cont=fecha_cont,
                        fecha_doc=fecha_doc,
                        fecha_venc=fecha_venc,
                        total_factura=total_doc,
                        comentario=_clip_str(payload.get("comentario"), 500),
                        periodo_cont=periodo_cont,
                        ejercicio=ejercicio,
                        usuario_id=usuario_id,
                        usuario_nombre=_clip_str((auth_payload or {}).get("usuario_nombre"), 100),
                        terminal=terminal,
                        cta_asociada=_clip_str(cta_asociada, 20),
                        total_cantidad=total_cantidad_ed,
                    )
            except ValueError as exc:
                return None, JsonResponse({"detail": str(exc)}, status=400)

            _update_existing_columns(
                cursor,
                "CAB_FACTURA",
                "ID_DOC",
                factura_id,
                [
                    (("NO_ED",), no_ed),
                ],
            )

            mov_doc_columns = _load_table_columns(cursor, "MOV_DOC")
            mov_doc_identity_columns = (
                _load_identity_columns(cursor, "MOV_DOC") if mov_doc_columns else set()
            )

            if factura_id_payload > 0:
                cursor.execute(
                    "DELETE FROM DET_FACTURA WHERE TRY_CAST(ID_DOC AS BIGINT) = %s",
                    [factura_id],
                )

            tarjetero_columns = _load_table_columns(cursor, "TARJETERO")
            tarjetero_identity_columns = (
                _load_identity_columns(cursor, "TARJETERO") if tarjetero_columns else set()
            )
            id_vendedor_selected = _to_int_or_none(payload.get("id_vendedor")) or -1

            for index, detalle in enumerate(detalles, start=1):
                id_articulo = _clip_str(detalle.get("id_articulo"), 20).strip()
                if not id_articulo:
                    continue
                descrip_art = _clip_str(detalle.get("descrip_art"), 500)
                medida = _clip_str(detalle.get("uom"), 50)
                cantidad = _to_decimal(detalle.get("cantidad"), Decimal("1"))
                if cantidad <= 0:
                    cantidad = Decimal("1")
                cant_und = _to_decimal(detalle.get("cant_emp"), cantidad)
                if cant_und <= 0:
                    cant_und = cantidad
                precio = _to_decimal(detalle.get("precio_unit"))
                if precio < 0:
                    precio = Decimal("0")
                precio_bruto = _to_decimal(detalle.get("precio_bruto"), precio)
                if precio_bruto < 0:
                    precio_bruto = precio
                porc_desc = _to_decimal(detalle.get("porc_desc"))
                if porc_desc < 0:
                    porc_desc = Decimal("0")
                if porc_desc > 100:
                    porc_desc = Decimal("100")
                total_precio = cantidad * precio
                total_desc_monto = total_precio * (porc_desc / Decimal("100"))
                total_precio_neto = total_precio - total_desc_monto
                total_linea = total_precio_neto
                precio_tras_desc = precio - (precio * (porc_desc / Decimal("100")))
                id_almacen = _to_int_or_none(detalle.get("alm")) or 1
                referencia = _clip_str(referencia_map.get(id_articulo, ""), 15)
                observacion = _clip_str(detalle.get("observacion"), 500)

                cursor.execute(
                    """
                    INSERT INTO DET_FACTURA
                    (ID_DOC, ID_DOC_PV, No_LINEA, CLASE_DOC_BASE, REF_DOC_BASE, ESTATUS_LINEA, CLASE_ART, ID_ARTICULO,
                     DESCRIP_ART, CANTIDAD, CANT_ENT, CANT_PEND, CANT_DESP, MEDIDA, COSTO, PRECIO, PRECIO_BRUTO, PORC_DESC,
                     ID_IMPTO, TOTAL_DESC, TOTAL_COSTO, TOTAL_PRECIO, TOTAL_PRECIO_NETO, TOTAL_LINEA, ID_ALMACEN, ID_VENDEDOR,
                     PORC_COM, CTA_INGRESO, CTA_GASTOS, CTA_COSTOS, CTA_INV, CTA_IMPTO, CTA_DEV_VENTA, PRECIO_TRAS_DESC,
                     FECHA_CONT, CEBE, CECO, PERIODO_CONT, EJERCICIO, REFERENCIA, OBSERVACION, CANT_UND, No_LINEA_BASE)
                    VALUES
                    (%s, %s, %s, %s, %s, 'A', %s, %s,
                     %s, %s, 0, %s, 0, %s, %s, %s, %s, %s,
                     %s, %s, %s, %s, %s, %s, %s, %s,
                     %s, %s, %s, %s, %s, %s, %s, %s,
                     %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        factura_id,
                        detalle_doc_pv,
                        index,
                        detalle_tipo_doc_base,
                        detalle_ref_doc_base,
                        _clip_str("Articulo", 10),
                        id_articulo,
                        descrip_art,
                        cantidad,
                        cantidad,
                        medida,
                        Decimal("1"),
                        precio,
                        precio_bruto,
                        porc_desc,
                        _to_int_or_none(detalle.get("id_itbis")),
                        total_desc_monto,
                        Decimal("1"),
                        total_precio,
                        total_precio_neto,
                        total_linea,
                        id_almacen,
                        usuario_id,
                        Decimal("1"),
                        _clip_str("41010101", 20),
                        _clip_str("11030102", 20),
                        _clip_str("51010101", 20),
                        _clip_str("11030101", 20),
                        _clip_str("21020301", 20),
                        _clip_str("41020201", 20),
                        precio_tras_desc,
                        fecha_cont,
                        _clip_str(detalle.get("proyecto"), 50),
                        _clip_str(detalle.get("cebe"), 12),
                        periodo_cont,
                        ejercicio,
                        referencia,
                        observacion,
                        cant_und,
                        index,
                    ],
                )

                if tarjetero_columns:
                    cantidad_neg = -cantidad
                    total_costo_t = cantidad_neg
                    costo_t = Decimal("1")
                    total_precio_t = total_costo_t * precio
                    tarj_values = {}
                    _assign_existing_values(tarj_values, tarjetero_columns, "FC", "TIPO_DOC", "TD", "CLASE_DOC", "TIPO")
                    _assign_existing_values(tarj_values, tarjetero_columns, factura_id, "ID_DOC", "NO_DOC", "NO", "DOCUMENTO")
                    _assign_existing_values(tarj_values, tarjetero_columns, cab_tipo_doc_base, "TIPO_DOC_BASE")
                    _assign_existing_values(tarj_values, tarjetero_columns, cab_id_doc_base, "ID_DOC_BASE")
                    _assign_existing_values(tarj_values, tarjetero_columns, cab_referencia, "REFERENCIA1")
                    _assign_existing_values(tarj_values, tarjetero_columns, id_sn, "ID_SN", "ID_CLIENTE")
                    _assign_existing_values(tarj_values, tarjetero_columns, nom_socio, "NOM_SN", "NOM_SOCIO", "NOMBRE_SN")
                    _assign_existing_values(tarj_values, tarjetero_columns, _clip_str("11020101", 20), "CTA_ASOCIADA")
                    _assign_existing_values(tarj_values, tarjetero_columns, id_articulo, "ID_ARTICULO", "ARTICULO", "COD_ART")
                    _assign_existing_values(tarj_values, tarjetero_columns, descrip_art, "DESCRIP_ART", "DESCRIPCION", "DESCRIP")
                    _assign_existing_values(tarj_values, tarjetero_columns, cantidad_neg, "CANTIDAD", "CANT")
                    _assign_existing_values(tarj_values, tarjetero_columns, total_costo_t, "TOTAL_COSTO")
                    _assign_existing_values(tarj_values, tarjetero_columns, costo_t, "COSTO", "COSTO_UNIT", "COSTO_UNITARIO")
                    _assign_existing_values(tarj_values, tarjetero_columns, precio, "PRECIO", "PRECIO_UNIT", "PRECIO_UNITARIO")
                    _assign_existing_values(tarj_values, tarjetero_columns, total_precio_t, "TOTAL_PRECIO", "TOTAL_NETO")
                    _assign_existing_values(tarj_values, tarjetero_columns, "No", "LOTE")
                    _assign_existing_values(tarj_values, tarjetero_columns, fecha_cont, "FECHA_CONT", "F_CONT")
                    _assign_existing_values(tarj_values, tarjetero_columns, fecha_venc, "FECHA_VENC", "F_VENC")
                    _assign_existing_values(tarj_values, tarjetero_columns, fecha_doc, "FECHA_DOC", "F_DOC")
                    _assign_existing_values(tarj_values, tarjetero_columns, "RD$", "MONEDA", "MON_DOC")
                    _assign_existing_values(tarj_values, tarjetero_columns, timezone.localdate(), "FECHA_CREACION")
                    _assign_existing_values(tarj_values, tarjetero_columns, int(fecha_cont.month), "PERIODO_CONT")
                    _assign_existing_values(tarj_values, tarjetero_columns, int(fecha_cont.year), "EJERCICIO")
                    _assign_existing_values(tarj_values, tarjetero_columns, 1, "ID_ALMACEN", "ALM", "ALMACEN")
                    _assign_existing_values(tarj_values, tarjetero_columns, id_vendedor_selected, "ID_VENDEDOR", "VENDEDOR")
                    _assign_existing_values(tarj_values, tarjetero_columns, usuario_id, "ID_USUARIO", "USUARIO_ID")
                    _assign_existing_values(tarj_values, tarjetero_columns, _clip_str("41010101", 20), "CTA_INGRESO")
                    _assign_existing_values(tarj_values, tarjetero_columns, _clip_str("21020301", 20), "CTA_IMPTO_VT", "CTA_IMPTO")
                    _insert_dynamic_row(
                        cursor,
                        "TARJETERO",
                        tarjetero_columns,
                        tarj_values,
                        skip_columns=tarjetero_identity_columns,
                    )

                _update_existing_columns_where(
                    cursor,
                    "DET_FACTURA",
                    "[ID_DOC] = %s AND [No_LINEA] = %s",
                    [factura_id, index],
                    [
                        (("LOTE",), "No"),
                        (("ANO",), 0),
                        (("MARCA",), ""),
                        (("MODELO",), ""),
                        (("COLOR",), ""),
                        (("CHASIS",), ""),
                        (("MAQUINA",), ""),
                    ],
                )

            if prefactura_id:
                cursor.execute(
                    """
                    UPDATE CAB_PEDIDO
                    SET EST_DOC = 'Cerrado',
                        FECHA_ACT = CONVERT(VARCHAR(30), GETDATE(), 121)
                    WHERE CAST(ID_DOC AS VARCHAR(50)) = %s
                    """,
                    [prefactura_id],
                )

            if mov_doc_columns:
                mov_values = {}
                _assign_existing_values(mov_values, mov_doc_columns, "FC", "TIPO_DOC", "TD", "CLASE_DOC", "TIPO")
                _assign_existing_values(mov_values, mov_doc_columns, factura_id, "ID_DOC", "NO_DOC", "NO", "DOCUMENTO")
                _assign_existing_values(mov_values, mov_doc_columns, id_sn, "ID_SN", "ID_CLIENTE")
                _assign_existing_values(mov_values, mov_doc_columns, nom_socio, "NOM_SN", "NOM_SOCIO", "NOMBRE_SN")
                _assign_existing_values(mov_values, mov_doc_columns, _clip_str(payload.get("rnc_ced"), 13), "RNC_CED", "RNC")
                _assign_existing_values(mov_values, mov_doc_columns, fecha_cont, "FECHA_CONT", "F_CONT")
                _assign_existing_values(mov_values, mov_doc_columns, fecha_venc, "FECHA_VENC", "F_VENC")
                _assign_existing_values(mov_values, mov_doc_columns, fecha_doc, "FECHA_DOC", "F_DOC")
                _assign_existing_values(mov_values, mov_doc_columns, cab_tipo_doc_base, "CLASE_DOC_BASE", "TIPO_DOC_BASE")
                _assign_existing_values(mov_values, mov_doc_columns, cab_referencia, "REF_DOC_BASE", "DOC_BASE", "ID_DOC_BASE", "REFERENCIA")
                _assign_existing_values(mov_values, mov_doc_columns, cab_referencia, "REFERENCIA", "REFERENCIA1")
                _assign_existing_values(mov_values, mov_doc_columns, subtotal, "TOTAL_BASE", "BASE")
                _assign_existing_values(mov_values, mov_doc_columns, "Abierto", "EST_DOC", "ESTATUS", "ESTADO")
                _assign_existing_values(mov_values, mov_doc_columns, total_doc, "TOTAL_DOC", "TOTAL", "MONTO", "IMPORTE", "VALOR")
                _assign_existing_values(mov_values, mov_doc_columns, subtotal, "SUBTOTAL")
                _assign_existing_values(mov_values, mov_doc_columns, total_desc, "TOTAL_DESC", "DESCUENTO")
                _assign_existing_values(mov_values, mov_doc_columns, _clip_str("RD$", 50), "MON_DOC", "MONEDA")
                _assign_existing_values(mov_values, mov_doc_columns, total_doc, "SALDO")
                _assign_existing_values(mov_values, mov_doc_columns, _clip_str("11020101", 20), "CTA_ASOCIADA")
                _assign_existing_values(mov_values, mov_doc_columns, "-1", "NO_RECIBO", "ID_RECIBO")
                _assign_existing_values(mov_values, mov_doc_columns, datetime(1900, 1, 1), "FECHA_REC", "FECHA_RECIBO")
                _assign_existing_values(mov_values, mov_doc_columns, no_ed, "NO_ED", "ID_ED")
                _assign_existing_values(mov_values, mov_doc_columns, int(fecha_cont.month), "PERIODO_CONT")
                _assign_existing_values(mov_values, mov_doc_columns, int(fecha_cont.year), "EJERCICIO")
                _assign_existing_values(mov_values, mov_doc_columns, usuario_id, "ID_USUARIO", "USUARIO_ID")
                _assign_existing_values(mov_values, mov_doc_columns, id_vendedor_selected, "ID_VENDEDOR", "VENDEDOR")
                _assign_existing_values(mov_values, mov_doc_columns, _to_int_or_none(payload.get("id_condicion")), "ID_CONDICION", "CONDICION_ID")
                _assign_existing_values(mov_values, mov_doc_columns, _clip_str(payload.get("condicion"), 15), "CONDICION")
                _assign_existing_values(mov_values, mov_doc_columns, _to_int_or_none(payload.get("dia")), "DIA")
                _assign_existing_values(mov_values, mov_doc_columns, "N", "CANCELADO", "ANULADO")
                _assign_existing_values(mov_values, mov_doc_columns, terminal, "TERMINAL")
                _assign_existing_values(mov_values, mov_doc_columns, _clip_str(payload.get("comentario"), 500) or "Factura", "COMENTARIO", "OBSERVACION", "NOTA")
                _insert_dynamic_row(
                    cursor,
                    "MOV_DOC",
                    mov_doc_columns,
                    mov_values,
                    skip_columns=mov_doc_identity_columns,
                )
        if prefactura_id:
            transaction.on_commit(
                lambda: broadcast_prefacturas_refresh(
                    reason="prefactura-invoiced",
                    event_id=client_event_id,
                )
            )
            transaction.on_commit(
                lambda: broadcast_prefactura_document_status(
                    document_id=prefactura_id,
                    estado="Cerrado",
                    reason="prefactura-invoiced",
                    event_id=client_event_id,
                )
            )
            if lock_owner:
                transaction.on_commit(lambda: _prefactura_lock_release(prefactura_id, owner_id=lock_owner))

        return (
            {
                "ok": True,
                "factura_id": factura_id,
                "updated": factura_id_payload > 0,
                "prefactura_id": prefactura_id,
                "print_url": f"/app/factura/impresion/?id_doc={factura_id}",
            },
            None,
        )


@require_http_methods(["POST"])
def emitir_factura_manual_view(request):
    auth_payload = _require_perm_json(request, "factura", "crear")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"detail": "JSON invalido"}, status=400)

    if payload.get("detalles") or payload.get("id_sn"):
        result, error = _emitir_factura_manual_desde_payload(
            request=request,
            auth_payload=auth_payload,
            payload=payload,
        )
    else:
        result, error = _emitir_factura_desde_prefactura(
            request=request,
            auth_payload=auth_payload,
            prefactura_id=payload.get("prefactura_id"),
            tipo_ecf="",
            lock_owner=payload.get("lock_owner"),
            event_id=payload.get("event_id"),
            terminal_cliente=payload.get("terminal_cliente"),
        )
    if error:
        return error
    return JsonResponse(result)


@require_http_methods(["POST"])
def emitir_factura_view(request):
    auth_payload = _require_perm_json(request, "factura", "crear")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"detail": "JSON invalido"}, status=400)

    result, error = _emitir_factura_desde_prefactura(
        request=request,
        auth_payload=auth_payload,
        prefactura_id=payload.get("prefactura_id"),
        tipo_ecf=payload.get("tipo_ecf") or "32",
        lock_owner=payload.get("lock_owner"),
        event_id=payload.get("event_id"),
        terminal_cliente=payload.get("terminal_cliente"),
    )
    if error:
        return error
    return JsonResponse(result)


@require_http_methods(["GET"])
def factura_print_view(request):
    auth_payload = _require_any_factura_perm_json(request, "ver_emision", "ver_documentos")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    id_doc = (request.GET.get("id_doc") or "").strip()
    if not id_doc:
        return JsonResponse({"detail": "Parametro id_doc requerido"}, status=400)
    try:
        copies = int(request.GET.get("copies") or "1")
    except Exception:
        copies = 1
    copies = max(1, min(copies, 20))
    autoprint = str(request.GET.get("autoprint") or "").strip().lower() in {"1", "true", "yes", "si"}
    mobile_print = str(request.GET.get("mobile_print") or "").strip().lower() in {"1", "true", "yes", "si"}

    empresa = _get_empresa_data()
    empresa["logo_src"] = _build_inline_image_src(empresa.get("logo_b64"), empresa.get("logo_tipo"))
    firma_bytes = get_user_signature_bytes((auth_payload or {}).get("usuario_id"))
    firma_src = _build_inline_image_src(base64.b64encode(firma_bytes).decode("ascii"), "image/png") if firma_bytes else ""
    factura = None
    detalles = []

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT TOP 1
                ID_DOC,
                COALESCE(
                    CASE
                        WHEN UPPER(ISNULL(TIPO_DOC_BASE, '')) = 'PC'
                             AND NULLIF(LTRIM(RTRIM(ISNULL(REFERENCIA, ''))), '') IS NOT NULL
                        THEN NULLIF(LTRIM(RTRIM(ISNULL(REFERENCIA, ''))), '')
                        ELSE NULL
                    END,
                    CASE
                        WHEN UPPER(ISNULL(TIPO_DOC_BASE, '')) IN ('PV', 'PC')
                             AND TRY_CAST(ID_DOC_BASE AS BIGINT) IS NOT NULL
                             AND TRY_CAST(ID_DOC_BASE AS BIGINT) >= 0
                        THEN CAST(TRY_CAST(ID_DOC_BASE AS BIGINT) AS VARCHAR(50))
                        ELSE NULL
                    END,
                    CAST(ID_DOC_PV AS VARCHAR(50)),
                    ''
                ),
                ID_SN, NOM_SOCIO, RNC_CED, CONTACTO, FECHA_DOC, FECHA_VENC,
                ENT_FACTURA, ENT_MERCANCIA, COMENTARIO, SUBTOTAL, TOTAL_DESC, TOTAL_ITBIS,
                TOTAL_DOC, NCF, ID_NCF, TIPO, CONDICION, DIA, UPPER(ISNULL(CANCELADO, 'N')), ISNULL(NCF_NC, '')
            FROM CAB_FACTURA
            WHERE CAST(ID_DOC AS VARCHAR(50)) = %s
            """,
            [id_doc],
        )
        row = cursor.fetchone()

        if row:
            factura = {
                "id_doc": str(row[0] or ""),
                "id_doc_pv": str(row[1] or ""),
                "id_sn": str(row[2] or ""),
                "nom_socio": str(row[3] or ""),
                "rnc_ced": str(row[4] or ""),
                "contacto": str(row[5] or ""),
                "fecha_doc": row[6],
                "fecha_venc": row[7],
                "ent_factura": str(row[8] or ""),
                "ent_mercancia": str(row[9] or ""),
                "comentario": str(row[10] or ""),
                "subtotal": Decimal(row[11] or 0),
                "total_desc": Decimal(row[12] or 0),
                "total_itbis": Decimal(row[13] or 0),
                "total_doc": Decimal(row[14] or 0),
                "encf": str(row[15] or ""),
                "id_ncf": _to_int(row[16], 0),
                "tipo": str(row[17] or ""),
                "condicion": str(row[18] or ""),
                "dia": _to_int(row[19], 0),
                "cancelado": str(row[20] or "").strip().upper() == "Y",
                "ncf_nc": str(row[21] or ""),
            }

            cursor.execute(
                """
                SELECT No_LINEA, ID_ARTICULO, DESCRIP_ART, CANTIDAD, MEDIDA, PRECIO,
                       PORC_DESC, TOTAL_DESC, TOTAL_ITBIS, TOTAL_LINEA
                FROM DET_FACTURA
                WHERE CAST(ID_DOC AS VARCHAR(50)) = %s
                ORDER BY No_LINEA, ID_DETALLE
                """,
                [id_doc],
            )
            for d in cursor.fetchall():
                detalles.append(
                    {
                        "linea": _to_int(d[0], 0),
                        "id_articulo": str(d[1] or ""),
                        "descrip_art": str(d[2] or ""),
                        "cantidad": Decimal(d[3] or 0),
                        "medida": str(d[4] or ""),
                        "precio": Decimal(d[5] or 0),
                        "porc_desc": Decimal(d[6] or 0),
                        "total_desc": Decimal(d[7] or 0),
                        "total_itbis": Decimal(d[8] or 0),
                        "total_linea": Decimal(d[9] or 0),
                    }
                )

    if not factura:
        return JsonResponse({"detail": "Factura no encontrada"}, status=404)

    documento_ecf = FacturacionElectronicaDocumento.objects.filter(id_doc=_to_int(id_doc, 0)).first()
    qr_target_url = ""
    qr_image_url = ""
    codigo_seguridad = ""
    estado_ecf = ""
    if documento_ecf:
        codigo_seguridad = str(documento_ecf.codigo_seguridad or "")
        estado_ecf = str(documento_ecf.estado or "")
        qr_target_url = _build_qr_url(empresa.get("rnc", ""), documento_ecf)
        qr_image_url = _build_qr_image_url(qr_target_url)

    factura_encf = str((factura or {}).get("encf") or "").strip()
    factura_id_ncf = _to_int((factura or {}).get("id_ncf"), 0)
    is_credit_note = factura_id_ncf == 34
    is_electronica = bool(documento_ecf or factura_encf.upper().startswith("E") or factura_id_ncf in ECF_ID_NCF_CODES)
    if is_credit_note:
        doc_label = "NOTA DE CREDITO / RI e-CF" if is_electronica else "NOTA DE CREDITO"
        doc_numero_label = "No. Nota de credito"
        detalle_label = "Detalle de Nota de Credito"
    else:
        doc_label = "FACTURA / RI e-CF" if is_electronica else "FACTURA"
        doc_numero_label = "No. Factura"
        detalle_label = "Detalle de Factura"

    return render(
        request,
        "factura/factura_print.html",
        {
            "auth_payload": auth_payload,
            "empresa": empresa,
            "firma_src": firma_src,
            "factura": factura,
            "detalles": detalles,
            "documento_ecf": documento_ecf,
            "codigo_seguridad": codigo_seguridad,
            "estado_ecf": estado_ecf,
            "qr_target_url": qr_target_url,
            "qr_image_url": qr_image_url,
            "fecha_impresion": timezone.localtime(),
            "copies_range": range(copies),
            "autoprint": autoprint,
            "mobile_print": mobile_print,
            "formato_impresion": get_print_format("factura"),
            "doc_label": doc_label,
            "doc_numero_label": doc_numero_label,
            "detalle_label": detalle_label,
            "is_electronica": is_electronica,
            "default_tipo_label": "Factura Electronica" if is_electronica else "Factura",
        },
    )


@csrf_exempt
@require_http_methods(["POST"])
def recibir_ecf_view(request):
    unauthorized = _require_ecf_callback_auth(request)
    if unauthorized:
        return unauthorized
    payload = _parse_request_payload(request)
    if payload is None:
        return JsonResponse({"detail": "JSON invalido"}, status=400)

    documento = _find_ecf_document(payload)
    if not documento:
        return JsonResponse({"detail": "Documento e-CF no encontrado. Envie id_doc, encf o track_id."}, status=404)

    estado = str(payload.get("estado") or payload.get("estatus") or "RECIBIDO").strip().upper() or "RECIBIDO"
    detalle = str(payload.get("detalle") or payload.get("observacion") or payload.get("mensaje") or "").strip()
    track_id = str(payload.get("track_id") or "").strip()
    codigo_seguridad = str(payload.get("codigo_seguridad") or payload.get("security_code") or "").strip()
    url_consulta_qr = str(payload.get("url_consulta_qr") or payload.get("qr_url") or "").strip()
    empresa = _get_empresa_data()

    update_fields = ["estado", "actualizado_en"]
    documento.estado = estado
    if track_id and not documento.track_id:
        documento.track_id = track_id
        update_fields.append("track_id")
    if codigo_seguridad:
        documento.codigo_seguridad = codigo_seguridad
        update_fields.append("codigo_seguridad")
    if "xml_generado" in payload:
        documento.xml_generado = _boolish(payload.get("xml_generado"))
        update_fields.append("xml_generado")
    if "firmado" in payload:
        documento.firmado = _boolish(payload.get("firmado"))
        update_fields.append("firmado")
    if "enviado_dgii" in payload:
        documento.enviado_dgii = _boolish(payload.get("enviado_dgii"))
        update_fields.append("enviado_dgii")
    if detalle:
        documento.respuesta_dgii = detalle
        update_fields.append("respuesta_dgii")
    if not url_consulta_qr and codigo_seguridad:
        url_consulta_qr = _build_qr_url(empresa.get("rnc", ""), documento)
    if url_consulta_qr:
        documento.url_consulta_qr = url_consulta_qr
        update_fields.append("url_consulta_qr")
    documento.save(update_fields=update_fields)

    FacturacionElectronicaEvento.objects.create(
        documento=documento,
        tipo_evento="RECEPCION_ECF",
        detalle=detalle or f"Recepcion registrada con estado {estado}.",
    )
    return JsonResponse(_build_event_response(documento, "RECEPCION_ECF"))


@csrf_exempt
@require_http_methods(["POST"])
def aprobar_ecf_view(request):
    unauthorized = _require_ecf_callback_auth(request)
    if unauthorized:
        return unauthorized
    payload = _parse_request_payload(request)
    if payload is None:
        return JsonResponse({"detail": "JSON invalido"}, status=400)

    documento = _find_ecf_document(payload)
    if not documento:
        return JsonResponse({"detail": "Documento e-CF no encontrado. Envie id_doc, encf o track_id."}, status=404)

    decision = str(payload.get("decision") or payload.get("aprobacion") or payload.get("estado") or "").strip().lower()
    decision_map = {
        "aprobado": "APROBADO_COMERCIAL",
        "aprobada": "APROBADO_COMERCIAL",
        "approved": "APROBADO_COMERCIAL",
        "aceptado": "APROBADO_COMERCIAL",
        "aceptada": "APROBADO_COMERCIAL",
        "rechazado": "RECHAZADO_COMERCIAL",
        "rechazada": "RECHAZADO_COMERCIAL",
        "rejected": "RECHAZADO_COMERCIAL",
        "devuelto": "RECHAZADO_COMERCIAL",
    }
    estado = decision_map.get(decision, "APROBACION_REGISTRADA")
    detalle = str(payload.get("detalle") or payload.get("observacion") or payload.get("mensaje") or "").strip()
    codigo_seguridad = str(payload.get("codigo_seguridad") or payload.get("security_code") or "").strip()
    url_consulta_qr = str(payload.get("url_consulta_qr") or payload.get("qr_url") or "").strip()
    empresa = _get_empresa_data()

    update_fields = ["estado", "actualizado_en"]
    documento.estado = estado
    if codigo_seguridad:
        documento.codigo_seguridad = codigo_seguridad
        update_fields.append("codigo_seguridad")
    if "xml_generado" in payload:
        documento.xml_generado = _boolish(payload.get("xml_generado"))
        update_fields.append("xml_generado")
    if "firmado" in payload:
        documento.firmado = _boolish(payload.get("firmado"))
        update_fields.append("firmado")
    if "enviado_dgii" in payload:
        documento.enviado_dgii = _boolish(payload.get("enviado_dgii"))
        update_fields.append("enviado_dgii")
    if detalle:
        documento.respuesta_dgii = detalle
        update_fields.append("respuesta_dgii")
    if not url_consulta_qr and codigo_seguridad:
        url_consulta_qr = _build_qr_url(empresa.get("rnc", ""), documento)
    if url_consulta_qr:
        documento.url_consulta_qr = url_consulta_qr
        update_fields.append("url_consulta_qr")
    documento.save(update_fields=update_fields)

    FacturacionElectronicaEvento.objects.create(
        documento=documento,
        tipo_evento="APROBACION_ECF",
        detalle=detalle or f"Aprobacion comercial registrada con estado {estado}.",
    )
    return JsonResponse(_build_event_response(documento, "APROBACION_ECF"))


@require_http_methods(["GET", "POST"])
def electronica_view(request):
    ctx = _base_context(request, page_title="Factura - Facturacion Electronica", active_nav="factura")
    if not ctx:
        return redirect("login")
    if not has_perm(ctx["auth_payload"]["usuario_id"], "factura", "ver_electronica"):
        return render_denied(request, active_nav="factura")

    config, _ = FacturacionElectronicaConfig.objects.get_or_create(
        id_config=1,
        defaults={"habilitado": False, "ambiente": "precertificacion", "modo_envio": "manual"},
    )
    status_message = ""

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip().lower()
        if action == "save_config":
            public_urls = _build_public_ecf_urls(request)
            config.habilitado = _bool_post(request.POST.get("habilitado"))
            config.ambiente = (request.POST.get("ambiente") or "precertificacion").strip() or "precertificacion"
            config.modo_envio = (request.POST.get("modo_envio") or "manual").strip() or "manual"
            config.certificado_ruta = (request.POST.get("certificado_ruta") or "").strip() or None
            config.certificado_clave = (request.POST.get("certificado_clave") or "").strip() or None
            config.url_recepcion_emisor = (request.POST.get("url_recepcion_emisor") or "").strip() or public_urls["recepcion"]
            config.url_aprobacion_emisor = (request.POST.get("url_aprobacion_emisor") or "").strip() or public_urls["aprobacion"]
            config.observaciones = (request.POST.get("observaciones") or "").strip() or None
            config.save()
            status_message = "Configuracion e-CF guardada."
        elif action == "save_sequence":
            tipo_ecf = (request.POST.get("tipo_ecf") or "").strip()
            if tipo_ecf in ECF_TIPOS_MAP:
                secuencia, _ = FacturacionElectronicaSecuencia.objects.get_or_create(
                    tipo_ecf=tipo_ecf,
                    defaults={"descripcion": ECF_TIPOS_MAP[tipo_ecf]},
                )
                secuencia.descripcion = ECF_TIPOS_MAP[tipo_ecf]
                secuencia.habilitada = _bool_post(request.POST.get("habilitada"))
                secuencia.secuencia_actual = max(_to_int(request.POST.get("secuencia_actual"), 1), 1)
                secuencia.secuencia_desde = max(_to_int(request.POST.get("secuencia_desde"), 1), 1)
                secuencia.secuencia_hasta = max(_to_int(request.POST.get("secuencia_hasta"), 0), 0)
                secuencia.vencimiento_secuencia = _to_date_or_none(request.POST.get("vencimiento_secuencia"))
                secuencia.save()
                status_message = f"Secuencia e-CF {tipo_ecf} actualizada."
        elif action == "sync_docs":
            total = _sync_ecf_documentos(limit=200)
            status_message = f"Sincronizados {total} documentos desde CAB_PEDIDO."
        elif action == "demo_qr":
            documento_id = _to_int(request.POST.get("id_doc"), 0)
            empresa = ctx.get("empresa") or {}
            documento = FacturacionElectronicaDocumento.objects.filter(id_doc=documento_id).first()
            if documento and documento.encf:
                _apply_demo_qr(documento, empresa.get("rnc", ""))
                status_message = f"QR demo generado para el documento {documento_id}."
            elif documento:
                status_message = f"El documento {documento_id} aun no tiene e-NCF; no se pudo generar QR demo."
            else:
                status_message = "Documento no encontrado para la prueba QR."

    if not FacturacionElectronicaDocumento.objects.exists():
        _sync_ecf_documentos(limit=80)

    empresa = ctx.get("empresa") or {}
    seq_map = {item.tipo_ecf: item for item in FacturacionElectronicaSecuencia.objects.all()}
    secuencias = []
    for codigo, nombre in ECF_TIPOS:
        secuencia = seq_map.get(codigo)
        preview = ""
        if secuencia:
            preview = f"E{codigo}{int(secuencia.secuencia_actual):010d}"
        secuencias.append({"codigo": codigo, "nombre": nombre, "obj": secuencia, "preview": preview})

    documentos = list(FacturacionElectronicaDocumento.objects.all()[:60])
    for documento in documentos:
        documento.tipo_ecf_nombre = ECF_TIPOS_MAP.get(documento.tipo_ecf or "", "Sin definir")
        documento.url_consulta_qr = _build_qr_url(empresa.get("rnc", ""), documento)

    readiness = {
        "empresa_rnc": bool(str(empresa.get("rnc") or "").strip()),
        "config_activa": bool(config.habilitado),
        "certificado": bool(str(config.certificado_ruta or "").strip()),
        "urls_receptor": bool(str(config.url_recepcion_emisor or "").strip()) and bool(str(config.url_aprobacion_emisor or "").strip()),
        "secuencias": FacturacionElectronicaSecuencia.objects.filter(habilitada=True).exists(),
    }
    readiness["completo"] = all(readiness.values())
    resumen = {
        "total": FacturacionElectronicaDocumento.objects.count(),
        "listos": FacturacionElectronicaDocumento.objects.filter(estado="LISTO_PARA_XML").count(),
        "registrados": FacturacionElectronicaDocumento.objects.filter(estado="REGISTRADO").count(),
        "pendientes_tipo": FacturacionElectronicaDocumento.objects.filter(estado="TIPO_PENDIENTE").count(),
    }
    ecf_public_urls = _build_public_ecf_urls(request)
    runtime_report = build_ecf_runtime_report()

    ctx.update(
        {
            "status_message": status_message,
            "ecf_config": config,
            "ecf_secuencias": secuencias,
            "ecf_documentos": documentos,
            "ecf_resumen": resumen,
            "ecf_readiness": readiness,
            "ecf_public_urls": ecf_public_urls,
            "ecf_runtime": runtime_report,
            "dgii_fuentes": [
                {
                    "title": "Documentacion sobre e-CF",
                    "url": "https://dgii.gov.do/cicloContribuyente/facturacion/comprobantesFiscalesElectronicosE-CF/Paginas/documentacionSobreE-CF.aspx",
                },
                {
                    "title": "Descripcion tecnica de facturacion electronica",
                    "url": "https://dgii.gov.do/cicloContribuyente/facturacion/comprobantesFiscalesElectronicosE-CF/Documentacin%20sobre%20eCF/Informe%20y%20Descripci%C3%B3n%20T%C3%A9cnica/Descripcion-tecnica-de-facturacion-electronica.pdf",
                },
                {
                    "title": "Preguntas Tecnicas e-CF",
                    "url": "https://dgii.gov.do/cicloContribuyente/facturacion/comprobantesFiscalesElectronicosE-CF/Preguntas%20frecuentes/T%C3%A9cnicas/Preguntas%20T%C3%A9cnicas%20e-CF.pdf",
                },
                {
                    "title": "Calendario oficial de implementacion",
                    "url": "https://dgii.gov.do/cicloContribuyente/facturacion/comprobantesFiscalesElectronicosE-CF/Paginas/Listados-contribuyentes-obligados-implementar-facturacion-electronica.aspx",
                },
            ],
        }
    )
    return render(request, "factura/electronica.html", ctx)
