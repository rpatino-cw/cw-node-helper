"""Cache utilities, HTTP retry, and pure helper functions."""
from __future__ import annotations

import json
import os
import re
import sys
import time
__all__ = ['_IB_TOPO_PATH', '_get_ib_topology', '_lookup_ib_connections', '_escape_jql', '_classify_port_role', '_cache_put', '_request_with_retry', '_brief_pause']




# ---------------------------------------------------------------------------
# IB topology (self-contained lazy cache)
# ---------------------------------------------------------------------------
_IB_TOPO_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "ib_topology.json",
)
_ib_topo: dict | None = None


def _get_ib_topology() -> dict:
    """Load IB topology from JSON (lazy, cached)."""
    global _ib_topo
    if _ib_topo is not None:
        return _ib_topo
    if os.path.exists(_IB_TOPO_PATH):
        try:
            with open(_IB_TOPO_PATH) as f:
                _ib_topo = json.load(f)
        except (json.JSONDecodeError, OSError):
            _ib_topo = {}
    else:
        _ib_topo = {}
    return _ib_topo


def _lookup_ib_connections(hostname: str, rack_location: str = None) -> list:
    """Look up IB port connections from the topology JSON.

    Matches by DH + rack + node number extracted from hostname.
    Returns list of {port, leaf_rack, leaf_id, leaf_port} or [].
    """
    topo = _get_ib_topology()
    if not topo:
        return []
    # Extract DH, rack, node from hostname like 'dh1-r102-node-04-us-site-01a'
    m = re.match(r"(dh\d+)-r(\d+)-node-(\d+)", (hostname or "").lower())
    if not m:
        # Try s1-r027-node-14 pattern
        m = re.match(r"s\d+-r(\d+)-node-(\d+)", (hostname or "").lower())
        if m:
            rack = m.group(1).lstrip("0") or "0"
            node = int(m.group(2))
            # Try sector/DH keys
            for dh in ("HALL-A", "DH1", "DH2"):
                key = f"{dh}:{rack}:{node}"
                if key in topo:
                    return topo[key]
            return []
        return []
    dh = m.group(1).upper()
    rack = m.group(2).lstrip("0") or "0"
    node = int(m.group(3))
    # Check for site-specific prefixes based on hostname suffix
    hostname_lower = (hostname or "").lower()
    # Check for site-specific prefix in hostname
    _site_prefixes = [s.strip().lower() for s in os.environ.get("SITE_TOPO_PREFIXES", "").split(",") if s.strip()]
    for _sp in _site_prefixes:
        if _sp in hostname_lower:
            site_key = f"{_sp.upper()}:{rack}:{node}"
            if site_key in topo:
                return topo[site_key]
    key = f"{dh}:{rack}:{node}"
    if key in topo:
        return topo[key]
    # Cutsheet may use continuous numbering (e.g. R306 nodes 17-24 instead of 1-8).
    # Find all entries for this rack, sort by node number, and pick by position.
    prefix = f"{dh}:{rack}:"
    rack_entries = sorted(
        [(k, v) for k, v in topo.items() if k.startswith(prefix)],
        key=lambda x: int(x[0].split(":")[2])
    )
    if rack_entries and 1 <= node <= len(rack_entries):
        return rack_entries[node - 1][1]
    return []


# ---------------------------------------------------------------------------
# Pure utility functions
# ---------------------------------------------------------------------------

def _escape_jql(value: str) -> str:
    """Escape special characters for safe JQL string interpolation."""
    if not value:
        return value
    return value.replace('\\', '\\\\').replace('"', '\\"')


def _classify_port_role(port_name: str) -> str:
    """Classify a network interface by its name into a DCT-readable role."""
    name_lower = port_name.lower()
    if "bmc" in name_lower or "ipmi" in name_lower:
        return "BMC"
    if "dpu" in name_lower:
        return "DPU"
    if "ib" in name_lower or "mlx" in name_lower or "infiniband" in name_lower:
        return "IB"
    if "eno" in name_lower or "eth" in name_lower or "bond" in name_lower:
        return "NIC"
    return "\u2014"


def _cache_put(cache: dict, key: str, value, max_size: int):
    """Insert into a dict-cache and evict the oldest entry if over max_size."""
    if len(cache) >= max_size:
        oldest = next(iter(cache))
        del cache[oldest]
    cache[key] = value


def _request_with_retry(method, *args, retries: int = 2, **kwargs):
    """Call a requests method with simple retry on transient errors.

    Retries on connection errors, SSL errors, and 5xx server errors.
    Uses 1s, 2s backoff between attempts.
    On final failure for network/SSL errors, prints a friendly message
    instead of raising a raw traceback.
    """
    import requests
    last_exc = None
    for attempt in range(1 + retries):
        try:
            resp = method(*args, **kwargs)
            if resp.status_code < 500 or attempt == retries:
                return resp
            # 5xx — retry
        except (requests.exceptions.SSLError, requests.exceptions.ConnectionError) as exc:
            last_exc = exc
            if attempt == retries:
                url = args[0] if args else kwargs.get("url", "unknown")
                host = url.split("/")[2] if isinstance(url, str) and "/" in url else str(url)
                print(f"\n  \033[33m⚠  Network error reaching {host}\033[0m")
                print(f"     {type(exc).__name__}: {_short_exc(exc)}")
                print(f"     Check VPN/Teleport and retry.\n")
                raise SystemExit(1)
        except requests.RequestException as exc:
            last_exc = exc
            if attempt == retries:
                raise
        time.sleep(min(attempt + 1, 3))
    if last_exc:
        raise last_exc


def _short_exc(exc) -> str:
    """Extract a one-line summary from a nested requests exception."""
    msg = str(exc)
    if "SSL" in msg:
        return "SSL handshake failed — server closed connection unexpectedly"
    if "Max retries" in msg:
        # dig into the reason
        reason = getattr(exc, "args", [None])
        inner = reason[0] if reason else exc
        if hasattr(inner, "reason"):
            return str(inner.reason)
    return msg[:120]


def _brief_pause(seconds: float = 0.3):
    """Brief UI feedback pause, capped at 0.3s. Skipped for non-TTY output."""
    if sys.stdout.isatty():
        time.sleep(min(seconds, 0.3))
