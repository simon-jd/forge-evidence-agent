def collect(events):
    text = ""
    usage = None
    for event in events:
        if event.get("finished"):
            break
        text += event.get("text", "")
        if "usage" in event:
            usage = event["usage"]
    return text, usage
