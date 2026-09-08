import unittest
from failures import *

class PublicSmoke(unittest.TestCase):
    def test_case_0(self):
        self.assertTrue(should_retry(503,1,3))
        self.assertFalse(should_retry(503,3,3))
