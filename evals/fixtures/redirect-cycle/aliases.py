def resolve_alias(name, aliases):
    visited = set()
    while name in aliases:
        if name in visited:
            raise ValueError("alias cycle")
        visited.add(aliases[name])
        name = aliases[name]
    return name
