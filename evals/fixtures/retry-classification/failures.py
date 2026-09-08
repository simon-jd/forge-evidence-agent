def should_retry(status, attempt, max_attempts):
    if attempt >= max_attempts:
        return False
    return status >= 400
