import unittest
from billing import *

class PublicSmoke(unittest.TestCase):
    def test_case_0(self):
        from decimal import Decimal as D
        self.assertEqual(cost(1000, 0, 100, D("2"), D(".2"), D("4")), D(".0024"))
