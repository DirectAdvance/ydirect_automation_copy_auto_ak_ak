"""DI state for copy verification modules."""
from __future__ import annotations

_v5_call = None
_token_for_login = None
_direct_tokens = None


def configure(deps: dict) -> None:
    globals().update(deps)
