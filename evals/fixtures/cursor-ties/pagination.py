def next_page(rows, cursor, limit):
    ordered = sorted(rows, key=lambda row: (row["created_at"], row["id"]))
    if cursor is not None:
        ordered = [row for row in ordered if row["created_at"] > cursor[0]]
    return ordered[:limit]
