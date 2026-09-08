import unittest
from aliases import *

class PublicSmoke(unittest.TestCase):
    def test_case_0(self):
        self.assertEqual(resolve_alias("final",{}),"final")
        self.assertEqual(resolve_alias("start",{"start":"final"}),"final")
