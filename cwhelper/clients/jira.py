"""Jira Cloud API client — auth, CRUD, transitions."""
from __future__ import annotations

import time

import os
import sys

from cwhelper import config as _cfg
from cwhelper.cache import _cache_put, _request_with_retry
__all__ = ['_get_env_or_exit', '_get_credentials', '_jira_health_check', '_jira_get', '_jira_post', '_jira_put', '_get_my_account_id', '_get_first_name', '_post_comment', '_upload_attachment', '_grab_ticket', '_assign_ticket', '_jira_link_issues', '_get_existing_links', '_is_mine', '_text_to_adf', '_fetch_transitions', '_find_transition', '_execute_transition', '_handle_response_errors', '_jira_get_issue', '_refresh_ctx', '_fetch_site_teammates', '_jira_user_search']




# ---------------------------------------------------------------------------
# Auth & credentials
# ---------------------------------------------------------------------------

def _get_env_or_exit(var_name: str) -> str:
    """Return the value of an env var, or offer setup wizard if missing."""
    value = os.environ.get(var_name, "").strip()
    if not value:
        print(f"\n  \033[33mMissing: {var_name}\033[0m\n")
        # Offer to run the setup wizard
        try:
            answer = input("  Run the setup wizard? [Y/n]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"
        if answer in ("", "y", "yes"):
            from cwhelper.cli import _cli_setup
            _cli_setup()
            # Re-check after setup (setup writes .env and sets env vars)
            value = os.environ.get(var_name, "").strip()
            if value:
                return value
            print(f"\n  \033[33m{var_name} still not set after setup.\033[0m")
        else:
            print(f"  To set up manually:")
            print(f"    cwhelper setup")
            print(f"    # or: export {var_name}='your-value-here'\n")
        sys.exit(1)
    return value


def _get_credentials() -> tuple:
    """Return (email, token) from env vars."""
    return _get_env_or_exit("JIRA_EMAIL"), _get_env_or_exit("JIRA_API_TOKEN")


def _jira_health_check(email: str, token: str) -> bool:
    """Quick ping to Jira to verify connectivity. Returns True if reachable."""
    import requests
    try:
        resp = _cfg._session.get(
            f"{_cfg.JIRA_BASE_URL}/rest/api/3/myself",
            auth=(email, token),
            headers={"Accept": "application/json"},
            timeout=(3, 5),
        )
        return resp.status_code < 400
    except (requests.exceptions.SSLError, requests.exceptions.ConnectionError):
        return False
    except requests.RequestException:
        return False


# ---------------------------------------------------------------------------
# HTTP methods
# ---------------------------------------------------------------------------

def _jira_get(path: str, email: str, token: str, params: dict = None):
    """Make an authenticated GET to Jira and return the response object."""
    url = f"{_cfg.JIRA_BASE_URL}{path}"
    return _request_with_retry(
        _cfg._session.get,
        url,
        auth=(email, token),
        headers={"Accept": "application/json"},
        params=params,
        timeout=(5, 10),
    )


def _jira_post(path: str, email: str, token: str, body: dict):
    """Make an authenticated POST to Jira and return the response object."""
    url = f"{_cfg.JIRA_BASE_URL}{path}"
    return _request_with_retry(
        _cfg._session.post,
        url,
        auth=(email, token),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        json=body,
        timeout=(5, 10),
    )


def _jira_put(path: str, email: str, token: str, body: dict):
    """Make an authenticated PUT to Jira and return the response object."""
    url = f"{_cfg.JIRA_BASE_URL}{path}"
    return _request_with_retry(
        _cfg._session.put,
        url,
        auth=(email, token),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        json=body,
        timeout=(5, 10),
    )


# ---------------------------------------------------------------------------
# User identity
# ---------------------------------------------------------------------------

def _get_my_account_id(email: str, token: str) -> str | None:
    """Fetch and cache the current user's Jira accountId and displayName."""
    if _cfg._my_account_id:
        return _cfg._my_account_id
    resp = _jira_get("/rest/api/3/myself", email, token)
    if resp.ok:
        data = resp.json()
        _cfg._my_account_id = data.get("accountId")
        _cfg._my_display_name = data.get("displayName")
    return _cfg._my_account_id


def _get_first_name(email: str, token: str) -> str:
    """Get the user's first name for greeting. Uses state cache, then API."""
    from cwhelper.state import _load_user_state, _save_user_state

    # In-memory cache (already fetched this session)
    if _cfg._my_display_name:
        return _cfg._my_display_name.split()[0]

    # State file cache (avoids API call on startup)
    state = _load_user_state()
    cached = state.get("user", {}).get("first_name")
    if cached:
        _cfg._my_display_name = state["user"].get("display_name", cached)
        return cached

    # Trigger API call (also populates _cfg._my_display_name)
    _get_my_account_id(email, token)
    if _cfg._my_display_name:
        first = _cfg._my_display_name.split()[0]
        state["user"] = {
            "display_name": _cfg._my_display_name,
            "first_name": first,
            "account_id": _cfg._my_account_id,
        }
        _save_user_state(state)
        return first

    # Fallback: email prefix
    return email.split("@")[0].split(".")[0].capitalize()


# ---------------------------------------------------------------------------
# Issue operations
# ---------------------------------------------------------------------------

def _upload_attachment(key: str, file_path: str, email: str, token: str) -> bool:
    """Upload a file as an attachment to a Jira issue. Returns True on success."""
    import base64
    import mimetypes
    base_url = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
    url = f"{base_url}/rest/api/3/issue/{key}/attachments"
    credentials = base64.b64encode(f"{email}:{token}".encode()).decode()
    mime = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    try:
        with open(file_path, "rb") as fh:
            resp = _cfg._session.post(
                url,
                headers={
                    "Authorization": f"Basic {credentials}",
                    "X-Atlassian-Token": "no-check",
                },
                files={"file": (os.path.basename(file_path), fh, mime)},
                timeout=30,
            )
        return resp.status_code in (200, 201)
    except Exception:
        return False


def _post_comment(key: str, text: str, email: str, token: str) -> bool:
    """Post a comment on a Jira issue. Returns True on success."""
    body = {"body": _text_to_adf(text)}
    resp = _jira_post(f"/rest/api/3/issue/{key}/comment", email, token, body=body)
    return resp.status_code in (200, 201)


def _grab_ticket(key: str, email: str, token: str) -> bool:
    """Assign a ticket to the current user. Returns True on success."""
    account_id = _get_my_account_id(email, token)
    if not account_id:
        print(f"  {_cfg.DIM}Could not determine your Jira account ID.{_cfg.RESET}")
        return False
    resp = _jira_put(
        f"/rest/api/3/issue/{key}/assignee",
        email, token,
        body={"accountId": account_id},
    )
    if resp.status_code == 204:
        # Late import to avoid circular dependency
        try:
            from cwhelper.services.notifications import _ntfy_send
            _ntfy_send("Ticket Grabbed", f"{key} assigned to you", tags="hand")
        except ImportError:
            pass
        return True
    if resp.status_code == 403:
        print(f"  {_cfg.DIM}Permission denied — cannot assign {key}.{_cfg.RESET}")
        return False
    print(f"  {_cfg.DIM}Assign failed ({resp.status_code}): {resp.text[:200]}{_cfg.RESET}")
    return False


def _assign_ticket(key: str, account_id: str, email: str, token: str) -> bool:
    """Assign a ticket to any user by Jira accountId. Returns True on success."""
    resp = _jira_put(
        f"/rest/api/3/issue/{key}/assignee",
        email, token,
        body={"accountId": account_id},
    )
    if resp.status_code == 204:
        return True
    if resp.status_code == 403:
        print(f"  {_cfg.DIM}Permission denied — cannot assign {key}.{_cfg.RESET}")
        return False
    print(f"  {_cfg.DIM}Assign failed ({resp.status_code}): {resp.text[:200]}{_cfg.RESET}")
    return False


def _jira_link_issues(key_a: str, key_b: str, email: str, token: str,
                      link_type: str = "Relates") -> bool:
    """Create a Jira issue link between two tickets. Returns True on success.

    Auto-discovers the correct 'Relates' link type name from this Jira instance
    on first call (result cached) to avoid case/name mismatches.
    """
    # Resolve the actual link type name from the Jira instance
    resolved = link_type
    if link_type.lower() == "relates":
        if not _cfg._relates_link_type:
            try:
                resp = _jira_get("/rest/api/3/issueLinkType", email, token)
                if resp and resp.ok:
                    for lt in resp.json().get("issueLinkTypes", []):
                        if "relat" in lt.get("name", "").lower():
                            _cfg._relates_link_type = lt["name"]
                            break
            except Exception:
                pass
        if _cfg._relates_link_type:
            resolved = _cfg._relates_link_type

    body = {
        "type": {"name": resolved},
        "inwardIssue":  {"key": key_a},
        "outwardIssue": {"key": key_b},
    }
    resp = _jira_post("/rest/api/3/issueLink", email, token, body)
    if resp and resp.status_code == 201:
        return True
    if resp:
        print(f"  {_cfg.DIM}Link failed ({resp.status_code}): {resp.text[:200]}{_cfg.RESET}")
    return False


def _get_existing_links(key: str, email: str, token: str) -> set:
    """Return set of issue keys already linked to `key` (any direction)."""
    resp = _jira_get(f"/rest/api/3/issue/{key}?fields=issuelinks", email, token)
    if not resp or not resp.ok:
        return set()
    linked = set()
    for lnk in resp.json().get("fields", {}).get("issuelinks", []):
        if "inwardIssue" in lnk:
            linked.add(lnk["inwardIssue"]["key"])
        if "outwardIssue" in lnk:
            linked.add(lnk["outwardIssue"]["key"])
    return linked


# ---------------------------------------------------------------------------
# Teammate lookup (for hand-off feature)
# ---------------------------------------------------------------------------

def _fetch_site_teammates(site: str, email: str, token: str) -> list:
    """Return [{name, account_id}] for recent DO assignees at site, excluding self.
    Cached 60s per site.
    """
    cache_key = f"teammates:{site}"
    cached = _cfg._jql_cache.get(cache_key)
    if cached and time.time() - cached.get("_ts", 0) < 300:
        return cached.get("data", [])

    site_filter = f'AND cf[10194] = "{site}"' if site else ""
    try:
        my_aid = _cfg._my_account_id
        seen: set = set()
        teammates = []

        # Query DO + HO + SDA over last 30 days to capture all active teammates
        for proj in ("DO", "HO", "SDA"):
            resp = _jira_post("/rest/api/3/search/jql", email, token, body={
                "jql": (f'project = "{proj}" {site_filter} AND assignee is not EMPTY '
                        f'AND updated >= -30d ORDER BY updated DESC'),
                "maxResults": 100,
                "fields": ["assignee"],
            })
            if not resp or not resp.ok:
                continue
            for iss in resp.json().get("issues", []):
                asgn = (iss.get("fields") or {}).get("assignee") or {}
                aid  = asgn.get("accountId", "")
                name = asgn.get("displayName", "")
                if not aid or not name or aid in seen:
                    continue
                if my_aid and aid == my_aid:
                    continue
                seen.add(aid)
                teammates.append({"name": name, "account_id": aid})

        _cfg._jql_cache[cache_key] = {"data": teammates, "_ts": time.time()}
        return teammates
    except Exception:
        return []


def _jira_user_search(query: str, email: str, token: str) -> list:
    """Search Jira users by display name. Returns [{name, account_id}], excluding self."""
    try:
        resp = _jira_get("/rest/api/3/user/search", email, token,
                         params={"query": query, "maxResults": 8})
        if not resp or not resp.ok:
            return []
        my_aid = _cfg._my_account_id
        results = []
        for u in resp.json():
            aid  = u.get("accountId", "")
            name = u.get("displayName", "")
            if not aid or not name:
                continue
            if my_aid and aid == my_aid:
                continue
            results.append({"name": name, "account_id": aid})
        return results
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Identity check
# ---------------------------------------------------------------------------

def _is_mine(ctx: dict) -> bool:
    """Check if the ticket is assigned to the logged-in user."""
    assignee = ctx.get("assignee")
    if not assignee:
        return False
    if _cfg._my_display_name and _cfg._my_display_name.lower() == assignee.lower():
        return True
    if _cfg._my_account_id and ctx.get("_assignee_account_id") == _cfg._my_account_id:
        return True
    my_email = os.environ.get("JIRA_EMAIL", "")
    my_name = " ".join(w.capitalize() for w in my_email.split("@")[0].split("."))
    return bool(my_name and my_name.lower() == assignee.lower())


# ---------------------------------------------------------------------------
# ADF formatting
# ---------------------------------------------------------------------------

def _text_to_adf(text: str) -> dict:
    """Convert plain text into Jira Atlassian Document Format."""
    paragraphs = []
    for line in text.split("\n"):
        if line.strip():
            paragraphs.append({
                "type": "paragraph",
                "content": [{"type": "text", "text": line}],
            })
        else:
            paragraphs.append({"type": "paragraph", "content": []})
    if not paragraphs:
        paragraphs = [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]
    return {"type": "doc", "version": 1, "content": paragraphs}


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------

def _fetch_transitions(key: str, email: str, token: str) -> list[dict]:
    """Fetch available transitions for an issue from Jira."""
    resp = _jira_get(f"/rest/api/3/issue/{key}/transitions", email, token)
    if not resp or not resp.ok:
        return []
    return resp.json().get("transitions", [])


def _find_transition(transitions: list[dict], action: str) -> dict | None:
    """Find the best matching transition for a conceptual action."""
    mapping = _cfg.TRANSITION_MAP.get(action)
    if not mapping:
        return None

    for t in transitions:
        to_status = t.get("to", {}).get("name", "").lower()
        # Priority 1: exact target status match
        for kw in mapping["target_status"]:
            if kw == to_status:
                return t

    for t in transitions:
        to_status = t.get("to", {}).get("name", "").lower()
        t_name = t.get("name", "").lower()
        # Priority 2: target status contains keyword
        for kw in mapping["target_status"]:
            if kw in to_status:
                return t
        # Priority 3: transition name contains hint
        for kw in mapping["transition_hints"]:
            if kw in t_name:
                return t

    return None


def _execute_transition(ctx: dict, action: str, email: str, token: str,
                        comment_text: str = None) -> bool:
    """Execute a Jira status transition.

    Lazily fetches available transitions (cached in ctx), finds the match,
    and POSTs the transition.  Returns True on success.
    """
    key = ctx["issue_key"]

    # Lazy-fetch transitions
    if ctx.get("_transitions") is None:
        ctx["_transitions"] = _fetch_transitions(key, email, token)

    match = _find_transition(ctx["_transitions"], action)
    if not match:
        available = ", ".join(t.get("name", "?") for t in ctx["_transitions"]) or "none"
        print(f"  {_cfg.YELLOW}Transition not available. Available: {available}{_cfg.RESET}")
        return False

    body: dict = {"transition": {"id": match["id"]}}
    if comment_text:
        body["update"] = {
            "comment": [{"add": {"body": _text_to_adf(comment_text)}}]
        }

    resp = _jira_post(f"/rest/api/3/issue/{key}/transitions", email, token, body)
    if resp and resp.status_code == 204:
        return True
    if resp and resp.status_code == 400:
        try:
            msg = resp.json().get("errorMessages", [resp.text[:200]])[0]
        except Exception:
            msg = resp.text[:200]
        print(f"  {_cfg.YELLOW}Transition failed: {msg}{_cfg.RESET}")
        return False
    status = resp.status_code if resp else "no response"
    print(f"  {_cfg.YELLOW}Transition failed ({status}).{_cfg.RESET}")
    return False


# ---------------------------------------------------------------------------
# Error handling & issue fetch
# ---------------------------------------------------------------------------

def _handle_response_errors(response, context_msg: str):
    """Check for common HTTP errors and exit with a clear message."""
    if response.status_code == 401:
        print(
            "Error: Jira returned 401 Unauthorized.\n"
            "Check that JIRA_EMAIL and JIRA_API_TOKEN are correct.\n"
            "Generate a token at: https://id.atlassian.com/manage-profile/security/api-tokens"
        )
        sys.exit(1)
    if response.status_code == 404:
        print(f"Not found: {context_msg}")
        sys.exit(1)
    if not response.ok:
        print(f"Jira API error {response.status_code}: {response.text[:300]}")
        sys.exit(1)


def _jira_get_issue(key: str, email: str, token: str) -> dict:
    """Fetch a single Jira issue with only the fields we need.

    Results are cached in-memory by issue key for the process lifetime.
    Returns parsed JSON dict (not a response object).
    """
    if key in _cfg._issue_cache:
        return _cfg._issue_cache[key]

    fields_param = ",".join(_cfg.ISSUE_DETAIL_FIELDS)
    response = _jira_get(
        f"/rest/api/3/issue/{key}",
        email, token,
        params={"fields": fields_param},
    )
    _handle_response_errors(response, key)
    data = response.json()
    _cache_put(_cfg._issue_cache, key, data, _cfg._ISSUE_CACHE_MAX)
    return data


def _refresh_ctx(ctx: dict, email: str, token: str):
    """Invalidate cache, re-fetch, rebuild context in-place after a mutation."""
    # Late import to avoid circular dependency with context.py
    from cwhelper.services.context import _build_context

    key = ctx["issue_key"]
    identifier = ctx.get("identifier", key)
    _cfg._issue_cache.pop(key, None)
    _cfg._jql_cache.clear()

    issue = _jira_get_issue(key, email, token)
    new_ctx = _build_context(identifier, issue, email, token)
    new_ctx["_transitions"] = None  # force re-fetch next time

    # Preserve display toggles
    for toggle in ("_show_comments", "_show_desc", "_show_diags", "_show_sla"):
        if ctx.get(toggle):
            new_ctx[toggle] = ctx[toggle]

    ctx.clear()
    ctx.update(new_ctx)
