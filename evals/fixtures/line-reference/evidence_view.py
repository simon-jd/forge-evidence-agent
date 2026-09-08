from line_store import read_lines

def reference(text, start_line, end_line):
    return {"start_line": start_line + 1, "end_line": end_line + 1,
            "text": read_lines(text, start_line, end_line)}
