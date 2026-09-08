import unittest
from ranges import *

class PublicSmoke(unittest.TestCase):
    def test_case_0(self):
        self.assertEqual(merge_ranges([(1,3),(3,5)]),[(1,5)])
