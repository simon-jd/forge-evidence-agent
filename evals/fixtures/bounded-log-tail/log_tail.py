from collections import deque

def error_tail(lines, limit):
    tail = deque(lines, maxlen=limit)
    return [line.rstrip("\n") for line in tail if " ERROR " in line]
