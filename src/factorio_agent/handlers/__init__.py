"""Capability handlers, one module per domain (see ``capability_registry``).

Adding a domain module: create it next to these, register its handlers at
the bottom of the module, and list it in ``MODULES``.
"""

from importlib import import_module

MODULES = (
    "early",
    "research",
    "mining",
    "smelting_and_science",
    "bus_and_industry",
    "fluids",
    "late_fluids",
    "defense",
)


def load_all() -> None:
    for name in MODULES:
        import_module(__name__ + "." + name)
