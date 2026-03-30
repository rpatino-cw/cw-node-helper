"""Action panel and detail view hotkeys."""
from __future__ import annotations

import html as html_mod
import html.parser
import os
import re
import subprocess
import tempfile
import textwrap
import time
import webbrowser
from urllib.parse import quote as _url_quote

from cwhelper import config as _cfg
from cwhelper.config import (
    BOLD, DIM, RESET, RED, GREEN, YELLOW, CYAN, WHITE, MAGENTA, BLUE,
    JIRA_BASE_URL, JIRA_KEY_PATTERN,
)
__all__ = ['_print_action_panel', '_post_detail_prompt', '_run_cab_view']
from cwhelper.tui.rack_helpers import (
    _find_related_tickets, _hold_ticket_by_key,
    _check_rack_tickets, _show_rack_suggestions,
)
from cwhelper.tui.cab_view import _run_cab_view
from cwhelper.tui.connection_view import (
    _print_connections_inline, _find_linked_ho, _summarize_ho_for_dct,
    _show_mrb_for_node, _show_sdx_for_ticket, _trace_connection,
)
from cwhelper.tui.display import (
    _clear_screen, _print_pretty, _print_banner, _print_help, _status_color,
    _print_linked_inline, _print_diagnostics_inline, _print_sla_detail, _prompt_select,
)
from cwhelper.services.notifications import _ntfy_send
from cwhelper.services.weekend import _weekend_auto_assign
from cwhelper.services.walkthrough import _walkthrough_mode
from cwhelper.services.watcher import _start_background_watcher, _stop_background_watcher, _is_watcher_running, _handle_new_tickets, _drain_new_tickets
from cwhelper.services.queue import _run_history_interactive, _run_queue_interactive, _run_stale_verification, _search_node_history
from cwhelper.services.bookmarks import _manage_bookmarks, _add_bookmark_wizard, _remove_bookmark_wizard, _rename_bookmark_wizard, _build_bookmark_suggestions
from cwhelper.clients.grafana import _find_psu_dashboard_url
from cwhelper.services.rack import _handle_rack_view, _handle_rack_neighbors, _draw_mini_dh_map, _draw_rack_elevation
from cwhelper.services.ai import _ai_available, _ai_dispatch, _ai_chat_loop, _ai_find_ticket, _ai_summarize, _suggest_comments, _pick_or_type_comment
from cwhelper.clients.jira import _is_mine, _refresh_ctx, _post_comment, _upload_attachment, _grab_ticket, _assign_ticket, _jira_link_issues, _get_existing_links, _execute_transition, _get_my_account_id, _jira_get, _jira_put, _jira_get_issue, _text_to_adf, _get_credentials, _fetch_site_teammates, _jira_user_search
from cwhelper.clients.netbox import _netbox_find_rack_by_name
from cwhelper.clients.fleet import _cwctl_available, _cwctl_rack_blockers
from cwhelper.services.search import _jql_search
from cwhelper.state import _load_user_state, _save_user_state, _record_ticket_view, _add_bookmark, _remove_bookmark
from cwhelper.cache import _brief_pause, _escape_jql
from cwhelper.services.context import _build_context, _parse_rack_location, _extract_comments, _adf_to_plain_text, _render_adf_description, _format_age, _fetch_and_show
from cwhelper.services.session_log import _log_event
import datetime as _dt





class _FleetDiagLinkParser(html.parser.HTMLParser):
    """Parse log file links from a Fleet Diags HTML index page."""

    def __init__(self, index_url: str):
        super().__init__()
        self._index_url = index_url
        self.links: list[dict] = []
        self._cur_row: dict = {}
        self._in_td: bool = False
        self._td_count: int = 0

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._cur_row = {}
            self._td_count = 0
        elif tag == "td":
            self._in_td = True
            self._td_count += 1
        elif tag == "a":
            for k, v in attrs:
                if k == "href" and v and v != self._index_url:
                    self._cur_row["url"] = v

    def handle_data(self, data):
        if self._in_td:
            d = data.strip()
            if self._td_count == 1 and d:
                self._cur_row["time"] = d
            elif self._td_count == 2 and d:
                self._cur_row["mode"] = d

    def handle_endtag(self, tag):
        if tag == "td":
            self._in_td = False
        elif tag == "tr" and self._cur_row.get("url"):
            self.links.append(dict(self._cur_row))


def _is_ticket_mine(ctx: dict) -> bool:
    """Return True if the current user is the assignee of ctx.

    Checks in order: display name → account ID → email-derived name fallback.
    """
    current_assignee = ctx.get("assignee")
    if not current_assignee:
        return False
    _dn = _cfg._my_display_name
    _aid = _cfg._my_account_id
    if _dn and _dn.lower() == current_assignee.lower():
        return True
    if _aid and ctx.get("_assignee_account_id") == _aid:
        return True
    my_email = os.environ.get("JIRA_EMAIL", "")
    my_name = " ".join(w.capitalize() for w in my_email.split("@")[0].split("."))
    return bool(my_name and my_name.lower() == current_assignee.lower())


def _pick_teammate(site: str, email: str, token: str, prompt: str) -> "dict | None":
    """Interactive teammate picker used by hand-off and cab-give handlers.
    Returns the selected user dict, or None if the user cancelled."""
    print(f"\n  {DIM}Loading teammates...{RESET}", end="", flush=True)
    teammates = _fetch_site_teammates(site, email, token)
    print(f"\r{'':50}\r", end="")

    if teammates:
        print(f"\n  {BOLD}{prompt}{RESET}")
        for i, t in enumerate(teammates[:8], 1):
            print(f"    {BOLD}{i}{RESET}  {t['name']}")
        print(f"\n  {DIM}Pick [1-{min(len(teammates), 8)}], type a name to search, or b to cancel:{RESET}")
    else:
        print(f"\n  {DIM}Type a name to search for a teammate, or b to cancel:{RESET}")

    try:
        raw = input("  > ").strip()
    except (EOFError, KeyboardInterrupt):
        return None

    if not raw or raw.lower() in ("b", "back"):
        return None

    selected: dict | None = None
    try:
        idx = int(raw)
        if teammates and 1 <= idx <= min(len(teammates), 8):
            selected = teammates[idx - 1]
    except ValueError:
        pass

    if not selected:
        print(f"  {DIM}Searching...{RESET}", end="", flush=True)
        results = _jira_user_search(raw, email, token)
        print(f"\r{'':40}\r", end="")
        if not results:
            print(f"  {YELLOW}No users found for '{raw}'.{RESET}")
            _brief_pause(1.5)
            return None
        if len(results) == 1:
            selected = results[0]
        else:
            print(f"\n  {DIM}Search results:{RESET}")
            for i, r in enumerate(results, 1):
                print(f"    {BOLD}{i}{RESET}  {r['name']}")
            try:
                pick = input(f"  Pick [1-{len(results)}] or b to cancel: ").strip()
            except (EOFError, KeyboardInterrupt):
                return None
            if not pick or pick.lower() in ("b", "back"):
                return None
            try:
                pidx = int(pick)
                if 1 <= pidx <= len(results):
                    selected = results[pidx - 1]
            except ValueError:
                pass

    return selected


