from __future__ import annotations

from decimal import Decimal

from django.db import connection

from core.views import _get_empresa_data


def _to_float(value):
    try:
        return float(Decimal(value or 0))
    except Exception:
        return 0.0


def _fmt_date(value):
    if not value:
        return ""
    try:
        return value.isoformat()
    except Exception:
        return str(value)


def build_document_snapshot(documento_id):
    header = None
    detalles = []

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT TOP 1
                ID_DOC, ID_DOC_PV, ID_DOC_BASE, TIPO_DOC_BASE, FECHA_DOC, FECHA_VENC,
                ID_SN, NOM_SOCIO, RNC_CED, CONTACTO, ENT_FACTURA, ENT_MERCANCIA, COMENTARIO,
                SUBTOTAL, TOTAL_DESC, TOTAL_ITBIS, TOTAL_DOC, MON_DOC, NCF, NCF_NC, ID_NCF,
                TIPO, CONDICION, DIA, UPPER(ISNULL(CANCELADO, 'N'))
            FROM CAB_FACTURA
            WHERE TRY_CAST(ID_DOC AS BIGINT) = %s
            """,
            [documento_id],
        )
        row = cursor.fetchone()
        if row:
            header = {
                "id_doc": str(row[0] or ""),
                "id_doc_pv": str(row[1] or ""),
                "id_doc_base": str(row[2] or ""),
                "tipo_doc_base": str(row[3] or ""),
                "fecha_doc": _fmt_date(row[4]),
                "fecha_venc": _fmt_date(row[5]),
                "id_sn": str(row[6] or ""),
                "nom_socio": str(row[7] or ""),
                "rnc_ced": str(row[8] or ""),
                "contacto": str(row[9] or ""),
                "ent_factura": str(row[10] or ""),
                "ent_mercancia": str(row[11] or ""),
                "comentario": str(row[12] or ""),
                "subtotal": _to_float(row[13]),
                "total_desc": _to_float(row[14]),
                "total_itbis": _to_float(row[15]),
                "total_doc": _to_float(row[16]),
                "mon_doc": str(row[17] or ""),
                "encf": str(row[18] or ""),
                "ncf_nc": str(row[19] or ""),
                "id_ncf": int(row[20] or 0) if row[20] is not None else 0,
                "tipo": str(row[21] or ""),
                "condicion": str(row[22] or ""),
                "dia": int(row[23] or 0) if row[23] is not None else 0,
                "cancelado": str(row[24] or "") == "Y",
            }

        cursor.execute(
            """
            SELECT
                No_LINEA, ID_ARTICULO, DESCRIP_ART, CANTIDAD, CANT_ENT, MEDIDA, PRECIO, PRECIO_BRUTO,
                PORC_DESC, TOTAL_ITBIS, TOTAL_LINEA, CEBE, CECO, REFERENCIA, OBSERVACION
            FROM DET_FACTURA
            WHERE TRY_CAST(ID_DOC AS BIGINT) = %s
            ORDER BY No_LINEA, ID_DETALLE
            """,
            [documento_id],
        )
        for row in cursor.fetchall():
            detalles.append(
                {
                    "no_linea": int(row[0] or 0) if row[0] is not None else 0,
                    "id_articulo": str(row[1] or ""),
                    "descrip_art": str(row[2] or ""),
                    "cantidad": _to_float(row[3]),
                    "cant_ent": _to_float(row[4]),
                    "medida": str(row[5] or ""),
                    "precio": _to_float(row[6]),
                    "precio_bruto": _to_float(row[7]),
                    "porc_desc": _to_float(row[8]),
                    "total_itbis": _to_float(row[9]),
                    "total_linea": _to_float(row[10]),
                    "cebe": str(row[11] or ""),
                    "ceco": str(row[12] or ""),
                    "referencia": str(row[13] or ""),
                    "observacion": str(row[14] or ""),
                }
            )

    return {
        "empresa": _get_empresa_data(),
        "factura": header or {},
        "detalles": detalles,
    }
