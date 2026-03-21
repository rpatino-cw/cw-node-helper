"""Interactive menu — main TUI loop."""
from __future__ import annotations

import re
import time

import os
import sys

from cwhelper import config as _cfg
from cwhelper.config import *  # noqa: F401,F403
__all__ = ['_ask_site', '_ask_queue_filters', '_interactive_menu']
from cwhelper.tui.display import _clear_screen, _print_pretty, _print_banner, _print_help, _status_color, _prompt_select, _print_json, _print_raw
from cwhelper.tui.actions import _post_detail_prompt, _run_cab_view
from cwhelper.services.ai import _ai_available, _ai_dispatch, _ai_chat_loop, _ai_find_ticket, _ai_summarize, _suggest_comments, _pick_or_type_comment, _ai_work_feedback
from cwhelper.clients.jira import _get_credentials, _get_first_name, _get_my_account_id, _jira_post, _execute_transition
from cwhelper.state import _load_user_state, _save_user_state, _record_ticket_view, _record_queue_view, _record_node_lookup, _record_rack_view
from cwhelper.cache import _brief_pause
from cwhelper.services.context import _format_age, _parse_jira_timestamp, _unwrap_field, _parse_rack_location, _fetch_and_show
from cwhelper.services.search import _search_queue
from cwhelper.services.queue import _run_queue_interactive, _run_history_interactive, _search_node_history, _run_stale_verification
from cwhelper.services.watcher import _is_watcher_running, _start_background_watcher, _stop_background_watcher, _handle_new_tickets, _is_radar_running, _handle_radar_tickets
from cwhelper.services.bookmarks import _manage_bookmarks
from cwhelper.services.rack import _draw_mini_dh_map
from cwhelper.services.session_log import _log_event, _print_session_log, _copy_session_to_clipboard, _print_jira_activity
from cwhelper.services.walkthrough import _walkthrough_mode
from cwhelper.services.brief import run_shift_brief
from cwhelper.clients.teleport import _tsh_cluster_status
from cwhelper.tui.rich_console import _rich_print_menu, console



