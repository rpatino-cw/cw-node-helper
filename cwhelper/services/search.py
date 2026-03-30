"""JQL search, SLA fetch, text search, queue search."""
from __future__ import annotations

import json
import time

from cwhelper import config as _cfg
from cwhelper.config import *  # noqa: F401,F403
from cwhelper.cache import _cache_put, _escape_jql
__all__ = ['_jql_search', '_fetch_sla', '_search_by_text', '_search_queue']
from cwhelper.clients.jira import _jira_get, _jira_post, _handle_response_errors




def _jql_search(jql: str, email: str, token: str, max_results: int = 10,
                fields: list | None = None, use_cache: bool = True) -> list:
    """Run a JQL query via POST /rest/api/3/search/jql and return issues list.

    Results are cached for _JQL_CACHE_TTL seconds. Pass use_cache=False
    (e.g. from the background watcher) to always hit the API.
    """
    fields_key = tuple(sorted(fields)) if fields else ()
    cache_key = f"{jql}|{max_results}|{fields_key}"

    if use_cache and cache_key in _cfg._jql_cache:
        ts, cached_issues = _cfg._jql_cache[cache_key]
        if time.time() - ts < _JQL_CACHE_TTL:
            return cached_issues

    body = {"jql": jql, "maxResults": max_results}
    if fields:
        body["fields"] = fields

    response = _jira_post("/rest/api/3/search/jql", email, token, body=body)
    _handle_response_errors(response, f"JQL search")
    issues = response.json().get("issues", [])

    # Cache non-empty results; skip caching empty so retries always hit the API
    if issues:
        _cache_put(_cfg._jql_cache, cache_key, (time.time(), issues), _JQL_CACHE_MAX)
    # NOTE: Do NOT cache queue results in _cfg._issue_cache — queue searches use a
    # limited field list (no description/comments/issuelinks).  Caching them
    # would cause _jira_get_issue() to return partial data and hide buttons.

    return issues



def _fetch_sla(issue_key: str, email: str, token: str) -> list:
    """Fetch SLA data from Jira Service Desk API.

    Returns a list of SLA metric dicts, each with 'name', 'ongoingCycle',
    and 'completedCycles'.  Empty list if unavailable or error.
    """
    try:
        resp = _jira_get(
            f"/rest/servicedeskapi/request/{issue_key}/sla", email, token)
        if resp and resp.ok:
            return resp.json().get("values", [])
    except Exception:
        pass
    return []



def _search_by_text(query_text: str, email: str, token: str) -> list:
    """Search DO/HO/SDA projects for issues matching query_text.

    Searches across:
      - text (summary + description)
      - cf[10193] (service_tag)
      - cf[10192] (hostname)
      - assignee (display name)
    """
    projects = ", ".join(f'"{p}"' for p in SEARCH_PROJECTS)
    escaped = _escape_jql(query_text)

    # Try assignee search first if the query looks like a person name
    # (2+ words, all alpha, no digits/dashes — likely a name not a serial)
    words = query_text.strip().split()
    is_name = (len(words) >= 2 and
               all(w.replace("'", "").replace("-", "").isalpha() for w in words))

    if is_name:
        # Search by assignee (open tickets first, then all)
        jql_assignee = (
            f'project in ({projects}) AND '
            f'assignee = "{escaped}" AND statusCategory != Done '
            f'ORDER BY updated DESC'
        )
        results = _jql_search(jql_assignee, email, token, max_results=20,
                              fields=["key", "summary", "status", "issuetype", "assignee", "created", "statuscategorychangedate"])
        if results:
            return results
        # Try without status filter
        jql_assignee_all = (
            f'project in ({projects}) AND '
            f'assignee = "{escaped}" '
            f'ORDER BY updated DESC'
        )
        results = _jql_search(jql_assignee_all, email, token, max_results=20,
                              fields=["key", "summary", "status", "issuetype", "assignee", "created", "statuscategorychangedate"])
        if results:
            return results

    # Fallback: text + custom field search — open/active tickets only
    jql = (
        f'project in ({projects}) AND statusCategory != Done AND '
        f'(text ~ "{escaped}" '
        f'OR cf[10193] ~ "{escaped}" '   # service_tag field
        f'OR cf[10192] ~ "{escaped}")'    # hostname field
    )
    results = _jql_search(jql, email, token,
                          fields=["key", "summary", "status", "issuetype", "created", "statuscategorychangedate"])
    if results:
        return results
    # Retry without status filter if nothing found
    jql_all = (
        f'project in ({projects}) AND '
        f'(text ~ "{escaped}" '
        f'OR cf[10193] ~ "{escaped}" '
        f'OR cf[10192] ~ "{escaped}")'
    )
    return _jql_search(jql_all, email, token,
                       fields=["key", "summary", "status", "issuetype", "created", "statuscategorychangedate"])



def _search_queue(site: str, email: str, token: str,
                  mine_only: bool = False, limit: int = 20,
                  status_filter: str = "open",
                  project: str = "DO",
                  use_cache: bool = True) -> list:
    """Search for tickets in a project, filtered by site and status.

    status_filter can be:
      "open"         — statusCategory != Done (default)
      "closed"       — status = "Closed"
      "verification" — status = "Verification"
      "in progress"  — status = "In Progress"
      "waiting"      — status = "Waiting For Support"
      "radar"        — HO pre-DO statuses (RMA-initiate, Sent to DCT, etc.)
      "all"          — no status filter (everything)
      or any raw status name like "Reopened"
    """
    # Apply status filter
    sf = QUEUE_FILTERS.get(status_filter.lower())
    _site_escaped = _escape_jql(site) if site else ""

    def _build_status_clause() -> str:
        q = ""
        if sf is None and status_filter.lower() != "all":
            q += f'AND status = "{_escape_jql(status_filter)}" '
        elif sf:
            q += f'AND {sf} '
        return q

    def _build_jql(site_clause: str = "") -> str:
        q = f'project = "{_escape_jql(project)}" '
        q += _build_status_clause()
        q += site_clause
        if mine_only:
            q += 'AND assignee = currentUser() '
        q += 'ORDER BY created DESC'
        return q

    _fields = [
        "summary", "status", "issuetype",
        "customfield_10193",   # service_tag
        "customfield_10207",   # rack_location
        "customfield_10192",   # hostname
        "customfield_10194",   # site
        "assignee", "created", "updated", "statuscategorychangedate",
    ]

    if not _site_escaped:
        return _jql_search(
            _build_jql(), email, token, max_results=limit,
            fields=_fields, use_cache=use_cache,
        )

    # 1. Try exact match on region field (cf[10194])
    results = _jql_search(
        _build_jql(f'AND cf[10194] = "{_site_escaped}" '),
        email, token, max_results=limit,
        fields=_fields, use_cache=use_cache,
    )

    # 2. Fall back to contains on region field
    if not results:
        results = _jql_search(
            _build_jql(f'AND cf[10194] ~ "{_site_escaped}" '),
            email, token, max_results=limit,
            fields=_fields, use_cache=use_cache,
        )

    # 3. Fall back to rack_location prefix (cf[10207]) — catches LoCode mismatches
    #    e.g. site="US-RIN01" matches rack_location "US-RIN01.DH1.R35.RU28"
    if not results:
        results = _jql_search(
            _build_jql(f'AND cf[10207] ~ "{_site_escaped}" '),
            email, token, max_results=limit,
            fields=_fields, use_cache=use_cache,
        )

    return results


