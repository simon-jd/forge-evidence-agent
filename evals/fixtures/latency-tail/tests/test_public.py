import unittest
from latency import *

class PublicSmoke(unittest.TestCase):
    def test_case_0(self):
        self.assertEqual(percentile([1,2,3],.5),2)
