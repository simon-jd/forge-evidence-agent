class NeedsReconciliation(RuntimeError):
    """An operation may have taken effect; replay requires external resolution."""


class StaleEpoch(RuntimeError):
    """A worker no longer owns the workspace execution epoch."""


class SandboxUnavailable(RuntimeError):
    """The explicitly required container boundary cannot be established."""


class UnsafePath(ValueError):
    """A path escapes the authorized workspace or traverses a symlink."""
