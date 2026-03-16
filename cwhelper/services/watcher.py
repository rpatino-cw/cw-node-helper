"""Background queue watcher thread."""
from __future__ import annotations

import queue as queue_mod
import re
import select
import sys
import threading
import time

from cwhelper import config as _cfg
from cwhelper.config import *  # noqa: F401,F403
__all__ = ['_watcher_wait', '_run_queue_watcher', '_background_watcher_loop', '_start_background_watcher', '_stop_background_watcher', '_is_watcher_running', '_drain_new_tickets', '_show_grab_card', '_handle_new_tickets', '_check_radar_link', '_ho_radar_loop', '_start_radar', '_stop_radar', '_is_radar_running', '_drain_radar_tickets', '_show_radar_prep_card', '_handle_radar_tickets', '_infer_procedure']
from cwhelper.clients.jira import _get_credentials, _grab_ticket, _is_mine, _jira_get_issue
from cwhelper.services.search import _search_queue
from cwhelper.services.notifications import _macos_notify, _ntfy_send, _check_stale_unassigned, _check_sla_warnings
from cwhelper.services.weekend import _weekend_auto_assign
from cwhelper.services.context import _build_context, _format_age, _parse_jira_timestamp, _unwrap_field
from cwhelper.tui.display import _status_color, _clear_screen, _print_pretty
from cwhelper.cache import _brief_pause




def _watcher_wait(interval: int, has_new: bool) -> str | None:
    """Wait for `interval` seconds, but let the user type a ticket key anytime.

    Shows a prompt so the user knows they can interact.
    Returns the typed ticket key (e.g. "DO-12345"), or None if timeout/skip.
    """
    if has_new:
        prompt = f"  {DIM}Open a ticket (e.g. DO-12345) or ENTER to continue:{RESET} "
    else:
        prompt = f"  {DIM}Type a ticket key or wait...{RESET} "

    sys.stdout.write(prompt)
    sys.stdout.flush()

    # Use select() to wait for stdin with a timeout (Unix/macOS only)
    ready, _, _ = select.select([sys.stdin], [], [], interval)
    if ready:
        line = sys.stdin.readline().strip().upper()
        if line and (re.match(r"^[A-Z]+-\d+$", line) or line == "RADAR"):
            return line
    else:
        # Timeout — clear the prompt line
        sys.stdout.write("\r" + " " * 80 + "\r")
        sys.stdout.flush()
    return None



