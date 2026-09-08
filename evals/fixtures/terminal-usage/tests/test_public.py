import unittest
from stream_reduce import *

class PublicSmoke(unittest.TestCase):
    def test_case_0(self):
        self.assertEqual(collect([{"text":"hello"},{"usage":{"input":1}}]),("hello",{"input":1}))
