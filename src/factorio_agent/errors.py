"""Exception types shared across entry points.

The supervisor runs as ``python -m factorio_agent.supervisor``, which loads
``supervisor.py`` under the name ``__main__``. A class defined there is a
different object from the one other modules get by importing
``factorio_agent.supervisor``, so an ``except CLIError`` in the supervisor did
not catch the ``CLIError`` raised by the in-process bridge. Shared exception
classes therefore live here, in a module that is only ever imported.
"""

from __future__ import annotations


class CLIError(RuntimeError):
    """A typed command failed or returned an error receipt."""
