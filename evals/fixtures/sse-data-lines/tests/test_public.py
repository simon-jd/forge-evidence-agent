import unittest
from sse import *

class PublicSmoke(unittest.TestCase):
    def test_case_0(self):
        self.assertEqual(event_data(["data: hello"]), "hello")
