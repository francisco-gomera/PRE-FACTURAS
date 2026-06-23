from django.test import TestCase
from decimal import Decimal
from ajustes.views import _tu_money, _tu_estatus_cuenta

class TransUnionTests(TestCase):
    def test_tu_money_formatting(self):
        # Test basic rounding
        self.assertEqual(_tu_money("1250.50"), "1251")
        self.assertEqual(_tu_money("1250.49"), "1250")
        
        # Test thousands separator removal
        self.assertEqual(_tu_money("1,250.50"), "1251")
        self.assertEqual(_tu_money("1,250,000.00"), "1250000")
        
        # Test default/invalid inputs
        self.assertEqual(_tu_money(""), "0")
        self.assertEqual(_tu_money(None), "0")
        self.assertEqual(_tu_money("invalid"), "0")
        
        # Test numeric inputs
        self.assertEqual(_tu_money(1250.5), "1251")
        self.assertEqual(_tu_money(Decimal("123.45")), "123")

    def test_tu_estatus_cuenta(self):
        # Overdue cases (should be CASTIGADA)
        self.assertEqual(_tu_estatus_cuenta(1, 0), "CASTIGADA")
        self.assertEqual(_tu_estatus_cuenta(0, 1), "CASTIGADA")
        self.assertEqual(_tu_estatus_cuenta(5, 3), "CASTIGADA")
        
        # Normal cases
        self.assertEqual(_tu_estatus_cuenta(0, 0), "NORMAL")
        self.assertEqual(_tu_estatus_cuenta(0, None), "NORMAL")

