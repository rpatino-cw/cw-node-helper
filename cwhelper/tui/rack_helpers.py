"""Rack-level ticket helpers — conflict checks, bulk grab/hold/link actions."""
from __future__ import annotations

import datetime as _dt

from cwhelper import config as _cfg
from cwhelper.config import BOLD, DIM, RESET, CYAN, GREEN, YELLOW, RED
from cwhelper.clients.jira import _jira_get, _execute_transition, _grab_ticket, _jira_link_issues
from cwhelper.services.context import _format_age

__all__ = [
    '_find_related_tickets', '_hold_ticket_by_key',
    '_check_rack_tickets', '_show_rack_suggestions',
]


def _find_related_tickets(ctx: dict, email: str, token: str,
                          window_minutes: int = 15) -> list:
    """Find tickets for the same node created within window_minutes of this one."""
    from cwhelper.services.search import _jql_search
    from cwhelper.cache import _escape_jql as _esc
    tag = ctx.get("service_tag", "")
    current_key = ctx.get("issue_key", "")
    created_str = ctx.get("created", "")
    if not tag or not created_str:
        return []
    try:
        created_ts = _dt.datetime.fromisoformat(
            created_str.replace("Z", "+00:00")).timestamp()
    except Exception:
        return []
    results = _jql_search(
        f'cf[10193] = "{_esc(tag)}" AND key != "{current_key}" ORDER BY created DESC',
        email, token, max_results=20,
        fields=["key", "summary", "status", "created", "issuetype", "assignee"],
    )
    related = []
    for iss in results:
        iss_created = (iss.get("fields") or {}).get("created", "")
        try:
            iss_ts = _dt.datetime.fromisoformat(
                iss_created.replace("Z", "+00:00")).timestamp()
            if abs(iss_ts - created_ts) <= window_minutes * 60:
                related.append(iss)
        except Exception:
            pass
    return related


def _hold_ticket_by_key(key: str, status: str, email: str, token: str) -> str:
    """Transition a ticket to On Hold. Two-step if currently in Awaiting Support.

    Fetches live assignee + status from Jira before acting — skips if the
    ticket is not assigned to the current user (prevents orphaning tickets).

    Returns: 'ok' | 'skip' (already on hold / not mine) | 'fail'
    """
    # Fetch live state so we don't act on stale info
    try:
        resp = _jira_get(f"/rest/api/3/issue/{key}?fields=assignee,status", email, token)
        if resp and resp.ok:
            _fields    = resp.json().get("fields", {})
            _assignee  = (_fields.get("assignee") or {})
            _acct      = _assignee.get("accountId", "")
            # Skip if not assigned to current user
            my_id = _cfg._my_account_id
            if not _acct or (my_id and _acct != my_id):
                return "skip"
            status_lower = (_fields.get("status") or {}).get("name", status).lower()
        else:
            status_lower = (status or "").lower()
    except Exception:
        status_lower = (status or "").lower()

    if "hold" in status_lower:
        return "skip"

    mini_ctx = {"issue_key": key, "_transitions": None}
    # From Awaiting Support we must start work first (per status flow diagram)
    if status_lower not in ("in progress",):
        if not _execute_transition(mini_ctx, "start", email, token):
            return "fail"
        mini_ctx["_transitions"] = None  # clear cache so re-fetch for In Progress transitions
    return "ok" if _execute_transition(mini_ctx, "hold", email, token) else "fail"


