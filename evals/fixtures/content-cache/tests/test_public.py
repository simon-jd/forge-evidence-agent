import unittest
from file_cache import *

class PublicSmoke(unittest.TestCase):
    def test_case_0(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/"a"; p.write_text("one")
            self.assertEqual(ParseCache(str.upper).get(p), "ONE")
