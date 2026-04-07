"""Queue browser and history search."""
from __future__ import annotations

import os
import re
import sys
import time

import json

from cwhelper import config as _cfg
from cwhelper.config import *  # noqa: F401,F403
__all__ = ['_run_stale_verification', '_run_queue_interactive', '_search_node_history', '_run_history_interactive', '_run_history_json', '_run_queue_json']
from cwhelper.clients.jira import _get_credentials, _jira_get_issue, _refresh_ctx, _execute_transition, _is_mine, _grab_ticket
from cwhelper.state import _load_user_state, _save_user_state, _record_ticket_view, _record_queue_view, _add_bookmark, _remove_bookmark
from cwhelper.cache import _brief_pause, _escape_jql
from cwhelper.services.context import _build_context, _format_age, _parse_jira_timestamp, _short_device_name, _unwrap_field
from cwhelper.services.search import _jql_search, _search_queue, _search_by_text
from cwhelper.services.ai import _ai_dispatch
from cwhelper.tui.display import _clear_screen, _print_pretty, _prompt_select, _status_color
from cwhelper.tui.rich_console import _rich_print_queue_table, _rich_queue_prompt, console
from cwhelper.services.session_log import _log_event
# _post_detail_prompt imported lazily inside functions to avoid circular import


def _read_key():
    """Read a single keypress (including arrow keys) in raw mode.

    Returns one of: 'up', 'down', 'space', 'enter', 'q', 'a', 'n', 'y', 'm',
    or the literal character pressed. Returns None on failure / non-tty.
    """
    try:
        import tty
        import termios
    except ImportError:
        return None
    if not sys.stdin.isatty():
        return None
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == '\x1b':
            ch2 = sys.stdin.read(1)
            if ch2 == '[':
                ch3 = sys.stdin.read(1)
                if ch3 == 'A':
                    return 'up'
                elif ch3 == 'B':
                    return 'down'
                return None
            return 'q'  # bare Esc = quit
        elif ch == ' ':
            return 'space'
        elif ch in ('\r', '\n'):
            return 'enter'
        elif ch == '\x03':  # Ctrl-C
            return 'q'
        else:
            return ch.lower()
    except (OSError, ValueError):
        return None
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)