def _check_rack_tickets(ctx: dict, email: str, token: str) -> dict:
    """Check for open unassigned tickets and active colleagues in the same rack/DH.

    Returns:
        {
            "rack_open":    [...],   # open unassigned tickets in same rack
            "dh_open":      [...],   # open unassigned tickets in same DH, different rack
            "others_active": {       # another DCT with fresh (<3h) In Progress work in same rack
                "name": str,
                "tickets": [...],
                "age_secs": int,
            } | None,
            "rack_label": str,       # e.g. "R273"
            "dh_label":   str,       # e.g. "DH1"
        }
    """
    from cwhelper.services.search import _jql_search
    from cwhelper.services.context import _parse_rack_location, _parse_jira_timestamp

    rack_loc    = ctx.get("rack_location", "")
    current_key = ctx.get("issue_key", "")
    if not rack_loc:
        return {}

    parsed = _parse_rack_location(rack_loc)
    if not parsed or not parsed.get("dh") or parsed.get("rack") is None:
        return {}

    dh        = parsed["dh"]                          # e.g. "DH1"
    rack_num  = parsed["rack"]                        # e.g. 273
    row_tens  = rack_num // 10                        # e.g. 27
    row_base  = row_tens * 10
    row_label = f"R{row_base}–R{row_base + 9}"       # e.g. "R270–R279"
    site      = ctx.get("site", "")                   # e.g. "US-SITE01"
    result    = {
        "rack_label": f"R{rack_num}", "dh_label": dh,
        "rack_open": [], "row_open": [], "dh_open": [],
        "row_label": row_label, "others_active": None,
    }

    _open_statuses = '"Open","Awaiting Support","Awaiting Triage","To Do","New"'

    # Build the site clause using cf[10194] (exact match) — same approach as the queue browser.
    # Avoids Lucene tokenization issues with cf[10207] ~ "DH1.R273" (dots are word separators).
    site_clause = f'cf[10194] = "{site}"' if site else f'cf[10207] ~ "{parsed["site_code"]}"'

    # --- One query: all open unassigned tickets at this site, filter client-side by rack/row/DH ---
    try:
        all_site_open = _jql_search(
            f'project = "DO" AND {site_clause} '
            f'AND status in ({_open_statuses}) AND assignee is EMPTY '
            f'AND key != "{current_key}" ORDER BY created ASC',
            email, token, max_results=150, use_cache=False,
            fields=["key", "summary", "customfield_10193", "customfield_10207"],
        )
        for iss in all_site_open:
            loc = (iss.get("fields", {}).get("customfield_10207") or "")
            if isinstance(loc, dict):
                loc = loc.get("value", "") or ""
            p = _parse_rack_location(str(loc))
            if not p:
                continue
            iss_rack = p.get("rack")
            if iss_rack == rack_num:
                result["rack_open"].append(iss)
            elif iss_rack is not None and iss_rack // 10 == row_tens:
                result["row_open"].append(iss)
            else:
                result["dh_open"].append(iss)
    except Exception:
        pass

    # --- Another DCT with fresh In Progress work in the same rack ---
    try:
        active = _jql_search(
            f'project = "DO" AND {site_clause} '
            f'AND status = "In Progress" AND assignee != currentUser() '
            f'ORDER BY statuscategorychangedate DESC',
            email, token, max_results=50, use_cache=False,
            fields=["key", "assignee", "statuscategorychangedate", "customfield_10207"],
        )
        # Filter client-side to same rack
        rack_active = []
        for iss in active:
            loc = (iss.get("fields", {}).get("customfield_10207") or "")
            if isinstance(loc, dict):
                loc = loc.get("value", "") or ""
            p = _parse_rack_location(str(loc))
            if p and p.get("rack") == rack_num:
                rack_active.append(iss)
        if rack_active:
            by_assignee: dict[str, list] = {}
            for iss in rack_active:
                name = ((iss.get("fields") or {}).get("assignee") or {}).get("displayName", "Unknown")
                by_assignee.setdefault(name, []).append(iss)
            top_name    = max(by_assignee, key=lambda k: len(by_assignee[k]))
            top_tickets = by_assignee[top_name]
            ages = [
                _parse_jira_timestamp((i.get("fields") or {}).get("statuscategorychangedate", ""))
                for i in top_tickets
            ]
            min_age = min((a for a in ages if a > 0), default=None)
            if min_age is not None and min_age < 3 * 3600:
                result["others_active"] = {"name": top_name, "tickets": top_tickets, "age_secs": min_age}
    except Exception:
        pass

    return result


