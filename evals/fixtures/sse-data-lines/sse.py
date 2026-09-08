def event_data(lines):
    chunks = []
    for line in lines:
        if line.startswith(":") or ":" not in line:
            continue
        field, value = line.split(":", 1)
        if field == "data":
            chunks.append(value.strip())
    return "".join(chunks)
