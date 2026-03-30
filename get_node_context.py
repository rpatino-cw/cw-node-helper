#!/usr/bin/env python3
"""
get_node_context.py — Backward-compatibility shim.

All code has been moved to the cwhelper/ package.
This file re-exports everything so existing imports
(import get_node_context as gnc; gnc._function_name())
continue to work.
"""

from __future__ import annotations

# Stdlib imports needed by some callers that do gnc.os / gnc.sys etc.
import os  # noqa: F401

# ---------------------------------------------------------------------------
# Auto-load .env from the project root so the app runs without `source load_env.sh`
# ---------------------------------------------------------------------------
def _load_dotenv():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _key, _val = _line.split("=", 1)
            _key = _key.strip()
            _val = _val.strip().strip('"').strip("'")
            if _key and _key not in os.environ:  # don't overwrite already-set vars
                os.environ[_key] = _val

_load_dotenv()
import sys  # noqa: F401
import re  # noqa: F401
import json  # noqa: F401
import threading  # noqa: F401
import queue as queue_mod  # noqa: F401

# Mock-safe requests import (tests mock this before importing us)
try:
    import requests  # noqa: F401
except ImportError:
    pass

try:
    import openai as _openai_mod  # noqa: F401
    _HAS_OPENAI = True
except ImportError:
    _openai_mod = None
    _HAS_OPENAI = False

# ---------------------------------------------------------------------------
# Re-export everything from the cwhelper package
# ---------------------------------------------------------------------------
from cwhelper.config import *  # noqa: F401,F403
from cwhelper.cache import *  # noqa: F401,F403
from cwhelper.state import *  # noqa: F401,F403
from cwhelper.clients.jira import *  # noqa: F401,F403
from cwhelper.clients.netbox import *  # noqa: F401,F403
from cwhelper.clients.grafana import *  # noqa: F401,F403
from cwhelper.services.context import *  # noqa: F401,F403
from cwhelper.services.search import *  # noqa: F401,F403
from cwhelper.services.notifications import *  # noqa: F401,F403
from cwhelper.services.ai import *  # noqa: F401,F403
from cwhelper.services.queue import *  # noqa: F401,F403
from cwhelper.services.watcher import *  # noqa: F401,F403
from cwhelper.services.weekend import *  # noqa: F401,F403
from cwhelper.services.walkthrough import *  # noqa: F401,F403
from cwhelper.services.bookmarks import *  # noqa: F401,F403
from cwhelper.services.rack import *  # noqa: F401,F403
from cwhelper.tui.display import *  # noqa: F401,F403
from cwhelper.tui.connection_view import *  # noqa: F401,F403
from cwhelper.tui.actions import *  # noqa: F401,F403
from cwhelper.tui.menu import *  # noqa: F401,F403
from cwhelper.cli import *  # noqa: F401,F403

# ---------------------------------------------------------------------------
# Mutable globals — canonical copies live in cwhelper.config
# Re-exported here so test code that does gnc._my_display_name = X works
# (but for new code, use cwhelper.config directly)
# ---------------------------------------------------------------------------
import cwhelper.config as _cfg  # noqa: E402
_session = _cfg._session
_executor = _cfg._executor
_issue_cache = _cfg._issue_cache
_netbox_cache = _cfg._netbox_cache
_jql_cache = _cfg._jql_cache
_ANIMATE = _cfg._ANIMATE
_AI_ENABLED = _cfg._AI_ENABLED
NTFY_TOPIC = _cfg.NTFY_TOPIC
_NTFY_ENABLED = _cfg._NTFY_ENABLED
_my_account_id = _cfg._my_account_id
_my_display_name = _cfg._my_display_name
_ntfy_alerted = _cfg._ntfy_alerted

if __name__ == "__main__":
    main()
