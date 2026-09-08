import unittest
from evidence_view import *

class PublicSmoke(unittest.TestCase):
    def test_case_0(self):
        self.assertIsInstance(reference("one\ntwo\n",1,1),dict)
