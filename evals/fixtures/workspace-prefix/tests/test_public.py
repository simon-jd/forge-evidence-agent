import unittest
from paths import *

class PublicSmoke(unittest.TestCase):
    def test_case_0(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(resolve_workspace_path(d, "a.py"), Path(d) / "a.py")