def _run_queue_watcher(email: str, token: str, site: str,
                       project: str = "DO", interval: int = 300,
                       auto_assign_group: str = ""):
    """Watch a queue for new tickets. Runs in foreground, Ctrl+C to stop.

    Polls the queue every `interval` seconds. When a ticket appears that
    wasn't in the previous poll, prints it and fires a macOS notification.
    Between polls, the user can type a ticket key to drill into it.

    If auto_assign_group is set and it's a weekend, unassigned new tickets
    are auto-assigned via round-robin before showing grab cards.
    """
    print(f"\n  {BOLD}Watching {project} queue for {site or 'all sites'}{RESET}")
    print(f"  {DIM}Checking every {interval // 60}m {interval % 60}s — Ctrl+C to stop{RESET}")
    if auto_assign_group:
        print(f"  {DIM}Weekend auto-assign: group '{auto_assign_group}'{RESET}")
    print(f"  {DIM}Type a ticket key anytime, or 'radar' for HO dashboard{RESET}")
    print(f"  {DIM}{'─' * 50}{RESET}\n")

    known_keys = set()
    first_run = True

    try:
        while True:
            issues = _search_queue(site, email, token, limit=50,
                                   status_filter="open", project=project,
                                   use_cache=False)

            current_keys = {iss["key"] for iss in issues}
            has_new = False

            if first_run:
                known_keys = current_keys
                ts = time.strftime("%H:%M")
                print(f"  {DIM}[{ts}]{RESET} Initial state: {BOLD}{len(current_keys)}{RESET} open tickets")
                first_run = False
            else:
                new_keys = current_keys - known_keys
                gone_keys = known_keys - current_keys

                if new_keys:
                    has_new = True
                    issue_map = {iss["key"]: iss for iss in issues}

                    # Weekend auto-assignment: assign unassigned new tickets
                    if auto_assign_group and _is_weekend():
                        assigned = _weekend_auto_assign(
                            site, auto_assign_group, email, token,
                            project=project)
                        if assigned:
                            assigned_keys = {a["key"] for a in assigned}
                            for a in assigned:
                                _macos_notify("CW Node Helper",
                                              "Weekend auto-assign",
                                              f"{a['key']} -> {a['assigned_to']}")
                                _ntfy_send("Auto-Assign",
                                           f"{a['key']} assigned",
                                           tags="robot")
                            new_keys = new_keys - assigned_keys

                    # macOS notification
                    count = len(new_keys)
                    first_key = sorted(new_keys)[0]
                    first_iss = issue_map.get(first_key, {})
                    first_tag = _unwrap_field(
                        first_iss.get("fields", {}).get("customfield_10193")) or ""
                    msg = f"{count} new: {first_key} {first_tag}"
                    if count > 1:
                        msg += f" (+{count - 1} more)"
                    _macos_notify("CW Node Helper", f"{site} queue", msg)
                    _ntfy_send("Queue Update",
                               f"{count} new ticket{'s' if count > 1 else ''} in {site} queue",
                               tags="inbox_tray")

                    # Show grab card for each new ticket
                    for key in sorted(new_keys):
                        iss = issue_map.get(key, {})
                        action = _show_grab_card(iss, email, token)
                        if action == "grab":
                            print(f"\n  {DIM}Assigning {key} to you...{RESET}", end="", flush=True)
                            grabbed = _grab_ticket(key, email, token)
                            _cfg._issue_cache.pop(key, None)
                            if grabbed:
                                print(f"\r  {GREEN}{BOLD}Grabbed {key}!{RESET}                    ")
                                try:
                                    follow = input(f"  Press {BOLD}v{RESET} to open ticket, or ENTER to continue watching: ").strip().lower()
                                except (EOFError, KeyboardInterrupt):
                                    follow = ""
                                if follow in ("v", "view"):
                                    ctx = _fetch_and_show(key, email, token)
                                    if ctx:
                                        _clear_screen()
                                        _print_pretty(ctx)
                                        result = _post_detail_prompt(ctx, email, token)
                                        if result == "quit":
                                            return "quit"
                            else:
                                print(f"\r  {YELLOW}Could not grab {key}.{RESET}              ")
                        elif action == "view":
                            ctx = _fetch_and_show(key, email, token)
                            if ctx:
                                _clear_screen()
                                _print_pretty(ctx)
                                result = _post_detail_prompt(ctx, email, token)
                                if result == "quit":
                                    return "quit"
                    print(f"\n  {DIM}Resuming watch...{RESET}")

                if gone_keys:
                    ts = time.strftime("%H:%M")
                    for key in sorted(gone_keys):
                        print(
                            f"  {DIM}GONE [{ts}]  {key} (closed/moved){RESET}"
                        )

                if not new_keys and not gone_keys:
                    ts = time.strftime("%H:%M")
                    print(f"  {DIM}[{ts}] No changes — {len(current_keys)} open{RESET}")

                known_keys = current_keys

            # ntfy.sh: check for stale unassigned + SLA warnings
            _check_stale_unassigned(issues, site)
            _check_sla_warnings(issues, email, token)

            # Wait for next poll — user can type a ticket key or "radar" to open dashboard
            ticket_key = _watcher_wait(interval, has_new)
            if ticket_key == "RADAR":
                from cwhelper.services.radar import _run_radar_interactive
                result = _run_radar_interactive(email, token, site=site)
                if result == "quit":
                    print(f"\n  {DIM}Goodbye.{RESET}\n")
                    return "quit"
                print(f"\n  {DIM}Resuming watch...{RESET}\n")
                continue
            if ticket_key:
                print(f"\n  Fetching {ticket_key}...\n")
                ctx = _fetch_and_show(ticket_key, email, token)
                if ctx:
                    _clear_screen()
                    _print_pretty(ctx)
                    action = _post_detail_prompt(ctx, email, token)
                    if action == "quit":
                        print(f"\n  {DIM}Goodbye.{RESET}\n")
                        return "quit"
                    while action == "history":
                        tag = ctx.get("service_tag") or ctx.get("hostname")
                        if not tag:
                            break
                        h_action = _run_history_interactive(email, token, tag)
                        if h_action == "quit":
                            print(f"\n  {DIM}Goodbye.{RESET}\n")
                            return "quit"
                        _clear_screen()
                        _print_pretty(ctx)
                        action = _post_detail_prompt(ctx, email, token)
                print(f"\n  {DIM}Resuming watch...{RESET}\n")

    except KeyboardInterrupt:
        print(f"\n\n  {DIM}Watcher stopped.{RESET}\n")
        return "back"



