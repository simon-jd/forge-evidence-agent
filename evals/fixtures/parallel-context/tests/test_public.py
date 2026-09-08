import unittest
from conversation import *

class PublicSmoke(unittest.TestCase):
    def test_case_0(self):
        rows=[{"role":"assistant","calls":["a"]},{"role":"tool","call_id":"a"}]
        self.assertEqual(newest_transaction(rows),rows)
