from datetime import datetime, timezone

def expired(value, now):
    moment = datetime.fromisoformat(value)
    moment = moment.replace(tzinfo=timezone.utc)
    return moment <= now
