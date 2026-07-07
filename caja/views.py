import base64
import json
import socket
import subprocess
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import lru_cache
from pathlib import Path

from django.db import connection, transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_http_methods

from ajustes.permissions import has_perm
from ajustes.print_formats import get_print_format, get_print_format_label
from ajustes.user_signatures import get_user_signature_bytes
from core.realtime import broadcast_cxc_document_status, broadcast_financiamiento_document_status
from core.views import _base_context, _get_empresa_data, render_denied
from prefacturas_app.models_existing import MaestroSn
from prefacturas_app.views import _get_auth_payload, _get_open_ed_balance, _require_perm_json


def _fmt_date(value):
    if not value:
        return ""
    if hasattr(value, "strftime"):
        return value.strftime("%d/%m/%Y")
    return str(value)


def _load_firma_b64(usuario_id):
    try:
        firma_bytes = get_user_signature_bytes(usuario_id)
        if firma_bytes:
            return base64.b64encode(firma_bytes).decode("ascii")
    except Exception:
        return ""
    return ""


def _load_usuario_meta(usuario_ref):
    usuario_ref = str(usuario_ref or "").strip()
    if not usuario_ref:
        return {}
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT TOP 1
                    ID_USUARIO,
                    ISNULL(NULLIF(LTRIM(RTRIM(NOMBRE)), ''), USUARIO) AS NOMBRE
                FROM USUARIO
                WHERE CAST(ID_USUARIO AS NVARCHAR(255)) = %s
                   OR LTRIM(RTRIM(ISNULL(USUARIO, ''))) = %s
                   OR LTRIM(RTRIM(ISNULL(NOMBRE, ''))) = %s
                """,
                [usuario_ref, usuario_ref, usuario_ref],
            )
            row = cursor.fetchone()
            if row:
                return {
                    "id": str(row[0] or "").strip(),
                    "nombre": str(row[1] or "").strip(),
                }
    except Exception:
        return {}
    return {}


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


def _to_float(value):
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


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


def _to_int_or_none(value):
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return int(float(raw))
    except Exception:
        return None


def _values_match(left, right, tolerance=Decimal("0.01")):
    return abs(_to_decimal(left) - _to_decimal(right)) <= tolerance


def _calculate_det_recibo_sub_pdo(total_pago, total_fact, cuota):
    total_pago_dec = _to_decimal(total_pago)
    total_fact_dec = _to_decimal(total_fact)
    cuota_dec = _to_decimal(cuota)
    if cuota_dec == Decimal("0"):
        return Decimal("0.00")
    sub_pdo = (total_pago_dec * total_fact_dec) / cuota_dec
    return sub_pdo.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _parse_date_value(value):
    if not value:
        return None
    if hasattr(value, "date"):
        try:
            return value.date()
        except Exception:
            pass
    if hasattr(value, "strftime"):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(text[:26], fmt).date()
        except ValueError:
            continue
    return None


def _days_overdue(value):
    if not value:
        return 0
    try:
        d = value.date() if hasattr(value, "date") else value
        return max((timezone.localdate() - d).days, 0)
    except Exception:
        return 0


def _chunked(sequence, size):
    for idx in range(0, len(sequence), size):
        yield sequence[idx:idx + size]


def _stringify_doc(value):
    if value is None:
        return ""
    text = str(value).strip()
    if text.endswith(".0"):
        return text[:-2]
    return text


def _doc_sort_key(value):
    text = _stringify_doc(value).replace(",", "")
    if not text:
        return (0, Decimal("0"), "")
    try:
        return (1, Decimal(text), text)
    except (InvalidOperation, TypeError, ValueError):
        return (0, Decimal("0"), text.lower())


def _catalogo_nombre_cta(row):
    for key in ("NOM_CTA", "NOMBRE_CTA", "NOM_CUENTA", "NOMBRE_CUENTA", "DESCRIPCION", "NOMBRE"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def _load_catalogo_cuentas(*, cta_financ=None, cta_prima_vacia=False):
    with connection.cursor() as cursor:
        sql = """
            SELECT *
            FROM CATALOGO
            WHERE NOM_TIPO = %s
              AND CTA_CAPITAL = %s
        """
        params = ["DETALLE", "N"]
        if cta_financ is not None:
            sql += " AND CTA_FINANC = %s"
            params.append(cta_financ)
        if cta_prima_vacia:
            sql += " AND NULLIF(LTRIM(RTRIM(ISNULL(CTA_PRIMA, ''))), '') IS NULL"
        sql += " ORDER BY NUM_CTA"
        cursor.execute(sql, params)
        columns = [col[0] for col in cursor.description]
        results = []
        for raw_row in cursor.fetchall():
            row = {columns[idx]: raw_row[idx] for idx in range(len(columns))}
            num_cta = str(row.get("NUM_CTA") or "").strip()
            if not num_cta:
                continue
            results.append(
                {
                    "num_cta": num_cta,
                    "nombre_cta": _catalogo_nombre_cta(row),
                }
            )
    return results


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


@lru_cache(maxsize=128)
def _load_table_string_limits(table_name):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_NAME = %s
            """,
            [table_name],
        )
        limits = {}
        for column_name, data_type, max_length in cursor.fetchall():
            normalized_name = str(column_name or "").strip().upper()
            normalized_type = str(data_type or "").strip().lower()
            if normalized_type not in {"char", "varchar", "nchar", "nvarchar"}:
                continue
            if max_length is None:
                continue
            try:
                max_length_value = int(max_length)
            except (TypeError, ValueError):
                continue
            if max_length_value <= 0:
                continue
            limits[normalized_name] = max_length_value
        return limits


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
    available = {str(column).upper(): str(column).upper() for column in columns}
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


def _assign_existing_values(target, columns, value, *candidates):
    if value is None:
        return
    available = {str(column).upper(): str(column).upper() for column in columns}
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
    sanitized = {}
    for column_name, raw_value in (values_by_column or {}).items():
        normalized_name = str(column_name).upper()
        value = raw_value
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
    params = [values_by_column[column] for column in insert_columns]
    if output_column:
        sql = (
            "DECLARE @codex_inserted_output TABLE (value sql_variant); "
            f"INSERT INTO {table_name} ({', '.join(f'[{column}]' for column in insert_columns)}) "
            f"OUTPUT INSERTED.[{output_column}] INTO @codex_inserted_output "
            f"VALUES ({placeholders}); "
            "SELECT TOP 1 CAST(value AS NVARCHAR(255)) FROM @codex_inserted_output;"
        )
        cursor.execute(sql, params)
        while cursor.description is None:
            if not cursor.nextset():
                return None
        row = cursor.fetchone()
        return row[0] if row else None
    sql = f"INSERT INTO {table_name} ({', '.join(f'[{column}]' for column in insert_columns)}) VALUES ({placeholders})"
    cursor.execute(sql, params)
    return None


def _update_dynamic_row(cursor, table_name, set_values, where_sql, where_params):
    set_values = _sanitize_table_values(table_name, set_values)
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


def _create_cxc_ed_entries(
    cursor,
    *,
    no_recibo,
    id_sn,
    nombre_cliente,
    rnc_ced,
    fecha_cont,
    fecha_doc,
    fecha_venc,
    total_recibo,
    comentario,
    periodo_cont,
    ejercicio,
    usuario_id,
    usuario_nombre,
    terminal,
    cta_asociada,
    cuenta_medio_pago,
    cuenta_medio_pago_desc,
):
    cuenta_cliente_num = "11020101"
    cuenta_cliente_nombre = "Cuentas por Cobrar Clientes"
    cuenta_medio_pago_num = str(cuenta_medio_pago or "").strip() or cuenta_cliente_num
    cuenta_medio_pago_nombre = str(cuenta_medio_pago_desc or "").strip() or cuenta_cliente_nombre
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

    comentario_ed = str(comentario or "").strip() or f"Recibo de ingreso {no_recibo}"
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
    _assign_existing_values(cab_ed_values, cab_ed_columns, "RI", "TIPO_DOC", "TD", "CLASE_DOC", "TIPO")
    _assign_existing_values(cab_ed_values, cab_ed_columns, total_recibo, "TOTAL_DOC", "MONTO", "IMPORTE")
    _assign_existing_values(cab_ed_values, cab_ed_columns, total_recibo, "ABONO")
    _assign_existing_values(cab_ed_values, cab_ed_columns, Decimal("0"), "SALDO")
    _assign_existing_values(cab_ed_values, cab_ed_columns, "RD$", "MON_DOC", "MONEDA")
    _assign_existing_values(cab_ed_values, cab_ed_columns, comentario_ed, "COMENTARIO", "OBSERVACION")
    _assign_existing_values(cab_ed_values, cab_ed_columns, "Abierto", "EST_DOC", "ESTADO", "ESTATUS")
    _assign_existing_values(cab_ed_values, cab_ed_columns, no_recibo, "ORIGEN", "REFERENCIA", "NO_RECIBO")
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
    det_ed_cliente_col = _pick_existing_column(det_ed_columns, "ID_SN", "CLIENTE", "COD_CLIENTE")

    def _build_det_ed_values(*, line_no, id_sn_value, cuenta_num, cuenta_nombre):
        det_ed_values = {}
        _assign_existing_values(det_ed_values, det_ed_columns, ed_doc_id, "ID_DOC", "ID_ED")
        _assign_existing_values(det_ed_values, det_ed_columns, ed_doc_no, "NO_DOC", "NO_ED")
        if det_line_col and det_line_col not in det_ed_identity_columns:
            _assign_existing_values(det_ed_values, det_ed_columns, line_no, det_line_col)
        _assign_existing_values(det_ed_values, det_ed_columns, fecha_cont, "FECHA_CONT", "F_CONT")
        _assign_existing_values(det_ed_values, det_ed_columns, fecha_doc, "FECHA_DOC", "FECHA_APLIC")
        _assign_existing_values(det_ed_values, det_ed_columns, fecha_venc, "FECHA_VENC", "F_VENC")
        if id_sn_value is None and det_ed_cliente_col:
            det_ed_values[det_ed_cliente_col] = None
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
        _assign_existing_values(det_ed_values, det_ed_columns, "RI", "TIPO_DOC", "TD", "CLASE_DOC", "TIPO")
        _assign_existing_values(det_ed_values, det_ed_columns, no_recibo, "ORIGEN", "REFERENCIA", "NO_RECIBO")
        _assign_existing_values(det_ed_values, det_ed_columns, Decimal("0"), "DEBITO", "DEBE")
        _assign_existing_values(det_ed_values, det_ed_columns, total_recibo, "CREDITO", "HABER")
        _assign_existing_values(det_ed_values, det_ed_columns, "RD$", "MON_DOC", "MONEDA")
        _assign_existing_values(det_ed_values, det_ed_columns, comentario_ed, "COMENTARIO", "OBSERVACION")
        _assign_existing_values(det_ed_values, det_ed_columns, cta_asociada, "CTA_ASOCIADA")
        _assign_existing_values(det_ed_values, det_ed_columns, usuario_id, "ID_USUARIO", "USUARIO_ID")
        _assign_existing_values(det_ed_values, det_ed_columns, usuario_nombre, "USUARIO", "USUARIO_NOMBRE")
        _assign_existing_values(det_ed_values, det_ed_columns, terminal, "TERMINAL")
        _assign_existing_values(det_ed_values, det_ed_columns, periodo_cont, "PERIODO_CONT")
        _assign_existing_values(det_ed_values, det_ed_columns, ejercicio, "EJERCICIO")
        _assign_existing_values(det_ed_values, det_ed_columns, timezone.localdate(), "FECHA_CREACION")
        _assign_existing_values(det_ed_values, det_ed_columns, timezone.localtime(), "FECHA_ACT")
        return det_ed_values

    for line_no, line_id_sn, line_cta, line_nom_cta in (
        (1, None, cuenta_medio_pago_num, cuenta_medio_pago_nombre),
        (2, id_sn, cuenta_cliente_num, cuenta_cliente_nombre),
    ):
        _insert_dynamic_row(
            cursor,
            "DET_ED",
            det_ed_columns,
            _build_det_ed_values(
                line_no=line_no,
                id_sn_value=line_id_sn,
                cuenta_num=line_cta,
                cuenta_nombre=line_nom_cta,
            ),
            skip_columns=det_ed_identity_columns,
        )

    catalogo_columns = _load_table_columns("CATALOGO")
    saldo_actual_col = _pick_existing_column(catalogo_columns, "SALDO_ACTUAL")
    num_cta_col = _pick_existing_column(catalogo_columns, "NUM_CTA")
    nom_cta_col = _pick_existing_column(catalogo_columns, "NOM_CTA", "NOMBRE_CTA", "NOM_CUENTA", "NOMBRE_CUENTA", "NOMBRE")
    if saldo_actual_col and num_cta_col:
        where_sql = f"[{num_cta_col}] = %s"
        where_params = [cuenta_cliente_num]
        if nom_cta_col:
            where_sql += f" AND LTRIM(RTRIM(ISNULL([{nom_cta_col}], ''))) = %s"
            where_params.append(cuenta_cliente_nombre)

        cursor.execute(
            f"SELECT TOP 1 ISNULL([{saldo_actual_col}], 0) FROM CATALOGO WITH (UPDLOCK, HOLDLOCK) WHERE {where_sql}",
            where_params,
        )
        saldo_row = cursor.fetchone()
        if not saldo_row:
            raise ValueError(
                f"No se encontro en CATALOGO la cuenta {cuenta_cliente_num} - {cuenta_cliente_nombre}."
            )

        nuevo_saldo = _to_decimal(saldo_row[0]) + _to_decimal(total_recibo)
        _update_dynamic_row(
            cursor,
            "CATALOGO",
            {saldo_actual_col: nuevo_saldo},
            where_sql,
            where_params,
        )


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


def _unique_columns(*columns):
    unique = []
    seen = set()
    for column in columns:
        if not column:
            continue
        key = str(column).upper()
        if key in seen:
            continue
        seen.add(key)
        unique.append(key)
    return unique


def _load_maestro_sn_lookup(id_sns):
    lookup = {}
    ids = [str(value).strip() for value in id_sns if str(value or "").strip()]
    if not ids:
        return lookup

    for ids_chunk in _chunked(list(dict.fromkeys(ids)), 300):
        placeholders = ", ".join(["%s"] * len(ids_chunk))
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT
                    m.ID_SN,
                    m.NOM_SOCIO,
                    m.CONTACTO,
                    m.RNC_CED,
                    ISNULL(m.DIR_FACTURA, ''),
                    ISNULL(t.DESCRIPCION, '')
                FROM MAESTRO_SN m
                LEFT JOIN Territorio t ON t.ID_CODIGO = m.ID_SECTOR
                WHERE m.ID_SN IN ({placeholders})
                """,
                ids_chunk,
            )
            for id_sn, nom_socio, contacto, rnc_ced, dir_factura, sector in cursor.fetchall():
                lookup[str(id_sn).strip()] = {
                    "nombre": str(nom_socio or "").strip(),
                    "apodo": str(contacto or "").strip(),
                    "rnc_ced": str(rnc_ced or "").strip(),
                    "direccion": str(dir_factura or "").strip(),
                    "sector": str(sector or "").strip(),
                }
    return lookup


def _pick_amount_value(row, *candidates, default=0.0, scan_patterns=None):
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

    if scan_patterns:
        scanned_matches = []
        for key, raw_value in row.items():
            key_upper = str(key).upper()
            if raw_value is None:
                continue
            if isinstance(raw_value, str) and not raw_value.strip():
                continue
            if any(token in key_upper for token in ("BALANCE", "SALDO", "PEND", "CUOTA", "MORA", "DESC", "RET")):
                continue
            amount = _to_float(raw_value)
            for priority, pattern in enumerate(scan_patterns):
                if all(token in key_upper for token in pattern):
                    scanned_matches.append((priority, key_upper, amount))
                    break

        scanned_matches.sort(key=lambda item: (item[0], item[1]))
        for _, _, amount in scanned_matches:
            if abs(amount) > 0.0001:
                return amount
        if scanned_matches:
            return scanned_matches[0][2]

    if present_values:
        return present_values[0]
    return _to_float(default)


def _number_to_spanish_words(number):
    units = (
        "",
        "UNO",
        "DOS",
        "TRES",
        "CUATRO",
        "CINCO",
        "SEIS",
        "SIETE",
        "OCHO",
        "NUEVE",
    )
    teens = (
        "DIEZ",
        "ONCE",
        "DOCE",
        "TRECE",
        "CATORCE",
        "QUINCE",
        "DIECISEIS",
        "DIECISIETE",
        "DIECIOCHO",
        "DIECINUEVE",
    )
    tens = (
        "",
        "",
        "VEINTE",
        "TREINTA",
        "CUARENTA",
        "CINCUENTA",
        "SESENTA",
        "SETENTA",
        "OCHENTA",
        "NOVENTA",
    )
    hundreds = (
        "",
        "CIENTO",
        "DOSCIENTOS",
        "TRESCIENTOS",
        "CUATROCIENTOS",
        "QUINIENTOS",
        "SEISCIENTOS",
        "SETECIENTOS",
        "OCHOCIENTOS",
        "NOVECIENTOS",
    )

    def convert_hundreds(n):
        n = int(n)
        if n == 0:
            return ""
        if n == 100:
            return "CIEN"
        if n < 10:
            return units[n]
        if n < 20:
            return teens[n - 10]
        if n < 30:
            if n == 20:
                return "VEINTE"
            return f"VEINTI{units[n - 20]}"
        if n < 100:
            ten = n // 10
            unit = n % 10
            return tens[ten] if unit == 0 else f"{tens[ten]} Y {units[unit]}"
        hundred = n // 100
        remainder = n % 100
        return hundreds[hundred] if remainder == 0 else f"{hundreds[hundred]} {convert_hundreds(remainder)}"

    n = int(abs(number or 0))
    if n == 0:
        return "CERO"

    millions = n // 1_000_000
    thousands = (n % 1_000_000) // 1000
    remainder = n % 1000
    parts = []

    if millions:
        if millions == 1:
            parts.append("UN MILLON")
        else:
            parts.append(f"{convert_hundreds(millions)} MILLONES")
    if thousands:
        if thousands == 1:
            parts.append("MIL")
        else:
            parts.append(f"{convert_hundreds(thousands)} MIL")
    if remainder:
        parts.append(convert_hundreds(remainder))

    return " ".join(part for part in parts if part).strip()


def _amount_to_spanish_words(value):
    amount = round(_to_float(value), 2)
    integer_part = int(amount)
    decimal_part = int(round((amount - integer_part) * 100))
    return f"{_number_to_spanish_words(integer_part)} PESOS CON {decimal_part:02d}/100 *****"


def _build_cxc_facturas_comment(detail_rows, *, close_account=False):
    if not detail_rows:
        return ""
    unique_docs = []
    seen_docs = set()
    for item in detail_rows:
        if not isinstance(item, dict):
            continue
        doc = str(item.get("no_doc") or "").strip()
        if not doc or doc in seen_docs:
            continue
        seen_docs.add(doc)
        unique_docs.append(doc)
    documentos = "".join(f"#{doc}," for doc in unique_docs)
    if not documentos:
        return ""
    prefix = "Cierre de Cuenta Factura(s): " if close_account else "Pago/abono Factura(s): "
    return f"{prefix}{documentos}"


def _get_det_recibo_payment_amount(row):
    return _to_decimal(
        _pick_amount_value(
            row,
            "TOTAL_PAGO",
            "TOTAL_PAGO2",
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
            "SALDO_VENC",
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


def _resolve_cxc_ed_total(cash_total, total_desc, *, is_close_account=False):
    cash_total_dec = _to_decimal(cash_total)
    total_desc_dec = _to_decimal(total_desc)
    if is_close_account:
        return total_desc_dec
    return max(cash_total_dec + total_desc_dec, Decimal("0"))


def _append_cancelled_comment(comment):
    marker = "(Documento Cancelado)"
    base = str(comment or "").strip()
    if marker.lower() in base.lower():
        return base
    return f"{base} {marker}".strip() if base else marker


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


def _load_cxc_default_payment_method(id_usuario):
    try:
        from ajustes.models import UsuarioCajaPreferencia

        record = UsuarioCajaPreferencia.objects.filter(id_usuario=id_usuario).only("metodo_pago_default").first()
        metodo = str(getattr(record, "metodo_pago_default", "") or "").strip()
        return metodo if metodo in {"Efectivo", "Transferencia"} else "Efectivo"
    except Exception:
        return "Efectivo"


def _require_any_caja_perm_json(request, *perm_codes):
    auth_payload = _get_auth_payload(request)
    if not auth_payload:
        return JsonResponse({"detail": "No autenticado"}, status=401)
    usuario_id = auth_payload.get("usuario_id")
    if any(has_perm(usuario_id, "caja", perm_code) for perm_code in perm_codes if perm_code):
        return auth_payload
    return JsonResponse({"detail": "Acceso denegado."}, status=403)


def _split_prefixed_row(row, prefix):
    prefix_upper = str(prefix).upper()
    return {
        key[len(prefix_upper):]: value
        for key, value in row.items()
        if str(key).upper().startswith(prefix_upper)
    }


def _adjust_catalogo_saldo_actual(cursor, *, cuenta_num, cuenta_nombre, delta):
    catalogo_columns = _load_table_columns("CATALOGO")
    saldo_actual_col = _pick_existing_column(catalogo_columns, "SALDO_ACTUAL")
    num_cta_col = _pick_existing_column(catalogo_columns, "NUM_CTA")
    nom_cta_col = _pick_existing_column(catalogo_columns, "NOM_CTA", "NOMBRE_CTA", "NOM_CUENTA", "NOMBRE_CUENTA", "NOMBRE")
    if not saldo_actual_col or not num_cta_col:
        return

    where_sql = f"[{num_cta_col}] = %s"
    where_params = [cuenta_num]
    if nom_cta_col:
        where_sql += f" AND LTRIM(RTRIM(ISNULL([{nom_cta_col}], ''))) = %s"
        where_params.append(cuenta_nombre)

    cursor.execute(
        f"SELECT TOP 1 ISNULL([{saldo_actual_col}], 0) FROM CATALOGO WITH (UPDLOCK, HOLDLOCK) WHERE {where_sql}",
        where_params,
    )
    saldo_row = cursor.fetchone()
    if not saldo_row:
        raise ValueError(
            f"No se encontro en CATALOGO la cuenta {cuenta_num} - {cuenta_nombre}."
        )

    nuevo_saldo = _to_decimal(saldo_row[0]) + _to_decimal(delta)
    _update_dynamic_row(
        cursor,
        "CATALOGO",
        {saldo_actual_col: nuevo_saldo},
        where_sql,
        where_params,
    )


def _create_cxc_cancel_ed_entries(
    cursor,
    *,
    recibo_id,
    no_recibo,
    usuario_id,
    usuario_nombre,
    terminal,
):
    cab_ed_columns = _load_table_columns("CAB_ED")
    det_ed_columns = _load_table_columns("DET_ED")
    if not cab_ed_columns or not det_ed_columns:
        raise ValueError("No se pudieron cargar las tablas CAB_ED/DET_ED.")

    cab_ed_identity_columns = _load_identity_columns("CAB_ED")
    det_ed_identity_columns = _load_identity_columns("DET_ED")
    cab_ed_key_col = _pick_existing_column(cab_ed_columns, "ID_DOC", "ID_ED", "NO_DOC", "NO_ED")
    cab_ed_no_col = _pick_existing_column(cab_ed_columns, "NO_DOC", "NO_ED", "ID_DOC", "ID_ED")
    cab_ed_tipo_col = _pick_existing_column(cab_ed_columns, "TIPO_DOC", "TD", "CLASE_DOC", "TIPO")
    cab_ed_origen_col = _pick_existing_column(cab_ed_columns, "ORIGEN", "REFERENCIA", "NO_RECIBO")
    cab_ed_status_col = _pick_existing_column(cab_ed_columns, "EST_DOC", "ESTADO", "ESTATUS")
    cab_ed_comment_col = _pick_existing_column(cab_ed_columns, "COMENTARIO", "OBSERVACION")
    if not cab_ed_key_col and not cab_ed_no_col:
        raise ValueError("No se pudo determinar la clave de CAB_ED.")
    if not cab_ed_origen_col:
        raise ValueError("No se pudo determinar el campo origen de CAB_ED.")

    origen_where_parts = [f"CAST([{cab_ed_origen_col}] AS NVARCHAR(255)) = %s"]
    where_params = [no_recibo]
    recibo_id_text = str(recibo_id or "").strip()
    if recibo_id_text and recibo_id_text != str(no_recibo or "").strip():
        origen_where_parts.append(f"CAST([{cab_ed_origen_col}] AS NVARCHAR(255)) = %s")
        where_params.append(recibo_id_text)
    where_sql_parts = [f"({' OR '.join(f'({part})' for part in origen_where_parts)})"]
    if cab_ed_tipo_col:
        where_sql_parts.append(f"UPPER(LTRIM(RTRIM(ISNULL([{cab_ed_tipo_col}], '')))) = 'RI'")
    if cab_ed_status_col:
        where_sql_parts.append(f"UPPER(LTRIM(RTRIM(ISNULL([{cab_ed_status_col}], '')))) <> 'CANCELADO'")

    order_column = cab_ed_no_col or cab_ed_key_col
    cursor.execute(
        f"""
        SELECT TOP 1 *
        FROM CAB_ED WITH (UPDLOCK, HOLDLOCK)
        WHERE {" AND ".join(where_sql_parts)}
        ORDER BY [{order_column}] DESC
        """,
        where_params,
    )
    raw_cab_ed = cursor.fetchone()
    if not raw_cab_ed:
        raise ValueError("No se encontro el CAB_ED abierto asociado al recibo.")
    raw_cab_ed_columns = [col[0] for col in cursor.description]
    original_cab_ed = _normalize_result_row(raw_cab_ed_columns, raw_cab_ed)

    original_cab_ed_id = _stringify_doc(_pick_row_value(original_cab_ed, cab_ed_key_col, cab_ed_no_col))
    original_cab_ed_no = _stringify_doc(_pick_row_value(original_cab_ed, cab_ed_no_col, cab_ed_key_col))
    if not original_cab_ed_id and not original_cab_ed_no:
        raise ValueError("No se pudo identificar el CAB_ED del recibo.")

    cancel_comment = _append_cancelled_comment(_pick_row_text(original_cab_ed, "COMENTARIO", "OBSERVACION"))
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
        raise ValueError("No se pudo identificar el detalle DET_ED del recibo.")

    det_ed_sql = f"SELECT * FROM DET_ED WITH (UPDLOCK, HOLDLOCK) WHERE {' OR '.join(f'({part})' for part in det_ed_where_parts)}"
    if det_ed_line_col:
        det_ed_sql += f" ORDER BY [{det_ed_line_col}]"
    cursor.execute(det_ed_sql, det_ed_where_params)
    raw_det_ed_columns = [col[0] for col in cursor.description]
    original_det_ed_rows = [_normalize_result_row(raw_det_ed_columns, raw_row) for raw_row in cursor.fetchall()]
    if not original_det_ed_rows:
        raise ValueError("No se encontraron registros en DET_ED para el recibo.")

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


def _load_factura_meta_lookup(doc_numbers):
    lookup = {}
    docs = [str(value).strip() for value in doc_numbers if str(value or "").strip()]
    if not docs:
        return lookup

    for docs_chunk in _chunked(list(dict.fromkeys(docs)), 300):
        placeholders = ", ".join(["%s"] * len(docs_chunk))
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT ID_DOC, TIPO_DOC, TOTAL_DOC, SALDO, FECHA_VENC, EST_DOC, ABONO
                FROM CAB_FACTURA
                WHERE CAST(ID_DOC AS VARCHAR(50)) IN ({placeholders})
                """,
                docs_chunk,
            )
            for id_doc, tipo_doc, total_doc, saldo, fecha_venc, est_doc, abono in cursor.fetchall():
                lookup[_stringify_doc(id_doc)] = {
                    "tipo_doc": str(tipo_doc or "").strip(),
                    "total_doc": _to_float(total_doc),
                    "saldo": _to_float(saldo),
                    "abono": _to_float(abono),
                    "fecha_venc": fecha_venc,
                    "est_doc": str(est_doc or "").strip(),
                }
    return lookup


def _load_prestamo_rows_by_doc(doc_numbers):
    rows_by_doc = {}
    docs = [str(value).strip() for value in doc_numbers if str(value or "").strip()]
    if not docs:
        return rows_by_doc

    det_columns = _load_table_columns("DET_PRESTAMO")
    doc_col = _pick_existing_column(det_columns, "NO_DOC", "ID_DOC", "DOCUMENTO", "FACTURA")
    cuota_num_col = _pick_existing_column(det_columns, "NO_CUOTA", "CUOTA_NUM", "NUM_CUOTA")
    cuota_col = _pick_existing_column(det_columns, "CUOTA", "MONTO_CUOTA", "VALOR_CUOTA")
    balance_col = _pick_existing_column(det_columns, "BALANCE")
    saldo_insoluto_col = _pick_existing_column(det_columns, "SALDO_INSOLUTO")
    fecha_col = _pick_existing_column(det_columns, "FECHA")
    fecha_venc_col = _pick_existing_column(det_columns, "FECHA_VENC", "F_VENC", "VENCIMIENTO")
    abono_cuota_col = _pick_existing_column(det_columns, "ABONO_CUOTA", "ABONOCUOTA", "ABONO_CUENTA", "ABONOCUENTA")
    if not doc_col:
        return rows_by_doc

    selected_columns = _unique_columns(
        doc_col,
        cuota_num_col,
        fecha_col,
        fecha_venc_col,
        cuota_col,
        balance_col,
        saldo_insoluto_col,
        abono_cuota_col,
    )
    if not selected_columns:
        return rows_by_doc

    for docs_chunk in _chunked(list(dict.fromkeys(docs)), 300):
        placeholders = ", ".join(["%s"] * len(docs_chunk))
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT {', '.join(f'[{column}]' for column in selected_columns)}
                FROM DET_PRESTAMO
                WHERE CAST([{doc_col}] AS VARCHAR(50)) IN ({placeholders})
                ORDER BY [{doc_col}], [{cuota_num_col or doc_col}]
                """,
                docs_chunk,
            )
            raw_columns = [col[0] for col in cursor.description]
            for raw_row in cursor.fetchall():
                row = _normalize_result_row(raw_columns, raw_row)
                no_doc = _stringify_doc(_pick_row_value(row, doc_col))
                if not no_doc:
                    continue
                rows_by_doc.setdefault(no_doc, []).append(
                    {
                        "no_cuota": _stringify_doc(_pick_row_value(row, cuota_num_col, default="1")) or "1",
                        "fecha": _pick_row_value(row, fecha_col),
                        "fecha_venc": _pick_row_value(row, fecha_venc_col),
                        "cuota": _pick_row_value(row, cuota_col, default=0),
                        "balance": _pick_row_value(row, balance_col, default=0),
                        "saldo_insoluto": _pick_row_value(row, saldo_insoluto_col, default=0),
                        "abono_cuota": _pick_row_value(row, abono_cuota_col, default=0),
                    }
                )
    return rows_by_doc


def _load_prestamo_meta_lookup(doc_numbers):
    lookup = {}
    for no_doc, rows in _load_prestamo_rows_by_doc(doc_numbers).items():
        for row in rows:
            no_cuota = _stringify_doc(row.get("no_cuota")) or "1"
            lookup[(no_doc, no_cuota)] = {
                "cuota": _to_float(row.get("cuota")),
                "balance": _to_float(row.get("balance")),
                "saldo_insoluto": _to_float(row.get("saldo_insoluto")),
                "abono_cuota": _to_float(row.get("abono_cuota")),
                "fecha_venc": row.get("fecha_venc"),
            }
    return lookup


def _detail_row_targets_prestamo(row, prestamo_lookup, *, no_doc=None, no_cuota=None, tolerance=Decimal("0.01")):
    no_doc_text = _stringify_doc(no_doc if no_doc is not None else _pick_row_value(row, "NO_DOC", "ID_DOC", "DOCUMENTO", "FACTURA"))
    no_cuota_text = _stringify_doc(
        no_cuota if no_cuota is not None else _pick_row_value(row, "NO_CUOTA", "CUOTA_NUM", "NUM_CUOTA", default="1")
    ) or "1"
    prestamo_meta = (prestamo_lookup or {}).get((no_doc_text, no_cuota_text)) or {}
    if not prestamo_meta:
        return False

    if no_cuota_text not in {"", "0", "1"}:
        return True

    cuota_prestamo = _to_decimal(prestamo_meta.get("cuota"))
    if cuota_prestamo <= tolerance:
        return False

    cuota_detalle = _to_decimal(
        _pick_amount_value(
            row,
            "CUOTA",
            "MONTO_CUOTA",
            "VALOR_CUOTA",
            default=0,
        )
    )
    if cuota_detalle > tolerance and abs(cuota_detalle - cuota_prestamo) <= tolerance:
        return True

    monto_pagado = _get_det_recibo_applied_amount(row)
    balance_pendiente = _to_decimal(
        _pick_amount_value(
            row,
            "TOTAL_RECIBO",
            "TOTAL_RECIBO2",
            "BALANCE_PEND",
            "SALDO_PEND",
            "PENDIENTE",
            default=0,
        )
    )
    cuota_reconstruida = balance_pendiente + monto_pagado
    if cuota_reconstruida > tolerance and abs(cuota_reconstruida - cuota_prestamo) <= tolerance:
        return True

    return False


def _resolve_prestamo_balance(balance, saldo_insoluto=None, cuota=None, pagos_aplicados=None, abono_cuota=None):
    balance_dec = _to_decimal(balance)
    cuota_dec = _to_decimal(cuota)
    pagos_dec = _to_decimal(pagos_aplicados)
    abono_cuota_dec = _to_decimal(abono_cuota)

    if balance_dec > Decimal("0.01"):
        return balance_dec

    if cuota is not None and abono_cuota is not None:
        reconstruido = max(cuota_dec - abono_cuota_dec, Decimal("0"))
        if reconstruido > Decimal("0.01") or abono_cuota_dec > Decimal("0.01"):
            return reconstruido

    if cuota is not None and pagos_aplicados is not None:
        reconstruido = max(cuota_dec - pagos_dec, Decimal("0"))
        if reconstruido > Decimal("0.01") or pagos_dec > Decimal("0.01"):
            return reconstruido

    if cuota_dec > Decimal("0.01"):
        return cuota_dec

    return max(balance_dec, Decimal("0"))


def _factura_closed_by_abono(total_doc, abono, tolerance=Decimal("0.01")):
    total_doc_dec = _to_decimal(total_doc)
    abono_dec = _to_decimal(abono)
    return total_doc_dec > Decimal("0") and abono_dec >= (total_doc_dec - tolerance)


def _resolve_factura_pending_for_payment(factura_row, *, pagos_doc=None, balance_hint=None, cuotas_rows=None):
    factura_row = factura_row or {}
    total_doc = max(_to_decimal(factura_row.get("total_doc")), Decimal("0"))
    saldo = max(_to_decimal(factura_row.get("saldo")), Decimal("0"))
    abono = max(_to_decimal(factura_row.get("abono")), Decimal("0"))
    pagos_doc_dec = max(_to_decimal(pagos_doc), Decimal("0"))
    balance_hint_dec = max(_to_decimal(balance_hint), Decimal("0"))
    estado_doc = str(factura_row.get("est_doc") or "").strip().upper()

    cuotas_pending = Decimal("0")
    for cuota_row in cuotas_rows or []:
        cuotas_pending += _to_decimal(
            _resolve_prestamo_balance(
                cuota_row.get("balance"),
                cuota_row.get("saldo_insoluto"),
                cuota_row.get("cuota"),
                None,
                cuota_row.get("abono_cuota"),
            )
        )

    if cuotas_pending > Decimal("0.01"):
        pending = cuotas_pending
        if balance_hint_dec > Decimal("0.01"):
            pending = max(pending, balance_hint_dec)
        if total_doc > Decimal("0"):
            pending = min(pending, total_doc)
        return max(pending, Decimal("0"))

    if saldo > Decimal("0.01"):
        if total_doc > Decimal("0"):
            return min(saldo, total_doc)
        return saldo

    paid_evidence = max(abono, pagos_doc_dec)
    if total_doc > Decimal("0") and paid_evidence > Decimal("0.01"):
        return max(total_doc - paid_evidence, Decimal("0"))

    if balance_hint_dec > Decimal("0.01"):
        if total_doc > Decimal("0"):
            return min(balance_hint_dec, total_doc)
        return balance_hint_dec

    if estado_doc == "ABIERTO" and total_doc > Decimal("0") and not _factura_closed_by_abono(total_doc, abono):
        return total_doc

    if total_doc > Decimal("0") and abono > Decimal("0.01"):
        return max(total_doc - abono, Decimal("0"))

    return saldo


def _load_cxc_active_payment_lookup(doc_numbers):
    lookup = {"by_doc": {}, "by_cuota": {}, "recibo_by_cuota": {}}
    docs = [str(value).strip() for value in doc_numbers if str(value or "").strip()]
    if not docs:
        return lookup

    prestamo_lookup = _load_prestamo_meta_lookup(docs)
    det_columns = _load_table_columns("DET_RECIBO_INGRESO")
    cab_columns = _load_table_columns("CAB_RECIBO_INGRESO")
    det_doc_col = _pick_existing_column(det_columns, "NO_DOC", "ID_DOC", "DOCUMENTO", "FACTURA")
    det_recibo_col = _pick_existing_column(det_columns, "ID_RECIBO", "NO_RECIBO", "ID_DOC", "NO_DOC")
    det_cuota_col = _pick_existing_column(det_columns, "NO_CUOTA", "CUOTA_NUM", "NUM_CUOTA")
    cab_id_col = _pick_existing_column(cab_columns, "ID_RECIBO", "NO_RECIBO", "ID_DOC", "NO_DOC")
    cab_no_col = _pick_existing_column(cab_columns, "NO_RECIBO", "ID_RECIBO", "ID_DOC", "NO_DOC")
    if not det_doc_col or not det_recibo_col or not cab_id_col:
        return lookup

    detail_rows = []
    recibo_refs = set()
    for docs_chunk in _chunked(list(dict.fromkeys(docs)), 300):
        placeholders = ", ".join(["%s"] * len(docs_chunk))
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT * FROM DET_RECIBO_INGRESO WHERE CAST([{det_doc_col}] AS NVARCHAR(255)) IN ({placeholders})",
                docs_chunk,
            )
            raw_columns = [col[0] for col in cursor.description]
            chunk_rows = [_normalize_result_row(raw_columns, raw_row) for raw_row in cursor.fetchall()]
        detail_rows.extend(chunk_rows)
        for row in chunk_rows:
            recibo_ref = _stringify_doc(_pick_row_value(row, det_recibo_col, "ID_RECIBO", "NO_RECIBO"))
            if recibo_ref:
                recibo_refs.add(recibo_ref)

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

            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT * FROM CAB_RECIBO_INGRESO WHERE {' OR '.join(f'({part})' for part in where_parts)}",
                    params,
                )
                raw_columns = [col[0] for col in cursor.description]
                for raw_row in cursor.fetchall():
                    row = _normalize_result_row(raw_columns, raw_row)
                    estado = _pick_row_text(row, "ESTATUS", "EST_DOC", "ESTADO").strip().upper()
                    cancelado = _pick_row_text(row, "CANCELADO").strip().upper()
                    if estado == "CANCELADO" or cancelado == "Y":
                        continue
                    recibo_id = _stringify_doc(_pick_row_value(row, cab_id_col, cab_no_col))
                    recibo_no = _stringify_doc(_pick_row_value(row, cab_no_col, cab_id_col))
                    if recibo_id:
                        recibos_activos.add(recibo_id)
                    if recibo_no:
                        recibos_activos.add(recibo_no)

    for row in detail_rows:
        recibo_ref = _stringify_doc(_pick_row_value(row, det_recibo_col, "ID_RECIBO", "NO_RECIBO"))
        if recibo_ref and recibo_ref not in recibos_activos:
            continue

        no_doc = _stringify_doc(_pick_row_value(row, det_doc_col, "NO_DOC", "ID_DOC", "DOCUMENTO", "FACTURA"))
        if not no_doc:
            continue
        no_cuota = _stringify_doc(_pick_row_value(row, det_cuota_col, default="1")) or "1"
        monto_pagado = _get_det_recibo_applied_amount(row)
        if monto_pagado <= Decimal("0"):
            continue
        lookup["by_doc"][no_doc] = lookup["by_doc"].get(no_doc, Decimal("0")) + monto_pagado
        if _detail_row_targets_prestamo(row, prestamo_lookup, no_doc=no_doc, no_cuota=no_cuota):
            lookup["by_cuota"][(no_doc, no_cuota)] = lookup["by_cuota"].get((no_doc, no_cuota), Decimal("0")) + monto_pagado
            if recibo_ref:
                current_ref = lookup["recibo_by_cuota"].get((no_doc, no_cuota))
                if not current_ref or _doc_sort_key(recibo_ref) >= _doc_sort_key(current_ref):
                    lookup["recibo_by_cuota"][(no_doc, no_cuota)] = recibo_ref

    return lookup


def _rebuild_det_prestamo_from_active_receipts(cursor, no_doc):
    no_doc_text = str(no_doc or "").strip()
    if not no_doc_text:
        return

    det_columns = _load_table_columns("DET_PRESTAMO")
    det_doc_col = _pick_existing_column(det_columns, "NO_DOC", "ID_DOC", "DOCUMENTO", "FACTURA")
    det_cuota_num_col = _pick_existing_column(det_columns, "NO_CUOTA", "CUOTA_NUM", "NUM_CUOTA")
    det_cuota_col = _pick_existing_column(det_columns, "CUOTA", "MONTO_CUOTA", "VALOR_CUOTA")
    det_balance_col = _pick_existing_column(det_columns, "BALANCE")
    det_abono_cuota_col = _pick_existing_column(det_columns, "ABONO_CUOTA", "ABONOCUOTA", "ABONO_CUENTA", "ABONOCUENTA")
    det_no_recibo_col = _pick_existing_column(det_columns, "NORECIBO", "NO_RECIBO")
    if not det_doc_col or not det_cuota_num_col or not det_cuota_col:
        return
    if not det_balance_col and not det_abono_cuota_col and not det_no_recibo_col:
        return

    cursor.execute(
        f"""
        SELECT *
        FROM DET_PRESTAMO WITH (UPDLOCK, HOLDLOCK)
        WHERE CAST([{det_doc_col}] AS NVARCHAR(255)) = %s
        ORDER BY [{det_cuota_num_col}]
        """,
        [no_doc_text],
    )
    raw_rows = cursor.fetchall()
    if not raw_rows:
        return
    raw_columns = [col[0] for col in cursor.description]
    prestamo_rows = [_normalize_result_row(raw_columns, raw_row) for raw_row in raw_rows]

    pagos_lookup = _load_cxc_active_payment_lookup([no_doc_text])
    pagos_por_cuota = pagos_lookup.get("by_cuota") or {}
    recibo_por_cuota = pagos_lookup.get("recibo_by_cuota") or {}

    for prestamo_row in prestamo_rows:
        no_cuota = _stringify_doc(_pick_row_value(prestamo_row, det_cuota_num_col, default="1")) or "1"
        cuota_val = _to_decimal(_pick_row_value(prestamo_row, det_cuota_col, default=0))
        pagado_activo = _to_decimal(pagos_por_cuota.get((no_doc_text, no_cuota)))
        if cuota_val > Decimal("0"):
            pagado_activo = min(pagado_activo, cuota_val)
        nuevo_abono_cuota = pagado_activo
        nuevo_balance = max(cuota_val - nuevo_abono_cuota, Decimal("0")) if cuota_val > Decimal("0") else Decimal("0")

        prestamo_updates = {}
        if det_balance_col:
            prestamo_updates[det_balance_col] = nuevo_balance
        if det_abono_cuota_col:
            prestamo_updates[det_abono_cuota_col] = nuevo_abono_cuota
        if det_no_recibo_col:
            prestamo_updates[det_no_recibo_col] = (
                recibo_por_cuota.get((no_doc_text, no_cuota))
                if nuevo_abono_cuota > Decimal("0.01")
                else None
            )
        if prestamo_updates:
            _update_dynamic_row(
                cursor,
                "DET_PRESTAMO",
                prestamo_updates,
                f"CAST([{det_doc_col}] AS NVARCHAR(255)) = %s AND CAST([{det_cuota_num_col}] AS NVARCHAR(255)) = %s",
                [no_doc_text, no_cuota],
            )


def _sync_cab_prestamo_from_det(cursor, no_doc, *, now=None):
    no_doc_text = str(no_doc or "").strip()
    if not no_doc_text:
        return

    cab_columns = _load_table_columns("CAB_PRESTAMO")
    det_columns = _load_table_columns("DET_PRESTAMO")
    if not cab_columns or not det_columns:
        return

    cab_doc_col = _pick_existing_column(cab_columns, "NO_DOC", "ID_DOC", "DOCUMENTO", "FACTURA")
    det_doc_col = _pick_existing_column(det_columns, "NO_DOC", "ID_DOC", "DOCUMENTO", "FACTURA")
    det_balance_col = _pick_existing_column(det_columns, "BALANCE")
    det_cuota_col = _pick_existing_column(det_columns, "CUOTA", "MONTO_CUOTA", "VALOR_CUOTA")
    det_abono_cuota_col = _pick_existing_column(det_columns, "ABONO_CUOTA", "ABONOCUOTA", "ABONO_CUENTA", "ABONOCUENTA")
    if not cab_doc_col or not det_doc_col or (not det_balance_col and not det_cuota_col):
        return

    cursor.execute(
        f"SELECT TOP 1 * FROM CAB_PRESTAMO WITH (UPDLOCK, HOLDLOCK) WHERE CAST([{cab_doc_col}] AS NVARCHAR(255)) = %s",
        [no_doc_text],
    )
    raw_cab_row = cursor.fetchone()
    if not raw_cab_row:
        return
    raw_cab_columns = [col[0] for col in cursor.description]
    cab_row = _normalize_result_row(raw_cab_columns, raw_cab_row)

    select_parts = []
    if det_balance_col:
        select_parts.append(f"ISNULL([{det_balance_col}], 0)")
    else:
        select_parts.append("0")
    if det_abono_cuota_col:
        select_parts.append(f"ISNULL([{det_abono_cuota_col}], 0)")
    else:
        select_parts.append("0")
    if det_cuota_col:
        select_parts.append(f"ISNULL([{det_cuota_col}], 0)")
    else:
        select_parts.append("0")
    cursor.execute(
        f"""
        SELECT {", ".join(select_parts)}
        FROM DET_PRESTAMO WITH (UPDLOCK, HOLDLOCK)
        WHERE CAST([{det_doc_col}] AS NVARCHAR(255)) = %s
        """,
        [no_doc_text],
    )
    balance_total = Decimal("0")
    cuotas_total = Decimal("0")
    for balance_raw, abono_cuota_raw, cuota_raw in cursor.fetchall():
        balance_total += _resolve_prestamo_balance(
            balance_raw,
            cuota=cuota_raw,
            abono_cuota=abono_cuota_raw,
        )
        cuotas_total += _to_decimal(cuota_raw)

    total_prestamo = _to_decimal(
        _pick_row_value(
            cab_row,
            "TOTAL_DOC",
            "MONTO",
            "IMPORTE",
            "TOTAL",
            "CAPITAL",
            "CAPITAL_FINANCIADO",
            default=cuotas_total,
        )
    )
    if total_prestamo <= Decimal("0") and cuotas_total > Decimal("0"):
        total_prestamo = cuotas_total

    cab_updates = {}
    _assign_existing_values(cab_updates, cab_columns, balance_total, "SALDO", "BALANCE", "SALDO_INSOLUTO")
    if total_prestamo > Decimal("0"):
        _assign_existing_values(cab_updates, cab_columns, max(total_prestamo - balance_total, Decimal("0")), "ABONO")
    _assign_existing_values(cab_updates, cab_columns, "Abierto" if balance_total > Decimal("0.01") else "Cerrado", "EST_DOC", "ESTATUS", "ESTADO")
    if now is not None:
        _assign_existing_values(cab_updates, cab_columns, now, "FECHA_ACT")

    if cab_updates:
        _update_dynamic_row(
            cursor,
            "CAB_PRESTAMO",
            cab_updates,
            f"CAST([{cab_doc_col}] AS NVARCHAR(255)) = %s",
            [no_doc_text],
        )


def _load_cxc_recibos_busqueda(query="", filtro="recibo", limit=150):
    columns = _load_table_columns("CAB_RECIBO_INGRESO")
    if not columns:
        return []

    id_col = _pick_existing_column(columns, "ID_RECIBO", "NO_RECIBO", "ID_DOC", "NO_DOC")
    no_recibo_col = _pick_existing_column(columns, "NO_RECIBO", "ID_RECIBO", "ID_DOC", "NO_DOC")
    cliente_col = _pick_existing_column(columns, "ID_SN", "CLIENTE", "COD_CLIENTE")
    nombre_col = _pick_existing_column(columns, "NOM_SOCIO", "NOMBRE", "NOM_CLIENTE")
    rnc_col = _pick_existing_column(columns, "RNC_CED", "RNC", "CEDULA")
    fecha_col = _pick_existing_column(columns, "FECHA_CONT", "F_CONT", "FECHA_DOC", "FECHA")
    efectivo_col = _pick_existing_column(columns, "IMP_EFECTIVO", "EFECTIVO", "MONTO_EFECTIVO", "PAGO_EFECTIVO")
    transferencia_col = _pick_existing_column(
        columns,
        "IMP_TRANSF",
        "TRANSFERENCIA",
        "MONTO_TRANSFERENCIA",
        "PAGO_TRANSFERENCIA",
    )
    total_cobro_col = _pick_existing_column(columns, "TOTAL_COBRO", "TOTAL_DOC", "IMPORTE", "MONTO")
    descuento_col = _pick_existing_column(columns, "TOTAL_DESCTO", "DESC_AVANCE", "DESCUENTO", "AVANCE")
    estatus_col = _pick_existing_column(columns, "ESTATUS", "EST_DOC", "ESTADO")
    selected_columns = _unique_columns(
        id_col,
        no_recibo_col,
        cliente_col,
        nombre_col,
        rnc_col,
        fecha_col,
        efectivo_col,
        transferencia_col,
        total_cobro_col,
        descuento_col,
        estatus_col,
    )

    like_value = f"%{(query or '').strip()}%"
    where_columns = []
    matching_client_ids = []
    filtro_normalizado = (filtro or "recibo").strip().lower()
    if like_value != "%%":
        if filtro_normalizado == "cliente":
            where_columns = _unique_columns(cliente_col, nombre_col)
            try:
                matching_client_ids = list(
                    MaestroSn.objects.filter(nom_socio__icontains=(query or "").strip())
                    .order_by("id_sn")
                    .values_list("id_sn", flat=True)[:50]
                )
            except Exception:
                matching_client_ids = []
        elif filtro_normalizado == "rnc":
            where_columns = _unique_columns(rnc_col)
        else:
            where_columns = _unique_columns(no_recibo_col, id_col)

    sql = f"SELECT TOP {max(1, min(int(limit), 300))} {', '.join(f'[{column}]' for column in selected_columns)} FROM CAB_RECIBO_INGRESO"
    params = []
    where_parts = []
    if where_columns:
        where_parts.append("(" + " OR ".join(f"CAST([{column}] AS NVARCHAR(255)) LIKE %s" for column in where_columns) + ")")
        params.extend([like_value] * len(where_columns))
    if filtro_normalizado == "cliente" and cliente_col and matching_client_ids:
        client_placeholders = ", ".join(["%s"] * len(matching_client_ids))
        where_parts.append(f"CAST([{cliente_col}] AS NVARCHAR(255)) IN ({client_placeholders})")
        params.extend(matching_client_ids)
    if where_parts:
        sql += " WHERE " + " OR ".join(where_parts)

    order_col = _pick_existing_column(columns, "FECHA_CONT", "F_CONT", "FECHA_DOC", "FECHA")
    receipt_order_columns = _unique_columns(no_recibo_col, id_col, order_col)
    order_parts = []
    for column in receipt_order_columns:
        order_parts.append(f"[{column}] DESC")
    if order_parts:
        sql += " ORDER BY " + ", ".join(order_parts)

    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        raw_columns = [col[0] for col in cursor.description]
        rows = [_normalize_result_row(raw_columns, raw_row) for raw_row in cursor.fetchall()]

    clientes_lookup = _load_maestro_sn_lookup(
        [_pick_row_text(row, cliente_col) for row in rows]
    )
    descuentos_lookup = _load_cxc_descuentos_lookup_by_refs(
        [
            *[_stringify_doc(_pick_row_value(row, id_col, no_recibo_col)) for row in rows],
            *[_stringify_doc(_pick_row_value(row, no_recibo_col, id_col)) for row in rows],
        ]
    )

    results = []
    for row in rows:
        recibo_id = _stringify_doc(_pick_row_value(row, id_col, no_recibo_col))
        no_recibo = _stringify_doc(_pick_row_value(row, no_recibo_col, id_col))
        cliente = _pick_row_text(row, cliente_col)
        maestro_sn = clientes_lookup.get(cliente, {})
        nombre = maestro_sn.get("nombre") or _pick_row_text(row, nombre_col)
        cliente_label = nombre or cliente
        efectivo = _pick_amount_value(row, efectivo_col, default=0.0)
        transferencia = _pick_amount_value(row, transferencia_col, default=0.0)
        total_cobro = _to_float(_pick_row_value(row, total_cobro_col, default=efectivo + transferencia))
        descuento_total = max(
            _pick_amount_value(row, descuento_col, default=0.0),
            _to_float(descuentos_lookup.get(recibo_id)),
            _to_float(descuentos_lookup.get(no_recibo)),
        )
        results.append(
            {
                "recibo_id": recibo_id,
                "no_recibo": no_recibo or recibo_id,
                "cliente": cliente_label,
                "cliente_codigo": cliente,
                "cliente_nombre": nombre,
                "rnc_ced": maestro_sn.get("rnc_ced") or _pick_row_text(row, rnc_col),
                "fecha_cont": _fmt_date(_pick_row_value(row, fecha_col)),
                "efectivo": efectivo,
                "transferencia": transferencia,
                "descuento_total": descuento_total,
                "total_cobro": total_cobro,
                "estatus": _pick_row_text(row, estatus_col) or "Abierto",
            }
        )
    results.sort(
        key=lambda item: (
            _doc_sort_key(item.get("no_recibo") or item.get("recibo_id")),
            _doc_sort_key(item.get("recibo_id")),
        ),
        reverse=True,
    )
    return results


def _load_cxc_descuentos_lookup_by_refs(receipt_refs):
    refs = [str(value or "").strip() for value in (receipt_refs or []) if str(value or "").strip()]
    refs = list(dict.fromkeys(refs))
    if not refs:
        return {}

    det_columns = _load_table_columns("DET_RECIBO_INGRESO")
    if not det_columns:
        return {}

    det_key_col = _pick_existing_column(det_columns, "ID_RECIBO", "NO_RECIBO", "ID_DOC", "NO_DOC")
    det_no_recibo_col = _pick_existing_column(det_columns, "NO_RECIBO", "ID_RECIBO", "ID_DOC", "NO_DOC")
    det_desc_col = _pick_existing_column(det_columns, "DESC_AVANCE", "DESCUENTO", "AVANCE", "DESC")
    if not det_key_col or not det_desc_col:
        return {}

    lookup = {}
    ref_columns = _unique_columns(det_key_col, det_no_recibo_col)
    try:
        with connection.cursor() as cursor:
            for ref_col in ref_columns:
                for refs_chunk in _chunked(refs, 300):
                    placeholders = ", ".join(["%s"] * len(refs_chunk))
                    cursor.execute(
                        f"""
                        SELECT
                            CAST([{ref_col}] AS NVARCHAR(255)) AS RECIBO_REF,
                            SUM(ISNULL(TRY_CONVERT(DECIMAL(18, 2), [{det_desc_col}]), 0)) AS TOTAL_DESCUENTO
                        FROM DET_RECIBO_INGRESO
                        WHERE CAST([{ref_col}] AS NVARCHAR(255)) IN ({placeholders})
                        GROUP BY CAST([{ref_col}] AS NVARCHAR(255))
                        """,
                        refs_chunk,
                    )
                    for recibo_ref, total_descuento in cursor.fetchall():
                        key = _stringify_doc(recibo_ref)
                        if not key:
                            continue
                        lookup[key] = max(_to_float(lookup.get(key)), _to_float(total_descuento))
    except Exception:
        return {}
    return lookup


def _load_cxc_cobros_anteriores(id_sn, exclude_recibo_id=""):
    if not id_sn:
        return []

    cab_columns = _load_table_columns("CAB_RECIBO_INGRESO")
    det_columns = _load_table_columns("DET_RECIBO_INGRESO")
    if not cab_columns or not det_columns:
        return []

    cab_key_col = _pick_existing_column(cab_columns, "ID_RECIBO", "NO_RECIBO", "ID_DOC", "NO_DOC")
    cab_no_recibo_col = _pick_existing_column(cab_columns, "NO_RECIBO", "ID_RECIBO", "ID_DOC", "NO_DOC")
    cab_cliente_col = _pick_existing_column(cab_columns, "ID_SN", "CLIENTE", "COD_CLIENTE")
    det_key_col = _pick_existing_column(det_columns, "ID_RECIBO", "NO_RECIBO", "ID_DOC", "NO_DOC")
    if not cab_key_col or not cab_cliente_col or not det_key_col:
        return []

    select_parts = [f"c.[{column}] AS [C__{column}]" for column in cab_columns]
    select_parts.extend(f"d.[{column}] AS [D__{column}]" for column in det_columns)
    sql = (
        f"SELECT {', '.join(select_parts)} "
        "FROM CAB_RECIBO_INGRESO c "
        f"INNER JOIN DET_RECIBO_INGRESO d ON c.[{cab_key_col}] = d.[{det_key_col}] "
        f"WHERE c.[{cab_cliente_col}] = %s"
    )
    params = [id_sn]

    exclude_text = str(exclude_recibo_id or "").strip()
    if exclude_text:
        sql += f" AND CAST(c.[{cab_key_col}] AS NVARCHAR(255)) <> %s"
        params.append(exclude_text)
        if cab_no_recibo_col and cab_no_recibo_col != cab_key_col:
            sql += f" AND CAST(c.[{cab_no_recibo_col}] AS NVARCHAR(255)) <> %s"
            params.append(exclude_text)

    order_candidates = _unique_columns(
        _pick_existing_column(cab_columns, "FECHA_CONT", "FECHA_DOC"),
        cab_no_recibo_col,
        _pick_existing_column(det_columns, "NO_DOC", "ID_DOC"),
        _pick_existing_column(det_columns, "NO_CUOTA", "CUOTA_NUM", "NUM_CUOTA"),
    )
    if order_candidates:
        sql += " ORDER BY " + ", ".join(
            [
                f"c.[{order_candidates[0]}] ASC",
                *[
                    (
                        f"c.[{column}] ASC"
                        if column in cab_columns
                        else f"d.[{column}]"
                    )
                    for column in order_candidates[1:]
                ],
            ]
        )

    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        raw_columns = [col[0] for col in cursor.description]
        joined_rows = [_normalize_result_row(raw_columns, raw_row) for raw_row in cursor.fetchall()]

    detail_rows = []
    doc_numbers = []
    for row in joined_rows:
        cab_row = _split_prefixed_row(row, "C__")
        det_row = _split_prefixed_row(row, "D__")
        no_doc = _stringify_doc(_pick_row_value(det_row, "NO_DOC", "ID_DOC", "DOCUMENTO", "FACTURA"))
        if no_doc:
            doc_numbers.append(no_doc)
        detail_rows.append((cab_row, det_row))

    factura_lookup = _load_factura_meta_lookup(doc_numbers)
    prestamo_lookup = _load_prestamo_meta_lookup(doc_numbers)

    results = []
    for cab_row, det_row in detail_rows:
        no_factura = _stringify_doc(_pick_row_value(det_row, "NO_DOC", "ID_DOC", "DOCUMENTO", "FACTURA"))
        no_cuota = _stringify_doc(_pick_row_value(det_row, "NO_CUOTA", "CUOTA_NUM", "NUM_CUOTA", default="1")) or "1"
        factura_meta = factura_lookup.get(no_factura, {})
        prestamo_meta = prestamo_lookup.get((no_factura, no_cuota), {})
        total_pago = _to_float(_get_det_recibo_payment_amount(det_row))
        monto_fact = _pick_amount_value(
            det_row,
            "MONTO_FACT",
            "MONTO_DOC",
            "TOTAL_DOC",
            "MONTO_FACTURA",
            "IMP_FACT",
            "VALOR_FACTURA",
            default=factura_meta.get("total_doc", 0.0),
        )
        cuota = _pick_amount_value(
            det_row,
            "CUOTA",
            "MONTO_CUOTA",
            "VALOR_CUOTA",
            default=prestamo_meta.get("cuota") or monto_fact,
        )
        saldo_pend = _pick_amount_value(
            det_row,
            "TOTAL_RECIBO",
            "BALANCE_PEND",
            "SALDO_PEND",
            "PENDIENTE",
            "BALANCE_DOC",
            "BALANCE",
            "SALDO_DOC",
            "SALDO",
            default=prestamo_meta.get("balance", factura_meta.get("saldo", 0.0)),
        )
        fecha_venc = _pick_row_value(
            det_row,
            "FECHA_VENC",
            "F_VENC",
            "VENCIMIENTO",
            default=prestamo_meta.get("fecha_venc") or factura_meta.get("fecha_venc"),
        )
        results.append(
            {
                "recibo_id": _stringify_doc(_pick_row_value(cab_row, "ID_RECIBO", "NO_RECIBO", "ID_DOC")),
                "no_recibo": _stringify_doc(_pick_row_value(cab_row, "NO_RECIBO", "ID_RECIBO", "ID_DOC")),
                "fecha_rec": _fmt_date(_pick_row_value(cab_row, "FECHA_CONT", "FECHA_DOC")),
                "td": _pick_row_text(det_row, "TIPO_DOC", "TD", "CLASE_DOC", "TIPO") or factura_meta.get("tipo_doc", ""),
                "no_factura": no_factura,
                "monto_fact": monto_fact,
                "no_cuota": no_cuota,
                "cuota": cuota,
                "saldo_pend": saldo_pend,
                "fecha_venc": _fmt_date(fecha_venc),
                "mora": _pick_amount_value(det_row, "MORA", "CARGO", "TOTAL_MORA", default=0.0),
                "descuento": _pick_amount_value(det_row, "DESCUENTO", "DESC_AVANCE", "AVANCE", "DESC", default=0.0),
                "total_pago": total_pago,
                "estado": _pick_row_text(cab_row, "ESTATUS", "EST_DOC", "ESTADO"),
            }
        )

    return results


def _load_cxc_factura_historial_pago(no_doc):
    no_doc_text = str(no_doc or "").strip()
    if not no_doc_text:
        return None

    cab_columns = _load_table_columns("CAB_FACTURA")
    if not cab_columns:
        return None

    doc_col = _pick_existing_column(cab_columns, "ID_DOC", "NO_DOC", "DOCUMENTO", "FACTURA")
    fecha_col = _pick_existing_column(cab_columns, "FECHA_DOC", "FECHA_CONT", "FECHA")
    fecha_venc_col = _pick_existing_column(cab_columns, "FECHA_VENC", "F_VENC", "VENCIMIENTO")
    total_col = _pick_existing_column(cab_columns, "TOTAL_DOC", "MONTO", "IMPORTE", "TOTAL")
    saldo_col = _pick_existing_column(cab_columns, "SALDO", "BALANCE")
    abono_col = _pick_existing_column(cab_columns, "ABONO")
    estado_col = _pick_existing_column(cab_columns, "EST_DOC", "ESTATUS", "ESTADO")
    cliente_col = _pick_existing_column(cab_columns, "ID_SN", "CLIENTE", "COD_CLIENTE")

    if not doc_col:
        return None

    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT TOP 1 * FROM CAB_FACTURA WHERE CAST([{doc_col}] AS NVARCHAR(255)) = %s",
            [no_doc_text],
        )
        raw_row = cursor.fetchone()
        if not raw_row:
            return None
        raw_columns = [col[0] for col in cursor.description]
        factura_row = _normalize_result_row(raw_columns, raw_row)

    id_sn = _pick_row_text(factura_row, cliente_col)
    clientes_lookup = _load_maestro_sn_lookup([id_sn])
    cliente_info = clientes_lookup.get(id_sn, {})

    factura_data = {
        "no_doc": no_doc_text,
        "cliente_codigo": id_sn,
        "cliente_nombre": cliente_info.get("nombre") or id_sn,
        "fecha_doc": _fmt_date(_pick_row_value(factura_row, fecha_col)),
        "fecha_venc": _fmt_date(_pick_row_value(factura_row, fecha_venc_col)),
        "total_doc": _to_float(_pick_row_value(factura_row, total_col, default=0.0)),
        "saldo": _to_float(_pick_row_value(factura_row, saldo_col, default=0.0)),
        "abono": _to_float(_pick_row_value(factura_row, abono_col, default=0.0)),
        "estado": _pick_row_text(factura_row, estado_col),
    }

    # Query payments applied to this invoice
    det_columns = _load_table_columns("DET_RECIBO_INGRESO")
    cab_columns_ri = _load_table_columns("CAB_RECIBO_INGRESO")
    if not det_columns or not cab_columns_ri:
        return {"factura": factura_data, "pagos": []}

    det_doc_col = _pick_existing_column(det_columns, "NO_DOC", "ID_DOC", "DOCUMENTO", "FACTURA")
    det_recibo_col = _pick_existing_column(det_columns, "ID_RECIBO", "NO_RECIBO")
    cab_key_col = _pick_existing_column(cab_columns_ri, "ID_RECIBO", "NO_RECIBO")

    if not det_doc_col or not det_recibo_col or not cab_key_col:
        return {"factura": factura_data, "pagos": []}

    select_parts = [f"c.[{column}] AS [C__{column}]" for column in cab_columns_ri]
    select_parts.extend(f"d.[{column}] AS [D__{column}]" for column in det_columns)
    sql = (
        f"SELECT {', '.join(select_parts)} "
        "FROM CAB_RECIBO_INGRESO c "
        f"INNER JOIN DET_RECIBO_INGRESO d ON c.[{cab_key_col}] = d.[{det_recibo_col}] "
        f"WHERE CAST(d.[{det_doc_col}] AS NVARCHAR(255)) = %s"
    )

    order_col_ri = _pick_existing_column(cab_columns_ri, "FECHA_CONT", "FECHA_DOC", "FECHA")
    if order_col_ri:
        sql += f" ORDER BY c.[{order_col_ri}] ASC"

    with connection.cursor() as cursor:
        cursor.execute(sql, [no_doc_text])
        raw_ri_columns = [col[0] for col in cursor.description]
        ri_rows = [_normalize_result_row(raw_ri_columns, raw_row) for raw_row in cursor.fetchall()]

    pagos = []
    for row in ri_rows:
        cab_ri = _split_prefixed_row(row, "C__")
        det_ri = _split_prefixed_row(row, "D__")

        pago_abono = _to_float(_get_det_recibo_payment_amount(det_ri))
        descuento = _to_float(_get_det_recibo_discount_amount(det_ri))
        mora = _to_float(_pick_amount_value(det_ri, "MORA", "CARGO", "TOTAL_MORA", default=0.0))

        pagos.append({
            "recibo_id": _stringify_doc(_pick_row_value(cab_ri, "ID_RECIBO", "NO_RECIBO")),
            "no_recibo": _stringify_doc(_pick_row_value(cab_ri, "NO_RECIBO", "ID_RECIBO")),
            "fecha_rec": _fmt_date(_pick_row_value(cab_ri, "FECHA_CONT", "FECHA_DOC")),
            "pago_abono": pago_abono,
            "descuento": descuento,
            "mora": mora,
            "total_pago": pago_abono + descuento + mora,
            "estado": _pick_row_text(cab_ri, "ESTATUS", "EST_DOC", "ESTADO") or "Abierto",
        })

    # Query products (details) for this invoice
    det_factura_columns = _load_table_columns("DET_FACTURA")
    productos = []
    if det_factura_columns:
        det_doc_col_f = _pick_existing_column(det_factura_columns, "ID_DOC", "NO_DOC", "DOCUMENTO")
        det_art_col_f = _pick_existing_column(det_factura_columns, "ID_ARTICULO", "ARTICULO", "COD_ART")
        det_desc_col_f = _pick_existing_column(det_factura_columns, "DESCRIP_ART", "DESCRIPCION", "DESCRIP")
        det_cant_col_f = _pick_existing_column(det_factura_columns, "CANTIDAD", "CANT")
        det_precio_col_f = _pick_existing_column(det_factura_columns, "PRECIO", "PRECIO_UNIT")
        det_total_col_f = _pick_existing_column(det_factura_columns, "TOTAL_LINEA", "TOTAL", "IMPORTE", "TOTAL_PRECIO")

        if det_doc_col_f and det_art_col_f:
            sql_det = (
                f"SELECT * FROM DET_FACTURA "
                f"WHERE CAST([{det_doc_col_f}] AS NVARCHAR(255)) = %s"
            )
            with connection.cursor() as cursor:
                cursor.execute(sql_det, [no_doc_text])
                raw_det_cols = [col[0] for col in cursor.description]
                det_rows = [_normalize_result_row(raw_det_cols, raw_row) for raw_row in cursor.fetchall()]

            for r in det_rows:
                cant = _to_float(_pick_row_value(r, det_cant_col_f, default=0.0))
                precio = _to_float(_pick_row_value(r, det_precio_col_f, default=0.0))
                total = _to_float(_pick_row_value(r, det_total_col_f, default=cant * precio))
                productos.append({
                    "articulo_id": _pick_row_text(r, det_art_col_f),
                    "descripcion": _pick_row_text(r, det_desc_col_f) or "",
                    "cantidad": cant,
                    "precio": precio,
                    "total": total,
                })

    return {
        "factura": factura_data,
        "pagos": pagos,
        "productos": productos
    }


def _build_cxc_recibo_payload(header_row, detail_rows):
    cliente_codigo = _pick_row_text(header_row, "ID_SN", "CLIENTE", "COD_CLIENTE")
    maestro_sn = _load_maestro_sn_lookup([cliente_codigo]).get(cliente_codigo, {})

    efectivo = _pick_amount_value(
        header_row,
        "IMP_EFECTIVO",
        "EFECTIVO",
        "MONTO_EFECTIVO",
        "PAGO_EFECTIVO",
        default=0.0,
    )
    transferencia = _pick_amount_value(
        header_row,
        "IMP_TRANSF",
        "TRANSFERENCIA",
        "MONTO_TRANSFERENCIA",
        "PAGO_TRANSFERENCIA",
        default=0.0,
    )
    total_recibo_header = _to_float(
        _pick_row_value(header_row, "TOTAL_COBRO", "TOTAL_DOC", "IMPORTE", "MONTO", default=efectivo + transferencia)
    )
    total_mora_header = _to_float(_pick_row_value(header_row, "TOTAL_MORA", "MORA", "CARGO", default=0))
    desc_avance_header = _to_float(_pick_row_value(header_row, "TOTAL_DESCTO", "DESC_AVANCE", "DESCUENTO", "AVANCE", default=0))
    total_ret_header = _to_float(_pick_row_value(header_row, "TOTAL_RET", "RETENCION", "RET", default=0))
    comentario = _pick_row_text(header_row, "COMENTARIO", "OBSERVACION")
    moneda = _pick_row_text(header_row, "MONEDA", "MON_DOC", default="RD$")
    proyecto = _pick_row_text(header_row, "ID_PROYECTO", "PROYECTO")
    cuenta_caja = _pick_row_text(
        header_row,
        "CTA_CAJA",
        "CTA_BANCO_CAJA",
        "CTA_BANCO",
        "CTA_COBRO",
        "CTA_INGRESO",
        default="11010101",
    )
    cuenta_caja_desc = _pick_row_text(
        header_row,
        "CTA_CAJA_DESC",
        "DESC_CTA_CAJA",
        "NOM_CTA_CAJA",
        "CTA_BANCO_CAJA_DESC",
        "NOM_CTA_BANCO",
        default="Caja General" if cuenta_caja == "11010101" else "",
    )
    cuenta_efectivo = _pick_row_text(
        header_row,
        "CTA_EFECTIVO",
        "CTA_CHEQUE",
        "CTA_TARJETA",
        "CTA_CAJA",
        "CTA_BANCO_CAJA",
        default=cuenta_caja,
    )
    cuenta_efectivo_desc = _pick_row_text(
        header_row,
        "NOM_CTA",
        "NOM_CTA3",
        "CTA_CAJA_DESC",
        "DESC_CTA_CAJA",
        "NOM_CTA_CAJA",
        default=cuenta_caja_desc,
    )
    cuenta_transferencia = _pick_row_text(
        header_row,
        "CTA_TRANSF",
        "CTA_CAJA",
        "CTA_BANCO_CAJA",
        default=cuenta_caja,
    )
    cuenta_transferencia_desc = _pick_row_text(
        header_row,
        "NOM_CTA2",
        "CTA_CAJA_DESC",
        "DESC_CTA_CAJA",
        "NOM_CTA_CAJA",
        default=cuenta_caja_desc,
    )
    cuenta_desc_ret = _pick_row_text(
        header_row,
        "CTA_DESCTO",
        "CTA_DESC_RET",
        "CTA_DESC",
        "CTA_RET",
        default="41020102",
    )
    cuenta_desc_ret_desc = _pick_row_text(
        header_row,
        "CTA_DESC_RET_DESC",
        "DESC_CTA_DESC_RET",
        "NOM_CTA_DESC",
        "NOM_CTA_RET",
        default="Descuentos en Servicios" if cuenta_desc_ret == "41020102" else "",
    )
    no_transferencia = _pick_row_text(
        header_row,
        "NO_TRANSF",
        "NO_TRANSFERENCIA",
        "REFERENCIA_TRANSF",
        "NO_REF",
        "REFERENCIA",
    )
    cuenta_cliente_pago = _pick_row_text(
        header_row,
        "NO_CTA_CLIENTE",
        "CTA_CLIENTE",
        "CUENTA_CLIENTE",
    )
    metodo = "Transferencia" if transferencia > 0.0001 else "Efectivo"
    detail_doc_counts = {}
    detail_doc_numbers = []
    for raw_row in detail_rows:
        no_doc = _stringify_doc(_pick_row_value(raw_row, "NO_DOC", "ID_DOC", "DOCUMENTO", "FACTURA"))
        if not no_doc:
            continue
        detail_doc_numbers.append(no_doc)
        detail_doc_counts[no_doc] = detail_doc_counts.get(no_doc, 0) + 1
    factura_lookup = _load_factura_meta_lookup(detail_doc_numbers)
    prestamo_rows_by_doc = _load_prestamo_rows_by_doc(detail_doc_numbers)
    prestamo_lookup = {}
    balance_total_por_doc = {}
    for no_doc, rows in prestamo_rows_by_doc.items():
        total_balance_doc = 0.0
        for row in rows:
            no_cuota = _stringify_doc(row.get("no_cuota")) or "1"
            prestamo_lookup[(no_doc, no_cuota)] = {
                "cuota": _to_float(row.get("cuota")),
                "balance": _to_float(row.get("balance")),
                "saldo_insoluto": _to_float(row.get("saldo_insoluto")),
                "abono_cuota": _to_float(row.get("abono_cuota")),
                "fecha_venc": row.get("fecha_venc"),
            }
            total_balance_doc += _to_float(
                _resolve_prestamo_balance(
                    row.get("balance"),
                    row.get("saldo_insoluto"),
                    row.get("cuota"),
                    None,
                    row.get("abono_cuota"),
                )
            )
        balance_total_por_doc[no_doc] = total_balance_doc

    for no_doc in dict.fromkeys(detail_doc_numbers):
        if no_doc in balance_total_por_doc:
            continue
        factura_meta = factura_lookup.get(no_doc, {})
        total_doc_meta = _to_float(factura_meta.get("total_doc"))
        saldo_meta = _to_float(factura_meta.get("saldo"))
        abono_meta = _to_float(factura_meta.get("abono"))
        balance_total_por_doc[no_doc] = max(saldo_meta, max(total_doc_meta - abono_meta, 0.0))
    detalle = []

    for raw_row in detail_rows:
        no_doc = _stringify_doc(_pick_row_value(raw_row, "NO_DOC", "ID_DOC", "DOCUMENTO", "FACTURA"))
        no_cuota = _stringify_doc(_pick_row_value(raw_row, "NO_CUOTA", "CUOTA_NUM", "NUM_CUOTA", default="1")) or "1"
        prestamo_meta_candidate = prestamo_lookup.get((no_doc, no_cuota), {})
        is_financed_line = _detail_row_targets_prestamo(
            raw_row,
            prestamo_lookup,
            no_doc=no_doc,
            no_cuota=no_cuota,
        )
        prestamo_meta = prestamo_meta_candidate if is_financed_line else {}
        balance_doc = _to_float(
            _pick_row_value(raw_row, "BALANCE_DOC", "BALANCE", "SALDO_DOC", "SALDO", "MONTO_DOC", "CUOTA", default=0)
        )
        pago_abono = _pick_amount_value(
            raw_row,
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
            default=balance_doc,
            scan_patterns=[
                ("IMP", "ABONO"),
                ("IMP", "PAGO"),
                ("IMP", "COBRO"),
                ("IMP", "APLIC"),
                ("MONTO", "ABONO"),
                ("MONTO", "PAGO"),
                ("MONTO", "COBRO"),
                ("MONTO", "APLIC"),
                ("VALOR", "ABONO"),
                ("ABONO",),
                ("PAGADO",),
                ("PAGO",),
                ("COBRO",),
                ("APLIC",),
            ],
        )
        if balance_doc <= 0 < pago_abono:
            balance_doc = pago_abono
        cuota = _to_float(
            _pick_row_value(raw_row, "CUOTA", "MONTO_CUOTA", "VALOR_CUOTA", default=prestamo_meta.get("cuota") or balance_doc)
        )
        fecha_venc_value = _pick_row_value(raw_row, "FECHA_VENC", "F_VENC", "VENCIMIENTO")
        dias = int(_to_float(_pick_row_value(raw_row, "DIAS", "DIAS_VENC", "ATRASO", default=_days_overdue(fecha_venc_value))))
        balance_pend = _to_float(
            _pick_amount_value(
                raw_row,
                "TOTAL_RECIBO",
                "TOTAL_RECIBO2",
                "BALANCE_PEND",
                "SALDO_PEND",
                "PENDIENTE",
                "BALANCE_DOC",
                "BALANCE",
                "SALDO_DOC",
                "SALDO",
                default=prestamo_meta.get("balance", max(balance_doc - pago_abono, 0)),
            )
        )
        if is_financed_line:
            if cuota > 0:
                balance_doc = cuota
            else:
                cuota_balance = balance_pend + pago_abono
                if cuota_balance > 0:
                    balance_doc = cuota_balance
                elif prestamo_meta.get("balance") is not None:
                    balance_doc = _to_float(prestamo_meta.get("balance"))
        desc_avance_value = _to_float(_pick_row_value(raw_row, "DESC_AVANCE", "DESCUENTO", "AVANCE", default=0))
        balance_pend_with_discount = max(balance_pend - desc_avance_value, 0)
        detalle.append(
            {
                "td": _pick_row_text(raw_row, "TIPO_DOC", "TD", "CLASE_DOC", "TIPO"),
                "linea": _stringify_doc(_pick_row_value(raw_row, "NO_LINEA", "LINEA", "NO_ITEM", "ORDEN", default="")),
                "no_doc": no_doc,
                "fecha_cont": _fmt_date(_pick_row_value(raw_row, "FECHA_CONT", "F_CONT", "FECHA_DOC", "FECHA", default=_pick_row_value(header_row, "FECHA_CONT", "FECHA_DOC"))),
                "monto_doc": _to_float(_pick_row_value(raw_row, "MONTO_DOC", "TOTAL_DOC", "MONTO", "CUOTA", default=balance_doc or pago_abono)),
                "comentario_factura": _pick_row_text(raw_row, "COMENTARIO_FACTURA", "COMENTARIO", "OBSERVACION"),
                "no_cuota": no_cuota,
                "cuota": cuota if cuota > 0 else balance_doc or pago_abono,
                "balance_doc": balance_doc or pago_abono,
                "balance_total_factura": _to_float(
                    balance_total_por_doc.get(
                        no_doc,
                        _pick_row_value(raw_row, "BALANCE_TOTAL_FACTURA", "SALDO_FACTURA", "TOTAL_DOC", default=balance_doc or pago_abono),
                    )
                ),
                "fecha_venc": _fmt_date(fecha_venc_value),
                "venc": "*" if dias > 0 else "",
                "dias": dias,
                "cargo": _to_float(_pick_row_value(raw_row, "CARGO", "MORA", "TOTAL_MORA", default=0)),
                "porc_desc": _to_float(_pick_row_value(raw_row, "PORC_DESC", "PORCENTAJE_DESC", "PCT_DESC", default=0)),
                "desc_avance": desc_avance_value,
                "pago_abono": pago_abono,
                "balance_pend": balance_pend_with_discount,
                "total_ret": _to_float(_pick_row_value(raw_row, "TOTAL_RET", "RETENCION", "RET", default=0)),
                "selected": True,
                "tiene_financiamiento": is_financed_line,
            }
        )

    total_doc_detail = sum(_to_float(item.get("pago_abono")) for item in detalle)
    total_mora_detail = sum(_to_float(item.get("cargo")) for item in detalle)
    desc_avance_detail = sum(_to_float(item.get("desc_avance")) for item in detalle)
    total_ret_detail = sum(_to_float(item.get("total_ret")) for item in detalle)

    total_doc_summary = total_doc_detail if detalle else total_recibo_header
    total_mora_summary = max(total_mora_detail, total_mora_header) if detalle else total_mora_header
    desc_avance_summary = max(desc_avance_detail, desc_avance_header) if detalle else desc_avance_header
    total_ret_summary = max(total_ret_detail, total_ret_header) if detalle else total_ret_header
    cash_total_formula = (
        max(total_doc_summary + total_mora_summary - total_ret_summary, 0)
        if total_doc_summary > 0.0001
        else 0
    )
    total_pago_summary = efectivo + transferencia
    total_recibo_summary = total_recibo_header
    if total_recibo_summary <= 0.0001:
        total_recibo_summary = total_pago_summary or cash_total_formula or total_doc_summary
    monto_pagar_summary = total_pago_summary or cash_total_formula or total_recibo_summary

    return {
        "header": {
            "recibo_id": _stringify_doc(_pick_row_value(header_row, "ID_RECIBO", "NO_RECIBO", "ID_DOC")),
            "no": _stringify_doc(_pick_row_value(header_row, "NO_RECIBO", "ID_RECIBO", "ID_DOC")),
            "estado": _pick_row_text(header_row, "ESTATUS", "EST_DOC", "ESTADO") or "Abierto",
            "fecha_cont": _fmt_date_input(_pick_row_value(header_row, "FECHA_CONT")),
            "fecha_venc": _fmt_date_input(_pick_row_value(header_row, "FECHA_VENC")),
            "fecha_aplic": _fmt_date_input(_pick_row_value(header_row, "FECHA_DOC")),
            "cliente": _pick_row_text(header_row, "ID_SN", "CLIENTE", "COD_CLIENTE"),
            "nombre": maestro_sn.get("nombre") or _pick_row_text(header_row, "NOM_SOCIO", "NOMBRE", "NOM_CLIENTE"),
            "apodo": maestro_sn.get("apodo") or _pick_row_text(header_row, "CONTACTO", "APODO"),
            "direccion": maestro_sn.get("direccion", ""),
            "sector": maestro_sn.get("sector", ""),
            "proyecto": proyecto,
            "moneda": moneda or "RD$",
            "moneda_pago": moneda or "RD$",
            "tasa_pago": _to_float(_pick_row_value(header_row, "TASA_PAGO", "TASA", default=1.0)),
            "metodo": metodo,
            "cuenta_caja": cuenta_caja,
            "cuenta_caja_desc": cuenta_caja_desc,
            "cuenta_efectivo": cuenta_efectivo,
            "cuenta_efectivo_desc": cuenta_efectivo_desc,
            "cuenta_transferencia": cuenta_transferencia,
            "cuenta_transferencia_desc": cuenta_transferencia_desc,
            "no_transferencia": no_transferencia,
            "cuenta_cliente_pago": cuenta_cliente_pago,
            "cuenta_desc_ret": cuenta_desc_ret,
            "cuenta_desc_ret_desc": cuenta_desc_ret_desc,
            "comentario": comentario,
            "rnc_ced": maestro_sn.get("rnc_ced") or _pick_row_text(header_row, "RNC_CED", "RNC", "CEDULA"),
            "impreso": _pick_row_text(header_row, "IMPRESO", default="N"),
            "terminal": _pick_row_text(header_row, "TERMINAL"),
            "total_letra": _pick_row_text(header_row, "TOTAL_LETRA", default=_amount_to_spanish_words(total_recibo_summary)),
            "medio_pago": efectivo > 0.0001 and transferencia > 0.0001,
            "efectivo": efectivo,
            "transferencia": transferencia,
            "fecha_pago": _fmt_date_input(_pick_row_value(header_row, "FECHA_PAGO", "FECHA_CONT", "F_CONT")),
            "usuario_id": _pick_row_text(header_row, "ID_USUARIO", "USUARIO_ID"),
            "usuario_nombre": _pick_row_text(header_row, "USUARIO_NOMBRE", "NOMBRE_USUARIO", "USUARIO"),
        },
        "summary": {
            "total_mora": total_mora_summary,
            "desc_avance": desc_avance_summary,
            "total_doc": total_doc_summary,
            "total_ret": total_ret_summary,
            "total_pago": total_pago_summary,
            "monto_pagar": monto_pagar_summary,
            "total_recibo": total_recibo_summary,
        },
        "detail": detalle,
    }


def _load_cxc_recibo_detalle(recibo_id):
    if not recibo_id:
        return None

    cab_columns = _load_table_columns("CAB_RECIBO_INGRESO")
    det_columns = _load_table_columns("DET_RECIBO_INGRESO")
    if not cab_columns or not det_columns:
        return None

    cab_key_col = _pick_existing_column(cab_columns, "ID_RECIBO", "NO_RECIBO", "ID_DOC", "NO_DOC")
    cab_no_recibo_col = _pick_existing_column(cab_columns, "NO_RECIBO", "ID_RECIBO", "ID_DOC", "NO_DOC")
    det_key_col = _pick_existing_column(det_columns, "ID_RECIBO", "NO_RECIBO", "ID_DOC", "NO_DOC")
    det_no_recibo_col = _pick_existing_column(det_columns, "NO_RECIBO", "ID_RECIBO", "ID_DOC", "NO_DOC")
    if not cab_key_col or not det_key_col:
        return None

    with connection.cursor() as cursor:
        header_where_parts = [f"CAST([{cab_key_col}] AS NVARCHAR(255)) = %s"]
        header_where_params = [recibo_id]
        if cab_no_recibo_col and cab_no_recibo_col != cab_key_col:
            header_where_parts.append(f"CAST([{cab_no_recibo_col}] AS NVARCHAR(255)) = %s")
            header_where_params.append(recibo_id)
        cursor.execute(
            f"""
            SELECT TOP 1 *
            FROM CAB_RECIBO_INGRESO
            WHERE {" OR ".join(f"({part})" for part in header_where_parts)}
            """,
            header_where_params,
        )
        raw_header = cursor.fetchone()
        if not raw_header:
            return None
        header_columns = [col[0] for col in cursor.description]
        header_row = _normalize_result_row(header_columns, raw_header)

    recibo_id_real = _stringify_doc(_pick_row_value(header_row, cab_key_col, cab_no_recibo_col))
    no_recibo = _stringify_doc(_pick_row_value(header_row, cab_no_recibo_col, cab_key_col))

    order_columns = _unique_columns(
        _pick_existing_column(det_columns, "NO_DOC", "ID_DOC"),
        _pick_existing_column(det_columns, "NO_CUOTA", "CUOTA_NUM", "NUM_CUOTA"),
        _pick_existing_column(det_columns, "FECHA_CONT", "F_CONT", "FECHA"),
    )
    detail_where_parts = [f"CAST([{det_key_col}] AS NVARCHAR(255)) = %s"]
    detail_where_params = [recibo_id_real or recibo_id]
    if det_no_recibo_col and det_no_recibo_col != det_key_col and no_recibo:
        detail_where_parts.append(f"CAST([{det_no_recibo_col}] AS NVARCHAR(255)) = %s")
        detail_where_params.append(no_recibo)
    elif det_no_recibo_col and no_recibo and no_recibo != (recibo_id_real or recibo_id):
        detail_where_parts.append(f"CAST([{det_no_recibo_col}] AS NVARCHAR(255)) = %s")
        detail_where_params.append(no_recibo)

    detail_sql = f"SELECT * FROM DET_RECIBO_INGRESO WHERE {' OR '.join(f'({part})' for part in detail_where_parts)}"
    if order_columns:
        detail_sql += " ORDER BY " + ", ".join(f"[{column}]" for column in order_columns)

    with connection.cursor() as cursor:
        cursor.execute(detail_sql, detail_where_params)
        detail_columns = [col[0] for col in cursor.description]
        detail_rows = [_normalize_result_row(detail_columns, raw_row) for raw_row in cursor.fetchall()]

    return _build_cxc_recibo_payload(header_row, detail_rows)


def _build_cxc_recibo_print_payload(record, auth_payload):
    record = record or {}
    header = record.get("header") or {}
    summary = record.get("summary") or {}
    detail = record.get("detail") or []
    empresa = _get_empresa_data() or {}
    total_recibo = _to_float(
        summary.get("total_recibo")
        or summary.get("total_pago")
        or summary.get("monto_pagar")
        or summary.get("total_doc")
    )
    balance_rd = _to_float(_get_open_ed_balance(header.get("cliente")))
    metodo_lineas = []
    if _to_float(header.get("efectivo")) > 0:
        metodo_lineas.append({"label": "Efectivo", "amount": _to_float(header.get("efectivo"))})
    if _to_float(header.get("transferencia")) > 0:
        metodo_lineas.append({"label": "Transferencia", "amount": _to_float(header.get("transferencia"))})
    if not metodo_lineas:
        metodo_lineas.append({"label": header.get("metodo") or "Pago", "amount": total_recibo})

    comentario = str(header.get("comentario") or "").strip()
    if not comentario and detail:
        comentario = _build_cxc_facturas_comment(
            detail,
            close_account=str(header.get("metodo") or "").strip().upper() == "CIERRE DE CUENTA",
        )

    usuario_original_id = str(header.get("usuario_id") or "").strip()
    usuario_original_nombre = str(header.get("usuario_nombre") or "").strip()
    usuario_meta = _load_usuario_meta(usuario_original_id or usuario_original_nombre)
    if not usuario_original_id:
        usuario_original_id = str(usuario_meta.get("id") or "").strip()
    if not usuario_original_nombre:
        usuario_original_nombre = str(usuario_meta.get("nombre") or "").strip()
    usuario_impresion_id = usuario_original_id or str((auth_payload or {}).get("usuario_id") or "").strip()
    usuario_impresion_nombre = (
        usuario_original_nombre
        or str((auth_payload or {}).get("usuario_nombre") or "").strip()
    )

    return {
        "empresa": {
            "nombre": empresa.get("nombre", ""),
            "direccion": empresa.get("direccion", ""),
            "tel1": empresa.get("tel1", ""),
            "tel2": empresa.get("tel2", ""),
            "email": empresa.get("email", ""),
            "rnc": empresa.get("rnc", ""),
            "logo_b64": empresa.get("logo_b64", ""),
            "logo_tipo": empresa.get("logo_tipo", ""),
        },
        "cliente": {
            "codigo": header.get("cliente", ""),
            "nombre": header.get("nombre", ""),
            "rnc_ced": header.get("rnc_ced", ""),
            "sector": header.get("sector", ""),
            "direccion": header.get("direccion", ""),
        },
        "documento": {
            "no_recibo": header.get("no", ""),
            "fecha_cont": _fmt_date_flexible(header.get("fecha_cont")),
            "fecha_venc": _fmt_date_flexible(header.get("fecha_venc")),
            "fecha_aplic": _fmt_date_flexible(header.get("fecha_aplic")),
            "moneda_doc": header.get("moneda", "RD$"),
            "moneda_pago": header.get("moneda_pago", header.get("moneda", "RD$")),
            "tasa_pago": _to_float(header.get("tasa_pago", 1.0)),
            "suma_letras": header.get("total_letra") or _amount_to_spanish_words(total_recibo),
            "monto_rd": total_recibo,
            "comentario": comentario,
            "estado": header.get("estado", ""),
        },
        "detalle": [
            {
                "td": item.get("td", ""),
                "no_factura": item.get("no_doc", ""),
                "no_cuota": item.get("no_cuota", ""),
                "fecha_cont": _fmt_date_flexible(item.get("fecha_cont")),
                "fecha_venc": _fmt_date_flexible(item.get("fecha_venc")),
                "balance_fact": _to_float(item.get("balance_doc")),
                "balance_total_factura": _to_float(item.get("balance_total_factura")),
                "mora": _to_float(item.get("cargo")),
                "descuento": _to_float(item.get("desc_avance")),
                "monto_aplicado": _to_float(item.get("pago_abono")),
                "balance_pendiente": _to_float(item.get("balance_pend")),
            }
            for item in detail
        ],
        "metodos_pago": metodo_lineas,
        "balance_rd": balance_rd,
        "total_recibo": total_recibo,
        "usuario_nombre": usuario_impresion_nombre,
        "usuario_firma_b64": _load_firma_b64(usuario_impresion_id),
        "impreso_por_nombre": str((auth_payload or {}).get("usuario_nombre") or "").strip(),
        "impreso_fecha": timezone.localdate().strftime("%d/%m/%Y"),
        "impreso_hora": timezone.localtime().strftime("%I:%M:%S %p").lstrip("0").lower(),
    }


def _build_image_src(base64_value, image_type):
    if not base64_value:
        return ""
    normalized_type = str(image_type or "").strip().lower()
    if normalized_type.startswith("image/"):
        return f"data:{normalized_type};base64,{base64_value}"
    if normalized_type:
        return f"data:image/{normalized_type};base64,{base64_value}"
    return f"data:image/png;base64,{base64_value}"


def _paginate_cxc_receipt_pages(formatted_detail, first_capacity=11, summary_capacity=9):
    detail_rows = list(formatted_detail or [])
    if not detail_rows:
        return [{"detalle": [], "continued": False, "show_summary": True}]

    if len(detail_rows) <= summary_capacity:
        return [{"detalle": detail_rows, "continued": False, "show_summary": True}]

    if len(detail_rows) <= first_capacity:
        return [{"detalle": detail_rows, "continued": False, "show_summary": False}]

    pages = []
    first_page_rows = detail_rows[:first_capacity]
    remaining_rows = detail_rows[first_capacity:]

    pages.append(
        {
            "detalle": first_page_rows,
            "continued": False,
            "show_summary": False,
        }
    )

    if len(remaining_rows) <= summary_capacity:
        pages.append(
            {
                "detalle": remaining_rows,
                "continued": True,
                "show_summary": True,
            }
        )
        return pages

    head_rows = remaining_rows[:-summary_capacity]
    tail_rows = remaining_rows[-summary_capacity:]
    for start in range(0, len(head_rows), first_capacity):
        pages.append(
            {
                "detalle": head_rows[start:start + first_capacity],
                "continued": True,
                "show_summary": False,
            }
        )
    pages.append(
        {
            "detalle": tail_rows,
            "continued": True,
            "show_summary": True,
        }
    )
    return [page for page in pages if page.get("detalle") or page.get("show_summary")]


def _build_cxc_receipt_template_context(print_data, copies=1):
    print_data = print_data or {}
    empresa = dict(print_data.get("empresa") or {})
    cliente = dict(print_data.get("cliente") or {})
    documento = dict(print_data.get("documento") or {})
    detalle = list(print_data.get("detalle") or [])
    metodos_pago = list(print_data.get("metodos_pago") or [])
    normalized_copies = max(1, min(int(copies or 1), 20))

    empresa["logo_src"] = _build_image_src(empresa.get("logo_b64"), empresa.get("logo_tipo"))
    usuario_firma_src = _build_image_src(print_data.get("usuario_firma_b64"), "")
    documento["monto_rd_fmt"] = _pdf_money(documento.get("monto_rd"))
    documento["tasa_pago_fmt"] = _pdf_money(documento.get("tasa_pago") or 1)
    is_cancelled = str(documento.get("estado") or "").strip().upper() == "CANCELADO"

    formatted_detail = []
    balance_facturas = {}
    for item in detalle:
        no_factura = str(item.get("no_factura") or "").strip()
        balance_total_factura = _to_float(item.get("balance_total_factura"))
        if no_factura:
            balance_facturas[no_factura] = max(balance_facturas.get(no_factura, 0.0), balance_total_factura)
        formatted_detail.append(
            {
                **item,
                "balance_fact_fmt": _pdf_money(item.get("balance_fact")),
                "mora_fmt": _pdf_money(item.get("mora")),
                "descuento_fmt": _pdf_money(item.get("descuento")),
                "monto_aplicado_fmt": _pdf_money(item.get("monto_aplicado")),
                "balance_pendiente_fmt": _pdf_money(item.get("balance_pendiente")),
            }
        )

    payment_lines = [
        {
            **item,
            "amount_fmt": _pdf_money(item.get("amount")),
        }
        for item in metodos_pago
    ]
    if not payment_lines:
        payment_lines = [{"label": "Pago", "amount_fmt": _pdf_money(print_data.get("total_recibo"))}]

    balance_facturas_lines = [
        {"no_factura": no_factura, "balance_fmt": _pdf_money(balance_value)}
        for no_factura, balance_value in balance_facturas.items()
    ]

    paged_detail = _paginate_cxc_receipt_pages(formatted_detail)
    page_instances = []
    total_pages = len(paged_detail) or 1
    for copy_index in range(normalized_copies):
        for page_index, page in enumerate(paged_detail or [{"detalle": [], "continued": False, "show_summary": True}]):
            page_instances.append(
                {
                    **page,
                    "page_no": page_index + 1,
                    "total_pages": total_pages,
                    "is_last_output": copy_index == normalized_copies - 1 and page_index == total_pages - 1,
                }
            )

    return {
        "empresa": empresa,
        "cliente": cliente,
        "documento": documento,
        "metodos_pago": payment_lines,
        "balance_facturas": balance_facturas_lines,
        "balance_rd_fmt": _pdf_money(print_data.get("balance_rd")),
        "total_recibo_fmt": _pdf_money(print_data.get("total_recibo")),
        "usuario_nombre": str(print_data.get("usuario_nombre") or "").strip(),
        "impreso_por_nombre": str(print_data.get("impreso_por_nombre") or "").strip(),
        "usuario_firma_src": usuario_firma_src,
        "formato_impresion": get_print_format("recibo_pago"),
        "impreso_fecha": str(print_data.get("impreso_fecha") or "").strip(),
        "impreso_hora": str(print_data.get("impreso_hora") or "").strip(),
        "is_cancelled": is_cancelled,
        "page_instances": page_instances,
    }


def _get_headless_browser_path():
    candidates = [
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _render_cxc_receipt_pdf_via_browser(print_data, copies=1):
    browser_path = _get_headless_browser_path()
    if not browser_path:
        raise RuntimeError("No hay un navegador compatible instalado para generar PDF.")

    base_dir = Path(__file__).resolve().parents[1]
    temp_root = base_dir / "media" / "tmp" / "cxc_pdf"
    temp_root.mkdir(parents=True, exist_ok=True)
    job_id = uuid.uuid4().hex
    html_path = temp_root / f"{job_id}.html"
    pdf_path = temp_root / f"{job_id}.pdf"
    user_data_dir = temp_root / f"profile-{job_id}"
    user_data_dir.mkdir(parents=True, exist_ok=True)

    try:
        html_content = render_to_string(
            "caja/cxc_recibo_pdf.html",
            _build_cxc_receipt_template_context(print_data, copies=copies),
        )
        html_path.write_text(html_content, encoding="utf-8")

        command = [
            str(browser_path),
            "--headless",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--allow-file-access-from-files",
            "--run-all-compositor-stages-before-draw",
            "--virtual-time-budget=4000",
            f"--user-data-dir={user_data_dir}",
            "--print-to-pdf-no-header",
            f"--print-to-pdf={pdf_path}",
            html_path.as_uri(),
        ]
        subprocess.run(command, check=True, capture_output=True, timeout=45)
        if not pdf_path.exists():
            raise RuntimeError("El navegador no genero el archivo PDF.")
        return pdf_path.read_bytes()
    finally:
        for path in (html_path, pdf_path):
            try:
                if path.exists():
                    path.unlink()
            except Exception:
                pass
        try:
            if user_data_dir.exists():
                for child in sorted(user_data_dir.rglob("*"), reverse=True):
                    try:
                        if child.is_file():
                            child.unlink()
                        else:
                            child.rmdir()
                    except Exception:
                        pass
                user_data_dir.rmdir()
        except Exception:
            pass


def _pdf_mm_to_pt(value):
    return _to_float(value) * 72.0 / 25.4


def _pdf_format_num(value):
    text = f"{_to_float(value):.3f}"
    return text.rstrip("0").rstrip(".") or "0"


def _pdf_escape_text(value):
    text = str(value or "").replace("\r", " ").replace("\n", " ")
    text = text.encode("latin-1", "replace").decode("latin-1")
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _pdf_money(value):
    return f"{_to_float(value):,.2f}"


def _pdf_text_width(text, font_size, mono=False):
    factor = 0.6 if mono else 0.52
    return len(str(text or "")) * _to_float(font_size) * factor


def _pdf_truncate_text(text, max_width, font_size, mono=False):
    safe_text = str(text or "").strip()
    if not safe_text:
        return ""
    if _pdf_text_width(safe_text, font_size, mono=mono) <= max_width:
        return safe_text
    ellipsis = "..."
    current = safe_text
    while current and _pdf_text_width(current + ellipsis, font_size, mono=mono) > max_width:
        current = current[:-1]
    return (current + ellipsis).strip() if current else ellipsis


def _pdf_wrap_text(text, max_width, font_size, mono=False, max_lines=None):
    source = str(text or "").strip()
    if not source:
        return []
    words = source.replace("\r", " ").replace("\n", " ").split()
    if not words:
        return []
    lines = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and _pdf_text_width(candidate, font_size, mono=mono) > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    if max_lines and len(lines) > max_lines:
        trimmed = lines[:max_lines]
        trimmed[-1] = _pdf_truncate_text(trimmed[-1], max_width, font_size, mono=mono)
        return trimmed
    return lines


def _pdf_text_ops(x, y, text, *, font="F1", size=9, color=None):
    safe_text = _pdf_escape_text(text)
    color_ops = ""
    if color:
        color_ops = f"{_pdf_format_num(color[0])} {_pdf_format_num(color[1])} {_pdf_format_num(color[2])} rg\n"
    return (
        f"{color_ops}BT\n"
        f"/{font} {_pdf_format_num(size)} Tf\n"
        f"1 0 0 1 {_pdf_format_num(x)} {_pdf_format_num(y)} Tm\n"
        f"({safe_text}) Tj\n"
        "ET\n"
    )


def _pdf_line_ops(x1, y1, x2, y2, *, line_width=0.5, stroke_gray=0):
    return (
        f"{_pdf_format_num(stroke_gray)} G\n"
        f"{_pdf_format_num(line_width)} w\n"
        f"{_pdf_format_num(x1)} {_pdf_format_num(y1)} m\n"
        f"{_pdf_format_num(x2)} {_pdf_format_num(y2)} l\n"
        "S\n"
    )


def _pdf_rect_ops(x, y, width, height, *, line_width=0.5, stroke_gray=0):
    return (
        f"{_pdf_format_num(stroke_gray)} G\n"
        f"{_pdf_format_num(line_width)} w\n"
        f"{_pdf_format_num(x)} {_pdf_format_num(y)} {_pdf_format_num(width)} {_pdf_format_num(height)} re\n"
        "S\n"
    )


def _pdf_text_aligned(ops, x, y, width, text, *, font="F1", size=9, align="left", mono=False, color=None):
    safe_text = str(text or "")
    text_width = _pdf_text_width(safe_text, size, mono=mono)
    draw_x = x
    if align == "right":
        draw_x = x + max(width - text_width, 0)
    elif align == "center":
        draw_x = x + max((width - text_width) / 2.0, 0)
    ops.append(_pdf_text_ops(draw_x, y, safe_text, font=font, size=size, color=color))


def _pdf_render_receipt_page_ops(print_data):
    empresa = (print_data or {}).get("empresa") or {}
    cliente = (print_data or {}).get("cliente") or {}
    documento = (print_data or {}).get("documento") or {}
    detalle = list((print_data or {}).get("detalle") or [])
    metodos_pago = list((print_data or {}).get("metodos_pago") or [])
    balance_total = _to_float((print_data or {}).get("balance_rd"))
    total_recibo = _to_float((print_data or {}).get("total_recibo"))
    usuario_nombre = str((print_data or {}).get("usuario_nombre") or "").strip()
    impreso_por_nombre = str((print_data or {}).get("impreso_por_nombre") or "").strip()
    impreso_fecha = str((print_data or {}).get("impreso_fecha") or "").strip()
    impreso_hora = str((print_data or {}).get("impreso_hora") or "").strip()

    page_width = _pdf_mm_to_pt(210)
    page_height = _pdf_mm_to_pt(148.5)
    left = _pdf_mm_to_pt(6)
    top = lambda mm: page_height - _pdf_mm_to_pt(mm)

    ops = []

    company_name = str(empresa.get("nombre") or "").strip()
    company_line = str(empresa.get("direccion") or "").strip()
    company_contact = "Telefono: " + "  ".join(filter(None, [empresa.get("tel1"), empresa.get("tel2")])).strip()
    if empresa.get("email"):
        company_contact += f"   E-Mail: {empresa.get('email')}"
    rnc_line = f"RNC: {empresa.get('rnc') or ''}".strip()
    sector = str(cliente.get("sector") or "").strip()
    cancelled = str(documento.get("estado") or "").strip().upper() == "CANCELADO"

    ops.append(_pdf_text_ops(left, top(8), company_name.upper(), font="F2", size=13, color=(0.0, 0.11, 0.61)))
    ops.append(_pdf_text_ops(left, top(13), company_line, font="F1", size=7.5))
    ops.append(_pdf_text_ops(left, top(17), company_contact, font="F1", size=7))
    ops.append(_pdf_text_ops(left, top(21), rnc_line, font="F2", size=7.8))
    if sector:
        _pdf_text_aligned(
            ops,
            _pdf_mm_to_pt(145),
            top(18),
            _pdf_mm_to_pt(58),
            sector.upper(),
            font="F2",
            size=8,
            align="right",
            color=(0.8, 0.0, 0.0),
        )
    if cancelled:
        _pdf_text_aligned(
            ops,
            _pdf_mm_to_pt(145),
            top(24),
            _pdf_mm_to_pt(58),
            "CANCELADO",
            font="F2",
            size=12,
            align="right",
            color=(0.76, 0.07, 0.07),
        )

    _pdf_text_aligned(
        ops,
        left,
        top(30),
        _pdf_mm_to_pt(198),
        "RECIBO DE INGRESO",
        font="F2",
        size=12,
        align="center",
    )

    left_info_x = left
    right_info_x = _pdf_mm_to_pt(150)
    left_info_y = top(38)
    info_step = _pdf_mm_to_pt(4.4)

    info_rows = [
        f"Hemos recibido de: {cliente.get('nombre') or ''}",
        f"Codigo: {cliente.get('codigo') or ''}",
        f"RNC/Ced.: {cliente.get('rnc_ced') or ''}",
    ]
    for idx, line in enumerate(info_rows):
        ops.append(_pdf_text_ops(left_info_x, left_info_y - (info_step * idx), line, font="F1", size=7.8))

    suma_lines = _pdf_wrap_text(
        f"La suma de: {documento.get('suma_letras') or ''}",
        _pdf_mm_to_pt(112),
        7.2,
        max_lines=2,
    ) or ["La suma de:"]
    suma_y = left_info_y - (info_step * len(info_rows))
    for idx, line in enumerate(suma_lines):
        ops.append(_pdf_text_ops(left_info_x, suma_y - (_pdf_mm_to_pt(3.7) * idx), line, font="F1", size=7.2))

    moneda_y = suma_y - (_pdf_mm_to_pt(3.9) * max(len(suma_lines), 1))
    ops.append(_pdf_text_ops(left_info_x, moneda_y, f"Moneda de pago: {documento.get('moneda_pago') or 'RD$'}", font="F1", size=7.6))
    ops.append(_pdf_text_ops(left_info_x, moneda_y - info_step, f"Monto en RD$: {_pdf_money(documento.get('monto_rd'))}", font="F2", size=7.8))

    right_rows = [
        ("No. Recibo", documento.get("no_recibo") or ""),
        ("Fecha cont.", documento.get("fecha_cont") or ""),
        ("Moneda Doc", documento.get("moneda_doc") or "RD$"),
        ("Tasa pago", _pdf_money(documento.get("tasa_pago") or 1)),
    ]
    for idx, (label, value) in enumerate(right_rows):
        ops.append(_pdf_text_ops(right_info_x, left_info_y - (info_step * idx), f"{label}: {value}", font="F1", size=7.6))

    table_left = left
    table_top_mm = 50
    table_top_y = top(table_top_mm)
    table_widths_mm = [9, 25, 12, 18, 18, 20, 12, 18, 28, 34]
    table_widths = [_pdf_mm_to_pt(value) for value in table_widths_mm]
    headers = [
        "TDN.",
        "Factura No.",
        "Cuota",
        "Fecha cont.",
        "Fecha venc",
        "Balance fact",
        "Mora",
        "Descuento",
        "Monto aplicado",
        "Bce. pendiente",
    ]
    header_height = _pdf_mm_to_pt(4.6)
    table_total_width = sum(table_widths)
    ops.append(_pdf_rect_ops(table_left, table_top_y - header_height, table_total_width, header_height))
    current_x = table_left
    for idx, header in enumerate(headers):
        width = table_widths[idx]
        _pdf_text_aligned(
            ops,
            current_x + 2,
            table_top_y - header_height + 4,
            max(width - 4, 0),
            header,
            font="F2",
            size=6.0,
            align="left",
        )
        current_x += width

    rows_available_mm = 50
    row_count = max(len(detalle), 1)
    row_height_mm = max(2.25, min(3.4, rows_available_mm / row_count))
    row_height = _pdf_mm_to_pt(row_height_mm)
    row_font_size = max(4.8, min(6.5, row_height_mm * 2.0))
    amount_font_size = max(4.7, min(6.2, row_font_size))
    row_top_y = table_top_y - header_height

    numeric_keys = {"balance_fact", "mora", "descuento", "monto_aplicado", "balance_pendiente"}
    detail_rows = detalle or [{}]
    for row_index, item in enumerate(detail_rows):
        next_y = row_top_y - (row_height * (row_index + 1))
        ops.append(_pdf_line_ops(table_left, next_y, table_left + table_total_width, next_y, line_width=0.25, stroke_gray=0.7))
        row_values = [
            str(item.get("td") or ""),
            str(item.get("no_factura") or ""),
            str(item.get("no_cuota") or ""),
            str(item.get("fecha_cont") or ""),
            str(item.get("fecha_venc") or ""),
            _pdf_money(item.get("balance_fact")),
            _pdf_money(item.get("mora")),
            _pdf_money(item.get("descuento")),
            _pdf_money(item.get("monto_aplicado")),
            _pdf_money(item.get("balance_pendiente")),
        ]
        current_x = table_left
        for col_index, value in enumerate(row_values):
            width = table_widths[col_index]
            is_numeric = col_index >= 5
            display_value = _pdf_truncate_text(value, max(width - 4, 0), amount_font_size if is_numeric else row_font_size, mono=is_numeric)
            _pdf_text_aligned(
                ops,
                current_x + 2,
                next_y + (row_height * 0.32),
                max(width - 4, 0),
                display_value,
                font="F3" if is_numeric else "F1",
                size=amount_font_size if is_numeric else row_font_size,
                align="right" if is_numeric else ("center" if col_index == 2 else "left"),
                mono=is_numeric,
            )
            current_x += width

    comment_box_top = top(106)
    comment_box_height = _pdf_mm_to_pt(11.5)
    comment_box_width = _pdf_mm_to_pt(132)
    payment_box_x = _pdf_mm_to_pt(147)
    payment_box_width = _pdf_mm_to_pt(56)
    ops.append(_pdf_rect_ops(left, comment_box_top - comment_box_height, comment_box_width, comment_box_height))
    ops.append(_pdf_text_ops(left + 3, comment_box_top - 4, "Comentario:", font="F2", size=7.2))
    comment_lines = _pdf_wrap_text(documento.get("comentario") or "", comment_box_width - 8, 6.8, max_lines=2)
    for idx, line in enumerate(comment_lines[:2]):
        ops.append(_pdf_text_ops(left + 3, comment_box_top - 8 - (_pdf_mm_to_pt(3.4) * idx), line, font="F1", size=6.8))

    ops.append(_pdf_rect_ops(payment_box_x, comment_box_top - comment_box_height, payment_box_width, comment_box_height))
    payment_lines = metodos_pago or [{"label": "Pago", "amount": total_recibo}]
    for idx, item in enumerate(payment_lines[:3]):
        payment_y = comment_box_top - 4 - (_pdf_mm_to_pt(3.6) * idx)
        label = f"{item.get('label') or 'Pago'} ->"
        _pdf_text_aligned(ops, payment_box_x + 3, payment_y, payment_box_width - 6, label, font="F1", size=7.0, align="left")
        _pdf_text_aligned(
            ops,
            payment_box_x + 3,
            payment_y,
            payment_box_width - 6,
            _pdf_money(item.get("amount")),
            font="F3",
            size=7.0,
            align="right",
            mono=True,
        )

    balance_map = {}
    for item in detalle:
        no_factura = str(item.get("no_factura") or "").strip()
        if not no_factura:
            continue
        current = balance_map.get(no_factura, 0.0)
        balance_map[no_factura] = max(current, _to_float(item.get("balance_total_factura")))

    paid_box_top = top(121)
    paid_box_height = _pdf_mm_to_pt(12)
    paid_box_width = _pdf_mm_to_pt(120)
    ops.append(_pdf_rect_ops(left, paid_box_top - paid_box_height, paid_box_width, paid_box_height))
    ops.append(_pdf_text_ops(left + 3, paid_box_top - 4, "Balance de Fact. pagadas", font="F2", size=7.2))
    paid_lines = list(balance_map.items())[:3]
    if not paid_lines:
        ops.append(_pdf_text_ops(left + 3, paid_box_top - 8, "Sin facturas aplicadas.", font="F1", size=6.6))
    else:
        for idx, (no_factura, balance_value) in enumerate(paid_lines):
            line_y = paid_box_top - 8 - (_pdf_mm_to_pt(3.2) * idx)
            _pdf_text_aligned(ops, left + 3, line_y, paid_box_width - 6, f"#{no_factura}", font="F1", size=6.6, align="left")
            _pdf_text_aligned(
                ops,
                left + 3,
                line_y,
                paid_box_width - 6,
                f"RD$ {_pdf_money(balance_value)}",
                font="F3",
                size=6.6,
                align="right",
                mono=True,
            )

    ops.append(_pdf_text_ops(left, top(135), f"Balance de cuentas: RD$ {_pdf_money(balance_total)}", font="F2", size=8.2))
    _pdf_text_aligned(
        ops,
        _pdf_mm_to_pt(150),
        top(126),
        _pdf_mm_to_pt(52),
        "Total Recibo -> RD$",
        font="F2",
        size=8.0,
        align="left",
    )
    _pdf_text_aligned(
        ops,
        _pdf_mm_to_pt(150),
        top(132),
        _pdf_mm_to_pt(52),
        _pdf_money(total_recibo),
        font="F3",
        size=9.0,
        align="right",
        mono=True,
    )

    signature_y = top(139)
    signature_width = _pdf_mm_to_pt(38)
    left_sig_x = _pdf_mm_to_pt(30)
    right_sig_x = _pdf_mm_to_pt(128)
    ops.append(_pdf_line_ops(left_sig_x, signature_y, left_sig_x + signature_width, signature_y))
    ops.append(_pdf_line_ops(right_sig_x, signature_y, right_sig_x + signature_width, signature_y))
    _pdf_text_aligned(ops, left_sig_x - 10, signature_y - 10, signature_width + 20, usuario_nombre or " ", font="F1", size=6.8, align="center")
    _pdf_text_aligned(ops, left_sig_x - 10, signature_y - 16, signature_width + 20, "Realizado por", font="F1", size=6.8, align="center")
    _pdf_text_aligned(ops, right_sig_x - 10, signature_y - 16, signature_width + 20, "Recibido por", font="F1", size=6.8, align="center")

    footer_y = top(145)
    ops.append(_pdf_line_ops(left, footer_y + 4, left + table_total_width, footer_y + 4, line_width=0.4))
    ops.append(_pdf_text_ops(left, footer_y, "Page 1 of 1", font="F1", size=6.2))
    _pdf_text_aligned(
        ops,
        left,
        footer_y,
        table_total_width,
        f"Impreso por: {impreso_por_nombre}",
        font="F1",
        size=6.2,
        align="center",
    )
    _pdf_text_aligned(
        ops,
        left,
        footer_y,
        table_total_width,
        f"{impreso_fecha} {impreso_hora}".strip(),
        font="F1",
        size=6.2,
        align="right",
    )

    return "".join(ops).encode("latin-1", "replace")


def _build_pdf_document(page_width, page_height, page_streams):
    page_streams = list(page_streams or [])
    if not page_streams:
        page_streams = [b""]

    objects = []

    def add_object(data):
        objects.append(data if isinstance(data, bytes) else str(data).encode("latin-1", "replace"))
        return len(objects)

    catalog_id = add_object("<< /Type /Catalog /Pages 2 0 R >>")
    pages_id = add_object(b"")
    font_regular_id = add_object("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    font_bold_id = add_object("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")
    font_mono_id = add_object("<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>")

    page_ids = []
    for stream in page_streams:
        content_id = add_object(
            (
                f"<< /Length {len(stream)} >>\nstream\n".encode("latin-1")
                + stream
                + b"\nendstream"
            )
        )
        page_id = add_object(
            (
                "<< /Type /Page /Parent 2 0 R "
                f"/MediaBox [0 0 {_pdf_format_num(page_width)} {_pdf_format_num(page_height)}] "
                "/Resources << /Font << "
                f"/F1 {font_regular_id} 0 R /F2 {font_bold_id} 0 R /F3 {font_mono_id} 0 R"
                " >> >> "
                f"/Contents {content_id} 0 R >>"
            )
        )
        page_ids.append(page_id)

    objects[pages_id - 1] = (
        f"<< /Type /Pages /Count {len(page_ids)} /Kids [{' '.join(f'{page_id} 0 R' for page_id in page_ids)}] >>"
    ).encode("latin-1")

    chunks = [b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"]
    offsets = [0]
    current_offset = len(chunks[0])

    for object_id, data in enumerate(objects, start=1):
        offsets.append(current_offset)
        chunk = f"{object_id} 0 obj\n".encode("latin-1") + data + b"\nendobj\n"
        chunks.append(chunk)
        current_offset += len(chunk)

    xref_offset = current_offset
    xref_lines = [f"xref\n0 {len(objects) + 1}\n".encode("latin-1"), b"0000000000 65535 f \n"]
    for offset in offsets[1:]:
        xref_lines.append(f"{offset:010d} 00000 n \n".encode("latin-1"))
    trailer = (
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF"
    ).encode("latin-1")
    chunks.extend(xref_lines)
    chunks.append(trailer)
    return b"".join(chunks)


def _build_cxc_receipt_pdf(print_data, copies=1):
    page_width = _pdf_mm_to_pt(210)
    page_height = _pdf_mm_to_pt(148.5)
    page_stream = _pdf_render_receipt_page_ops(print_data)
    normalized_copies = max(1, min(int(copies or 1), 20))
    return _build_pdf_document(page_width, page_height, [page_stream for _ in range(normalized_copies)])


def _load_cxc_pendientes(id_sn):
    if not id_sn:
        return []

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT FECHA_CONT, ID_DOC, TIPO_DOC, TOTAL_DOC, SALDO, FECHA_VENC, COMENTARIO, ABONO, EST_DOC
            FROM CAB_FACTURA
            WHERE ID_SN = %s
            ORDER BY FECHA_CONT, ID_DOC
            """,
            [id_sn],
        )
        factura_rows = cursor.fetchall()

    docs = [_stringify_doc(row[1]) for row in factura_rows if row[1] is not None]
    cuotas_by_doc = {}
    pagos_lookup = _load_cxc_active_payment_lookup(docs)
    pagos_por_doc = pagos_lookup.get("by_doc") or {}
    pagos_por_cuota = pagos_lookup.get("by_cuota") or {}

    if docs:
        cuotas_by_doc = _load_prestamo_rows_by_doc(docs)

    results = []
    for factura in factura_rows:
        fecha_cont, id_doc_raw, tipo_doc, total_doc, saldo_doc, fecha_venc, comentario, abono_doc, est_doc = factura
        id_doc = _stringify_doc(id_doc_raw)
        total_doc_val = _to_float(total_doc)
        saldo_doc_val = _to_float(saldo_doc)
        abono_doc_val = _to_float(abono_doc)
        estado_doc = str(est_doc or "").strip().upper()
        factura_abierta_por_estado = estado_doc == "ABIERTO"
        factura_cerrada_por_abono = _factura_closed_by_abono(total_doc, abono_doc)
        pagos_doc_val = _to_float(pagos_por_doc.get(id_doc))
        saldo_doc_reconstruido = saldo_doc_val if saldo_doc_val > 0.01 else max(
            max(total_doc_val - abono_doc_val, 0.0),
            max(total_doc_val - pagos_doc_val, 0.0),
        )
        if factura_abierta_por_estado and saldo_doc_reconstruido <= 0.01 and total_doc_val > 0 and pagos_doc_val <= 0.01:
            # Si el documento sigue abierto y no hay pagos activos aplicados, no lo descartamos
            # solo porque CAB_FACTURA.SALDO venga en cero por datos inconsistentes.
            saldo_doc_reconstruido = total_doc_val
        cuotas = cuotas_by_doc.get(id_doc, [])

        if cuotas:
            cuota_results = []
            total_cuotas_pendientes = 0.0
            for cuota in cuotas:
                no_cuota = _stringify_doc(cuota.get("no_cuota")) or "1"
                cuota_total = _to_float(cuota.get("cuota"))
                pagos_cuota_val = _to_float(pagos_por_cuota.get((id_doc, no_cuota)))
                saldo_cuota_val = _to_float(
                    _resolve_prestamo_balance(
                        cuota.get("balance"),
                        cuota.get("saldo_insoluto"),
                        cuota_total,
                        pagos_cuota_val,
                        cuota.get("abono_cuota"),
                    )
                )
                if saldo_cuota_val <= 0.01:
                    continue
                fecha_venc_cuota = cuota.get("fecha_venc") or fecha_venc
                dias = _days_overdue(fecha_venc_cuota)
                total_cuotas_pendientes += saldo_cuota_val
                cuota_results.append(
                    {
                        "td": str(tipo_doc or "").strip(),
                        "no_doc": id_doc,
                        "fecha_cont": _fmt_date(fecha_cont),
                        "monto_doc": total_doc_val,
                        "comentario_factura": str(comentario or "").strip(),
                        "no_cuota": no_cuota,
                        "cuota": cuota_total,
                        "balance_doc": saldo_cuota_val,
                        "balance_total_factura": 0.0,
                        "fecha_venc": _fmt_date(fecha_venc_cuota),
                        "venc": "*" if dias > 0 else "",
                        "dias": dias,
                        "cargo": 0.0,
                        "porc_desc": 0.0,
                        "desc_avance": 0.0,
                        "pago_abono": saldo_cuota_val,
                        "balance_pend": 0.0,
                        "tiene_financiamiento": True,
                    }
                )
            saldo_inicial_fuera_financiamiento = max(saldo_doc_reconstruido - total_cuotas_pendientes, 0.0)
            if cuota_results or saldo_inicial_fuera_financiamiento > 0.01:
                balance_total_factura = max(total_cuotas_pendientes + saldo_inicial_fuera_financiamiento, saldo_doc_reconstruido)
                if saldo_inicial_fuera_financiamiento > 0.01:
                    dias = _days_overdue(fecha_venc)
                    results.append(
                        {
                            "td": str(tipo_doc or "").strip(),
                            "no_doc": id_doc,
                            "fecha_cont": _fmt_date(fecha_cont),
                            "monto_doc": total_doc_val,
                            "comentario_factura": str(comentario or "").strip(),
                            "no_cuota": "1",
                            "cuota": saldo_inicial_fuera_financiamiento,
                            "balance_doc": saldo_inicial_fuera_financiamiento,
                            "balance_total_factura": balance_total_factura,
                            "fecha_venc": _fmt_date(fecha_venc),
                            "venc": "*" if dias > 0 else "",
                            "dias": dias,
                            "cargo": 0.0,
                            "porc_desc": 0.0,
                            "desc_avance": 0.0,
                            "pago_abono": saldo_inicial_fuera_financiamiento,
                            "balance_pend": 0.0,
                            "tiene_financiamiento": False,
                        }
                    )
                for cuota_result in cuota_results:
                    cuota_result["balance_total_factura"] = balance_total_factura
                results.extend(cuota_results)
                continue
            continue

        if not factura_abierta_por_estado:
            continue

        if factura_cerrada_por_abono:
            continue

        if saldo_doc_reconstruido <= 0.01:
            continue

        dias = _days_overdue(fecha_venc)
        results.append(
            {
                "td": str(tipo_doc or "").strip(),
                "no_doc": id_doc,
                "fecha_cont": _fmt_date(fecha_cont),
                "monto_doc": total_doc_val,
                "comentario_factura": str(comentario or "").strip(),
                "no_cuota": "1",
                "cuota": saldo_doc_reconstruido,
                "balance_doc": saldo_doc_reconstruido,
                "balance_total_factura": saldo_doc_reconstruido,
                "fecha_venc": _fmt_date(fecha_venc),
                "venc": "*" if dias > 0 else "",
                "dias": dias,
                "cargo": 0.0,
                "porc_desc": 0.0,
                "desc_avance": 0.0,
                "pago_abono": saldo_doc_reconstruido,
                "balance_pend": 0.0,
                "tiene_financiamiento": False,
            }
        )

    return results


def index(request):
    ctx = _base_context(request, page_title="Caja", active_nav="caja")
    if not ctx:
        return redirect("login")
    if not has_perm(ctx["auth_payload"]["usuario_id"], "caja", "ver"):
        return render_denied(request, active_nav="caja")
    ctx["submodules"] = {
        "cuentas_por_cobrar": has_perm(ctx["auth_payload"]["usuario_id"], "caja", "ver_cuentas_por_cobrar"),
        "cuadre_caja": has_perm(ctx["auth_payload"]["usuario_id"], "caja", "ver_cuadre_caja"),
        "financiamiento": has_perm(ctx["auth_payload"]["usuario_id"], "caja", "ver_financiamiento"),
    }
    return render(request, "caja/index.html", ctx)


def _render_submodule(request, *, perm_code, page_title, submodule_title, submodule_description):
    ctx = _base_context(request, page_title=page_title, active_nav="caja")
    if not ctx:
        return redirect("login")
    if not has_perm(ctx["auth_payload"]["usuario_id"], "caja", perm_code):
        return render_denied(request, active_nav="caja")
    ctx["submodule_title"] = submodule_title
    ctx["submodule_description"] = submodule_description
    return render(request, "caja/submodulo.html", ctx)


def _load_financiamiento_search_rows(query="", filtro="documento", limit=120):
    cab_columns = _load_table_columns("CAB_PRESTAMO")
    if not cab_columns:
        return []

    safe_limit = max(20, min(int(limit or 120), 300))
    doc_col = _pick_existing_column(cab_columns, "NO_DOC", "ID_DOC", "DOCUMENTO", "FACTURA")
    no_fact_col = _pick_existing_column(cab_columns, "ID_DOC", "NO_DOC", "DOCUMENTO", "FACTURA", "NO_FACT", "NO_FACTURA", "DOCUMENTO_BASE", "FACTURA_BASE", doc_col)
    no_col = _pick_existing_column(cab_columns, "ID_PRESTAMO", "NO_PRESTAMO", "NO", "ID_DOC", doc_col)
    id_sn_col = _pick_existing_column(cab_columns, "ID_SN", "CLIENTE", "COD_CLIENTE")
    nombre_col = _pick_existing_column(cab_columns, "NOM_SN", "NOM_SOCIO", "NOMBRE", "NOM_CLIENTE")
    rnc_col = _pick_existing_column(cab_columns, "RNC_CED", "RNC", "CEDULA")
    fecha_col = _pick_existing_column(cab_columns, "FECHA_DOC", "FECHA_CONT", "FECHA", "FECHA_APLIC")
    estado_col = _pick_existing_column(cab_columns, "EST_DOC", "ESTATUS", "ESTADO")
    total_col = _pick_existing_column(
        cab_columns,
        "TOTAL_DOC",
        "MONTO",
        "IMPORTE",
        "TOTAL",
        "CAPITAL",
        "CAPITAL_FINANCIADO",
    )
    saldo_col = _pick_existing_column(cab_columns, "SALDO", "BALANCE", "SALDO_INSOLUTO")
    abono_col = _pick_existing_column(cab_columns, "ABONO")
    tipo_col = _pick_existing_column(cab_columns, "TIPO", "TIPO_DOC", "CLASE_DOC")

    if not doc_col:
        return []

    sql = f"SELECT TOP {safe_limit} * FROM CAB_PRESTAMO"
    where_parts = []
    params = []
    query = str(query or "").strip()
    filtro = str(filtro or "documento").strip().lower()
    if query:
        target_col = doc_col
        if filtro == "codigo" and id_sn_col:
            target_col = id_sn_col
        elif filtro == "nombre" and nombre_col:
            target_col = nombre_col
        elif filtro == "cedula" and rnc_col:
            target_col = rnc_col
        where_parts.append(f"CAST([{target_col}] AS NVARCHAR(255)) LIKE %s")
        params.append(f"%{query}%")
    if where_parts:
        sql += " WHERE " + " AND ".join(where_parts)
    order_col = fecha_col or no_col or doc_col
    sql += f" ORDER BY [{order_col}] DESC"

    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        raw_columns = [col[0] for col in cursor.description]
        rows = [_normalize_result_row(raw_columns, raw_row) for raw_row in cursor.fetchall()]

    id_sns = [
        _pick_row_text(row, id_sn_col)
        for row in rows
        if id_sn_col
    ]
    maestro_lookup = _load_maestro_sn_lookup(id_sns)
    detail_lookup_candidates = []
    for row in rows:
        for candidate in (
            _stringify_doc(_pick_row_value(row, doc_col)),
            _stringify_doc(_pick_row_value(row, no_col)),
            _stringify_doc(_pick_row_value(row, no_fact_col)),
        ):
            if candidate:
                detail_lookup_candidates.append(candidate)
    prestamo_rows_by_doc = _load_prestamo_rows_by_doc(detail_lookup_candidates)

    results = []
    for row in rows:
        codigo = _pick_row_text(row, id_sn_col)
        maestro_row = maestro_lookup.get(codigo, {})
        monto = _to_float(_pick_row_value(row, total_col, default=0))
        saldo = _to_float(_pick_row_value(row, saldo_col, default=0))
        abono = _to_float(_pick_row_value(row, abono_col, default=0))
        saldo_reconstruido = max(monto - abono, 0.0) if monto > 0.01 else 0.0
        saldo_detalle = 0.0
        has_detail_rows = False
        for detail_lookup in (
            _stringify_doc(_pick_row_value(row, doc_col)),
            _stringify_doc(_pick_row_value(row, no_col)),
            _stringify_doc(_pick_row_value(row, no_fact_col)),
        ):
            if not detail_lookup:
                continue
            detail_rows = prestamo_rows_by_doc.get(detail_lookup) or []
            if not detail_rows:
                continue
            has_detail_rows = True
            saldo_detalle = sum(
                _to_float(
                    _resolve_prestamo_balance(
                        detail_row.get("balance"),
                        detail_row.get("saldo_insoluto"),
                        detail_row.get("cuota"),
                        None,
                        detail_row.get("abono_cuota"),
                    )
                )
                for detail_row in detail_rows
            )
            break
        saldo = saldo_detalle if has_detail_rows else max(saldo, saldo_reconstruido)
        results.append(
            {
                "no_doc": _stringify_doc(_pick_row_value(row, doc_col, no_col)),
                "no_factura": _stringify_doc(_pick_row_value(row, no_fact_col, doc_col, no_col)),
                "no": _stringify_doc(_pick_row_value(row, no_col, doc_col)),
                "codigo": codigo,
                "nombre": _pick_row_text(row, nombre_col) or maestro_row.get("nombre", ""),
                "cedula": _pick_row_text(row, rnc_col) or maestro_row.get("rnc_ced", ""),
                "fecha": _fmt_date_flexible(_pick_row_value(row, fecha_col)),
                "estado": _pick_row_text(row, estado_col) or ("Abierto" if saldo > 0.01 else "Cerrado"),
                "tipo": _pick_row_text(row, tipo_col) or "Financiamiento",
                "monto": monto,
                "saldo": saldo,
            }
        )
    return results


def _load_financiamiento_factura_doc_set(doc_numbers):
    docs = [_stringify_doc(value) for value in doc_numbers if _stringify_doc(value)]
    if not docs:
        return set()

    financed_docs = set()
    unique_docs = list(dict.fromkeys(docs))

    det_columns = _load_table_columns("DET_PRESTAMO")
    det_doc_col = _pick_existing_column(det_columns, "NO_DOC", "ID_DOC", "DOCUMENTO", "FACTURA")
    if det_doc_col:
        for docs_chunk in _chunked(unique_docs, 300):
            placeholders = ", ".join(["%s"] * len(docs_chunk))
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT DISTINCT CAST([{det_doc_col}] AS NVARCHAR(255))
                    FROM DET_PRESTAMO
                    WHERE CAST([{det_doc_col}] AS NVARCHAR(255)) IN ({placeholders})
                    """,
                    docs_chunk,
                )
                financed_docs.update(_stringify_doc(row[0]) for row in cursor.fetchall() if row and row[0] is not None)

    cab_columns = _load_table_columns("CAB_PRESTAMO")
    cab_fact_col = _pick_existing_column(cab_columns, "NO_FACT", "NO_FACTURA", "DOCUMENTO_BASE", "FACTURA_BASE")
    if cab_fact_col:
        for docs_chunk in _chunked(unique_docs, 300):
            placeholders = ", ".join(["%s"] * len(docs_chunk))
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT DISTINCT CAST([{cab_fact_col}] AS NVARCHAR(255))
                    FROM CAB_PRESTAMO
                    WHERE CAST([{cab_fact_col}] AS NVARCHAR(255)) IN ({placeholders})
                    """,
                    docs_chunk,
                )
                financed_docs.update(_stringify_doc(row[0]) for row in cursor.fetchall() if row and row[0] is not None)

    return financed_docs


def _load_financiamiento_facturas_disponibles(query="", filtro="nombre", limit=120):
    cab_columns = _load_table_columns("CAB_FACTURA")
    if not cab_columns:
        return []

    safe_limit = max(20, min(int(limit or 120), 300))
    fetch_limit = max(safe_limit * 4, 120)
    doc_col = _pick_existing_column(cab_columns, "ID_DOC", "NO_DOC", "DOCUMENTO", "FACTURA")
    id_sn_col = _pick_existing_column(cab_columns, "ID_SN", "CLIENTE", "COD_CLIENTE")
    nombre_col = _pick_existing_column(cab_columns, "NOM_SN", "NOM_SOCIO", "NOMBRE", "NOM_CLIENTE")
    rnc_col = _pick_existing_column(cab_columns, "RNC_CED", "RNC", "CEDULA")
    fecha_col = _pick_existing_column(cab_columns, "FECHA_DOC", "FECHA_CONT", "FECHA", "FECHA_APLIC")
    total_col = _pick_existing_column(cab_columns, "TOTAL_DOC", "MONTO", "IMPORTE", "TOTAL")
    saldo_col = _pick_existing_column(cab_columns, "SALDO", "BALANCE")
    abono_col = _pick_existing_column(cab_columns, "ABONO")
    estado_col = _pick_existing_column(cab_columns, "EST_DOC", "ESTATUS", "ESTADO")
    tipo_col = _pick_existing_column(cab_columns, "TIPO_DOC", "TD", "CLASE_DOC", "TIPO")
    comentario_col = _pick_existing_column(cab_columns, "COMENTARIO", "OBSERVACION")

    if not doc_col:
        return []

    sql = f"SELECT TOP {fetch_limit} * FROM CAB_FACTURA"
    where_parts = []
    params = []
    query = str(query or "").strip()
    filtro = str(filtro or "nombre").strip().lower()
    if query:
        target_col = nombre_col or doc_col
        if filtro == "documento":
            target_col = doc_col
        elif filtro == "codigo" and id_sn_col:
            target_col = id_sn_col
        elif filtro == "cedula" and rnc_col:
            target_col = rnc_col
        where_parts.append(f"CAST([{target_col}] AS NVARCHAR(255)) LIKE %s")
        params.append(f"%{query}%")
    if where_parts:
        sql += " WHERE " + " AND ".join(where_parts)
    order_col = fecha_col or doc_col
    sql += f" ORDER BY [{order_col}] DESC"

    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        raw_columns = [col[0] for col in cursor.description]
        rows = [_normalize_result_row(raw_columns, raw_row) for raw_row in cursor.fetchall()]

    doc_numbers = [_stringify_doc(_pick_row_value(row, doc_col)) for row in rows]
    maestro_lookup = _load_maestro_sn_lookup(
        [_pick_row_text(row, id_sn_col) for row in rows if id_sn_col]
    )
    financed_docs = _load_financiamiento_factura_doc_set(doc_numbers)

    results = []
    for row in rows:
        no_doc = _stringify_doc(_pick_row_value(row, doc_col))
        if not no_doc or no_doc in financed_docs:
            continue

        codigo = _pick_row_text(row, id_sn_col)
        maestro_row = maestro_lookup.get(codigo, {})
        total_doc_val = max(_to_float(_pick_row_value(row, total_col, default=0)), 0.0)
        saldo_doc_val = max(_to_float(_pick_row_value(row, saldo_col, default=0)), 0.0)
        abono_doc_val = max(_to_float(_pick_row_value(row, abono_col, default=0)), 0.0)
        estado_doc = str(_pick_row_text(row, estado_col) or "").strip().upper()
        saldo_doc_reconstruido = saldo_doc_val if saldo_doc_val > 0.01 else max(total_doc_val - abono_doc_val, 0.0)
        if estado_doc != "ABIERTO" or saldo_doc_reconstruido <= 0.01 or _factura_closed_by_abono(total_doc_val, abono_doc_val):
            continue

        results.append(
            {
                "no_doc": no_doc,
                "codigo": codigo,
                "nombre": _pick_row_text(row, nombre_col) or maestro_row.get("nombre", ""),
                "cedula": _pick_row_text(row, rnc_col) or maestro_row.get("rnc_ced", ""),
                "fecha": _fmt_date_flexible(_pick_row_value(row, fecha_col)),
                "fecha_iso": _fmt_date_input(_pick_row_value(row, fecha_col)),
                "estado": _pick_row_text(row, estado_col) or "Abierto",
                "tipo_doc": _pick_row_text(row, tipo_col) or "",
                "total_doc": total_doc_val,
                "saldo": saldo_doc_reconstruido,
                "comentario": _pick_row_text(row, comentario_col),
            }
        )
        if len(results) >= safe_limit:
            break

    def _factura_sort_key(item):
        fecha_key = str(item.get("fecha_iso") or "")
        no_doc_key = str(item.get("no_doc") or "").strip()
        try:
            no_doc_numeric = int(no_doc_key)
        except (TypeError, ValueError):
            no_doc_numeric = -1
        return (fecha_key, no_doc_numeric, no_doc_key)

    results.sort(key=_factura_sort_key, reverse=True)
    return results


def _load_financiamiento_detail_rows(no_doc):
    no_doc_text = str(no_doc or "").strip()
    if not no_doc_text:
        return []

    det_columns = _load_table_columns("DET_PRESTAMO")
    doc_col = _pick_existing_column(det_columns, "NO_DOC", "ID_DOC", "DOCUMENTO", "FACTURA")
    cuota_num_col = _pick_existing_column(det_columns, "NO_CUOTA", "CUOTA_NUM", "NUM_CUOTA")
    fecha_col = _pick_existing_column(det_columns, "FECHA")
    fecha_venc_col = _pick_existing_column(det_columns, "FECHA_VENC", "F_VENC", "VENCIMIENTO")
    cuota_col = _pick_existing_column(det_columns, "CUOTA", "MONTO_CUOTA", "VALOR_CUOTA")
    balance_col = _pick_existing_column(det_columns, "BALANCE")
    saldo_insoluto_col = _pick_existing_column(det_columns, "SALDO_INSOLUTO")
    abono_cuota_col = _pick_existing_column(det_columns, "ABONO_CUOTA", "ABONOCUOTA", "ABONO_CUENTA", "ABONOCUENTA")
    interes_col = _pick_existing_column(det_columns, "MONTO_INTERES", "MTO_INTERES", "INTERES", "IMP_INTERES", "INT")
    capital_col = _pick_existing_column(det_columns, "CAPITAL", "MONTO_CAPITAL", "CAPITAL_CUOTA")
    no_recibo_col = _pick_existing_column(det_columns, "NORECIBO", "NO_RECIBO")
    if not doc_col:
        return []

    where_sql, where_params = _build_doc_lookup_where([doc_col], no_doc_text)

    with connection.cursor() as cursor:
        order_sql = f"[{cuota_num_col}]" if cuota_num_col else f"[{doc_col}]"
        if cuota_num_col:
            order_sql = f"TRY_CAST([{cuota_num_col}] AS BIGINT), [{cuota_num_col}]"
        cursor.execute(
            f"""
            SELECT *
            FROM DET_PRESTAMO
            WHERE {where_sql}
            ORDER BY {order_sql}
            """,
            where_params,
        )
        raw_columns = [col[0] for col in cursor.description]
        raw_rows = [_normalize_result_row(raw_columns, raw_row) for raw_row in cursor.fetchall()]

    detail_rows = []
    for row in raw_rows:
        cuota = max(_to_float(_pick_row_value(row, cuota_col, default=0)), 0.0)
        pagado = max(_to_float(_pick_row_value(row, abono_cuota_col, default=0)), 0.0)
        cuota_pendiente = _to_float(
            _resolve_prestamo_balance(
                _pick_row_value(row, balance_col),
                cuota=_pick_row_value(row, cuota_col),
                abono_cuota=_pick_row_value(row, abono_cuota_col),
            )
        )
        balance = max(
            _to_float(_pick_row_value(row, saldo_insoluto_col, balance_col, default=cuota_pendiente)),
            0.0,
        )
        if pagado <= 0.0001 and cuota > 0:
            pagado = max(cuota - max(cuota_pendiente, 0.0), 0.0)
        interes = max(_to_float(_pick_row_value(row, interes_col, default=0)), 0.0)
        capital = max(_to_float(_pick_row_value(row, capital_col, default=max(cuota - interes, 0.0))), 0.0)
        pendiente = max(cuota_pendiente, 0.0)
        detail_rows.append(
            {
                "no_cuota": _stringify_doc(_pick_row_value(row, cuota_num_col, default="")),
                "fecha": _fmt_date_flexible(_pick_row_value(row, fecha_col)),
                "fecha_venc": _fmt_date_flexible(_pick_row_value(row, fecha_venc_col)),
                "monto_interes": interes,
                "capital": capital,
                "balance": balance,
                "cuota": cuota,
                "pagado": pagado,
                "pendiente": pendiente,
                "no_recibo": _pick_row_text(row, no_recibo_col),
            }
        )
    return detail_rows


def _load_financiamiento_record(no_doc):
    no_doc_text = str(no_doc or "").strip()
    if not no_doc_text:
        return None

    cab_columns = _load_table_columns("CAB_PRESTAMO")
    if not cab_columns:
        return None

    doc_col = _pick_existing_column(cab_columns, "NO_DOC", "ID_DOC", "DOCUMENTO", "FACTURA")
    no_fact_col = _pick_existing_column(cab_columns, "ID_DOC", "NO_DOC", "DOCUMENTO", "FACTURA", "NO_FACT", "NO_FACTURA", "DOCUMENTO_BASE", "FACTURA_BASE", doc_col)
    no_col = _pick_existing_column(cab_columns, "ID_PRESTAMO", "NO_PRESTAMO", "NO", "ID_DOC", doc_col)
    id_sn_col = _pick_existing_column(cab_columns, "ID_SN", "CLIENTE", "COD_CLIENTE")
    nombre_col = _pick_existing_column(cab_columns, "NOM_SN", "NOM_SOCIO", "NOMBRE", "NOM_CLIENTE")
    rnc_col = _pick_existing_column(cab_columns, "RNC_CED", "RNC", "CEDULA")
    fecha_col = _pick_existing_column(cab_columns, "FECHA_DOC", "FECHA_CONT", "FECHA", "FECHA_APLIC")
    fecha_base_col = _pick_existing_column(cab_columns, "FECHA_BASE", "FECHA_DOC", "FECHA_CONT", "FECHA")
    estado_col = _pick_existing_column(cab_columns, "EST_DOC", "ESTATUS", "ESTADO")
    tipo_col = _pick_existing_column(cab_columns, "TIPO", "TIPO_DOC", "CLASE_DOC")
    moneda_col = _pick_existing_column(cab_columns, "MON_DOC", "MONEDA")
    tasa_col = _pick_existing_column(cab_columns, "TASAFACT", "TASA", "FACTOR", "TIPO_CAMBIO")
    total_col = _pick_existing_column(
        cab_columns,
        "TOTAL_DOC",
        "MONTO",
        "IMPORTE",
        "TOTAL",
        "CAPITAL",
        "CAPITAL_FINANCIADO",
    )
    saldo_col = _pick_existing_column(cab_columns, "SALDO", "BALANCE", "SALDO_INSOLUTO")
    abono_col = _pick_existing_column(cab_columns, "ABONO")
    porc_interes_col = _pick_existing_column(cab_columns, "PORC_INTERES", "PORC_INT", "INTERES_PORC", "PORCENTAJE_INTERES")
    plazo_col = _pick_existing_column(cab_columns, "PLAZO", "MESES", "NUM_CUOTAS", "CUOTAS")
    metodo_col = _pick_existing_column(cab_columns, "METODO", "METODO_CALC", "FORMA", "SISTEMA", "AMORTIZACION")
    tipo_cuota_col = _pick_existing_column(cab_columns, "TIPO_CUOTA", "TIPO_PAGO", "FRECUENCIA", "PERIODO")
    cuota_valor_col = _pick_existing_column(cab_columns, "CUOTA", "MONTO_CUOTA", "VALOR_CUOTA")
    comentario_col = _pick_existing_column(cab_columns, "COMENTARIO", "OBSERVACION")
    if not doc_col:
        return None

    where_sql, where_params = _build_doc_lookup_where([doc_col, no_fact_col, no_col], no_doc_text)

    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT TOP 1 * FROM CAB_PRESTAMO WHERE {where_sql}",
            where_params,
        )
        raw_row = cursor.fetchone()
        if not raw_row:
            return None
        raw_columns = [col[0] for col in cursor.description]
        cab_row = _normalize_result_row(raw_columns, raw_row)

    detail_rows = []
    detail_lookup_candidates = [
        _stringify_doc(_pick_row_value(cab_row, doc_col)),
        _stringify_doc(_pick_row_value(cab_row, no_col)),
        _stringify_doc(_pick_row_value(cab_row, no_fact_col)),
        no_doc_text,
    ]
    for detail_lookup in detail_lookup_candidates:
        if not detail_lookup:
            continue
        detail_rows = _load_financiamiento_detail_rows(detail_lookup)
        if detail_rows:
            break
    codigo = _pick_row_text(cab_row, id_sn_col)
    maestro_lookup = _load_maestro_sn_lookup([codigo]) if codigo else {}
    maestro_row = maestro_lookup.get(codigo, {})

    monto = max(_to_float(_pick_row_value(cab_row, total_col, default=0)), 0.0)
    saldo = max(_to_float(_pick_row_value(cab_row, saldo_col, default=0)), 0.0)
    total_pagado = sum(float(row.get("pagado") or 0) for row in detail_rows)
    total_pendiente = sum(float(row.get("pendiente") or 0) for row in detail_rows)
    capital_total = sum(float(row.get("capital") or 0) for row in detail_rows)
    interes_total = sum(float(row.get("monto_interes") or 0) for row in detail_rows)
    cuota_total = sum(float(row.get("cuota") or 0) for row in detail_rows)
    if monto <= 0.0001 and cuota_total > 0:
        monto = cuota_total
    if saldo <= 0.0001 and total_pendiente > 0:
        saldo = total_pendiente
    valor_cuota = max(
        _to_float(_pick_row_value(cab_row, cuota_valor_col, default=0)),
        float(detail_rows[0].get("cuota") or 0) if detail_rows else 0.0,
    )
    plazo = int(_to_float(_pick_row_value(cab_row, plazo_col, default=len(detail_rows) if detail_rows else 0)))
    if plazo <= 0:
        plazo = len(detail_rows)

    prestamo = {
        "no_doc": _stringify_doc(_pick_row_value(cab_row, doc_col, no_col)),
        "no_factura": _stringify_doc(_pick_row_value(cab_row, no_fact_col, doc_col, no_col)),
        "no": _stringify_doc(_pick_row_value(cab_row, no_col, doc_col)),
        "codigo": codigo,
        "nombre": _pick_row_text(cab_row, nombre_col) or maestro_row.get("nombre", ""),
        "cedula": _pick_row_text(cab_row, rnc_col) or maestro_row.get("rnc_ced", ""),
        "estado": _pick_row_text(cab_row, estado_col) or ("Abierto" if saldo > 0.01 else "Cerrado"),
        "fecha": _fmt_date_input(_pick_row_value(cab_row, fecha_col)),
        "tipo": _pick_row_text(cab_row, tipo_col) or "Financiamiento",
        "moneda": _pick_row_text(cab_row, moneda_col) or "RD$",
        "tasa": max(_to_float(_pick_row_value(cab_row, tasa_col, default=1)), 0.0) or 1.0,
        "monto": monto,
        "porc_interes": max(_to_float(_pick_row_value(cab_row, porc_interes_col, default=0)), 0.0),
        "plazo": plazo,
        "metodo": _pick_row_text(cab_row, metodo_col) or "Lineal",
        "tipo_cuota": _pick_row_text(cab_row, tipo_cuota_col) or "Mensual",
        "fecha_base": _fmt_date_input(_pick_row_value(cab_row, fecha_base_col)),
        "valor_cuota": valor_cuota,
        "comentario": _pick_row_text(cab_row, comentario_col),
        "saldo": saldo,
        "abono": max(_to_float(_pick_row_value(cab_row, abono_col, default=max(monto - saldo, 0))), 0.0),
        "total_pagado": total_pagado,
        "total_pendiente": total_pendiente,
    }
    historial = [
        row for row in detail_rows
        if row.get("pagado", 0) > 0.0001 or str(row.get("no_recibo") or "").strip()
    ]
    tolerance = 0.01
    estado_text = str(prestamo.get("estado") or "").strip().upper()
    detail_initial_state = True
    for row in detail_rows:
        cuota_val = _to_float(row.get("cuota"))
        pagado_val = _to_float(row.get("pagado"))
        pendiente_val = _to_float(row.get("pendiente"))
        if pagado_val > tolerance:
            detail_initial_state = False
            break
        if str(row.get("no_recibo") or "").strip():
            detail_initial_state = False
            break
        if cuota_val > tolerance and abs(cuota_val - pendiente_val) > tolerance:
            detail_initial_state = False
            break
    editable = (
        estado_text == "ABIERTO"
        and _to_float(prestamo.get("abono")) <= tolerance
        and _to_float(prestamo.get("total_pagado")) <= tolerance
        and detail_initial_state
    )
    factura_base = None
    factura_base_doc = prestamo.get("no_factura") or prestamo.get("no_doc")
    if editable and factura_base_doc:
        factura_base = _load_financiamiento_factura_base_snapshot(factura_base_doc)
    finanzas = {
        "capital_total": capital_total,
        "interes_total": interes_total,
        "cuota_total": cuota_total,
        "pagado_total": total_pagado,
        "pendiente_total": total_pendiente,
    }
    return {
        "prestamo": prestamo,
        "detalle": detail_rows,
        "historial": historial,
        "finanzas": finanzas,
        "editable": editable,
        "factura_base": factura_base,
    }


def _load_financiamiento_factura_base_snapshot(factura_no):
    factura_no = _stringify_doc(factura_no)
    if not factura_no:
        return None

    cab_columns = _load_table_columns("CAB_FACTURA")
    if not cab_columns:
        return None

    doc_col = _pick_existing_column(cab_columns, "ID_DOC", "NO_DOC", "DOCUMENTO", "FACTURA")
    id_sn_col = _pick_existing_column(cab_columns, "ID_SN", "CLIENTE", "COD_CLIENTE")
    nombre_col = _pick_existing_column(cab_columns, "NOM_SN", "NOM_SOCIO", "NOMBRE", "NOM_CLIENTE")
    rnc_col = _pick_existing_column(cab_columns, "RNC_CED", "RNC", "CEDULA")
    fecha_col = _pick_existing_column(cab_columns, "FECHA_DOC", "FECHA_CONT", "FECHA", "FECHA_APLIC")
    total_col = _pick_existing_column(cab_columns, "TOTAL_DOC", "MONTO", "IMPORTE", "TOTAL")
    saldo_col = _pick_existing_column(cab_columns, "SALDO", "BALANCE")
    abono_col = _pick_existing_column(cab_columns, "ABONO")
    estado_col = _pick_existing_column(cab_columns, "EST_DOC", "ESTATUS", "ESTADO")
    tipo_col = _pick_existing_column(cab_columns, "TIPO_DOC", "TD", "CLASE_DOC", "TIPO")
    comentario_col = _pick_existing_column(cab_columns, "COMENTARIO", "OBSERVACION")
    if not doc_col:
        return None

    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT TOP 1 * FROM CAB_FACTURA WHERE CAST([{doc_col}] AS NVARCHAR(255)) = %s",
            [factura_no],
        )
        raw_row = cursor.fetchone()
        if not raw_row:
            return None
        row = _normalize_result_row([col[0] for col in cursor.description], raw_row)

    codigo = _pick_row_text(row, id_sn_col)
    maestro_row = (_load_maestro_sn_lookup([codigo]).get(codigo, {}) if codigo else {})
    total_doc_val = max(_to_float(_pick_row_value(row, total_col, default=0)), 0.0)
    saldo_doc_val = max(_to_float(_pick_row_value(row, saldo_col, default=0)), 0.0)
    abono_doc_val = max(_to_float(_pick_row_value(row, abono_col, default=0)), 0.0)
    saldo_doc_reconstruido = saldo_doc_val if saldo_doc_val > 0.01 else max(total_doc_val - abono_doc_val, 0.0)
    estado_doc = _pick_row_text(row, estado_col) or "Abierto"
    return {
        "no_doc": factura_no,
        "codigo": codigo,
        "nombre": _pick_row_text(row, nombre_col) or maestro_row.get("nombre", ""),
        "cedula": _pick_row_text(row, rnc_col) or maestro_row.get("rnc_ced", ""),
        "fecha": _fmt_date_flexible(_pick_row_value(row, fecha_col)),
        "fecha_iso": _fmt_date_input(_pick_row_value(row, fecha_col)),
        "estado": estado_doc,
        "tipo_doc": _pick_row_text(row, tipo_col) or "",
        "total_doc": total_doc_val,
        "saldo": saldo_doc_reconstruido,
        "abono": abono_doc_val,
        "comentario": _pick_row_text(row, comentario_col),
        "disponible": (
            str(estado_doc or "").strip().upper() == "ABIERTO"
            and saldo_doc_reconstruido > 0.01
            and not _factura_closed_by_abono(total_doc_val, abono_doc_val)
        ),
    }


def _normalize_financiamiento_choice(value, allowed, default_value):
    text = str(value or "").strip().lower()
    allowed_map = {str(option).strip().lower(): option for option in (allowed or [])}
    return allowed_map.get(text, default_value)


def _prepare_financiamiento_detail_rows(raw_detail):
    if not isinstance(raw_detail, list):
        raise ValueError("Detalle invalido.")

    rows = []
    used_cuotas = set()
    for index, item in enumerate(raw_detail, start=1):
        if not isinstance(item, dict):
            continue

        no_cuota = str(item.get("no_cuota") or index).strip() or str(index)
        if no_cuota in used_cuotas:
            raise ValueError(f"La cuota {no_cuota} esta repetida.")
        used_cuotas.add(no_cuota)

        fecha_venc = _parse_date_value(item.get("fecha_venc") or item.get("fecha"))
        if not fecha_venc:
            raise ValueError(f"Debes indicar la fecha de vencimiento de la cuota {no_cuota}.")

        monto_interes = max(_to_decimal(item.get("monto_interes")), Decimal("0"))
        capital = max(_to_decimal(item.get("capital")), Decimal("0"))
        saldo_insoluto = max(_to_decimal(item.get("balance")), Decimal("0"))
        cuota = max(_to_decimal(item.get("cuota")), Decimal("0"))
        if cuota <= Decimal("0.01"):
            raise ValueError(f"La cuota {no_cuota} debe tener un valor mayor a 0.")
        if capital <= Decimal("0.01") and cuota <= Decimal("0.01"):
            raise ValueError(f"La cuota {no_cuota} no tiene valores validos.")

        rows.append(
            {
                "linea": index,
                "no_cuota": no_cuota,
                "fecha_venc": fecha_venc,
                "monto_interes": monto_interes,
                "capital": capital,
                "saldo_insoluto": saldo_insoluto,
                "cuota": cuota,
                "balance": cuota,
                "abono_cuota": Decimal("0"),
            }
        )

    if not rows:
        raise ValueError("Debes generar al menos una cuota antes de grabar.")
    return rows


def _lock_financiamiento_factura_base(cursor, factura_no):
    factura_columns = _load_table_columns("CAB_FACTURA")
    factura_doc_col = _pick_existing_column(factura_columns, "ID_DOC", "NO_DOC", "DOCUMENTO", "FACTURA")
    if not factura_doc_col:
        raise ValueError("No se pudo identificar la clave de CAB_FACTURA.")

    cursor.execute(
        f"SELECT TOP 1 * FROM CAB_FACTURA WITH (UPDLOCK, HOLDLOCK) WHERE CAST([{factura_doc_col}] AS NVARCHAR(255)) = %s",
        [factura_no],
    )
    raw_row = cursor.fetchone()
    if not raw_row:
        raise ValueError(f"No se encontro la factura {factura_no}.")

    factura_row = _normalize_result_row([col[0] for col in cursor.description], raw_row)
    estado_col = _pick_existing_column(factura_columns, "EST_DOC", "ESTATUS", "ESTADO")
    total_col = _pick_existing_column(factura_columns, "TOTAL_DOC", "MONTO", "IMPORTE", "TOTAL")
    saldo_col = _pick_existing_column(factura_columns, "SALDO", "BALANCE")
    abono_col = _pick_existing_column(factura_columns, "ABONO")
    id_sn_col = _pick_existing_column(factura_columns, "ID_SN", "CLIENTE", "COD_CLIENTE")
    nombre_col = _pick_existing_column(factura_columns, "NOM_SN", "NOM_SOCIO", "NOMBRE", "NOM_CLIENTE")
    rnc_col = _pick_existing_column(factura_columns, "RNC_CED", "RNC", "CEDULA")
    fecha_col = _pick_existing_column(factura_columns, "FECHA_DOC", "FECHA_CONT", "FECHA", "FECHA_APLIC")
    comentario_col = _pick_existing_column(factura_columns, "COMENTARIO", "OBSERVACION")

    total_doc = max(_to_decimal(_pick_row_value(factura_row, total_col, default=0)), Decimal("0"))
    saldo_doc = max(_to_decimal(_pick_row_value(factura_row, saldo_col, default=0)), Decimal("0"))
    abono_doc = max(_to_decimal(_pick_row_value(factura_row, abono_col, default=0)), Decimal("0"))
    estado_doc = _pick_row_text(factura_row, estado_col).strip().upper()
    saldo_reconstruido = saldo_doc if saldo_doc > Decimal("0.01") else max(total_doc - abono_doc, Decimal("0"))
    if estado_doc != "ABIERTO" or saldo_reconstruido <= Decimal("0.01") or _factura_closed_by_abono(total_doc, abono_doc):
        raise ValueError(f"La factura {factura_no} no esta disponible para financiamiento.")

    return {
        "columns": factura_columns,
        "doc_col": factura_doc_col,
        "row": factura_row,
        "id_sn": _pick_row_text(factura_row, id_sn_col),
        "nombre": _pick_row_text(factura_row, nombre_col),
        "cedula": _pick_row_text(factura_row, rnc_col),
        "fecha": _pick_row_value(factura_row, fecha_col),
        "comentario": _pick_row_text(factura_row, comentario_col),
        "total_doc": total_doc,
        "saldo": saldo_reconstruido,
        "estado": estado_doc,
    }


def _persist_financiamiento_record(
    cursor,
    *,
    record_lookup,
    factura_no,
    fecha_doc,
    fecha_base,
    metodo,
    tipo_cuota,
    porc_interes,
    comentario,
    detail_rows,
    usuario_id,
    usuario_nombre,
    terminal,
):
    cab_columns = _load_table_columns("CAB_PRESTAMO")
    det_columns = _load_table_columns("DET_PRESTAMO")
    if not cab_columns or not det_columns:
        raise ValueError("No se pudieron cargar las tablas CAB_PRESTAMO/DET_PRESTAMO.")

    cab_identity_columns = _load_identity_columns("CAB_PRESTAMO")
    det_identity_columns = _load_identity_columns("DET_PRESTAMO")

    cab_doc_col = _pick_existing_column(cab_columns, "NO_DOC", "ID_DOC", "DOCUMENTO", "FACTURA")
    cab_fact_col = _pick_existing_column(
        cab_columns,
        "ID_DOC",
        "NO_FACT",
        "NO_FACTURA",
        "DOCUMENTO_BASE",
        "FACTURA_BASE",
        "FACTURA",
        cab_doc_col,
    )
    cab_no_col = _pick_existing_column(cab_columns, "ID_PRESTAMO", "NO_PRESTAMO", "NO", cab_doc_col)
    if not cab_doc_col and not cab_fact_col and not cab_no_col:
        raise ValueError("No se pudo determinar la clave de CAB_PRESTAMO.")

    factura_base = _lock_financiamiento_factura_base(cursor, factura_no)
    id_sn = str(factura_base.get("id_sn") or "").strip()
    nombre_cliente = str(factura_base.get("nombre") or "").strip()
    rnc_ced = str(factura_base.get("cedula") or "").strip()
    comentario_final = str(comentario or "").strip() or str(factura_base.get("comentario") or "").strip()

    monto_principal = sum((row.get("capital") or Decimal("0")) for row in detail_rows)
    cuota_total = sum((row.get("cuota") or Decimal("0")) for row in detail_rows)
    valor_cuota = detail_rows[0].get("cuota") if detail_rows else Decimal("0")
    plazo = len(detail_rows)
    saldo_total = cuota_total

    existing_record = None
    locked_cab_row = None
    loan_no_value = ""
    main_doc_value = ""
    lookup_value = str(record_lookup or "").strip()
    if lookup_value:
        existing_record = _load_financiamiento_record(lookup_value)
        if not existing_record:
            raise ValueError("No se encontro el financiamiento que intentas actualizar.")
        if not existing_record.get("editable"):
            raise ValueError("Solo se pueden modificar financiamientos virgenes.")
        existing_prestamo = existing_record.get("prestamo") or {}
        existing_factura_no = str(existing_prestamo.get("no_factura") or existing_prestamo.get("no_doc") or "").strip()
        if existing_factura_no and existing_factura_no != factura_no:
            raise ValueError("No se puede cambiar la factura base de un financiamiento existente.")

        where_sql, where_params = _build_doc_lookup_where([cab_doc_col, cab_fact_col, cab_no_col], lookup_value)
        cursor.execute(
            f"SELECT TOP 1 * FROM CAB_PRESTAMO WITH (UPDLOCK, HOLDLOCK) WHERE {where_sql}",
            where_params,
        )
        raw_locked = cursor.fetchone()
        if not raw_locked:
            raise ValueError("No se encontro el financiamiento seleccionado para actualizar.")
        locked_cab_row = _normalize_result_row([col[0] for col in cursor.description], raw_locked)
        loan_no_value = _stringify_doc(_pick_row_value(locked_cab_row, cab_no_col, default=""))
        main_doc_value = _stringify_doc(_pick_row_value(locked_cab_row, cab_doc_col, default=""))
    else:
        financed_docs = _load_financiamiento_factura_doc_set([factura_no])
        if factura_no in financed_docs:
            raise ValueError(f"La factura {factura_no} ya tiene un financiamiento registrado.")

    next_loan_no = None
    if not loan_no_value:
        loan_candidates = [
            candidate
            for candidate in _unique_columns(cab_no_col, "ID_PRESTAMO", "NO_PRESTAMO", "NO", "NO_DOC")
            if candidate and candidate in cab_columns and candidate not in cab_identity_columns
        ]
        if loan_candidates:
            next_loan_no = _next_table_numeric_value(cursor, "CAB_PRESTAMO", loan_candidates[0])
            loan_no_value = _stringify_doc(next_loan_no)

    if not main_doc_value:
        main_doc_value = loan_no_value or factura_no

    header_values = {}
    if cab_doc_col == "NO_DOC":
        header_values[cab_doc_col] = main_doc_value
    elif cab_doc_col in {"DOCUMENTO"}:
        header_values[cab_doc_col] = main_doc_value
    elif cab_doc_col in {"ID_DOC", "FACTURA"}:
        header_values[cab_doc_col] = factura_no

    if cab_fact_col and cab_fact_col != cab_doc_col:
        header_values[cab_fact_col] = factura_no
    elif cab_fact_col and cab_fact_col == cab_doc_col and cab_fact_col not in header_values:
        header_values[cab_fact_col] = factura_no

    if cab_no_col and loan_no_value and cab_no_col not in cab_identity_columns:
        header_values[cab_no_col] = loan_no_value

    if loan_no_value:
        _assign_existing_values(header_values, cab_columns, loan_no_value, "ID_PRESTAMO", "NO_PRESTAMO", "NO")
    if "NO_DOC" in cab_columns and "NO_DOC" not in header_values:
        header_values["NO_DOC"] = main_doc_value
    if "DOCUMENTO" in cab_columns and "DOCUMENTO" not in header_values:
        header_values["DOCUMENTO"] = main_doc_value
    _assign_existing_values(header_values, cab_columns, factura_no, "ID_DOC", "NO_FACT", "NO_FACTURA", "DOCUMENTO_BASE", "FACTURA_BASE", "FACTURA")
    _assign_existing_values(header_values, cab_columns, fecha_doc, "FECHA_DOC", "FECHA_CONT", "FECHA", "FECHA_APLIC")
    _assign_existing_values(header_values, cab_columns, fecha_base, "FECHA_BASE")
    _assign_existing_values(header_values, cab_columns, id_sn, "ID_SN", "CLIENTE", "COD_CLIENTE")
    _assign_existing_values(header_values, cab_columns, nombre_cliente, "NOM_SN", "NOM_SOCIO", "NOMBRE", "NOM_CLIENTE")
    _assign_existing_values(header_values, cab_columns, rnc_ced, "RNC_CED", "RNC", "CEDULA")
    _assign_existing_values(header_values, cab_columns, "Financiamiento", "TIPO", "TIPO_DOC", "CLASE_DOC")
    _assign_existing_values(header_values, cab_columns, "RD$", "MON_DOC", "MONEDA")
    _assign_existing_values(header_values, cab_columns, Decimal("1"), "TASAFACT", "TASA", "FACTOR", "TIPO_CAMBIO")
    _assign_existing_values(header_values, cab_columns, monto_principal, "TOTAL_DOC", "MONTO", "IMPORTE", "TOTAL", "CAPITAL", "CAPITAL_FINANCIADO")
    _assign_existing_values(header_values, cab_columns, saldo_total, "SALDO", "BALANCE", "SALDO_INSOLUTO")
    _assign_existing_values(header_values, cab_columns, Decimal("0"), "ABONO")
    _assign_existing_values(header_values, cab_columns, porc_interes, "PORC_INTERES", "PORC_INT", "INTERES_PORC", "PORCENTAJE_INTERES")
    _assign_existing_values(header_values, cab_columns, plazo, "PLAZO", "MESES", "NUM_CUOTAS", "CUOTAS")
    _assign_existing_values(header_values, cab_columns, metodo, "METODO", "METODO_CALC", "FORMA", "SISTEMA", "AMORTIZACION")
    _assign_existing_values(header_values, cab_columns, tipo_cuota, "TIPO_CUOTA", "TIPO_PAGO", "FRECUENCIA", "PERIODO")
    _assign_existing_values(header_values, cab_columns, valor_cuota, "CUOTA", "MONTO_CUOTA", "VALOR_CUOTA")
    _assign_existing_values(header_values, cab_columns, comentario_final, "COMENTARIO", "OBSERVACION")
    _assign_existing_values(header_values, cab_columns, "Abierto" if saldo_total > Decimal("0.01") else "Cerrado", "EST_DOC", "ESTATUS", "ESTADO")
    _assign_existing_values(header_values, cab_columns, usuario_id, "ID_USUARIO", "USUARIO_ID")
    _assign_existing_values(header_values, cab_columns, usuario_nombre, "USUARIO", "USUARIO_NOMBRE")
    _assign_existing_values(header_values, cab_columns, terminal, "TERMINAL")
    _assign_existing_values(header_values, cab_columns, timezone.localdate(), "FECHA_CREACION")
    _assign_existing_values(header_values, cab_columns, timezone.localtime(), "FECHA_ACT")
    _assign_existing_values(header_values, cab_columns, str(timezone.localdate().month), "PERIODO_CONT")
    _assign_existing_values(header_values, cab_columns, timezone.localdate().year, "EJERCICIO")

    if locked_cab_row:
        where_sql, where_params = _build_doc_lookup_where([cab_doc_col, cab_fact_col, cab_no_col], lookup_value)
        _update_dynamic_row(cursor, "CAB_PRESTAMO", header_values, where_sql, where_params)
    else:
        _insert_dynamic_row(
            cursor,
            "CAB_PRESTAMO",
            cab_columns,
            header_values,
            output_column=cab_no_col or cab_doc_col or cab_fact_col,
            skip_columns=cab_identity_columns,
        )

    det_doc_col = _pick_existing_column(det_columns, "NO_DOC", "ID_DOC", "DOCUMENTO", "FACTURA")
    det_line_col = _pick_existing_column(det_columns, "NO_LINEA", "LINEA", "NO_ITEM", "ORDEN")
    det_cuota_num_col = _pick_existing_column(det_columns, "NO_CUOTA", "CUOTA_NUM", "NUM_CUOTA")
    det_balance_col = _pick_existing_column(det_columns, "BALANCE")
    det_abono_cuota_col = _pick_existing_column(det_columns, "ABONO_CUOTA", "ABONOCUOTA", "ABONO_CUENTA", "ABONOCUENTA")
    det_no_recibo_col = _pick_existing_column(det_columns, "NORECIBO", "NO_RECIBO")
    if not det_doc_col or not det_cuota_num_col:
        raise ValueError("No se pudo determinar la estructura del detalle del financiamiento.")

    where_sql, where_params = _build_doc_lookup_where([det_doc_col], factura_no)
    cursor.execute(f"DELETE FROM DET_PRESTAMO WHERE {where_sql}", where_params)

    for row in detail_rows:
        detail_values = {}
        _assign_existing_values(detail_values, det_columns, factura_no, "NO_DOC", "ID_DOC", "DOCUMENTO", "FACTURA")
        if loan_no_value:
            _assign_existing_values(detail_values, det_columns, loan_no_value, "ID_PRESTAMO", "NO_PRESTAMO", "NO")
        if det_line_col and det_line_col not in det_identity_columns:
            _assign_existing_values(detail_values, det_columns, row["linea"], det_line_col)
        _assign_existing_values(detail_values, det_columns, row["no_cuota"], "NO_CUOTA", "CUOTA_NUM", "NUM_CUOTA")
        _assign_existing_values(detail_values, det_columns, fecha_doc, "FECHA", "FECHA_DOC", "FECHA_CONT")
        _assign_existing_values(detail_values, det_columns, row["fecha_venc"], "FECHA_VENC", "F_VENC", "VENCIMIENTO")
        _assign_existing_values(detail_values, det_columns, row["monto_interes"], "MONTO_INTERES", "MTO_INTERES", "INTERES", "IMP_INTERES", "INT")
        _assign_existing_values(detail_values, det_columns, row["capital"], "CAPITAL", "MONTO_CAPITAL", "CAPITAL_CUOTA")
        _assign_existing_values(detail_values, det_columns, row["saldo_insoluto"], "SALDO_INSOLUTO")
        _assign_existing_values(detail_values, det_columns, row["cuota"], "CUOTA", "MONTO_CUOTA", "VALOR_CUOTA")
        if det_balance_col:
            detail_values[det_balance_col] = row["balance"]
        if det_abono_cuota_col:
            detail_values[det_abono_cuota_col] = row["abono_cuota"]
        if det_no_recibo_col:
            detail_values[det_no_recibo_col] = None
        _assign_existing_values(detail_values, det_columns, row["balance"], "PENDIENTE", "SALDO")
        _assign_existing_values(detail_values, det_columns, Decimal("0"), "PAGADO", "ABONO")
        _assign_existing_values(detail_values, det_columns, id_sn, "ID_SN", "CLIENTE", "COD_CLIENTE")
        _assign_existing_values(detail_values, det_columns, nombre_cliente, "NOM_SN", "NOM_SOCIO", "NOMBRE", "NOM_CLIENTE")
        _assign_existing_values(detail_values, det_columns, rnc_ced, "RNC_CED", "RNC", "CEDULA")
        _assign_existing_values(detail_values, det_columns, "Financiamiento", "TIPO", "TIPO_DOC", "CLASE_DOC")
        _assign_existing_values(detail_values, det_columns, comentario_final, "COMENTARIO", "OBSERVACION")
        _assign_existing_values(detail_values, det_columns, "Abierto", "EST_DOC", "ESTATUS", "ESTADO")
        _assign_existing_values(detail_values, det_columns, usuario_id, "ID_USUARIO", "USUARIO_ID")
        _assign_existing_values(detail_values, det_columns, usuario_nombre, "USUARIO", "USUARIO_NOMBRE")
        _assign_existing_values(detail_values, det_columns, terminal, "TERMINAL")
        _assign_existing_values(detail_values, det_columns, timezone.localdate(), "FECHA_CREACION")
        _assign_existing_values(detail_values, det_columns, timezone.localtime(), "FECHA_ACT")
        _insert_dynamic_row(
            cursor,
            "DET_PRESTAMO",
            det_columns,
            detail_values,
            skip_columns=det_identity_columns,
        )

    factura_columns = factura_base.get("columns") or []
    factura_doc_col = str(factura_base.get("doc_col") or "").upper()
    factura_updates = {}
    _assign_existing_values(factura_updates, factura_columns, monto_principal, "MONTO_FTO")
    _assign_existing_values(factura_updates, factura_columns, plazo, "PLAZO")
    _assign_existing_values(factura_updates, factura_columns, porc_interes, "INTERES")
    _assign_existing_values(factura_updates, factura_columns, metodo, "METODO")
    _assign_existing_values(factura_updates, factura_columns, tipo_cuota, "FORMA")
    _assign_existing_values(factura_updates, factura_columns, valor_cuota, "CUOTA")
    _assign_existing_values(factura_updates, factura_columns, "Y", "FINANCIADO")
    if factura_updates and factura_doc_col:
        factura_where_sql, factura_where_params = _build_doc_lookup_where([factura_doc_col], factura_no)
        _update_dynamic_row(cursor, "CAB_FACTURA", factura_updates, factura_where_sql, factura_where_params)

    return {
        "lookup": main_doc_value or factura_no,
        "factura_no": factura_no,
        "loan_no": loan_no_value,
        "created": not bool(locked_cab_row),
    }

@ensure_csrf_cookie
def cuentas_por_cobrar_view(request):
    ctx = _base_context(request, page_title="Caja - Cuentas por cobrar", active_nav="caja")
    if not ctx:
        return redirect("login")
    if not has_perm(ctx["auth_payload"]["usuario_id"], "caja", "ver_cuentas_por_cobrar"):
        return render_denied(request, active_nav="caja")
    usuario_id = ctx["auth_payload"]["usuario_id"]
    ctx["cxc_permissions"] = {
        "nuevo": has_perm(usuario_id, "caja", "cxc_nuevo"),
        "buscar": has_perm(usuario_id, "caja", "cxc_buscar"),
        "imprimir": has_perm(usuario_id, "caja", "cxc_imprimir"),
        "cancelar": has_perm(usuario_id, "caja", "cxc_cancelar"),
        "cerrar_cuenta": has_perm(usuario_id, "caja", "cxc_cerrar_cuenta"),
        "modificar_medio_pago": has_perm(usuario_id, "caja", "cxc_modificar_medio_pago"),
        "corregir_monto_pago": has_perm(usuario_id, "caja", "cxc_corregir_monto_pago"),
    }
    ctx["cxc_shortcuts"] = {
        "financiamiento": has_perm(usuario_id, "caja", "ver_financiamiento"),
        "factura": has_perm(usuario_id, "factura", "ver_documentos"),
        "prefactura": has_perm(usuario_id, "prefacturas", "ver"),
    }
    ctx["cxc_default_payment_method"] = _load_cxc_default_payment_method(usuario_id)
    cxc_print_format = get_print_format("recibo_pago")
    ctx["cxc_print_format"] = cxc_print_format
    ctx["cxc_print_format_label"] = get_print_format_label(cxc_print_format)
    return render(request, "caja/cuentas_por_cobrar.html", ctx)


@require_GET
def cuentas_por_cobrar_pendientes_view(request):
    auth_payload = _require_perm_json(request, "caja", "cxc_nuevo")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    id_sn = (request.GET.get("id_sn") or "").strip()
    if not id_sn:
        return JsonResponse({"detail": "Parametro id_sn requerido"}, status=400)

    try:
        results = _load_cxc_pendientes(id_sn)
    except Exception:
        return JsonResponse({"detail": "No se pudieron cargar las facturas pendientes."}, status=500)

    return JsonResponse({"results": results})


@require_GET
def cuentas_por_cobrar_buscar_view(request):
    auth_payload = _require_perm_json(request, "caja", "cxc_buscar")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    query = (request.GET.get("q") or "").strip()
    filtro = (request.GET.get("filtro") or "recibo").strip().lower()

    try:
        results = _load_cxc_recibos_busqueda(query=query, filtro=filtro)
    except Exception:
        return JsonResponse({"detail": "No se pudieron cargar los recibos registrados."}, status=500)

    return JsonResponse({"results": results})


@require_GET
def cuentas_por_cobrar_detalle_view(request):
    auth_payload = _require_any_caja_perm_json(request, "cxc_buscar", "cxc_nuevo", "cxc_imprimir", "cxc_cancelar")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    recibo_id = (request.GET.get("recibo_id") or "").strip()
    if not recibo_id:
        return JsonResponse({"detail": "Parametro recibo_id requerido"}, status=400)

    try:
        record = _load_cxc_recibo_detalle(recibo_id)
    except Exception:
        return JsonResponse({"detail": "No se pudo cargar el recibo seleccionado."}, status=500)

    if not record:
        return JsonResponse({"detail": "No se encontro el recibo solicitado."}, status=404)

    return JsonResponse({"record": record})


@require_GET
def cuentas_por_cobrar_print_data_view(request):
    auth_payload = _require_perm_json(request, "caja", "cxc_imprimir")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    recibo_id = (request.GET.get("recibo_id") or "").strip()
    if not recibo_id:
        return JsonResponse({"detail": "Parametro recibo_id requerido"}, status=400)

    try:
        record = _load_cxc_recibo_detalle(recibo_id)
    except Exception:
        return JsonResponse({"detail": "No se pudo cargar el recibo para imprimir."}, status=500)

    if not record:
        return JsonResponse({"detail": "No se encontro el recibo solicitado."}, status=404)

    return JsonResponse({"print_data": _build_cxc_recibo_print_payload(record, auth_payload)})


@require_GET
def cuentas_por_cobrar_pdf_view(request):
    auth_payload = _require_perm_json(request, "caja", "cxc_imprimir")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    recibo_id = (request.GET.get("recibo_id") or "").strip()
    if not recibo_id:
        return JsonResponse({"detail": "Parametro recibo_id requerido"}, status=400)

    try:
        copies = max(1, min(int((request.GET.get("copies") or "1").strip() or "1"), 20))
    except (TypeError, ValueError):
        copies = 1

    try:
        record = _load_cxc_recibo_detalle(recibo_id)
    except Exception:
        return JsonResponse({"detail": "No se pudo cargar el recibo para generar PDF."}, status=500)

    if not record:
        return JsonResponse({"detail": "No se encontro el recibo solicitado."}, status=404)

    try:
        print_payload = _build_cxc_recibo_print_payload(record, auth_payload)
        pdf_bytes = _render_cxc_receipt_pdf_via_browser(print_payload, copies=copies)
    except Exception as exc:
        return JsonResponse({"detail": f"No se pudo generar el PDF del recibo: {exc}"}, status=500)

    no_recibo = _stringify_doc((print_payload.get("documento") or {}).get("no_recibo")) or recibo_id
    safe_filename = f"Recibo-{no_recibo}.pdf".replace('"', "").replace("\n", " ").replace("\r", " ")
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="{safe_filename}"'
    return response


@require_http_methods(["POST"])
def cuentas_por_cobrar_marcar_impreso_view(request):
    auth_payload = _require_perm_json(request, "caja", "cxc_imprimir")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"detail": "JSON invalido"}, status=400)

    recibo_id = str(payload.get("recibo_id") or "").strip()
    if not recibo_id:
        return JsonResponse({"detail": "Parametro recibo_id requerido"}, status=400)

    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cab_columns = _load_table_columns("CAB_RECIBO_INGRESO")
                cab_key_col = _pick_existing_column(cab_columns, "ID_RECIBO", "NO_RECIBO", "ID_DOC", "NO_DOC")
                cab_no_recibo_col = _pick_existing_column(cab_columns, "NO_RECIBO", "ID_RECIBO", "ID_DOC", "NO_DOC")
                impreso_col = _pick_existing_column(cab_columns, "IMPRESO")
                if not cab_key_col or not impreso_col:
                    return JsonResponse({"detail": "No se pudo actualizar el estado de impresion."}, status=500)

                where_parts = [f"CAST([{cab_key_col}] AS NVARCHAR(255)) = %s"]
                where_params = [recibo_id]
                if cab_no_recibo_col and cab_no_recibo_col != cab_key_col:
                    where_parts.append(f"CAST([{cab_no_recibo_col}] AS NVARCHAR(255)) = %s")
                    where_params.append(recibo_id)

                updated = _update_dynamic_row(
                    cursor,
                    "CAB_RECIBO_INGRESO",
                    {impreso_col: "Y"},
                    " OR ".join(where_parts),
                    where_params,
                )
                if updated <= 0:
                    return JsonResponse({"detail": "No se encontro el recibo para actualizar impresion."}, status=404)
    except Exception as exc:
        return JsonResponse({"detail": f"No se pudo actualizar impresion: {exc}"}, status=500)

    return JsonResponse({"ok": True})


@require_http_methods(["POST"])
def cuentas_por_cobrar_medio_pago_view(request):
    auth_payload = _require_perm_json(request, "caja", "cxc_modificar_medio_pago")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"detail": "JSON invalido"}, status=400)

    recibo_id = str(payload.get("recibo_id") or "").strip()
    if not recibo_id:
        return JsonResponse({"detail": "Parametro recibo_id requerido"}, status=400)

    efectivo = max(_to_decimal(payload.get("efectivo")), Decimal("0"))
    transferencia = max(_to_decimal(payload.get("transferencia")), Decimal("0"))
    total_metodos = efectivo + transferencia
    client_event_id = str(payload.get("event_id") or "").strip()
    recibo_id_real = ""
    no_recibo = ""

    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cab_columns = _load_table_columns("CAB_RECIBO_INGRESO")
                cab_key_col = _pick_existing_column(cab_columns, "ID_RECIBO", "ID_DOC", "NO_RECIBO", "NO_DOC")
                cab_no_recibo_col = _pick_existing_column(cab_columns, "NO_RECIBO", "NO_DOC", "ID_RECIBO", "ID_DOC")
                efectivo_col = _pick_existing_column(cab_columns, "IMP_EFECTIVO", "EFECTIVO", "MONTO_EFECTIVO", "PAGO_EFECTIVO")
                transferencia_col = _pick_existing_column(
                    cab_columns,
                    "IMP_TRANSF",
                    "TRANSFERENCIA",
                    "MONTO_TRANSFERENCIA",
                    "PAGO_TRANSFERENCIA",
                )
                total_col = _pick_existing_column(cab_columns, "TOTAL_COBRO", "TOTAL_DOC", "IMPORTE", "MONTO")
                if not cab_key_col or not efectivo_col or not transferencia_col:
                    return JsonResponse({"detail": "No se pudo determinar la estructura del medio de pago."}, status=500)

                where_parts = [f"CAST([{cab_key_col}] AS NVARCHAR(255)) = %s"]
                where_params = [recibo_id]
                if cab_no_recibo_col and cab_no_recibo_col != cab_key_col:
                    where_parts.append(f"CAST([{cab_no_recibo_col}] AS NVARCHAR(255)) = %s")
                    where_params.append(recibo_id)

                cursor.execute(
                    f"""
                    SELECT TOP 1 *
                    FROM CAB_RECIBO_INGRESO WITH (UPDLOCK, HOLDLOCK)
                    WHERE {' OR '.join(f'({part})' for part in where_parts)}
                    """,
                    where_params,
                )
                raw_header = cursor.fetchone()
                if not raw_header:
                    return JsonResponse({"detail": "No se encontro el recibo para actualizar medio de pago."}, status=404)
                header_row = _normalize_result_row([col[0] for col in cursor.description], raw_header)

                estado_actual = _pick_row_text(header_row, "EST_DOC", "ESTATUS", "ESTADO").strip().upper()
                cancelado_actual = _pick_row_text(header_row, "CANCELADO").strip().upper()
                if estado_actual == "CANCELADO" or cancelado_actual == "Y":
                    return JsonResponse({"detail": "No se puede modificar el medio de pago de un recibo cancelado."}, status=400)

                original_efectivo = _to_decimal(_pick_row_value(header_row, efectivo_col, default=0))
                original_transferencia = _to_decimal(_pick_row_value(header_row, transferencia_col, default=0))
                original_total = original_efectivo + original_transferencia
                total_recibo = _to_decimal(_pick_row_value(header_row, total_col, default=original_total))
                total_requerido = original_total if original_total > Decimal("0.01") else total_recibo
                if total_requerido <= Decimal("0.01"):
                    return JsonResponse({"detail": "Este recibo no tiene monto de pago para modificar."}, status=400)
                if not _values_match(total_metodos, total_requerido):
                    return JsonResponse(
                        {"detail": "La suma de efectivo y transferencia debe ser igual al monto original a pagar."},
                        status=400,
                    )
                if _values_match(efectivo, original_efectivo) and _values_match(transferencia, original_transferencia):
                    return JsonResponse({"detail": "No se detectaron cambios en el medio de pago."}, status=400)

                recibo_id_real = _stringify_doc(_pick_row_value(header_row, cab_key_col, cab_no_recibo_col))
                no_recibo = _stringify_doc(_pick_row_value(header_row, cab_no_recibo_col, cab_key_col))
                updates = {
                    efectivo_col: efectivo,
                    transferencia_col: transferencia,
                }
                _assign_existing_values(updates, cab_columns, timezone.localtime(), "FECHA_ACT")
                updated = _update_dynamic_row(
                    cursor,
                    "CAB_RECIBO_INGRESO",
                    updates,
                    f"CAST([{cab_key_col}] AS NVARCHAR(255)) = %s",
                    [recibo_id_real or recibo_id],
                )
                if updated <= 0:
                    return JsonResponse({"detail": "No se pudo actualizar el medio de pago."}, status=500)

                transaction.on_commit(
                    lambda rid=recibo_id_real or recibo_id, nro=no_recibo or recibo_id, estado=estado_actual, eid=client_event_id: broadcast_cxc_document_status(
                        document_id=rid,
                        no_recibo=nro,
                        estado=estado,
                        reason="medio-pago-updated",
                        event_id=eid,
                    )
                )
    except Exception as exc:
        return JsonResponse({"detail": f"No se pudo actualizar el medio de pago: {exc}"}, status=500)

    return JsonResponse({"ok": True, "recibo_id": recibo_id_real or recibo_id, "no_recibo": no_recibo or recibo_id})


@require_http_methods(["POST"])
def cuentas_por_cobrar_corregir_monto_view(request):
    auth_payload = _require_perm_json(request, "caja", "cxc_corregir_monto_pago")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"detail": "JSON invalido"}, status=400)

    recibo_id = str(payload.get("recibo_id") or "").strip()
    raw_detail = payload.get("detail") or []
    if not recibo_id:
        return JsonResponse({"detail": "Parametro recibo_id requerido"}, status=400)
    if not isinstance(raw_detail, list) or not raw_detail:
        return JsonResponse({"detail": "Debes indicar el detalle corregido."}, status=400)

    requested = {}
    for item in raw_detail:
        if not isinstance(item, dict):
            continue
        key = (
            str(item.get("linea") or "").strip(),
            str(item.get("no_doc") or "").strip(),
            str(item.get("no_cuota") or "").strip() or "1",
        )
        if not key[0] and not key[1]:
            continue
        requested[key] = max(_to_decimal(item.get("pago_abono")), Decimal("0"))
    if not requested:
        return JsonResponse({"detail": "No se recibieron montos validos para corregir."}, status=400)

    efectivo = max(_to_decimal(payload.get("efectivo")), Decimal("0"))
    transferencia = max(_to_decimal(payload.get("transferencia")), Decimal("0"))
    client_event_id = str(payload.get("event_id") or "").strip()
    recibo_id_real = ""
    no_recibo = ""
    usuario_modificacion = (
        str((auth_payload or {}).get("usuario_nombre") or "").strip()
        or str((auth_payload or {}).get("usuario") or "").strip()
        or str((auth_payload or {}).get("usuario_id") or "").strip()
    )[:120]

    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cab_columns = _load_table_columns("CAB_RECIBO_INGRESO")
                det_columns = _load_table_columns("DET_RECIBO_INGRESO")
                cab_key_col = _pick_existing_column(cab_columns, "ID_RECIBO", "ID_DOC", "NO_RECIBO", "NO_DOC")
                cab_no_recibo_col = _pick_existing_column(cab_columns, "NO_RECIBO", "NO_DOC", "ID_RECIBO", "ID_DOC")
                det_key_col = _pick_existing_column(det_columns, "ID_RECIBO", "NO_RECIBO", "ID_DOC", "NO_DOC")
                det_no_recibo_col = _pick_existing_column(det_columns, "NO_RECIBO", "ID_RECIBO", "ID_DOC", "NO_DOC")
                det_line_col = _pick_existing_column(det_columns, "NO_LINEA", "LINEA", "NO_ITEM", "ORDEN")
                det_doc_col = _pick_existing_column(det_columns, "NO_DOC", "ID_DOC", "DOCUMENTO", "FACTURA")
                det_cuota_col = _pick_existing_column(det_columns, "NO_CUOTA", "CUOTA_NUM", "NUM_CUOTA")
                if not cab_key_col or not det_key_col:
                    return JsonResponse({"detail": "No se pudo determinar la estructura del recibo."}, status=500)

                header_where_parts = [f"CAST([{cab_key_col}] AS NVARCHAR(255)) = %s"]
                header_where_params = [recibo_id]
                if cab_no_recibo_col and cab_no_recibo_col != cab_key_col:
                    header_where_parts.append(f"CAST([{cab_no_recibo_col}] AS NVARCHAR(255)) = %s")
                    header_where_params.append(recibo_id)
                cursor.execute(
                    f"""
                    SELECT TOP 1 *
                    FROM CAB_RECIBO_INGRESO WITH (UPDLOCK, HOLDLOCK)
                    WHERE {' OR '.join(f'({part})' for part in header_where_parts)}
                    """,
                    header_where_params,
                )
                raw_header = cursor.fetchone()
                if not raw_header:
                    return JsonResponse({"detail": "No se encontro el recibo para corregir."}, status=404)
                header_row = _normalize_result_row([col[0] for col in cursor.description], raw_header)

                estado_actual = _pick_row_text(header_row, "EST_DOC", "ESTATUS", "ESTADO").strip().upper()
                cancelado_actual = _pick_row_text(header_row, "CANCELADO").strip().upper()
                if estado_actual != "ABIERTO" or cancelado_actual == "Y":
                    return JsonResponse({"detail": "Solo se pueden corregir recibos abiertos."}, status=400)

                recibo_id_real = _stringify_doc(_pick_row_value(header_row, cab_key_col, cab_no_recibo_col))
                no_recibo = _stringify_doc(_pick_row_value(header_row, cab_no_recibo_col, cab_key_col))
                detail_lookup_value = (
                    no_recibo
                    if det_no_recibo_col and det_key_col == det_no_recibo_col and no_recibo
                    else recibo_id_real or recibo_id
                )
                detail_where_parts = [f"CAST([{det_key_col}] AS NVARCHAR(255)) = %s"]
                detail_where_params = [detail_lookup_value]
                if det_no_recibo_col and det_no_recibo_col != det_key_col and no_recibo:
                    detail_where_parts.append(f"CAST([{det_no_recibo_col}] AS NVARCHAR(255)) = %s")
                    detail_where_params.append(no_recibo)

                detail_sql = f"SELECT * FROM DET_RECIBO_INGRESO WITH (UPDLOCK, HOLDLOCK) WHERE {' OR '.join(f'({part})' for part in detail_where_parts)}"
                if det_line_col:
                    detail_sql += f" ORDER BY [{det_line_col}]"
                cursor.execute(detail_sql, detail_where_params)
                detail_columns = [col[0] for col in cursor.description]
                detail_rows = [_normalize_result_row(detail_columns, raw_row) for raw_row in cursor.fetchall()]
                if not detail_rows:
                    return JsonResponse({"detail": "El recibo no tiene detalle para corregir."}, status=400)

                old_total_pago = Decimal("0")
                new_total_pago = Decimal("0")
                total_mora = Decimal("0")
                total_desc = Decimal("0")
                total_ret = Decimal("0")
                old_applied_by_doc = {}
                new_applied_by_doc = {}
                corrected_rows = []
                prestamo_docs_actualizados = set()

                for row in detail_rows:
                    line_value = _stringify_doc(_pick_row_value(row, det_line_col, default="")) if det_line_col else ""
                    no_doc = _stringify_doc(_pick_row_value(row, "NO_DOC", "ID_DOC", "DOCUMENTO", "FACTURA"))
                    no_cuota = _stringify_doc(_pick_row_value(row, "NO_CUOTA", "CUOTA_NUM", "NUM_CUOTA", default="1")) or "1"
                    key = (line_value, no_doc, no_cuota)
                    fallback_key = ("", no_doc, no_cuota)
                    old_pago = _to_decimal(
                        _pick_amount_value(
                            row,
                            "TOTAL_PAGO",
                            "TOTAL_PAGO2",
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
                    desc = _get_det_recibo_discount_amount(row)
                    cargo = _to_decimal(_pick_row_value(row, "CARGO", "MORA", "TOTAL_MORA", default=0))
                    ret = _to_decimal(_pick_row_value(row, "TOTAL_RET", "RETENCION", "RET", default=0))
                    new_pago = requested.get(key, requested.get(fallback_key, old_pago))
                    balance_doc = _to_decimal(
                        _pick_row_value(row, "BALANCE_DOC", "BALANCE", "SALDO_DOC", "SALDO", "MONTO_DOC", "CUOTA", default=old_pago + desc)
                    )
                    if balance_doc <= Decimal("0"):
                        balance_doc = old_pago + desc
                    max_pago_line = max(balance_doc - desc, Decimal("0"))
                    if new_pago > max_pago_line + Decimal("0.01"):
                        return JsonResponse(
                            {"detail": f"El monto corregido excede el balance de la linea {no_doc} cuota {no_cuota}."},
                            status=400,
                        )

                    old_total_pago += old_pago
                    new_total_pago += new_pago
                    total_mora += cargo
                    total_desc += desc
                    total_ret += ret
                    old_applied_by_doc[no_doc] = old_applied_by_doc.get(no_doc, Decimal("0")) + old_pago + desc
                    new_applied_by_doc[no_doc] = new_applied_by_doc.get(no_doc, Decimal("0")) + new_pago + desc
                    corrected_rows.append(
                        {
                            "row": row,
                            "linea": line_value,
                            "no_doc": no_doc,
                            "no_cuota": no_cuota,
                            "old_pago": old_pago,
                            "new_pago": new_pago,
                            "desc": desc,
                            "balance_doc": balance_doc,
                        }
                    )

                if new_total_pago <= Decimal("0.01"):
                    return JsonResponse({"detail": "El recibo debe conservar un monto pagado mayor a 0."}, status=400)

                new_cash_total = max(new_total_pago + total_mora - total_ret, Decimal("0"))
                new_ed_total = _resolve_cxc_ed_total(new_cash_total, total_desc)
                new_ed_total_letra = _amount_to_spanish_words(new_ed_total)
                if not _values_match(efectivo + transferencia, new_cash_total):
                    return JsonResponse(
                        {"detail": "La suma de efectivo y transferencia debe coincidir con el nuevo monto a pagar."},
                        status=400,
                    )

                factura_columns = _load_table_columns("CAB_FACTURA")
                factura_has_abono = "ABONO" in factura_columns
                factura_has_fecha_act = "FECHA_ACT" in factura_columns
                doc_numbers = [doc for doc in old_applied_by_doc.keys() if doc]
                if not doc_numbers:
                    return JsonResponse({"detail": "No se pudo identificar las facturas del recibo."}, status=400)
                placeholders = ", ".join(["%s"] * len(doc_numbers))
                cursor.execute(
                    f"""
                    SELECT ID_DOC, SALDO, TOTAL_DOC, EST_DOC, ABONO
                    FROM CAB_FACTURA WITH (UPDLOCK, HOLDLOCK)
                    WHERE CAST(ID_DOC AS VARCHAR(50)) IN ({placeholders})
                    """,
                    doc_numbers,
                )
                factura_lookup = {
                    _stringify_doc(row[0]): {
                        "saldo": _to_decimal(row[1]),
                        "total_doc": _to_decimal(row[2]),
                        "est_doc": str(row[3] or "").strip(),
                        "abono": _to_decimal(row[4]),
                    }
                    for row in cursor.fetchall()
                }
                for doc_number in doc_numbers:
                    factura_row = factura_lookup.get(doc_number)
                    if not factura_row:
                        return JsonResponse({"detail": f"No se encontro la factura {doc_number}."}, status=400)
                    available = max(_to_decimal(factura_row.get("saldo")) + old_applied_by_doc.get(doc_number, Decimal("0")), Decimal("0"))
                    total_factura = max(_to_decimal(factura_row.get("total_doc")), Decimal("0"))
                    if total_factura > Decimal("0"):
                        available = min(available, total_factura)
                    if new_applied_by_doc.get(doc_number, Decimal("0")) > available + Decimal("0.01"):
                        return JsonResponse(
                            {"detail": f"El monto corregido de la factura {doc_number} excede el saldo disponible actual."},
                            status=400,
                        )

                now = timezone.localtime()
                total_letra = _amount_to_spanish_words(new_cash_total)
                header_updates = {}
                _assign_existing_values(header_updates, cab_columns, efectivo, "IMP_EFECTIVO", "EFECTIVO", "MONTO_EFECTIVO", "PAGO_EFECTIVO")
                _assign_existing_values(header_updates, cab_columns, transferencia, "IMP_TRANSF", "TRANSFERENCIA", "MONTO_TRANSFERENCIA", "PAGO_TRANSFERENCIA")
                _assign_existing_values(header_updates, cab_columns, new_cash_total, "TOTAL_COBRO", "TOTAL_DOC", "IMPORTE", "MONTO")
                _assign_existing_values(header_updates, cab_columns, total_mora, "TOTAL_MORA", "MORA", "CARGO")
                _assign_existing_values(header_updates, cab_columns, total_desc, "TOTAL_DESCTO", "DESC_AVANCE", "DESCUENTO", "AVANCE")
                _assign_existing_values(header_updates, cab_columns, total_ret, "TOTAL_RET", "RETENCION", "RET")
                _assign_existing_values(header_updates, cab_columns, total_letra, "TOTAL_LETRA")
                _assign_existing_values(header_updates, cab_columns, usuario_modificacion, "USUARIO_MODIF_MONTO")
                _assign_existing_values(header_updates, cab_columns, now, "FECHA_MODIF_MONTO")
                _assign_existing_values(header_updates, cab_columns, now, "FECHA_ACT")
                _update_dynamic_row(
                    cursor,
                    "CAB_RECIBO_INGRESO",
                    header_updates,
                    f"CAST([{cab_key_col}] AS NVARCHAR(255)) = %s",
                    [recibo_id_real or recibo_id],
                )

                for item in corrected_rows:
                    row = item["row"]
                    new_pago = item["new_pago"]
                    desc = item["desc"]
                    balance_doc = item["balance_doc"]
                    total_recibo_linea = max(balance_doc - new_pago, Decimal("0"))
                    saldo_venc_linea = new_pago + total_recibo_linea + desc
                    det_updates = {}
                    _assign_existing_values(
                        det_updates,
                        det_columns,
                        new_pago,
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
                    )
                    _assign_existing_values(det_updates, det_columns, new_pago, "TOTAL_PAGO")
                    _assign_existing_values(det_updates, det_columns, new_pago, "TOTAL_PAGO2")
                    _assign_existing_values(det_updates, det_columns, saldo_venc_linea, "SALDO_VENC")
                    _assign_existing_values(det_updates, det_columns, total_recibo_linea, "TOTAL_RECIBO")
                    _assign_existing_values(det_updates, det_columns, total_recibo_linea, "TOTAL_RECIBO2")
                    _assign_existing_values(det_updates, det_columns, max(balance_doc - new_pago - desc, Decimal("0")), "BALANCE_PEND", "SALDO_PEND", "PENDIENTE")
                    where_parts = [f"CAST([{det_key_col}] AS NVARCHAR(255)) = %s"]
                    where_params = [detail_lookup_value]
                    if det_line_col and item["linea"]:
                        where_parts.append(f"CAST([{det_line_col}] AS NVARCHAR(255)) = %s")
                        where_params.append(item["linea"])
                    else:
                        if not det_doc_col or not det_cuota_col:
                            return JsonResponse({"detail": "No se pudo identificar la linea del detalle a corregir."}, status=500)
                        where_parts.extend([
                            f"CAST([{det_doc_col}] AS NVARCHAR(255)) = %s",
                            f"CAST([{det_cuota_col}] AS NVARCHAR(255)) = %s",
                        ])
                        where_params.extend([item["no_doc"], item["no_cuota"]])
                    _update_dynamic_row(cursor, "DET_RECIBO_INGRESO", det_updates, " AND ".join(where_parts), where_params)

                for doc_number in doc_numbers:
                    factura_row = factura_lookup.get(doc_number) or {}
                    old_applied = old_applied_by_doc.get(doc_number, Decimal("0"))
                    new_applied = new_applied_by_doc.get(doc_number, Decimal("0"))
                    total_factura = max(_to_decimal(factura_row.get("total_doc")), Decimal("0"))
                    new_saldo = max(_to_decimal(factura_row.get("saldo")) + old_applied - new_applied, Decimal("0"))
                    if total_factura > Decimal("0"):
                        new_saldo = min(new_saldo, total_factura)
                    factura_updates = {"SALDO": new_saldo, "EST_DOC": "CERRADO" if new_saldo <= Decimal("0.01") else "ABIERTO"}
                    if factura_has_abono:
                        new_abono = max(_to_decimal(factura_row.get("abono")) - old_applied + new_applied, Decimal("0"))
                        if total_factura > Decimal("0"):
                            new_abono = min(new_abono, total_factura)
                        factura_updates["ABONO"] = new_abono
                    if factura_has_fecha_act:
                        factura_updates["FECHA_ACT"] = now
                    _update_dynamic_row(
                        cursor,
                        "CAB_FACTURA",
                        factura_updates,
                        "CAST(ID_DOC AS VARCHAR(50)) = %s",
                        [doc_number],
                    )

                prestamo_columns = _load_table_columns("DET_PRESTAMO")
                prestamo_doc_col = _pick_existing_column(prestamo_columns, "NO_DOC", "ID_DOC", "DOCUMENTO", "FACTURA")
                prestamo_cuota_col = _pick_existing_column(prestamo_columns, "NO_CUOTA", "CUOTA_NUM", "NUM_CUOTA")
                if prestamo_doc_col and prestamo_cuota_col:
                    for item in corrected_rows:
                        if not item["no_doc"] or not item["no_cuota"]:
                            continue
                        cursor.execute(
                            f"""
                            SELECT TOP 1 1
                            FROM DET_PRESTAMO WITH (UPDLOCK, HOLDLOCK)
                            WHERE CAST([{prestamo_doc_col}] AS NVARCHAR(255)) = %s
                              AND CAST([{prestamo_cuota_col}] AS NVARCHAR(255)) = %s
                            """,
                            [item["no_doc"], item["no_cuota"]],
                        )
                        if cursor.fetchone():
                            prestamo_docs_actualizados.add(item["no_doc"])
                    for prestamo_doc in prestamo_docs_actualizados:
                        _rebuild_det_prestamo_from_active_receipts(cursor, prestamo_doc)
                        _sync_cab_prestamo_from_det(cursor, prestamo_doc, now=now)

                old_cash_total = _to_decimal(_pick_row_value(header_row, "TOTAL_COBRO", "TOTAL_DOC", "IMPORTE", "MONTO", default=old_total_pago))
                delta_total = new_cash_total - old_cash_total
                if abs(delta_total) > Decimal("0.01"):
                    _adjust_catalogo_saldo_actual(
                        cursor,
                        cuenta_num="11020101",
                        cuenta_nombre="Cuentas por Cobrar Clientes",
                        delta=delta_total,
                    )

                cab_ed_columns = _load_table_columns("CAB_ED")
                det_ed_columns = _load_table_columns("DET_ED")
                cab_ed_key_col = _pick_existing_column(cab_ed_columns, "ID_DOC", "ID_ED", "NO_DOC", "NO_ED")
                cab_ed_no_col = _pick_existing_column(cab_ed_columns, "NO_DOC", "NO_ED", "ID_DOC", "ID_ED")
                cab_ed_tipo_col = _pick_existing_column(cab_ed_columns, "TIPO_DOC", "TD", "CLASE_DOC", "TIPO")
                cab_ed_origen_col = _pick_existing_column(cab_ed_columns, "ORIGEN", "REFERENCIA", "NO_RECIBO")
                cab_ed_status_col = _pick_existing_column(cab_ed_columns, "EST_DOC", "ESTADO", "ESTATUS")
                if cab_ed_origen_col and (cab_ed_key_col or cab_ed_no_col):
                    ed_where_parts = [f"CAST([{cab_ed_origen_col}] AS NVARCHAR(255)) = %s"]
                    ed_where_params = [no_recibo or recibo_id_real or recibo_id]
                    if recibo_id_real and recibo_id_real != no_recibo:
                        ed_where_parts.append(f"CAST([{cab_ed_origen_col}] AS NVARCHAR(255)) = %s")
                        ed_where_params.append(recibo_id_real)
                    ed_filter = [f"({' OR '.join(f'({part})' for part in ed_where_parts)})"]
                    if cab_ed_tipo_col:
                        ed_filter.append(f"UPPER(LTRIM(RTRIM(ISNULL([{cab_ed_tipo_col}], '')))) = 'RI'")
                    if cab_ed_status_col:
                        ed_filter.append(f"UPPER(LTRIM(RTRIM(ISNULL([{cab_ed_status_col}], '')))) <> 'CANCELADO'")
                    cursor.execute(
                        f"SELECT TOP 1 * FROM CAB_ED WITH (UPDLOCK, HOLDLOCK) WHERE {' AND '.join(ed_filter)} ORDER BY [{cab_ed_no_col or cab_ed_key_col}] DESC",
                        ed_where_params,
                    )
                    raw_ed = cursor.fetchone()
                    if raw_ed:
                        ed_row = _normalize_result_row([col[0] for col in cursor.description], raw_ed)
                        ed_id = _stringify_doc(_pick_row_value(ed_row, cab_ed_key_col, cab_ed_no_col))
                        ed_no = _stringify_doc(_pick_row_value(ed_row, cab_ed_no_col, cab_ed_key_col))
                        ed_updates = {}
                        _assign_existing_values(ed_updates, cab_ed_columns, new_ed_total, "TOTAL_DOC", "MONTO", "IMPORTE")
                        _assign_existing_values(ed_updates, cab_ed_columns, new_ed_total, "ABONO")
                        _assign_existing_values(ed_updates, cab_ed_columns, new_ed_total_letra, "TOTAL_LETRA")
                        _assign_existing_values(ed_updates, cab_ed_columns, now, "FECHA_ACT")
                        _update_dynamic_row(
                            cursor,
                            "CAB_ED",
                            ed_updates,
                            f"CAST([{cab_ed_key_col or cab_ed_no_col}] AS NVARCHAR(255)) = %s",
                            [ed_id or ed_no],
                        )

                        det_ed_doc_key_col = _pick_existing_column(det_ed_columns, "ID_DOC", "ID_ED")
                        det_ed_doc_no_col = _pick_existing_column(det_ed_columns, "NO_DOC", "NO_ED")
                        det_ed_origen_col = _pick_existing_column(det_ed_columns, "ORIGEN", "REFERENCIA", "NO_RECIBO")
                        det_ed_line_col = _pick_existing_column(det_ed_columns, "NO_LINEA", "LINEA", "NO_ITEM", "ORDEN")
                        det_ed_debito_col = _pick_existing_column(det_ed_columns, "DEBITO", "DEBE")
                        det_ed_credito_col = _pick_existing_column(det_ed_columns, "CREDITO", "HABER")

                        det_ed_where_parts = []
                        det_ed_where_params = []
                        if det_ed_doc_key_col and ed_id:
                            det_ed_where_parts.append(f"CAST([{det_ed_doc_key_col}] AS NVARCHAR(255)) = %s")
                            det_ed_where_params.append(ed_id)
                        if det_ed_doc_no_col and ed_no and ed_no != ed_id:
                            det_ed_where_parts.append(f"CAST([{det_ed_doc_no_col}] AS NVARCHAR(255)) = %s")
                            det_ed_where_params.append(ed_no)
                        if det_ed_origen_col:
                            origen_refs = [value for value in (no_recibo, recibo_id_real, recibo_id) if str(value or "").strip()]
                            for origen_ref in dict.fromkeys(origen_refs):
                                det_ed_where_parts.append(f"CAST([{det_ed_origen_col}] AS NVARCHAR(255)) = %s")
                                det_ed_where_params.append(origen_ref)

                        if det_ed_where_parts and (det_ed_debito_col or det_ed_credito_col):
                            det_ed_sql = f"SELECT * FROM DET_ED WITH (UPDLOCK, HOLDLOCK) WHERE {' OR '.join(f'({part})' for part in det_ed_where_parts)}"
                            if det_ed_line_col:
                                det_ed_sql += f" ORDER BY [{det_ed_line_col}]"
                            cursor.execute(det_ed_sql, det_ed_where_params)
                            det_ed_raw_columns = [col[0] for col in cursor.description]
                            det_ed_rows = [_normalize_result_row(det_ed_raw_columns, raw_row) for raw_row in cursor.fetchall()]

                            for det_ed_row in det_ed_rows:
                                debito_actual = _to_decimal(_pick_row_value(det_ed_row, det_ed_debito_col, "DEBITO", "DEBE", default=0))
                                credito_actual = _to_decimal(_pick_row_value(det_ed_row, det_ed_credito_col, "CREDITO", "HABER", default=0))
                                det_ed_updates = {}
                                _assign_existing_values(det_ed_updates, det_ed_columns, new_ed_total, "TOTAL_DOC", "MONTO", "IMPORTE", "ABONO")
                                if debito_actual > Decimal("0.01") and credito_actual <= Decimal("0.01"):
                                    _assign_existing_values(det_ed_updates, det_ed_columns, new_ed_total, "DEBITO", "DEBE")
                                    _assign_existing_values(det_ed_updates, det_ed_columns, Decimal("0"), "CREDITO", "HABER")
                                else:
                                    _assign_existing_values(det_ed_updates, det_ed_columns, Decimal("0"), "DEBITO", "DEBE")
                                    _assign_existing_values(det_ed_updates, det_ed_columns, new_ed_total, "CREDITO", "HABER")
                                _assign_existing_values(det_ed_updates, det_ed_columns, now, "FECHA_ACT")

                                row_where_parts = []
                                row_where_params = []
                                if det_ed_doc_key_col:
                                    row_doc_key = _stringify_doc(_pick_row_value(det_ed_row, det_ed_doc_key_col, default=""))
                                    if row_doc_key:
                                        row_where_parts.append(f"CAST([{det_ed_doc_key_col}] AS NVARCHAR(255)) = %s")
                                        row_where_params.append(row_doc_key)
                                if det_ed_doc_no_col:
                                    row_doc_no = _stringify_doc(_pick_row_value(det_ed_row, det_ed_doc_no_col, default=""))
                                    if row_doc_no:
                                        row_where_parts.append(f"CAST([{det_ed_doc_no_col}] AS NVARCHAR(255)) = %s")
                                        row_where_params.append(row_doc_no)
                                if det_ed_line_col:
                                    row_line = _stringify_doc(_pick_row_value(det_ed_row, det_ed_line_col, default=""))
                                    if row_line:
                                        row_where_parts.append(f"CAST([{det_ed_line_col}] AS NVARCHAR(255)) = %s")
                                        row_where_params.append(row_line)
                                elif det_ed_origen_col:
                                    row_origen = _stringify_doc(_pick_row_value(det_ed_row, det_ed_origen_col, default=""))
                                    if row_origen:
                                        row_where_parts.append(f"CAST([{det_ed_origen_col}] AS NVARCHAR(255)) = %s")
                                        row_where_params.append(row_origen)

                                if row_where_parts and det_ed_updates:
                                    _update_dynamic_row(
                                        cursor,
                                        "DET_ED",
                                        det_ed_updates,
                                        " AND ".join(row_where_parts),
                                        row_where_params,
                                    )

                transaction.on_commit(
                    lambda rid=recibo_id_real or recibo_id, nro=no_recibo or recibo_id, eid=client_event_id: broadcast_cxc_document_status(
                        document_id=rid,
                        no_recibo=nro,
                        estado="Abierto",
                        reason="monto-pago-corrected",
                        event_id=eid,
                    )
                )
    except Exception as exc:
        return JsonResponse({"detail": f"No se pudo corregir el monto pagado: {exc}"}, status=500)

    return JsonResponse({"ok": True, "recibo_id": recibo_id_real or recibo_id, "no_recibo": no_recibo or recibo_id})


@require_http_methods(["POST"])
def cuentas_por_cobrar_cancelar_view(request):
    auth_payload = _require_perm_json(request, "caja", "cxc_cancelar")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"detail": "JSON invalido"}, status=400)

    recibo_id = str(payload.get("recibo_id") or "").strip()
    if not recibo_id:
        return JsonResponse({"detail": "Parametro recibo_id requerido"}, status=400)

    now = timezone.localtime()
    local_date = timezone.localdate()
    usuario_id = int((auth_payload or {}).get("usuario_id") or 0) or None
    usuario_nombre = str((auth_payload or {}).get("usuario_nombre") or "").strip()
    terminal = _resolve_request_terminal(request, payload)
    client_event_id = str(payload.get("event_id") or "").strip()

    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cab_columns = _load_table_columns("CAB_RECIBO_INGRESO")
                det_columns = _load_table_columns("DET_RECIBO_INGRESO")
                cab_key_col = _pick_existing_column(cab_columns, "ID_RECIBO", "ID_DOC", "NO_RECIBO", "NO_DOC")
                cab_no_recibo_col = _pick_existing_column(cab_columns, "NO_RECIBO", "NO_DOC", "ID_RECIBO", "ID_DOC")
                det_key_col = _pick_existing_column(det_columns, "ID_RECIBO", "NO_RECIBO", "ID_DOC", "NO_DOC")
                det_no_recibo_col = _pick_existing_column(det_columns, "NO_RECIBO", "ID_RECIBO", "ID_DOC", "NO_DOC")
                if not cab_key_col or not det_key_col:
                    return JsonResponse({"detail": "No se pudo determinar la clave del recibo."}, status=500)

                header_where_parts = [f"CAST([{cab_key_col}] AS NVARCHAR(255)) = %s"]
                header_where_params = [recibo_id]
                if cab_no_recibo_col and cab_no_recibo_col != cab_key_col:
                    header_where_parts.append(f"CAST([{cab_no_recibo_col}] AS NVARCHAR(255)) = %s")
                    header_where_params.append(recibo_id)

                cursor.execute(
                    f"""
                    SELECT TOP 1 *
                    FROM CAB_RECIBO_INGRESO WITH (UPDLOCK, HOLDLOCK)
                    WHERE {' OR '.join(f'({part})' for part in header_where_parts)}
                    """,
                    header_where_params,
                )
                raw_header = cursor.fetchone()
                if not raw_header:
                    return JsonResponse({"detail": "No se encontro el recibo a cancelar."}, status=404)
                header_columns = [col[0] for col in cursor.description]
                header_row = _normalize_result_row(header_columns, raw_header)

                recibo_id_real = _stringify_doc(_pick_row_value(header_row, cab_key_col, cab_no_recibo_col))
                no_recibo = _stringify_doc(_pick_row_value(header_row, cab_no_recibo_col, cab_key_col))
                estado_actual = _pick_row_text(header_row, "EST_DOC", "ESTATUS", "ESTADO")
                cancelado_actual = _pick_row_text(header_row, "CANCELADO")
                if estado_actual.strip().upper() == "CANCELADO" or cancelado_actual.strip().upper() == "Y":
                    return JsonResponse({"detail": "Este recibo ya se encuentra cancelado."}, status=400)

                detail_lookup_value = (
                    no_recibo
                    if det_no_recibo_col and det_key_col == det_no_recibo_col and no_recibo
                    else recibo_id_real or recibo_id
                )
                detail_where_parts = [f"CAST([{det_key_col}] AS NVARCHAR(255)) = %s"]
                detail_where_params = [detail_lookup_value]
                if det_no_recibo_col and det_no_recibo_col != det_key_col and no_recibo:
                    detail_where_parts.append(f"CAST([{det_no_recibo_col}] AS NVARCHAR(255)) = %s")
                    detail_where_params.append(no_recibo)

                detail_order_columns = _unique_columns(
                    _pick_existing_column(det_columns, "NO_DOC", "ID_DOC", "DOCUMENTO", "FACTURA"),
                    _pick_existing_column(det_columns, "NO_CUOTA", "CUOTA_NUM", "NUM_CUOTA"),
                    _pick_existing_column(det_columns, "FECHA_CONT", "F_CONT", "FECHA"),
                )
                detail_sql = f"SELECT * FROM DET_RECIBO_INGRESO WITH (UPDLOCK, HOLDLOCK) WHERE {' OR '.join(f'({part})' for part in detail_where_parts)}"
                if detail_order_columns:
                    detail_sql += " ORDER BY " + ", ".join(f"[{column}]" for column in detail_order_columns)
                cursor.execute(detail_sql, detail_where_params)
                detail_columns = [col[0] for col in cursor.description]
                detail_rows = [_normalize_result_row(detail_columns, raw_row) for raw_row in cursor.fetchall()]
                if not detail_rows:
                    return JsonResponse({"detail": "El recibo no tiene detalle para cancelar."}, status=400)

                total_recibo = _to_decimal(
                    _pick_row_value(header_row, "TOTAL_COBRO", "TOTAL_DOC", "IMPORTE", "MONTO", default=0)
                )
                header_comment = _append_cancelled_comment(_pick_row_text(header_row, "COMENTARIO", "OBSERVACION"))
                header_updates = {}
                _assign_existing_values(header_updates, cab_columns, "Cancelado", "EST_DOC", "ESTATUS", "ESTADO")
                _assign_existing_values(header_updates, cab_columns, "Y", "CANCELADO")
                _assign_existing_values(header_updates, cab_columns, header_comment, "COMENTARIO", "OBSERVACION")
                _assign_existing_values(
                    header_updates,
                    cab_columns,
                    local_date,
                    "FECHA_CANCEL",
                    "F_CANCEL",
                    "FECHA_CANCELACION",
                )
                _assign_existing_values(header_updates, cab_columns, now, "FECHA_ACT")
                _update_dynamic_row(
                    cursor,
                    "CAB_RECIBO_INGRESO",
                    header_updates,
                    f"CAST([{cab_key_col}] AS NVARCHAR(255)) = %s",
                    [recibo_id_real or recibo_id],
                )

                factura_columns = _load_table_columns("CAB_FACTURA")
                factura_has_abono = "ABONO" in factura_columns
                factura_has_fecha_act = "FECHA_ACT" in factura_columns
                pagos_por_factura = {}
                for detail_row in detail_rows:
                    no_doc = _stringify_doc(_pick_row_value(detail_row, "NO_DOC", "ID_DOC", "DOCUMENTO", "FACTURA"))
                    if not no_doc:
                        continue
                    pagos_por_factura[no_doc] = pagos_por_factura.get(no_doc, Decimal("0")) + _get_det_recibo_applied_amount(detail_row)

                for no_doc, pago_cancelado in pagos_por_factura.items():
                    cursor.execute(
                        """
                        SELECT TOP 1 ID_DOC, ISNULL(SALDO, 0), ISNULL(TOTAL_DOC, 0), ISNULL(ABONO, 0)
                        FROM CAB_FACTURA WITH (UPDLOCK, HOLDLOCK)
                        WHERE CAST(ID_DOC AS NVARCHAR(255)) = %s
                        """,
                        [no_doc],
                    )
                    factura_row = cursor.fetchone()
                    if not factura_row:
                        raise ValueError(f"No se encontro la factura {no_doc} para cancelar el recibo.")

                    factura_total = _to_decimal(factura_row[2])
                    nuevo_saldo = _to_decimal(factura_row[1]) + pago_cancelado
                    if factura_total > 0:
                        nuevo_saldo = min(nuevo_saldo, factura_total)
                    nuevo_abono = max(_to_decimal(factura_row[3]) - pago_cancelado, Decimal("0"))
                    factura_updates = {
                        "SALDO": nuevo_saldo,
                        "EST_DOC": "ABIERTO" if nuevo_saldo > Decimal("0.01") else "CERRADO",
                    }
                    if factura_has_abono:
                        factura_updates["ABONO"] = nuevo_abono
                    if factura_has_fecha_act:
                        factura_updates["FECHA_ACT"] = now
                    _update_dynamic_row(
                        cursor,
                        "CAB_FACTURA",
                        factura_updates,
                        "CAST(ID_DOC AS NVARCHAR(255)) = %s",
                        [no_doc],
                    )

                prestamo_columns = _load_table_columns("DET_PRESTAMO")
                prestamo_has_balance = "BALANCE" in prestamo_columns
                prestamo_abono_cuota_col = _pick_existing_column(
                    prestamo_columns,
                    "ABONO_CUOTA",
                    "ABONOCUOTA",
                    "ABONO_CUENTA",
                    "ABONOCUENTA",
                )
                prestamo_no_recibo_col = _pick_existing_column(prestamo_columns, "NORECIBO", "NORECIBO", "NO_RECIBO")
                prestamo_docs_actualizados = set()
                for detail_row in detail_rows:
                    no_doc = _stringify_doc(_pick_row_value(detail_row, "NO_DOC", "ID_DOC", "DOCUMENTO", "FACTURA"))
                    no_cuota = _stringify_doc(_pick_row_value(detail_row, "NO_CUOTA", "CUOTA_NUM", "NUM_CUOTA", default=""))
                    pago_cancelado = _get_det_recibo_applied_amount(detail_row)
                    if not no_doc or not no_cuota or pago_cancelado <= Decimal("0"):
                        continue

                    cursor.execute(
                        """
                        SELECT TOP 1 *
                        FROM DET_PRESTAMO WITH (UPDLOCK, HOLDLOCK)
                        WHERE CAST(NO_DOC AS NVARCHAR(255)) = %s
                          AND CAST(NO_CUOTA AS NVARCHAR(255)) = %s
                        """,
                        [no_doc, no_cuota],
                    )
                    raw_prestamo_row = cursor.fetchone()
                    if not raw_prestamo_row:
                        continue
                    prestamo_row = _normalize_result_row([col[0] for col in cursor.description], raw_prestamo_row)

                    cuota_original = _to_decimal(_pick_row_value(prestamo_row, "CUOTA", "MONTO_CUOTA", "VALOR_CUOTA"))
                    balance_guardado = _to_decimal(_pick_row_value(prestamo_row, "BALANCE"))
                    abono_cuota_actual = _to_decimal(
                        _pick_row_value(
                            prestamo_row,
                            prestamo_abono_cuota_col,
                            "ABONO_CUOTA",
                            "ABONOCUOTA",
                            "ABONO_CUENTA",
                            "ABONOCUENTA",
                        )
                    )

                    if balance_guardado <= Decimal("0.01") and cuota_original > Decimal("0"):
                        balance_guardado = max(cuota_original - abono_cuota_actual, Decimal("0"))

                    nuevo_abono_cuota = max(abono_cuota_actual - pago_cancelado, Decimal("0"))
                    nuevo_balance = balance_guardado + pago_cancelado
                    if cuota_original > Decimal("0"):
                        nuevo_abono_cuota = min(nuevo_abono_cuota, cuota_original)
                        nuevo_balance = min(nuevo_balance, cuota_original)
                        if prestamo_has_balance and prestamo_abono_cuota_col:
                            nuevo_balance = max(cuota_original - nuevo_abono_cuota, Decimal("0"))

                    prestamo_updates = {}
                    if prestamo_has_balance:
                        prestamo_updates["BALANCE"] = nuevo_balance
                    if prestamo_abono_cuota_col:
                        prestamo_updates[prestamo_abono_cuota_col] = nuevo_abono_cuota
                    if prestamo_no_recibo_col and nuevo_abono_cuota <= Decimal("0.01"):
                        prestamo_updates[prestamo_no_recibo_col] = None
                    if prestamo_updates:
                        _update_dynamic_row(
                            cursor,
                            "DET_PRESTAMO",
                            prestamo_updates,
                            "CAST(NO_DOC AS NVARCHAR(255)) = %s AND CAST(NO_CUOTA AS NVARCHAR(255)) = %s",
                            [no_doc, no_cuota],
                        )
                    prestamo_docs_actualizados.add(no_doc)

                for prestamo_doc in prestamo_docs_actualizados:
                    _rebuild_det_prestamo_from_active_receipts(cursor, prestamo_doc)
                    _sync_cab_prestamo_from_det(cursor, prestamo_doc, now=now)

                _create_cxc_cancel_ed_entries(
                    cursor,
                    recibo_id=recibo_id_real or recibo_id,
                    no_recibo=no_recibo or recibo_id,
                    usuario_id=usuario_id,
                    usuario_nombre=usuario_nombre,
                    terminal=terminal,
                )

                _adjust_catalogo_saldo_actual(
                    cursor,
                    cuenta_num="11020101",
                    cuenta_nombre="Cuentas por Cobrar Clientes",
                    delta=-total_recibo,
                )

                transaction.on_commit(
                    lambda rid=recibo_id_real or recibo_id, nro=no_recibo or recibo_id, eid=client_event_id: broadcast_cxc_document_status(
                        document_id=rid,
                        no_recibo=nro,
                        estado="Cancelado",
                        reason="recibo-cancelled",
                        event_id=eid,
                    )
                )

    except Exception as exc:
        return JsonResponse({"detail": f"No se pudo cancelar el recibo: {exc}"}, status=500)

    return JsonResponse({"ok": True})


@require_http_methods(["POST"])
def cuentas_por_cobrar_guardar_view(request):
    auth_payload = _require_perm_json(request, "caja", "cxc_nuevo")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"detail": "JSON invalido"}, status=400)

    id_sn = str(payload.get("id_sn") or "").strip()
    if not id_sn:
        return JsonResponse({"detail": "Debes seleccionar un cliente antes de grabar."}, status=400)

    raw_detail = payload.get("detail") or []
    if not isinstance(raw_detail, list):
        return JsonResponse({"detail": "Detalle invalido."}, status=400)

    is_close_account = str(payload.get("cerrar_cuenta") or "").strip().lower() in {"1", "true", "y", "yes", "on"}
    if is_close_account and not has_perm((auth_payload or {}).get("usuario_id"), "caja", "cxc_cerrar_cuenta"):
        return JsonResponse({"detail": "No tienes permiso para grabar cierres de cuenta."}, status=403)
    detail = []
    for item in raw_detail:
        if not isinstance(item, dict):
            continue
        no_doc = str(item.get("no_doc") or "").strip()
        pago_abono = _to_decimal(item.get("pago_abono"))
        desc_avance = _to_decimal(item.get("desc_avance"))
        applied_amount = pago_abono + desc_avance
        if not no_doc or applied_amount <= Decimal("0"):
            continue
        detail.append(item)

    if not detail:
        return JsonResponse({"detail": "No puedes grabar si no tiene pagos pendientes seleccionados."}, status=400)

    efectivo = _to_decimal(payload.get("efectivo"))
    transferencia = _to_decimal(payload.get("transferencia"))
    total_metodos = efectivo + transferencia
    total_doc = sum(_to_decimal(item.get("pago_abono")) for item in detail)
    total_mora = sum(_to_decimal(item.get("cargo")) for item in detail)
    total_desc = sum(_to_decimal(item.get("desc_avance")) for item in detail)
    total_ret = sum(_to_decimal(item.get("total_ret")) for item in detail)
    applied_total = total_doc + total_desc
    cash_total = Decimal("0")
    if total_doc > Decimal("0.01"):
        cash_total = max(total_doc + total_mora - total_ret, Decimal("0"))
    ed_total = _resolve_cxc_ed_total(cash_total, total_desc, is_close_account=is_close_account)
    monto_pagar = _to_decimal(payload.get("monto_pagar"))

    if applied_total <= Decimal("0"):
        return JsonResponse({"detail": "No se puede grabar si el pago o descuento aplicado no es mayor a 0."}, status=400)

    if is_close_account and total_desc <= Decimal("0"):
        return JsonResponse({"detail": "Debes aplicar un descuento mayor a 0 para cerrar la cuenta."}, status=400)

    if not is_close_account and not _values_match(monto_pagar, cash_total):
        return JsonResponse({"detail": "El monto total del pago no coincide con el detalle seleccionado."}, status=400)

    if is_close_account:
        efectivo = Decimal("0")
        transferencia = Decimal("0")
        total_metodos = Decimal("0")
        cash_total = Decimal("0")
    elif cash_total <= Decimal("0.01"):
        efectivo = Decimal("0")
        transferencia = Decimal("0")
        total_metodos = Decimal("0")
    elif total_metodos <= Decimal("0"):
        return JsonResponse({"detail": "Debes asignar un importe mayor a 0 en los metodos de pago."}, status=400)

    if not is_close_account and cash_total > Decimal("0.01") and not _values_match(total_metodos, cash_total):
        return JsonResponse(
            {"detail": "El importe asignado en medio de pago debe ser igual al monto total del pago del documento."},
            status=400,
        )

    fecha_cont = _parse_date_value(payload.get("fecha_cont")) or timezone.localdate()
    fecha_venc = _parse_date_value(payload.get("fecha_venc")) or fecha_cont
    fecha_aplic = _parse_date_value(payload.get("fecha_aplic")) or fecha_cont
    fecha_pago = _parse_date_value(payload.get("fecha_pago")) or fecha_cont
    now = timezone.localtime()
    local_date = timezone.localdate()
    periodo_cont = str(local_date.month)
    ejercicio = local_date.year
    usuario_id = int((auth_payload or {}).get("usuario_id") or 0) or None
    usuario_nombre = str((auth_payload or {}).get("usuario_nombre") or "").strip()
    terminal = _resolve_request_terminal(request, payload)
    recibo_estado = str(payload.get("estado") or "Abierto").strip() or "Abierto"
    cuenta_caja = str(payload.get("cuenta_caja") or "").strip()
    cuenta_caja_desc = str(payload.get("cuenta_caja_desc") or "").strip()
    cuenta_efectivo = str(payload.get("cuenta_efectivo") or cuenta_caja).strip()
    cuenta_efectivo_desc = str(payload.get("cuenta_efectivo_desc") or cuenta_caja_desc).strip()
    cuenta_transferencia = str(payload.get("cuenta_transferencia") or cuenta_caja).strip()
    cuenta_transferencia_desc = str(payload.get("cuenta_transferencia_desc") or cuenta_caja_desc).strip()
    cuenta_desc_ret = str(payload.get("cuenta_desc_ret") or "").strip()
    cuenta_desc_ret_desc = str(payload.get("cuenta_desc_ret_desc") or "").strip()
    no_transferencia = str(payload.get("no_transferencia") or "").strip()
    cuenta_cliente_pago = str(payload.get("cuenta_cliente_pago") or "").strip()
    comentario = str(payload.get("comentario") or "").strip()
    client_event_id = str(payload.get("event_id") or "").strip()
    if is_close_account and not cuenta_desc_ret:
        return JsonResponse({"detail": "Debes indicar una cuenta Desc./Ret. para grabar el cierre de cuenta."}, status=400)
    total_letra = _amount_to_spanish_words(total_desc if is_close_account else cash_total)
    comentario_ed = _build_cxc_facturas_comment(detail, close_account=is_close_account)

    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT TOP 1
                        ISNULL(BLOQUEADO, 'N'),
                        ISNULL(NOM_SOCIO, ''),
                        ISNULL(RNC_CED, ''),
                        ISNULL(CTA_ASOCIADA, '')
                    FROM MAESTRO_SN
                    WHERE ID_SN = %s
                    """,
                    [id_sn],
                )
                cliente_row = cursor.fetchone()
                if not cliente_row:
                    return JsonResponse({"detail": "No se encontro el cliente seleccionado."}, status=400)
                if str(cliente_row[0] or "").strip().upper() == "Y":
                    return JsonResponse({"detail": "No se puede grabar: el cliente esta bloqueado."}, status=400)
                nombre_cliente = str(cliente_row[1] or "").strip() or str(payload.get("nombre") or "").strip()
                rnc_ced = str(cliente_row[2] or "").strip()
                cta_asociada = str(cliente_row[3] or "").strip()

                balance_cliente = _to_decimal(_get_open_ed_balance(id_sn))

                cab_columns = _load_table_columns("CAB_RECIBO_INGRESO")
                det_columns = _load_table_columns("DET_RECIBO_INGRESO")
                cab_identity_columns = _load_identity_columns("CAB_RECIBO_INGRESO")
                cab_key_col = _pick_existing_column(cab_columns, "ID_RECIBO", "ID_DOC", "NO_RECIBO", "NO_DOC")
                cab_no_recibo_col = _pick_existing_column(cab_columns, "NO_RECIBO", "NO_DOC")
                if not cab_key_col and not cab_no_recibo_col:
                    return JsonResponse({"detail": "No se pudo determinar la clave del recibo."}, status=500)

                next_no_recibo = None
                if cab_no_recibo_col and cab_no_recibo_col not in cab_identity_columns:
                    next_no_recibo = _next_table_numeric_value(cursor, "CAB_RECIBO_INGRESO", cab_no_recibo_col)
                elif cab_key_col and cab_key_col not in cab_identity_columns:
                    next_no_recibo = _next_table_numeric_value(cursor, "CAB_RECIBO_INGRESO", cab_key_col)

                detail_by_doc = {}
                detail_balance_hint_by_doc = {}
                for item in detail:
                    doc_key = str(item.get("no_doc") or "").strip()
                    detail_by_doc[doc_key] = detail_by_doc.get(doc_key, Decimal("0")) + _to_decimal(item.get("pago_abono")) + _to_decimal(item.get("desc_avance"))
                    balance_hint = max(
                        _to_decimal(item.get("balance_total_factura")),
                        _to_decimal(item.get("balance_doc")),
                    )
                    current_hint = detail_balance_hint_by_doc.get(doc_key, Decimal("0"))
                    detail_balance_hint_by_doc[doc_key] = max(current_hint, balance_hint)

                doc_numbers = list(detail_by_doc.keys())
                placeholders = ", ".join(["%s"] * len(doc_numbers))
                cursor.execute(
                    f"""
                    SELECT ID_DOC, SALDO, TOTAL_DOC, EST_DOC, ABONO
                    FROM CAB_FACTURA WITH (UPDLOCK, HOLDLOCK)
                    WHERE CAST(ID_DOC AS VARCHAR(50)) IN ({placeholders})
                    """,
                    doc_numbers,
                )
                factura_lookup = {
                    _stringify_doc(row[0]): {
                        "saldo": _to_decimal(row[1]),
                        "total_doc": _to_decimal(row[2]),
                        "est_doc": str(row[3] or "").strip(),
                        "abono": _to_decimal(row[4]),
                    }
                    for row in cursor.fetchall()
                }
                pagos_lookup = _load_cxc_active_payment_lookup(doc_numbers)
                pagos_por_doc = pagos_lookup.get("by_doc") or {}
                prestamo_rows_by_doc = _load_prestamo_rows_by_doc(doc_numbers)

                for doc_number, applied_amount in detail_by_doc.items():
                    factura_row = factura_lookup.get(doc_number)
                    if not factura_row:
                        return JsonResponse({"detail": f"No se encontro la factura {doc_number}."}, status=400)
                    pending_actual = _resolve_factura_pending_for_payment(
                        factura_row,
                        pagos_doc=pagos_por_doc.get(doc_number),
                        balance_hint=detail_balance_hint_by_doc.get(doc_number),
                        cuotas_rows=prestamo_rows_by_doc.get(doc_number),
                    )
                    factura_row["pending_actual"] = pending_actual
                    factura_row["abono_base"] = max(
                        factura_row.get("abono", Decimal("0")),
                        max(factura_row.get("total_doc", Decimal("0")) - pending_actual, Decimal("0")),
                    )
                    if applied_amount > pending_actual + Decimal("0.01"):
                        return JsonResponse(
                            {"detail": f"El pago asignado a la factura {doc_number} excede el saldo pendiente actual."},
                            status=400,
                        )

                prestamo_columns = _load_table_columns("DET_PRESTAMO")
                prestamo_has_balance = "BALANCE" in prestamo_columns
                prestamo_abono_cuota_col = _pick_existing_column(
                    prestamo_columns,
                    "ABONO_CUOTA",
                    "ABONOCUOTA",
                    "ABONO_CUENTA",
                    "ABONOCUENTA",
                )
                prestamo_no_recibo_col = _pick_existing_column(prestamo_columns, "NORECIBO", "NORECIBO", "NO_RECIBO")
                for item in detail:
                    if not bool(item.get("tiene_financiamiento")):
                        continue
                    no_doc = str(item.get("no_doc") or "").strip()
                    no_cuota = str(item.get("no_cuota") or "").strip()
                    if not no_doc or not no_cuota:
                        continue
                    cursor.execute(
                        """
                        SELECT TOP 1 *
                        FROM DET_PRESTAMO WITH (UPDLOCK, HOLDLOCK)
                        WHERE CAST(NO_DOC AS VARCHAR(50)) = %s
                          AND CAST(NO_CUOTA AS VARCHAR(50)) = %s
                        """,
                        [no_doc, no_cuota],
                    )
                    raw_prestamo_row = cursor.fetchone()
                    if not raw_prestamo_row:
                        return JsonResponse(
                            {"detail": f"No se encontro la cuota {no_cuota} del documento {no_doc}."},
                            status=400,
                        )
                    prestamo_row = _normalize_result_row([col[0] for col in cursor.description], raw_prestamo_row)
                    current_balance = _resolve_prestamo_balance(
                        _pick_row_value(prestamo_row, "BALANCE"),
                        cuota=_pick_row_value(prestamo_row, "CUOTA", "MONTO_CUOTA", "VALOR_CUOTA"),
                        abono_cuota=_pick_row_value(
                            prestamo_row,
                            prestamo_abono_cuota_col,
                            "ABONO_CUOTA",
                            "ABONOCUOTA",
                            "ABONO_CUENTA",
                            "ABONOCUENTA",
                        ),
                    )
                    applied_amount = _to_decimal(item.get("pago_abono")) + _to_decimal(item.get("desc_avance"))
                    if applied_amount > current_balance + Decimal("0.01"):
                        return JsonResponse(
                            {"detail": f"El monto aplicado a la cuota {no_cuota} del documento {no_doc} excede el saldo pendiente."},
                            status=400,
                        )

                header_values = {}
                if cab_key_col and cab_key_col not in cab_identity_columns:
                    _assign_existing_values(header_values, cab_columns, next_no_recibo, cab_key_col)
                if next_no_recibo is not None:
                    _assign_existing_values(header_values, cab_columns, next_no_recibo, "NO_RECIBO", "NO_DOC")
                _assign_existing_values(header_values, cab_columns, fecha_cont, "FECHA_CONT", "F_CONT")
                _assign_existing_values(header_values, cab_columns, fecha_venc, "FECHA_VENC", "F_VENC")
                _assign_existing_values(header_values, cab_columns, fecha_aplic, "FECHA_DOC", "FECHA_APLIC")
                _assign_existing_values(header_values, cab_columns, fecha_pago, "FECHA_PAGO")
                _assign_existing_values(header_values, cab_columns, id_sn, "ID_SN", "CLIENTE", "COD_CLIENTE")
                _assign_existing_values(header_values, cab_columns, nombre_cliente, "NOM_SN", "NOM_SOCIO", "NOMBRE", "NOM_CLIENTE")
                _assign_existing_values(header_values, cab_columns, str(payload.get("apodo") or "").strip(), "CONTACTO", "APODO")
                _assign_existing_values(header_values, cab_columns, rnc_ced, "RNC_CED", "RNC", "CEDULA")
                _assign_existing_values(header_values, cab_columns, str(payload.get("proyecto") or "").strip(), "ID_PROYECTO", "PROYECTO")
                _assign_existing_values(header_values, cab_columns, "RD$", "MONEDA", "MON_DOC", "MONEDA_PAGO")
                _assign_existing_values(header_values, cab_columns, "RD$", "MONPAGO", "MON_PAGO")
                _assign_existing_values(header_values, cab_columns, _to_decimal(payload.get("tasa_pago"), Decimal("1")), "TASA_PAGO", "TASA")
                _assign_existing_values(header_values, cab_columns, efectivo, "IMP_EFECTIVO", "EFECTIVO", "MONTO_EFECTIVO", "PAGO_EFECTIVO")
                _assign_existing_values(header_values, cab_columns, transferencia, "IMP_TRANSF", "TRANSFERENCIA", "MONTO_TRANSFERENCIA", "PAGO_TRANSFERENCIA")
                _assign_existing_values(header_values, cab_columns, total_desc if is_close_account else cash_total, "TOTAL_COBRO", "TOTAL_DOC", "IMPORTE", "MONTO")
                _assign_existing_values(header_values, cab_columns, total_mora, "TOTAL_MORA", "MORA", "CARGO")
                _assign_existing_values(header_values, cab_columns, total_desc, "TOTAL_DESCTO", "DESC_AVANCE", "DESCUENTO", "AVANCE")
                _assign_existing_values(header_values, cab_columns, total_ret, "TOTAL_RET", "RETENCION", "RET")
                _assign_existing_values(header_values, cab_columns, "N", "IMPRESO")
                _assign_existing_values(header_values, cab_columns, balance_cliente, "BALANCE")
                _assign_existing_values(header_values, cab_columns, total_letra, "TOTAL_LETRA")
                _assign_existing_values(
                    header_values,
                    cab_columns,
                    cuenta_caja,
                    "CTA_CAJA",
                    "CTA_BANCO_CAJA",
                    "CTA_COBRO",
                    "CTA_INGRESO",
                )
                _assign_existing_values(
                    header_values,
                    cab_columns,
                    cuenta_caja_desc,
                    "CTA_CAJA_DESC",
                    "DESC_CTA_CAJA",
                    "NOM_CTA_CAJA",
                    "CTA_BANCO_CAJA_DESC",
                    "NOM_CTA_BANCO",
                )
                _assign_existing_values(header_values, cab_columns, cuenta_efectivo, "CTA_EFECTIVO")
                _assign_existing_values(header_values, cab_columns, cuenta_efectivo, "CTA_CHEQUE")
                _assign_existing_values(header_values, cab_columns, cuenta_efectivo, "CTA_TARJETA")
                _assign_existing_values(header_values, cab_columns, cuenta_efectivo_desc, "NOM_CTA")
                _assign_existing_values(header_values, cab_columns, cuenta_efectivo_desc, "NOM_CTA3")
                _assign_existing_values(header_values, cab_columns, cuenta_transferencia, "CTA_TRANSF")
                _assign_existing_values(header_values, cab_columns, cuenta_transferencia_desc, "NOM_CTA2")
                _assign_existing_values(
                    header_values,
                    cab_columns,
                    cuenta_desc_ret,
                    "CTA_DESCTO",
                    "CTA_DESC_RET",
                    "CTA_DESC",
                    "CTA_RET",
                )
                _assign_existing_values(
                    header_values,
                    cab_columns,
                    cuenta_desc_ret_desc,
                    "CTA_DESC_RET_DESC",
                    "DESC_CTA_DESC_RET",
                    "NOM_CTA_DESC",
                    "NOM_CTA_RET",
                )
                _assign_existing_values(
                    header_values,
                    cab_columns,
                    no_transferencia,
                    "NO_TRANSF",
                    "NO_TRANSFERENCIA",
                    "REFERENCIA_TRANSF",
                    "NO_REF",
                    "REFERENCIA",
                )
                _assign_existing_values(
                    header_values,
                    cab_columns,
                    cuenta_cliente_pago,
                    "NO_CTA_CLIENTE",
                    "CTA_CLIENTE",
                    "CUENTA_CLIENTE",
                )
                _assign_existing_values(header_values, cab_columns, cta_asociada, "CTA_ASOCIADA")
                _assign_existing_values(header_values, cab_columns, cta_asociada, "CTA_ANTICIPO")
                _assign_existing_values(header_values, cab_columns, "41040120", "CTA_MORA")
                _assign_existing_values(header_values, cab_columns, "41040105", "CTA_DIF_CAMBIO")
                _assign_existing_values(header_values, cab_columns, "11010202P", "CTA_PRIMAT")
                _assign_existing_values(header_values, cab_columns, "11010202P", "CTA_PRIMAE")
                _assign_existing_values(header_values, cab_columns, "11010202P", "CTA_PRIMAC")
                _assign_existing_values(header_values, cab_columns, "11010202P", "CTA_PRIMAJ")
                _assign_existing_values(header_values, cab_columns, comentario, "COMENTARIO", "OBSERVACION")
                _assign_existing_values(header_values, cab_columns, recibo_estado, "ESTATUS", "EST_DOC", "ESTADO")
                _assign_existing_values(header_values, cab_columns, usuario_id, "ID_USUARIO", "USUARIO_ID")
                _assign_existing_values(header_values, cab_columns, usuario_nombre, "USUARIO", "USUARIO_NOMBRE")
                _assign_existing_values(header_values, cab_columns, terminal, "TERMINAL")
                _assign_existing_values(header_values, cab_columns, local_date, "FECHA_CREACION")
                _assign_existing_values(header_values, cab_columns, now, "FECHA_ACT")
                _assign_existing_values(header_values, cab_columns, periodo_cont, "PERIODO_CONT")
                _assign_existing_values(header_values, cab_columns, ejercicio, "EJERCICIO")
                cta_banco_col = _pick_existing_column(cab_columns, "CTA_BANCO")
                if cta_banco_col:
                    header_values[cta_banco_col] = None

                inserted_recibo_id = _insert_dynamic_row(
                    cursor,
                    "CAB_RECIBO_INGRESO",
                    cab_columns,
                    header_values,
                    output_column=cab_key_col or cab_no_recibo_col,
                    skip_columns=cab_identity_columns,
                )
                inserted_recibo_id = _stringify_doc(inserted_recibo_id)
                next_no_recibo_text = _stringify_doc(next_no_recibo)
                recibo_id = inserted_recibo_id or next_no_recibo_text
                no_recibo = next_no_recibo_text or inserted_recibo_id

                det_line_col = _pick_existing_column(det_columns, "NO_LINEA", "LINEA", "NO_ITEM", "ORDEN")
                next_det_line = 0
                if det_line_col:
                    cursor.execute(
                        f"SELECT ISNULL(MAX(TRY_CAST([{det_line_col}] AS BIGINT)), 0) FROM DET_RECIBO_INGRESO WITH (UPDLOCK, HOLDLOCK)"
                    )
                    row = cursor.fetchone()
                    next_det_line = int(row[0] or 0)

                for item in detail:
                    next_det_line += 1
                    fecha_fact = fecha_pago
                    vencida = "*" if int(_to_float(item.get("dias"))) > 0 else ""
                    monto_total_factura = _to_decimal(item.get("monto_doc"))
                    cuota_linea = _to_decimal(item.get("cuota"))
                    balance_doc_linea = _to_decimal(item.get("balance_doc"))
                    monto_pagado_linea = _to_decimal(item.get("pago_abono"))
                    monto_descuento_linea = _to_decimal(item.get("desc_avance"))
                    total_recibo_linea = max(balance_doc_linea - monto_pagado_linea, Decimal("0"))
                    saldo_venc_linea = monto_pagado_linea + total_recibo_linea + monto_descuento_linea
                    sub_pdo_linea = _calculate_det_recibo_sub_pdo(
                        monto_pagado_linea,
                        monto_total_factura,
                        cuota_linea,
                    )
                    detail_values = {}
                    _assign_existing_values(detail_values, det_columns, recibo_id, "ID_RECIBO")
                    _assign_existing_values(detail_values, det_columns, no_recibo, "NO_RECIBO")
                    if det_line_col:
                        _assign_existing_values(detail_values, det_columns, next_det_line, det_line_col)
                    _assign_existing_values(detail_values, det_columns, str(item.get("no_doc") or "").strip(), "NO_DOC", "ID_DOC", "DOCUMENTO", "FACTURA")
                    _assign_existing_values(detail_values, det_columns, str(item.get("td") or "").strip(), "TIPO_DOC", "TD", "CLASE_DOC", "TIPO")
                    _assign_existing_values(detail_values, det_columns, _parse_date_value(item.get("fecha_cont")) or fecha_cont, "FECHA_CONT", "F_CONT", "FECHA_DOC", "FECHA")
                    _assign_existing_values(detail_values, det_columns, fecha_fact, "FECHA_FACT")
                    _assign_existing_values(detail_values, det_columns, _to_decimal(item.get("monto_doc")), "MONTO_DOC", "TOTAL_DOC", "MONTO")
                    _assign_existing_values(detail_values, det_columns, vencida, "VENCIDA")
                    _assign_existing_values(detail_values, det_columns, monto_total_factura, "SUBTOTAL")
                    _assign_existing_values(detail_values, det_columns, monto_total_factura, "TOTAL_FACT")
                    _assign_existing_values(detail_values, det_columns, str(item.get("comentario_factura") or "").strip(), "COMENTARIO_FACTURA", "COMENTARIO", "OBSERVACION")
                    _assign_existing_values(detail_values, det_columns, str(item.get("no_cuota") or "").strip(), "NO_CUOTA", "CUOTA_NUM", "NUM_CUOTA")
                    _assign_existing_values(detail_values, det_columns, _to_decimal(item.get("cuota")), "CUOTA", "MONTO_CUOTA", "VALOR_CUOTA")
                    _assign_existing_values(detail_values, det_columns, _to_decimal(item.get("balance_doc")), "BALANCE_DOC", "BALANCE", "SALDO_DOC", "SALDO")
                    _assign_existing_values(detail_values, det_columns, _to_decimal(item.get("balance_total_factura")), "BALANCE_TOTAL_FACTURA", "SALDO_FACTURA")
                    _assign_existing_values(detail_values, det_columns, _parse_date_value(item.get("fecha_venc")), "FECHA_VENC", "F_VENC", "VENCIMIENTO")
                    _assign_existing_values(detail_values, det_columns, int(_to_float(item.get("dias"))), "DIAS", "DIAS_VENC", "ATRASO")
                    _assign_existing_values(detail_values, det_columns, _to_decimal(item.get("cargo")), "CARGO", "MORA", "TOTAL_MORA")
                    _assign_existing_values(detail_values, det_columns, _to_decimal(item.get("porc_desc")), "PORC_DESC", "PORCENTAJE_DESC", "PCT_DESC")
                    _assign_existing_values(detail_values, det_columns, _to_decimal(item.get("desc_avance")), "DESC_AVANCE", "DESCUENTO", "AVANCE")
                    _assign_existing_values(
                        detail_values,
                        det_columns,
                        _to_decimal(item.get("pago_abono")),
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
                    )
                    _assign_existing_values(detail_values, det_columns, monto_pagado_linea, "TOTAL_PAGO")
                    _assign_existing_values(detail_values, det_columns, monto_pagado_linea, "TOTAL_PAGO2")
                    _assign_existing_values(detail_values, det_columns, sub_pdo_linea, "SUB_PDO", "SUBPDO")
                    _assign_existing_values(detail_values, det_columns, saldo_venc_linea, "SALDO_VENC")
                    _assign_existing_values(detail_values, det_columns, Decimal("1.0000"), "TASAFACT")
                    _assign_existing_values(detail_values, det_columns, total_recibo_linea, "TOTAL_RECIBO")
                    _assign_existing_values(detail_values, det_columns, total_recibo_linea, "TOTAL_RECIBO2")
                    _assign_existing_values(detail_values, det_columns, "Y", "SELECCION")
                    _assign_existing_values(detail_values, det_columns, _to_decimal(item.get("balance_pend")), "BALANCE_PEND", "SALDO_PEND", "PENDIENTE")
                    _assign_existing_values(detail_values, det_columns, _to_decimal(item.get("total_ret")), "TOTAL_RET", "RETENCION", "RET")
                    _assign_existing_values(detail_values, det_columns, id_sn, "ID_SN")
                    _assign_existing_values(detail_values, det_columns, usuario_id, "ID_USUARIO", "USUARIO_ID")
                    _assign_existing_values(detail_values, det_columns, periodo_cont, "PERIODO_CONT")
                    _assign_existing_values(detail_values, det_columns, ejercicio, "EJERCICIO")
                    _insert_dynamic_row(cursor, "DET_RECIBO_INGRESO", det_columns, detail_values)

                _create_cxc_ed_entries(
                    cursor,
                    no_recibo=no_recibo,
                    id_sn=id_sn,
                    nombre_cliente=nombre_cliente,
                    rnc_ced=rnc_ced,
                    fecha_cont=fecha_cont,
                    fecha_doc=fecha_pago,
                    fecha_venc=fecha_venc,
                    total_recibo=ed_total,
                    comentario=comentario_ed,
                    periodo_cont=periodo_cont,
                    ejercicio=ejercicio,
                    usuario_id=usuario_id,
                    usuario_nombre=usuario_nombre,
                    terminal=terminal,
                    cta_asociada=cta_asociada,
                    cuenta_medio_pago=cuenta_desc_ret if is_close_account else cuenta_caja,
                    cuenta_medio_pago_desc=cuenta_desc_ret_desc if is_close_account else cuenta_caja_desc,
                )

                factura_columns = _load_table_columns("CAB_FACTURA")
                factura_has_abono = "ABONO" in factura_columns
                factura_has_fecha_act = "FECHA_ACT" in factura_columns
                for doc_number, applied_amount in detail_by_doc.items():
                    factura_row = factura_lookup.get(doc_number) or {}
                    pending_actual = max(_to_decimal(factura_row.get("pending_actual")), Decimal("0"))
                    new_saldo = max(pending_actual - applied_amount, Decimal("0"))
                    factura_updates = {"SALDO": new_saldo, "EST_DOC": "CERRADO" if new_saldo <= Decimal("0.01") else "ABIERTO"}
                    if factura_has_abono:
                        total_doc_actual = max(_to_decimal(factura_row.get("total_doc")), Decimal("0"))
                        new_abono = max(_to_decimal(factura_row.get("abono_base")) + applied_amount, Decimal("0"))
                        if total_doc_actual > Decimal("0"):
                            new_abono = min(new_abono, total_doc_actual)
                        factura_updates["ABONO"] = new_abono
                    if factura_has_fecha_act:
                        factura_updates["FECHA_ACT"] = now
                    _update_dynamic_row(
                        cursor,
                        "CAB_FACTURA",
                        factura_updates,
                        "CAST(ID_DOC AS VARCHAR(50)) = %s",
                        [doc_number],
                    )

                prestamo_docs_actualizados = set()
                for item in detail:
                    if not bool(item.get("tiene_financiamiento")):
                        continue
                    no_doc = str(item.get("no_doc") or "").strip()
                    no_cuota = str(item.get("no_cuota") or "").strip()
                    if not no_doc or not no_cuota:
                        continue
                    new_balance = max(_to_decimal(item.get("balance_pend")), Decimal("0"))
                    cuota_original = _to_decimal(item.get("cuota"))
                    new_abono_cuota = max(cuota_original - new_balance, Decimal("0"))
                    prestamo_updates = {}
                    if prestamo_has_balance:
                        prestamo_updates["BALANCE"] = new_balance
                    if prestamo_abono_cuota_col:
                        prestamo_updates[prestamo_abono_cuota_col] = new_abono_cuota
                    if prestamo_no_recibo_col:
                        prestamo_updates[prestamo_no_recibo_col] = no_recibo
                    _update_dynamic_row(
                        cursor,
                        "DET_PRESTAMO",
                        prestamo_updates,
                        "CAST(NO_DOC AS VARCHAR(50)) = %s AND CAST(NO_CUOTA AS VARCHAR(50)) = %s",
                        [no_doc, no_cuota],
                    )
                    prestamo_docs_actualizados.add(no_doc)

                for prestamo_doc in prestamo_docs_actualizados:
                    _rebuild_det_prestamo_from_active_receipts(cursor, prestamo_doc)
                    _sync_cab_prestamo_from_det(cursor, prestamo_doc, now=now)

                transaction.on_commit(
                    lambda rid=recibo_id, nro=no_recibo, estado=recibo_estado, eid=client_event_id: broadcast_cxc_document_status(
                        document_id=rid,
                        no_recibo=nro,
                        estado=estado,
                        reason="recibo-saved",
                        event_id=eid,
                    )
                )

    except Exception as exc:
        return JsonResponse({"detail": f"No se pudo grabar el recibo: {exc}"}, status=500)

    return JsonResponse({"ok": True, "recibo_id": recibo_id, "no_recibo": no_recibo})


@require_GET
def cuentas_por_cobrar_cobros_anteriores_view(request):
    auth_payload = _require_perm_json(request, "caja", "cxc_buscar")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    id_sn = (request.GET.get("id_sn") or "").strip()
    exclude_recibo_id = (request.GET.get("exclude_recibo_id") or "").strip()
    if not id_sn:
        return JsonResponse({"detail": "Parametro id_sn requerido"}, status=400)

    try:
        results = _load_cxc_cobros_anteriores(id_sn, exclude_recibo_id=exclude_recibo_id)
    except Exception:
        return JsonResponse({"detail": "No se pudieron cargar los cobros anteriores."}, status=500)

    return JsonResponse({"results": results})


@require_GET
def cuentas_por_cobrar_historial_pago_view(request):
    auth_payload = _require_perm_json(request, "caja", "cxc_buscar")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    no_doc = (request.GET.get("no_doc") or "").strip()
    if not no_doc:
        return JsonResponse({"detail": "Parametro no_doc requerido"}, status=400)

    try:
        results = _load_cxc_factura_historial_pago(no_doc)
        if not results:
            return JsonResponse({"detail": "No se encontro la factura o no se pudo cargar su historial."}, status=404)
    except Exception as e:
        return JsonResponse({"detail": f"Error cargando historial de pago: {e}"}, status=500)

    return JsonResponse(results)


@require_GET
def catalogo_cuentas_detalle_view(request):
    auth_payload = _require_perm_json(request, "caja", "cxc_nuevo")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    try:
        results = _load_catalogo_cuentas()
    except Exception:
        return JsonResponse({"detail": "No se pudieron cargar las cuentas del catalogo."}, status=500)

    return JsonResponse({"results": results})


@require_GET
def catalogo_cuentas_financ_view(request):
    auth_payload = _require_perm_json(request, "caja", "cxc_nuevo")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    try:
        results = _load_catalogo_cuentas(cta_financ="Y", cta_prima_vacia=True)
    except Exception:
        return JsonResponse({"detail": "No se pudieron cargar las cuentas financieras del catalogo."}, status=500)

    return JsonResponse({"results": results})


def _load_cuadre_caja_usuarios():
    users = []
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT ID_USUARIO, USUARIO, ISNULL(NOMBRE, USUARIO) AS NOMBRE
                FROM USUARIO
                ORDER BY USUARIO
                """
            )
            for user_id, usuario_login, nombre in cursor.fetchall():
                users.append(
                    {
                        "id": str(user_id or "").strip(),
                        "usuario": str(usuario_login or "").strip(),
                        "nombre": str(nombre or usuario_login or "").strip(),
                        "label": str(nombre or usuario_login or "").strip(),
                    }
                )
    except Exception:
        return []
    return users


def _load_cuadre_caja_terminales():
    columns = _load_table_columns("CAB_RECIBO_INGRESO")
    if not columns:
        return []

    terminal_col = _pick_existing_column(columns, "TERMINAL")
    if not terminal_col:
        return []

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT DISTINCT LTRIM(RTRIM(CAST([{terminal_col}] AS NVARCHAR(255)))) AS TERMINAL
                FROM CAB_RECIBO_INGRESO
                WHERE NULLIF(LTRIM(RTRIM(CAST([{terminal_col}] AS NVARCHAR(255)))), '') IS NOT NULL
                ORDER BY TERMINAL
                """
            )
            terminales = []
            for (terminal,) in cursor.fetchall():
                terminal_text = str(terminal or "").strip()
                if not terminal_text:
                    continue
                terminales.append(
                    {
                        "terminal": terminal_text,
                        "label": terminal_text,
                    }
                )
    except Exception:
        return []
    return terminales


def _load_cuadre_caja_descuentos_por_recibo(fecha_desde, fecha_hasta):
    cab_columns = _load_table_columns("CAB_RECIBO_INGRESO")
    det_columns = _load_table_columns("DET_RECIBO_INGRESO")
    if not cab_columns or not det_columns:
        return {}

    fecha_col = _pick_existing_column(cab_columns, "FECHA_CONT", "F_CONT", "FECHA_DOC", "FECHA")
    cab_key_col = _pick_existing_column(cab_columns, "ID_RECIBO", "NO_RECIBO", "ID_DOC", "NO_DOC")
    cab_no_recibo_col = _pick_existing_column(cab_columns, "NO_RECIBO", "ID_RECIBO", "ID_DOC", "NO_DOC")
    det_key_col = _pick_existing_column(det_columns, "ID_RECIBO", "NO_RECIBO", "ID_DOC", "NO_DOC")
    det_no_recibo_col = _pick_existing_column(det_columns, "NO_RECIBO", "ID_RECIBO", "ID_DOC", "NO_DOC")
    det_desc_col = _pick_existing_column(det_columns, "DESC_AVANCE", "DESCUENTO", "AVANCE", "DESC")
    if not fecha_col or not cab_key_col or not det_key_col or not det_desc_col:
        return {}

    join_parts = []
    join_signatures = set()
    cab_refs = [column for column in (cab_key_col, cab_no_recibo_col) if column]
    det_refs = [column for column in (det_key_col, det_no_recibo_col) if column]
    for det_ref in det_refs:
        for cab_ref in cab_refs:
            signature = (det_ref, cab_ref)
            if signature in join_signatures:
                continue
            join_signatures.add(signature)
            join_parts.append(
                f"CAST(d.[{det_ref}] AS NVARCHAR(255)) = CAST(c.[{cab_ref}] AS NVARCHAR(255))"
            )

    if not join_parts:
        return {}

    group_columns = _unique_columns(cab_key_col, cab_no_recibo_col)
    sql = f"""
        SELECT
            CAST(c.[{cab_key_col}] AS NVARCHAR(255)) AS RECIBO_KEY,
            CAST(c.[{cab_no_recibo_col}] AS NVARCHAR(255)) AS RECIBO_NO,
            SUM(ISNULL(TRY_CONVERT(DECIMAL(18, 2), d.[{det_desc_col}]), 0)) AS TOTAL_DESCUENTO
        FROM CAB_RECIBO_INGRESO c
        LEFT JOIN DET_RECIBO_INGRESO d
            ON {" OR ".join(f"({part})" for part in join_parts)}
        WHERE CONVERT(date, c.[{fecha_col}]) BETWEEN %s AND %s
        GROUP BY {", ".join(f"c.[{column}]" for column in group_columns)}
    """

    lookup = {}
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql, [fecha_desde, fecha_hasta])
            for recibo_key, recibo_no, total_descuento in cursor.fetchall():
                total_value = _to_float(total_descuento)
                key = _stringify_doc(recibo_key)
                no_key = _stringify_doc(recibo_no)
                if key:
                    lookup[key] = total_value
                if no_key:
                    lookup[no_key] = total_value
    except Exception:
        return {}
    return lookup


def _load_cuadre_caja_movimientos(fecha_desde, fecha_hasta, usuarios_lookup=None):
    usuarios_lookup = usuarios_lookup or {}
    columns = _load_table_columns("CAB_RECIBO_INGRESO")
    if not columns:
        return []

    fecha_col = _pick_existing_column(columns, "FECHA_CONT", "F_CONT", "FECHA_DOC", "FECHA")
    cab_key_col = _pick_existing_column(columns, "ID_RECIBO", "NO_RECIBO", "ID_DOC", "NO_DOC")
    no_recibo_col = _pick_existing_column(columns, "NO_RECIBO", "ID_RECIBO", "ID_DOC")
    efectivo_col = _pick_existing_column(columns, "IMP_EFECTIVO", "EFECTIVO", "MONTO_EFECTIVO", "PAGO_EFECTIVO")
    transferencia_col = _pick_existing_column(columns, "IMP_TRANSF", "TRANSFERENCIA", "MONTO_TRANSFERENCIA", "PAGO_TRANSFERENCIA")
    descuento_col = _pick_existing_column(columns, "TOTAL_DESCTO", "DESC_AVANCE", "DESCUENTO", "AVANCE")
    total_col = _pick_existing_column(columns, "TOTAL_COBRO", "TOTAL_DOC", "IMPORTE", "MONTO")
    estatus_col = _pick_existing_column(columns, "ESTATUS", "EST_DOC", "ESTADO")
    cancelado_col = _pick_existing_column(columns, "CANCELADO")
    usuario_id_col = _pick_existing_column(columns, "ID_USUARIO", "USUARIO_ID")
    usuario_nombre_col = _pick_existing_column(columns, "USUARIO_NOMBRE", "NOMBRE_USUARIO", "USUARIO")
    terminal_col = _pick_existing_column(columns, "TERMINAL")

    if not fecha_col:
        return []

    selected_columns = _unique_columns(
        fecha_col,
        cab_key_col,
        no_recibo_col,
        efectivo_col,
        transferencia_col,
        descuento_col,
        total_col,
        estatus_col,
        cancelado_col,
        usuario_id_col,
        usuario_nombre_col,
        terminal_col,
    )
    if not selected_columns:
        return []

    order_columns = _unique_columns(fecha_col, no_recibo_col)
    sql = (
        f"SELECT {', '.join(f'[{column}]' for column in selected_columns)} "
        "FROM CAB_RECIBO_INGRESO "
        f"WHERE CONVERT(date, [{fecha_col}]) BETWEEN %s AND %s"
    )
    if order_columns:
        sql += " ORDER BY " + ", ".join(f"[{column}]" for column in order_columns)

    movimientos = []
    with connection.cursor() as cursor:
        cursor.execute(sql, [fecha_desde, fecha_hasta])
        raw_columns = [col[0] for col in cursor.description]
        rows = [_normalize_result_row(raw_columns, raw_row) for raw_row in cursor.fetchall()]

    descuentos_por_recibo = _load_cuadre_caja_descuentos_por_recibo(fecha_desde, fecha_hasta)

    for row in rows:
        recibo_key = _stringify_doc(_pick_row_value(row, cab_key_col, no_recibo_col))
        efectivo = _pick_amount_value(row, efectivo_col, default=0.0)
        transferencia = _pick_amount_value(row, transferencia_col, default=0.0)
        descuento_header = _pick_amount_value(row, descuento_col, default=0.0)
        no_recibo = _stringify_doc(_pick_row_value(row, no_recibo_col))
        descuento_detalle = max(
            _to_float(descuentos_por_recibo.get(recibo_key)),
            _to_float(descuentos_por_recibo.get(no_recibo)),
        )
        descuento = max(descuento_header, descuento_detalle)
        total_header = _pick_amount_value(row, total_col, default=efectivo + transferencia)
        usuario_id = _pick_row_text(row, usuario_id_col)
        usuario_nombre = usuarios_lookup.get(usuario_id) or _pick_row_text(row, usuario_nombre_col) or usuario_id or "Sin usuario"
        terminal = _pick_row_text(row, terminal_col) or "Sin terminal"
        estado = _pick_row_text(row, estatus_col).upper()
        cancelado = _pick_row_text(row, cancelado_col).upper() == "Y" or estado == "CANCELADO"
        total_efectivo_transfer = max(efectivo + transferencia, 0.0)
        movimientos.append(
            {
                "fecha": _pick_row_value(row, fecha_col),
                "no_recibo": no_recibo,
                "no_recibo_sort": _doc_sort_key(no_recibo),
                "usuario_id": usuario_id,
                "usuario_nombre": usuario_nombre,
                "terminal": terminal,
                "efectivo": efectivo,
                "transferencia": transferencia,
                "descuento": descuento,
                "total_header": total_header,
                "total_efectivo_transfer": total_efectivo_transfer,
                "cancelado": cancelado,
            }
        )

    return movimientos


def _build_empty_cuadre_caja_report(label, fecha_desde, fecha_hasta):
    return {
        "label": label,
        "header_prefix": "INFORME GENERAL",
        "detail_title": "DETALLE DEL INFORME",
        "usuario_label": "",
        "terminal_label": "",
        "fecha_desde": _fmt_date(fecha_desde),
        "fecha_hasta": _fmt_date(fecha_hasta),
        "desde_recibo": "",
        "hasta_recibo": "",
        "efectivo": 0.0,
        "transferencia": 0.0,
        "descuentos": 0.0,
        "cancelados": 0.0,
        "total_recibos": 0.0,
        "total_descuentos": 0.0,
        "total_caja": 0.0,
        "efectivo_fmt": _pdf_money(0),
        "transferencia_fmt": _pdf_money(0),
        "descuentos_fmt": _pdf_money(0),
        "cancelados_fmt": _pdf_money(0),
        "total_recibos_fmt": _pdf_money(0),
        "total_descuentos_fmt": _pdf_money(0),
        "total_caja_fmt": _pdf_money(0),
    }


def _init_cuadre_caja_report_group(
    label,
    fecha_desde,
    fecha_hasta,
    *,
    header_prefix="INFORME GENERAL",
    detail_title="DETALLE DEL INFORME",
    usuario_label="",
    terminal_label="",
):
    return {
        "label": label,
        "header_prefix": header_prefix,
        "detail_title": detail_title,
        "usuario_label": usuario_label,
        "terminal_label": terminal_label,
        "fecha_desde": _fmt_date(fecha_desde),
        "fecha_hasta": _fmt_date(fecha_hasta),
        "desde_recibo": "",
        "hasta_recibo": "",
        "desde_sort": None,
        "hasta_sort": None,
        "efectivo": 0.0,
        "transferencia": 0.0,
        "descuentos": 0.0,
        "cancelados": 0.0,
    }


def _accumulate_cuadre_caja_group(group, item):
    no_recibo = str(item.get("no_recibo") or "").strip()
    doc_sort = item.get("no_recibo_sort")
    if no_recibo and (group["desde_sort"] is None or doc_sort < group["desde_sort"]):
        group["desde_sort"] = doc_sort
        group["desde_recibo"] = no_recibo
    if no_recibo and (group["hasta_sort"] is None or doc_sort > group["hasta_sort"]):
        group["hasta_sort"] = doc_sort
        group["hasta_recibo"] = no_recibo

    if item.get("cancelado"):
        group["cancelados"] += _to_float(item.get("total_efectivo_transfer"))
        return

    group["efectivo"] += _to_float(item.get("efectivo"))
    group["transferencia"] += _to_float(item.get("transferencia"))
    group["descuentos"] += _to_float(item.get("descuento"))


def _finalize_cuadre_caja_group(group):
    total_recibos = _to_float(group["efectivo"]) + _to_float(group["transferencia"])
    total_descuentos = _to_float(group["descuentos"])
    total_caja = total_recibos
    return {
        **group,
        "total_recibos": total_recibos,
        "total_descuentos": total_descuentos,
        "total_caja": total_caja,
        "efectivo_fmt": _pdf_money(group["efectivo"]),
        "transferencia_fmt": _pdf_money(group["transferencia"]),
        "descuentos_fmt": _pdf_money(group["descuentos"]),
        "cancelados_fmt": _pdf_money(group["cancelados"]),
        "total_recibos_fmt": _pdf_money(total_recibos),
        "total_descuentos_fmt": _pdf_money(total_descuentos),
        "total_caja_fmt": _pdf_money(total_caja),
    }


def _build_cuadre_caja_reports(*, movimientos, modo, fecha_desde, fecha_hasta, usuario_filtro="", terminal_filtro="", usuarios_lookup=None, terminales_lookup=None):
    usuarios_lookup = usuarios_lookup or {}
    terminales_lookup = terminales_lookup or {}

    filtered = []
    for item in movimientos:
        if usuario_filtro and str(item.get("usuario_id") or "").strip() != usuario_filtro:
            continue
        if terminal_filtro and str(item.get("terminal") or "").strip() != terminal_filtro:
            continue
        filtered.append(item)

    if modo == "general":
        general_group = _init_cuadre_caja_report_group(
            "Todas las terminales y usuarios",
            fecha_desde,
            fecha_hasta,
            header_prefix="INFORME GENERAL",
            detail_title="DETALLE GENERAL",
        )
        detail_groups = {}

        for item in filtered:
            _accumulate_cuadre_caja_group(general_group, item)

            usuario_label = str(item.get("usuario_nombre") or "Sin usuario").strip() or "Sin usuario"
            terminal_label = str(item.get("terminal") or "Sin terminal").strip() or "Sin terminal"
            usuario_key = str(item.get("usuario_id") or usuario_label).strip() or usuario_label
            terminal_key = terminal_label
            detail_key = f"{usuario_key}::{terminal_key}"
            detail_group = detail_groups.setdefault(
                detail_key,
                _init_cuadre_caja_report_group(
                    f"{usuario_label} / {terminal_label}",
                    fecha_desde,
                    fecha_hasta,
                    header_prefix="INFORME INDIVIDUAL",
                    detail_title="DETALLE DE USUARIO Y TERMINAL",
                    usuario_label=usuario_label,
                    terminal_label=terminal_label,
                ),
            )
            _accumulate_cuadre_caja_group(detail_group, item)

        if filtered:
            reports = [_finalize_cuadre_caja_group(general_group)]
            reports.extend(
                _finalize_cuadre_caja_group(group)
                for _, group in sorted(
                    detail_groups.items(),
                    key=lambda item: (
                        str(item[1].get("usuario_label") or "").lower(),
                        str(item[1].get("terminal_label") or "").lower(),
                        str(item[1].get("label") or "").lower(),
                    ),
                )
            )
            return reports
    else:
        groups = {}
        for item in filtered:
            if modo == "terminal":
                key = str(item.get("terminal") or "SIN_TERMINAL").strip() or "SIN_TERMINAL"
                label = item.get("terminal") or terminales_lookup.get(key) or "Sin terminal"
                group = groups.setdefault(
                    key,
                    _init_cuadre_caja_report_group(
                        label,
                        fecha_desde,
                        fecha_hasta,
                        header_prefix="INFORME DE TERMINAL",
                        detail_title="DETALLE DE TERMINAL",
                        terminal_label=label,
                    ),
                )
            else:
                key = str(item.get("usuario_id") or item.get("usuario_nombre") or "SIN_USUARIO").strip() or "SIN_USUARIO"
                label = item.get("usuario_nombre") or usuarios_lookup.get(key) or "Sin usuario"
                group = groups.setdefault(
                    key,
                    _init_cuadre_caja_report_group(
                        label,
                        fecha_desde,
                        fecha_hasta,
                        header_prefix="INFORME DE USUARIO",
                        detail_title="DETALLE DE USUARIO",
                        usuario_label=label,
                    ),
                )
            _accumulate_cuadre_caja_group(group, item)

        reports = [
            _finalize_cuadre_caja_group(group)
            for _, group in sorted(groups.items(), key=lambda item: str(item[1].get("label") or "").lower())
        ]
        if reports:
            return reports

    if modo == "terminal":
        label = terminales_lookup.get(terminal_filtro, "Sin terminal") if terminal_filtro else "Sin terminal"
        empty = _build_empty_cuadre_caja_report(label, fecha_desde, fecha_hasta)
        empty["header_prefix"] = "INFORME DE TERMINAL"
        empty["detail_title"] = "DETALLE DE TERMINAL"
        empty["terminal_label"] = label
    elif modo == "usuario":
        label = usuarios_lookup.get(usuario_filtro, "Sin usuario") if usuario_filtro else "Sin usuario"
        empty = _build_empty_cuadre_caja_report(label, fecha_desde, fecha_hasta)
        empty["header_prefix"] = "INFORME DE USUARIO"
        empty["detail_title"] = "DETALLE DE USUARIO"
        empty["usuario_label"] = label
    else:
        empty = _build_empty_cuadre_caja_report("Todas las terminales y usuarios", fecha_desde, fecha_hasta)
        empty["header_prefix"] = "INFORME GENERAL"
        empty["detail_title"] = "DETALLE GENERAL"
    return [empty]


def cuadre_caja_view(request):
    ctx = _base_context(request, page_title="Caja - Cuadre de caja", active_nav="caja")
    if not ctx:
        return redirect("login")
    if not has_perm(ctx["auth_payload"]["usuario_id"], "caja", "ver_cuadre_caja"):
        return render_denied(request, active_nav="caja")

    today = timezone.localdate()
    fecha_desde = _parse_date_value(request.GET.get("fecha_desde")) or today
    fecha_hasta = _parse_date_value(request.GET.get("fecha_hasta")) or today
    if fecha_hasta < fecha_desde:
        fecha_desde, fecha_hasta = fecha_hasta, fecha_desde

    modo = str(request.GET.get("modo") or "general").strip().lower()
    if modo == "caja":
        modo = "terminal"
    if modo not in {"general", "terminal", "usuario"}:
        modo = "general"
    usuario_filtro = str(request.GET.get("usuario") or "").strip()
    terminal_filtro = str(request.GET.get("terminal") or request.GET.get("caja") or "").strip()

    usuarios = _load_cuadre_caja_usuarios()
    terminales = _load_cuadre_caja_terminales()
    usuarios_lookup = {item["id"]: item["label"] for item in usuarios if item.get("id")}
    terminales_lookup = {item["terminal"]: item["label"] for item in terminales if item.get("terminal")}

    try:
        movimientos = _load_cuadre_caja_movimientos(fecha_desde, fecha_hasta, usuarios_lookup=usuarios_lookup)
        reports = _build_cuadre_caja_reports(
            movimientos=movimientos,
            modo=modo,
            fecha_desde=fecha_desde,
            fecha_hasta=fecha_hasta,
            usuario_filtro=usuario_filtro,
            terminal_filtro=terminal_filtro,
            usuarios_lookup=usuarios_lookup,
            terminales_lookup=terminales_lookup,
        )
    except Exception:
        reports = [_build_empty_cuadre_caja_report("Todas las terminales y usuarios", fecha_desde, fecha_hasta)]
        ctx["cuadre_error"] = "No se pudieron cargar los movimientos del cuadre de caja."

    ctx.update(
        {
            "usuarios_cuadre": usuarios,
            "terminales_cuadre": terminales,
            "cuadre_reports": reports,
            "cuadre_filters": {
                "fecha_desde": _fmt_date_input(fecha_desde),
                "fecha_hasta": _fmt_date_input(fecha_hasta),
                "fecha_desde_fmt": _fmt_date(fecha_desde),
                "fecha_hasta_fmt": _fmt_date(fecha_hasta),
                "modo": modo,
                "usuario": usuario_filtro,
                "terminal": terminal_filtro,
            },
        }
    )
    return render(request, "caja/cuadre_caja.html", ctx)


@ensure_csrf_cookie
def financiamiento_view(request):
    ctx = _base_context(request, page_title="Caja - Financiamiento", active_nav="caja")
    if not ctx:
        return redirect("login")
    if not has_perm(ctx["auth_payload"]["usuario_id"], "caja", "ver_financiamiento"):
        return render_denied(request, active_nav="caja")
    usuario_id = ctx["auth_payload"]["usuario_id"]
    ctx["server_today_iso"] = _fmt_date_input(timezone.localdate())
    ctx["usuario_firma_b64"] = _load_firma_b64(usuario_id)
    ctx["fin_shortcuts"] = {
        "cuentas_por_cobrar": has_perm(usuario_id, "caja", "ver_cuentas_por_cobrar"),
        "factura": has_perm(usuario_id, "factura", "ver_documentos"),
        "prefactura": has_perm(usuario_id, "prefacturas", "ver"),
    }
    return render(request, "caja/financiamiento.html", ctx)


@require_GET
def financiamiento_buscar_view(request):
    auth_payload = _require_perm_json(request, "caja", "ver_financiamiento")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    query = (request.GET.get("q") or "").strip()
    filtro = (request.GET.get("filtro") or "documento").strip().lower()
    return JsonResponse({"results": _load_financiamiento_search_rows(query=query, filtro=filtro)})


@require_GET
def financiamiento_facturas_disponibles_view(request):
    auth_payload = _require_perm_json(request, "caja", "ver_financiamiento")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    query = (request.GET.get("q") or "").strip()
    filtro = (request.GET.get("filtro") or "nombre").strip().lower()
    return JsonResponse({"results": _load_financiamiento_facturas_disponibles(query=query, filtro=filtro)})


@require_GET
def financiamiento_detalle_view(request):
    auth_payload = _require_perm_json(request, "caja", "ver_financiamiento")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    no_doc = (request.GET.get("no_doc") or "").strip()
    if not no_doc:
        return JsonResponse({"detail": "Parametro no_doc requerido"}, status=400)

    record = _load_financiamiento_record(no_doc)
    if not record:
        return JsonResponse({"detail": "Financiamiento no encontrado."}, status=404)
    return JsonResponse(record)


@require_http_methods(["POST"])
def financiamiento_guardar_view(request):
    auth_payload = _require_perm_json(request, "caja", "ver_financiamiento")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"detail": "JSON invalido"}, status=400)
    client_event_id = str(payload.get("event_id") or "").strip()

    factura_no = str(payload.get("factura_no") or "").strip()
    if not factura_no:
        return JsonResponse({"detail": "Debes seleccionar la factura a financiar."}, status=400)

    try:
        detail_rows = _prepare_financiamiento_detail_rows(payload.get("detail") or [])
    except ValueError as exc:
        return JsonResponse({"detail": str(exc)}, status=400)

    try:
        plazo_payload = int(str(payload.get("plazo") or len(detail_rows)).strip() or len(detail_rows))
    except (TypeError, ValueError):
        plazo_payload = len(detail_rows)
    if plazo_payload != len(detail_rows):
        return JsonResponse({"detail": "El plazo no coincide con la cantidad de cuotas generadas."}, status=400)

    metodo = _normalize_financiamiento_choice(payload.get("metodo"), ["Lineal", "Insoluto"], "Lineal")
    tipo_cuota = _normalize_financiamiento_choice(
        payload.get("tipo_cuota"),
        ["Mensual", "Quincenal", "Semanal", "Diario", "Acuerdo"],
        "Mensual",
    )
    fecha_doc = _parse_date_value(payload.get("fecha")) or timezone.localdate()
    fecha_base = _parse_date_value(payload.get("fecha_base")) or fecha_doc
    porc_interes = max(Decimal("0"), min(_to_decimal(payload.get("porc_interes")), Decimal("100")))
    comentario = str(payload.get("comentario") or "").strip()
    record_lookup = str(payload.get("record_lookup") or "").strip()

    monto_payload = max(_to_decimal(payload.get("monto")), Decimal("0"))
    capital_total = sum((row.get("capital") or Decimal("0")) for row in detail_rows)
    if capital_total <= Decimal("0.01"):
        return JsonResponse({"detail": "El financiamiento debe tener un capital total mayor a 0."}, status=400)
    if monto_payload > Decimal("0.01") and not _values_match(monto_payload, capital_total):
        return JsonResponse({"detail": "La tabla de financiamiento no coincide con el monto del documento."}, status=400)

    usuario_id = int((auth_payload or {}).get("usuario_id") or 0) or None
    usuario_nombre = str((auth_payload or {}).get("usuario_nombre") or "").strip()
    terminal = _resolve_request_terminal(request, payload)

    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                save_result = _persist_financiamiento_record(
                    cursor,
                    record_lookup=record_lookup,
                    factura_no=factura_no,
                    fecha_doc=fecha_doc,
                    fecha_base=fecha_base,
                    metodo=metodo,
                    tipo_cuota=tipo_cuota,
                    porc_interes=porc_interes,
                    comentario=comentario,
                    detail_rows=detail_rows,
                    usuario_id=usuario_id,
                    usuario_nombre=usuario_nombre,
                    terminal=terminal,
                )
            record = _load_financiamiento_record(
                save_result.get("lookup")
                or save_result.get("loan_no")
                or save_result.get("factura_no")
            )
            event_document_id = str(
                save_result.get("lookup")
                or save_result.get("loan_no")
                or save_result.get("factura_no")
                or ""
            ).strip()
            event_factura_no = str(save_result.get("factura_no") or factura_no or "").strip()
            event_estado = str(((record or {}).get("prestamo") or {}).get("estado") or "").strip() or "Abierto"
            event_reason = "financiamiento-updated" if record_lookup else "financiamiento-saved"
            if event_document_id:
                transaction.on_commit(
                    lambda doc_id=event_document_id, fact_no=event_factura_no, estado=event_estado, reason=event_reason, eid=client_event_id: broadcast_financiamiento_document_status(
                        document_id=doc_id,
                        factura_no=fact_no,
                        estado=estado,
                        reason=reason,
                        event_id=eid,
                    )
                )
    except ValueError as exc:
        return JsonResponse({"detail": str(exc)}, status=400)
    except Exception as exc:
        return JsonResponse({"detail": f"No se pudo grabar el financiamiento: {exc}"}, status=500)

    if not record:
        return JsonResponse({"detail": "El financiamiento se grabo, pero no se pudo recargar automaticamente."}, status=500)

    return JsonResponse(
        {
            "detail": "Financiamiento actualizado correctamente." if record_lookup else "Financiamiento grabado correctamente.",
            "record": record,
        }
    )


# ==============================================================================
# POINT OF SALE (POS) VIEWS
# ==============================================================================

from django.views.decorators.csrf import ensure_csrf_cookie

@ensure_csrf_cookie
def caja_pos_view(request):
    ctx = _base_context(request, page_title="Caja - Venta POS", active_nav="pos")
    if not ctx:
        return redirect("login")
    if not has_perm(ctx["auth_payload"]["usuario_id"], "caja", "ver_pos"):
        return render_denied(request, active_nav="caja")
    return render(request, "caja/pos.html", ctx)


@require_GET
def caja_pos_session_status_view(request):
    auth_payload = _require_perm_json(request, "caja", "ver_pos")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload
    
    cajero = auth_payload["usuario_id"]
    terminal = _resolve_request_terminal(request)
    
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT TOP 1 IDcuadre, Fecha, Caja, Fondo, Confirmado, Observaciones
                FROM CuadreCaja
                WHERE Cajero = %s AND Caja = %s AND ISNULL(Confirmado, 'N') <> 'Y'
                ORDER BY Fecha DESC
                """,
                [cajero, terminal]
            )
            row = cursor.fetchone()
            
        if row:
            session_data = {
                "id": int(row[0]),
                "fecha": row[1].strftime("%Y-%m-%d %H:%M:%S") if row[1] else "",
                "caja": str(row[2] or "").strip(),
                "monto_inicial": float(row[3] or 0),
                "confirmado": str(row[4] or "").strip(),
                "observaciones": str(row[5] or "").strip()
            }
            return JsonResponse({"ok": True, "active": True, "session": session_data})
        else:
            return JsonResponse({"ok": True, "active": False})
    except Exception as exc:
        return JsonResponse({"detail": f"Error verificando caja: {exc}"}, status=500)


@require_http_methods(["POST"])
def caja_pos_session_open_view(request):
    auth_payload = _require_perm_json(request, "caja", "ver_pos")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload
        
    cajero = auth_payload["usuario_id"]
    terminal = _resolve_request_terminal(request)
    
    try:
        payload = json.loads(request.body.decode("utf-8"))
        monto_inicial = _to_decimal(payload.get("monto_inicial"), Decimal("0"))
        observaciones = str(payload.get("observaciones") or "").strip()
    except Exception:
        return JsonResponse({"detail": "JSON invalido"}, status=400)
        
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                # Verificar si ya existe abierta
                cursor.execute(
                    """
                    SELECT TOP 1 IDcuadre FROM CuadreCaja
                    WHERE Cajero = %s AND Caja = %s AND ISNULL(Confirmado, 'N') <> 'Y'
                    """,
                    [cajero, terminal]
                )
                if cursor.fetchone():
                    return JsonResponse({"detail": "Ya existe una sesion de caja abierta en esta terminal."}, status=400)
                
                # Obtener max id
                cursor.execute("SELECT ISNULL(MAX(IDcuadre), 0) + 1 FROM CuadreCaja WITH (UPDLOCK, HOLDLOCK)")
                new_id = int(cursor.fetchone()[0] or 1)
                
                # Insertar
                cursor.execute(
                    """
                    INSERT INTO CuadreCaja 
                    (IDcuadre, Fecha, Caja, Cajero, Fondo, Confirmado, Observaciones,
                     Efectivo, Cheque, Tarjeta, Credito, Regalo, NCredito, TotalVenta, TotalVentaPOS,
                     FondoUS, EfectivoUS, ChequeUS, TarjetaUS, CreditoUS, RegaloUS, NCreditoUS, TotalVentaUS,
                     FondoEU, EfectivoEU, ChequeEU, TarjetaEU, CreditoEU, RegaloEU, NCreditoEU, TotalVentaEU,
                     TotalEfectivo, TotalRI, Diferencia)
                    VALUES 
                    (%s, GETDATE(), %s, %s, %s, 'N', %s,
                     0, 0, 0, 0, 0, 0, 0, 0,
                     0, 0, 0, 0, 0, 0, 0, 0,
                     0, 0, 0, 0, 0, 0, 0, 0,
                     0, 0, 0)
                    """,
                    [new_id, terminal, cajero, monto_inicial, observaciones]
                )
        return JsonResponse({"ok": True, "session_id": new_id})
    except Exception as exc:
        return JsonResponse({"detail": f"No se pudo abrir la caja: {exc}"}, status=500)


@require_http_methods(["POST"])
def caja_pos_session_close_view(request):
    auth_payload = _require_perm_json(request, "caja", "ver_pos")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload
        
    cajero = auth_payload["usuario_id"]
    terminal = _resolve_request_terminal(request)
    
    try:
        payload = json.loads(request.body.decode("utf-8"))
        monto_cierre = _to_decimal(payload.get("monto_cierre"), Decimal("0"))
        observaciones = str(payload.get("observaciones") or "").strip()
    except Exception:
        return JsonResponse({"detail": "JSON invalido"}, status=400)
        
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                # Obtener session activa
                cursor.execute(
                    """
                    SELECT TOP 1 IDcuadre, Fecha, Fondo FROM CuadreCaja
                    WHERE Cajero = %s AND Caja = %s AND ISNULL(Confirmado, 'N') <> 'Y'
                    ORDER BY Fecha DESC
                    """,
                    [cajero, terminal]
                )
                row = cursor.fetchone()
                if not row:
                    return JsonResponse({"detail": "No hay una sesion de caja abierta en esta terminal."}, status=400)
                
                session_id = int(row[0])
                fecha_apertura = row[1]
                fondo = _to_decimal(row[2], Decimal("0"))
                
                # Sumar ventas pos
                cursor.execute(
                    """
                    SELECT 
                        COALESCE(SUM(EFECTIVO - CAMBIO), 0) AS total_efectivo,
                        COALESCE(SUM(TARJETA), 0) AS total_tarjeta,
                        COALESCE(SUM(TRANSFERENCIA), 0) AS total_transferencia,
                        COALESCE(SUM(TOTAL_DOC), 0) AS total_ventas
                    FROM CAB_POS
                    WHERE ID_USUARIO = %s AND TERMINAL = %s AND FECHA_CREACION >= %s AND ISNULL(CANCELADO, 'N') <> 'Y'
                    """,
                    [cajero, terminal, fecha_apertura]
                )
                sales_row = cursor.fetchone()
                total_efectivo = _to_decimal(sales_row[0], Decimal("0"))
                total_tarjeta = _to_decimal(sales_row[1], Decimal("0"))
                total_transferencia = _to_decimal(sales_row[2], Decimal("0"))
                total_ventas = _to_decimal(sales_row[3], Decimal("0"))
                
                expected_cash = fondo + total_efectivo
                difference = monto_cierre - expected_cash
                
                # Actualizar CuadreCaja
                cursor.execute(
                    """
                    UPDATE CuadreCaja
                    SET Confirmado = 'Y',
                        Efectivo = %s,
                        Tarjeta = %s,
                        TotalRI = %s,
                        TotalVenta = %s,
                        TotalVentaPOS = %s,
                        TotalEfectivo = %s,
                        Diferencia = %s,
                        Observaciones = %s
                    WHERE IDcuadre = %s
                    """,
                    [
                        total_efectivo,
                        total_tarjeta,
                        total_transferencia,
                        total_ventas,
                        total_ventas,
                        monto_cierre,
                        difference,
                        observaciones,
                        session_id
                    ]
                )
                
        summary = {
            "fondo": float(fondo),
            "efectivo_ventas": float(total_efectivo),
            "tarjeta_ventas": float(total_tarjeta),
            "transferencia_ventas": float(total_transferencia),
            "total_ventas": float(total_ventas),
            "monto_cierre": float(monto_cierre),
            "diferencia": float(difference)
        }
        return JsonResponse({"ok": True, "session_id": session_id, "summary": summary})
    except Exception as exc:
        return JsonResponse({"detail": f"No se pudo cerrar la caja: {exc}"}, status=500)


@require_GET
def caja_pos_buscar_articulo_view(request):
    auth_payload = _require_perm_json(request, "caja", "ver_pos")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload
        
    q = (request.GET.get("q") or "").strip()
    if not q:
        return JsonResponse({"results": []})
        
    from django.db.models import Q
    from prefacturas_app.models_existing import MaestroArticulo
    from prefacturas_app.models import CodigoVariable
    
    scale_rule = None
    parsed_code = None
    parsed_value = None
    
    # 1. Comprobar si el query coincide con alguna regla de balanza activa
    if q.isdigit() and len(q) >= 8:
        rules = CodigoVariable.objects.filter(activo="Y")
        for rule in rules:
            if q.startswith(rule.prefijo):
                scale_rule = rule
                break
                
    if scale_rule:
        try:
            start_p = scale_rule.pos_producto
            len_p = scale_rule.len_producto
            parsed_code = q[start_p : start_p + len_p]
            
            start_v = scale_rule.pos_valor
            len_v = scale_rule.len_valor
            val_str = q[start_v : start_v + len_v]
            
            raw_val = float(val_str)
            parsed_value = raw_val / float(scale_rule.divisor_valor)
        except Exception:
            scale_rule = None
            
    # 2. Realizar la búsqueda
    qs = MaestroArticulo.objects.exclude(bloqueado__iexact="Y")
    
    articles = []
    
    if scale_rule and parsed_code:
        # Búsqueda por balanza
        candidates = [parsed_code]
        stripped = parsed_code.lstrip("0")
        if stripped and stripped not in candidates:
            candidates.append(stripped)
            
        exact_match = qs.filter(Q(cod_barra__in=candidates) | Q(id_articulo__in=candidates))
        if exact_match.exists():
            matched_list = list(exact_match)
            matched_list.sort(key=lambda a: 0 if a.cod_barra.strip() in candidates else 1)
            articles = matched_list[:1]
        else:
            # Fallback a búsqueda normal
            scale_rule = None
            
    if not scale_rule or not articles:
        # Búsqueda estándar
        candidates = [q]
        q_clean = q.strip()
        if q_clean.isdigit():
            stripped = q_clean.lstrip("0")
            if stripped and stripped not in candidates:
                candidates.append(stripped)
            if len(q_clean) > 1:
                wo_last = q_clean[:-1]
                if wo_last not in candidates:
                    candidates.append(wo_last)
                stripped_wo_last = stripped[:-1]
                if stripped_wo_last and stripped_wo_last not in candidates:
                    candidates.append(stripped_wo_last)
                    
        exact_match = qs.filter(cod_barra__in=candidates)
        if exact_match.exists():
            matched_list = list(exact_match)
            matched_list.sort(key=lambda a: candidates.index(a.cod_barra.strip()) if a.cod_barra.strip() in candidates else 999)
            articles = matched_list[:1]
        else:
            articles = list(qs.filter(Q(referencia__icontains=q) | Q(descrip_art__icontains=q))[:40])
            
    if not articles:
        return JsonResponse({"results": []})
        
    articulo_ids = [a.id_articulo for a in articles]
    tarj_stock = {}
    if articulo_ids:
        with connection.cursor() as cursor:
            placeholders = ", ".join(["%s"] * len(articulo_ids))
            cursor.execute(
                f"SELECT ID_ARTICULO, COALESCE(SUM(CANTIDAD), 0) FROM TARJETERO WHERE ID_ARTICULO IN ({placeholders}) GROUP BY ID_ARTICULO",
                articulo_ids
            )
            tarj_stock = {str(row[0]).strip(): float(row[1] or 0) for row in cursor.fetchall()}
            
    results = []
    for a in articles:
        art_id = a.id_articulo.strip()
        
        es_pesado = False
        cantidad_pesada = 1.0
        tipo_variable = "normal"
        
        if scale_rule and parsed_code:
            es_pesado = True
            cantidad_pesada = parsed_value
            tipo_variable = scale_rule.tipo
            
        results.append({
            "id_articulo": art_id,
            "descrip_art": a.descrip_art.strip() if a.descrip_art else "",
            "referencia": a.referencia.strip() if a.referencia else "",
            "cod_barra": a.cod_barra.strip() if a.cod_barra else "",
            "precio_det": float(a.precio_det or 0),
            "tarifa_vt": float(a.tarifa_vt or 0),
            "stock": tarj_stock.get(art_id, 0.0),
            "id_impto_vt": a.id_impto_vt,
            "es_pesado": es_pesado,
            "cantidad_pesada": cantidad_pesada,
            "tipo_variable": tipo_variable,
        })
        
    return JsonResponse({"results": results})


@require_http_methods(["POST"])
def caja_pos_save_sale_view(request):
    auth_payload = _require_perm_json(request, "caja", "ver_pos")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload
        
    cajero = auth_payload["usuario_id"]
    terminal = _resolve_request_terminal(request)
    
    try:
        payload = json.loads(request.body.decode("utf-8"))
        cliente_id = str(payload.get("cliente_id") or "1").strip()
        cliente_nombre = str(payload.get("cliente_nombre") or "Cliente General").strip()
        cliente_rnc = str(payload.get("cliente_rnc") or "").strip()
        items = payload.get("items") or []
        efectivo_pagado = _to_decimal(payload.get("efectivo_pagado"), Decimal("0"))
        tarjeta_pagado = _to_decimal(payload.get("tarjeta_pagado"), Decimal("0"))
        transferencia_pagado = _to_decimal(payload.get("transferencia_pagado"), Decimal("0"))
        cambio = _to_decimal(payload.get("cambio"), Decimal("0"))
        subtotal = _to_decimal(payload.get("subtotal"), Decimal("0"))
        total_desc = _to_decimal(payload.get("total_desc"), Decimal("0"))
        total_itbis = _to_decimal(payload.get("total_itbis"), Decimal("0"))
        total_doc = _to_decimal(payload.get("total_doc"), Decimal("0"))
        comentario = str(payload.get("comentario") or "").strip()
    except Exception:
        return JsonResponse({"detail": "JSON invalido"}, status=400)
        
    if not items:
        return JsonResponse({"detail": "Debes agregar al menos un articulo."}, status=400)
        
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                # Validar caja abierta
                cursor.execute(
                    """
                    SELECT TOP 1 IDcuadre, Fecha FROM CuadreCaja
                    WHERE Cajero = %s AND Caja = %s AND ISNULL(Confirmado, 'N') <> 'Y'
                    ORDER BY Fecha DESC
                    """,
                    [cajero, terminal]
                )
                session_row = cursor.fetchone()
                if not session_row:
                    return JsonResponse({"detail": "No tienes una sesion de caja abierta en esta terminal. Abre caja primero."}, status=400)
                
                session_id = int(session_row[0])
                
                # Obtener max ID_DOC
                cursor.execute("SELECT ISNULL(MAX(TRY_CAST(ID_DOC AS BIGINT)), 0) + 1 FROM CAB_POS WITH (UPDLOCK, HOLDLOCK)")
                new_id_doc = int(cursor.fetchone()[0] or 1)
                
                # Insertar en CAB_POS
                cab_columns = _load_table_columns("CAB_POS")
                cab_values = {}
                _assign_existing_values(cab_values, cab_columns, new_id_doc, "ID_DOC")
                _assign_existing_values(cab_values, cab_columns, new_id_doc, "ID_DOC_BASE", "NO_DOC", "NO_ED", "NO_RECIBO")
                _assign_existing_values(cab_values, cab_columns, "FC", "TIPO_DOC", "TIPO")
                _assign_existing_values(cab_values, cab_columns, "N", "CANCELADO")
                _assign_existing_values(cab_values, cab_columns, "N", "IMPRESO")
                _assign_existing_values(cab_values, cab_columns, "Cerrado", "EST_DOC", "ESTATUS")
                _assign_existing_values(cab_values, cab_columns, cliente_id, "ID_SN")
                _assign_existing_values(cab_values, cab_columns, cliente_nombre, "NOM_SOCIO")
                _assign_existing_values(cab_values, cab_columns, cliente_rnc, "RNC_CED")
                _assign_existing_values(cab_values, cab_columns, timezone.localdate(), "FECHA_CONT", "FECHA_DOC", "FECHA_VENC")
                _assign_existing_values(cab_values, cab_columns, subtotal, "SUBTOTAL")
                _assign_existing_values(cab_values, cab_columns, total_desc, "TOTAL_DESC")
                _assign_existing_values(cab_values, cab_columns, total_itbis, "TOTAL_ITBIS")
                _assign_existing_values(cab_values, cab_columns, total_doc, "TOTAL_DOC")
                _assign_existing_values(cab_values, cab_columns, "RD$", "MON_DOC")
                _assign_existing_values(cab_values, cab_columns, efectivo_pagado, "EFECTIVO", "RECIBO_E")
                _assign_existing_values(cab_values, cab_columns, tarjeta_pagado, "TARJETA", "RECIBO_T")
                _assign_existing_values(cab_values, cab_columns, transferencia_pagado, "TRANSFERENCIA", "RECIBO_F")
                _assign_existing_values(cab_values, cab_columns, cambio, "CAMBIO")
                _assign_existing_values(cab_values, cab_columns, comentario, "COMENTARIO")
                _assign_existing_values(cab_values, cab_columns, cajero, "ID_USUARIO")
                _assign_existing_values(cab_values, cab_columns, terminal, "TERMINAL")
                _assign_existing_values(cab_values, cab_columns, timezone.localtime(), "FECHA_CREACION")
                _assign_existing_values(cab_values, cab_columns, "Y", "POSTEADO")
                _assign_existing_values(cab_values, cab_columns, timezone.localdate().year, "EJERCICIO")
                _assign_existing_values(cab_values, cab_columns, str(timezone.localdate().month), "PERIODO_CONT")
                
                _insert_dynamic_row(cursor, "CAB_POS", cab_columns, cab_values)
                
                # Insertar en DET_POS, actualizar stock, insertar en TARJETERO
                det_columns = _load_table_columns("DET_POS")
                tarjetero_columns = _load_table_columns("TARJETERO")
                
                for idx, item in enumerate(items, start=1):
                    qty = _to_decimal(item["cantidad"], Decimal("1"))
                    price = _to_decimal(item["precio_unit"], Decimal("0"))
                    desc_pct = _to_decimal(item.get("porc_desc"), Decimal("0"))
                    impto_pct = _to_decimal(item.get("tarifa_vt"), Decimal("18"))
                    id_impto = _to_int_or_none(item.get("id_impto_vt")) or 1
                    
                    total_precio = qty * price
                    total_desc_monto = total_precio * (desc_pct / Decimal("100"))
                    total_precio_neto = total_precio - total_desc_monto
                    itbis_line = total_precio_neto * (impto_pct / Decimal("100"))
                    total_linea = total_precio_neto + itbis_line
                    
                    # Cargar costo
                    cursor.execute("SELECT COSTO FROM MAESTRO_ARTICULO WHERE ID_ARTICULO = %s", [item["id_articulo"]])
                    cost_row = cursor.fetchone()
                    cost = _to_decimal(cost_row[0] if cost_row else Decimal("1.0"), Decimal("1.0"))
                    
                    det_values = {}
                    _assign_existing_values(det_values, det_columns, new_id_doc, "ID_DOC")
                    _assign_existing_values(det_values, det_columns, idx, "No_LINEA", "LINEA")
                    _assign_existing_values(det_values, det_columns, "POS", "CLASE_DOC_BASE")
                    _assign_existing_values(det_values, det_columns, new_id_doc, "REF_DOC_BASE")
                    _assign_existing_values(det_values, det_columns, "C", "ESTATUS_LINEA")
                    _assign_existing_values(det_values, det_columns, item["id_articulo"], "ID_ARTICULO")
                    _assign_existing_values(det_values, det_columns, item["descrip_art"], "DESCRIP_ART")
                    _assign_existing_values(det_values, det_columns, qty, "CANTIDAD", "CANT_ENT")
                    _assign_existing_values(det_values, det_columns, Decimal("0"), "CANT_PEND")
                    _assign_existing_values(det_values, det_columns, price, "PRECIO", "PRECIO_BRUTO")
                    _assign_existing_values(det_values, det_columns, desc_pct, "PORC_DESC")
                    _assign_existing_values(det_values, det_columns, id_impto, "ID_IMPTO")
                    _assign_existing_values(det_values, det_columns, impto_pct, "TARIFA", "ITBIS")
                    _assign_existing_values(det_values, det_columns, itbis_line, "TOTAL_ITBIS")
                    _assign_existing_values(det_values, det_columns, total_desc_monto, "TOTAL_DESC")
                    _assign_existing_values(det_values, det_columns, total_precio, "TOTAL_PRECIO")
                    _assign_existing_values(det_values, det_columns, total_precio_neto, "TOTAL_PRECIO_NETO")
                    _assign_existing_values(det_values, det_columns, total_linea, "TOTAL_LINEA")
                    _assign_existing_values(det_values, det_columns, cost, "COSTO")
                    _assign_existing_values(det_values, det_columns, cost * qty, "TOTAL_COSTO")
                    _assign_existing_values(det_values, det_columns, 1, "ID_ALMACEN")
                    _assign_existing_values(det_values, det_columns, cajero, "ID_VENDEDOR")
                    _assign_existing_values(det_values, det_columns, timezone.localdate(), "FECHA_CONT")
                    _assign_existing_values(det_values, det_columns, str(timezone.localdate().month), "PERIODO_CONT")
                    _assign_existing_values(det_values, det_columns, timezone.localdate().year, "EJERCICIO")
                    
                    _insert_dynamic_row(cursor, "DET_POS", det_columns, det_values, skip_columns=["ID_DETALLE"])
                    
                    # Descontar stock
                    cursor.execute(
                        """
                        UPDATE MAESTRO_ARTICULO
                        SET STOCK = ISNULL(STOCK, 0) - %s,
                            FECHA_ACT = GETDATE()
                        WHERE ID_ARTICULO = %s
                        """,
                        [qty, item["id_articulo"]]
                    )
                    
                    # Insertar en TARJETERO
                    tarj_values = {
                        "TIPO_DOC": "POS",
                        "ID_DOC": str(new_id_doc),
                        "ID_SN": cliente_id,
                        "NOM_SN": cliente_nombre,
                        "ID_ARTICULO": item["id_articulo"],
                        "DESCRIP_ART": item["descrip_art"],
                        "CANTIDAD": -qty,
                        "COSTO": cost,
                        "PRECIO": price,
                        "TOTAL_COSTO": -qty * cost,
                        "TOTAL_PRECIO": -qty * price,
                        "CTA_INV": "11030101",
                        "LOTE": "No",
                        "FECHA_CONT": timezone.localdate(),
                        "FECHA_VENC": timezone.localdate(),
                        "FECHA_DOC": timezone.localdate(),
                        "FECHA_CREACION": timezone.localtime(),
                        "MONEDA": "RD$",
                        "ID_ALMACEN": 1,
                        "ID_USUARIO": cajero,
                        "PERIODO_CONT": timezone.localdate().month,
                        "EJERCICIO": timezone.localdate().year,
                    }
                    _insert_dynamic_row(cursor, "TARJETERO", tarjetero_columns, tarj_values)
                    
        return JsonResponse({"ok": True, "id_doc": new_id_doc})
    except Exception as exc:
        return JsonResponse({"detail": f"No se pudo guardar la venta: {exc}"}, status=500)


@require_GET
def caja_pos_ticket_print_data_view(request):
    auth_payload = _require_perm_json(request, "caja", "ver_pos")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload
        
    id_doc = (request.GET.get("id_doc") or "").strip()
    if not id_doc:
        return JsonResponse({"detail": "Parametro id_doc requerido"}, status=400)
        
    try:
        with connection.cursor() as cursor:
            # Cargar cabecera
            cursor.execute("SELECT * FROM CAB_POS WHERE CAST(ID_DOC AS VARCHAR(50)) = %s", [id_doc])
            cab_cols = [col[0] for col in cursor.description]
            cab_row = cursor.fetchone()
            if not cab_row:
                return JsonResponse({"detail": "Venta POS no encontrada."}, status=404)
            header = _normalize_result_row(cab_cols, cab_row)
            
            # Cargar detalle
            cursor.execute("SELECT * FROM DET_POS WHERE CAST(ID_DOC AS VARCHAR(50)) = %s ORDER BY No_LINEA", [id_doc])
            det_cols = [col[0] for col in cursor.description]
            det_rows = cursor.fetchall()
            detail = [_normalize_result_row(det_cols, r) for r in det_rows]
            
        empresa = _get_empresa_data()
        
        # Desglose de formas de pago
        pago_lineas = []
        efectivo = _to_float(header.get("EFECTIVO"))
        tarjeta = _to_float(header.get("TARJETA"))
        transf = _to_float(header.get("TRANSFERENCIA"))
        cambio = _to_float(header.get("CAMBIO"))
        
        if efectivo > 0:
            pago_lineas.append({"label": "Efectivo", "monto": efectivo, "cambio": cambio})
        if tarjeta > 0:
            pago_lineas.append({"label": "Tarjeta", "monto": tarjeta, "cambio": 0.0})
        if transf > 0:
            pago_lineas.append({"label": "Transferencia", "monto": transf, "cambio": 0.0})
            
        print_data = {
            "empresa": empresa,
            "header": {
                "id_doc": _stringify_doc(header.get("ID_DOC")),
                "fecha": header.get("FECHA_CREACION").strftime("%d/%m/%Y %I:%M %p") if header.get("FECHA_CREACION") else "",
                "cliente_nombre": str(header.get("NOM_SOCIO") or "Cliente General").strip(),
                "cliente_rnc": str(header.get("RNC_CED") or "").strip(),
                "tipo_doc": str(header.get("TIPO_DOC") or "POS").strip(),
                "comentario": str(header.get("COMENTARIO") or "").strip(),
                "subtotal": _to_float(header.get("SUBTOTAL")),
                "total_desc": _to_float(header.get("TOTAL_DESC")),
                "total_itbis": _to_float(header.get("TOTAL_ITBIS")),
                "total_doc": _to_float(header.get("TOTAL_DOC")),
                "cajero": str(header.get("ID_USUARIO") or "").strip(),
                "terminal": str(header.get("TERMINAL") or "").strip(),
            },
            "detail": [
                {
                    "id_articulo": str(r.get("ID_ARTICULO") or "").strip(),
                    "descrip_art": str(r.get("DESCRIP_ART") or "").strip(),
                    "cantidad": _to_float(r.get("CANTIDAD")),
                    "precio": _to_float(r.get("PRECIO")),
                    "total_linea": _to_float(r.get("TOTAL_LINEA")),
                    "porc_desc": _to_float(r.get("PORC_DESC")),
                }
                for r in detail
            ],
            "pagos": pago_lineas
        }
        if request.GET.get("format") == "html":
            return render(request, "caja/pos_ticket.html", {"print_data": print_data})
        return JsonResponse({"print_data": print_data})
    except Exception as exc:
        return JsonResponse({"detail": f"Error cargando ticket de venta: {exc}"}, status=500)


@require_GET
def caja_pos_session_print_close_view(request):
    auth_payload = _require_perm_json(request, "caja", "ver_pos")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload
        
    session_id = (request.GET.get("session_id") or "").strip()
    if not session_id:
        return JsonResponse({"detail": "Parametro session_id requerido"}, status=400)
        
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT 
                    IDcuadre, Fecha, Caja, Cajero, Fondo,
                    Efectivo, Tarjeta, TotalRI, TotalVenta,
                    TotalEfectivo, Diferencia, Observaciones
                FROM CuadreCaja
                WHERE CAST(IDcuadre AS VARCHAR(50)) = %s
                """,
                [session_id]
            )
            row = cursor.fetchone()
            if not row:
                return JsonResponse({"detail": "Cierre de caja no encontrado."}, status=404)
                
            cols = [col[0] for col in cursor.description]
            data = _normalize_result_row(cols, row)
            
        empresa = _get_empresa_data()
        
        # Cargar nombre del cajero
        cajero_meta = _load_usuario_meta(data.get("CAJERO"))
        cajero_nombre = cajero_meta.get("nombre") or str(data.get("CAJERO") or "").strip()
        
        fondo = _to_float(data.get("FONDO"))
        efectivo_ventas = _to_float(data.get("EFECTIVO"))
        tarjeta_ventas = _to_float(data.get("TARJETA"))
        transferencia_ventas = _to_float(data.get("TOTALRI"))
        total_ventas = _to_float(data.get("TOTALVENTA"))
        monto_cierre = _to_float(data.get("TOTALEFECTIVO"))
        diferencia = _to_float(data.get("DIFERENCIA"))
        
        expected_cash = fondo + efectivo_ventas
        
        fecha_apert = data.get("FECHA")
        fecha_cierr = timezone.localtime()
        
        print_data = {
            "empresa": empresa,
            "session_id": str(data.get("IDCUADRE")),
            "cajero": cajero_nombre,
            "terminal": str(data.get("CAJA") or "Caja-1").strip(),
            "fecha_apertura": fecha_apert.strftime("%d/%m/%Y %I:%M %p") if fecha_apert else "",
            "fecha_cierre": fecha_cierr.strftime("%d/%m/%Y %I:%M %p") if fecha_cierr else "",
            "observaciones": str(data.get("OBSERVACIONES") or "").strip(),
            "expected_cash": expected_cash,
            "summary": {
                "fondo": fondo,
                "efectivo_ventas": efectivo_ventas,
                "tarjeta_ventas": tarjeta_ventas,
                "transferencia_ventas": transferencia_ventas,
                "total_ventas": total_ventas,
                "monto_cierre": monto_cierre,
                "diferencia": diferencia
            }
        }
        
        if request.GET.get("format") == "html":
            return render(request, "caja/pos_cierre_ticket.html", {"print_data": print_data})
        return JsonResponse({"print_data": print_data})
    except Exception as exc:
        return JsonResponse({"detail": f"Error cargando ticket de cierre: {exc}"}, status=500)


@require_GET
def caja_pos_buscar_cliente_view(request):
    auth_payload = _require_perm_json(request, "caja", "ver_pos")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload
        
    q = (request.GET.get("q") or "").strip()
    if not q:
        return JsonResponse({"results": []})
        
    from django.db.models import Q
    from prefacturas_app.models_existing import MaestroSn
    
    clientes = MaestroSn.objects.exclude(bloqueado__iexact="Y")
    clientes = clientes.filter(Q(id_sn__icontains=q) | Q(nom_socio__icontains=q) | Q(rnc_ced__icontains=q))[:40]
    
    results = []
    for c in clientes:
        results.append({
            "id_sn": c.id_sn.strip() if c.id_sn else "",
            "nom_socio": c.nom_socio.strip() if c.nom_socio else "",
            "rnc_ced": c.rnc_ced.strip() if c.rnc_ced else "",
        })
        
    return JsonResponse({"results": results})


@require_GET
def caja_pos_sales_list_view(request):
    auth_payload = _require_perm_json(request, "caja", "ver_pos")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload
        
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT TOP 100 
                    ID_DOC, FECHA_CREACION, NOM_SOCIO, TOTAL_DOC, ISNULL(CANCELADO, 'N') AS CANCELADO, ID_USUARIO
                FROM CAB_POS
                ORDER BY FECHA_CREACION DESC
                """
            )
            cols = [col[0] for col in cursor.description]
            rows = cursor.fetchall()
            sales = []
            for r in rows:
                normalized = _normalize_result_row(cols, r)
                sales.append({
                    "id_doc": _stringify_doc(normalized.get("ID_DOC")),
                    "fecha": normalized.get("FECHA_CREACION").strftime("%d/%m/%Y %I:%M %p") if normalized.get("FECHA_CREACION") else "",
                    "cliente": str(normalized.get("NOM_SOCIO") or "Cliente General").strip(),
                    "total": _to_float(normalized.get("TOTAL_DOC")),
                    "cancelado": str(normalized.get("CANCELADO") or "N").strip(),
                    "cajero": str(normalized.get("ID_USUARIO") or "").strip(),
                })
        return JsonResponse({"ok": True, "sales": sales})
    except Exception as exc:
        return JsonResponse({"detail": f"Error listando ventas: {exc}"}, status=500)


@require_GET
def caja_pos_sales_detail_view(request):
    auth_payload = _require_perm_json(request, "caja", "ver_pos")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload
        
    id_doc = (request.GET.get("id_doc") or "").strip()
    if not id_doc:
        return JsonResponse({"detail": "Parametro id_doc requerido"}, status=400)
        
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM CAB_POS WHERE CAST(ID_DOC AS VARCHAR(50)) = %s", [id_doc])
            cab_cols = [col[0] for col in cursor.description]
            cab_row = cursor.fetchone()
            if not cab_row:
                return JsonResponse({"detail": "Venta POS no encontrada."}, status=404)
            header = _normalize_result_row(cab_cols, cab_row)
            
            cursor.execute("SELECT * FROM DET_POS WHERE CAST(ID_DOC AS VARCHAR(50)) = %s ORDER BY No_LINEA", [id_doc])
            det_cols = [col[0] for col in cursor.description]
            det_rows = cursor.fetchall()
            detail = [_normalize_result_row(det_cols, r) for r in det_rows]
            
        print_data = {
            "header": {
                "id_doc": _stringify_doc(header.get("ID_DOC")),
                "fecha": header.get("FECHA_CREACION").strftime("%d/%m/%Y %I:%M %p") if header.get("FECHA_CREACION") else "",
                "cliente_id": str(header.get("ID_SN") or "1").strip(),
                "cliente_nombre": str(header.get("NOM_SOCIO") or "Cliente General").strip(),
                "cliente_rnc": str(header.get("RNC_CED") or "").strip(),
                "tipo_doc": str(header.get("TIPO_DOC") or "POS").strip(),
                "comentario": str(header.get("COMENTARIO") or "").strip(),
                "subtotal": _to_float(header.get("SUBTOTAL")),
                "total_desc": _to_float(header.get("TOTAL_DESC")),
                "total_itbis": _to_float(header.get("TOTAL_ITBIS")),
                "total_doc": _to_float(header.get("TOTAL_DOC")),
                "cajero": str(header.get("ID_USUARIO") or "").strip(),
                "terminal": str(header.get("TERMINAL") or "").strip(),
                "cancelado": str(header.get("CANCELADO") or "N").strip(),
                "efectivo": _to_float(header.get("EFECTIVO")),
                "tarjeta": _to_float(header.get("TARJETA")),
                "transferencia": _to_float(header.get("TRANSFERENCIA")),
                "cambio": _to_float(header.get("CAMBIO")),
            },
            "detail": [
                {
                    "id_articulo": str(r.get("ID_ARTICULO") or "").strip(),
                    "descrip_art": str(r.get("DESCRIP_ART") or "").strip(),
                    "cantidad": _to_float(r.get("CANTIDAD")),
                    "precio_unit": _to_float(r.get("PRECIO")),
                    "porc_desc": _to_float(r.get("PORC_DESC")),
                    "id_impto_vt": _to_int_or_none(r.get("ID_IMPTO")) or 1,
                    "tarifa_vt": 18.0,
                }
                for r in detail
            ]
        }
        
        if print_data["detail"]:
            art_ids = [d["id_articulo"] for d in print_data["detail"]]
            with connection.cursor() as cursor:
                placeholders = ", ".join(["%s"] * len(art_ids))
                cursor.execute(
                    f"SELECT ID_ARTICULO, ISNULL(TARIFA_VT, 18.0) FROM MAESTRO_ARTICULO WHERE ID_ARTICULO IN ({placeholders})",
                    art_ids
                )
                rates = {str(row[0]).strip(): float(row[1] or 18.0) for row in cursor.fetchall()}
                for d in print_data["detail"]:
                    d["tarifa_vt"] = rates.get(d["id_articulo"], 18.0)
                    
        return JsonResponse({"ok": True, "sale": print_data})
    except Exception as exc:
        return JsonResponse({"detail": f"Error cargando detalle de venta: {exc}"}, status=500)


@require_http_methods(["POST"])
def caja_pos_sales_cancel_view(request):
    auth_payload = _require_perm_json(request, "caja", "ver_pos")
    if isinstance(auth_payload, JsonResponse):
        return auth_payload
        
    try:
        payload = json.loads(request.body.decode("utf-8"))
        id_doc = str(payload.get("id_doc") or "").strip()
    except Exception:
        return JsonResponse({"detail": "JSON invalido"}, status=400)
        
    if not id_doc:
        return JsonResponse({"detail": "Parametro id_doc requerido"}, status=400)
        
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SELECT ID_DOC, ISNULL(CANCELADO, 'N') FROM CAB_POS WITH (UPDLOCK, HOLDLOCK) WHERE CAST(ID_DOC AS VARCHAR(50)) = %s", [id_doc])
                header_row = cursor.fetchone()
                if not header_row:
                    return JsonResponse({"detail": "Venta POS no encontrada."}, status=404)
                if header_row[1] == "Y":
                    return JsonResponse({"detail": "Esta venta ya ha sido cancelada previamente."}, status=400)
                
                cursor.execute(
                    """
                    UPDATE CAB_POS
                    SET CANCELADO = 'Y',
                        EST_DOC = 'Cancelado'
                    WHERE CAST(ID_DOC AS VARCHAR(50)) = %s
                    """,
                    [id_doc]
                )
                
                cursor.execute("SELECT ID_ARTICULO, DESCRIP_ART, CANTIDAD, PRECIO FROM DET_POS WHERE CAST(ID_DOC AS VARCHAR(50)) = %s", [id_doc])
                lines = cursor.fetchall()
                
                tarjetero_columns = _load_table_columns("TARJETERO")
                cajero = auth_payload["usuario_id"]
                
                for line in lines:
                    art_id = str(line[0]).strip()
                    desc = str(line[1] or "").strip()
                    qty = _to_decimal(line[2], Decimal("0"))
                    price = _to_decimal(line[3], Decimal("0"))
                    
                    if qty <= 0:
                        continue
                        
                    cursor.execute(
                        """
                        UPDATE MAESTRO_ARTICULO
                        SET STOCK = ISNULL(STOCK, 0) + %s,
                            FECHA_ACT = GETDATE()
                        WHERE ID_ARTICULO = %s
                        """,
                        [qty, art_id]
                    )
                    
                    cursor.execute("SELECT COSTO FROM MAESTRO_ARTICULO WHERE ID_ARTICULO = %s", [art_id])
                    cost_row = cursor.fetchone()
                    cost = _to_decimal(cost_row[0] if cost_row else Decimal("1.0"), Decimal("1.0"))
                    
                    tarj_values = {
                        "TIPO_DOC": "POS_ANUL",
                        "ID_DOC": id_doc,
                        "ID_SN": "1",
                        "NOM_SN": "Devolucion POS",
                        "ID_ARTICULO": art_id,
                        "DESCRIP_ART": desc,
                        "CANTIDAD": qty,
                        "COSTO": cost,
                        "PRECIO": price,
                        "TOTAL_COSTO": qty * cost,
                        "TOTAL_PRECIO": qty * price,
                        "CTA_INV": "11030101",
                        "LOTE": "No",
                        "FECHA_CONT": timezone.localdate(),
                        "FECHA_VENC": timezone.localdate(),
                        "FECHA_DOC": timezone.localdate(),
                        "FECHA_CREACION": timezone.localtime(),
                        "MONEDA": "RD$",
                        "ID_ALMACEN": 1,
                        "ID_USUARIO": cajero,
                        "PERIODO_CONT": timezone.localdate().month,
                        "EJERCICIO": timezone.localdate().year,
                    }
                    _insert_dynamic_row(cursor, "TARJETERO", tarjetero_columns, tarj_values)
                    
        return JsonResponse({"ok": True})
    except Exception as exc:
        return JsonResponse({"detail": f"Error cancelando venta: {exc}"}, status=500)
