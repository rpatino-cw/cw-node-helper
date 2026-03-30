"""Bookmark management — suggestions, wizards."""
from __future__ import annotations

import json

from cwhelper import config as _cfg
from cwhelper.config import *  # noqa: F401,F403
__all__ = ['_build_bookmark_suggestions', '_manage_bookmarks', '_add_bookmark_wizard', '_remove_bookmark_wizard', '_rename_bookmark_wizard']
from cwhelper.state import _load_user_state, _save_user_state, _add_bookmark, _remove_bookmark
from cwhelper.cache import _brief_pause
from cwhelper.tui.display import _clear_screen




def _build_bookmark_suggestions(state: dict, bookmarks: list) -> list:
    """Build suggestions from recent activity + popular queues, deduped against existing bookmarks."""
    suggestions = []
    # Build a set of existing bookmark signatures for dedup
    existing = set()
    for bm in bookmarks:
        existing.add((bm.get("type"), json.dumps(bm.get("params", {}), sort_keys=True)))

    def _already_exists(bm_type, params):
        return (bm_type, json.dumps(params, sort_keys=True)) in existing

    # 1. Recent tickets
    for r in state.get("recent_tickets", []):
        if len(suggestions) >= 5:
            break
        params = {"key": r["key"]}
        if _already_exists("ticket", params):
            continue
        summary = r.get("summary", "")[:40]
        label = f"{r['key']} \u2014 {summary}" if summary else r["key"]
        suggestions.append({"label": label, "type": "ticket", "params": params, "source": "recent ticket"})

    # 2. Recent nodes
    for n in state.get("recent_nodes", []):
        if len(suggestions) >= 5:
            break
        params = {"term": n["term"]}
        if _already_exists("node", params):
            continue
        suggestions.append({"label": f"Node {n['term']}", "type": "node", "params": params, "source": "recent node"})

    # 3. Recently browsed queues
    for q in state.get("recent_queues", []):
        if len(suggestions) >= 5:
            break
        params = q.get("params", {})
        if _already_exists("queue", params):
            continue
        suggestions.append({"label": q.get("label", "?"), "type": "queue",
                            "params": params, "source": "recent queue"})

    # 4. Popular queue presets
    popular_queues = [
        {"project": "DO", "site": KNOWN_SITES[0] if KNOWN_SITES else "", "status_filter": "open"},
        {"project": "HO", "site": KNOWN_SITES[0] if KNOWN_SITES else "", "status_filter": "open"},
        {"project": "DO", "site": KNOWN_SITES[1] if len(KNOWN_SITES) > 1 else "", "status_filter": "open"},
        {"project": "DO", "site": "", "status_filter": "verification"},
    ]
    for q in popular_queues:
        if len(suggestions) >= 5:
            break
        if _already_exists("queue", q):
            continue
        site_label = q["site"] or "all sites"
        label = f"{q['project']} {q['status_filter']} @ {site_label}"
        suggestions.append({"label": label, "type": "queue", "params": q, "source": "popular queue"})

    return suggestions[:5]



