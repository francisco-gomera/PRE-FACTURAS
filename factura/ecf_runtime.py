from __future__ import annotations

import importlib.util
import os
from pathlib import Path

from django.db import connection

SUPPORTED_PROVIDER_MODES = {"manual", "external", "dgii_direct"}


def _env(name, default=""):
    return str(os.getenv(name, default) or "").strip()


def get_ecf_provider_mode():
    mode = _env("ECF_PROVIDER_MODE", "external").lower()
    if mode not in SUPPORTED_PROVIDER_MODES:
        return "manual"
    return mode


def get_ecf_callback_api_key():
    return _env("ECF_CALLBACK_API_KEY")


def get_ecf_external_submit_url():
    return _env("ECF_EXTERNAL_SUBMIT_URL")


def get_ecf_external_api_key():
    return _env("ECF_EXTERNAL_API_KEY")


def get_ecf_certificate_path():
    return _env("ECF_CERT_FILE")


def _module_available(module_name):
    return importlib.util.find_spec(module_name) is not None


def _probe_database():
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        return True, "Conexion SQL disponible."
    except Exception as exc:
        detail = str(exc).splitlines()[0].strip()
        return False, f"{type(exc).__name__}: {detail}"


def build_ecf_runtime_report():
    provider_mode = get_ecf_provider_mode()
    certificate_path = get_ecf_certificate_path()
    callback_api_key = get_ecf_callback_api_key()
    external_submit_url = get_ecf_external_submit_url()
    dgii_auth_url = _env("ECF_DGII_AUTH_URL")
    dgii_submit_url = _env("ECF_DGII_SUBMIT_URL")
    cert_exists = bool(certificate_path and Path(certificate_path).exists())
    db_ok, db_detail = _probe_database()
    direct_signing_libs = _module_available("lxml") and _module_available("signxml")

    checks = [
        {
            "label": "Base SQL accesible",
            "ok": db_ok,
            "level": "required",
            "detail": db_detail,
        },
        {
            "label": "Certificado digital disponible en disco",
            "ok": cert_exists,
            "level": "required",
            "detail": certificate_path or "Define ECF_CERT_FILE o carga la ruta desde la UI.",
        },
        {
            "label": "Callbacks protegidos con API key",
            "ok": bool(callback_api_key),
            "level": "required",
            "detail": "Configura ECF_CALLBACK_API_KEY para proteger /ecf/recepcion y /ecf/aprobacion.",
        },
        {
            "label": "Modo de integracion e-CF definido",
            "ok": provider_mode in SUPPORTED_PROVIDER_MODES,
            "level": "required",
            "detail": provider_mode,
        },
    ]

    if provider_mode == "external":
        checks.append(
            {
                "label": "Integrador externo configurado",
                "ok": bool(external_submit_url),
                "level": "required",
                "detail": external_submit_url or "Falta ECF_EXTERNAL_SUBMIT_URL.",
            }
        )
    elif provider_mode == "dgii_direct":
        checks.extend(
            [
                {
                    "label": "Endpoints DGII configurados",
                    "ok": bool(dgii_auth_url and dgii_submit_url),
                    "level": "required",
                    "detail": "Define ECF_DGII_AUTH_URL y ECF_DGII_SUBMIT_URL.",
                },
                {
                    "label": "Librerias de firma XML disponibles",
                    "ok": direct_signing_libs,
                    "level": "required",
                    "detail": "Se espera lxml + signxml para firma directa.",
                },
            ]
        )
    else:
        checks.append(
            {
                "label": "Salida automatica habilitada",
                "ok": False,
                "level": "required",
                "detail": "El modo manual sirve para preparacion interna, no para precertificacion.",
            }
        )

    ready_for_precertificacion = all(item["ok"] for item in checks if item["level"] == "required")
    return {
        "provider_mode": provider_mode,
        "certificate_path": certificate_path,
        "external_submit_url": external_submit_url,
        "checks": checks,
        "ready_for_precertificacion": ready_for_precertificacion,
    }
