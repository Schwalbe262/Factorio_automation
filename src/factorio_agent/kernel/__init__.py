"""Long-lived execution kernel: one process, one RCON socket, cached world facts."""

from .session import InProcessBridge, Session

__all__ = ["InProcessBridge", "Session"]
