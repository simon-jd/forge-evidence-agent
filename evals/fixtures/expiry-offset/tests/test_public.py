import unittest
from expiry import *

class PublicSmoke(unittest.TestCase):
    def test_case_0(self):
        from datetime import datetime, timezone
        self.assertTrue(expired("2026-09-07T00:00:00+00:00",datetime(2026,9,7,1,tzinfo=timezone.utc)))
