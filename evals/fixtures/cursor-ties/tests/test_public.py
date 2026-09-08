import unittest
from pagination import *

class PublicSmoke(unittest.TestCase):
    def test_case_0(self):
        rows=[{"created_at":1,"id":"a"},{"created_at":2,"id":"b"}]
        self.assertEqual(next_page(rows, None, 1), rows[:1])