def _run_stale_verification(stale_issues: list, email: str, token: str) -> str:
    """Show only stale (>48h) verification tickets with age, let user drill in.

    Returns "quit" or "menu" to propagate upward. "back" is handled
    internally by re-displaying the stale list.
    """
    _team_mode = False
    _current_issues = list(stale_issues)

    def _fetch_team_issues():
        return _jql_search(
            'project in ("DO", "HO") AND assignee is not EMPTY AND status = "Verification" ORDER BY updated ASC',
            email, token, max_results=60, use_cache=False,
            fields=["key", "summary", "statuscategorychangedate",
                    "customfield_10193", "customfield_10194",
                    "customfield_10192", "customfield_10207",
                    "reporter", "assignee"],
        )

    while True:
        _clear_screen()
        _mode_label = f"{CYAN}Team{RESET}" if _team_mode else f"{BOLD}Mine{RESET}"
        print(f"\n  {RED}{BOLD}Stale Verification{RESET} {DIM}— {len(_current_issues)} tickets{RESET}  [{_mode_label}]  {DIM}t = toggle mine/team{RESET}")
        print(f"  {'━' * 54}\n")

        _tm = _team_mode  # capture for closure

        def _stale_label(i, iss):
            f = iss.get("fields", {})
            tag = _unwrap_field(f.get("customfield_10193")) or "\u2014"
            summary = f.get("summary", "")[:35]
            age_secs = _parse_jira_timestamp(f.get("statuscategorychangedate"))
            age_fmt = _format_age(age_secs)
            ac = RED if age_secs > 5 * 86400 else YELLOW
            if _tm:
                assignee_obj = f.get("assignee") or {}
                asgn = assignee_obj.get("displayName", "?")[:14]
                return (
                    f"  {BOLD}{i:>2}.{RESET}  {iss['key']}  "
                    f"{ac}{BOLD}{age_fmt:>7}{RESET}  "
                    f"{CYAN}{asgn:<15}{RESET} "
                    f"{DIM}{summary}{RESET}"
                )
            return (
                f"  {BOLD}{i:>2}.{RESET}  {iss['key']}  "
                f"{ac}{BOLD}{age_fmt:>7}{RESET}  "
                f"{CYAN}{tag:<16}{RESET} "
                f"{DIM}{summary}{RESET}"
            )

        _extra = f", {BOLD}t{RESET} toggle mine/team, {BOLD}e{RESET} export for Slack"
        chosen = _prompt_select(_current_issues, _stale_label, extra_hint=_extra)

        if chosen == "t":
            _team_mode = not _team_mode
            if _team_mode:
                print(f"\n  {DIM}Fetching team verification tickets...{RESET}", end="", flush=True)
                _current_issues = _fetch_team_issues()
                print(f"\r{'':50}\r", end="")
            else:
                _current_issues = list(stale_issues)
            continue

        if chosen == "e":
            # Shorten URLs via TinyURL (free, no auth)
            print(f"\n  {DIM}Shortening URLs...{RESET}", end="", flush=True)
            short_urls: dict[str, str] = {}
            try:
                import urllib.request as _urlreq
                import urllib.parse as _urlparse
                for iss in _current_issues:
                    full = f"{JIRA_BASE_URL}/browse/{iss['key']}"
                    try:
                        api = f"https://tinyurl.com/api-create.php?url={_urlparse.quote(full, safe='')}"
                        req = _urlreq.Request(api, headers={"User-Agent": "cwhelper"})
                        with _urlreq.urlopen(req, timeout=4) as r:
                            short_urls[iss["key"]] = r.read().decode().strip()
                    except Exception:
                        short_urls[iss["key"]] = full  # fallback to full URL
            except Exception:
                for iss in _current_issues:
                    short_urls[iss["key"]] = f"{JIRA_BASE_URL}/browse/{iss['key']}"
            print(f"\r{'':40}\r", end="")

            # Group by reporter
            by_reporter: dict[str, list] = {}
            for iss in _current_issues:
                reporter = (iss.get("fields", {}).get("reporter") or {}).get("displayName", "Unknown")
                by_reporter.setdefault(reporter, []).append(iss)

            lines = [
                f":warning: *Stale Verification — {len(_current_issues)} tickets need action*",
                f"These have been in Verification >48h. Engineers, please review your tickets:\n",
            ]
            for reporter, issues in sorted(by_reporter.items()):
                lines.append(f"*@{reporter}* ({len(issues)} ticket{'s' if len(issues) != 1 else ''}):")
                for iss in issues:
                    f = iss.get("fields", {})
                    tag = _unwrap_field(f.get("customfield_10193")) or ""
                    age_secs = _parse_jira_timestamp(f.get("statuscategorychangedate"))
                    age_fmt = _format_age(age_secs)
                    url = short_urls.get(iss["key"], "")
                    lines.append(f"  • {iss['key']}  {url}  _{age_fmt}_  {tag}")
                lines.append("")

            lines.append("DCT work is complete on all of the above. Happy to close any that are confirmed done, or re-engage if something needs follow-up.")
            slack_msg = "\n".join(lines)

            # Copy to clipboard
            try:
                import subprocess as _sp
                _sp.run(["pbcopy"], input=slack_msg.encode(), check=True)
                print(f"\n  {GREEN}{BOLD}Copied to clipboard!{RESET} Paste into your site ops Slack channel.")
            except Exception:
                print(f"\n  {YELLOW}Could not copy to clipboard. Here\u2019s the message:{RESET}\n")
                print(slack_msg)
            print()
            input(f"  {DIM}Press ENTER to continue...{RESET}")
            continue

        if chosen == "refresh":
            continue
        if chosen == "quit":
            return "quit"
        if chosen == "menu":
            return "menu"
        if not chosen:
            return "menu"

        key = chosen["key"]
        print(f"\n  Fetching {key}...\n")
        issue = _jira_get_issue(key, email, token)
        ctx = _build_context(key, issue, email, token)

        st = _load_user_state()
        st = _record_ticket_view(st, ctx["issue_key"], ctx.get("summary", ""),
                                    assignee=ctx.get("assignee"), updated=ctx.get("updated"))
        _save_user_state(st)

        _clear_screen()
        _print_pretty(ctx)

        from cwhelper.tui.actions import _post_detail_prompt  # lazy — avoids circular import
        action = _post_detail_prompt(ctx, email, token, state=st)

        while action == "history":
            tag = ctx.get("service_tag") or ctx.get("hostname")
            if not tag:
                break
            h_action = _run_history_interactive(email, token, tag)
            if h_action == "quit":
                return "quit"
            if h_action == "menu":
                return "menu"
            _clear_screen()
            _print_pretty(ctx)
            action = _post_detail_prompt(ctx, email, token, state=st)

        if action in ("quit", "menu"):
            return action
        # "back" → loop back to re-display the stale list



