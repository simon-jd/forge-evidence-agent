import asyncio

class SlotPool:
    def __init__(self, capacity):
        self.semaphore = asyncio.Semaphore(capacity)

    async def run(self, operation):
        await self.semaphore.acquire()
        result = await operation()
        self.semaphore.release()
        return result
