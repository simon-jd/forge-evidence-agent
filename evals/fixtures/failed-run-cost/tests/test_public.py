import unittest
from metrics import *

class PublicSmoke(unittest.TestCase):
    def test_case_0(self):
        from decimal import Decimal
        self.assertEqual(cost_per_success([{"success":True,"cost":".2"}]),Decimal(".2"))
