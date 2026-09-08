import unittest
from event_log import *

class PublicSmoke(unittest.TestCase):
    def test_case_0(self):
        log=EventLog(["a"])
        log.append("first")
        self.assertEqual(log.pending("a"), [(1,"first")])
