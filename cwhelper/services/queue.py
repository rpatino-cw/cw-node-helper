"""Queue browser and history search."""
from __future__ import annotations

import re
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

    def _apply_col_filters(lst: list) -> list:
        out = []
        for iss in lst:
            f = iss.get("fields") or {}
            st_name = (f.get("status", {}).get("name", "") if isinstance(f.get("status"), dict)
                       else str(f.get("status", ""))).lower()
            assignee_obj = f.get("assignee")
            asgn = (assignee_obj.get("displayName", "") if isinstance(assignee_obj, dict)
                    else str(assignee_obj or "")).lower()
            rack_loc = str(f.get("customfield_10207") or "")
            hostname = str(f.get("customfield_10192") or "")
            rack_m = re.search(r'\.R(\d+)\.', rack_loc)
            node_m = re.search(r'-node-(\d+)', hostname)
            rack_num = int(rack_m.group(1)) if rack_m else None
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
            m = re.search(r'\.R(\d+)\.', str(f.get("customfield_10207") or ""))
            return int(m.group(1)) if m else 9999

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
        _hints += ["f filter", "s sort"]
        if _col_filters or _sort_by != "created":
            _hints.append("r reset")

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
            if raw_input.lower() in ("x", "n", "a", "e", "f", "s", "r", "l") or raw_input == "*":
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


