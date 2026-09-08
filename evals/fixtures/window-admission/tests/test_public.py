import unittest
from limiter import *

class PublicSmoke(unittest.TestCase):
    def test_case_0(self):
        q = SlidingQuota(2, 10)
        self.assertTrue(q.allow(1))
        self.assertTrue(q.allow(2))
        self.assertFalse(q.allow(3))
