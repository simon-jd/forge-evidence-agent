from decimal import Decimal

def cost_per_success(trials):
    successes = [row for row in trials if row["success"]]
    if not successes:
        return None
    total = sum((Decimal(row["cost"]) for row in successes), Decimal(0))
    return total / len(successes)
