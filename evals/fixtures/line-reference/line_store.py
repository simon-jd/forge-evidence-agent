def read_lines(text, start, end):
    lines = text.splitlines(keepends=True)
    return "".join(lines[start:end + 1])