def _run_queue_interactive(email: str, token: str, site: str,
                           mine_only: bool = False, limit: int = 20,
                           status_filter: str = "open",
                           project: str = "DO"):
    """Browse tickets for a site. Pick one to drill in.

    Returns "quit" or "menu" to propagate upward. "back" is handled
    internally by re-displaying the queue list.
    """
    mine_label = " (my tickets)" if mine_only else ""
    filter_label = f"  {DIM}filter: {status_filter}{RESET}" if status_filter != "open" else ""
    _q_first = True
    _sort_by = "created"
    _col_filters: dict = {}  # keys: "status", "assignee", "rack", "node"

    _SORT_LABELS = {
        "created":  "Created (newest first)",
        "updated":  "Updated (newest first)",
        "age":      "Status age (oldest first)",
        "rack":     "Rack # (ascending)",
        "node":     "Node # (ascending)",
        "assignee": "Assignee (A→Z)",
    }

    def _extract_rack_num(f: dict) -> int | None:
        """Extract rack number from all available ticket fields."""
        rack_loc = str(f.get("customfield_10207") or "")
        hostname = str(f.get("customfield_10192") or "")
        summary = str(f.get("summary") or "")
        desc_raw = f.get("description") or ""
        if isinstance(desc_raw, dict):
            _dp = []
            for _b in desc_raw.get("content", []):
                for _i in _b.get("content", []):
                    if _i.get("type") == "text":
                        _dp.append(_i.get("text", ""))
            desc_text = " ".join(_dp)
        else:
            desc_text = str(desc_raw)
        m = (re.search(r'\.R(\d+)\.', rack_loc)
             or re.search(r':(\d+)(?::|$)', rack_loc)
             or re.search(r'\bR(\d+)\b', rack_loc, re.IGNORECASE)
             or re.search(r'\br(\d+)\b', hostname, re.IGNORECASE)
             or re.search(r'\bR(\d+)\b', summary, re.IGNORECASE)
             or re.search(r'\.R(\d+)(?:\.|$)', desc_text)
             or re.search(r'\bR(\d+)\b', desc_text, re.IGNORECASE))
        return int(m.group(1)) if m else None

    def _apply_col_filters(lst: list) -> list:
        out = []
        for iss in lst:
            f = iss.get("fields") or {}
            st_name = (f.get("status", {}).get("name", "") if isinstance(f.get("status"), dict)
                       else str(f.get("status", ""))).lower()
            assignee_obj = f.get("assignee")
            asgn = (assignee_obj.get("displayName", "") if isinstance(assignee_obj, dict)
                    else str(assignee_obj or "")).lower()
            rack_num = _extract_rack_num(f)
            hostname = str(f.get("customfield_10192") or "")
            node_m = re.search(r'-node-(\d+)', hostname)
            node_num = int(node_m.group(1)) if node_m else None

            if "status" in _col_filters and _col_filters["status"].lower() not in st_name:
                continue
            if "assignee" in _col_filters:
                fv = _col_filters["assignee"].lower()
                if fv == "mine":
                    if not (_cfg._my_display_name and
                            _cfg._my_display_name.lower() in asgn):
                        continue
                elif fv == "unassigned":
                    if asgn:
                        continue
                elif fv not in asgn:
                    continue
            if "rack" in _col_filters:
                try:
                    if rack_num != int(_col_filters["rack"]):
                        continue
                except ValueError:
                    pass
            if "node" in _col_filters:
                try:
                    if node_num != int(_col_filters["node"]):
                        continue
                except ValueError:
                    pass
            out.append(iss)
        return out

    def _apply_sort(lst: list) -> list:
        def _rack_key(iss):
            f = iss.get("fields") or {}
            return _extract_rack_num(f) or 9999

        def _node_key(iss):
            f = iss.get("fields") or {}
            m = re.search(r'-node-(\d+)', str(f.get("customfield_10192") or ""))
            return int(m.group(1)) if m else 9999

        def _asgn_key(iss):
            f = iss.get("fields") or {}
            a = f.get("assignee")
            return (a.get("displayName", "") if isinstance(a, dict) else str(a or "")).lower()

        if _sort_by == "age":
            return sorted(lst, key=lambda x: _parse_jira_timestamp(
                (x.get("fields") or {}).get("statuscategorychangedate")), reverse=True)
        if _sort_by == "updated":
            return sorted(lst, key=lambda x: (x.get("fields") or {}).get("updated", ""), reverse=True)
        if _sort_by == "rack":
            return sorted(lst, key=_rack_key)
        if _sort_by == "node":
            return sorted(lst, key=_node_key)
        if _sort_by == "assignee":
            return sorted(lst, key=_asgn_key)
        return lst  # "created" — already ordered by Jira

    while True:
        _clear_screen()
        _count_hint = f"top {limit}" if limit <= 20 else f"showing {limit}"
        # Build active filter/sort indicator
        _active = []
        if _col_filters:
            _active.append("  ".join(f"{k}={v}" for k, v in _col_filters.items()))
        if _sort_by != "created":
            _active.append(f"sort:{_sort_by}")
        _active_str = f"  [{', '.join(_active)}]" if _active else ""

        console.print(f"  [dim]Searching...[/]", end="\r")
        issues = _search_queue(site, email, token, mine_only=mine_only, limit=limit,
                               status_filter=status_filter, project=project,
                               use_cache=_q_first)
        _q_first = False

        # Apply column filters then sort
        issues = _apply_col_filters(issues)
        issues = _apply_sort(issues)

        # Record this queue view for suggestions
        if issues:
            _record_queue_view(_load_user_state(), project, site, status_filter, mine_only)

        if not issues:
            filter_display = status_filter.replace("_", " ").title() if status_filter != "all" else "All"
            site_display = site or "all sites"
            console.print(f"  [yellow bold]{filter_display}[/] — [dim]no {project} tickets for {site_display}[/]")
            if _col_filters:
                console.print(f"\n  [dim]No results match your filters. Press r to reset.[/]")
                try:
                    _empty_choice = input("  r reset, f change filter, ENTER to go back, or b for menu: ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    return "menu"
                if _empty_choice == "r":
                    _col_filters = {}
                    _sort_by = "created"
                    continue
                elif _empty_choice == "f":
                    continue
                elif _empty_choice in ("b", "m", "menu"):
                    return "menu"
                else:
                    # ENTER or anything else → back to menu
                    return "menu"
            console.print(f"  [dim]Try a different status filter or site.[/]")
            return "menu"

        # Prefetch top tickets in background while user reads the list
        _prefetch_keys = [iss.get("key") for iss in issues[:5] if iss.get("key")]
        for _pk in _prefetch_keys:
            if _pk not in _cfg._issue_cache:
                _cfg._executor.submit(_jira_get_issue, _pk, email, token)

        # Check if this queue is already bookmarked
        _q_params = {"project": project, "site": site,
                     "status_filter": status_filter, "mine_only": mine_only}
        _q_bookmarked = any(
            b.get("type") == "queue" and b.get("params") == _q_params
            for b in _load_user_state().get("bookmarks", [])
        )

        # Build queue title
        _queue_title = f"{project} queue  {site or 'all sites'}{mine_label}  ({_count_hint}){filter_label}{_active_str}"
        _page_info = f"Showing {len(issues)} tickets" + (" — n for more, a for all" if len(issues) >= limit else "")

        # Render Rich table
        _rich_print_queue_table(issues, title=_queue_title, page_info=_page_info)

        # Build hint list for prompt
        _hints = ["* bookmark" if not _q_bookmarked else "* remove bookmark"]
        if len(issues) >= limit:
            _hints += ["n next page", "a load all"]
        _hints += ["f filter", "s sort", "R rack report"]
        if _col_filters or _sort_by != "created":
            _hints.append("r reset")
        _hints.append("p start all")

        raw = _rich_queue_prompt(len(issues), extra_hints=_hints)

        # Map raw input to the same "chosen" interface as before
        def _chosen_from_raw(raw_input):
            if raw_input == "":
                return "refresh"
            if raw_input.lower() in ("q", "quit", "exit"):
                return "quit"
            if raw_input.lower() in ("m", "menu"):
                return "menu"
            if raw_input.lower() in ("b", "back"):
                return None
            if raw_input.lower() == "ai":
                return "ai"
            if raw_input == "R":
                return "R"  # uppercase R = rack report (distinct from lowercase r = reset)
            if raw_input.lower() in ("x", "n", "a", "e", "f", "s", "r", "l", "p") or raw_input == "*":
                return raw_input
            try:
                idx = int(raw_input)
                if 1 <= idx <= len(issues):
                    return issues[idx - 1]
            except ValueError:
                pass
            return "refresh"

        chosen = _chosen_from_raw(raw)

        if chosen == "refresh":
            continue  # re-fetch and re-render the queue

        if chosen == "quit":
            return "quit"

        if chosen == "menu":
            return "menu"

        if chosen == "n":
            limit += 20
            continue

        if chosen == "a":
            limit = 200
            continue

        if chosen == "*":
            # Toggle bookmark for this queue view
            site_label = site or "all sites"
            bm_label = f"{project} {status_filter} @ {site_label}"
            if mine_only:
                bm_label += " (mine)"
            bm_params = {"project": project, "site": site,
                         "status_filter": status_filter, "mine_only": mine_only}
            st = _load_user_state()
            bookmarks = st.get("bookmarks", [])
            bm_idx = next((i for i, b in enumerate(bookmarks)
                           if b.get("type") == "queue" and b.get("params") == bm_params), None)
            if bm_idx is not None:
                st = _remove_bookmark(st, bm_idx)
                _save_user_state(st)
                print(f"\n  {YELLOW}Removed bookmark: {bm_label}{RESET}")
            else:
                st = _add_bookmark(st, bm_label, "queue", bm_params)
                _save_user_state(st)
                print(f"\n  {GREEN}Bookmarked: {bm_label}{RESET}")
            time.sleep(0.5)
            continue  # re-render the queue

        if chosen == "r":
            _col_filters = {}
            _sort_by = "created"
            continue

        if chosen == "R":
            # Rack report — jump straight from queue view
            from cwhelper.services.rack_report import _run_rack_report
            _run_rack_report(email, token, site,
                             status_filter=status_filter,
                             project=project,
                             limit=200)
            try:
                input(f"\n  {DIM}Press ENTER to return to queue...{RESET}")
            except (EOFError, KeyboardInterrupt):
                pass
            continue  # back to queue, not menu

        if chosen == "p":
            # Bulk start — transition all startable tickets to In Progress
            _startable = [
                iss for iss in issues
                if (iss.get("fields", {}).get("status", {}).get("name", "").lower()
                    not in ("in progress", "verification", "closed", "done", "resolved"))
            ]
            if not _startable:
                print(f"\n  {GREEN}No startable tickets — all already In Progress or beyond.{RESET}")
                _brief_pause(1.5)
                continue

            # --- Compact preview grouped by location ---
            def _loc_from_issue(iss):
                """Extract (site, DH, Rack) from rack_location + site fields."""
                _f = iss.get("fields") or {}
                _rl = str(_f.get("customfield_10207") or "")
                _site_raw = _f.get("customfield_10194") or ""
                if isinstance(_site_raw, dict):
                    _site_raw = _site_raw.get("value", "") or _site_raw.get("name", "")
                _site = str(_site_raw).strip() or "?"
                _dh = "?"
                _rack = "?"
                _dh_m = re.search(r'(DH\d+)', _rl, re.IGNORECASE)
                if _dh_m:
                    _dh = _dh_m.group(1).upper()
                _r_m = re.search(r'\bR(\d+)\b', _rl, re.IGNORECASE)
                if not _r_m:
                    _r_m = _extract_rack_num(_f)
                    if _r_m is not None:
                        _rack = f"R{_r_m}"
                else:
                    _rack = f"R{_r_m.group(1)}"
                return _site, _dh, _rack

            # Build per-location groups
            from collections import OrderedDict
            _loc_groups = OrderedDict()
            for _pi in _startable:
                _site, _dh, _rack = _loc_from_issue(_pi)
                _loc_key = (_site, _dh, _rack)
                _loc_groups.setdefault(_loc_key, []).append(_pi)

            # --- Flat indexed list for interactive picking ---
            _pick_list = []  # [(index, issue, site, dh, rack)]
            for (_site, _dh, _rack), _grp in _loc_groups.items():
                for _pi in _grp:
                    _pick_list.append((len(_pick_list) + 1, _pi, _site, _dh, _rack))
            # --- Auto-select DEFAULT_SITE tickets if env is set ---
            _default_site = os.environ.get("DEFAULT_SITE", "").strip()
            if _default_site:
                _my_site_idx = set(
                    i for i, _, s, _, _ in _pick_list
                    if s.lower() == _default_site.lower()
                    or re.sub(r'^US-', '', s).lower() == re.sub(r'^US-', '', _default_site).lower()
                )
                if _my_site_idx:
                    _selected = _my_site_idx
                else:
                    _selected = set(i for i, *_ in _pick_list)  # fallback: all
            else:
                _selected = set(i for i, *_ in _pick_list)  # all selected by default

            _cursor = 1  # 1-indexed cursor position for arrow-key nav
            _use_raw = sys.stdin.isatty()  # arrow keys only work in a real terminal

            # --- Helper: match a site name loosely ---
            def _match_site(query, site_val):
                if site_val == "?":
                    return False
                q = query.lower().strip()
                s = site_val.lower()
                return q == s or q == re.sub(r'^us-', '', s)

            # --- Interactive pick loop ---
            while True:
                _clear_screen()
                _sel_count = len(_selected)
                _mode_hint = f"  {DIM}(arrow keys + space to toggle, enter to start){RESET}" if _use_raw else ""
                print(f"\n  {BOLD}Bulk Start — {_sel_count}/{len(_pick_list)} selected{RESET}{_mode_hint}\n")
                for _idx, _pi, _site, _dh, _rack in _pick_list:
                    _pf = _pi.get("fields") or {}
                    _tag = str(_pf.get("customfield_10193") or "")[:18] or _pi.get("key", "?")
                    _check = f"{GREEN}■{RESET}" if _idx in _selected else f"{DIM}□{RESET}"
                    _num = f"{WHITE}{_idx:>2}{RESET}"
                    _row_dim = "" if _idx in _selected else DIM
                    _cur_marker = f"{CYAN}▸{RESET}" if _idx == _cursor and _use_raw else " "
                    print(f"  {_cur_marker}{_check} {_num}  {_row_dim}{YELLOW}{_site:<16}{RESET} {_row_dim}{CYAN}{_dh:<5}{RESET} {_row_dim}{WHITE}{_rack:<5}{RESET} {_row_dim}{DIM}{_tag}{RESET}")

                # Summary line
                _site_sel = sorted(set(s for i, _, s, _, _ in _pick_list if i in _selected and s != "?"))
                _dh_sel = sorted(set(dh for i, _, _, dh, _ in _pick_list if i in _selected and dh != "?"))
                _rack_sel = sorted(set(r for i, _, _, _, r in _pick_list if i in _selected and r != "?"),
                                   key=lambda x: int(re.search(r'\d+', x).group()) if re.search(r'\d+', x) else 0)
                _sum_parts = []
                if _site_sel:
                    _sum_parts.append(f"Sites: {', '.join(_site_sel)}")
                if _dh_sel:
                    _sum_parts.append(f"Halls: {', '.join(_dh_sel)}")
                if _rack_sel:
                    _sum_parts.append(f"Racks: {', '.join(_rack_sel)}")
                if _sum_parts:
                    print(f"\n  {BOLD}{' · '.join(_sum_parts)}{RESET}")

                # Available halls and sites for filter hints — include KNOWN_SITES
                _all_halls = sorted(set(dh for _, _, _, dh, _ in _pick_list if dh != "?"))
                _all_sites = sorted(set(s for _, _, s, _, _ in _pick_list if s != "?"))
                # Merge KNOWN_SITES into the hint so user sees their site even if no tickets
                _hint_sites = sorted(set(_all_sites) | set(KNOWN_SITES))
                _filter_hints = []
                if _all_halls:
                    _filter_hints.append(f"hall: {'/'.join(_all_halls)}")
                if _hint_sites:
                    _short_sites = [re.sub(r'^US-', '', s) for s in _hint_sites]
                    _filter_hints.append(f"site: {'/'.join(_short_sites)}")
                _filter_line = f"  Filter: {' · '.join(_filter_hints)}" if _filter_hints else ""
                _my_site_hint = f"  [m]y site ({re.sub(r'^US-', '', _default_site)})" if _default_site else ""
                if _use_raw:
                    print(f"\n  {DIM}↑↓ move  SPACE toggle  ENTER start  [a]ll  [n]one{_my_site_hint}  [q] cancel{RESET}")
                else:
                    print(f"\n  {DIM}Toggle: [1-{len(_pick_list)}]  [a]ll  [n]one{_my_site_hint}  [y] start  [q] cancel{RESET}")
                if _filter_line:
                    print(f"  {DIM}{_filter_line}{RESET}")

                # --- Read input ---
                if _use_raw:
                    _key = _read_key()
                    if _key is None:
                        _use_raw = False  # fall back to line input next loop
                        continue
                    _pcmd = _key
                else:
                    try:
                        _pcmd = input(f"\n  > ").strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        _pcmd = "q"

                # --- Handle arrow-key navigation ---
                if _pcmd == 'up':
                    _cursor = max(1, _cursor - 1)
                    continue
                elif _pcmd == 'down':
                    _cursor = min(len(_pick_list), _cursor + 1)
                    continue
                elif _pcmd == 'space':
                    _selected.symmetric_difference_update({_cursor})
                    # auto-advance cursor after toggle
                    if _cursor < len(_pick_list):
                        _cursor += 1
                    continue
                elif _pcmd == 'enter':
                    _pcmd = 'y'  # treat enter as confirm

                if _pcmd == "q":
                    print(f"  {DIM}Cancelled.{RESET}")
                    _brief_pause()
                    break
                elif _pcmd == "a":
                    _selected = set(i for i, *_ in _pick_list)
                elif _pcmd == "n":
                    _selected.clear()
                elif _pcmd == "m" and _default_site:
                    # My site — select only tickets matching DEFAULT_SITE
                    _my = set(i for i, _, s, _, _ in _pick_list if _match_site(_default_site, s))
                    if _my:
                        _selected = _my
                    else:
                        print(f"  {YELLOW}No tickets for {_default_site} in this batch.{RESET}")
                        _brief_pause(1)
                elif _pcmd.startswith("dh") and _pcmd.upper() in {dh for _, _, _, dh, _ in _pick_list if dh != "?"}:
                    # Hall filter — select only tickets matching this hall
                    _filt_dh = _pcmd.upper()
                    _selected = set(i for i, _, _, dh, _ in _pick_list if dh == _filt_dh)
                elif any(_match_site(_pcmd, s) for _, _, s, _, _ in _pick_list):
                    # Site filter — select only tickets matching this site
                    _filt_site = None
                    for _, _, s, _, _ in _pick_list:
                        if _match_site(_pcmd, s):
                            _filt_site = s
                            break
                    if _filt_site:
                        _selected = set(i for i, _, s, _, _ in _pick_list if s == _filt_site)
                elif _pcmd == "y":
                    if not _selected:
                        print(f"  {YELLOW}Nothing selected.{RESET}")
                        _brief_pause(1)
                        continue
                    # Execute transitions on selected tickets
                    _to_start = [pi for idx, pi, *_ in _pick_list if idx in _selected]
                    _p_ok = _p_fail = 0
                    print()
                    for _pi in _to_start:
                        _pctx = {"issue_key": _pi["key"], "_transitions": None}
                        print(f"  {DIM}Starting {_pi['key']}...{RESET}", end="", flush=True)
                        if _execute_transition(_pctx, "start", email, token):
                            print(f"\r  {GREEN}{BOLD}✓{RESET} {_pi['key']} → In Progress          ")
                            _p_ok += 1
                        else:
                            print(f"\r  {YELLOW}✗{RESET} {_pi['key']} — could not start        ")
                            _p_fail += 1

                    if _p_ok:
                        _log_event("bulk_start", "", "", f"{_p_ok}/{len(_to_start)} tickets started from queue")
                    print(f"\n  {GREEN}{BOLD}{_p_ok} started{RESET}", end="")
                    if _p_fail:
                        print(f"  {YELLOW}{_p_fail} failed{RESET}", end="")
                    print()
                    _brief_pause()
                    break
                else:
                    # Try parsing as number(s) — supports "3", "1 5 7", "2,4,6", "3-8"
                    _toggled = set()
                    for _part in re.split(r'[\s,]+', _pcmd):
                        _range_m = re.match(r'^(\d+)-(\d+)$', _part)
                        if _range_m:
                            _lo, _hi = int(_range_m.group(1)), int(_range_m.group(2))
                            _toggled.update(range(_lo, _hi + 1))
                        elif _part.isdigit():
                            _toggled.add(int(_part))
                    for _t in _toggled:
                        if 1 <= _t <= len(_pick_list):
                            _selected.symmetric_difference_update({_t})
            continue  # re-fetch to show updated statuses

        if chosen == "s":
            _clear_screen()
            print(f"\n  {BOLD}Sort by:{RESET}")
            _sort_opts = list(_SORT_LABELS.items())
            for _si, (_sk, _sl) in enumerate(_sort_opts, 1):
                _cur = f"  {CYAN}← current{RESET}" if _sk == _sort_by else ""
                print(f"    {BOLD}{_si}{RESET}  {_sl}{_cur}")
            try:
                _sraw = input(f"\n  Pick [1-{len(_sort_opts)}] or ENTER to cancel: ").strip()
                if _sraw:
                    _sidx = int(_sraw) - 1
                    if 0 <= _sidx < len(_sort_opts):
                        _sort_by = _sort_opts[_sidx][0]
            except (ValueError, EOFError, KeyboardInterrupt):
                pass
            continue

        if chosen == "f":
            _clear_screen()
            print(f"\n  {BOLD}Filter by:{RESET}  {DIM}(ENTER to skip a field, 'x' to clear){RESET}\n")
            _filter_fields = [
                ("status",   "Status",   "e.g. in progress, awaiting, verification"),
                ("assignee", "Assignee", "mine, unassigned, or a name"),
                ("rack",     "Rack #",   "e.g. 35"),
                ("node",     "Node #",   "e.g. 7"),
            ]
            for _fk, _flabel, _fhint in _filter_fields:
                _cur = f"  {CYAN}[{_col_filters[_fk]}]{RESET}" if _fk in _col_filters else ""
                try:
                    _fval = input(f"  {_flabel:<10} {DIM}({_fhint}){RESET}{_cur}: ").strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if _fval == "x":
                    _col_filters.pop(_fk, None)
                elif _fval:
                    _col_filters[_fk] = _fval
            continue

        if chosen == "ai":
            # Build queue context for AI from current listing
            q_lines = [f"{project} queue for {site or 'all sites'} — filter: {status_filter}"]
            for i, iss in enumerate(issues[:20], 1):
                f = iss.get("fields") or {}
                st_name = (f.get("status", {}).get("name", "?") if isinstance(f.get("status"), dict)
                           else str(f.get("status", "?")))
                q_lines.append(f"  {i}. {iss.get('key', '?')} [{st_name}] — {f.get('summary', '?')}")
            _ai_dispatch(email=email, token=token, queue_info="\n".join(q_lines))
            continue

        if not chosen:
            return "menu"

        # Drill into the selected ticket
        key = chosen["key"]
        print(f"\n  Fetching {key}...\n")
        issue = _jira_get_issue(key, email, token)
        ctx = _build_context(key, issue, email, token)

        _log_event("view", ctx["issue_key"], ctx.get("summary", ""))

        # Record this ticket view in persistent state
        st = _load_user_state()
        st = _record_ticket_view(st, ctx["issue_key"], ctx.get("summary", ""),
                                    assignee=ctx.get("assignee"), updated=ctx.get("updated"))
        _save_user_state(st)

        _clear_screen()
        _print_pretty(ctx)

        # After viewing, offer follow-up actions
        from cwhelper.tui.actions import _post_detail_prompt  # lazy — avoids circular import
        action = _post_detail_prompt(ctx, email, token, state=st)

        # Handle "history" inline — return to ticket if user backs out of history
        while action == "history":
            tag = ctx.get("service_tag") or ctx.get("hostname")
            if not tag:
                break
            h_action = _run_history_interactive(email, token, tag)
            if h_action == "quit":
                return "quit"
            if h_action == "menu":
                return "menu"
            # "back" from history → re-render this ticket
            _clear_screen()
            _print_pretty(ctx)
            action = _post_detail_prompt(ctx, email, token, state=st)

        if action in ("quit", "menu"):
            return action
        # "back" → loop back to re-display the queue



def _search_node_history(identifier: str, email: str, token: str,
                         limit: int = 20, use_cache: bool = True) -> list:
    """Find all DO + HO tickets for a service tag or hostname.

    JQL searches across:
      - cf[10193] (service_tag)
      - cf[10192] (hostname)
      - text (summary + description) as fallback
    Ordered by created DESC so newest tickets appear first.
    """
    projects = ", ".join(f'"{p}"' for p in SEARCH_PROJECTS)
    jql = (
        f'project in ({projects}) AND '
        f'(cf[10193] ~ "{_escape_jql(identifier)}" '     # service_tag
        f'OR cf[10192] ~ "{_escape_jql(identifier)}" '   # hostname
        f'OR text ~ "{_escape_jql(identifier)}") '       # summary/description fallback
        f'ORDER BY created DESC'
    )
    return _jql_search(
        jql, email, token, max_results=limit, use_cache=use_cache,
        fields=[
            "key", "summary", "status", "issuetype", "created",
            "customfield_10193",   # service_tag
            "customfield_10207",   # rack_location
            "customfield_10194",   # site
            "assignee",
        ],
    )



def _run_history_interactive(email: str, token: str, identifier: str,
                             limit: int = 20):
    """Show all tickets for a node (by service tag or hostname).

    Returns "quit" or "menu" to propagate upward. "back" is handled
    internally by re-displaying the history list.
    """
    _first_load = True
    while True:
        _clear_screen()
        print(f"\n  {BOLD}Node history for '{identifier}'{RESET}  (limit {limit})\n")

        issues = _search_node_history(identifier, email, token, limit=limit,
                                       use_cache=_first_load)
        _first_load = False  # subsequent refreshes bypass cache

        if not issues:
            print(f"  {YELLOW}{BOLD}No tickets{RESET} {DIM}found for '{identifier}'.{RESET}")
            print(f"  {DIM}This node may not have any DO/HO tickets yet.{RESET}")
            return "back"

        def _history_label(i, iss):
            f = iss.get("fields", {})
            st = f.get("status", {}).get("name", "?")
            created = f.get("created", "")[:10]  # just the date
            sc, sd = _status_color(st)
            return (
                f"  {BOLD}{i:>2}.{RESET}  {iss['key']}  "
                f"{sc}{sd} {st:<20}{RESET} "
                f"{DIM}{created}{RESET}  "
                f"{f.get('summary', '')}"
            )

        chosen = _prompt_select(issues, _history_label)

        if chosen == "refresh":
            continue
        if chosen == "quit":
            return "quit"
        if chosen == "menu":
            return "menu"
        if not chosen:
            return "back"

        key = chosen["key"]
        print(f"\n  Fetching {key}...\n")
        _cfg._issue_cache.pop(key, None)  # always fetch fresh from history view
        issue = _jira_get_issue(key, email, token)
        ctx = _build_context(key, issue, email, token)

        # Record this ticket view in persistent state
        st = _load_user_state()
        st = _record_ticket_view(st, ctx["issue_key"], ctx.get("summary", ""),
                                    assignee=ctx.get("assignee"), updated=ctx.get("updated"))
        _save_user_state(st)

        _clear_screen()
        _print_pretty(ctx)

        from cwhelper.tui.actions import _post_detail_prompt  # lazy — avoids circular import
        action = _post_detail_prompt(ctx, email, token, state=st)

        # Handle "history" inline — return to ticket if user backs out of history
        while action == "history":
            tag = ctx.get("service_tag") or ctx.get("hostname")
            if not tag:
                break
            h_action = _run_history_interactive(email, token, tag)
            if h_action == "quit":
                return "quit"
            if h_action == "menu":
                return "menu"
            _clear_screen()
            _print_pretty(ctx)
            action = _post_detail_prompt(ctx, email, token, state=st)

        if action in ("quit", "menu"):
            return action
        # "back" → loop back to re-display the history list



def _run_history_json(email: str, token: str, identifier: str,
                      limit: int = 20):
    """Dump node history as JSON."""
    issues = _search_node_history(identifier, email, token, limit=limit)
    out = []
    for iss in issues:
        f = iss.get("fields", {})
        out.append({
            "key": iss["key"],
            "status": f.get("status", {}).get("name", "?"),
            "created": f.get("created", "")[:10],
            "service_tag": _unwrap_field(f.get("customfield_10193")),
            "rack_location": _unwrap_field(f.get("customfield_10207")),
            "site": _unwrap_field(f.get("customfield_10194")),
            "summary": f.get("summary", ""),
        })
    print(json.dumps(out, indent=2))



def _run_queue_json(email: str, token: str, site: str,
                    mine_only: bool = False, limit: int = 20,
                    status_filter: str = "open", project: str = "DO"):
    """Non-interactive queue dump as JSON."""
    issues = _search_queue(site, email, token, mine_only=mine_only, limit=limit,
                           status_filter=status_filter, project=project)
    out = []
    for iss in issues:
        f = iss.get("fields", {})
        out.append({
            "key": iss["key"],
            "status": f.get("status", {}).get("name", "?"),
            "service_tag": _unwrap_field(f.get("customfield_10193")),
            "rack_location": _unwrap_field(f.get("customfield_10207")),
            "site": _unwrap_field(f.get("customfield_10194")),
            "summary": f.get("summary", ""),
            "assignee": f.get("assignee", {}).get("displayName") if f.get("assignee") else None,
        })
    print(json.dumps(out, indent=2))


