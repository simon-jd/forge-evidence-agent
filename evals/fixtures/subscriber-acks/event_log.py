class EventLog:
    def __init__(self, subscribers):
        self.events = []
        self.acked = {name: 0 for name in subscribers}

    def append(self, payload):
        seq = len(self.events) + 1
        self.events.append((seq, payload))
        return seq

    def ack(self, subscriber, seq):
        self.acked[subscriber] = max(self.acked[subscriber], seq)

    def pending(self, subscriber):
        cutoff = max(self.acked.values())
        return [event for event in self.events if event[0] > cutoff]