def _show_rack_suggestions(ctx: dict, email: str, token: str, reassigned_from: str = "") -> None:
    """After grabbing a ticket, surface nearby open tickets and rack-conflict warnings."""
    data         = _check_rack_tickets(ctx, email, token)
    rack_open    = data.get("rack_open", [])
    row_open     = data.get("row_open", [])
    dh_open      = data.get("dh_open", [])
    others       = data.get("others_active")
    rack_label   = data.get("rack_label", "this rack")
    row_label    = data.get("row_label", "this row")
    dh_label     = data.get("dh_label", "this DH")

    if not rack_open and not row_open and not dh_open and not others:
        return

    print(f"\n  {DIM}{'─' * 50}{RESET}")

    # Warn if another DCT has fresh active work here (skip when we just reassigned from someone)
    if others and not reassigned_from:
        first_name = others["name"].split()[0]
        count      = len(others["tickets"])
        age_str    = _format_age(others["age_secs"])
        print(f"  {YELLOW}⚠  {first_name} has {count} active ticket{'s' if count != 1 else ''} "
              f"in {rack_label} (started {age_str} ago){RESET}")
        print(f"  {DIM}They may already be on-site — confirm before heading there.{RESET}")

    # Offer to bulk-grab open tickets in the same rack
    if rack_open:
        count = len(rack_open)
        print(f"\n  {CYAN}{count} more open ticket{'s' if count != 1 else ''} in {rack_label}:{RESET}")
        for iss in rack_open[:5]:
            f    = iss.get("fields", {})
            tag  = f.get("customfield_10193") or "—"
            tag  = tag.get("value", tag) if isinstance(tag, dict) else tag
            smry = f.get("summary", "")[:42]
            print(f"    {DIM}{iss['key']}  {tag}  {smry}{RESET}")
        if len(rack_open) > 5:
            print(f"    {DIM}... and {len(rack_open) - 5} more{RESET}")
        try:
            ans = input(f"\n  Grab all {count}? [{GREEN}y{RESET}/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        if ans == "y":
            ok = fail = 0
            rack_grabbed = []
            for iss in rack_open:
                k = iss["key"]
                print(f"  {DIM}Grabbing {k}...{RESET}", end="", flush=True)
                if _grab_ticket(k, email, token):
                    print(f"\r  {GREEN}✓{RESET} {k} grabbed          ")
                    ok += 1
                    rack_grabbed.append(k)
                else:
                    print(f"\r  {YELLOW}✗{RESET} {k} — failed         ")
                    fail += 1
            print(f"\n  {GREEN}{BOLD}{ok} grabbed{RESET}", end="")
            if fail:
                print(f"  {YELLOW}{fail} failed{RESET}", end="")
            print()

            # Offer to bulk-hold all grabbed + current ticket
            if rack_grabbed:
                hub_key = ctx.get("issue_key", "")
                all_to_hold = ([hub_key] if hub_key else []) + rack_grabbed
                try:
                    hold_ans = input(f"\n  Put all {len(all_to_hold)} {rack_label} tickets on hold? [{GREEN}y{RESET}/N]: ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    hold_ans = ""
                if hold_ans == "y":
                    h_ok = h_fail = 0
                    for k in all_to_hold:
                        print(f"  {DIM}Holding {k}...{RESET}", end="", flush=True)
                        res = _hold_ticket_by_key(k, "Awaiting Support", email, token)
                        if res == "ok":
                            print(f"\r  {GREEN}✓{RESET} {k} → On Hold        ")
                            h_ok += 1
                        else:
                            print(f"\r  {YELLOW}✗{RESET} {k} — failed        ")
                            h_fail += 1
                    print(f"\n  {GREEN}{BOLD}{h_ok} held{RESET}", end="")
                    if h_fail:
                        print(f"  {YELLOW}{h_fail} failed{RESET}", end="")
                    print()

                # Offer to link all grabbed to the current ticket as related
                if hub_key and rack_grabbed:
                    try:
                        link_ans = input(f"\n  Link all {len(rack_grabbed)} to {hub_key} as related? [{GREEN}y{RESET}/N]: ").strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        link_ans = ""
                    if link_ans == "y":
                        l_ok = l_fail = 0
                        for k in rack_grabbed:
                            print(f"  {DIM}Linking {k}...{RESET}", end="", flush=True)
                            if _jira_link_issues(hub_key, k, email, token):
                                print(f"\r  {GREEN}✓{RESET} {k} linked          ")
                                l_ok += 1
                            else:
                                print(f"\r  {YELLOW}✗{RESET} {k} — failed        ")
                                l_fail += 1
                        print(f"\n  {GREEN}{BOLD}{l_ok} linked to {hub_key}{RESET}", end="")
                        if l_fail:
                            print(f"  {YELLOW}{l_fail} failed{RESET}", end="")
                        print()

    # Offer to bulk-grab open tickets in the same row (other cabs)
    if row_open:
        count = len(row_open)
        print(f"\n  {CYAN}{count} more open ticket{'s' if count != 1 else ''} in {row_label} (other cabs in row):{RESET}")
        for iss in row_open[:5]:
            f    = iss.get("fields", {})
            tag  = f.get("customfield_10193") or "—"
            tag  = tag.get("value", tag) if isinstance(tag, dict) else tag
            loc  = (f.get("customfield_10207") or "")
            loc  = loc.get("value", loc) if isinstance(loc, dict) else str(loc)
            # Extract just the rack portion (e.g. "DH1.R271" → "R271")
            cab  = loc.split(".")[-1] if "." in loc else loc
            smry = f.get("summary", "")[:38]
            print(f"    {DIM}{iss['key']}  {cab:<6}  {tag}  {smry}{RESET}")
        if len(row_open) > 5:
            print(f"    {DIM}... and {len(row_open) - 5} more{RESET}")
        try:
            ans = input(f"\n  Grab row tickets ({count})? [{GREEN}y{RESET}/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        if ans == "y":
            ok = fail = 0
            for iss in row_open:
                k = iss["key"]
                print(f"  {DIM}Grabbing {k}...{RESET}", end="", flush=True)
                if _grab_ticket(k, email, token):
                    print(f"\r  {GREEN}✓{RESET} {k} grabbed          ")
                    ok += 1
                else:
                    print(f"\r  {YELLOW}✗{RESET} {k} — failed         ")
                    fail += 1
            print(f"\n  {GREEN}{BOLD}{ok} grabbed{RESET}", end="")
            if fail:
                print(f"  {YELLOW}{fail} failed{RESET}", end="")
            print()

    # Locality hint: more open tickets elsewhere in the same DH (outside this row)
    if dh_open:
        count = len(dh_open)
        print(f"\n  {DIM}Also {count} open ticket{'s' if count != 1 else ''} elsewhere in {dh_label} "
              f"— use Browse queue to see them.{RESET}")
