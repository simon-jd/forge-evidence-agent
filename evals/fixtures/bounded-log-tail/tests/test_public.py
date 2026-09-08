import unittest
from log_tail import *

class PublicSmoke(unittest.TestCase):
    def test_case_0(self):
        self.assertEqual(error_tail(["t ERROR x\n"],1),["t ERROR x"])
