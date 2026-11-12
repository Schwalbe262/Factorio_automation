"""One handler per capability: what ``StarterExecutors.step``/``proof``/``record_action`` call.

A handler module under ``factorio_agent.handlers`` defines plain functions
taking the ``StarterExecutors`` object as ``ex`` and registers them here with
``register``. Handlers import their controller modules lazily, so importing
this registry (``Request.validate`` does) stays cheap.

Signatures:

- ``step(ex, obs, parameters) -> dict``: one action dict or one status dict.
- ``proof(ex, obs, result, parameters) -> dict | None``; without one the
  generic flow proof (``handlers.upkeep.flow_proof``) applies.
- ``record_action(ex, obs, action, receipt, parameters)``: optional receipt hook.
- ``validate(parameters)``: optional structural check that raises
  ``ValueError``; without one the capability must take ``{}``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class Handler:
    name: str
    step: Callable[..., dict]
    proof: Callable[..., dict | None] | None = None
    record_action: Callable[..., None] | None = None
    validate: Callable[[Any], None] | None = None


_HANDLERS: dict[str, Handler] = {}
_LOADED = False


def register(name: str, *, step, proof=None, record_action=None, validate=None) -> Handler:
    """Register the handler of capability ``name``; a second registration is a bug."""
    if name in _HANDLERS:
        raise ValueError("capability handler registered twice: " + name)
    handler = Handler(name, step, proof=proof, record_action=record_action, validate=validate)
    _HANDLERS[name] = handler
    return handler


def _load() -> None:
    global _LOADED
    if not _LOADED:
        from .handlers import load_all

        load_all()
        _LOADED = True


def get_handler(name: str) -> Handler | None:
    """The handler of ``name``, or None for an unknown capability."""
    _load()
    return _HANDLERS.get(name)


def handler_for(name: str) -> Handler:
    """The handler of ``name``; an unknown capability raises the historical dispatch error."""
    handler = get_handler(name)
    if handler is None:
        raise ValueError("no executor for capability: " + name)
    return handler


def registered_names() -> frozenset[str]:
    _load()
    return frozenset(_HANDLERS)
