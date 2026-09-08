from decimal import Decimal

class Reservation:
    def __init__(self, amount):
        self.committed = Decimal(amount)
        self.status = "reserved"

    def settle(self, actual):
        self.committed = Decimal(actual or "0")
        self.status = "settled"
