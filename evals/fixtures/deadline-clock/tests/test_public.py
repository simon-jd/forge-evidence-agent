import unittest
from deadline import *

class PublicSmoke(unittest.TestCase):
    def test_case_0(self):
        d=Deadline(0, clock=lambda:0)
        self.assertEqual(d.remaining(),0)