def _background_watcher_loop(email: str, token: str, site: str,
                              project: str, interval: int,
                              stop_event: threading.Event,
                              notify_q: queue_mod.Queue,
                              auto_assign_group: str = ""):
    """Daemon thread: polls the queue and pushes new ticket dicts to notify_q."""
    known_keys: set[str] = set()
    first_run = True

    while not stop_event.is_set():
        try:
            issues = _search_queue(site, email, token, limit=50,
                                   status_filter="open", project=project,
                                   use_cache=False)
            current_keys = {iss["key"] for iss in issues}

            if first_run:
                known_keys = current_keys
                first_run = False
            else:
                new_keys = current_keys - known_keys
                if new_keys:
                    # Weekend auto-assignment
                    if auto_assign_group and _is_weekend():
                        _weekend_auto_assign(site, auto_assign_group,
                                             email, token, project=project)

                    issue_map = {iss["key"]: iss for iss in issues}
                    for key in sorted(new_keys):
                        iss = issue_map.get(key)
                        if iss:
                            # Check if this DO links to a tracked radar HO
                            _check_radar_link(iss, email, token)
                            notify_q.put(iss)
                            # macOS notification
                            f = iss.get("fields", {})
                            tag = _unwrap_field(f.get("customfield_10193")) or ""
                            summary = f.get("summary", "")[:50]
                            _macos_notify("CW Node Helper",
                                          f"New {project} ticket",
                                          f"{key} {tag} {summary}")
                            _ntfy_send(f"New {project} Ticket",
                                       f"{key} ({site})" if site else key,
                                       priority="high",
                                       tags="rotating_light")
                known_keys = current_keys

            # ntfy.sh: check for stale unassigned + SLA warnings
            _check_stale_unassigned(issues, site)
            _check_sla_warnings(issues, email, token)
        except Exception:
            pass  # silently retry next interval

        # Wait for the interval (or until stop is signaled)
        stop_event.wait(interval)
        if stop_event.is_set():
            return



def _start_background_watcher(email: str, token: str, site: str,
                               project: str = "DO", interval: int = 45,
                               auto_assign_group: str = ""):
    """Start the background queue watcher. No-op if already running."""
    # Watcher state lives in _cfg (no global needed)

    if _cfg._watcher_thread and _cfg._watcher_thread.is_alive():
        return False  # already running

    _cfg._watcher_stop_event.clear()
    # Drain any old items from the queue
    while not _cfg._watcher_queue.empty():
        try:
            _cfg._watcher_queue.get_nowait()
        except queue_mod.Empty:
            break

    _cfg._watcher_site = site
    _cfg._watcher_project = project
    _cfg._watcher_interval = interval

    _cfg._watcher_thread = threading.Thread(
        target=_background_watcher_loop,
        args=(email, token, site, project, interval,
              _cfg._watcher_stop_event, _cfg._watcher_queue, auto_assign_group),
        daemon=True,
    )
    _cfg._watcher_thread.start()

    # Auto-start radar alongside the DO watcher (same site)
    _start_radar(email, token, site)

    return True



def _stop_background_watcher():
    """Signal the background watcher to stop."""
    # Stop radar first
    _stop_radar()
    # Watcher state lives in _cfg
    _cfg._watcher_stop_event.set()
    if _cfg._watcher_thread:
        _cfg._watcher_thread.join(timeout=3)
        _cfg._watcher_thread = None



