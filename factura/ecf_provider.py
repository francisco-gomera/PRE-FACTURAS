from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from urllib import error, request

from .ecf_runtime import (
    get_ecf_callback_api_key,
    get_ecf_external_api_key,
    get_ecf_external_submit_url,
    get_ecf_provider_mode,
)
from .ecf_snapshot import build_document_snapshot


@dataclass
class EcfSubmissionResult:
    attempted: bool
    ok: bool
    provider: str
    message: str
    track_id: str = ""
    raw_response: str = ""


def should_auto_dispatch(config_mode):
    return str(config_mode or "").strip().lower() == "automatico"


def _to_float(value):
    try:
        return float(Decimal(value or 0))
    except Exception:
        return 0.0


def _build_payload(documento, callback_urls):
    callback_api_key = get_ecf_callback_api_key()
    return {
        "id_doc": documento.id_doc,
        "tipo_ecf": documento.tipo_ecf,
        "encf": documento.encf,
        "cliente_rnc": documento.cliente_rnc,
        "cliente_nombre": documento.cliente_nombre,
        "monto_total": _to_float(documento.monto_total),
        "callback_recepcion_url": callback_urls.get("recepcion"),
        "callback_aprobacion_url": callback_urls.get("aprobacion"),
        "callback_auth_mode": "x-ecf-api-key" if callback_api_key else "none",
        "callback_api_key": callback_api_key or "",
        "snapshot": build_document_snapshot(documento.id_doc),
    }


def submit_document(documento, callback_urls, dispatch_enabled):
    provider_mode = get_ecf_provider_mode()
    if not dispatch_enabled:
        return EcfSubmissionResult(False, False, provider_mode, "Modo de envio no automatico.")
    if provider_mode == "manual":
        return EcfSubmissionResult(False, False, provider_mode, "Modo manual: no se envio al integrador.")
    if provider_mode == "dgii_direct":
        return EcfSubmissionResult(False, False, provider_mode, "Integracion DGII directa aun no implementada en este proyecto.")

    submit_url = get_ecf_external_submit_url()
    if not submit_url:
        return EcfSubmissionResult(False, False, provider_mode, "Falta ECF_EXTERNAL_SUBMIT_URL para despacho automatico.")

    payload = _build_payload(documento, callback_urls)
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    external_api_key = get_ecf_external_api_key()
    if external_api_key:
        headers["X-ECF-API-Key"] = external_api_key

    req = request.Request(submit_url, data=body, headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=20) as response:
            raw = response.read().decode("utf-8", errors="ignore")
            try:
                data = json.loads(raw or "{}")
            except Exception:
                data = {}
            track_id = str(data.get("track_id") or data.get("id") or "").strip()
            message = str(data.get("detail") or data.get("message") or "Documento remitido al integrador.").strip()
            return EcfSubmissionResult(True, True, provider_mode, message, track_id=track_id, raw_response=raw[:4000])
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="ignore")
        return EcfSubmissionResult(True, False, provider_mode, f"HTTP {exc.code} al contactar el integrador.", raw_response=raw[:4000])
    except Exception as exc:
        return EcfSubmissionResult(True, False, provider_mode, f"{type(exc).__name__}: {exc}")
