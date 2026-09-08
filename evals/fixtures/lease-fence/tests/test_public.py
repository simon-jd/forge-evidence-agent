import unittest
from lease_store import *

class PublicSmoke(unittest.TestCase):
    def test_case_0(self):
        s=LeaseStore()
        e=s.claim(); s.write(e,"ok")
        self.assertEqual(s.value,"ok")
