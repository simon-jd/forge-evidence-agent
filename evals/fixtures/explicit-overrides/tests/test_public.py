import unittest
from profiles import *

class PublicSmoke(unittest.TestCase):
    def test_case_0(self):
        self.assertEqual(merge({"enabled":True},{"enabled":True}),{"enabled":True})
        self.assertEqual(configured_retries({}),3)
