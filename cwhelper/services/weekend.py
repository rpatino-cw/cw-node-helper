"""Weekend auto-assign (round-robin)."""
from __future__ import annotations

import time

import datetime
import json
import os

from cwhelper import config as _cfg
from cwhelper.config import *  # noqa: F401,F403
__all__ = ['_is_weekend', '_fetch_group_members', '_load_robin_state', '_save_robin_state', '_weekend_auto_assign']
from cwhelper.clients.jira import _jira_get, _assign_ticket
from cwhelper.services.search import _search_queue




def _is_weekend(force: bool = False) -> bool:
    """Return True if today is Saturday or Sunday (local time), or if forced."""
    import datetime
    if force or os.environ.get("WEEKEND_FORCE", "").strip() == "1":
        return True
    return datetime.datetime.now().weekday() in (5, 6)



def _fetch_group_members(group_name: str, email: str, token: str) -> list[dict]:
    """Fetch active members of a Jira group.

    Returns list of {"accountId": ..., "displayName": ...} sorted by
    displayName for deterministic round-robin ordering.
    """
    members = []
    start_at = 0
    while True:
        resp = _jira_get(
            f"/rest/api/3/group/member?groupname={requests.utils.quote(group_name)}"
            f"&maxResults=50&startAt={start_at}",
            email, token,
        )
        if not resp or not resp.ok:
            if resp is not None and resp.status_code == 403:
                print(f"  {DIM}Permission denied reading group '{group_name}'.{RESET}")
            elif resp is not None:
                print(f"  {DIM}Failed to fetch group '{group_name}' ({resp.status_code}).{RESET}")
            break
        data = resp.json()
        for m in data.get("values", []):
            if m.get("active", True):
                members.append({
                    "accountId": m["accountId"],
                    "displayName": m.get("displayName", m["accountId"]),
                })
        if data.get("isLast", True):
            break
        start_at += len(data.get("values", []))
    members.sort(key=lambda m: m["displayName"].lower())
    return members



def _load_robin_state() -> dict:
    """Load the weekend round-robin state from .cwhelper_state.json."""
    state = _load_user_state()
    return state.get("weekend_robin", {"index": 0, "last_run": None, "assignments": []})



def _save_robin_state(robin: dict):
    """Save the weekend round-robin state back to .cwhelper_state.json."""
    robin["assignments"] = robin.get("assignments", [])[:200]
    state = _load_user_state()
    state["weekend_robin"] = robin
    _save_user_state(state)



def _weekend_auto_assign(site: str, group_name: str,
                          email: str, token: str,
                          project: str = "DO",
                          dry_run: bool = False,
                          force_weekend: bool = False) -> list[dict]:
    """Run one round of weekend auto-assignment via round-robin.

    Returns list of {"key", "assigned_to", "account_id", "ts"} dicts.
    """
    import datetime

    if not _is_weekend(force=force_weekend):
        print(f"  {DIM}Not a weekend — skipping (use --force to override).{RESET}")
        return []

    members = _fetch_group_members(group_name, email, token)
    if not members:
        print(f"  {DIM}No active members found in group '{group_name}'.{RESET}")
        return []

    issues = _search_queue(site, email, token, limit=50,
                           status_filter="open", project=project,
                           use_cache=False)

    unassigned = [
        iss for iss in issues
        if iss.get("fields", {}).get("assignee") is None
        and iss.get("fields", {}).get("status", {})
            .get("statusCategory", {}).get("key") != "done"
    ]

    if not unassigned:
        print(f"  {DIM}No unassigned tickets found.{RESET}")
        return []

    robin = _load_robin_state()
    idx = robin.get("index", 0) % len(members)
    results = []

    for iss in unassigned:
        target = members[idx % len(members)]
        key = iss["key"]

        if dry_run:
            print(f"  {DIM}[DRY RUN]{RESET} {key}  ->  {target['displayName']}")
        else:
            ok = _assign_ticket(key, target["accountId"], email, token)
            if ok:
                _post_comment(key,
                              f"Auto-assigned to {target['displayName']} (weekend rotation)",
                              email, token)
                print(f"  {GREEN}AUTO{RESET}  {key}  ->  {target['displayName']}")
            else:
                print(f"  {RED}FAIL{RESET}  {key}  ->  {target['displayName']}")
                continue  # don't advance rotation on failure

        now = datetime.datetime.utcnow().isoformat() + "Z"
        results.append({
            "key": key,
            "assigned_to": target["displayName"],
            "account_id": target["accountId"],
            "ts": now,
        })
        idx = (idx + 1) % len(members)

    robin["index"] = idx
    robin["last_run"] = datetime.datetime.utcnow().isoformat() + "Z"
    robin["assignments"] = results + robin.get("assignments", [])
    _save_robin_state(robin)

    return results


