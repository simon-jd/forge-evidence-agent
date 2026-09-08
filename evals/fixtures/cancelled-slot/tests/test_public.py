import unittest
from async_pool import *

class PublicSmoke(unittest.TestCase):
    def test_case_0(self):
        import asyncio
        async def demo():
            async def work(): return 7
            return await SlotPool(1).run(work)
        self.assertEqual(asyncio.run(demo()),7)
