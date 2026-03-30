"""cwctl fleet client — wraps cwctl CLI for rack/node fleet data.

Fully optional — if `cwctl` is not installed or kubeconfig is not
configured, everything returns None. No errors, no prompts, no slowdowns.
"""
from __future__ import annotations

import json
import subprocess
import time
from typing import Optional

__all__ = [
    '_cwctl_available',
    '_cwctl_seed_blame',
    '_cwctl_describe_rack',
    '_cwctl_rack_blockers',
    '_cwctl_install_hint',
]

# ── availability cache (checked once per session) ──────────────────────
_cwctl_available_cache: Optional[bool] = None

# ── response cache with TTL ────────────────────────────────────────────
_fleet_cache: dict[str, tuple[float, object]] = {}
_BLAME_TTL = 60   # seconds
_RACK_TTL = 30    # rack state changes faster


def _cache_get(key: str, ttl: float) -> object | None:
    entry = _fleet_cache.get(key)
    if entry and (time.time() - entry[0]) < ttl:
        return entry[1]
    return None


def _cache_put(key: str, data: object) -> None:
    # evict oldest if over 50 entries
    if len(_fleet_cache) >= 50:
        oldest = min(_fleet_cache, key=lambda k: _fleet_cache[k][0])
        del _fleet_cache[oldest]
    _fleet_cache[key] = (time.time(), data)


# ── public functions ───────────────────────────────────────────────────

def _cwctl_available() -> bool:
    """True if `cwctl` is on PATH. Checked once per session."""
    global _cwctl_available_cache
    if _cwctl_available_cache is not None:
        return _cwctl_available_cache
    try:
        r = subprocess.run(
            ["cwctl", "version"],
            capture_output=True, timeout=5, text=True,
        )
        _cwctl_available_cache = r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        _cwctl_available_cache = False
    return _cwctl_available_cache


def _cwctl_seed_blame(bmn_name: str) -> Optional[dict]:
    """Run `cwctl seed blame bmn <name> --json` and return parsed dict, or None."""
    if not _cwctl_available():
        return None

    cached = _cache_get(f"blame:{bmn_name}", _BLAME_TTL)
    if cached is not None:
        return cached

    try:
        r = subprocess.run(
            ["cwctl", "seed", "blame", "bmn", bmn_name, "--json"],
            capture_output=True, timeout=10, text=True,
        )
        if r.returncode != 0:
            return None
        data = json.loads(r.stdout)
        # seed blame bmn returns an array; extract first element for single query
        result = data[0] if isinstance(data, list) and data else data
        _cache_put(f"blame:{bmn_name}", result)
        return result
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError, json.JSONDecodeError, IndexError, KeyError):
        return None


def _cwctl_describe_rack(rack_name: str, sections: Optional[list[str]] = None) -> Optional[dict]:
    """Run `cwctl describe rack <name> -o json` and return parsed dict, or None."""
    if not _cwctl_available():
        return None

    cache_key = f"rack:{rack_name}:{','.join(sections or [])}"
    cached = _cache_get(cache_key, _RACK_TTL)
    if cached is not None:
        return cached

    cmd = ["cwctl", "describe", "rack", rack_name, "-o", "json"]
    if sections:
        cmd.extend(["--sections", ",".join(sections)])

    try:
        r = subprocess.run(
            cmd,
            capture_output=True, timeout=15, text=True,
        )
        if r.returncode != 0:
            return None
        data = json.loads(r.stdout)
        # response is {rack_name: {...}} — extract inner dict
        result = data.get(rack_name)
        if result is not None:
            _cache_put(cache_key, result)
        return result
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError, json.JSONDecodeError, AttributeError):
        return None


def _cwctl_rack_blockers(rack_name: str) -> Optional[list]:
    """Return checkpoint blockers for a rack, or None.

    Each blocker dict has: severity, reason, source, message,
    first-observed, last-observed.
    """
    detail = _cwctl_describe_rack(rack_name, sections=["checkpoint-blockers"])
    if not detail:
        return None
    try:
        blockers = (
            detail.get("rack", {})
            .get("status", {})
            .get("checkpoint", {})
            .get("blockers", [])
        )
        return blockers if blockers else None
    except (AttributeError, TypeError):
        return None


def _cwctl_install_hint() -> str:
    """Return the one-liner to install cwctl."""
    return (
        'gh release download -R <org>/cwctl '
        '-p "cwctl_$(uname)_$(uname -m).tar.gz" '
        '-O - --clobber | tar zxf - cwctl && '
        'mv cwctl $HOME/.local/bin/'
    )
