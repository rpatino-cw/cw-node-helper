"""Teleport (tsh) client — detects Teleport access for BMC shortcuts.

Fully optional — if `tsh` is not installed or user isn't logged in,
everything returns False. No errors, no prompts, no slowdowns.

Note: `tsh ls` is unusable on large clusters (hangs). This module only
uses `tsh status` (instant) to detect login state. BMC URLs are
constructed deterministically from NetBox hostnames — no listing needed.
"""
from __future__ import annotations

import json
import subprocess
import time
from typing import Optional

__all__ = ['_tsh_available', '_tsh_cluster_status', '_tsh_kube_context']

_tsh_available_cache: Optional[bool] = None
_cluster_status_cache: Optional[dict] = None
_cluster_status_ts: float = 0
_kube_context_cache: Optional[str] = None
_kube_context_ts: float = 0


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


def _tsh_cluster_status(cluster: str = "us-central-07a") -> Optional[str]:
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


def _tsh_kube_context() -> Optional[str]:
    """Return the active kube cluster type (e.g. 'mgmt') or None. Cached 5min."""
    global _kube_context_cache, _kube_context_ts
    if not _tsh_available():
        return None
    now = time.time()
    if _kube_context_cache is not None and (now - _kube_context_ts) < 300:
        return _kube_context_cache if _kube_context_cache != "" else None
    try:
        r = subprocess.run(
            ["tsh", "kube", "ls", "--format=json"],
            capture_output=True, timeout=15, text=True,
        )
        if r.returncode != 0:
            _kube_context_cache = ""
            _kube_context_ts = now
            return None
        clusters = json.loads(r.stdout)
        for c in clusters:
            if c.get("selected"):
                ctype = c.get("labels", {}).get("cluster.coreweave.cloud/type", "")
                _kube_context_cache = ctype or c.get("kube_cluster_name", "")
                _kube_context_ts = now
                return _kube_context_cache
        _kube_context_cache = ""
        _kube_context_ts = now
        return None
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
        _kube_context_cache = ""
        _kube_context_ts = now
        return None
