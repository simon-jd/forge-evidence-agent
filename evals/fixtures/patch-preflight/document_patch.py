def apply_changes(documents, changes):
    for path, before, after in changes:
        if documents.get(path) != before:
            raise ValueError("stale precondition: " + path)
        documents[path] = after
    return documents
