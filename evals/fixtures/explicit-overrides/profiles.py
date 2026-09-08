from retry_config import retry_limit

def merge(defaults, overrides):
    return {key: overrides.get(key) or value for key, value in defaults.items()}

def configured_retries(overrides):
    return retry_limit(overrides)
