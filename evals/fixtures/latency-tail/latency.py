import math

def percentile(values, p):
    if not values or not 0 < p <= 1:
        raise ValueError("nonempty values and 0 < p <= 1 required")
    ordered = sorted(values)
    index = int(p * len(ordered))
    return ordered[index]