def _manage_bookmarks(state: dict, email: str, token: str) -> dict:
    """Interactive bookmark manager. Returns updated state."""
    bm_keys = "abcde"
    while True:
        _clear_screen()
        bookmarks = state.get("bookmarks", [])

        print(f"\n  {BOLD}Bookmarks{RESET}\n")

        if not bookmarks:
            print(f"  {DIM}No bookmarks saved yet.{RESET}\n")
        else:
            for i, bm in enumerate(bookmarks):
                letter = bm_keys[i] if i < len(bm_keys) else "?"
                type_tag = f"{DIM}({bm.get('type', '?')}){RESET}"
                print(f"    {BOLD}{letter}{RESET}  {bm.get('label', '?')}  {type_tag}")
            print()

        # Show suggestions when there's room for more bookmarks
        suggestions = []
        if len(bookmarks) < 5:
            suggestions = _build_bookmark_suggestions(state, bookmarks)
            if suggestions:
                print(f"  {DIM}Suggestions{RESET}  {DIM}(press number to add){RESET}")
                for i, s in enumerate(suggestions, 1):
                    print(f"    {BOLD}{i}{RESET}. {s['label']}  {DIM}({s['source']}){RESET}")
                print()

        options = f"  {BOLD}+{RESET} Add custom"
        if bookmarks:
            options += f"    {BOLD}-{RESET} Remove    {BOLD}r{RESET} Rename"
        options += f"    {BOLD}ENTER{RESET} Back to menu"
        print(options)
        print()

        try:
            action = input("  > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return state

        if action in ("", "b", "back", "m", "menu"):
            return state

        # Quick-add a suggestion by number
        if action.isdigit():
            idx = int(action) - 1
            if 0 <= idx < len(suggestions):
                s = suggestions[idx]
                state = _add_bookmark(state, s["label"], s["type"], s["params"])
                _save_user_state(state)
                print(f"  {GREEN}Added: {s['label']}{RESET}")
                _brief_pause()
                continue

        if action == "+":
            if len(bookmarks) >= 5:
                print(f"  {DIM}Max 5 bookmarks. Remove one first.{RESET}")
                _brief_pause()
                continue
            state = _add_bookmark_wizard(state, email, token)
            _save_user_state(state)
        elif action == "-" and bookmarks:
            state = _remove_bookmark_wizard(state)
            _save_user_state(state)
        elif action == "r" and bookmarks:
            state = _rename_bookmark_wizard(state)
            _save_user_state(state)



def _add_bookmark_wizard(state: dict, email: str, token: str) -> dict:
    """Guided bookmark creation. Returns updated state."""
    print(f"\n  {DIM}Bookmark type:{RESET}")
    print(f"    {BOLD}1{RESET} Ticket   {DIM}(a specific Jira ticket){RESET}")
    print(f"    {BOLD}2{RESET} Node     {DIM}(service tag or hostname){RESET}")
    print(f"    {BOLD}3{RESET} Queue    {DIM}(project + site + filter){RESET}")

    try:
        bm_choice = input("  Type [1-3]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return state

    if bm_choice == "1":
        try:
            key = input("  Ticket key (e.g. DO-12345): ").strip().upper()
        except (EOFError, KeyboardInterrupt):
            print()
            return state
        if not key:
            return state
        # Try to fetch summary for the label
        label = key
        ctx = _fetch_and_show(key, email, token)
        if ctx:
            summary = ctx.get("summary", "")[:40]
            label = f"{key} \u2014 {summary}" if summary else key
        state = _add_bookmark(state, label, "ticket", {"key": key})

    elif bm_choice == "2":
        try:
            term = input("  Service tag or hostname: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return state
        if not term:
            return state
        state = _add_bookmark(state, f"Node {term}", "node", {"term": term})

    elif bm_choice == "3":
        print(f"\n  {DIM}Project:{RESET}")
        print(f"    {BOLD}1{RESET} DO {DIM}(default){RESET}")
        print(f"    {BOLD}2{RESET} HO")
        try:
            proj_input = input("  Project [1-2] or ENTER for DO: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return state
        project = "HO" if proj_input == "2" else "DO"

        site = _ask_site()

        print(f"\n  {DIM}Status filters:{RESET}")
        print(f"    {BOLD}1{RESET} Open               {BOLD}4{RESET} Waiting For Support")
        print(f"    {BOLD}2{RESET} Verification       {BOLD}5{RESET} Closed")
        print(f"    {BOLD}3{RESET} In Progress         {BOLD}6{RESET} All statuses {DIM}(default){RESET}")
        try:
            sf_input = input("  Filter [1-6] or ENTER for All: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return state

        filter_map = {
            "": "all", "1": "open",
            "2": "verification",
            "3": "in progress",
            "4": "waiting",
            "5": "closed",
            "6": "all",
        }
        status_filter = filter_map.get(sf_input, sf_input)

        site_label = site or "all sites"
        label = f"{project} {status_filter} @ {site_label}"
        state = _add_bookmark(state, label, "queue",
                              {"project": project, "site": site, "status_filter": status_filter})

    return state



def _remove_bookmark_wizard(state: dict) -> dict:
    """Pick and remove a bookmark. Returns updated state."""
    bookmarks = state.get("bookmarks", [])
    bm_keys = "abcde"

    try:
        raw = input(f"  Remove which? [{bm_keys[0]}-{bm_keys[min(len(bookmarks), len(bm_keys))-1]}]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return state

    if raw in bm_keys:
        idx = bm_keys.index(raw)
        if idx < len(bookmarks):
            removed = bookmarks[idx].get("label", "?")
            state = _remove_bookmark(state, idx)
            print(f"  {DIM}Removed: {removed}{RESET}")
            _brief_pause()
    return state



def _rename_bookmark_wizard(state: dict) -> dict:
    """Pick and rename a bookmark. Returns updated state."""
    bookmarks = state.get("bookmarks", [])
    bm_keys = "abcde"

    try:
        raw = input(f"  Rename which? [{bm_keys[0]}-{bm_keys[min(len(bookmarks), len(bm_keys))-1]}]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return state

    if raw in bm_keys:
        idx = bm_keys.index(raw)
        if idx < len(bookmarks):
            old_label = bookmarks[idx].get("label", "?")
            try:
                new_label = input(f"  New name for '{old_label}': ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return state
            if new_label:
                bookmarks[idx]["label"] = new_label
                state["bookmarks"] = bookmarks
                print(f"  {GREEN}Renamed → {new_label}{RESET}")
                _brief_pause()
    return state


