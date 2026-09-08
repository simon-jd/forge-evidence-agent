import unittest
from output_queue import *

class PublicSmoke(unittest.TestCase):
    def test_case_0(self):
        q=OutputQueue(3)
        self.assertTrue(q.put("abc"))
        self.assertFalse(q.put("d"))
        self.assertEqual(q.take(),"abc")
        self.assertEqual(q.used,0)
