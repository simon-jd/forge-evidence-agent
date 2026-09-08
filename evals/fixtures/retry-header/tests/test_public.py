import unittest
from retry_policy import *

class PublicSmoke(unittest.TestCase):
    def test_case_0(self):
        from datetime import datetime, timezone
        self.assertEqual(retry_after("3", datetime.now(timezone.utc)), 3)
