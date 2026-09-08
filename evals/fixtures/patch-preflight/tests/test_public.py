import unittest
from document_patch import *

class PublicSmoke(unittest.TestCase):
    def test_case_0(self):
        docs={"a":"old"}
        self.assertEqual(apply_changes(docs, [("a","old","new")]), {"a":"new"})
