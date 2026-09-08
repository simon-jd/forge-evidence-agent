import unittest
from assembler import *

class PublicSmoke(unittest.TestCase):
    def test_case_0(self):
        a = ToolAssembler()
        a.append("a", '{"x":1}')
        self.assertEqual(a.finish("a"), {"x": 1})
