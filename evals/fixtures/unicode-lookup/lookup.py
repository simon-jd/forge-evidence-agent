def lookup_identifiers(names, query):
    needle = query.lower()
    return [name for name in names if needle in name.lower()]
