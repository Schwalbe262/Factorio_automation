"""Recipe-driven production blocks: size a row, lay it out, audit it (pure Python).

``size_row`` (sizing) -> ``row_block`` / ``extend_row`` (row) -> ``audit_row`` (audit).
Undocumented game constants are explicit, overridable ``params.RowLimits``.
Names are imported lazily so ``python -m factorio_agent.blocks.row`` runs cleanly.
"""

from importlib import import_module

_EXPORTS = {
    "audit_row": "audit",
    "DEFAULT_LIMITS": "params",
    "RowLimits": "params",
    "extend_row": "row",
    "row_block": "row",
    "BeltLanes": "sizing",
    "Flow": "sizing",
    "RowSizing": "sizing",
    "size_row": "sizing",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(name)
    return getattr(import_module(f"{__name__}.{_EXPORTS[name]}"), name)
