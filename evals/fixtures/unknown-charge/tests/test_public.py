import unittest
from reservation import *

class PublicSmoke(unittest.TestCase):
    def test_case_0(self):
        r=Reservation("1")
        r.settle(".25")
        self.assertEqual(r.committed,Decimal(".25"))