def _is_watcher_running() -> bool:
    """Check if the background watcher is alive."""
    return _cfg._watcher_thread is not None and _cfg._watcher_thread.is_alive()



def _drain_new_tickets() -> list[dict]:
    """Non-blocking: pull all pending new-ticket notifications from the queue."""
    tickets = []
    while True:
        try:
            tickets.append(_cfg._watcher_queue.get_nowait())
        except queue_mod.Empty:
            break
    return tickets



def _show_grab_card(issue: dict, email: str, token: str) -> str:
    """Display an inline notification card for a new ticket.

    If the issue has '_radar_ho' data attached (from _check_radar_link),
    shows an enhanced card with HO context and procedure info.

    Returns "grab", "view", or "skip".
    """
    f = issue.get("fields", {})
    key = issue.get("key", "?")
    tag = _unwrap_field(f.get("customfield_10193")) or "—"
    status = f.get("status", {}).get("name", "?")
    sc, sd = _status_color(status)
    summary = f.get("summary", "")[:60]
    rack = _unwrap_field(f.get("customfield_10207")) or ""
    assignee = f.get("assignee", {}).get("displayName") if f.get("assignee") else None
    radar_ho = issue.get("_radar_ho")

    print()
    if radar_ho:
        label = f"NEW TICKET (EXPECTED)"
        print(f"  {GREEN}{BOLD}┌─ {label} ─────────────────────────────┐{RESET}")
    else:
        print(f"  {GREEN}{BOLD}┌─ NEW TICKET ──────────────────────────────────────┐{RESET}")
    print(f"  {GREEN}{BOLD}│{RESET}  {BOLD}{key}{RESET}  {sc}{sd} {status}{RESET}   {CYAN}{tag}{RESET}  {DIM}{rack}{RESET}")
    print(f"  {GREEN}{BOLD}│{RESET}  {summary}")
    if assignee:
        print(f"  {GREEN}{BOLD}│{RESET}  {DIM}Assigned: {assignee}{RESET}")
    else:
        print(f"  {GREEN}{BOLD}│{RESET}  {DIM}Unassigned{RESET}")

    # Radar HO context (Feature 5)
    if radar_ho:
        ho_key = radar_ho.get("ho_key", "?")
        proc = radar_ho.get("procedure", "?")
        hint = radar_ho.get("hint", "")
        print(f"  {GREEN}{BOLD}│{RESET}")
        print(f"  {GREEN}{BOLD}│{RESET}  {YELLOW}{BOLD}Linked to {ho_key}{RESET}  {DIM}({hint}){RESET}")
        print(f"  {GREEN}{BOLD}│{RESET}  {CYAN}Procedure:{RESET} {proc}")

    print(f"  {GREEN}{BOLD}│{RESET}")
    print(f"  {GREEN}{BOLD}│{RESET}  {BOLD}[g]{RESET} Grab   {BOLD}[v]{RESET} View details   {BOLD}[s]{RESET} Skip")
    print(f"  {GREEN}{BOLD}└───────────────────────────────────────────────────┘{RESET}")

    while True:
        try:
            choice = input(f"  {BOLD}>{RESET} ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return "skip"
        if choice in ("g", "grab"):
            return "grab"
        if choice in ("v", "view"):
            return "view"
        if choice in ("s", "skip", ""):
            return "skip"



def _handle_new_tickets(email: str, token: str) -> str | None:
    """Process any pending new-ticket notifications.

    Returns "quit" if the user quits from a detail view, else None.
    """
    tickets = _drain_new_tickets()
    for issue in tickets:
        key = issue.get("key", "?")
        action = _show_grab_card(issue, email, token)

        if action == "grab":
            print(f"\n  {DIM}Assigning {key} to you...{RESET}", end="", flush=True)
            grabbed = _grab_ticket(key, email, token)
            # Invalidate cache so next view shows updated assignee
            _cfg._issue_cache.pop(key, None)
            if grabbed:
                print(f"\r  {GREEN}{BOLD}Grabbed {key}!{RESET}                    ")
                try:
                    follow = input(f"  Press {BOLD}v{RESET} to open ticket, or ENTER to continue watching: ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    follow = ""
                if follow in ("v", "view"):
                    ctx = _fetch_and_show(key, email, token)
                    if ctx:
                        _clear_screen()
                        _print_pretty(ctx)
                        result = _post_detail_prompt(ctx, email, token)
                        if result == "quit":
                            return "quit"
            else:
                print(f"\r  {YELLOW}Could not grab {key}.{RESET}              ")

        elif action == "view":
            ctx = _fetch_and_show(key, email, token)
            if ctx:
                _clear_screen()
                _print_pretty(ctx)
                result = _post_detail_prompt(ctx, email, token)
                if result == "quit":
                    return "quit"

    return None


# ---------------------------------------------------------------------------
# Smart grab card — link new DOs to tracked radar HOs
# ---------------------------------------------------------------------------

def _check_radar_link(issue: dict, email: str, token: str) -> None:
    """Check if a new DO issue links to a tracked radar HO.

    If matched, attaches '_radar_ho' key to the issue dict with the
    linked HO key and procedure info. This is picked up by _show_grab_card.
    """
    if not _cfg._radar_known_keys:
        return

    key = issue.get("key", "")
    if not key.startswith("DO"):
        return

    try:
        # Fetch full issue to get issuelinks (queue search doesn't include them)
        full = _jira_get_issue(key, email, token)
        if not full:
            return
        links = full.get("fields", {}).get("issuelinks", [])
        for link in links:
            for direction in ("inwardIssue", "outwardIssue"):
                linked = link.get(direction)
                if linked:
                    linked_key = linked.get("key", "")
                    if linked_key in _cfg._radar_known_keys:
                        ho_iss = _cfg._radar_known_keys[linked_key]
                        ho_f = ho_iss.get("fields", {})
                        ho_status = (ho_f.get("status") or {}).get("name", "?")
                        proc, hint = _infer_procedure(ho_status)
                        issue["_radar_ho"] = {
                            "ho_key": linked_key,
                            "procedure": proc,
                            "hint": hint,
                            "rack": _unwrap_field(ho_f.get("customfield_10207")) or "",
                        }
                        return
    except Exception:
        pass


# ---------------------------------------------------------------------------
# HO Radar — background thread for pre-DO awareness
# ---------------------------------------------------------------------------

_RADAR_STATUS_HINTS = {
    "sent to dct uc": ("Uncable", "Uncable DO imminent"),
    "sent to dct rc": ("Recable", "Recable DO imminent"),
    "rma-initiate":   ("RMA Swap", "RMA swap DO coming soon"),
    "awaiting parts": ("Parts", "DO when parts arrive"),
}


def _infer_procedure(status_name: str) -> tuple[str, str]:
    """Return (procedure_type, eta_hint) from an HO radar status."""
    return _RADAR_STATUS_HINTS.get(status_name.lower(), ("Unknown", "DO pending"))


def _ho_radar_loop(email: str, token: str, site: str,
                   interval: int, stop_event: threading.Event,
                   notify_q: queue_mod.Queue):
    """Daemon thread: polls HO tickets in pre-DO statuses and pushes new ones to notify_q."""
    first_run = True

    while not stop_event.is_set():
        try:
            issues = _search_queue(site, email, token, limit=50,
                                   status_filter="radar", project="HO",
                                   use_cache=False)
            current_map = {iss["key"]: iss for iss in issues}
            current_keys = set(current_map.keys())

            if first_run:
                # Seed known keys — don't alert on existing tickets
                _cfg._radar_known_keys = {k: iss for k, iss in current_map.items()}
                first_run = False
            else:
                new_keys = current_keys - set(_cfg._radar_known_keys.keys())
                if new_keys:
                    for key in sorted(new_keys):
                        iss = current_map[key]
                        notify_q.put(iss)
                        # Push notification
                        f = iss.get("fields", {})
                        status = (f.get("status") or {}).get("name", "?")
                        rack = _unwrap_field(f.get("customfield_10207")) or ""
                        tag = _unwrap_field(f.get("customfield_10193")) or ""
                        proc, hint = _infer_procedure(status)
                        _macos_notify("HO Radar",
                                      f"{proc} incoming",
                                      f"{key} {tag} {rack}")
                        _ntfy_send(f"Radar: {proc}",
                                   f"{key} {rack} — {hint}",
                                   priority="default",
                                   tags="satellite")

                # Update known keys (track all current, remove gone ones)
                _cfg._radar_known_keys = {k: iss for k, iss in current_map.items()}
        except Exception:
            pass  # silently retry next interval

        stop_event.wait(interval)
        if stop_event.is_set():
            return


def _start_radar(email: str, token: str, site: str,
                 interval: int = 120):
    """Start the HO radar watcher. No-op if already running."""
    if _cfg._radar_thread and _cfg._radar_thread.is_alive():
        return False

    _cfg._radar_stop_event.clear()
    # Drain old items
    while not _cfg._radar_queue.empty():
        try:
            _cfg._radar_queue.get_nowait()
        except queue_mod.Empty:
            break

    _cfg._radar_interval = interval

    _cfg._radar_thread = threading.Thread(
        target=_ho_radar_loop,
        args=(email, token, site, interval,
              _cfg._radar_stop_event, _cfg._radar_queue),
        daemon=True,
    )
    _cfg._radar_thread.start()
    return True


def _stop_radar():
    """Signal the HO radar watcher to stop."""
    _cfg._radar_stop_event.set()
    if _cfg._radar_thread:
        _cfg._radar_thread.join(timeout=3)
        _cfg._radar_thread = None


def _is_radar_running() -> bool:
    """Check if the HO radar watcher is alive."""
    return _cfg._radar_thread is not None and _cfg._radar_thread.is_alive()


def _drain_radar_tickets() -> list[dict]:
    """Non-blocking: pull all pending radar HO notifications from the queue."""
    tickets = []
    while True:
        try:
            tickets.append(_cfg._radar_queue.get_nowait())
        except queue_mod.Empty:
            break
    return tickets


def _show_radar_prep_card(issue: dict) -> str:
    """Display a radar prep card for an HO ticket in pre-DO status.

    Unlike grab cards, prep cards are informational — no grab action.
    Returns "view" or "skip".
    """
    f = issue.get("fields", {})
    key = issue.get("key", "?")
    tag = _unwrap_field(f.get("customfield_10193")) or "—"
    status = (f.get("status") or {}).get("name", "?")
    sc, sd = _status_color(status)
    summary = (f.get("summary") or "")[:60]
    rack = _unwrap_field(f.get("customfield_10207")) or ""
    proc, hint = _infer_procedure(status)

    print()
    print(f"  {YELLOW}{BOLD}┌─ RADAR ── incoming {proc} ─────────────────────────┐{RESET}")
    print(f"  {YELLOW}{BOLD}│{RESET}  {BOLD}{key}{RESET}  {sc}{sd} {status}{RESET}   {CYAN}{tag}{RESET}  {DIM}{rack}{RESET}")
    print(f"  {YELLOW}{BOLD}│{RESET}  {summary}")
    print(f"  {YELLOW}{BOLD}│{RESET}  {DIM}{hint}{RESET}")
    print(f"  {YELLOW}{BOLD}│{RESET}")
    print(f"  {YELLOW}{BOLD}│{RESET}  {BOLD}[v]{RESET} View HO details   {BOLD}[s]{RESET} Skip")
    print(f"  {YELLOW}{BOLD}└───────────────────────────────────────────────────┘{RESET}")

    while True:
        try:
            choice = input(f"  {BOLD}>{RESET} ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return "skip"
        if choice in ("v", "view"):
            return "view"
        if choice in ("s", "skip", ""):
            return "skip"


def _handle_radar_tickets(email: str, token: str) -> str | None:
    """Process any pending radar HO notifications.

    Returns "quit" if user quits from a detail view, else None.
    """
    tickets = _drain_radar_tickets()
    for issue in tickets:
        key = issue.get("key", "?")
        action = _show_radar_prep_card(issue)

        if action == "view":
            ctx = _fetch_and_show(key, email, token)
            if ctx:
                _clear_screen()
                _print_pretty(ctx)
                result = _post_detail_prompt(ctx, email, token)
                if result == "quit":
                    return "quit"

    return None

