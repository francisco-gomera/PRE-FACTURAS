import json
from django.test import TestCase, Client
from django.core import signing
from django.urls import reverse
from unittest.mock import patch
from inventario.models import SolicitudExistencia

class SolicitudExistenciaValidationTests(TestCase):
    def setUp(self):
        self.client = Client()
        # Mock auth cookie
        self.auth_payload = {
            "usuario_id": 1,
            "usuario_login": "admin",
            "usuario_name": "Administrador",
        }
        token = signing.dumps(self.auth_payload)
        self.client.cookies["prefacturas_auth_v2"] = token

    @patch("inventario.views.has_perm")
    def test_create_duplicate_invoice_request(self, mock_has_perm):
        mock_has_perm.return_value = True

        # First request creation should succeed for Factura
        payload1 = {
            "origen_modulo": "FACTURA",
            "origen_referencia": "Factura 101",
            "cliente_codigo": "C01",
            "cliente_nombre": "Cliente Test",
            "detalles": [
                {
                    "articulo_id": "ART01",
                    "descripcion": "Articulo Test",
                    "cantidad_solicitada": "5.00",
                    "cantidad_disponible": "0.00",
                    "cantidad_faltante": "5.00",
                }
            ]
        }
        response1 = self.client.post(
            reverse("inventario:solicitudes_existencia_crear"),
            data=json.dumps(payload1),
            content_type="application/json"
        )
        self.assertEqual(response1.status_code, 200)
        self.assertTrue(SolicitudExistencia.objects.filter(origen_referencia="Factura 101").exists())

        # Second request for the same invoice reference should be blocked
        response2 = self.client.post(
            reverse("inventario:solicitudes_existencia_crear"),
            data=json.dumps(payload1),
            content_type="application/json"
        )
        self.assertEqual(response2.status_code, 400)
        data2 = json.loads(response2.content.decode("utf-8"))
        self.assertEqual(data2.get("detail"), "Ya existe un pedido de existencia para esta factura.")

        # Test Prefactura reference duplicates
        payload_pref = {
            "origen_modulo": "FACTURA",
            "origen_referencia": "Prefactura 101",
            "cliente_codigo": "C01",
            "cliente_nombre": "Cliente Test",
            "detalles": [
                {
                    "articulo_id": "ART01",
                    "descripcion": "Articulo Test",
                    "cantidad_solicitada": "5.00",
                    "cantidad_disponible": "0.00",
                    "cantidad_faltante": "5.00",
                }
            ]
        }
        response_pref1 = self.client.post(
            reverse("inventario:solicitudes_existencia_crear"),
            data=json.dumps(payload_pref),
            content_type="application/json"
        )
        self.assertEqual(response_pref1.status_code, 200)

        response_pref2 = self.client.post(
            reverse("inventario:solicitudes_existencia_crear"),
            data=json.dumps(payload_pref),
            content_type="application/json"
        )
        self.assertEqual(response_pref2.status_code, 400)
        data_pref2 = json.loads(response_pref2.content.decode("utf-8"))
        self.assertEqual(data_pref2.get("detail"), "Ya existe un pedido de existencia para esta prefactura.")

    @patch("inventario.views.has_perm")
    def test_create_duplicate_generic_requests(self, mock_has_perm):
        mock_has_perm.return_value = True

        # Multiple generic requests (e.g., "Facturacion") should be allowed
        payload = {
            "origen_modulo": "FACTURA",
            "origen_referencia": "Facturacion",
            "cliente_codigo": "C01",
            "cliente_nombre": "Cliente Test",
            "detalles": [
                {
                    "articulo_id": "ART01",
                    "descripcion": "Articulo Test",
                    "cantidad_solicitada": "5.00",
                    "cantidad_disponible": "0.00",
                    "cantidad_faltante": "5.00",
                }
            ]
        }
        
        response1 = self.client.post(
            reverse("inventario:solicitudes_existencia_crear"),
            data=json.dumps(payload),
            content_type="application/json"
        )
        self.assertEqual(response1.status_code, 200)

        response2 = self.client.post(
            reverse("inventario:solicitudes_existencia_crear"),
            data=json.dumps(payload),
            content_type="application/json"
        )
        self.assertEqual(response2.status_code, 200)
        self.assertEqual(SolicitudExistencia.objects.filter(origen_referencia="Facturacion").count(), 2)

    @patch("inventario.views.has_perm")
    def test_create_different_invoices(self, mock_has_perm):
        mock_has_perm.return_value = True

        # Requests for different invoices should be allowed
        payload1 = {
            "origen_modulo": "FACTURA",
            "origen_referencia": "Factura 201",
            "cliente_codigo": "C01",
            "cliente_nombre": "Cliente Test",
            "detalles": [
                {
                    "articulo_id": "ART01",
                    "descripcion": "Articulo Test",
                    "cantidad_solicitada": "5.00",
                    "cantidad_disponible": "0.00",
                    "cantidad_faltante": "5.00",
                }
            ]
        }
        payload2 = {
            "origen_modulo": "FACTURA",
            "origen_referencia": "Factura 202",
            "cliente_codigo": "C01",
            "cliente_nombre": "Cliente Test",
            "detalles": [
                {
                    "articulo_id": "ART01",
                    "descripcion": "Articulo Test",
                    "cantidad_solicitada": "5.00",
                    "cantidad_disponible": "0.00",
                    "cantidad_faltante": "5.00",
                }
            ]
        }

        response1 = self.client.post(
            reverse("inventario:solicitudes_existencia_crear"),
            data=json.dumps(payload1),
            content_type="application/json"
        )
        self.assertEqual(response1.status_code, 200)

        response2 = self.client.post(
            reverse("inventario:solicitudes_existencia_crear"),
            data=json.dumps(payload2),
            content_type="application/json"
        )
        self.assertEqual(response2.status_code, 200)
        self.assertEqual(SolicitudExistencia.objects.filter(origen_modulo="FACTURA").count(), 2)
