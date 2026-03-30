"""Teleport (tsh) client — detects Teleport access for BMC shortcuts.

Fully optional — if `tsh` is not installed or user isn't logged in,
everything returns False. No errors, no prompts, no slowdowns.

Note: `tsh ls` is unusable on large clusters (hangs). This module only
uses `tsh status` (instant) to detect login state. BMC URLs are
constructed deterministically from NetBox hostnames — no listing needed.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from typing import Optional

__all__ = ['_tsh_available', '_tsh_cluster_status', '_tsh_ensure_login', '_tsh_on_path']

_tsh_available_cache: Optional[bool] = None
_cluster_status_cache: Optional[dict] = None
_cluster_status_ts: float = 0


def _tsh_available() -> bool:
    """True if `tsh` is on PATH and logged in. Checked once per session."""
    global _tsh_available_cache
    if _tsh_available_cache is not None:
        return _tsh_available_cache
    try:
        r = subprocess.run(
            ["tsh", "status"],
            capture_output=True, timeout=5, text=True,
        )
        _tsh_available_cache = r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        _tsh_available_cache = False
    return _tsh_available_cache


def _tsh_on_path() -> bool:
    """True if `tsh` binary exists on PATH (regardless of login state)."""
    try:
        r = subprocess.run(
            ["tsh", "version"],
            capture_output=True, timeout=5, text=True,
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _tsh_ensure_login(proxy: str = os.environ.get("TELEPORT_PROXY", "teleport.na.int.example.com"),
                      interactive: bool = True) -> bool:
    """Check tsh login state. If expired/missing and interactive, prompt to login.

    Returns True if logged in (or successfully re-logged), False otherwise.
    """
    global _tsh_available_cache

    if _tsh_available():
        return True

    if not _tsh_on_path():
        return False

    # tsh is on PATH but not logged in — session expired or never logged in
    if not interactive:
        return False

    # Check if the session is expired vs never logged in
    try:
        r = subprocess.run(
            ["tsh", "status"],
            capture_output=True, timeout=5, text=True,
        )
        stderr = r.stderr.lower()
        expired = "expired" in stderr or "not logged in" in stderr or r.returncode != 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        expired = True

    if not expired:
        return True

    # Prompt user to login
    print()
    print(f"  \033[33m\033[1mTeleport session expired or not logged in.\033[0m")
    print(f"  \033[2mProxy: {proxy}\033[0m")
    print()
    try:
        choice = input("  Login now? [Y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False

    if choice in ("", "y", "yes"):
        print(f"\n  \033[2mRunning: tsh login --proxy={proxy} teleport\033[0m\n")
        try:
            # Run interactively — tsh login needs browser/SSO
            result = subprocess.run(
                ["tsh", "login", f"--proxy={proxy}", "teleport"],
                timeout=120,
            )
            if result.returncode == 0:
                # Clear cache so _tsh_available() re-checks
                _tsh_available_cache = None
                if _tsh_available():
                    print(f"\n  \033[32m\033[1mLogged in successfully.\033[0m\n")
                    return True
            print(f"\n  \033[31mLogin failed (exit code {result.returncode}).\033[0m")
        except subprocess.TimeoutExpired:
            print(f"\n  \033[31mLogin timed out (2 min).\033[0m")
        except (FileNotFoundError, OSError) as e:
            print(f"\n  \033[31mCould not run tsh: {e}\033[0m")

    return False


def _tsh_cluster_status(cluster: str = "us-site-01a") -> Optional[str]:
    """Return 'online'/'offline'/None for a Teleport leaf cluster. Cached 5min."""
    global _cluster_status_cache, _cluster_status_ts
    if not _tsh_available():
        return None
    now = time.time()
    if _cluster_status_cache is not None and (now - _cluster_status_ts) < 300:
        return _cluster_status_cache.get(cluster)
    try:
        r = subprocess.run(
            ["tsh", "clusters", "--format=json"],
            capture_output=True, timeout=10, text=True,
        )
        if r.returncode != 0:
            return None
        clusters = json.loads(r.stdout)
        lookup: dict[str, str] = {}
        for c in clusters:
            name = c.get("cluster_name", "")
            status = c.get("status", "offline")
            parts = name.split(".")
            if len(parts) >= 2:
                lookup[parts[1]] = status
        _cluster_status_cache = lookup
        _cluster_status_ts = now
        return lookup.get(cluster)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
        return None
