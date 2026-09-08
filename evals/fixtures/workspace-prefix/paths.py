from pathlib import Path

def resolve_workspace_path(root, candidate):
    root = Path(root).resolve()
    resolved = (root / candidate).resolve()
    if not str(resolved).startswith(str(root)):
        raise ValueError("outside workspace")
    return resolved
