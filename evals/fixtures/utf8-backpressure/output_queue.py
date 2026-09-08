from queue_size import payload_size

class OutputQueue:
    def __init__(self, max_bytes):
        self.limit = max_bytes
        self.used = 0
        self.items = []

    def put(self, text):
        size = payload_size(text)
        if self.used + size > self.limit:
            return False
        self.items.append(text)
        self.used += size
        return True

    def take(self):
        text = self.items.pop(0)
        self.used -= len(text)
        return text
