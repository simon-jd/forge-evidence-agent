import unittest
from decoder import *

class PublicSmoke(unittest.TestCase):
    def test_case_0(self):
        self.assertEqual(TextDecoder().feed(b"hello"), "hello")
