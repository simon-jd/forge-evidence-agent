import unittest
from lookup import *

class PublicSmoke(unittest.TestCase):
    def test_case_0(self):
        self.assertEqual(lookup_identifiers(["Client","Server"],"client"),["Client"])
