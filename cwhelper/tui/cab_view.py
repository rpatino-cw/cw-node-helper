"""Cabinet rack view — display all active tickets in a rack grouped by node."""
from __future__ import annotations

import re

from cwhelper.config import BOLD, DIM, RESET, RED, GREEN, YELLOW, CYAN
from cwhelper.tui.display import _clear_screen, _print_pretty

__all__ = ['_run_cab_view']


def _run_cab_view(rack_input: str, site: str, email: str, token: str):
    """Display all active tickets in a rack grouped by node. Returns ctx if user picks a ticket."""
    from cwhelper.services.search import _jql_search
    from cwhelper.services.context import _parse_rack_location, _format_age, _parse_jira_timestamp, _fetch_and_show

    _rack_m = re.match(r'^R?(\d{1,4})$', rack_input.strip(), re.IGNORECASE)
    if not _rack_m:
        print(f"\n  {DIM}Could not parse rack: {rack_input}{RESET}")
        return None
    rack_num   = int(_rack_m.group(1))
    rack_label = f"R{rack_num}"

    print(f"\n  {DIM}Querying tickets in {rack_label}...{RESET}", end="", flush=True)

    site_clause = f'cf[10194] = "{site}"' if site else 'project = "DO"'
    _open_st = ('"Open","Awaiting Support","Awaiting Triage","To Do","New",'
                '"In Progress","On Hold","Blocked","Verification"')
    try:
        all_tickets = _jql_search(
            f'project = "DO" AND {site_clause} AND status in ({_open_st}) ORDER BY created ASC',
            email, token, max_results=200, use_cache=False,
            fields=["key", "summary", "status", "assignee",
                    "customfield_10192", "customfield_10193", "customfield_10207"],
        )
    except Exception as _e:
        print(f"\r  {RED}Query failed: {_e}{RESET}      ")
        return None

    print(f"\r{'':60}\r", end="")

    # Client-side filter to this rack
    rack_tickets = []
    for _iss in all_tickets:
        _loc = (_iss.get("fields", {}).get("customfield_10207") or "")
        if isinstance(_loc, list):
            _loc = _loc[0] if _loc else ""
        if isinstance(_loc, dict):
            _loc = _loc.get("value", "") or ""
        _p = _parse_rack_location(str(_loc))
        if _p and _p.get("rack") == rack_num:
            rack_tickets.append(_iss)

    if not rack_tickets:
        print(f"  {DIM}No active tickets found in {rack_label}.{RESET}")
        return None

    # Group by service tag, extract node label from hostname
    _by_tag: dict[str, list] = {}
    _tag_node: dict[str, str] = {}
    for _iss in rack_tickets:
        _f = _iss.get("fields", {})
        _tag = _f.get("customfield_10193") or ""
        if isinstance(_tag, list):
            _tag = _tag[0] if _tag else "—"
        elif isinstance(_tag, dict):
            _tag = _tag.get("value", "—")
        _tag = str(_tag) if _tag else "—"
        _by_tag.setdefault(_tag, []).append(_iss)
        if _tag not in _tag_node:
            _hn = _f.get("customfield_10192") or ""
            if isinstance(_hn, list):
                _hn = _hn[0] if _hn else ""
            elif isinstance(_hn, dict):
                _hn = _hn.get("value", "")
            _nm = re.search(r"node-?(\d+)", str(_hn), re.IGNORECASE)
            _tag_node[_tag] = f"Node {int(_nm.group(1)):02d}" if _nm else ""

    # Build flat numbered list for picking
    flat: list = []

    _clear_screen()
    print(f"\n  {BOLD}{CYAN}Cab View — {rack_label}{RESET}  {DIM}({len(rack_tickets)} tickets){RESET}\n")

    for _tag, _issues in sorted(_by_tag.items()):
        _node_lbl = _tag_node.get(_tag, "")
        _header   = f"{_tag}  {DIM}{_node_lbl}{RESET}" if _node_lbl else _tag
        print(f"  {BOLD}{_header}{RESET}")
        for _iss in sorted(_issues, key=lambda x: x.get("key", "")):
            _key         = _iss.get("key", "")
            _f           = _iss.get("fields", {})
            _status_name = (_f.get("status") or {}).get("name", "")
            _assignee    = (_f.get("assignee") or {}).get("displayName", "")
            _first       = _assignee.split()[0] if _assignee else f"{RED}—{RESET}"
            _summary     = (_f.get("summary") or "")[:55]
            _sn_lower    = _status_name.lower()
            if "progress" in _sn_lower:
                _sc, _dot = GREEN, "●"
            elif "hold" in _sn_lower or "block" in _sn_lower:
                _sc, _dot = YELLOW, "◆"
            elif "verif" in _sn_lower:
                _sc, _dot = CYAN, "●"
            else:
                _sc, _dot = DIM, "○"
            _idx = len(flat) + 1
            flat.append(_iss)
            print(f"    {DIM}{_idx:2}.{RESET} {_sc}{_dot}{RESET} {BOLD}{_key}{RESET}  "
                  f"{DIM}{_status_name:<20}{RESET}  {_first:<14}  {DIM}{_summary}{RESET}")
        print()

    try:
        _raw = input(f"  {DIM}Pick a number to open, or ENTER to go back: {RESET}").strip()
    except (EOFError, KeyboardInterrupt):
        return None

    if not _raw:
        return None
    try:
        _pick = int(_raw)
        if 1 <= _pick <= len(flat):
            _picked_key = flat[_pick - 1].get("key", "")
            ctx = _fetch_and_show(_picked_key, email, token)
            if ctx:
                _clear_screen()
                _print_pretty(ctx)
                return ctx
    except ValueError:
        pass
    return None