def _print_action_panel(ctx: dict, state: dict = None):
    """Render the color-coded action button panel below ticket info."""
    line = "\u2500" * 50

    def btn(key_char, label, color):
        """Render a single button:  [x] Label  with colored bold bracket."""
        return f"{color}{BOLD}[{key_char}]{RESET} {WHITE}{label}{RESET}"

    is_ticket = ctx.get("source") != "netbox"
    netbox = ctx.get("netbox", {})
    show_more = ctx.get("_show_more_actions", False)
    mine = _is_mine(ctx)
    status_lower = ctx.get("status", "").lower()
    has_node_id = ctx.get("service_tag") or ctx.get("hostname")

    print(f"  {DIM}{line}{RESET}")
    print()

    # ── STATUS ACTIONS (most important — what can I do right now?) ──
    status_items = []
    if is_ticket:
        # Assignee action
        current_assignee = ctx.get("assignee")
        already_mine = _is_ticket_mine(ctx)
        if already_mine:
            status_items.append(btn("a", "Unassign", BLUE))
            status_items.append(btn("h", "Hand off", CYAN))
        elif current_assignee:
            status_items.append(btn("a", f"Take from {current_assignee}", YELLOW))
        else:
            status_items.append(btn("a", "Grab", GREEN))

        # Transitions
        unassigned = not ctx.get("assignee")
        if status_lower in ("to do", "new", "open", "waiting for triage",
                            "awaiting triage", "awaiting support",
                            "reopened") and (mine or unassigned):
            status_items.append(btn("s", "Start Work", GREEN))
        if status_lower == "in progress" and mine:
            status_items.append(btn("v", "Verification", BLUE))
            status_items.append(btn("y", "On Hold", YELLOW))
            if ctx.get("rack_location"):
                status_items.append(btn("hc", "Hold cab", YELLOW))
        if status_lower in ("on hold", "blocked", "paused",
                            "waiting for support", "awaiting support") and mine:
            status_items.append(btn("z", "Resume", CYAN))
        if status_lower == "verification" and mine:
            status_items.append(btn("z", "Back to In Progress", CYAN))
            status_items.append(btn("k", "Close Ticket", RED))
        if status_lower in ("closed", "done", "resolved") and mine:
            status_items.append(btn("vv", "Back to Verification", BLUE))

        # Cab-level bulk actions — always available when there's a rack location
        if ctx.get("rack_location"):
            status_items.append(btn("hg", "Give cab", CYAN))
            status_items.append(btn("lg", "Link cab", MAGENTA))

    if status_items:
        print(f"  {BOLD}{WHITE}Actions{RESET}")
        print(f"    {'   '.join(status_items)}")
        print()

    # ── VIEW (inline data views — always shown) ──
    view_items = []
    if ctx.get("rack_location"):
        view_items.append(btn("r", "Rack Map", CYAN))
    if netbox and netbox.get("interfaces"):
        view_items.append(btn("n", "Connections", MAGENTA))
    _cc = ctx.get("_comment_count") or len(ctx.get("comments") or [])
    cmt_label = "Close Comments" if ctx.get("_show_comments") else f"Comments ({_cc})"
    view_items.append(btn("c", cmt_label, GREEN))
    if ctx.get("description_text"):
        desc_label = "Close Description" if ctx.get("_show_desc") else "Description"
        view_items.append(btn("w", desc_label, WHITE))
    if ctx.get("diag_links"):
        diag_label = "Close Diags" if ctx.get("_show_diags") else "Diags"
        view_items.append(btn("d", diag_label, BLUE))
    if _cwctl_available():
        bl_label = "Close Blockers" if ctx.get("_show_blockers") else "Blockers"
        view_items.append(btn("bl", bl_label, RED))
    view_items.append(btn("img", "Attach Screenshot", MAGENTA))
    if has_node_id:
        view_items.append(btn("vr", "Verify", GREEN))
    if view_items:
        print(f"  {BOLD}{WHITE}View{RESET}")
        print(f"    {'   '.join(view_items)}")
        print()

    # ── OPEN (external links — always shown) ──
    open_items = []
    if is_ticket:
        open_items.append(btn("j", "Jira", CYAN))
    if ctx.get("grafana", {}).get("node_details"):
        open_items.append(btn("g", "Grafana", GREEN))
    if netbox and netbox.get("device_id"):
        open_items.append(btn("x", "NetBox", YELLOW))
    if netbox and netbox.get("snipe_url"):
        open_items.append(btn("si", "Snipe-IT", YELLOW))
    if netbox and netbox.get("device_name") and netbox.get("site_slug"):
        open_items.append(btn("t", "BMC", MAGENTA))
    if ctx.get("service_tag"):
        open_items.append(btn("fd", "Fleet Diags", YELLOW))
    if open_items:
        print(f"  {BOLD}{WHITE}Open{RESET}")
        print(f"    {'   '.join(open_items)}")
        print()

    # ── MORE (toggle with [+] — extra views, links, bookmark) ──
    more_items = []
    _att = ctx.get("attachments", [])
    if _att:
        more_items.append(btn("at", f"Attachments ({len(_att)})", MAGENTA))
    if ctx.get("linked_issues"):
        more_items.append(btn("l", "Linked", YELLOW))
    if (netbox and netbox.get("rack_id")) or ctx.get("rack_location"):
        more_items.append(btn("e", "Rack View", YELLOW))
    if ctx.get("_mrb_count", 0) > 0:
        more_items.append(btn("f", f"MRB ({ctx['_mrb_count']})", YELLOW))
    if is_ticket:
        sla_label = "Close SLA" if ctx.get("_show_sla") else "SLA"
        more_items.append(btn("u", sla_label, RED))
    if is_ticket and ctx.get("_portal_url"):
        more_items.append(btn("p", "Portal", CYAN))
    if ctx.get("grafana", {}).get("ib_node_search"):
        more_items.append(btn("i", "IB", GREEN))
    if ctx.get("ho_context"):
        more_items.append(btn("o", f"View {ctx['ho_context']['key']}", MAGENTA))
    if ctx.get("psu_info"):
        more_items.append(btn("pg", "PSU Dashboard", YELLOW))
    # Bookmark
    if is_ticket:
        _bm_key = ctx.get("issue_key", "")
        _is_bookmarked = any(
            b.get("type") == "ticket" and b.get("params", {}).get("key") == _bm_key
            for b in (state or {}).get("bookmarks", [])
        )
        if _is_bookmarked:
            more_items.append(btn("*", "Remove Bookmark", RED))
        else:
            more_items.append(btn("*", "Bookmark", YELLOW))

    # ── RELATED TICKETS (same node, created ~same time) ──
    _related = ctx.get("_related_tickets")
    if _related:
        _rel_keys = "  ".join(f"{CYAN}{r['key']}{RESET}" for r in _related[:6])
        print(f"  {YELLOW}{BOLD}⚡ {len(_related)} related ticket{'s' if len(_related) != 1 else ''} — same node, created ~same time{RESET}")
        print(f"    {_rel_keys}")
        # Only show [ra] if there are unassigned related tickets left to grab
        _rel_unassigned = [r for r in _related if not (r.get("fields") or {}).get("assignee")]
        _ra_hint = f"   [ra] grab all ({len(_rel_unassigned)})" if _rel_unassigned else ""
        print(f"    {DIM}[rel] reference in comment   [ro] open all in Jira{_ra_hint}   [rl] link all{RESET}")
        print()

    if more_items:
        if show_more:
            print(f"  {BOLD}{WHITE}More{RESET}  {DIM}(press {BOLD}>{RESET}{DIM} to hide){RESET}")
            print(f"    {'   '.join(more_items)}")
            print()
        else:
            print(f"  {DIM}[{BOLD}>{RESET}{DIM}] More options ({len(more_items)} hidden){RESET}")
            print()

    # ── NAV (hidden by default — press ? to show) ──
    nav_items = [btn("b", "Back", DIM), btn("m", "Menu", DIM)]
    if has_node_id:
        nav_items.append(btn("hn", "History", DIM))
    if is_ticket:
        nav_items.append(btn("=", "Refresh", CYAN))
    nav_items.append(btn("q", "Quit", DIM))
    if _ai_available():
        nav_items.append(btn("ai", "AI", CYAN))
    if ctx.get("_show_nav"):
        print(f"  {DIM}Nav{RESET}  {DIM}(press {BOLD}?{RESET}{DIM} to hide){RESET}")
        print(f"    {'   '.join(nav_items)}")
    else:
        print(f"  {DIM}[{BOLD}?{RESET}{DIM}] Commands{RESET}")
    print()

    # --- Next step (actionable guidance with hotkey — only when you own the ticket) ---
    if is_ticket:
        summary_lower = (ctx.get("summary") or "").lower()
        ho = ctx.get("ho_context")
        ho_hint = (ho.get("hint", "") if ho else "").lower()
        age = ctx.get("status_age_seconds", 0)
        step = ""
        if status_lower in ("to do", "new", "awaiting support", "awaiting triage"):
            if not ctx.get("assignee"):
                step = "Unassigned — press [a] to grab, then [s] to start"
            elif mine:
                step = "Press [s] to start work on this ticket"
        elif status_lower == "in progress" and mine:
            if "recable" in summary_lower or "recable" in ho_hint:
                step = "Recable node, comment what you did, [vr] verify → [v] to move"
            elif "power_cycle" in summary_lower.replace(" ", "_"):
                step = "Power cycle done? [vr] verify node is up → [v] to move"
            elif "reseat" in summary_lower:
                step = "Reseat done? [vr] verify health → [v] to move"
            elif "swap" in summary_lower:
                step = "Swap done? Note old/new serials, [vr] verify → [v] to move"
            elif "cable" in summary_lower:
                step = "Cable replaced? [vr] verify → [v] to move"
            else:
                step = "Done? [vr] verify node → [v] to move — not done? [y] for hold"
        elif status_lower == "verification" and mine:
            if age > 48 * 3600:
                step = f"STALE ({_format_age(age)}) — press [k] to close or [z] to reopen"
            else:
                step = "Waiting for requester — press [k] to close when confirmed"
        elif status_lower in ("on hold", "blocked", "paused", "waiting for support") and mine:
            step = "Press [z] to resume when ready to continue"
        elif status_lower == "closed":
            step = "Closed — press [hn] for node history if more work needed"
        if step:
            color = RED if "STALE" in step else CYAN
            print(f"  {color}{BOLD}Next:{RESET} {step}")
    print()



