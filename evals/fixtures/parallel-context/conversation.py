def newest_transaction(messages):
    if not messages:
        return []
    if messages[-1]["role"] == "tool":
        return messages[-2:]
    return messages[-1:]
