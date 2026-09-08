import unittest
from symbols import *

class PublicSmoke(unittest.TestCase):
    def test_case_0(self):
        self.assertEqual(symbol_names("def top():\n    pass\n"),["top"])