def _ask_site() -> str | None:
    """Prompt the user to pick a site from a numbered list or type one.
    Returns None if user wants to go back."""
    print(f"\n  {DIM}Sites:{RESET}")
    print(f"    {BOLD}0{RESET} All sites {DIM}(no filter){RESET}")
    for i, s in enumerate(KNOWN_SITES, start=1):
        print(f"    {BOLD}{i}{RESET} {s}")
    print(f"    {DIM}Or type a site name directly  |  b = back{RESET}")

    try:
        raw = input(f"  Site [0-{len(KNOWN_SITES)}] or ENTER for all: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None

    if raw.lower() in ("b", "back"):
        return None
    if raw == "" or raw == "0":
        return ""
    try:
        idx = int(raw)
        if 1 <= idx <= len(KNOWN_SITES):
            return KNOWN_SITES[idx - 1]
    except ValueError:
        pass
    # Treat as raw site string (typed manually)
    return raw


def _ask_queue_filters(prompt_site: bool = True, project: str = "DO") -> dict | None:
    """Prompt for site and optional status filter. Returns dict or None."""
    if prompt_site:
        site = _ask_site()
        if site is None:
            return None
    else:
        site = ""

    try:
        print(f"\n  {DIM}Status filters:{RESET}")
        print(f"    {BOLD}1{RESET} Open               {BOLD}4{RESET} Waiting For Support")
        print(f"    {BOLD}2{RESET} Verification       {BOLD}5{RESET} Closed")
        print(f"    {BOLD}3{RESET} In Progress         {BOLD}6{RESET} All statuses {DIM}(default){RESET}")
        if project == "HO":
            print(f"    {BOLD}7{RESET} {YELLOW}Radar{RESET} {DIM}(pre-DO: RMA-initiate, Sent to DCT, etc.){RESET}")
            max_opt = 7
        elif project == "SDA":
            print(f"    {BOLD}7{RESET} {YELLOW}Awaiting Triage{RESET}")
            print(f"    {BOLD}8{RESET} {YELLOW}Customer Verification{RESET}")
            max_opt = 8
        else:
            max_opt = 6

        sf_input = input(f"  Filter [1-{max_opt}], ENTER for All, or b to go back: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None

    if sf_input.lower() in ("b", "back"):
        return None

    filter_map = {
        "": "all", "1": "open",
        "2": "verification",
        "3": "in progress",
        "4": "waiting",
        "5": "closed",
        "6": "all",
    }
    if project == "HO":
        filter_map["7"] = "radar"
    elif project == "SDA":
        filter_map["7"] = "triage"
        filter_map["8"] = "cust verify"
    status_filter = filter_map.get(sf_input, sf_input)  # allow raw text too

    return {"site": site, "status_filter": status_filter}


def _open_ticket(key: str, email: str, token: str, state: dict) -> tuple[str, dict]:
    """Fetch, display, and run the action loop for a single ticket.

    Returns ('quit', state) if the user chose to quit, ('continue', state) otherwise.
    """
    ctx = _fetch_and_show(key, email, token)
    if not ctx:
        return "continue", state
    state = _record_ticket_view(state, ctx["issue_key"], ctx.get("summary", ""),
                                assignee=ctx.get("assignee"), updated=ctx.get("updated"))
    _save_user_state(state)
    _clear_screen()
    _print_pretty(ctx)
    action = _post_detail_prompt(ctx, email, token, state=state)
    if action == "quit":
        return "quit", state
    while action == "history":
        tag = ctx.get("service_tag") or ctx.get("hostname")
        if not tag:
            break
        h_action = _run_history_interactive(email, token, tag)
        if h_action == "quit":
            return "quit", state
        _clear_screen()
        _print_pretty(ctx)
        action = _post_detail_prompt(ctx, email, token, state=state)
    return "continue", state


def _interactive_menu():
    """Main interactive loop. Keeps running until user quits."""
    global _AI_ENABLED, _NTFY_ENABLED
    email, token = _get_credentials()
    _menu_compact = False  # full menu by default; ?? toggles compact mode

    # Pre-warm user identity in background (for greeting + "my tickets")
    _executor.submit(_get_my_account_id, email, token)

    # Load persistent state and resolve greeting
    state = _load_user_state()

    # Seed default bookmarks on first run (user can always remove them)
    if not state.get("bookmarks"):
        state["bookmarks"] = [
            {"label": "All my tickets", "type": "queue",
             "params": {"site": "", "project": "DO", "status_filter": "open", "mine_only": True}},
            {"label": "The Elks Open queue", "type": "queue",
             "params": {"site": "US-CENTRAL-07A", "project": "DO", "status_filter": "open"}},
            {"label": "My Verification tickets", "type": "queue",
             "params": {"site": "", "project": "DO", "status_filter": "verification", "mine_only": True}},
        ]
        _save_user_state(state)

    first_name = state.get("user", {}).get("first_name") or ""
    if not first_name:
        # Will resolve on first loop once the background call finishes
        first_name = ""

    # Stale check — cached result shown instantly, refreshed in background each loop
    _stale_count = 0
    _stale_cache = []
    _stale_future = None

    def _fetch_stale_issues():
        try:
            _vr = _jira_post("/rest/api/3/search/jql", email, token, body={
                "jql": 'project in ("DO", "HO") AND assignee = currentUser() AND status = "Verification" ORDER BY updated ASC',
                "maxResults": 30,
                "fields": ["key", "summary", "statuscategorychangedate",
                            "customfield_10193", "customfield_10194",
                            "customfield_10192", "customfield_10207",
                            "reporter"],
            })
            return _vr.json().get("issues", []) if _vr and _vr.ok else []
        except Exception:
            return []

    # Kick off first stale check immediately in background
    _stale_future = _executor.submit(_fetch_stale_issues)

    # Kick off Teleport cluster status check in background
    _cluster_future = _executor.submit(_tsh_cluster_status)
    _cluster_online: str | None = None
    _cluster_last_check: float = time.time()

    while True:
        # Collect completed cluster status (non-blocking)
        if _cluster_future and _cluster_future.done():
            try:
                _cluster_online = _cluster_future.result()
            except Exception:
                _cluster_online = None
            _cluster_future = None

        # Re-check cluster status every 5 min
        if _cluster_future is None and (time.time() - _cluster_last_check) > 300:
            _cluster_future = _executor.submit(_tsh_cluster_status)
            _cluster_last_check = time.time()

        # Collect completed stale check result (non-blocking)
        if _stale_future and _stale_future.done():
            try:
                _vi = _stale_future.result()
                _stale_cache = [iss for iss in _vi
                                if _parse_jira_timestamp(iss.get("fields", {}).get("statuscategorychangedate")) > 48 * 3600]
                _stale_count = len(_stale_cache)
            except Exception:
                pass
            _stale_future = None

        # Cache per-iteration function results (avoid repeated calls in hot loop)
        watcher_running = _is_watcher_running()
        ai_available = _ai_available()

        # Lazily resolve greeting if not yet available
        if not first_name and _my_display_name:
            first_name = _my_display_name.split()[0]
            state["user"] = {
                "display_name": _my_display_name,
                "first_name": first_name,
                "account_id": _my_account_id,
            }
            _save_user_state(state)

        _clear_screen()
        _print_banner(first_name)

        # Queue next stale check in background (result ready by next menu render)
        if _stale_future is None:
            _stale_future = _executor.submit(_fetch_stale_issues)

        # --- Watcher info ---
        watcher_str = ""
        if watcher_running:
            site_label = _watcher_site or "all sites"
            radar_tag = " + radar" if _is_radar_running() else ""
            watcher_str = f"{_watcher_project} @ {site_label} — every {_watcher_interval}s{radar_tag}"

        # --- Last ticket shortcut ---
        last_ticket_pair = None
        last_key = state.get("last_ticket")
        if last_key:
            last_summary = ""
            for r in state.get("recent_tickets", []):
                if r.get("key") == last_key:
                    last_summary = r.get("summary", "")
                    break
            last_ticket_pair = (last_key, last_summary)

        # --- Build options list ---
        opt4 = ("4", "Stop watching",  "watcher is running") if watcher_running \
               else ("4", "Watch queue", "grab tickets live")

        options = [
            ("1",  "Lookup",       "ticket key, service tag, or hostname"),
            ("2",  "Browse queue", "DO · HO · SDA"),
            ("3",  "My tickets",   ""),
            opt4,
            ("5",  "Rack map",     ""),
            ("6",  "Bookmarks",    ""),
            ("",   "",             ""),
            ("b",  "Shift brief",  "AI priority summary — what to work on first"),
            ("p",  "Start all",    "bulk In Progress"),
            ("l",  "Activity",     "log · Jira"),
            ("w",  "Walkthrough",  "rack-by-rack DH walk"),
        ]

        bookmarks = state.get("bookmarks", [])
        bm_keys   = "abcde"
        shortcuts = [
            (bm_keys[i], bm.get("label", "?"))
            for i, bm in enumerate(bookmarks)
            if i < len(bm_keys)
        ]

        _rich_print_menu(
            options=options,
            shortcuts=shortcuts if shortcuts else None,
            stale_count=_stale_count,
            last_ticket=last_ticket_pair,
            watcher_info=watcher_str,
            ai_enabled=_AI_ENABLED,
            ai_available=ai_available,
            compact=_menu_compact,
            cluster_status=_cluster_online,
        )

        # --- Check for new tickets from background watcher ---
        if watcher_running:
            result = _handle_new_tickets(email, token)
            if result == "quit":
                _stop_background_watcher()
                print(f"\n  {DIM}Goodbye.{RESET}\n")
                return

        # --- Check for radar HO tickets (pre-DO awareness) ---
        if _is_radar_running():
            result = _handle_radar_tickets(email, token)
            if result == "quit":
                _stop_background_watcher()
                print(f"\n  {DIM}Goodbye.{RESET}\n")
                return

        # Build prompt hint
        bm_hint = f", [{bm_keys[0]}-{bm_keys[len(bookmarks)-1]}]" if bookmarks else ""
        watcher_hint = ""
        if watcher_running:
            pending = _watcher_queue.qsize()
            radar_pending = _cfg._radar_queue.qsize() if _is_radar_running() else 0
            if pending > 0 or radar_pending > 0:
                parts = []
                if pending > 0:
                    parts.append(f"{pending} NEW TICKET{'S' if pending != 1 else ''}")
                if radar_pending > 0:
                    parts.append(f"{radar_pending} RADAR HO{'s' if radar_pending != 1 else ''}")
                combined = " + ".join(parts)
                watcher_hint = (
                    f"\n  {YELLOW}{BOLD}{'━' * 50}{RESET}"
                    f"\n  {YELLOW}{BOLD}  {combined} FOUND!"
                    f"  Press ENTER to view{RESET}"
                    f"\n  {YELLOW}{BOLD}{'━' * 50}{RESET}\n"
                )
            else:
                watcher_hint = f"\n  {DIM}Watching... press ENTER to refresh{RESET}"
        try:
            choice = input(f"  Select [0-6, b]{bm_hint}, ticket key, or q: {watcher_hint}").strip().lower()
        except (EOFError, KeyboardInterrupt):
            _stop_background_watcher()
            print(f"\n\n  {DIM}Goodbye.{RESET}\n")
            return

        # --- Empty input: refresh menu (re-check watcher, re-render) ---
        if choice == "":
            continue

        # --- Quit ----------------------------------------------------------
        if choice in ("q", "quit", "exit"):
            _stop_background_watcher()
            print(f"\n  {DIM}Goodbye.{RESET}\n")
            return

        # --- AI toggle -----------------------------------------------------
        if choice == "ai on":
            _AI_ENABLED = True
            print(f"\n  {GREEN}AI enabled.{RESET} {DIM}Unrecognized input will go to AI chat.{RESET}")
            _brief_pause(1)
            continue
        if choice == "ai off":
            _AI_ENABLED = False
            print(f"\n  {YELLOW}AI disabled.{RESET}")
            _brief_pause(1)
            continue
        # --- ntfy.sh toggle ------------------------------------------------
        if choice == "ntfy on":
            _NTFY_ENABLED = True
            print(f"\n  {GREEN}ntfy.sh notifications enabled.{RESET}")
            if not NTFY_TOPIC:
                print(f"  {YELLOW}Set NTFY_TOPIC in .env to receive alerts.{RESET}")
            _brief_pause(1)
            continue
        if choice == "ntfy off":
            _NTFY_ENABLED = False
            print(f"\n  {YELLOW}ntfy.sh notifications disabled.{RESET}")
            _brief_pause(1)
            continue
        # --- AI chat (explicit) --------------------------------------------
        if choice == "ai":
            found_key = _ai_dispatch(email=email, token=token)
            if found_key and JIRA_KEY_PATTERN.match(found_key):
                _act, state = _open_ticket(found_key, email, token, state)
                if _act == "quit":
                    print(f"\n  {DIM}Goodbye.{RESET}\n")
                    return
            continue

        # --- 0: Return to last ticket ------------------------------------
        if choice == "0" and state.get("last_ticket"):
            _act, state = _open_ticket(state["last_ticket"], email, token, state)
            if _act == "quit":
                print(f"\n  {DIM}Goodbye.{RESET}\n")
                return
            continue

        # --- Direct ticket key at main menu (e.g. DO-12345) --------------
        if JIRA_KEY_PATTERN.match(choice.upper()):
            _act, state = _open_ticket(choice.upper(), email, token, state)
            if _act == "quit":
                print(f"\n  {DIM}Goodbye.{RESET}\n")
                return
            continue

        # --- Bookmark shortcuts (a-e) ------------------------------------
        if choice in bm_keys and (_bm_idx := bm_keys.index(choice)) < len(bookmarks):
            bm = bookmarks[_bm_idx]
            bm_type = bm.get("type")
            params = bm.get("params", {})

            if bm_type == "ticket":
                _act, state = _open_ticket(params["key"], email, token, state)
                if _act == "quit":
                    print(f"\n  {DIM}Goodbye.{RESET}\n")
                    return
            elif bm_type == "node":
                state = _record_node_lookup(state, params["term"])
                _save_user_state(state)
                action = _run_history_interactive(email, token, params["term"])
                if action == "quit":
                    print(f"\n  {DIM}Goodbye.{RESET}\n")
                    return
            elif bm_type == "queue":
                action = _run_queue_interactive(
                    email, token, params.get("site", ""),
                    mine_only=params.get("mine_only", False),
                    status_filter=params.get("status_filter", "open"),
                    project=params.get("project", "DO"))
                if action == "quit":
                    print(f"\n  {DIM}Goodbye.{RESET}\n")
                    return
            continue

        # --- 1: Smart lookup — ticket key OR service tag / hostname ----------
        if choice == "1":
            recent_tickets = list(state.get("recent_tickets", []))
            recent_nodes   = list(state.get("recent_nodes", []))
            _fetched_issues: list = []

            # Single API fetch to backfill both lists if either is thin
            if len(recent_tickets) < 3 or len(recent_nodes) < 3:
                print(f"\n  {DIM}Loading recent...{RESET}", end="", flush=True)
                try:
                    _futs = [
                        _executor.submit(_search_queue, "", email, token,
                                         mine_only=True, limit=10, status_filter="all", project=p)
                        for p in ("DO", "HO", "SDA")
                    ]
                    _fetched_issues = []
                    for _f in _futs:
                        try:
                            _fetched_issues += _f.result()
                        except Exception:
                            pass
                except Exception:
                    pass
                print(f"\r{'':60}\r", end="")

            # Backfill tickets
            if len(recent_tickets) < 3 and _fetched_issues:
                seen_t = {r["key"] for r in recent_tickets}
                for iss in sorted(_fetched_issues, key=lambda x: x.get("key", ""), reverse=True):
                    if len(recent_tickets) >= 3:
                        break
                    k = iss["key"]
                    if k not in seen_t:
                        seen_t.add(k)
                        f_ = iss.get("fields", {})
                        entry = {"key": k, "summary": f_.get("summary", "")[:80], "_backfill": True}
                        assignee_obj = f_.get("assignee")
                        if assignee_obj:
                            entry["assignee"] = assignee_obj.get("displayName")
                        if f_.get("updated"):
                            entry["updated"] = f_["updated"]
                        recent_tickets.append(entry)

            # Backfill nodes
            if len(recent_nodes) < 3 and _fetched_issues:
                seen_n = {n["term"].lower() for n in recent_nodes}
                for iss in _fetched_issues:
                    if len(recent_nodes) >= 3:
                        break
                    f = iss.get("fields", {})
                    tag = _unwrap_field(f.get("customfield_10193"))
                    if tag and tag.lower() not in seen_n:
                        seen_n.add(tag.lower())
                        entry = {"term": tag, "_backfill": True}
                        hn   = _unwrap_field(f.get("customfield_10192")) or ""
                        site = _unwrap_field(f.get("customfield_10194")) or ""
                        if hn:
                            entry["hostname"] = hn
                        if site:
                            entry["site"] = site
                        entry["last_ticket"] = iss.get("key", "")
                        recent_nodes.append(entry)

            # Build combined numbered list (tickets first, then nodes)
            t_slice = recent_tickets[:3]
            n_slice = recent_nodes[:3]
            combined = (
                [{"type": "ticket", "data": r} for r in t_slice] +
                [{"type": "node",   "data": n} for n in n_slice]
            )

            if t_slice:
                print(f"\n  {DIM}Recent tickets:{RESET}")
                for i, r in enumerate(t_slice, 1):
                    label = r.get("summary", "")[:38]
                    assignee = r.get("assignee")
                    asgn_str = (f" {CYAN}{assignee.split()[0]}{RESET}" if assignee
                                else f" {RED}unassigned{RESET}")
                    upd = r.get("updated", "")
                    upd_str = (f" {DIM}upd {_format_age(_parse_jira_timestamp(upd))}{RESET}"
                               if upd else "")
                    print(f"    {BOLD}{i}{RESET}. {r['key']}  {DIM}{label}{RESET}{asgn_str}{upd_str}")

            if n_slice:
                print(f"\n  {DIM}Recent nodes:{RESET}")
                for i, n in enumerate(n_slice, len(t_slice) + 1):
                    extras = []
                    if n.get("hostname"):
                        extras.append(n["hostname"])
                    if n.get("site"):
                        extras.append(n["site"])
                    if n.get("last_ticket"):
                        extras.append(n["last_ticket"])
                    extra_str = f"  {DIM}{' │ '.join(extras)}{RESET}" if extras else ""
                    print(f"    {BOLD}{i}{RESET}. {n['term']}{extra_str}")

            print()

            try:
                prompt = "  Enter ticket key, service tag, or hostname"
                if combined:
                    prompt += f", pick [1-{len(combined)}]"
                prompt += ", or ENTER to go back: "
                raw = input(prompt).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                continue

            if not raw or raw.lower() in ("b", "back"):
                continue

            # Route: number → combined list; Jira key → ticket; else → node
            chosen = None
            try:
                idx = int(raw)
                if 1 <= idx <= len(combined):
                    chosen = combined[idx - 1]
            except ValueError:
                pass

            if chosen and chosen["type"] == "ticket":
                _act, state = _open_ticket(chosen["data"]["key"], email, token, state)
                if _act == "quit":
                    print(f"\n  {DIM}Goodbye.{RESET}\n")
                    return
            elif chosen and chosen["type"] == "node":
                term = chosen["data"]["term"]
                state = _record_node_lookup(state, term)
                _save_user_state(state)
                action = _run_history_interactive(email, token, term)
                if action == "quit":
                    print(f"\n  {DIM}Goodbye.{RESET}\n")
                    return
            elif JIRA_KEY_PATTERN.match(raw.upper()):
                _act, state = _open_ticket(raw.upper(), email, token, state)
                if _act == "quit":
                    print(f"\n  {DIM}Goodbye.{RESET}\n")
                    return
            elif re.match(r'^R?\d{1,4}$', raw, re.IGNORECASE):
                # Rack number lookup → Cab View
                _recent_nodes = state.get("recent_nodes", [])
                site = next((n.get("site", "") for n in _recent_nodes if n.get("site")), "")
                ctx = _run_cab_view(raw, site, email, token)
                if ctx:
                    state = _record_ticket_view(state, ctx["issue_key"], ctx.get("summary", ""),
                                               assignee=ctx.get("assignee"), updated=ctx.get("updated"))
                    _save_user_state(state)
                    action = _post_detail_prompt(ctx, email, token, state=state)
                    if action == "quit":
                        print(f"\n  {DIM}Goodbye.{RESET}\n")
                        return
            else:
                term = raw
                _node_hn, _node_site, _node_ticket = None, None, None
                try:
                    _node_issues = _search_node_history(term, email, token, limit=1)
                    if _node_issues:
                        _nf = _node_issues[0].get("fields", {})
                        _node_hn     = _unwrap_field(_nf.get("customfield_10192")) or None
                        _node_site   = _unwrap_field(_nf.get("customfield_10194")) or None
                        _node_ticket = _node_issues[0].get("key")
                except Exception:
                    pass
                state = _record_node_lookup(state, term,
                                            hostname=_node_hn, last_ticket=_node_ticket, site=_node_site)
                _save_user_state(state)
                action = _run_history_interactive(email, token, term)
                if action == "quit":
                    print(f"\n  {DIM}Goodbye.{RESET}\n")
                    return

        # --- 2: Browse queue (DO, HO, or SDA) --------------------------------
        elif choice == "2":
            print(f"\n  {DIM}Project:{RESET}")
            print(f"    {BOLD}1{RESET} DO  — Data Operations {DIM}(hands-on: reseat, swap, cable){RESET}")
            print(f"    {BOLD}2{RESET} HO  — Hardware Operations {DIM}(RMA lifecycle, vendor, parts){RESET}")
            print(f"    {BOLD}3{RESET} SDA — Service Desk Albatross {DIM}(Albatross hardware incidents){RESET}")
            try:
                proj_input = input(f"  Project [1-3], ENTER for DO, or b to go back: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                continue
            if proj_input.lower() in ("b", "back"):
                continue
            project = {"2": "HO", "3": "SDA"}.get(proj_input, "DO")

            opts = _ask_queue_filters(project=project)
            if not opts:
                continue

            action = _run_queue_interactive(
                email, token, opts["site"],
                status_filter=opts["status_filter"], project=project)
            if action == "quit":
                print(f"\n  {DIM}Goodbye.{RESET}\n")
                return

        # --- 3: My tickets — with stale sub-filter when stale tickets exist ---
        elif choice == "3":
            if _stale_count > 0:
                plural = "s" if _stale_count != 1 else ""
                print(f"\n  {DIM}My tickets:{RESET}")
                print(f"    {BOLD}1{RESET} All my tickets")
                print(f"    {BOLD}2{RESET} Stale verification  {RED}{BOLD}{_stale_count} ticket{plural} >48h{RESET}")
                try:
                    sub = input("  [1-2] or ENTER for all: ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    continue
                if sub == "2":
                    action = _run_stale_verification(_stale_cache, email, token)
                    if action == "quit":
                        _stop_background_watcher()
                        print(f"\n  {DIM}Goodbye.{RESET}\n")
                        return
                    continue
            action = _run_queue_interactive(
                email, token, "",
                mine_only=True, status_filter="all", project="DO")
            if action == "quit":
                print(f"\n  {DIM}Goodbye.{RESET}\n")
                return

        # --- 4: Watch queue (toggle background watcher) --------------------
        elif choice == "4":
            if _is_watcher_running():
                _stop_background_watcher()
                print(f"\n  {DIM}Watcher stopped.{RESET}")
                _brief_pause()
                continue

            print(f"\n  {DIM}Project:{RESET}")
            print(f"    {BOLD}1{RESET} DO {DIM}(default){RESET}")
            print(f"    {BOLD}2{RESET} HO")
            print(f"    {BOLD}3{RESET} SDA")
            try:
                proj_input = input(f"  Project [1-3], ENTER for DO, or b to go back: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                continue
            if proj_input.lower() in ("b", "back"):
                continue
            proj = {"2": "HO", "3": "SDA"}.get(proj_input, "DO")

            site = _ask_site()
            if site is None:
                continue

            print(f"\n  {DIM}Poll interval:{RESET}")
            print(f"    {BOLD}1{RESET} Every 30 seconds")
            print(f"    {BOLD}2{RESET} Every 45 seconds")
            print(f"    {BOLD}3{RESET} Every 60 seconds {DIM}(default){RESET}")
            try:
                int_input = input(f"  Interval [1-3], ENTER for 60s, or b to go back: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                continue
            if int_input.lower() in ("b", "back"):
                continue

            interval_map = {"1": 30, "2": 45, "3": 60, "": 60}
            interval = interval_map.get(int_input, 60)

            started = _start_background_watcher(
                email, token, site, project=proj, interval=interval)
            if started:
                site_label = site or "all sites"
                print(f"\n  {GREEN}{BOLD}Watcher started!{RESET} {proj} @ {site_label} — every {interval}s")
                print(f"  {DIM}New tickets will appear inline. Use option 4 to stop.{RESET}")
                _brief_pause()
            else:
                print(f"\n  {DIM}Watcher is already running.{RESET}")
                _brief_pause()

        # --- 5: Rack map -------------------------------------------------------
        elif choice == "5":
            recent_racks = list(state.get("recent_racks", []))

            # Backfill from user's queue if fewer than 5
            if len(recent_racks) < 5:
                print(f"\n  {DIM}Loading recent rack locations...{RESET}", end="", flush=True)
                try:
                    _rfuts = [
                        _executor.submit(_search_queue, "", email, token,
                                         mine_only=True, limit=10, status_filter="all", project=p)
                        for p in ("DO", "HO", "SDA")
                    ]
                    my_issues = []
                    for _f in _rfuts:
                        try:
                            my_issues += _f.result()
                        except Exception:
                            pass
                    seen = {r["loc"].lower() for r in recent_racks}
                    for iss in my_issues:
                        if len(recent_racks) >= 5:
                            break
                        f = iss.get("fields", {})
                        loc = _unwrap_field(f.get("customfield_10207"))  # rack_location
                        if loc and loc.lower() not in seen:
                            seen.add(loc.lower())
                            tag = _unwrap_field(f.get("customfield_10193")) or ""
                            recent_racks.append({"loc": loc, "tag": tag, "_backfill": True})
                except Exception:
                    pass
                print(f"\r{'':60}\r", end="")

            if recent_racks:
                print(f"\n  {DIM}Recent racks:{RESET}")
                for i, r in enumerate(recent_racks[:5], 1):
                    tag_hint = f"  {DIM}({r['tag']}){RESET}" if r.get("tag") else ""
                    dim = f"  {DIM}(from queue){RESET}" if r.get("_backfill") else ""
                    print(f"    {BOLD}{i}{RESET}. {r['loc']}{tag_hint}{dim}")
                print()

            try:
                prompt = "  Enter rack location"
                if recent_racks:
                    prompt += f", pick [1-{len(recent_racks[:5])}]"
                prompt += ": "
                raw = input(prompt).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                continue

            if not raw:
                continue

            # Check if user picked a recent by number
            rack_input = None
            try:
                idx = int(raw)
                if 1 <= idx <= len(recent_racks[:5]):
                    rack_input = recent_racks[idx - 1]["loc"]
            except ValueError:
                pass

            if not rack_input:
                rack_input = raw

            parsed = _parse_rack_location(rack_input)
            if parsed:
                state = _record_rack_view(state, rack_input)
                _save_user_state(state)
                _clear_screen()
                _draw_mini_dh_map(rack_input)
                input(f"  {DIM}Press ENTER to return to menu...{RESET}")
                _clear_screen()
            else:
                print(f"  {DIM}Could not parse rack location. Expected format: US-EVI01.DH1.R64.RU34{RESET}")

        # --- 6: Bookmark manager ----------------------------------------------
        elif choice == "6":
            state = _manage_bookmarks(state, email, token)

        # --- b: Shift brief — AI priority summary from live queue ------------
        elif choice == "b":
            _site = state.get("site_filter", "US-CENTRAL-07A")
            run_shift_brief(email, token, site=_site)
            try:
                input(f"  {DIM}Press ENTER to return to menu...{RESET}")
            except (EOFError, KeyboardInterrupt):
                pass

        # --- p: Bulk start — put all my open DO tickets In Progress -----------
        elif choice == "p":
            print(f"\n  {DIM}Finding your open DO tickets...{RESET}", end="", flush=True)
            try:
                _pr = _jira_post("/rest/api/3/search/jql", email, token, body={
                    "jql": (
                        'project = "DO" AND assignee = currentUser() '
                        'AND statusCategory != Done '
                        'AND status not in ("In Progress", "Verification")'
                        ' ORDER BY created DESC'
                    ),
                    "maxResults": 30,
                    "fields": ["key", "summary", "status"],
                })
                _p_issues = _pr.json().get("issues", []) if _pr and _pr.ok else []
            except Exception:
                _p_issues = []
            print(f"\r{'':60}\r", end="")

            if not _p_issues:
                print(f"\n  {GREEN}No startable tickets found — you're all set!{RESET}")
                _brief_pause(1.5)
                continue

            print(f"\n  {BOLD}Tickets to start ({len(_p_issues)}):{RESET}")
            for _pi in _p_issues:
                _pf = _pi.get("fields", {})
                _ps = _pf.get("status", {}).get("name", "?")
                _psummary = _pf.get("summary", "")[:55]
                print(f"    {CYAN}{_pi['key']}{RESET}  {DIM}{_ps:<20}{RESET}  {_psummary}")

            try:
                _pconf = input(f"\n  Start all {len(_p_issues)} ticket{'s' if len(_p_issues) != 1 else ''}? [{GREEN}y{RESET}/N]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                continue

            if _pconf != "y":
                print(f"  {DIM}Cancelled.{RESET}")
                _brief_pause()
                continue

            _p_ok = 0
            _p_fail = 0
            for _pi in _p_issues:
                _pctx = {"issue_key": _pi["key"], "_transitions": None}
                print(f"  {DIM}Starting {_pi['key']}...{RESET}", end="", flush=True)
                if _execute_transition(_pctx, "start", email, token):
                    print(f"\r  {GREEN}{BOLD}✓{RESET} {_pi['key']} → In Progress          ")
                    _p_ok += 1
                else:
                    print(f"\r  {YELLOW}✗{RESET} {_pi['key']} — could not start        ")
                    _p_fail += 1

            if _p_ok:
                _log_event("bulk_start", "", "", f"{_p_ok} tickets started")
            print(f"\n  {GREEN}{BOLD}{_p_ok} started{RESET}", end="")
            if _p_fail:
                print(f"  {YELLOW}{_p_fail} failed{RESET}", end="")
            print()
            _brief_pause(1.5)

        # --- l: Activity — session log or Jira changelog ----------------------
        elif choice == "l":
            print(f"\n  {DIM}Activity:{RESET}")
            print(f"    {BOLD}1{RESET} Session log     {DIM}today's actions{RESET}")
            print(f"    {BOLD}2{RESET} Jira activity   {DIM}changelog{RESET}")
            try:
                sub = input("  [1-2] or b to go back: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                continue
            if sub in ("b", "back", ""):
                continue
            elif sub == "2":
                _clear_screen()
                _print_jira_activity(email, token)
            else:
                _show_all = False
                while True:
                    _clear_screen()
                    _print_session_log(show_all=_show_all)
                    try:
                        _lchoice = input("  > ").strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        break
                    if _lchoice == "c":
                        if _copy_session_to_clipboard():
                            print(f"\n  {GREEN}{BOLD}Copied to clipboard!{RESET}")
                        else:
                            print(f"\n  {YELLOW}Could not copy (pbcopy not available).{RESET}")
                        _brief_pause(1)
                    elif _lchoice == "h" and not _show_all:
                        _show_all = True
                    elif _lchoice == "s" and _show_all:
                        _show_all = False
                    elif _lchoice == "f" and ai_available:
                        _ai_work_feedback(show_all=_show_all)
                        input(f"\n  {DIM}Press ENTER to return...{RESET}")
                    else:
                        break
                _clear_screen()

        # --- w: Walkthrough mode (rack-by-rack DH walk with annotations) ------
        elif choice == "w":
            state = _walkthrough_mode(state, email, token)

        elif choice == "??":
            # Toggle compact/full menu mode persistently
            _menu_compact = not _menu_compact

        elif choice in ("?", "h", "help"):
            _clear_screen()
            _print_help()
            input(f"  {DIM}Press ENTER to return to menu...{RESET}")
            _clear_screen()

        else:
            # AI default-on: route unrecognized input to AI chat
            if _AI_ENABLED and ai_available and len(choice) > 1:
                found_key = _ai_dispatch(email=email, token=token, initial_msg=choice)
                if found_key and JIRA_KEY_PATTERN.match(found_key):
                    _act, state = _open_ticket(found_key, email, token, state)
                    if _act == "quit":
                        print(f"\n  {DIM}Goodbye.{RESET}\n")
                        return
            else:
                print(f"\n  {DIM}Invalid choice. Try 1-6, ?, or q.{RESET}")