def _post_detail_prompt(ctx: dict = None, email: str = None, token: str = None,
                        state: dict = None) -> str:
    """After viewing a ticket detail, ask what to do next.
    Returns "back", "menu", "quit", or "history".
    Also handles opening URLs in browser and inline display via hotkeys."""
    has_node_id = ctx and (ctx.get("service_tag") or ctx.get("hostname"))
    has_grafana = ctx and ctx.get("grafana", {}).get("node_details")

    # Kick off related-ticket search in background on first open
    _related_future = None
    if ctx and "_related_tickets" not in ctx and ctx.get("service_tag") and email and token:
        _related_future = _cfg._executor.submit(
            _find_related_tickets, ctx, email, token)

    while True:
        # Auto-refresh ctx if stale (>45s since last fetch)
        if ctx and email and token and time.time() - ctx.get("_fetched_at", 0) > 45:
            _refresh_ctx(ctx, email, token)

        # Collect related-ticket result (non-blocking)
        if _related_future and _related_future.done():
            try:
                ctx["_related_tickets"] = _related_future.result() or []
            except Exception:
                ctx["_related_tickets"] = []
            _related_future = None

        # Render the action panel
        if ctx:
            _print_action_panel(ctx, state=state)

        try:
            choice = input("  > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return "quit"

        # --- Add comment ---
        if choice == "+" and ctx and ctx.get("source") != "netbox" and email and token:
            key = ctx.get("issue_key", "?")
            print(f"\n  {BOLD}Add comment to {key}{RESET}")
            print(f"  {DIM}Type your comment below (multi-line: end with an empty line).{RESET}")
            print(f"  {DIM}Press ENTER twice to submit, or type 'cancel' to abort.{RESET}\n")
            lines = []
            try:
                while True:
                    line = input(f"  {DIM}>{RESET} ")
                    if line.strip().lower() == "cancel":
                        lines = []
                        break
                    if line == "" and lines:
                        break
                    lines.append(line)
            except (EOFError, KeyboardInterrupt):
                lines = []
            if lines:
                comment_text = "\n".join(lines)
                print(f"\n  {DIM}Posting comment...{RESET}", end="", flush=True)
                if _post_comment(key, comment_text, email, token):
                    print(f"\r  {GREEN}{BOLD}Comment posted!{RESET}                    ")
                    _log_event("comment", key, ctx.get("summary", ""), comment_text[:60], ctx=ctx)
                    # Invalidate cache and refresh comments
                    _cfg._issue_cache.pop(key, None)
                    _refresh_ctx(ctx, email, token)
                    ctx["_show_comments"] = True
                else:
                    print(f"\r  {YELLOW}Failed to post comment.{RESET}              ")
            else:
                print(f"  {DIM}Cancelled.{RESET}")
            _brief_pause()
            _clear_screen()
            _print_pretty(ctx)
            if ctx.get("_show_comments") and ctx.get("comments"):
                print(f"\n  {BOLD}Comments{RESET}")
                for c in ctx["comments"]:
                    print(f"    {DIM}{c['created']}{RESET}  {MAGENTA}{c['author']}{RESET}")
                    if c["body"]:
                        print(f"      {c['body']}")
                print(f"\n    {DIM}Press {RESET}{GREEN}{BOLD}+{RESET}{DIM} to add a comment{RESET}")
            continue

        # --- Toggle nav commands ---
        if choice == "?":
            ctx["_show_nav"] = not ctx.get("_show_nav", False)
            _clear_screen()
            _print_pretty(ctx)
            continue

        # --- Navigation ---
        if choice in ("q", "quit", "exit"):
            return "quit"
        if choice in ("m", "menu"):
            return "menu"
        if choice in ("hn", "history") and has_node_id:
            return "history"
        if choice in ("b", "back"):
            return "back"

        # --- Related tickets: reference in comment ---
        if choice == "rel" and ctx and email and token:
            _related = ctx.get("_related_tickets") or []
            if not _related:
                print(f"\n  {DIM}No related tickets found within 15 min of this ticket.{RESET}")
                _brief_pause()
                _clear_screen()
                _print_pretty(ctx)
                continue
            key = ctx.get("issue_key", "?")
            _rel_keys_str = ", ".join(r["key"] for r in _related)
            _comment = (
                f"Related tickets for the same node (created ~same time): {_rel_keys_str}. "
                f"These appear to be auto-generated by monitoring for the same event. "
                f"Working them together."
            )
            print(f"\n  {BOLD}Reference comment:{RESET}")
            print(f"  {DIM}{_comment}{RESET}\n")
            try:
                _confirm = input(f"  Post this comment to {key}? [y/N]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                _confirm = ""
            if _confirm == "y":
                print(f"  {DIM}Posting...{RESET}", end="", flush=True)
                if _post_comment(key, _comment, email, token):
                    print(f"\r  {GREEN}{BOLD}Comment posted!{RESET}                ")
                    _log_event("comment", key, ctx.get("summary", ""), f"referenced: {_rel_keys_str}", ctx=ctx)
                    _cfg._issue_cache.pop(key, None)
                    _refresh_ctx(ctx, email, token)
                else:
                    print(f"\r  {YELLOW}Could not post comment.{RESET}     ")
            _brief_pause()
            _clear_screen()
            _print_pretty(ctx)
            continue

        # --- Related tickets: open all in Jira ---
        if choice == "ro" and ctx:
            _related = ctx.get("_related_tickets") or []
            if _related:
                for _r in _related:
                    _rk = _r.get("key", "")
                    if _rk and JIRA_BASE_URL:
                        webbrowser.open(f"{JIRA_BASE_URL}/browse/{_rk}")
            _clear_screen()
            _print_pretty(ctx)
            continue

        # --- Related tickets: grab all ---
        if choice == "ra" and ctx and email and token:
            _related = ctx.get("_related_tickets") or []
            if not _related:
                print(f"\n  {DIM}No related tickets found.{RESET}")
                _brief_pause()
                _clear_screen()
                _print_pretty(ctx)
                continue
            _unassigned = [r for r in _related if not (r.get("fields") or {}).get("assignee")]
            if not _unassigned:
                print(f"\n  {YELLOW}All related tickets are already assigned.{RESET}")
                _brief_pause()
                _clear_screen()
                _print_pretty(ctx)
                continue
            print(f"\n  {CYAN}Grabbing {len(_unassigned)} unassigned related ticket{'s' if len(_unassigned) != 1 else ''}:{RESET}")
            for _r in _unassigned:
                _rk = _r.get("key", "")
                print(f"  {DIM}Grabbing {_rk}...{RESET}", end="", flush=True)
                if _grab_ticket(_rk, email, token):
                    print(f"\r  {GREEN}✓{RESET} {_rk} grabbed          ")
                    _log_event("grab", _rk, (_r.get("fields") or {}).get("summary", ""), "grabbed via [ra]", ctx=ctx)
                else:
                    print(f"\r  {YELLOW}✗{RESET} {_rk} — failed         ")
            _brief_pause()
            _clear_screen()
            _print_pretty(ctx)
            continue

        # --- Related tickets: link all to current as "Relates to" ---
        if choice == "rl" and ctx and email and token:
            _related = ctx.get("_related_tickets") or []
            if not _related:
                print(f"\n  {DIM}No related tickets to link.{RESET}")
                _brief_pause()
                _clear_screen()
                _print_pretty(ctx)
                continue
            hub = ctx.get("issue_key", "?")
            spokes = [r["key"] for r in _related]
            _existing_rl = _get_existing_links(hub, email, token)
            spokes = [k for k in spokes if k not in _existing_rl]
            if not spokes:
                print(f"\n  {DIM}All related tickets are already linked to {hub}.{RESET}")
                _brief_pause()
                _clear_screen()
                _print_pretty(ctx)
                continue
            print(f"\n  {CYAN}Link {len(spokes)} ticket{'s' if len(spokes) != 1 else ''} to {hub} as 'Relates to':{RESET}")
            for k in spokes[:8]:
                print(f"    {DIM}{k}{RESET}")
            if len(spokes) > 8:
                print(f"    {DIM}... and {len(spokes) - 8} more{RESET}")
            if _existing_rl:
                _skip_count = len(_related) - len(spokes)
                if _skip_count:
                    print(f"  {DIM}({_skip_count} already linked — skipped){RESET}")
            try:
                _link_ans = input(f"\n  Confirm link all to {hub}? [{GREEN}y{RESET}/N]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                _link_ans = ""
            if _link_ans == "y":
                l_ok = l_fail = 0
                for k in spokes:
                    print(f"  {DIM}Linking {k}...{RESET}", end="", flush=True)
                    if _jira_link_issues(hub, k, email, token):
                        print(f"\r  {GREEN}✓{RESET} {k} linked to {hub}   ")
                        l_ok += 1
                    else:
                        print(f"\r  {YELLOW}✗{RESET} {k} — failed         ")
                        l_fail += 1
                print(f"\n  {GREEN}{BOLD}{l_ok} linked{RESET}", end="")
                if l_fail:
                    print(f"  {YELLOW}{l_fail} failed{RESET}", end="")
                print()
            _brief_pause()
            _clear_screen()
            _print_pretty(ctx)
            continue

        # --- Link all cab tickets to the current one as hub ---
        if choice == "lg" and ctx and email and token:
            rack_loc = ctx.get("rack_location", "")
            hub      = ctx.get("issue_key", "")
            if not rack_loc or not hub:
                print(f"\n  {DIM}No rack location for this ticket.{RESET}")
                _brief_pause()
                _clear_screen()
                _print_pretty(ctx)
                continue
            _parsed = _parse_rack_location(rack_loc)
            if not _parsed:
                _clear_screen()
                _print_pretty(ctx)
                continue
            _rack_num = _parsed["rack"]
            _site     = ctx.get("site", "")
            _sc       = f'cf[10194] = "{_site}"' if _site else f'cf[10207] ~ "{_parsed["site_code"]}"'
            _rl       = f"R{_rack_num}"
            _all_st   = '"Open","Awaiting Support","Awaiting Triage","To Do","New","In Progress","On Hold","Verification"'
            try:
                _cab_all = _jql_search(
                    f'project = "DO" AND {_sc} AND status in ({_all_st}) '
                    f'AND key != "{hub}" ORDER BY created ASC',
                    email, token, max_results=100, use_cache=False,
                    fields=["key", "customfield_10207", "customfield_10193", "customfield_10192"],
                )
            except Exception:
                _cab_all = []
            # Filter client-side to same rack
            _spokes = []
            for _iss in _cab_all:
                _loc = (_iss.get("fields", {}).get("customfield_10207") or "")
                if isinstance(_loc, dict):
                    _loc = _loc.get("value", "") or ""
                _pp = _parse_rack_location(str(_loc))
                if _pp and _pp.get("rack") == _rack_num:
                    _spokes.append(_iss["key"])
            if not _spokes:
                print(f"\n  {DIM}No other open tickets found in {_rl}.{RESET}")
                _brief_pause()
                _clear_screen()
                _print_pretty(ctx)
                continue

            # Group by service tag so user can see which nodes are involved
            _hub_tag = ctx.get("service_tag", "")
            _by_tag: dict[str, list[str]] = {}
            _tag_node: dict[str, str] = {}   # service_tag → "Node 03"
            for _iss in _cab_all:
                _loc = (_iss.get("fields", {}).get("customfield_10207") or "")
                if isinstance(_loc, dict):
                    _loc = _loc.get("value", "") or ""
                _pp = _parse_rack_location(str(_loc))
                if not (_pp and _pp.get("rack") == _rack_num):
                    continue
                _tag = (_iss.get("fields", {}).get("customfield_10193") or "")
                if isinstance(_tag, list):
                    _tag = _tag[0] if _tag else "—"
                elif isinstance(_tag, dict):
                    _tag = _tag.get("value", "—")
                _tag = str(_tag) if _tag else "—"
                _by_tag.setdefault(_tag, []).append(_iss["key"])
                # Extract node number from hostname (e.g. "dh1-r117-node-03-..." → "Node 03")
                if _tag not in _tag_node:
                    _hn = (_iss.get("fields", {}).get("customfield_10192") or "")
                    if isinstance(_hn, list):
                        _hn = _hn[0] if _hn else ""
                    elif isinstance(_hn, dict):
                        _hn = _hn.get("value", "")
                    _nm = re.search(r"node-?(\d+)", str(_hn), re.IGNORECASE)
                    _tag_node[_tag] = f"Node {int(_nm.group(1)):02d}" if _nm else ""

            _same_node_keys = _by_tag.get(_hub_tag, []) if _hub_tag else []

            # Build ordered list of (tag, keys, node_label) for numbered picking
            _node_rows = sorted(_by_tag.items())

            print(f"\n  {CYAN}Tickets in {_rl} by node:{RESET}")
            for _ni, (_tag, _keys) in enumerate(_node_rows, 1):
                _marker   = f"  {YELLOW}← this node{RESET}" if _tag == _hub_tag else ""
                _node_lbl = f" {DIM}({_tag_node[_tag]}){RESET}" if _tag_node.get(_tag) else ""
                print(f"    {BOLD}{_ni}{RESET}  {_tag}{_node_lbl}  "
                      f"{len(_keys)} ticket{'s' if len(_keys) != 1 else ''}  "
                      f"{DIM}{' '.join(_keys[:4])}{'...' if len(_keys) > 4 else ''}{RESET}{_marker}")

            print(f"\n  {DIM}Pick a node number to link just that node's tickets,{RESET}")
            print(f"  {BOLD}b{RESET}  Cancel")
            try:
                _lg_ans = input(f"\n  > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                _lg_ans = ""

            if _lg_ans == "b" or not _lg_ans:
                _clear_screen()
                _print_pretty(ctx)
                continue
            else:
                try:
                    _ni_pick = int(_lg_ans)
                    if 1 <= _ni_pick <= len(_node_rows):
                        _picked_tag, _picked_keys = _node_rows[_ni_pick - 1]
                        # Use the first ticket in that node as the hub
                        hub        = _picked_keys[0]
                        _to_link   = _picked_keys[1:]
                        if not _to_link:
                            print(f"\n  {DIM}Only 1 ticket for that node — nothing to link.{RESET}")
                            _brief_pause()
                            _clear_screen()
                            _print_pretty(ctx)
                            continue
                        print(f"\n  {DIM}Linking {len(_to_link)} tickets to {hub} ({_picked_tag}){RESET}")
                    else:
                        _clear_screen()
                        _print_pretty(ctx)
                        continue
                except ValueError:
                    _clear_screen()
                    _print_pretty(ctx)
                    continue

            # Skip any tickets already linked to hub
            _existing_lg = _get_existing_links(hub, email, token)
            _already_lg  = [k for k in _to_link if k in _existing_lg]
            _to_link     = [k for k in _to_link if k not in _existing_lg]
            if _already_lg:
                print(f"  {DIM}Skipping {len(_already_lg)} already linked: {' '.join(_already_lg)}{RESET}")
            if not _to_link:
                print(f"  {DIM}All tickets already linked to {hub}.{RESET}")
                _brief_pause()
                _clear_screen()
                _print_pretty(ctx)
                continue

            l_ok = l_fail = 0
            for k in _to_link:
                print(f"  {DIM}Linking {k}...{RESET}", end="", flush=True)
                if _jira_link_issues(hub, k, email, token):
                    print(f"\r  {GREEN}✓{RESET} {k} linked to {hub}   ")
                    l_ok += 1
                else:
                    print(f"\r  {YELLOW}✗{RESET} {k} — failed         ")
                    l_fail += 1
            print(f"\n  {GREEN}{BOLD}{l_ok} linked to {hub}{RESET}", end="")
            if l_fail:
                print(f"  {YELLOW}{l_fail} failed{RESET}", end="")
            print()
            _brief_pause()
            _clear_screen()
            _print_pretty(ctx)
            continue

        # --- Hold all my tickets in the same rack ---
        if choice == "hc" and ctx and email and token:
            if not _is_mine(ctx):
                print(f"\n  {DIM}You can only hold cabs you're assigned to.{RESET}")
                _brief_pause()
                _clear_screen()
                _print_pretty(ctx)
                continue
            rack_loc = ctx.get("rack_location", "")
            if not rack_loc:
                print(f"\n  {DIM}No rack location for this ticket.{RESET}")
                _brief_pause()
                _clear_screen()
                _print_pretty(ctx)
                continue
            _parsed = _parse_rack_location(rack_loc)
            if not _parsed:
                print(f"\n  {DIM}Could not parse rack location.{RESET}")
                _brief_pause()
                _clear_screen()
                _print_pretty(ctx)
                continue
            _rack_num = _parsed["rack"]
            _site     = ctx.get("site", "")
            _sc       = f'cf[10194] = "{_site}"' if _site else f'cf[10207] ~ "{_parsed["site_code"]}"'
            try:
                _mine_site = _jql_search(
                    f'project = "DO" AND {_sc} AND assignee = currentUser() '
                    f'AND status not in ("On Hold","Closed","RMA","Verification") '
                    f'ORDER BY created ASC',
                    email, token, max_results=60, use_cache=False,
                    fields=["key", "status", "customfield_10207"],
                )
            except Exception:
                _mine_site = []
            _mine_rack = []
            for _iss in _mine_site:
                _loc = (_iss.get("fields", {}).get("customfield_10207") or "")
                if isinstance(_loc, dict):
                    _loc = _loc.get("value", "") or ""
                _pp = _parse_rack_location(str(_loc))
                if _pp and _pp.get("rack") == _rack_num:
                    _mine_rack.append(_iss)
            _rl = f"R{_rack_num}"
            if not _mine_rack:
                print(f"\n  {DIM}No active tickets found in {_rl}.{RESET}")
                _brief_pause()
                _clear_screen()
                _print_pretty(ctx)
                continue
            print(f"\n  {CYAN}{len(_mine_rack)} active ticket{'s' if len(_mine_rack) != 1 else ''} in {_rl}:{RESET}")
            for _iss in _mine_rack[:8]:
                _st = (_iss.get("fields", {}).get("status") or {}).get("name", "?")
                print(f"    {DIM}{_iss['key']}  {_st}{RESET}")
            if len(_mine_rack) > 8:
                print(f"    {DIM}... and {len(_mine_rack) - 8} more{RESET}")
            try:
                _hold_ans = input(f"\n  Hold all {len(_mine_rack)} in {_rl}? [{GREEN}y{RESET}/N]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                _hold_ans = ""
            if _hold_ans == "y":
                h_ok = h_skip = h_fail = 0
                for _iss in _mine_rack:
                    _k  = _iss["key"]
                    _st = (_iss.get("fields", {}).get("status") or {}).get("name", "")
                    print(f"  {DIM}Holding {_k}...{RESET}", end="", flush=True)
                    _res = _hold_ticket_by_key(_k, _st, email, token)
                    if _res == "ok":
                        print(f"\r  {GREEN}✓{RESET} {_k} → On Hold        ")
                        h_ok += 1
                    elif _res == "skip":
                        print(f"\r  {DIM}↷{RESET} {_k} already on hold   ")
                        h_skip += 1
                    else:
                        print(f"\r  {YELLOW}✗{RESET} {_k} — failed        ")
                        h_fail += 1
                _cfg._issue_cache.pop(ctx.get("issue_key", ""), None)
                _refresh_ctx(ctx, email, token)
                print(f"\n  {GREEN}{BOLD}{h_ok} held{RESET}", end="")
                if h_skip:
                    print(f"  {DIM}{h_skip} already held{RESET}", end="")
                if h_fail:
                    print(f"  {YELLOW}{h_fail} failed{RESET}", end="")
                print()
            _brief_pause()
            _clear_screen()
            _print_pretty(ctx)
            continue

        # --- Toggle more options ---
        if choice == ">" and ctx:
            ctx["_show_more_actions"] = not ctx.get("_show_more_actions", False)
            _clear_screen()
            _print_pretty(ctx)
            continue

        # --- Refresh ticket (Enter or =) ---
        if choice in ("", "=") and ctx and ctx.get("source") != "netbox" and email and token:
            print(f"\n  {DIM}Refreshing...{RESET}", end="", flush=True)
            _refresh_ctx(ctx, email, token)
            print(f"\r  {GREEN}{BOLD}Refreshed!{RESET}        ")
            _brief_pause()
            _clear_screen()
            _print_pretty(ctx)
            continue
        if choice == "" and ctx:
            # Fallback if no refresh possible — just re-render
            _clear_screen()
            _print_pretty(ctx)
            continue

        # --- Actions ---
        if choice == "a" and ctx and email and token:
            key = ctx.get("issue_key", "?")
            current_assignee = ctx.get("assignee")

            # Determine if this is unassign, reassign, or grab
            already_mine = _is_ticket_mine(ctx)

            if already_mine:
                # Unassign from self
                print(f"\n  {DIM}Unassigning {key} from you...{RESET}", end="", flush=True)
                resp = _jira_put(f"/rest/api/3/issue/{key}/assignee", email, token,
                                 body={"accountId": None})
                if resp and resp.status_code == 204:
                    print(f"\r  {BLUE}{BOLD}Unassigned {key}{RESET}                    ")
                    ctx["assignee"] = None
                    _cfg._issue_cache.pop(key, None)
                else:
                    print(f"\r  {YELLOW}Could not unassign {key}.{RESET}              ")
            elif current_assignee:
                # Reassign — confirm first
                print(f"\n  {YELLOW}This ticket is assigned to {BOLD}{current_assignee}{RESET}{YELLOW}.{RESET}")
                try:
                    confirm = input(f"  Reassign to yourself? [{CYAN}y{RESET}/n]: ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    confirm = ""
                if confirm == "y":
                    print(f"  {DIM}Reassigning {key} to you...{RESET}", end="", flush=True)
                    if _grab_ticket(key, email, token):
                        print(f"\r  {GREEN}{BOLD}Reassigned {key}!{RESET}                    ")
                        _log_event("grab", key, ctx.get("summary", ""), "reassigned to you", ctx=ctx)
                        time.sleep(0.8)
                        _cfg._issue_cache.pop(key, None)
                        _refresh_ctx(ctx, email, token)
                        _show_rack_suggestions(ctx, email, token, reassigned_from=current_assignee)
                    else:
                        print(f"\r  {YELLOW}Could not reassign {key}.{RESET}              ")
            else:
                # Grab unassigned ticket
                print(f"\n  {DIM}Assigning {key} to you...{RESET}", end="", flush=True)
                if _grab_ticket(key, email, token):
                    print(f"\r  {GREEN}{BOLD}Grabbed {key}!{RESET}                    ")
                    _log_event("grab", key, ctx.get("summary", ""), ctx=ctx)
                    time.sleep(0.8)
                    _cfg._issue_cache.pop(key, None)
                    _refresh_ctx(ctx, email, token)
                    _show_rack_suggestions(ctx, email, token)
                else:
                    print(f"\r  {YELLOW}Could not grab {key}.{RESET}              ")

            _brief_pause()
            _clear_screen()
            _print_pretty(ctx)
            continue

        # --- Hand off ticket to a teammate ---
        if choice == "h" and ctx and email and token:
            key = ctx.get("issue_key", "?")
            if not _is_mine(ctx):
                print(f"\n  {DIM}You can only hand off tickets assigned to you.{RESET}")
                _brief_pause()
                _clear_screen()
                _print_pretty(ctx)
                continue

            site = ctx.get("site", "")
            selected = _pick_teammate(site, email, token, f"Hand off {key} to:")
            if not selected:
                _clear_screen()
                _print_pretty(ctx)
                continue

            # Confirm
            try:
                confirm = input(f"\n  Hand off {key} to {BOLD}{selected['name']}{RESET}? [{GREEN}y{RESET}/N]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                confirm = ""

            if confirm != "y":
                print(f"  {DIM}Cancelled.{RESET}")
                _brief_pause()
                _clear_screen()
                _print_pretty(ctx)
                continue

            print(f"  {DIM}Assigning...{RESET}", end="", flush=True)
            if _assign_ticket(key, selected["account_id"], email, token):
                print(f"\r  {GREEN}{BOLD}Handed off {key} to {selected['name']}!{RESET}          ")
                _log_event("handoff", key, ctx.get("summary", ""), f"→ {selected['name']}", ctx=ctx)
                time.sleep(0.5)
                _cfg._issue_cache.pop(key, None)
                _refresh_ctx(ctx, email, token)
                # Warn if recipient already has work in the same rack
                rack_data = _check_rack_tickets(ctx, email, token)
                others = rack_data.get("others_active")
                if others and others["name"].lower() == selected["name"].lower():
                    age_str = _format_age(others["age_secs"])
                    print(f"  {DIM}Note: {selected['name'].split()[0]} already has "
                          f"{len(others['tickets'])} active ticket(s) in "
                          f"{rack_data.get('rack_label', 'this rack')} ({age_str} ago){RESET}")
            _brief_pause()
            _clear_screen()
            _print_pretty(ctx)
            continue

        # --- Give unassigned cab tickets to a teammate ---
        if choice == "hg" and ctx and email and token:
            rack_loc = ctx.get("rack_location", "")
            if not rack_loc:
                print(f"\n  {DIM}No rack location for this ticket.{RESET}")
                _brief_pause()
                _clear_screen()
                _print_pretty(ctx)
                continue
            _parsed = _parse_rack_location(rack_loc)
            if not _parsed:
                _clear_screen()
                _print_pretty(ctx)
                continue
            _rack_num = _parsed["rack"]
            _site     = ctx.get("site", "")
            _sc       = f'cf[10194] = "{_site}"' if _site else f'cf[10207] ~ "{_parsed["site_code"]}"'
            _rl       = f"R{_rack_num}"
            _open_st  = '"Open","Awaiting Support","Awaiting Triage","To Do","New","In Progress"'

            # --- Teammate picker first ---
            selected = _pick_teammate(_site, email, token, f"Give {_rl} tickets to:")
            if not selected:
                _clear_screen()
                _print_pretty(ctx)
                continue

            # --- Fetch open unassigned tickets in this cab ---
            print(f"\n  {DIM}Checking {_rl} for open tickets...{RESET}", end="", flush=True)
            try:
                _site_open = _jql_search(
                    f'project = "DO" AND {_sc} AND status in ({_open_st}) '
                    f'AND assignee is EMPTY ORDER BY created ASC',
                    email, token, max_results=100, use_cache=False,
                    fields=["key", "summary", "customfield_10193", "customfield_10207"],
                )
            except Exception:
                _site_open = []
            print(f"\r{'':60}\r", end="")

            _cab_tickets = []
            for _iss in _site_open:
                _loc = (_iss.get("fields", {}).get("customfield_10207") or "")
                if isinstance(_loc, dict):
                    _loc = _loc.get("value", "") or ""
                _pp = _parse_rack_location(str(_loc))
                if _pp and _pp.get("rack") == _rack_num:
                    _cab_tickets.append(_iss)

            if not _cab_tickets:
                print(f"\n  {DIM}No open unassigned tickets in {_rl}.{RESET}")
                _brief_pause()
                _clear_screen()
                _print_pretty(ctx)
                continue

            # Show what we're about to assign
            _hg_count = len(_cab_tickets)
            print(f"\n  {CYAN}{_hg_count} open unassigned ticket{'s' if _hg_count != 1 else ''} in {_rl}:{RESET}")
            for _iss in _cab_tickets[:8]:
                _f   = _iss.get("fields", {})
                _tag = _f.get("customfield_10193") or "—"
                _tag = _tag.get("value", _tag) if isinstance(_tag, dict) else _tag
                _sm  = _f.get("summary", "")[:40]
                print(f"    {DIM}{_iss['key']}  {_tag}  {_sm}{RESET}")
            if _hg_count > 8:
                print(f"    {DIM}... and {_hg_count - 8} more{RESET}")

            try:
                confirm = input(
                    f"\n  Assign all {_hg_count} to {BOLD}{selected['name']}{RESET}? [{GREEN}y{RESET}/N]: "
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                confirm = ""

            if confirm != "y":
                print(f"  {DIM}Cancelled.{RESET}")
                _brief_pause()
                _clear_screen()
                _print_pretty(ctx)
                continue

            h_ok = h_fail = 0
            for _iss in _cab_tickets:
                _k = _iss["key"]
                print(f"  {DIM}Assigning {_k}...{RESET}", end="", flush=True)
                if _assign_ticket(_k, selected["account_id"], email, token):
                    print(f"\r  {GREEN}✓{RESET} {_k} → {selected['name']}          ")
                    h_ok += 1
                else:
                    print(f"\r  {YELLOW}✗{RESET} {_k} — failed         ")
                    h_fail += 1
            _cfg._issue_cache.pop(ctx.get("issue_key", ""), None)
            _refresh_ctx(ctx, email, token)
            print(f"\n  {GREEN}{BOLD}{h_ok} assigned to {selected['name']}{RESET}", end="")
            if h_fail:
                print(f"  {YELLOW}{h_fail} failed{RESET}", end="")
            print()
            _brief_pause()
            _clear_screen()
            _print_pretty(ctx)
            continue

        # --- Bookmark / unbookmark this ticket ---
        if choice == "*" and ctx and state is not None:
            key = ctx.get("issue_key", "?")
            bm_params = {"key": key}
            # Check if already bookmarked → toggle
            bookmarks = state.get("bookmarks", [])
            bm_idx = next((i for i, b in enumerate(bookmarks)
                           if b.get("type") == "ticket" and b.get("params") == bm_params), None)
            if bm_idx is not None:
                state = _remove_bookmark(state, bm_idx)
                _save_user_state(state)
                print(f"  {YELLOW}Removed bookmark for {key}{RESET}")
            else:
                summary = ctx.get("summary", "")[:40]
                label = f"{key} \u2014 {summary}" if summary else key
                state = _add_bookmark(state, label, "ticket", bm_params)
                _save_user_state(state)
                print(f"  {GREEN}Bookmarked {key}{RESET}")
            _brief_pause()
            _clear_screen()
            _print_pretty(ctx)
            continue

        # --- External links (open in browser) ---
        if choice == "j" and ctx:
            url = f"{JIRA_BASE_URL}/browse/{ctx['issue_key']}"
            print(f"  {DIM}Opening {url}{RESET}")
            webbrowser.open(url)
            _clear_screen()
            _print_pretty(ctx)
            continue
        if choice == "p" and ctx and ctx.get("_portal_url"):
            print(f"  {DIM}Opening portal...{RESET}")
            webbrowser.open(ctx["_portal_url"])
            _clear_screen()
            _print_pretty(ctx)
            continue
        if choice == "g" and has_grafana:
            url = ctx["grafana"]["node_details"]
            print(f"  {DIM}Opening Grafana...{RESET}")
            webbrowser.open(url)
            _clear_screen()
            _print_pretty(ctx)
            continue
        if choice == "pg" and ctx and ctx.get("psu_info"):
            psu_url = _find_psu_dashboard_url(ctx)
            if psu_url:
                print(f"  {DIM}Opening PSU dashboard...{RESET}")
                webbrowser.open(psu_url)
            _clear_screen()
            _print_pretty(ctx)
            continue
        if choice == "i" and ctx and ctx.get("grafana", {}).get("ib_node_search"):
            url = ctx["grafana"]["ib_node_search"]
            print(f"\n  {YELLOW}{BOLD}Note:{RESET} {DIM}IB Grafana link is not working properly — looking into it.{RESET}")
            print(f"  {DIM}URL: {url}{RESET}\n")
            try:
                raw = input(f"  Open anyway? [y/{CYAN}n{RESET}]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                raw = ""
            if raw == "y":
                webbrowser.open(url)
            _clear_screen()
            _print_pretty(ctx)
            continue
        if choice == "t" and ctx:
            netbox = ctx.get("netbox", {})
            dev = netbox.get("device_name")
            slug = netbox.get("site_slug")
            node_hex = ctx.get("node_name") or ""
            if dev and slug:
                _tp_domain = os.environ.get("TELEPORT_DOMAIN", "int.example.com")
                url = f"https://bmc-{dev}.teleport.{slug}.{_tp_domain}/"
                has_tsh = False
                try:
                    from cwhelper.clients.teleport import _tsh_available, _tsh_ensure_login
                    has_tsh = _tsh_available() or _tsh_ensure_login(interactive=True)
                except ImportError:
                    pass
                if has_tsh:
                    print(f"\n  {BOLD}Teleport / BMC{RESET}")
                    print(f"  {'━' * 55}")
                    print(f"  {DIM}Hostname:{RESET}  {BOLD}{dev}{RESET}")
                    if node_hex:
                        print(f"  {DIM}Node:    {RESET}  {CYAN}{node_hex}{RESET}")
                    print()
                    opts = []
                    if node_hex:
                        opts.append(("grep node", f"tsh ls | grep {node_hex}", "Check node reachability"))
                    opts.append(("grep rack", f"tsh ls | grep {dev.split('-')[0]}-.*{dev.split('-')[1] if '-' in dev else ''}.*{slug}", "Check rack reachability (this site)"))
                    if node_hex:
                        opts.append(("ssh", f"tsh ssh acc@{node_hex}", "SSH console (requires JIT)"))
                    opts.append(("bmc", None, f"Open BMC in browser"))
                    for i, (_, cmd, label) in enumerate(opts, 1):
                        if cmd:
                            print(f"    {BOLD}{i}{RESET}  {label}")
                            print(f"       {DIM}{cmd}{RESET}")
                        else:
                            print(f"    {BOLD}{i}{RESET}  {label}")
                            print(f"       {DIM}{url}{RESET}")
                    print(f"\n  {DIM}Pick a number to run, or ENTER to go back{RESET}")
                    print(f"  {YELLOW}⚠ tsh ls can take 30s+ on large clusters{RESET}\n")
                    try:
                        tsh_pick = input("  > ").strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        tsh_pick = ""
                    if tsh_pick.isdigit() and 1 <= int(tsh_pick) <= len(opts):
                        idx = int(tsh_pick) - 1
                        _, cmd, label = opts[idx]
                        if cmd is None:
                            webbrowser.open(url)
                        else:
                            print(f"\n  {DIM}Running: {cmd}{RESET}\n")
                            try:
                                r = subprocess.run(
                                    cmd, shell=True,
                                    capture_output=True, text=True, timeout=60,
                                )
                                output = (r.stdout or "").strip()
                                if output:
                                    lines = output.splitlines()
                                    for line in lines:
                                        print(f"  {line}")
                                    if "grep" in cmd:
                                        locked = [l for l in lines if "lockdown=enabled" in l]
                                        print(f"\n  {DIM}{len(lines)} node(s) found{RESET}")
                                        if locked:
                                            print(f"  {YELLOW}{BOLD}⚠ {len(locked)} node(s) in lockdown:{RESET}")
                                            for l in locked:
                                                hostname = l.split()[0].strip()
                                                print(f"    {YELLOW}{hostname}{RESET}")
                                elif r.returncode == 1 and "grep" in cmd:
                                    print(f"  {YELLOW}No nodes found in Teleport for this pattern{RESET}")
                                elif r.returncode != 0:
                                    err = (r.stderr or "").strip()
                                    print(f"  {RED}Command failed (exit {r.returncode}){RESET}")
                                    if err:
                                        for line in err.splitlines()[:5]:
                                            print(f"  {DIM}{line}{RESET}")
                                else:
                                    print(f"  {YELLOW}No output{RESET}")
                            except subprocess.TimeoutExpired:
                                print(f"  {YELLOW}Timed out after 60s — cluster too large for tsh ls{RESET}")
                            except OSError as e:
                                print(f"  {RED}Error: {e}{RESET}")
                            print()
                            try:
                                input(f"  {DIM}Press ENTER to continue{RESET}")
                            except (EOFError, KeyboardInterrupt):
                                pass
                else:
                    print(f"\n  {YELLOW}{BOLD}Note:{RESET} {DIM}Remote Console requires Teleport + site DCT BMC group role. You will not be able to view it unless you get access.{RESET}")
                    print(f"  {DIM}URL: {url}{RESET}\n")
                    try:
                        raw = input(f"  Open anyway? [y/{CYAN}n{RESET}]: ").strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        raw = ""
                    if raw == "y":
                        webbrowser.open(url)
            _clear_screen()
            _print_pretty(ctx)
            continue
        if choice == "at" and ctx and ctx.get("attachments"):
            attachments = ctx["attachments"]
            print(f"\n  {BOLD}Attachments ({len(attachments)}){RESET}\n")
            for i, att in enumerate(attachments, 1):
                size_kb = att.get("size", 0) / 1024
                size_str = f"{size_kb:.0f}KB" if size_kb < 1024 else f"{size_kb/1024:.1f}MB"
                print(f"    {BOLD}{i}{RESET}  {CYAN}{att['filename']}{RESET}  {DIM}{size_str}  {att['created']}  {att['author']}{RESET}")
            print(f"\n  {DIM}Pick a number to open in browser, or ENTER to go back{RESET}")
            try:
                att_pick = input("  > ").strip()
            except (EOFError, KeyboardInterrupt):
                att_pick = ""
            if att_pick.isdigit():
                idx = int(att_pick) - 1
                if 0 <= idx < len(attachments) and attachments[idx].get("url"):
                    print(f"  {DIM}Opening {attachments[idx]['filename']}...{RESET}")
                    webbrowser.open(attachments[idx]["url"])
            _clear_screen()
            _print_pretty(ctx)
            continue
        # --- img: Attach screenshot from clipboard ---------------------------
        if choice == "img" and ctx and email and token:
            key = ctx.get("issue_key", "?")
            tmp_path = os.path.join(tempfile.gettempdir(), f"{key}_screenshot.png")
            print(f"\n  {DIM}Reading clipboard image...{RESET}", end="", flush=True)
            try:
                result = subprocess.run(["pngpaste", tmp_path], capture_output=True)
            except FileNotFoundError:
                print(f"\r  {YELLOW}pngpaste not found.{RESET}  "
                      f"{DIM}Install with: brew install pngpaste{RESET}")
                _brief_pause(2)
                _clear_screen()
                _print_pretty(ctx)
                continue
            if result.returncode != 0:
                print(f"\r  {YELLOW}No image in clipboard.{RESET}  "
                      f"{DIM}Take a screenshot first (Cmd+Shift+4), then press [img].{RESET}")
                print(f"  {DIM}(Requires pngpaste — install with: brew install pngpaste){RESET}")
                _brief_pause(2)
                _clear_screen()
                _print_pretty(ctx)
                continue
            print(f"\r  {DIM}Uploading to {key}...{RESET}                    ", end="", flush=True)
            if _upload_attachment(key, tmp_path, email, token):
                print(f"\r  {GREEN}{BOLD}Screenshot attached to {key}!{RESET}                    ")
                _log_event("comment", key, ctx.get("summary", ""), "screenshot attached", ctx=ctx)
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                time.sleep(0.5)
                _cfg._issue_cache.pop(key, None)
                _refresh_ctx(ctx, email, token)
            else:
                print(f"\r  {YELLOW}Upload failed. Check your Jira permissions.{RESET}          ")
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                _brief_pause()
            _clear_screen()
            _print_pretty(ctx)
            continue

        # --- vv: Revert to Verification (from Closed) ------------------------
        if choice == "vv" and ctx and email and token:
            key = ctx.get("issue_key", "?")
            print(f"  {DIM}Moving {key} back to Verification...{RESET}", end="", flush=True)
            if _execute_transition(ctx, "revert_verify", email, token):
                print(f"\r  {BLUE}{BOLD}{key} → Verification{RESET}                    ")
                _log_event("verify", key, ctx.get("summary", ""), "reverted to Verification", ctx=ctx)
                time.sleep(0.5)
                _refresh_ctx(ctx, email, token)
            else:
                # Direct Verification not available — try Reopen → Verification chain
                _avail = [t.get("name", "").lower() for t in (ctx.get("_transitions") or [])]
                if any("reopen" in a for a in _avail):
                    print(f"\r  {DIM}Reopening {key} first...{RESET}                    ", flush=True)
                    ctx["_transitions"] = None  # reset cached transitions
                    if _execute_transition(ctx, "resume", email, token):
                        time.sleep(0.3)
                        ctx["_transitions"] = None  # refetch after status change
                        print(f"  {DIM}Now moving to Verification...{RESET}", end="", flush=True)
                        if _execute_transition(ctx, "verify", email, token):
                            print(f"\r  {BLUE}{BOLD}{key} → Reopened → Verification{RESET}                    ")
                            _log_event("verify", key, ctx.get("summary", ""), "reopened then moved to Verification", ctx=ctx)
                            time.sleep(0.5)
                            _refresh_ctx(ctx, email, token)
                        else:
                            print(f"\r  {YELLOW}Reopened {key} but could not move to Verification.{RESET}              ")
                            _refresh_ctx(ctx, email, token)
                            _brief_pause()
                    else:
                        print(f"\r  {YELLOW}Could not reopen {key}.{RESET}              ")
                        _brief_pause()
                else:
                    print(f"\r  {YELLOW}Could not revert {key} to Verification.{RESET}              ")
                    _brief_pause()
            _clear_screen()
            _print_pretty(ctx)
            continue

        # --- Rack Blockers (cwctl) ---
        if choice == "bl" and ctx:
            if ctx.get("_show_blockers"):
                ctx["_show_blockers"] = False
                _clear_screen()
                _print_pretty(ctx)
                continue
            # Try to get cwctl rack name from NetBox device, or prompt user
            rack_name = (ctx.get("netbox", {}).get("cwctl_rack")
                         or ctx.get("_cwctl_rack_name", ""))
            if not rack_name:
                print(f"\n  {DIM}cwctl rack name not in context (Jira uses physical location, cwctl uses K8s names).{RESET}")
                try:
                    rack_name = input(f"  Enter cwctl rack name (e.g. gb200-rack-site01-r064): ").strip()
                except (EOFError, KeyboardInterrupt):
                    rack_name = ""
            if not rack_name:
                print(f"\n  {YELLOW}No rack name found in ticket context.{RESET}")
                _brief_pause()
                _clear_screen()
                _print_pretty(ctx)
                continue
            ctx["_cwctl_rack_name"] = rack_name  # cache for toggle
            print(f"\n  {DIM}Fetching checkpoint blockers for {rack_name}...{RESET}", flush=True)
            blockers = _cwctl_rack_blockers(rack_name)
            if blockers:
                ctx["_show_blockers"] = True
                print(f"\n  {RED}{BOLD}Checkpoint Blockers — {rack_name}{RESET}  ({len(blockers)} total)\n")
                for i, blk in enumerate(blockers, 1):
                    severity = blk.get("severity", "unknown")
                    sev_color = RED if severity.lower() in ("critical", "error") else YELLOW
                    reason = blk.get("reason", "")
                    source = blk.get("source", "")
                    message = blk.get("message", "")
                    first_obs = blk.get("first-observed", blk.get("firstObserved", ""))
                    last_obs = blk.get("last-observed", blk.get("lastObserved", ""))
                    print(f"  {sev_color}{BOLD}{i}. [{severity}]{RESET}  {WHITE}{reason}{RESET}")
                    if source:
                        print(f"     {DIM}source:{RESET} {source}")
                    if message:
                        # Wrap long messages
                        msg_lines = textwrap.wrap(message, width=70)
                        for ml in msg_lines:
                            print(f"     {ml}")
                    if first_obs or last_obs:
                        print(f"     {DIM}first: {first_obs}  last: {last_obs}{RESET}")
                    print()
                input(f"  {DIM}Press ENTER to continue...{RESET}")
            elif blockers is not None:
                print(f"\n  {GREEN}{BOLD}No blockers{RESET} — rack {rack_name} is clear.")
                _brief_pause()
            else:
                print(f"\n  {YELLOW}Could not fetch blockers.{RESET} {DIM}(cwctl error or no kubeconfig){RESET}")
                _brief_pause()
            _clear_screen()
            _print_pretty(ctx)
            continue

        if choice == "fd" and ctx and ctx.get("service_tag"):
            tag = ctx["service_tag"]
            base_url = f"https://fleetops-storage.cwobject.com/diags/{tag}"
            print(f"\n  {DIM}Fetching Fleet Diags for {tag}...{RESET}")
            try:
                # Use curl to avoid Python SSL issues with old LibreSSL
                _curl_result = subprocess.run(
                    ["curl", "-s", "--max-time", "15", f"{base_url}/index.html"],
                    capture_output=True, text=True, timeout=20)
                _curl_ok = _curl_result.returncode == 0 and _curl_result.stdout.strip()
                if _curl_ok:
                    # Parse log URLs from HTML
                    _parser = _FleetDiagLinkParser(f"{base_url}/index.html")
                    _parser.feed(_curl_result.stdout)
                    _fd_links = _parser.links

                    if _fd_links:
                        # Auto-fetch key logs and analyze — one button, instant results
                        priority_names = ["sel.log", "dmesg.log", "alert_history.yaml",
                                          "firmware_versions.yaml", "nvidia_smi_q.txt",
                                          "nvidia_smi_nvlink.txt", "lspci.txt"]
                        log_text = []
                        fetched = 0
                        print(f"  {DIM}Found {len(_fd_links)} logs. Fetching key files...{RESET}")
                        for pname in priority_names:
                            for lg in _fd_links:
                                if lg["url"].endswith(pname) and fetched < 5:
                                    try:
                                        _lr = subprocess.run(
                                            ["curl", "-s", "--max-time", "15", lg["url"]],
                                            capture_output=True, text=True, timeout=20)
                                        if _lr.returncode == 0 and _lr.stdout:
                                            content = _lr.stdout
                                            if len(content) > 12000:
                                                content = content[:12000] + "\n[...truncated]"
                                            log_text.append(f"\n--- {pname} ---\n{content}")
                                            fetched += 1
                                            print(f"    {GREEN}{pname}{RESET}")
                                    except Exception:
                                        pass
                                    break
                        if log_text:
                            ctx["_fleet_diag_logs"] = "\n".join(log_text)
                            if _ai_available():
                                _ai_dispatch(ctx=ctx, email=email or "", token=token or "",
                                             initial_msg="Analyze these diagnostic logs. Summarize what's important — errors, warnings, hardware issues, missing components. Be specific with line references.")
                            else:
                                # No AI — print raw log excerpts
                                print(f"\n  {BOLD}Fleet Diags — {tag}{RESET}\n")
                                for lt in log_text:
                                    lines_list = lt.split("\n")
                                    for line in lines_list[:30]:
                                        print(f"  {DIM}{line}{RESET}")
                                    if len(lines_list) > 30:
                                        print(f"  {DIM}  ...({len(lines_list) - 30} more lines){RESET}")
                                input(f"\n  {DIM}Press ENTER to continue...{RESET}")
                            ctx.pop("_fleet_diag_logs", None)
                        else:
                            print(f"  {YELLOW}No text logs found. Opening in browser...{RESET}")
                            webbrowser.open(f"{base_url}/index.html")
                    else:
                        print(f"  {YELLOW}No logs found for {tag}.{RESET}")
                        print(f"  {DIM}Opening in browser instead...{RESET}")
                        webbrowser.open(f"{base_url}/index.html")
                else:
                    print(f"  {YELLOW}Could not fetch diags for {tag}.{RESET}")
                    print(f"  {DIM}Opening in browser...{RESET}")
                    webbrowser.open(f"{base_url}/index.html")
            except Exception:
                print(f"  {YELLOW}Could not fetch diags for {tag}. Check connectivity.{RESET}")
                print(f"  {DIM}Opening in browser...{RESET}")
                webbrowser.open(f"{base_url}/index.html")
            _clear_screen()
            _print_pretty(ctx)
            continue
        if choice == "x" and ctx:
            netbox = ctx.get("netbox", {})
            if netbox and netbox.get("device_id"):
                api_base = os.environ.get("NETBOX_API_URL", "").strip().rstrip("/")
                nb_base = api_base.rsplit("/api", 1)[0] if "/api" in api_base else api_base
                url = f"{nb_base}/dcim/devices/{netbox['device_id']}/"
                print(f"  {DIM}Opening {url}{RESET}")
                webbrowser.open(url)
            _clear_screen()
            _print_pretty(ctx)
            continue

        if choice == "si" and ctx:
            snipe_url = (ctx.get("netbox") or {}).get("snipe_url")
            if snipe_url:
                print(f"  {DIM}Opening Snipe-IT... {snipe_url}{RESET}")
                webbrowser.open(snipe_url)
            _clear_screen()
            _print_pretty(ctx)
            continue

        # --- Inline views (clear + ticket info + inline content) ---
        if choice == "c" and ctx:
            # Lazy-parse comments on first access
            if not ctx.get("comments") and ctx.get("_raw_comments"):
                ctx["comments"] = _extract_comments(
                    {"comment": {"comments": ctx["_raw_comments"]}}, max_comments=3)
            ctx["_show_comments"] = not ctx.get("_show_comments", False)
            _clear_screen()
            _print_pretty(ctx)
            if ctx["_show_comments"]:
                print(f"\n  {BOLD}Comments{RESET}")
                for c in ctx["comments"]:
                    print(f"    {DIM}{c['created']}{RESET}  {MAGENTA}{c['author']}{RESET}")
                    if c["body"]:
                        print(f"      {c['body']}")
                if ctx.get("source") != "netbox":
                    print(f"\n    {DIM}Press {RESET}{GREEN}{BOLD}+{RESET}{DIM} to add a comment{RESET}")
            continue
        if choice == "r" and ctx and ctx.get("rack_location"):
            _clear_screen()
            rl = ctx["rack_location"]
            # If rack_location looks like a hostname, build a proper location string
            if "." not in rl and "-" in rl:
                _rm = re.search(r"-r(\d{2,4})", rl.lower())
                if _rm:
                    _rn = int(_rm.group(1).lstrip("0") or "0")
                    _site = ctx.get("site") or ""
                    # Determine DH/sector from netbox or default
                    _nb = ctx.get("netbox") or {}
                    _dh = "DH1"  # default for hostname-derived
                    if _nb.get("rack"):
                        # Try to get DH from existing rack_location parse
                        _p = _parse_rack_location(ctx.get("rack_location", ""))
                        if _p:
                            _dh = _p["dh"]
                    rl = f"{_site}.{_dh}.R{_rn}"
            _draw_mini_dh_map(rl)
            input(f"  {DIM}Press ENTER to return...{RESET}")
            _clear_screen()
            _print_pretty(ctx)
            continue
        if choice == "n" and ctx:
            _clear_screen()
            _print_pretty(ctx)
            _print_connections_inline(ctx)
            continue
        if choice == "l" and ctx and ctx.get("linked_issues"):
            _clear_screen()
            _print_pretty(ctx)
            _print_linked_inline(ctx)
            continue
        if choice == "d" and ctx and ctx.get("diag_links"):
            ctx["_show_diags"] = not ctx.get("_show_diags", False)
            _clear_screen()
            _print_pretty(ctx)
            if ctx["_show_diags"]:
                _print_diagnostics_inline(ctx)
            continue
        if choice == "w" and ctx and ctx.get("description_text"):
            ctx["_show_desc"] = not ctx.get("_show_desc", False)
            _clear_screen()
            _print_pretty(ctx)
            if ctx["_show_desc"]:
                src = ctx.get("_description_source", "description")
                label = "Description" if src == "description" else f"Reporter ({ctx.get('reporter', '?')})"
                print(f"\n  {BOLD}{WHITE}{label}{RESET}")
                print(f"  {'─' * 50}")
                adf = ctx.get("_description_adf")
                if adf and isinstance(adf, dict):
                    rendered, _ = _render_adf_description(adf)
                    for ln in rendered:
                        print(ln)
                else:
                    for line in ctx["description_text"].splitlines():
                        if line.strip():
                            wrapped = textwrap.fill(
                                html_mod.unescape(line.strip()), width=70,
                                initial_indent="    ", subsequent_indent="    ")
                            print(wrapped)
                        else:
                            print()
                if ctx.get("diag_links"):
                    print(f"\n    {DIM}This description has links — use {RESET}{BLUE}{BOLD}[d]{RESET}{DIM} Diags to open them{RESET}")
                print(f"\n    {DIM}Press {RESET}{WHITE}{BOLD}[w]{RESET}{DIM} again to close{RESET}")
                print()
            continue
        if choice == "vr" and ctx:
            # Inline verify — run verification checks without leaving TUI
            from cwhelper.services.verify import run_verify, run_verify_batch, _detect_flow
            has_node_id = ctx.get("service_tag") or ctx.get("hostname")
            if has_node_id:
                identifier = ctx.get("issue_key") or ctx.get("hostname") or ctx.get("service_tag") or ""
                if identifier:
                    desc = ctx.get("description_text", "")
                    summ = ctx.get("summary", "")
                    flow_hint = _detect_flow(f"{summ} {desc}")
                    flow_arg = flow_hint if flow_hint != "general" else None

                    # Check for rack siblings — offer batch if multiple DO tickets
                    rack_loc = ctx.get("rack_location", "")
                    rack_serials = []
                    rack_label = ""
                    if rack_loc:
                        import re as _re
                        rack_m = _re.search(r"R(\d+)", rack_loc)
                        if rack_m:
                            rack_num = rack_m.group(1)
                            rack_label = f"R{rack_num}"
                            try:
                                from cwhelper.clients.jira import _get_credentials
                                from cwhelper.services.search import _jql_search
                                email, token = _get_credentials()
                                jql = (f'project = "DO" AND "Rack Location" ~ "R{rack_num}"'
                                       f' ORDER BY created DESC')
                                siblings = _jql_search(jql, email, token, max_results=20,
                                    fields=["customfield_10193", "summary"])
                                for sib in siblings:
                                    sf = sib.get("fields", {})
                                    stag = sf.get("customfield_10193")
                                    if isinstance(stag, dict):
                                        stag = stag.get("value", "")
                                    elif isinstance(stag, list):
                                        stag = stag[0] if stag else ""
                                    stag = str(stag or "").strip()
                                    if stag and stag not in rack_serials:
                                        rack_serials.append(stag)
                            except Exception:
                                rack_serials = []

                    if len(rack_serials) > 1:
                        print(f"\n  {BOLD}Found {len(rack_serials)} nodes in {rack_label} with open DO tickets.{RESET}")
                        print(f"  {DIM}[b] Batch verify all  [s] Single node only{RESET}")
                        try:
                            batch_choice = input(f"  {BOLD}>{RESET} ").strip().lower()
                        except (EOFError, KeyboardInterrupt):
                            batch_choice = "s"
                        if batch_choice == "b":
                            from cwhelper.services.verify import _FLOW_LABELS
                            flow_label = _FLOW_LABELS.get(flow_arg or "", "")
                            run_verify_batch(rack_serials, rack_label=rack_label,
                                             flow_label=flow_label)
                            print(f"  {DIM}Press ENTER to return to ticket view...{RESET}")
                            try:
                                input()
                            except (EOFError, KeyboardInterrupt):
                                pass
                            _clear_screen()
                            _print_pretty(ctx)
                            continue

                    # Single-node verify (default)
                    hints = {}
                    if ctx.get("hostname"):
                        hints["hostname"] = ctx["hostname"]
                    print()
                    run_verify(identifier, flow_type=flow_arg, hints=hints or None)
                    print(f"  {DIM}Press ENTER to return to ticket view...{RESET}")
                    try:
                        input()
                    except (EOFError, KeyboardInterrupt):
                        pass
                else:
                    print(f"\n  {YELLOW}No identifier available for verification.{RESET}")
                    _brief_pause()
            _clear_screen()
            _print_pretty(ctx)
            continue
        if choice == "e" and ctx and email and token:
            netbox = ctx.get("netbox") or {}
            # Resolve rack_id from rack_location if NetBox didn't have it
            if not netbox.get("rack_id") and (ctx.get("rack_location") or ctx.get("hostname")):
                rl = ctx.get("rack_location", "") or ""
                parsed_rl = _parse_rack_location(rl)
                # Fallback: extract rack number from hostname pattern (s1-r027-..., dh1-r064-...)
                if not parsed_rl:
                    rack_match = re.search(r"-r(\d{2,4})", (rl + " " + (ctx.get("hostname") or "")).lower())
                    site_match = re.search(r"(us-\S+)$", (rl + " " + (ctx.get("hostname") or "")).lower())
                    if rack_match:
                        rack_num = int(rack_match.group(1).lstrip("0") or "0")
                        site_code = ctx.get("site") or (site_match.group(1) if site_match else "")
                        parsed_rl = {"site_code": site_code, "dh": "DH1", "rack": rack_num, "ru": None}
                if parsed_rl:
                    site_slug = netbox.get("site_slug") or ""
                    # Derive site_slug from Jira site code when NetBox doesn't have it
                    if not site_slug and parsed_rl.get("site_code"):
                        site_slug = parsed_rl["site_code"].lower()
                    rack_obj = _netbox_find_rack_by_name(
                        str(parsed_rl["rack"]), site_slug or None)
                    # Fallback: try Jira's site field as site_slug (LoCode vs region mismatch)
                    if not rack_obj and ctx.get("site"):
                        jira_site_slug = ctx["site"].lower()
                        if jira_site_slug != site_slug:
                            rack_obj = _netbox_find_rack_by_name(
                                str(parsed_rl["rack"]), jira_site_slug)
                    if rack_obj and rack_obj.get("id"):
                        if not netbox:
                            netbox = {}
                            ctx["netbox"] = netbox
                        netbox["rack_id"] = rack_obj["id"]
                        netbox["rack"] = rack_obj.get("name") or str(parsed_rl["rack"])
                        if not netbox.get("site_slug"):
                            site_obj = rack_obj.get("site") or {}
                            netbox["site_slug"] = site_obj.get("slug", "")
            if netbox and netbox.get("rack_id"):
                _clear_screen()
                result = _handle_rack_view(ctx, email, token)
                if result in ("quit", "menu", "back"):
                    return result
                _clear_screen()
                _print_pretty(ctx)
            else:
                print(f"\n  {DIM}Could not find rack in NetBox.{RESET}")
            continue
        if choice == "f" and ctx:
            search = ctx.get("service_tag") or ctx.get("hostname") or ""
            if search:
                jql = f'project = MRB AND text ~ "{_escape_jql(search)}" ORDER BY created DESC'
                url = f"{JIRA_BASE_URL}/issues/?jql={_url_quote(jql)}"
                print(f"  {DIM}Opening MRB search in Jira...{RESET}")
                webbrowser.open(url)
            _clear_screen()
            _print_pretty(ctx)
            continue
        if choice == "o" and ctx and ctx.get("ho_context") and email and token:
            ho_key = ctx["ho_context"]["key"]
            print(f"\n  {DIM}Fetching {ho_key}...{RESET}")
            ho_issue = _jira_get_issue(ho_key, email, token)
            if ho_issue:
                ho_ctx = _build_context(ho_key, ho_issue, email, token)
                st = _load_user_state()
                st = _record_ticket_view(st, ho_ctx["issue_key"], ho_ctx.get("summary", ""),
                                         assignee=ho_ctx.get("assignee"), updated=ho_ctx.get("updated"))
                _save_user_state(st)
                _clear_screen()
                _print_pretty(ho_ctx)
                ho_action = _post_detail_prompt(ho_ctx, email, token, state=st)
                if ho_action == "quit":
                    return "quit"
                if ho_action == "menu":
                    return "menu"
                # "back" from HO → return to the DO ticket
                _clear_screen()
                _print_pretty(ctx)
            else:
                print(f"  {DIM}Could not fetch {ho_key}.{RESET}")
            continue

        if choice == "u" and ctx:
            ctx["_show_sla"] = not ctx.get("_show_sla", False)
            _clear_screen()
            _print_pretty(ctx)
            if ctx["_show_sla"]:
                _print_sla_detail(ctx)
            continue

        # --- Status transitions -------------------------------------------

        if choice == "s" and ctx and email and token:
            # [s] Start Work: New/To Do → In Progress (auto-assign if needed)
            key = ctx.get("issue_key", "?")
            status_lower = ctx.get("status", "").lower()
            if status_lower not in ("to do", "new", "open",
                                    "waiting for triage", "awaiting triage",
                                    "awaiting support", "reopened"):
                continue

            # Auto-assign if unassigned
            if not ctx.get("assignee"):
                print(f"\n  {DIM}Assigning {key} to you...{RESET}",
                      end="", flush=True)
                if not _grab_ticket(key, email, token):
                    print(f"\r  {YELLOW}Could not assign {key}. "
                          f"Aborting.{RESET}              ")
                    _brief_pause()
                    _clear_screen()
                    _print_pretty(ctx)
                    continue
                print(f"\r  {GREEN}{BOLD}Grabbed {key}!{RESET}"
                      f"                    ")

            print(f"  {DIM}Starting work on {key}...{RESET}",
                  end="", flush=True)
            if _execute_transition(ctx, "start", email, token):
                print(f"\r  {GREEN}{BOLD}{key} \u2192 In Progress"
                      f"{RESET}                    ")
                _log_event("start", key, ctx.get("summary", ""), ctx=ctx)
                _refresh_ctx(ctx, email, token)
            else:
                print(f"\r  {YELLOW}Could not start {key}."
                      f"{RESET}              ")
            _brief_pause()
            _clear_screen()
            _print_pretty(ctx)
            continue

        if choice == "v" and ctx and email and token:
            # [v] Move to Verification: In Progress → Verification
            key = ctx.get("issue_key", "?")
            comment = _pick_or_type_comment(ctx, action="verify")

            print(f"  {DIM}Moving {key} to Verification...{RESET}",
                  end="", flush=True)
            if _execute_transition(ctx, "verify", email, token):
                print(f"\r  {BLUE}{BOLD}{key} \u2192 Verification"
                      f"{RESET}                    ")
                if comment:
                    _post_comment(key, comment, email, token)
                _log_event("verify", key, ctx.get("summary", ""), ctx=ctx)
                time.sleep(0.5)
                _refresh_ctx(ctx, email, token)
            else:
                print(f"\r  {YELLOW}Could not move {key} to "
                      f"Verification.{RESET}              ")
            _brief_pause()
            _clear_screen()
            _print_pretty(ctx)
            continue

        if choice == "y" and ctx and email and token:
            # [y] Put On Hold: In Progress → On Hold (required comment)
            key = ctx.get("issue_key", "?")
            print(f"\n  {YELLOW}{BOLD}Reason for hold{RESET} {DIM}(required):{RESET}")
            comment = _pick_or_type_comment(ctx, action="hold")
            if not comment:
                print(f"  {YELLOW}Comment required. Cancelled.{RESET}")
                _brief_pause()
                _clear_screen()
                _print_pretty(ctx)
                continue

            print(f"  {DIM}Putting {key} on hold...{RESET}",
                  end="", flush=True)
            if _execute_transition(ctx, "hold", email, token):
                print(f"\r  {YELLOW}{BOLD}{key} \u2192 On Hold"
                      f"{RESET}                    ")
                if comment:
                    _post_comment(key, comment, email, token)
                _log_event("hold", key, ctx.get("summary", ""), ctx=ctx)
                time.sleep(0.5)
                _refresh_ctx(ctx, email, token)
            else:
                print(f"\r  {YELLOW}Could not put {key} on hold."
                      f"{RESET}              ")
            _brief_pause()
            _clear_screen()
            _print_pretty(ctx)
            continue

        if choice == "z" and ctx and email and token:
            # [z] Resume / Back to In Progress
            key = ctx.get("issue_key", "?")
            print(f"\n  {DIM}Add a comment (ENTER to skip):{RESET}")
            try:
                comment = input("  > ").strip()
            except (EOFError, KeyboardInterrupt):
                comment = ""

            print(f"  {DIM}Resuming {key}...{RESET}",
                  end="", flush=True)
            if _execute_transition(ctx, "resume", email, token):
                print(f"\r  {CYAN}{BOLD}{key} \u2192 In Progress"
                      f"{RESET}                    ")
                if comment:
                    _post_comment(key, comment, email, token)
                time.sleep(0.5)
                _refresh_ctx(ctx, email, token)
            else:
                print(f"\r  {YELLOW}Could not resume {key}."
                      f"{RESET}              ")
            _brief_pause()
            _clear_screen()
            _print_pretty(ctx)
            continue

        if choice == "k" and ctx and email and token:
            # [k] Close ticket: Verification → Closed (required comment)
            key = ctx.get("issue_key", "?")
            print(f"\n  {RED}{BOLD}Closing {key}{RESET}")
            comment = _pick_or_type_comment(ctx, action="close")
            if not comment:
                print(f"  {YELLOW}Comment required. Cancelled.{RESET}")
                _brief_pause()
                _clear_screen()
                _print_pretty(ctx)
                continue

            try:
                confirm = input(f"  Close {key}? "
                                f"[{RED}y{RESET}/n]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                confirm = ""
            if confirm != "y":
                print(f"  {DIM}Cancelled.{RESET}")
                _brief_pause()
                _clear_screen()
                _print_pretty(ctx)
                continue

            print(f"  {DIM}Closing {key}...{RESET}",
                  end="", flush=True)
            if _execute_transition(ctx, "close", email, token):
                print(f"\r  {GREEN}{BOLD}{key} \u2192 Closed"
                      f"{RESET}                    ")
                if comment:
                    _post_comment(key, comment, email, token)
                _log_event("close", key, ctx.get("summary", ""), ctx=ctx)
                time.sleep(0.5)
                _refresh_ctx(ctx, email, token)
            else:
                print(f"\r  {YELLOW}Could not close {key}."
                      f"{RESET}              ")
            _brief_pause()
            _clear_screen()
            _print_pretty(ctx)
            continue

        # --- AI Assistant (supports "ai" or "ai <message>") ---
        if (choice == "ai" or choice.startswith("ai ")) and ctx:
            initial = choice[3:].strip() if choice.startswith("ai ") else ""
            found_key = _ai_dispatch(ctx=ctx, email=email or "", token=token or "",
                                      initial_msg=initial)
            if found_key and JIRA_KEY_PATTERN.match(found_key):
                new_ctx = _fetch_and_show(found_key, email, token)
                if new_ctx:
                    ctx = new_ctx
            _clear_screen()
            _print_pretty(ctx)
            continue

        # Unrecognized input — stay in the detail view, don't exit
        continue



