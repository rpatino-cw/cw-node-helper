"""Persistent user state, DH layouts, bookmarks, and recent history."""
from __future__ import annotations

import time

import datetime
import json
import os

from cwhelper.config import (
    _DH_CONFIG_PATH, _USER_STATE_PATH, _DEFAULT_STATE,
    BOLD, DIM, RESET,
)

__all__ = ['_load_dh_layouts', '_save_dh_layouts', '_load_user_state', '_save_user_state', '_record_ticket_view', '_record_node_lookup', '_record_queue_view', '_record_rack_view', '_add_bookmark', '_remove_bookmark', '_get_dh_layout', '_setup_dh_layout']


# ---------------------------------------------------------------------------
# DH layout persistence
# ---------------------------------------------------------------------------

def _load_dh_layouts() -> dict:
    """Load saved DH layouts from JSON config file."""
    if os.path.exists(_DH_CONFIG_PATH):
        try:
            with open(_DH_CONFIG_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_dh_layouts(layouts: dict):
    """Persist DH layouts to JSON config file."""
    with open(_DH_CONFIG_PATH, "w") as f:
        json.dump(layouts, f, indent=2)
    try:
        os.chmod(_DH_CONFIG_PATH, 0o600)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# User state persistence
# ---------------------------------------------------------------------------

def _load_user_state() -> dict:
    """Load persistent user state (recents, bookmarks, greeting) from JSON."""
    if os.path.exists(_USER_STATE_PATH):
        try:
            with open(_USER_STATE_PATH) as f:
                data = json.load(f)
            # Ensure all expected keys exist (forward compat)
            for k, v in _DEFAULT_STATE.items():
                data.setdefault(k, v if not isinstance(v, (list, dict)) else type(v)())
            return data
        except (json.JSONDecodeError, OSError):
            pass
    return {k: (v if not isinstance(v, (list, dict)) else type(v)()) for k, v in _DEFAULT_STATE.items()}


def _save_user_state(state: dict):
    """Persist user state to JSON file."""
    state["version"] = 1
    with open(_USER_STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)
    try:
        os.chmod(_USER_STATE_PATH, 0o600)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Recent history recorders
# ---------------------------------------------------------------------------

def _record_ticket_view(state: dict, key: str, summary: str,
                        assignee: str = None, updated: str = None) -> dict:
    """Record a ticket view — updates last_ticket and recent_tickets (max 5)."""
    now = datetime.datetime.utcnow().isoformat() + "Z"
    state["last_ticket"] = key
    recents = state.get("recent_tickets", [])
    recents = [r for r in recents if r.get("key") != key]
    entry = {"key": key, "summary": (summary or "")[:80], "ts": now}
    if assignee:
        entry["assignee"] = assignee
    if updated:
        entry["updated"] = updated
    recents.insert(0, entry)
    state["recent_tickets"] = recents[:5]
    return state


def _record_node_lookup(state: dict, term: str,
                        hostname: str = None, last_ticket: str = None,
                        site: str = None) -> dict:
    """Record a node lookup — updates recent_nodes (max 5)."""
    now = datetime.datetime.utcnow().isoformat() + "Z"
    normalized = term.strip()
    recents = state.get("recent_nodes", [])
    # Preserve existing extra info if we're re-recording the same term
    old = next((r for r in recents if r.get("term", "").lower() == normalized.lower()), None)
    recents = [r for r in recents if r.get("term", "").lower() != normalized.lower()]
    entry = {"term": normalized, "ts": now}
    if hostname:
        entry["hostname"] = hostname
    elif old and old.get("hostname"):
        entry["hostname"] = old["hostname"]
    if last_ticket:
        entry["last_ticket"] = last_ticket
    elif old and old.get("last_ticket"):
        entry["last_ticket"] = old["last_ticket"]
    if site:
        entry["site"] = site
    elif old and old.get("site"):
        entry["site"] = old["site"]
    recents.insert(0, entry)
    state["recent_nodes"] = recents[:5]
    return state


def _record_queue_view(state: dict, project: str, site: str,
                       status_filter: str, mine_only: bool = False) -> dict:
    """Record a queue browse — updates recent_queues (max 5)."""
    now = datetime.datetime.utcnow().isoformat() + "Z"
    params = {"project": project, "site": site,
              "status_filter": status_filter, "mine_only": mine_only}
    sig = json.dumps(params, sort_keys=True)
    recents = state.get("recent_queues", [])
    recents = [r for r in recents if json.dumps(r.get("params", {}), sort_keys=True) != sig]
    site_label = site or "all sites"
    label = f"{project} {status_filter} @ {site_label}"
    if mine_only:
        label += " (mine)"
    recents.insert(0, {"label": label, "params": params, "ts": now})
    state["recent_queues"] = recents[:5]
    _save_user_state(state)
    return state


def _record_rack_view(state: dict, loc: str, tag: str = "") -> dict:
    """Record a rack map view — updates recent_racks (max 5)."""
    now = datetime.datetime.utcnow().isoformat() + "Z"
    recents = state.get("recent_racks", [])
    recents = [r for r in recents if r.get("loc", "").lower() != loc.lower()]
    recents.insert(0, {"loc": loc, "tag": tag, "ts": now})
    state["recent_racks"] = recents[:5]
    return state


# ---------------------------------------------------------------------------
# Bookmark operations
# ---------------------------------------------------------------------------

def _add_bookmark(state: dict, label: str, bm_type: str, params: dict) -> dict:
    """Add a bookmark. Deduplicates by type+params. Max 5 bookmarks."""
    bookmarks = state.get("bookmarks", [])
    # Remove existing bookmark with same type+params
    bookmarks = [b for b in bookmarks if not (b.get("type") == bm_type and b.get("params") == params)]
    bookmarks.append({"label": label, "type": bm_type, "params": params})
    state["bookmarks"] = bookmarks[:5]
    return state


def _remove_bookmark(state: dict, index: int) -> dict:
    """Remove a bookmark by index."""
    bookmarks = state.get("bookmarks", [])
    if 0 <= index < len(bookmarks):
        bookmarks.pop(index)
    state["bookmarks"] = bookmarks
    return state


# ---------------------------------------------------------------------------
# DH layout lookup
# ---------------------------------------------------------------------------

def _get_dh_layout(site_code: str, dh: str) -> dict | None:
    """Look up a saved layout for a site+dh combo (e.g. 'US-SITE01', 'DH1').

    Returns dict with keys: columns (list of {"label", "start", "num_rows"}),
    racks_per_row, serpentine, entrance.  Or None if not configured.
    """
    layouts = _load_dh_layouts()
    key = f"{site_code}.{dh}"
    return layouts.get(key)


def _setup_dh_layout(site_code: str, dh: str) -> dict | None:
    """Prompt user to manually edit dh_layouts.json for a new data hall.

    Returns None (user must edit the file and re-run).
    """
    key = f"{site_code}.{dh}"
    print(f"\n  {BOLD}No layout saved for {key}{RESET}")
    print(f"  {DIM}To add one, edit {os.path.basename(_DH_CONFIG_PATH)} and add:{RESET}\n")
    example = json.dumps({key: {
        "racks_per_row": 10,
        "columns": [
            {"label": "Left", "start": 1, "num_rows": 16},
            {"label": "Right", "start": 161, "num_rows": 15},
        ],
        "serpentine": True,
        "entrance": "bottom-right",
        "total_racks": 310,
    }}, indent=2)
    print(f"  {DIM}{example}{RESET}\n")
    print(f"  {DIM}Notes:{RESET}")
    print(f"  {DIM}  - serpentine: true = zig-zag rows, false = straight rows{RESET}")
    print(f"  {DIM}  - columns: can have 2+ blocks (e.g. A/B/C for 3-column halls){RESET}")
    print(f"  {DIM}  - Each column: start = first rack #, num_rows = rows in that block{RESET}\n")
    print(f"  {DIM}File: {_DH_CONFIG_PATH}{RESET}\n")

    # Create the file with the example if it doesn't exist
    if not os.path.exists(_DH_CONFIG_PATH):
        try:
            with open(_DH_CONFIG_PATH, "w") as f:
                f.write(example)
        except OSError:
            pass

    try:
        raw = input(f"  Press {BOLD}o{RESET} to open in editor, or ENTER to go back: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if raw == "o":
        import subprocess
        editor = os.environ.get("EDITOR", "")
        if not editor:
            # Try common editors
            import shutil as _shutil
            for e in ("code", "nano", "vim", "vi"):
                if _shutil.which(e):
                    editor = e
                    break
        if editor:
            print(f"  {DIM}Opening {_DH_CONFIG_PATH} with {editor}...{RESET}")
            try:
                subprocess.run([editor, _DH_CONFIG_PATH])
            except Exception:
                print(f"  {DIM}Could not open editor. Edit manually: {_DH_CONFIG_PATH}{RESET}")
        else:
            print(f"  {DIM}No editor found. Edit manually: {_DH_CONFIG_PATH}{RESET}")
    return None
