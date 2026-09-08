class LeaseStore:
    def __init__(self):
        self.epoch = 0
        self.value = None

    def claim(self):
        self.epoch += 1
        return self.epoch

    def write(self, epoch, value):
        if epoch > self.epoch:
            raise ValueError("not the lease owner")
        self.value = value
