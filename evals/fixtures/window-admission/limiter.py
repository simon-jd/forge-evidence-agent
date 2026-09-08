from collections import deque

class SlidingQuota:
    def __init__(self, limit, window):
        self.limit, self.window = limit, window
        self.accepted = deque()

    def allow(self, now):
        while self.accepted and self.accepted[0] < now - self.window:
            self.accepted.popleft()
        if len(self.accepted) >= self.limit:
            return False
        self.accepted.append(now)
        return True
