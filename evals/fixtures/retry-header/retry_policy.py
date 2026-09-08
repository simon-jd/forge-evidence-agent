from email.utils import parsedate_to_datetime

def retry_after(value, now, default=1.0):
    if value is None:
        return default
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            delay = (parsedate_to_datetime(value) - now).total_seconds()
            return max(0.0, delay * 1000)
        except (TypeError, ValueError, OverflowError):
            return default
