import unittest
from request_cache import *

class PublicSmoke(unittest.TestCase):
    def test_case_0(self):
        c=RequestCache()
        self.assertEqual(c.submit("x", {"a":1}, lambda x: 7), 7)
        self.assertEqual(c.submit("x", {"a":1}, lambda x: 8), 7)
