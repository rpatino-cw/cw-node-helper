#!/usr/bin/env python3
"""
get_node_context.py — CoreWeave DCT helper (v5)

Modes:
  LOOKUP   — look up a single node/ticket by key, service tag, or hostname.
  QUEUE    — browse open DO/HO tickets for a site.
  HISTORY  — see all tickets for a node over time.

Data sources:
  - Jira Cloud  (required) — tickets, custom fields, comments
  - NetBox      (optional) — authoritative rack/site, interfaces, cabling

Environment variables required:
    JIRA_EMAIL      — your CoreWeave email (e.g. first.last@coreweave.com)
    JIRA_API_TOKEN  — a personal Jira API token (generate at id.atlassian.com)

Environment variables optional (NetBox enrichment):
    NETBOX_API_URL   — e.g. https://coreweave-dev.cloud.netboxapp.com/api
    NETBOX_API_TOKEN — your NetBox API token
"""

from __future__ import annotations   # allows str | None on Python 3.9

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import queue as queue_mod
import re
import select
import subprocess
import html as html_mod
import sys
import textwrap
import threading
import time
import webbrowser

try:
    import requests
except ImportError:
    print("Error: 'requests' module not found. Install with: pip install requests")
    sys.exit(1)

try:
    import openai as _openai_mod
    _HAS_OPENAI = True
except ImportError:
    _openai_mod = None
    _HAS_OPENAI = False

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

APP_VERSION = "6.3.0"

JIRA_BASE_URL = "https://coreweave.atlassian.net"

# Jira issue key pattern: uppercase letters, dash, digits (e.g. DO-12345)
JIRA_KEY_PATTERN = re.compile(r"^[A-Z]+-\d+$")

# Known custom field IDs discovered from real DO/HO ticket JSON.
CUSTOM_FIELDS = {
    "customfield_10207": "rack_location",   # cf[10207] — e.g. "US-BVI01.DC7.R297.RU18"
    "customfield_10193": "service_tag",     # cf[10193] — e.g. "10NQ724"
    "customfield_10192": "hostname",        # cf[10192] — e.g. "d0001142"
    "customfield_10194": "site",            # cf[10194] — e.g. "US-EAST-03"
    "customfield_10191": "ip_address",      # cf[10191] — e.g. "0.0.0.0"
    "customfield_10210": "vendor",          # cf[10210] — e.g. "Dell" (plain string)
}

# Projects to search when looking up by serial/hostname.
SEARCH_PROJECTS = ["DO", "HO", "SDA"]

# SDx (Service Desk) project keys — used by _show_sdx_for_ticket() to identify
# customer-facing tickets linked to DO/HO work orders.
SDX_PROJECTS = {"SDA", "SDE", "SDO", "SDP", "SDS"}

# Fields requested for full issue fetch (reduces payload vs. fetching all ~50+ fields).
ISSUE_DETAIL_FIELDS = [
    "summary", "status", "issuetype", "project", "assignee", "reporter", "priority",
    "customfield_10207", "customfield_10193", "customfield_10192",
    "customfield_10194", "customfield_10191", "customfield_10210",
    "customfield_10010", "description", "comment", "issuelinks",
    "created", "updated", "statuscategorychangedate",
]

# Known site strings (from Jira cf[10194] values seen in real tickets).
# Add new sites here as you discover them.
KNOWN_SITES = [
    "US-CENTRAL-07A",   # Elk Grove (The Elks)
    "US-CENTRAL-01A",   # Volo (VO201 / ORD3)
    "US-EAST-03",
    "US-EAST-03A",
    "US-EAST-13A",      # Caledonia / Grand Rapids
    # NOTE: US-PHX01, US-QNC01, US-RIN01, US-EWS01 removed — their Jira
    # region/site field (cf[10194]) uses different values. Re-add once
    # the actual cf[10194] values are confirmed from real tickets.
]

# ANSI colors (used across output functions)
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
WHITE = "\033[97m"
MAGENTA = "\033[35m"
BLUE = "\033[34m"
UNDERLINE = "\033[4m"

# ---------------------------------------------------------------------------
# Status transition mapping — maps conceptual actions to Jira transition
# target status keywords.  We match against the transition's "to" status name.
# ---------------------------------------------------------------------------
TRANSITION_MAP = {
    "start": {
        "target_status": ["in progress"],
        "transition_hints": ["start", "begin", "in progress"],
    },
    "verify": {
        "target_status": ["verification"],
        "transition_hints": ["verif", "review"],
    },
    "hold": {
        "target_status": ["on hold", "blocked", "paused", "waiting for support"],
        "transition_hints": ["hold", "block", "pause", "wait"],
    },
    "resume": {
        "target_status": ["in progress"],
        "transition_hints": ["resume", "restart", "back to", "in progress", "reopen"],
    },
    "close": {
        "target_status": ["closed", "done", "resolved"],
        "transition_hints": ["close", "done", "resolve", "complete"],
    },
}

# ---------------------------------------------------------------------------
# AI Assistant (OpenAI) — optional
# ---------------------------------------------------------------------------
AI_MODEL = "gpt-4o"
AI_MAX_TOKENS = 1024
AI_TEMPERATURE = 0.3

_AI_DOMAIN_KNOWLEDGE = (
    "\n\n--- CoreWeave Jira Ticket System ---\n"
    "PROJECTS:\n"
    "- DO (Data Operations): Hands-on DCT work — reseat, swap, cable, power cycle, inspections. "
    "Created when physical site work is needed. DCTs pick these up.\n"
    "- HO (HPC Ops): Central ticket for a node's hardware problem, troubleshooting, and RMA history. "
    "Usually auto-created when a node enters triage (cwctl nlcc <nodeId> -s triage). "
    "Used by Fleet/FROps/FRR and TPMs to drive diagnosis, vendor RMA lifecycle, uncabling/recabling. "
    "Often linked to SDx customer-facing tickets and spawns DO tickets for DCT work.\n"
    "- SDA (Service Desk Albatross): Albatross bare-metal customer (Microsoft/OpenAI stack) hardware incidents. "
    "Tracks down/unavailable servers, NVLink issues, PSU/drive failures, cabling. "
    "Filed by Albatross via API or CW engineers. Used to compute SLA credits.\n"
    "- SDx projects (SDE, SDO, SDP, SDS): Other customer-facing service desk tickets.\n\n"
    "RELATIONSHIPS:\n"
    "- HO tickets are the 'home' for a node's hardware issue. They link to DO tickets for physical work.\n"
    "- DO tickets are the hands-on execution — what DCTs actually do on the floor.\n"
    "- SDA/SDx tickets are customer-facing — they link to HO/DO for internal tracking.\n\n"
    "WORKFLOW STATUSES:\n"
    "- DO/HO: To Do -> In Progress -> Verification -> Closed (also: On Hold, Waiting for Support)\n"
    "- HO also has: RMA-initiate, Sent to DCT UC, Awaiting Parts, Radar\n"
    "- SDA: Awaiting Triage -> Waiting for Support -> In Progress -> Customer Verification -> Closed\n\n"
    "COMMON TERMS:\n"
    "- DCT: Data Center Technician (the user of this tool)\n"
    "- BMC/iDRAC: Baseboard Management Controller — remote console for the server\n"
    "- NVLink: High-speed GPU interconnect between GPUs (high-speed bridge)\n"
    "- Service tag: Dell/Supermicro serial number for the server\n"
    "- RMA: Return Merchandise Authorization — sending hardware back to vendor\n"
    "- NetBox: DCIM tool — tracks devices, racks, cables, IPs\n"
    "- Grafana: Monitoring dashboards for node health metrics\n"
    "- NLCC/FLCC: Node Lifecycle / Fleet Lifecycle management tools\n"
    "- TOR: Top-of-Rack switch\n"
    "- PDU: Power Distribution Unit in the rack\n"
    "- Optic/SFP/transceiver: Fiber optic module that plugs into a port\n"
    "- IB: InfiniBand — high-speed fabric for GPU clusters\n\n"
    "--- DCT COMMON PROCEDURES ---\n\n"
    "RECABLE (recable and prepare for onboarding):\n"
    "This does NOT mean new hardware. It means re-wire an existing failed/triaged node so it matches "
    "the onboarding cabling layout, then leave it ready for Fleet to use.\n"
    "Steps:\n"
    "1. Confirm correct box — verify serial/service tag matches the ticket\n"
    "2. Check/move power cords — both PSUs to correct PDU circuits for the rack/row\n"
    "3. Move/verify data cables — IB, ethernet, management to the standard onboarding ports "
    "(onboarding TOR switches, not production fabric)\n"
    "4. Fix cable labels — node name and port labels match onboarding map\n"
    "5. Install optics if needed — correct SFP modules for the port type\n"
    "6. Do NOT power on unless SOP says to — Fleet/onboarding handles power-on and tests\n"
    "7. Comment on ticket: 'Recabled, optics installed, ready for onboarding'\n"
    "8. Move ticket to Verification\n\n"
    "POWER_CYCLE:\n"
    "1. Confirm correct box via serial/service tag\n"
    "2. Try BMC/iDRAC power cycle first (remote if BMC is reachable)\n"
    "3. If BMC unreachable, physically pull both power cables, wait 30 seconds, re-seat\n"
    "4. Verify node comes back up (check BMC IP, wait for boot)\n"
    "5. Comment on ticket with result\n"
    "6. Move ticket to Verification if resolved\n\n"
    "RESEAT (component reseat):\n"
    "1. Power off node gracefully via BMC if possible, then pull power\n"
    "2. Reseat the specified component (GPU, NVLink bridge, DIMM, PSU, riser, cable)\n"
    "3. Re-seat power, power on\n"
    "4. Verify component is detected (check BMC/Grafana)\n"
    "5. Comment and move to Verification\n\n"
    "SWAP (component replacement):\n"
    "1. Power off, pull power\n"
    "2. Remove failed component, note serial number\n"
    "3. Install replacement, note new serial\n"
    "4. Re-seat power, power on\n"
    "5. Verify in BMC/Grafana\n"
    "6. Comment with old/new serials, move to Verification\n"
    "7. If RMA needed, note in HO ticket\n\n"
    "DPU_PORT_CLEAN:\n"
    "1. Power off node\n"
    "2. Clean the DPU fiber ports with IPA and lint-free wipes\n"
    "3. Clean the optic/SFP end faces\n"
    "4. Re-seat optics, reconnect fibers\n"
    "5. Power on, verify link comes up\n"
    "6. Comment and move to Verification\n\n"
    "INSPECTION:\n"
    "1. Visually inspect node for: loose cables, LED indicators, damage, dust\n"
    "2. Check all connections are seated and labeled\n"
    "3. Note findings in ticket comment\n"
    "4. Move to Verification\n\n"
    "GENERAL DCT RULES:\n"
    "- Always verify serial/service tag BEFORE touching hardware\n"
    "- Always comment what you did on the ticket\n"
    "- Always move to Verification when done (not Closed — let the requester verify)\n"
    "- If you find additional issues, note them in the comment and/or create a new ticket\n"
    "- If unsure, check the HO ticket for context and guidance notes\n"
    "- 'Sent to DCT RC' on HO means it's in recable phase — expect a recable DO\n"
    "- 'Sent to DCT UC' on HO means it's in uncable phase — expect an uncable DO\n"
)

AI_SYSTEM_PROMPT_TICKET = (
    "You are a data center operations assistant embedded in a CLI tool called cwhelper. "
    "You help CoreWeave DCT technicians understand Jira tickets and troubleshoot node issues.\n\n"
    "Context you receive:\n"
    "- Jira ticket details (key, summary, status, assignee, description, comments)\n"
    "- NetBox device data (rack location, interfaces, IPs, model)\n"
    "- Grafana dashboard links\n\n"
    "Rules:\n"
    "- Be concise. Technicians are on the data center floor.\n"
    "- Use plain English, no jargon unless the ticket uses it.\n"
    "- When summarizing, lead with: what the issue is, what has been done, what the likely next step is.\n"
    "- When troubleshooting, reference specific fields from the ticket context.\n"
    "- If you don't know something, say so. Never invent ticket data."
    + _AI_DOMAIN_KNOWLEDGE
)

AI_SYSTEM_PROMPT_FINDER = (
    "You are a search assistant for CoreWeave Jira tickets. "
    "The user will describe what they remember about a ticket. Your job is to:\n"
    "1. Extract search keywords from their description.\n"
    "2. After seeing search results, rank them by relevance and explain why each might be the one.\n"
    "Be concise. Format output for a terminal (no markdown headers, use plain text)."
    + _AI_DOMAIN_KNOWLEDGE
)

AI_SYSTEM_PROMPT_CHAT = (
    "You are a helpful assistant for data center technicians at CoreWeave. "
    "You have access to the current ticket context if provided. Answer questions naturally. "
    "Be concise — this is a terminal interface. No markdown formatting."
    + _AI_DOMAIN_KNOWLEDGE
)

# Data hall layout config — stored next to this script
_DH_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dh_layouts.json")

# Persistent user state (recents, bookmarks, greeting cache)
_USER_STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cwhelper_state.json")

# HTTP session (reuses TCP connections via keep-alive)
_session = requests.Session()

# Thread pool for parallel API calls (NetBox serial+name, background fetches)
_executor = ThreadPoolExecutor(max_workers=3)

# In-memory cache for full issue responses (keyed by issue key)
_issue_cache: dict[str, dict] = {}

# In-memory cache for NetBox device context (keyed by lookup args)
_netbox_cache: dict[str, dict] = {}

# In-memory cache for JQL search results with TTL (keyed by query fingerprint)
# Values: (timestamp, issues_list)
_jql_cache: dict[str, tuple[float, list]] = {}
_JQL_CACHE_TTL = 60  # seconds

# Cache size limits (LRU eviction when exceeded)
_ISSUE_CACHE_MAX = 100
_NETBOX_CACHE_MAX = 50
_JQL_CACHE_MAX = 200

# Animation toggle (set CWHELPER_ANIMATE=0 to disable)
_ANIMATE = os.environ.get("CWHELPER_ANIMATE", "1") != "0"

# AI toggle — on by default if configured, user can toggle with "ai off"/"ai on"
_AI_ENABLED = True

# IB topology lookup (loaded from ib_topology.json once)
_IB_TOPO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ib_topology.json")
_ib_topo: dict | None = None

def _get_ib_topology() -> dict:
    """Load IB topology from JSON (lazy, cached)."""
    global _ib_topo
    if _ib_topo is not None:
        return _ib_topo
    if os.path.exists(_IB_TOPO_PATH):
        try:
            with open(_IB_TOPO_PATH) as f:
                _ib_topo = json.load(f)
        except (json.JSONDecodeError, OSError):
            _ib_topo = {}
    else:
        _ib_topo = {}
    return _ib_topo

def _lookup_ib_connections(hostname: str, rack_location: str = None) -> list:
    """Look up IB port connections from the topology JSON.

    Matches by DH + rack + node number extracted from hostname.
    Returns list of {port, leaf_rack, leaf_id, leaf_port} or [].
    """
    topo = _get_ib_topology()
    if not topo:
        return []
    # Extract DH, rack, node from hostname like 'dh1-r102-node-04-us-central-07a'
    import re
    m = re.match(r"(dh\d+)-r(\d+)-node-(\d+)", (hostname or "").lower())
    if not m:
        # Try s1-r027-node-14 pattern
        m = re.match(r"s\d+-r(\d+)-node-(\d+)", (hostname or "").lower())
        if m:
            rack = m.group(1).lstrip("0") or "0"
            node = int(m.group(2))
            # Try SEC1 keys
            for dh in ("SEC1", "DH1", "DH2"):
                key = f"{dh}:{rack}:{node}"
                if key in topo:
                    return topo[key]
            return []
        return []
    dh = m.group(1).upper()
    rack = m.group(2).lstrip("0") or "0"
    node = int(m.group(3))
    # Check for site-specific prefixes based on hostname suffix
    hostname_lower = (hostname or "").lower()
    if "ord3" in hostname_lower:
        ord_key = f"ORD3:{rack}:{node}"
        if ord_key in topo:
            return topo[ord_key]
    key = f"{dh}:{rack}:{node}"
    if key in topo:
        return topo[key]
    # Cutsheet may use continuous numbering (e.g. R306 nodes 17-24 instead of 1-8).
    # Find all entries for this rack, sort by node number, and pick by position.
    prefix = f"{dh}:{rack}:"
    rack_entries = sorted(
        [(k, v) for k, v in topo.items() if k.startswith(prefix)],
        key=lambda x: int(x[0].split(":")[2])
    )
    if rack_entries and 1 <= node <= len(rack_entries):
        return rack_entries[node - 1][1]
    return []

# ---------------------------------------------------------------------------
# Background watcher state (shared across threads)
# ---------------------------------------------------------------------------
_watcher_thread: threading.Thread | None = None
_watcher_stop_event = threading.Event()
_watcher_queue: queue_mod.Queue = queue_mod.Queue()   # new ticket dicts
_watcher_site: str = ""
_watcher_project: str = ""
_watcher_interval: int = 45


# ---------------------------------------------------------------------------
# Helpers — cache eviction, retry, UI pause, security
# ---------------------------------------------------------------------------

def _escape_jql(value: str) -> str:
    """Escape special characters for safe JQL string interpolation."""
    if not value:
        return value
    return value.replace('\\', '\\\\').replace('"', '\\"')


def _classify_port_role(port_name: str) -> str:
    """Classify a network interface by its name into a DCT-readable role."""
    name_lower = port_name.lower()
    if "bmc" in name_lower or "ipmi" in name_lower:
        return "BMC"
    if "dpu" in name_lower:
        return "DPU"
    if "ib" in name_lower or "mlx" in name_lower or "infiniband" in name_lower:
        return "IB"
    if "eno" in name_lower or "eth" in name_lower or "bond" in name_lower:
        return "NIC"
    return "\u2014"


def _cache_put(cache: dict, key: str, value, max_size: int):
    """Insert into a dict-cache and evict the oldest entry if over max_size."""
    if len(cache) >= max_size:
        oldest = next(iter(cache))
        del cache[oldest]
    cache[key] = value


def _request_with_retry(method, *args, retries: int = 2, **kwargs):
    """Call a requests method with simple retry on transient errors.

    Retries on connection errors and 5xx server errors only.
    Uses 1s, 2s backoff between attempts.
    """
    last_exc = None
    for attempt in range(1 + retries):
        try:
            resp = method(*args, **kwargs)
            if resp.status_code < 500 or attempt == retries:
                return resp
            # 5xx — retry
        except requests.RequestException as exc:
            last_exc = exc
            if attempt == retries:
                raise
        time.sleep(min(attempt + 1, 3))
    if last_exc:
        raise last_exc


def _brief_pause(seconds: float = 0.3):
    """Brief UI feedback pause, capped at 0.3s. Skipped for non-TTY output."""
    if sys.stdout.isatty():
        time.sleep(min(seconds, 0.3))


# ---------------------------------------------------------------------------
# Helpers — auth & HTTP
# ---------------------------------------------------------------------------

def _get_env_or_exit(var_name: str) -> str:
    """Return the value of an env var, or exit with a helpful message."""
    value = os.environ.get(var_name, "").strip()
    if not value:
        print(
            f"Error: environment variable {var_name} is not set.\n"
            f"Export it first:\n"
            f"  export {var_name}='your-value-here'"
        )
        sys.exit(1)
    return value


def _get_credentials() -> tuple:
    """Return (email, token) from env vars."""
    return _get_env_or_exit("JIRA_EMAIL"), _get_env_or_exit("JIRA_API_TOKEN")


def _jira_get(path: str, email: str, token: str, params: dict = None):
    """Make an authenticated GET to Jira and return the response object."""
    url = f"{JIRA_BASE_URL}{path}"
    return _request_with_retry(
        _session.get,
        url,
        auth=(email, token),
        headers={"Accept": "application/json"},
        params=params,
        timeout=(5, 10),
    )


def _jira_post(path: str, email: str, token: str, body: dict):
    """Make an authenticated POST to Jira and return the response object."""
    url = f"{JIRA_BASE_URL}{path}"
    return _request_with_retry(
        _session.post,
        url,
        auth=(email, token),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        json=body,
        timeout=(5, 10),
    )


def _jira_put(path: str, email: str, token: str, body: dict):
    """Make an authenticated PUT to Jira and return the response object."""
    url = f"{JIRA_BASE_URL}{path}"
    return _request_with_retry(
        _session.put,
        url,
        auth=(email, token),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        json=body,
        timeout=(5, 10),
    )


# Cached account ID and display name for the current user
_my_account_id: str | None = None
_my_display_name: str | None = None


def _get_my_account_id(email: str, token: str) -> str | None:
    """Fetch and cache the current user's Jira accountId and displayName."""
    global _my_account_id, _my_display_name
    if _my_account_id:
        return _my_account_id
    resp = _jira_get("/rest/api/3/myself", email, token)
    if resp.ok:
        data = resp.json()
        _my_account_id = data.get("accountId")
        _my_display_name = data.get("displayName")
    return _my_account_id


def _get_first_name(email: str, token: str) -> str:
    """Get the user's first name for greeting. Uses state cache, then API."""
    global _my_display_name

    # In-memory cache (already fetched this session)
    if _my_display_name:
        return _my_display_name.split()[0]

    # State file cache (avoids API call on startup)
    state = _load_user_state()
    cached = state.get("user", {}).get("first_name")
    if cached:
        _my_display_name = state["user"].get("display_name", cached)
        return cached

    # Trigger API call (also populates _my_display_name)
    _get_my_account_id(email, token)
    if _my_display_name:
        first = _my_display_name.split()[0]
        state["user"] = {
            "display_name": _my_display_name,
            "first_name": first,
            "account_id": _my_account_id,
        }
        _save_user_state(state)
        return first

    # Fallback: email prefix
    return email.split("@")[0].split(".")[0].capitalize()


def _post_comment(key: str, text: str, email: str, token: str) -> bool:
    """Post a comment on a Jira issue. Returns True on success."""
    body = {"body": _text_to_adf(text)}
    resp = _jira_post(f"/rest/api/3/issue/{key}/comment", email, token, body=body)
    return resp.status_code in (200, 201)


def _grab_ticket(key: str, email: str, token: str) -> bool:
    """Assign a ticket to the current user. Returns True on success."""
    account_id = _get_my_account_id(email, token)
    if not account_id:
        print(f"  {DIM}Could not determine your Jira account ID.{RESET}")
        return False
    resp = _jira_put(
        f"/rest/api/3/issue/{key}/assignee",
        email, token,
        body={"accountId": account_id},
    )
    if resp.status_code == 204:
        return True
    if resp.status_code == 403:
        print(f"  {DIM}Permission denied — cannot assign {key}.{RESET}")
        return False
    print(f"  {DIM}Assign failed ({resp.status_code}): {resp.text[:200]}{RESET}")
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
        print(f"  {DIM}Permission denied — cannot assign {key}.{RESET}")
        return False
    print(f"  {DIM}Assign failed ({resp.status_code}): {resp.text[:200]}{RESET}")
    return False


# ---------------------------------------------------------------------------
# Jira status transitions
# ---------------------------------------------------------------------------

def _is_mine(ctx: dict) -> bool:
    """Check if the ticket is assigned to the logged-in user."""
    assignee = ctx.get("assignee")
    if not assignee:
        return False
    if _my_display_name and _my_display_name.lower() == assignee.lower():
        return True
    if _my_account_id and ctx.get("_assignee_account_id") == _my_account_id:
        return True
    my_email = os.environ.get("JIRA_EMAIL", "")
    my_name = " ".join(w.capitalize() for w in my_email.split("@")[0].split("."))
    return bool(my_name and my_name.lower() == assignee.lower())


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


def _fetch_transitions(key: str, email: str, token: str) -> list[dict]:
    """Fetch available transitions for an issue from Jira."""
    resp = _jira_get(f"/rest/api/3/issue/{key}/transitions", email, token)
    if not resp or not resp.ok:
        return []
    return resp.json().get("transitions", [])


def _find_transition(transitions: list[dict], action: str) -> dict | None:
    """Find the best matching transition for a conceptual action."""
    mapping = TRANSITION_MAP.get(action)
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
        print(f"  {YELLOW}Transition not available. Available: {available}{RESET}")
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
        print(f"  {YELLOW}Transition failed: {msg}{RESET}")
        return False
    status = resp.status_code if resp else "no response"
    print(f"  {YELLOW}Transition failed ({status}).{RESET}")
    return False


def _refresh_ctx(ctx: dict, email: str, token: str):
    """Invalidate cache, re-fetch, rebuild context in-place after a mutation."""
    key = ctx["issue_key"]
    identifier = ctx.get("identifier", key)
    _issue_cache.pop(key, None)

    issue = _jira_get_issue(key, email, token)
    new_ctx = _build_context(identifier, issue, email, token)
    new_ctx["_transitions"] = None  # force re-fetch next time

    # Preserve display toggles
    for toggle in ("_show_comments", "_show_desc", "_show_diags", "_show_sla"):
        if ctx.get(toggle):
            new_ctx[toggle] = ctx[toggle]

    ctx.clear()
    ctx.update(new_ctx)


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
    if key in _issue_cache:
        return _issue_cache[key]

    fields_param = ",".join(ISSUE_DETAIL_FIELDS)
    response = _jira_get(
        f"/rest/api/3/issue/{key}",
        email, token,
        params={"fields": fields_param},
    )
    _handle_response_errors(response, key)
    data = response.json()
    _cache_put(_issue_cache, key, data, _ISSUE_CACHE_MAX)
    return data


# ---------------------------------------------------------------------------
# NetBox API (optional enrichment)
# ---------------------------------------------------------------------------

def _netbox_available() -> bool:
    """Check if NetBox env vars are configured."""
    return bool(os.environ.get("NETBOX_API_URL", "").strip()
                and os.environ.get("NETBOX_API_TOKEN", "").strip())


def _netbox_get(path: str, params: dict = None) -> dict | None:
    """Make an authenticated GET to NetBox. Returns JSON or None on error."""
    base = os.environ.get("NETBOX_API_URL", "").strip().rstrip("/")
    token = os.environ.get("NETBOX_API_TOKEN", "").strip()
    if not base or not token:
        return None

    url = f"{base}{path}"
    try:
        response = _session.get(
            url,
            headers={
                "Authorization": f"Token {token}",
                "Accept": "application/json",
                "User-Agent": "cw-node-helper/v0.5",
            },
            params=params,
            timeout=(5, 10),
        )
        if response.ok:
            return response.json()
    except requests.RequestException:
        pass
    return None


def _netbox_find_device(serial: str | None = None,
                        name: str | None = None) -> dict | None:
    """Find a device in NetBox by serial number or name.

    Fires both lookups in parallel when both are provided.
    Serial result takes priority. Returns the first matching device dict,
    or None if not found / NetBox not configured.
    """
    if not _netbox_available():
        return None

    searches = []
    if serial:
        searches.append(("serial", {"serial": serial}))
    if name:
        searches.append(("name", {"name": name}))

    if not searches:
        return None

    # Single search — no thread overhead
    if len(searches) == 1:
        data = _netbox_get("/dcim/devices/", params=searches[0][1])
        if data and data.get("results"):
            return data["results"][0]
        return None

    # Parallel: fire both, prefer serial result
    results = {}
    future_map = {
        _executor.submit(_netbox_get, "/dcim/devices/", params=params): label
        for label, params in searches
    }
    for future in as_completed(future_map):
        label = future_map[future]
        try:
            data = future.result()
            if data and data.get("results"):
                results[label] = data["results"][0]
        except Exception:
            pass

    return results.get("serial") or results.get("name")


def _netbox_get_interfaces(device_id: int) -> list:
    """Get interfaces for a device by its NetBox device ID."""
    data = _netbox_get("/dcim/interfaces/", params={
        "device_id": device_id,
        "limit": 100,
    })
    if not data:
        return []
    return data.get("results", [])


def _netbox_get_rack_devices(rack_id: int) -> list:
    """Get all devices in a rack by NetBox rack ID.

    Returns devices sorted by position descending (top of rack first).
    """
    data = _netbox_get("/dcim/devices/", params={
        "rack_id": rack_id,
        "limit": 50,
    })
    if not data:
        return []
    devices = data.get("results", [])
    devices.sort(key=lambda d: (d.get("position") is None, -(d.get("position") or 0)))
    return devices


def _netbox_find_rack_by_name(rack_name: str, site_slug: str = None) -> dict | None:
    """Find a rack in NetBox by name, trying zero-padded variants.

    NetBox rack names are often zero-padded (e.g. '064' not '64').
    Tries: exact → 3-digit → 4-digit padding.
    """
    # Build unique candidate names to try
    seen = set()
    candidates = []
    for c in [rack_name, rack_name.zfill(3), rack_name.zfill(4)]:
        if c not in seen:
            seen.add(c)
            candidates.append(c)

    for name in candidates:
        params = {"name": name, "limit": 1}
        if site_slug:
            params["site"] = site_slug
        data = _netbox_get("/dcim/racks/", params=params)
        if data and data.get("results"):
            return data["results"][0]
    return None


def _fetch_neighbor_devices(rack_num: int, layout: dict,
                            site_slug: str = None) -> dict:
    """Fetch devices in physically adjacent racks (parallel NetBox calls).

    Returns {"left": {"rack_num": int, "rack_id": int, "devices": list} | None,
             "right": ...}.
    """
    from concurrent.futures import as_completed
    neighbors = _get_physical_neighbors(rack_num, layout)
    result = {"left": None, "right": None}

    # Phase 1: look up rack IDs in parallel
    rack_futures = {}
    for side in ("left", "right"):
        n = neighbors.get(side)
        if n is not None:
            fut = _executor.submit(_netbox_find_rack_by_name, str(n), site_slug)
            rack_futures[fut] = (side, n)

    # Phase 2: for each found rack, fetch its devices in parallel
    device_futures = {}
    for fut in as_completed(rack_futures):
        side, n = rack_futures[fut]
        try:
            rack_data = fut.result(timeout=10)
        except Exception:
            rack_data = None
        if rack_data and rack_data.get("id"):
            rid = rack_data["id"]
            result[side] = {"rack_num": n, "rack_id": rid, "devices": []}
            dfut = _executor.submit(_netbox_get_rack_devices, rid)
            device_futures[dfut] = side
        else:
            result[side] = {"rack_num": n, "rack_id": None, "devices": []}

    for dfut in as_completed(device_futures):
        side = device_futures[dfut]
        try:
            devs = dfut.result(timeout=10)
        except Exception:
            devs = []
        if result[side]:
            result[side]["devices"] = devs

    return result


def _parse_iface_speed(nb_type) -> str:
    """Parse NetBox interface type to a short speed label (e.g. '100G')."""
    if not nb_type or not isinstance(nb_type, dict):
        return ""
    val = (nb_type.get("value") or "").lower()
    for prefix, label in [
        ("400g", "400G"), ("200g", "200G"), ("100g", "100G"),
        ("40g", "40G"), ("25g", "25G"), ("10g", "10G"), ("1000", "1G"),
    ]:
        if val.startswith(prefix):
            return label
    return ""


def _build_netbox_context(service_tag: str | None,
                          node_name: str | None,
                          hostname: str | None,
                          rack_location: str | None = None) -> dict:
    """Query NetBox for device info and interfaces. Returns a dict.

    This is called during context building. If NetBox is not configured
    or the device isn't found, returns an empty dict (no error).
    Results are cached in-memory by lookup args.
    """
    if not _netbox_available():
        return {}

    # Check NetBox cache
    cache_key = f"{service_tag}|{node_name}|{hostname}|{rack_location}"
    if cache_key in _netbox_cache:
        return _netbox_cache[cache_key]

    device = _netbox_find_device(serial=service_tag, name=node_name or hostname)
    # Fallback: if rack_location looks like a hostname, try it as a device name
    if not device and rack_location and "." not in rack_location and "-" in rack_location:
        device = _netbox_find_device(name=rack_location)
    # Fallback: look up device by rack + RU position
    if not device and rack_location and "." in rack_location:
        parsed_rl = _parse_rack_location(rack_location)
        if parsed_rl and parsed_rl.get("ru"):
            site_slug = parsed_rl["site_code"].lower()
            rack_obj = _netbox_find_rack_by_name(str(parsed_rl["rack"]), site_slug)
            # Try Jira site field as fallback slug
            if not rack_obj:
                # site_slug might be a LoCode; try common alternatives
                # We don't have ctx here, but we can try without site filter
                rack_obj = _netbox_find_rack_by_name(str(parsed_rl["rack"]))
            if rack_obj and rack_obj.get("id"):
                try:
                    ru = int(float(parsed_rl["ru"]))
                    devices_in_rack = _netbox_get_rack_devices(rack_obj["id"])
                    for d in devices_in_rack:
                        if d.get("position") and int(d["position"]) == ru:
                            device = d
                            break
                except (ValueError, TypeError):
                    pass
    if not device:
        _cache_put(_netbox_cache, cache_key, {}, _NETBOX_CACHE_MAX)
        return {}

    device_id = device.get("id")

    # Extract key fields from the device
    site_obj = device.get("site") or {}
    rack_obj = device.get("rack") or {}
    position = device.get("position")
    primary_ip_obj = device.get("primary_ip") or {}
    primary_ip4_obj = device.get("primary_ip4") or {}
    primary_ip6_obj = device.get("primary_ip6") or {}
    oob_ip_obj = device.get("oob_ip") or {}

    # Manufacturer + model from device_type
    device_type_obj = device.get("device_type") or {}
    manufacturer_obj = device_type_obj.get("manufacturer") or {}

    result = {
        "device_name": device.get("name"),
        "device_id": device_id,
        "serial": device.get("serial"),
        "asset_tag": device.get("asset_tag"),       # Snipe asset tag synced to NetBox
        "site": site_obj.get("display") or site_obj.get("name"),
        "site_slug": site_obj.get("slug"),  # e.g., "us-central-07a" — for Teleport BMC URL
        "rack": rack_obj.get("display") or rack_obj.get("name"),
        "rack_id": rack_obj.get("id"),
        "position": position,
        "primary_ip": primary_ip_obj.get("address"),
        "primary_ip4": primary_ip4_obj.get("address"),
        "primary_ip6": primary_ip6_obj.get("address"),
        "oob_ip": oob_ip_obj.get("address"),
        "status": (device.get("status") or {}).get("label"),
        "device_role": (device.get("role") or device.get("device_role") or {}).get("display"),
        "platform": (device.get("platform") or {}).get("display"),
        "manufacturer": manufacturer_obj.get("display") or manufacturer_obj.get("name"),
        "model": device_type_obj.get("display") or device_type_obj.get("model"),
        "interfaces": [],
    }

    # Fetch interfaces and classify them for DCT readability
    if device_id:
        ifaces = _netbox_get_interfaces(device_id)
        for iface in ifaces:
            cable = iface.get("cable")
            link_peers = iface.get("link_peers") or []
            if not cable or not link_peers:
                continue  # skip uncabled

            full_name = iface.get("display") or iface.get("name") or "?"
            # Strip device prefix from port name (e.g. "device:bmc" → "bmc")
            port_name = full_name.split(":")[-1] if ":" in full_name else full_name

            peer = link_peers[0]
            peer_device = peer.get("device", {})
            peer_name_full = peer_device.get("display") or peer_device.get("name") or "?"
            peer_port = peer.get("display") or peer.get("name") or "?"
            # Strip device prefix from peer port too
            peer_port_short = peer_port.split(":")[-1] if ":" in peer_port else peer_port

            # Short peer name + extract rack from full name
            peer_short = _short_device_name(peer_name_full)
            rack_match = re.search(r"-r(\d{2,4})", peer_name_full.lower())
            peer_rack = f"R{rack_match.group(1).lstrip('0') or '0'}" if rack_match else ""

            # Cable ID for NetBox link
            cable_id = cable.get("id") if isinstance(cable, dict) else None

            role = _classify_port_role(port_name)

            # Parse interface speed from NetBox type field
            speed = _parse_iface_speed(iface.get("type"))

            result["interfaces"].append({
                "name": port_name,
                "role": role,
                "speed": speed,
                "peer_device": peer_short,
                "peer_device_full": peer_name_full,
                "peer_port": peer_port_short,
                "peer_rack": peer_rack,
                "cable_id": cable_id,
                "connected_to": f"{peer_name_full}:{peer_port}",
            })

    _cache_put(_netbox_cache, cache_key, result, _NETBOX_CACHE_MAX)
    return result


# ---------------------------------------------------------------------------
# Helpers — field extraction
# ---------------------------------------------------------------------------

def _format_age(seconds: float) -> str:
    """Format seconds into a human-readable age string like '3d 4h' or '12m'."""
    total = int(seconds)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)


def _parse_jira_timestamp(ts: str) -> float:
    """Parse a Jira timestamp string and return seconds since that time."""
    if not ts:
        return 0
    # Strip fractional seconds and timezone for simple parsing
    # Format: "2026-02-02T15:32:00.000-0500" or "2026-02-02T15:32:00.000+0000"
    try:
        from datetime import datetime, timezone
        # Handle both +HHMM and Z formats
        clean = ts.replace("Z", "+0000")
        if "." in clean:
            base, frac_tz = clean.split(".", 1)
            # Extract timezone offset from end
            for i in range(len(frac_tz) - 1, -1, -1):
                if frac_tz[i] in "+-":
                    tz_str = frac_tz[i:]
                    break
            else:
                tz_str = "+0000"
            dt = datetime.strptime(f"{base}{tz_str}", "%Y-%m-%dT%H:%M:%S%z")
        else:
            dt = datetime.strptime(clean, "%Y-%m-%dT%H:%M:%S%z")
        now = datetime.now(timezone.utc)
        return max(0, (now - dt).total_seconds())
    except Exception:
        return 0


def _unwrap_field(raw_value):
    """Unwrap single-element lists like ["10NQ724"] -> "10NQ724"."""
    if isinstance(raw_value, list):
        return raw_value[0] if len(raw_value) == 1 else raw_value or None
    return raw_value


def _extract_custom_fields(fields: dict) -> dict:
    """Pull known DCT custom fields out of the Jira fields dict."""
    extracted = {}
    for jira_id, friendly_name in CUSTOM_FIELDS.items():
        extracted[friendly_name] = _unwrap_field(fields.get(jira_id))
    return extracted


def _extract_linked_issues(fields: dict) -> list:
    """Pull linked issue keys and their relationship type from issuelinks."""
    links = []
    for link in fields.get("issuelinks", []):
        link_type = link.get("type", {}).get("name", "Related")
        for direction in ("inwardIssue", "outwardIssue"):
            linked = link.get(direction)
            if linked:
                linked_fields = linked.get("fields", {})
                linked_status = linked_fields.get("status", {})
                links.append({
                    "key": linked.get("key"),
                    "relationship": link_type,
                    "summary": linked_fields.get("summary", ""),
                    "status": linked_status.get("name", "Unknown"),
                })
    return links


def _extract_portal_url(fields: dict) -> str | None:
    """Try to get the Service Desk portal URL from customfield_10010._links.web."""
    req_info = fields.get("customfield_10010")
    if isinstance(req_info, dict):
        return req_info.get("_links", {}).get("web")
    return None


def _extract_description_details(fields: dict) -> dict:
    """Parse the Atlassian Document Format (ADF) description to extract:
      - rma_reason:  text starting with "RMA Reason:"
      - node_name:   text starting with "Node:" or "Node name:"
      - diag_links:  list of {label, url} dicts from any URLs found
    """
    desc = fields.get("description")
    if not desc or not isinstance(desc, dict):
        return {"rma_reason": None, "node_name": None, "diag_links": []}

    rma_reason = None
    node_name = None
    diag_links = []
    desc_rack = None   # rack number parsed from device hostname in description
    desc_dh = None     # data hall (e.g. "DH1") from device hostname
    desc_ru = None     # rack unit from "rack unit N" mention

    def _walk_content(node):
        """Recursively walk ADF nodes and extract text + links."""
        nonlocal rma_reason, node_name, desc_rack, desc_dh, desc_ru

        if not isinstance(node, dict):
            return

        # Text node — check for RMA Reason / Node patterns
        if node.get("type") == "text":
            text = node.get("text", "").strip()

            if text.lower().startswith("rma reason:") and not rma_reason:
                rma_reason = text.split(":", 1)[1].strip()

            if re.match(r"^node\s*(name)?:", text, re.IGNORECASE) and not node_name:
                node_name = re.split(r":\s*", text, maxsplit=1)[1].strip()

            # Extract rack/DH from device hostnames like dh1-r264-node-02-us-central-07a
            if not desc_rack:
                m = re.search(r'\b(dh\w*)-r(\d+)-', text, re.IGNORECASE)
                if m:
                    desc_dh = m.group(1).upper()  # e.g. "DH1"
                    desc_rack = int(m.group(2))    # e.g. 264

            # Extract rack unit from "rack unit N" mentions
            if not desc_ru:
                m = re.search(r'rack\s+unit\s+(\d+)', text, re.IGNORECASE)
                if m:
                    desc_ru = int(m.group(1))

            # Check if this text node has a link mark (URL)
            marks = node.get("marks", [])
            for mark in marks:
                if mark.get("type") == "link":
                    href = mark.get("attrs", {}).get("href", "")
                    if href:
                        # Derive a short label from the URL filename
                        filename = href.rstrip("/").rsplit("/", 1)[-1]
                        diag_links.append({"label": filename, "url": href})

        # Recurse into child content
        for child in node.get("content", []):
            _walk_content(child)

    _walk_content(desc)

    # Deduplicate diag links by URL
    seen = set()
    unique_links = []
    for link in diag_links:
        if link["url"] not in seen:
            seen.add(link["url"])
            unique_links.append(link)

    return {
        "rma_reason": rma_reason,
        "node_name": node_name,
        "diag_links": unique_links,
        "desc_rack": desc_rack,
        "desc_dh": desc_dh,
        "desc_ru": desc_ru,
    }


def _extract_comments(fields: dict, max_comments: int = 3) -> list:
    """Pull the most recent comments from fields.comment.comments.

    Returns a list of dicts: {author, created, body} (most recent first).
    """
    comment_data = fields.get("comment", {})
    comments_raw = comment_data.get("comments", [])

    # Take the last N (most recent)
    recent = comments_raw[-max_comments:]
    recent.reverse()  # newest first

    results = []
    for c in recent:
        author_obj = c.get("author", {})
        author = author_obj.get("displayName", "Unknown")

        # created is like "2024-11-04T14:30:00.000-0500"
        created_raw = c.get("created", "")
        # Trim to just date + time (first 16 chars: "2024-11-04T14:30")
        created = created_raw[:16].replace("T", " ") if created_raw else "?"

        # Extract plain text from ADF body
        body_adf = c.get("body", {})
        body_text = _adf_to_plain_text(body_adf)
        # Trim to first ~120 chars for display
        if len(body_text) > 120:
            body_text = body_text[:117] + "..."

        results.append({"author": author, "created": created, "body": body_text})

    return results


def _adf_to_plain_text(node: dict) -> str:
    """Recursively extract plain text from an ADF document node."""
    if not isinstance(node, dict):
        return ""

    if node.get("type") == "text":
        return node.get("text", "")

    parts = []
    for child in node.get("content", []):
        parts.append(_adf_to_plain_text(child))

    return " ".join(parts).strip()


def _render_adf_description(node: dict, indent: str = "    ") -> tuple[list[str], list[dict]]:
    """Render an ADF document into formatted terminal lines.

    Preserves paragraph breaks, renders headings in bold, handles
    bullet/ordered lists, decodes HTML entities, and word-wraps.
    Returns (lines, links) where lines is a list of ready-to-print
    strings and links is a list of {label, url} dicts.
    """
    if not isinstance(node, dict):
        return [], []

    lines: list[str] = []
    links: list[dict] = []
    seen_urls: set[str] = set()
    width = 66  # wrap width (fits nicely with 4-char indent in ~80-col terminal)

    def _inline_text(n: dict) -> str:
        """Extract inline text from a node, applying bold/link marks."""
        if not isinstance(n, dict):
            return ""
        if n.get("type") == "text":
            raw = n.get("text", "")
            raw = html_mod.unescape(raw)
            marks = n.get("marks", [])
            for m in marks:
                if m.get("type") == "strong":
                    raw = f"{BOLD}{raw}{RESET}"
                elif m.get("type") == "link":
                    href = m.get("attrs", {}).get("href", "")
                    if href and href not in seen_urls:
                        seen_urls.add(href)
                        links.append({"label": raw, "url": href})
                    raw = f"{CYAN}{UNDERLINE}{raw}{RESET}"
            return raw
        if n.get("type") == "hardBreak":
            return "\n"
        parts = []
        for child in n.get("content", []):
            parts.append(_inline_text(child))
        return "".join(parts)

    def _plain_len(s: str) -> int:
        """Length of string ignoring ANSI escape sequences."""
        return len(re.sub(r'\x1b\[[0-9;]*m', '', s))

    def _wrap_text(text: str, prefix: str = indent) -> list[str]:
        """Word-wrap text while being aware of ANSI codes."""
        result = []
        for raw_line in text.split("\n"):
            if not raw_line.strip():
                result.append("")
                continue
            # Simple wrap: split on spaces, accumulate
            words = raw_line.split(" ")
            cur = prefix
            cur_plain_len = len(prefix)
            for w in words:
                wlen = _plain_len(w)
                if cur_plain_len + wlen + 1 > width + len(prefix) and cur != prefix:
                    result.append(cur)
                    cur = prefix + w
                    cur_plain_len = len(prefix) + wlen
                else:
                    if cur == prefix:
                        cur += w
                    else:
                        cur += " " + w
                    cur_plain_len += wlen + (0 if cur == prefix + w else 1)
            if cur.strip():
                result.append(cur)
        return result

    def _dim_wrap(text: str, prefix: str = indent) -> list[str]:
        """Wrap text and apply DIM to each line for consistent color."""
        wrapped = _wrap_text(text, prefix)
        return [f"{DIM}{ln}{RESET}" for ln in wrapped]

    def _walk_block(block: dict):
        """Process a top-level ADF block node."""
        btype = block.get("type", "")

        if btype == "heading":
            text = _inline_text(block)
            text = html_mod.unescape(re.sub(r'\x1b\[[0-9;]*m', '', text))
            lines.append("")
            lines.append(f"{indent}{BOLD}{text}{RESET}")
            lines.append("")

        elif btype == "paragraph":
            text = _inline_text(block)
            if text.strip():
                lines.extend(_dim_wrap(text))
            else:
                lines.append("")

        elif btype in ("bulletList", "orderedList"):
            for i, item in enumerate(block.get("content", []), 1):
                bullet = f"\u2022 " if btype == "bulletList" else f"{i}. "
                item_text = ""
                for child in item.get("content", []):
                    item_text += _inline_text(child)
                if item_text.strip():
                    first_prefix = indent + "  " + bullet
                    cont_prefix = indent + "    "
                    wrapped = _dim_wrap(item_text.strip(), first_prefix)
                    if wrapped:
                        lines.append(wrapped[0])
                        for w in wrapped[1:]:
                            lines.append(f"{DIM}{cont_prefix}{w.lstrip()}{RESET}")

        elif btype == "rule":
            lines.append(f"{indent}{DIM}{'─' * (width - 4)}{RESET}")

        else:
            text = _inline_text(block)
            if text.strip():
                lines.extend(_dim_wrap(text))

    for child in node.get("content", []):
        _walk_block(child)

    return lines, links


def _parse_rack_location(rack_loc: str) -> dict | None:
    """Parse 'US-EVI01.DH1.R64.RU34' into structured components.

    Returns {site_code, dh, rack, ru} or None if unparseable.
    """
    if not rack_loc:
        return None
    # Strip parenthetical annotations like "(US-EVI01:dh1:244)" from rack locations
    rack_loc = re.sub(r'\s*\([^)]*\)', '', rack_loc)
    parts = rack_loc.split(".")
    if len(parts) < 3:
        return None
    rack_num = None
    ru_num = None
    for p in parts:
        if p.startswith("RU") and p[2:].replace(".", "").isdigit():
            ru_num = p[2:]
        elif p.startswith("R") and p[1:].isdigit():
            rack_num = int(p[1:])
    if rack_num is None:
        return None
    return {"site_code": parts[0], "dh": parts[1], "rack": rack_num, "ru": ru_num}


def _get_physical_neighbors(rack_num: int, layout: dict) -> dict:
    """Return physically adjacent rack numbers accounting for serpentine layout.

    Returns {"left": int|None, "right": int|None, "row": int, "pos": int,
             "col_label": str}.
    """
    cols = layout.get("columns", [])
    default_per_row = layout.get("racks_per_row", 10)
    serpentine = layout.get("serpentine", True)

    # Find which column this rack belongs to
    target_col = None
    target_row = None
    per_row = default_per_row
    for col in cols:
        col_per_row = col.get("racks_per_row", default_per_row)
        col_start = col["start"]
        col_end = col_start + col["num_rows"] * col_per_row - 1
        if col_start <= rack_num <= col_end:
            target_col = col
            per_row = col_per_row
            offset = rack_num - col_start
            target_row = offset // per_row
            break

    if target_col is None:
        return {"left": None, "right": None, "row": 0, "pos": 0, "col_label": "?"}

    col_start = target_col["start"]
    row_start = col_start + target_row * per_row

    # Determine position within the row (accounting for serpentine reversal)
    if serpentine and target_row % 2 == 1:
        pos = (per_row - 1) - (rack_num - row_start)
    else:
        pos = rack_num - row_start

    # Compute neighbor rack numbers at pos-1 and pos+1
    def rack_at_pos(p):
        if p < 0 or p >= per_row:
            return None
        base = col_start + target_row * per_row
        if serpentine and target_row % 2 == 1:
            return base + (per_row - 1 - p)
        return base + p

    return {
        "left": rack_at_pos(pos - 1),
        "right": rack_at_pos(pos + 1),
        "row": target_row,
        "pos": pos,
        "col_label": target_col.get("label", ""),
    }


def _short_device_name(name: str) -> str:
    """Shorten a NetBox device name for rack view display.

    Node devices:  dh1-r064-node-01-us-central-07a  →  Node 1
    Other devices: dh1-bmc-a2-01-r012-us-central-07a →  BMC A2 01
    """
    if not name:
        return "?"
    # Strip parenthetical suffixes like (m1504860) or (serial)
    clean = re.sub(r"\s*\([^)]*\)", "", name).strip()
    # Detect node pattern: anything-node-NN-anything
    m = re.search(r"node-(\d+)", clean, re.IGNORECASE)
    if m:
        return f"Node {int(m.group(1))}"
    # General cleanup: strip dh prefix, rack number, site suffix
    short = clean.lower()
    short = re.sub(r"^dh\d+-", "", short)           # strip dhN- prefix
    short = re.sub(r"-r\d{2,4}", "", short)          # strip -rNNN rack
    short = re.sub(r"-us-\S+$", "", short)            # strip -us-site-suffix
    # Title-case each part; uppercase known acronyms and short tokens
    _ACRONYMS = {"bmc", "tor", "pdu", "dpu", "nic", "gpu", "cpu", "oob", "mgmt"}
    parts = [p for p in short.split("-") if p]
    def _fmt(p):
        if p in _ACRONYMS or len(p) <= 2:
            return p.upper()
        return p.capitalize()
    return " ".join(_fmt(p) for p in parts) or name


# ---------------------------------------------------------------------------
# Jira search helpers
# ---------------------------------------------------------------------------

def _jql_search(jql: str, email: str, token: str, max_results: int = 10,
                fields: list | None = None, use_cache: bool = True) -> list:
    """Run a JQL query via POST /rest/api/3/search/jql and return issues list.

    Results are cached for _JQL_CACHE_TTL seconds. Pass use_cache=False
    (e.g. from the background watcher) to always hit the API.
    """
    fields_key = tuple(sorted(fields)) if fields else ()
    cache_key = f"{jql}|{max_results}|{fields_key}"

    if use_cache and cache_key in _jql_cache:
        ts, cached_issues = _jql_cache[cache_key]
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
        _cache_put(_jql_cache, cache_key, (time.time(), issues), _JQL_CACHE_MAX)
    # NOTE: Do NOT cache queue results in _issue_cache — queue searches use a
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
    """Search DO and HO projects for issues matching query_text.

    Searches across:
      - text (summary + description)
      - cf[10193] (service_tag)
      - cf[10192] (hostname)
    """
    projects = ", ".join(f'"{p}"' for p in SEARCH_PROJECTS)
    jql = (
        f'project in ({projects}) AND '
        f'(text ~ "{_escape_jql(query_text)}" '
        f'OR cf[10193] ~ "{_escape_jql(query_text)}" '   # service_tag field
        f'OR cf[10192] ~ "{_escape_jql(query_text)}")'    # hostname field
    )
    return _jql_search(jql, email, token,
                       fields=["key", "summary", "status", "issuetype"])


# Status filter shortcuts for the interactive menu
QUEUE_FILTERS = {
    "open":         'statusCategory != Done',
    "closed":       'status = "Closed"',
    "verification": 'status = "Verification"',
    "in progress":  'status = "In Progress"',
    "waiting":      'status = "Waiting For Support"',
    "radar":        'status in ("RMA-initiate", "Sent to DCT UC", "Sent to DCT RC", "Ready for verification")',
    "triage":       'status = "Awaiting Triage"',
    "cust verify":  'status = "Customer Verification"',
    "all":          None,   # no status filter
}


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
        "customfield_10194",   # site
        "assignee", "updated", "statuscategorychangedate",
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


# ---------------------------------------------------------------------------
# Interactive selection
# ---------------------------------------------------------------------------

def _status_color(status_name: str) -> tuple:
    """Return (ansi_color, dot_char) for a status name."""
    s = status_name.lower()
    if s in ("closed", "done", "resolved", "canceled"):
        return GREEN, "\u25cf"
    if s in ("in progress", "open", "reopened"):
        return YELLOW, "\u25cf"
    if s in ("on hold", "blocked", "paused"):
        return MAGENTA, "\u25cb"
    if s in ("waiting for support", "verification"):
        return BLUE, "\u25cf"
    if s in ("to do", "new", "waiting for triage"):
        return DIM, "\u25cb"
    return DIM, "\u25cf"


def _prompt_select(items: list, label_fn, extra_hint: str = "") -> dict | str | None:
    """Generic numbered selection prompt.

    items       — list of dicts
    label_fn    — function(index, item) -> str to display for each item
    extra_hint  — optional extra text for the prompt (e.g. ", 'x' for NetBox")
    Returns the chosen item dict, a special string command (e.g. "x"), or None.
    """
    if not items:
        return None

    for i, item in enumerate(items, start=1):
        print(label_fn(i, item))
    print()

    prompt_text = f"  Type a number (1-{len(items)}){extra_hint}, {BOLD}b{RESET} back, {BOLD}m{RESET} menu, or ENTER to refresh: "
    for _ in range(3):
        try:
            raw = input(prompt_text).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None

        if raw == "":
            return "refresh"
        if raw.lower() in ("q", "quit", "exit"):
            return "quit"
        if raw.lower() in ("m", "menu"):
            return "menu"
        if raw.lower() in ("b", "back"):
            return None
        if raw.lower() == "ai":
            return "ai"
        # Pass through single-letter/special commands to the caller
        if raw.lower() in ("x", "n", "a", "e") or raw == "*":
            return raw
        try:
            choice = int(raw)
            if 1 <= choice <= len(items):
                return items[choice - 1]
            print(f"  That number is out of range. Pick between 1 and {len(items)}.")
        except ValueError:
            print(f"  Not a number. Type 1-{len(items)} to pick, or q to go back.")

    return None


# ---------------------------------------------------------------------------
# Build context dict
# ---------------------------------------------------------------------------

def _build_grafana_urls(node_name: str | None, hostname: str | None,
                        service_tag: str | None = None,
                        netbox_device: str | None = None,
                        ctx: dict | None = None) -> dict:
    """Build Grafana dashboard URLs with rich parameters.

    Uses all available context to pre-fill Grafana dashboard variables
    for a richer experience (IB neighbors, metrics, etc.).
    """
    search_key = node_name or netbox_device or hostname or service_tag
    if not search_key:
        return {}
    base = "https://grafana.int.coreweave.com"

    # Build rich params from context when available
    params = [f"var-search={search_key}"]
    if ctx:
        device_slot = hostname or netbox_device or ""
        if device_slot:
            params.append(f"var-device_slot={device_slot}")
        if service_tag:
            params.append(f"var-serial={service_tag}")
            # BMN = 's' + serial lowercase
            params.append(f"var-bmn=s{service_tag.lower()}")
        # k8s node name
        if node_name:
            params.append(f"var-node={node_name}")
        nb = ctx.get("netbox") or {}
        # IP addresses
        if nb.get("oob_ip"):
            params.append(f"var-bmc_ip={nb['oob_ip'].split('/')[0]}")
        if nb.get("primary_ip4"):
            params.append(f"var-node_ip={nb['primary_ip4'].split('/')[0]}")
        # Site / region
        site = ctx.get("site") or ""
        if site:
            params.append(f"var-zone={site}")
            region = site.rsplit("-", 1)[0] if "-" in site else site
            params.append(f"var-region={region}")
            # Cluster name
            params.append(f"var-cluster=fleetops-{site.lower()}")
        # Rack / location
        rack_loc = ctx.get("rack_location") or ""
        parsed = _parse_rack_location(rack_loc) if rack_loc and "." in rack_loc else None
        if parsed:
            params.append(f"var-rack={parsed['rack']}")
            params.append(f"var-location={parsed['dh']}")
        # Model
        model = nb.get("model") or ""
        if model:
            params.append(f"var-model={model}")

    params_str = "&".join(params)
    return {
        "node_details": f"{base}/d/ddbdicm9sw7c5x/node-details?{params_str}",
        "ib_node_search": f"{base}/d/HguJfdNDR/ib-node-search?var-search={search_key}",
    }


def _build_context(identifier: str, issue: dict,
                   email: str = "", token: str = "") -> dict:
    """Build a structured context dict from a fetched Jira issue."""
    fields = issue.get("fields", {})

    assignee_obj = fields.get("assignee")
    assignee_name = assignee_obj["displayName"] if assignee_obj else None
    assignee_account_id = assignee_obj.get("accountId") if assignee_obj else None
    reporter_obj = fields.get("reporter")
    reporter_name = reporter_obj["displayName"] if reporter_obj else None

    status_obj = fields.get("status") or {}
    issuetype_obj = fields.get("issuetype") or {}
    project_obj = fields.get("project") or {}

    custom = _extract_custom_fields(fields)
    desc_details = _extract_description_details(fields)
    hostname = custom.get("hostname")
    service_tag = custom.get("service_tag")
    node_name = desc_details.get("node_name")

    # Kick off NetBox + SLA in background while we finish parsing Jira fields
    netbox_future = None
    if _netbox_available():
        netbox_future = _executor.submit(
            _build_netbox_context, service_tag, node_name, hostname,
            rack_location=custom.get("rack_location")
        )
    sla_future = _executor.submit(_fetch_sla, identifier, email, token) if email else None

    # Continue CPU-bound parsing (overlaps with NetBox I/O)
    linked = _extract_linked_issues(fields)
    portal_url = _extract_portal_url(fields)

    # Check for linked HO (sync check of links, async fetch if found)
    ho_key = None
    for lnk in linked:
        if lnk.get("key", "").startswith("HO-"):
            ho_key = lnk["key"]
            break
    ho_future = None
    if ho_key and email:
        ho_future = _executor.submit(_jira_get_issue, ho_key, email, token)

    # Lazy comments: store raw data + count; full parsing deferred to [c] handler
    raw_comments = fields.get("comment", {}).get("comments", [])
    comment_count = len(raw_comments)

    # Extract description text (fallback: reporter's first comment)
    desc_adf = fields.get("description")
    description_text = _adf_to_plain_text(desc_adf) if desc_adf else ""
    description_adf = desc_adf  # keep raw ADF for rich rendering
    description_source = "description"
    if not description_text.strip() and raw_comments and reporter_name:
        for cmt in raw_comments:
            cmt_author = (cmt.get("author") or {}).get("displayName", "")
            if cmt_author == reporter_name:
                description_text = _adf_to_plain_text(cmt.get("body", {}))
                description_adf = cmt.get("body", {})
                description_source = "comment"
                break

    # Collect NetBox + SLA results
    netbox = {}
    if netbox_future is not None:
        try:
            netbox = netbox_future.result(timeout=15)
        except Exception:
            netbox = {}

    sla = []
    if sla_future is not None:
        try:
            sla = sla_future.result(timeout=5)
        except Exception:
            pass

    # Collect HO context
    ho_context = None
    if ho_future is not None:
        try:
            ho_issue = ho_future.result(timeout=5)
            if ho_issue:
                ho_context = _summarize_ho_for_dct(ho_issue)
        except Exception:
            pass

    # Fill missing Jira fields from NetBox when available
    netbox_device = netbox.get("device_name") if netbox else None
    if netbox:
        if not hostname:
            hostname = netbox.get("device_name")
        if not custom.get("ip_address") or custom.get("ip_address") == "0.0.0.0":
            nb_ip = netbox.get("primary_ip")
            if nb_ip:
                custom["ip_address"] = nb_ip.split("/")[0]
        if not custom.get("vendor"):
            custom["vendor"] = netbox.get("manufacturer")
        # Backfill rack location from NetBox when Jira is empty
        if not custom.get("rack_location"):
            nb_site = netbox.get("site") or ""
            nb_rack = netbox.get("rack") or ""
            nb_pos = netbox.get("position")
            if nb_rack and nb_pos:
                custom["rack_location"] = f"{nb_site}.DH1.R{nb_rack}.RU{int(nb_pos)}"

    # Backfill rack location from description hostnames (e.g. dh1-r264-node-02-...)
    if not custom.get("rack_location") and desc_details.get("desc_rack"):
        site = custom.get("site") or ""
        dh = desc_details.get("desc_dh") or "DH1"
        rack = desc_details["desc_rack"]
        ru = desc_details.get("desc_ru") or 1
        custom["rack_location"] = f"{site}.{dh}.R{rack}.RU{ru}"

    # Build result first, then enrich grafana URLs with full context
    result = {
        "source": "jira",
        "identifier": identifier,
        "issue_key": issue.get("key", identifier),
        "summary": fields.get("summary", ""),
        "status": status_obj.get("name", "Unknown"),
        "priority": (fields.get("priority") or {}).get("name"),
        "issue_type": issuetype_obj.get("name", "Unknown"),
        "project": project_obj.get("key", "Unknown"),
        "assignee": assignee_name,
        "reporter": reporter_name,
        # Ticket age tracking
        "created": fields.get("created"),
        "updated": fields.get("updated"),
        "status_age_seconds": _parse_jira_timestamp(fields.get("statuscategorychangedate")),
        "_assignee_account_id": assignee_account_id,
        "rack_location": custom.get("rack_location"),       # cf[10207]
        "service_tag": service_tag,                          # cf[10193]
        "hostname": hostname,                                # cf[10192] or NetBox device name
        "site": custom.get("site"),                          # cf[10194]
        "ip_address": custom.get("ip_address"),              # cf[10191] or NetBox mgmt IP
        "vendor": custom.get("vendor"),                      # cf[10210] or NetBox manufacturer
        # Parsed from description
        "rma_reason": desc_details.get("rma_reason"),
        "node_name": node_name,
        "diag_links": desc_details.get("diag_links", []),
        # Comments (lazy-loaded: parsed on first [c] press)
        "comments": [],
        "_raw_comments": raw_comments,
        "_comment_count": comment_count,
        # Related tickets
        "linked_issues": linked,
        # Grafana (placeholder — enriched below with full context)
        "grafana": {},
        # NetBox (optional — empty dict if not configured)
        "netbox": netbox,
        # SLA timers from Jira Service Desk API
        "sla": sla,
        # HO context (linked HO summary, if found)
        "ho_context": ho_context,
        # Description / work order text
        "description_text": description_text.strip(),
        "_description_adf": description_adf,
        "_description_source": description_source,
        # Internal / display-only
        "_portal_url": portal_url,
        "_transitions": None,   # lazy-loaded on first status button press
        "raw_issue": issue,
    }
    result["grafana"] = _build_grafana_urls(node_name, hostname, service_tag, netbox_device, ctx=result)
    return result


# ---------------------------------------------------------------------------
# AI Assistant — OpenAI integration (optional)
# ---------------------------------------------------------------------------

def _ai_available() -> bool:
    """Return True if OpenAI is configured and importable."""
    return _HAS_OPENAI and bool(os.environ.get("OPENAI_API_KEY", "").strip())


def _build_ai_context(ctx: dict) -> str:
    """Serialize a ticket context dict into rich plain text for AI prompts.

    Includes full description, all comments, connection details, HO context,
    and diagnostic info for thorough troubleshooting assistance.
    """
    if not ctx:
        return "(no ticket context loaded)"

    lines = []
    lines.append(f"TICKET: {ctx.get('issue_key', '?')}")
    lines.append(f"SUMMARY: {ctx.get('summary', '?')}")
    status = ctx.get("status", "?")
    age = ctx.get("status_age_seconds")
    age_str = f" (for {_format_age(age)})" if age else ""
    lines.append(f"STATUS: {status}{age_str}")
    lines.append(f"PROJECT: {ctx.get('project', '?')}")
    lines.append(f"TYPE: {ctx.get('issue_type', '?')}")
    lines.append(f"PRIORITY: {ctx.get('priority') or '?'}")
    lines.append(f"ASSIGNEE: {ctx.get('assignee') or 'Unassigned'}")
    lines.append(f"REPORTER: {ctx.get('reporter') or '?'}")
    lines.append(f"SITE: {ctx.get('site') or '?'}")
    lines.append(f"RACK: {ctx.get('rack_location') or '?'}")
    lines.append(f"SERVICE TAG: {ctx.get('service_tag') or '?'}")
    lines.append(f"HOSTNAME: {ctx.get('hostname') or '?'}")
    lines.append(f"VENDOR: {ctx.get('vendor') or '?'}")

    # Extra device fields from NetBox
    nb = ctx.get("netbox", {})
    if nb.get("asset_tag"):
        lines.append(f"ASSET TAG: {nb['asset_tag']}")
    if nb.get("device_type"):
        lines.append(f"MODEL: {nb.get('manufacturer', '')} {nb['device_type']}")
    if nb.get("status"):
        lines.append(f"NB STATUS: {nb['status']}")

    ip = ctx.get("ip_address") or nb.get("primary_ip")
    if ip:
        lines.append(f"IP: {ip}")
    if nb.get("oob_ip"):
        lines.append(f"OOB/BMC IP: {nb['oob_ip']}")

    # RMA reason if present
    if ctx.get("rma_reason"):
        lines.append(f"RMA REASON: {ctx['rma_reason']}")

    # Full description (generous limit for troubleshooting)
    desc = ctx.get("description_text", "")
    if desc:
        if len(desc) > 4000:
            desc = desc[:3997] + "..."
        lines.append(f"\nFULL DESCRIPTION:\n{desc}")

    # All comments — full text, not truncated (critical for troubleshooting)
    comments = ctx.get("comments") or []
    if not comments and ctx.get("_raw_comments"):
        comments = _extract_comments(
            {"comment": {"comments": ctx["_raw_comments"]}}, max_comments=20)
    if comments:
        lines.append(f"\nALL COMMENTS ({len(comments)}):")
        for c in comments:
            body = c.get("body", "")
            if len(body) > 1500:
                body = body[:1497] + "..."
            lines.append(f"  [{c.get('created', '?')}] {c.get('author', '?')}:")
            lines.append(f"    {body}")

    # NetBox connections — full detail for troubleshooting
    ifaces = nb.get("interfaces", [])
    if ifaces:
        lines.append(f"\nNETWORK CONNECTIONS ({len(ifaces)}):")
        for iface in ifaces:
            name = iface.get("name", "?")
            role = iface.get("role", "")
            speed = iface.get("speed", "")
            peer = iface.get("peer_device", "")
            peer_port = iface.get("peer_port", "")
            peer_rack = iface.get("peer_rack", "")
            uncabled = iface.get("_uncabled", False)
            status_str = " [UNCABLED]" if uncabled else ""
            peer_str = f" → {peer}:{peer_port}" if peer else ""
            rack_str = f" (rack {peer_rack})" if peer_rack else ""
            lines.append(f"  {name} ({speed} {role}){peer_str}{rack_str}{status_str}")

    # Linked issues with summaries
    linked = ctx.get("linked_issues", [])
    if linked:
        lines.append(f"\nLINKED ISSUES:")
        for l in linked[:8]:
            rel = l.get("relationship", "")
            lines.append(f"  {l.get('key', '?')} [{l.get('status', '?')}] {rel} — {l.get('summary', '')}")

    # HO context — full detail
    ho = ctx.get("ho_context")
    if ho and isinstance(ho, dict):
        lines.append(f"\nLINKED HO TICKET:")
        lines.append(f"  Key: {ho.get('key', '?')}")
        lines.append(f"  Status: {ho.get('status', '?')}")
        lines.append(f"  Summary: {ho.get('summary', '?')}")
        if ho.get("hint"):
            lines.append(f"  Guidance: {ho['hint']}")
        if ho.get("last_note"):
            lines.append(f"  Last note: {ho['last_note']}")

    # Diagnostic links
    diags = ctx.get("diag_links", [])
    if diags:
        lines.append(f"\nDIAGNOSTIC LINKS:")
        for d in diags:
            lines.append(f"  {d.get('label', '?')}: {d.get('url', '?')}")

    # Grafana URLs
    grafana = ctx.get("grafana", {})
    if grafana:
        for label, url in grafana.items():
            if url:
                lines.append(f"  Grafana {label}: {url}")

    # SLA info
    sla = ctx.get("sla")
    if sla and isinstance(sla, list):
        lines.append(f"\nSLA:")
        for s in sla:
            name = s.get("name", "?")
            ongoing = s.get("ongoingCycle", {})
            if ongoing:
                breached = ongoing.get("breached", False)
                remaining = ongoing.get("remainingTime", {}).get("friendly", "?")
                lines.append(f"  {name}: {'BREACHED' if breached else remaining}")

    result = "\n".join(lines)
    # Increased cap for thorough troubleshooting context
    if len(result) > 20000:
        result = result[:19997] + "..."
    return result


def _ai_chat(messages: list, temperature: float = AI_TEMPERATURE,
             max_tokens: int = AI_MAX_TOKENS, stream: bool = True) -> str:
    """Send messages to OpenAI and stream the response to the terminal.

    Returns the complete response text. Catches all errors gracefully.
    """
    if not _HAS_OPENAI:
        return f"{YELLOW}AI not available. Install: pip install openai{RESET}"

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return f"{YELLOW}AI not available. Set OPENAI_API_KEY in .env{RESET}"

    try:
        client = _openai_mod.OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model=AI_MODEL,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            stream=stream,
        )

        if not stream:
            text = response.choices[0].message.content or ""
            return text

        # Streaming — print tokens as they arrive
        collected = []
        print(f"\n  {CYAN}{BOLD}AI:{RESET} ", end="", flush=True)
        try:
            for chunk in response:
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta and delta.content:
                    token = delta.content
                    # Indent newlines for clean terminal output
                    token = token.replace("\n", f"\n      ")
                    print(token, end="", flush=True)
                    collected.append(delta.content)
        except KeyboardInterrupt:
            pass  # Graceful stop on Ctrl+C
        print(RESET)
        return "".join(collected)

    except Exception as e:
        err_type = type(e).__name__
        if "AuthenticationError" in err_type:
            msg = f"\n  {YELLOW}AI Error: Invalid API key. Check OPENAI_API_KEY in .env{RESET}"
        elif "RateLimitError" in err_type:
            msg = f"\n  {YELLOW}AI Error: Rate limited. Wait a moment and try again.{RESET}"
        elif "APIConnectionError" in err_type:
            msg = f"\n  {YELLOW}AI Error: Could not connect to OpenAI. Check internet.{RESET}"
        else:
            msg = f"\n  {YELLOW}AI Error: {e}{RESET}"
        print(msg)
        return ""


def _ai_summarize(ctx: dict) -> str:
    """Generate a one-shot summary of the current ticket."""
    context_text = _build_ai_context(ctx)
    messages = [
        {"role": "system", "content": AI_SYSTEM_PROMPT_TICKET},
        {"role": "user", "content": (
            "Summarize this ticket. What is the issue, what has been done, "
            "and what should happen next?\n\n" + context_text
        )},
    ]
    return _ai_chat(messages, temperature=0.3)


def _ai_find_ticket(user_description: str, email: str, token: str) -> str | None:
    """Help user find a ticket they can't remember.

    Flow: AI extracts keywords -> JQL search -> AI ranks results.
    Returns the selected ticket key, or None.
    """
    # Step 1: Extract search keywords
    print(f"\n  {DIM}Extracting search terms...{RESET}", flush=True)
    keyword_messages = [
        {"role": "system", "content": AI_SYSTEM_PROMPT_FINDER},
        {"role": "user", "content": (
            f"Extract 2-3 Jira search keywords from this description. "
            f"Return ONLY the keywords separated by spaces, nothing else.\n\n"
            f"Description: {user_description}"
        )},
    ]
    keywords = _ai_chat(keyword_messages, stream=False, temperature=0.2).strip()
    if not keywords or keywords.startswith(YELLOW):
        print(f"  {keywords}")
        return None

    print(f"  {DIM}Searching for:{RESET} {WHITE}{keywords}{RESET}", flush=True)

    # Step 2: Search Jira
    results = _search_by_text(keywords, email, token)
    if not results:
        # Try individual words
        for word in keywords.split():
            if len(word) >= 3:
                results = _search_by_text(word, email, token)
                if results:
                    break
    if not results:
        print(f"\n  {YELLOW}No tickets found matching that description.{RESET}")
        print(f"  {DIM}Try different details or use option 3 (Browse queue).{RESET}")
        return None

    # Step 3: Format results and ask AI to rank
    result_lines = []
    for i, issue in enumerate(results[:10], 1):
        key = issue.get("key", "?")
        summary = issue.get("fields", {}).get("summary", "?")
        status = issue.get("fields", {}).get("status", {}).get("name", "?")
        result_lines.append(f"  {i}. {key} [{status}] — {summary}")

    result_text = "\n".join(result_lines)
    print(f"\n  {DIM}Found {len(results)} results. Ranking...{RESET}\n")

    rank_messages = [
        {"role": "system", "content": AI_SYSTEM_PROMPT_FINDER},
        {"role": "user", "content": (
            f"The user described: \"{user_description}\"\n\n"
            f"Here are the search results:\n{result_text}\n\n"
            f"Rank the top 3 most likely matches and briefly explain why."
        )},
    ]
    _ai_chat(rank_messages, temperature=0.3)

    # Step 4: Let user pick
    print(f"\n\n  {DIM}Select a ticket [1-{min(len(results), 10)}], enter a key, or ENTER to cancel:{RESET}")
    try:
        pick = input("  > ").strip()
    except (EOFError, KeyboardInterrupt):
        return None

    if not pick:
        return None
    if pick.isdigit():
        idx = int(pick) - 1
        if 0 <= idx < len(results):
            return results[idx].get("key")
    if JIRA_KEY_PATTERN.match(pick.upper()):
        return pick.upper()
    return None


def _ai_chat_loop(ctx: dict = None, queue_info: str = "",
                   email: str = "", token: str = "",
                   initial_msg: str = ""):
    """Run an interactive AI chat session. Goes straight into chat.

    If ctx is provided, ticket context is included.
    If queue_info is provided, queue listing is included as context.
    If initial_msg is provided, sends it immediately as first message.
    Supports in-chat ticket lookup: type a ticket key like DO-12345 to load it.
    Exit: 'back', 'quit', 'q', 'exit', or empty Enter.
    Returns a ticket key string if user wants to load one, else None.
    """
    # Build system message
    if ctx:
        system = AI_SYSTEM_PROMPT_TICKET
        context_text = _build_ai_context(ctx)
        label = ctx.get("issue_key", "ticket")
    elif queue_info:
        system = AI_SYSTEM_PROMPT_CHAT
        context_text = queue_info
        label = "queue"
    else:
        system = AI_SYSTEM_PROMPT_CHAT
        context_text = ""
        label = "general"

    # Build user state context (recent tickets, last viewed, bookmarks)
    state_lines = []
    try:
        state = _load_user_state()
        last = state.get("last_ticket")
        if last:
            last_summary = ""
            for r in state.get("recent_tickets", []):
                if r.get("key") == last:
                    last_summary = f" — {r.get('summary', '')}"
                    break
            state_lines.append(f"LAST VIEWED: {last}{last_summary}")
        recents = state.get("recent_tickets", [])
        if recents:
            state_lines.append("RECENT TICKETS:")
            for r in recents[:5]:
                assignee = f" (assigned: {r['assignee']})" if r.get("assignee") else ""
                state_lines.append(f"  {r.get('key', '?')} — {r.get('summary', '?')}{assignee}")
        recent_nodes = state.get("recent_nodes", [])
        if recent_nodes:
            state_lines.append("RECENT NODES:")
            for n in recent_nodes[:5]:
                state_lines.append(f"  {n.get('term', '?')} — {n.get('hostname', '?')} @ {n.get('site', '?')}")
        bookmarks = state.get("bookmarks", [])
        if bookmarks:
            state_lines.append("BOOKMARKS:")
            for bm in bookmarks:
                state_lines.append(f"  {bm.get('label', '?')}")
    except Exception:
        pass

    state_context = "\n".join(state_lines) if state_lines else ""
    if state_context and not context_text:
        context_text = state_context
    elif state_context and context_text:
        context_text = context_text + "\n\n" + state_context

    # Add ticket lookup awareness to system prompt
    enhanced_system = system + (
        "\n\nYou have access to the user's recent tickets, bookmarks, and last viewed ticket. "
        "IMPORTANT COMMANDS you can embed in your responses:\n"
        "- [LOAD:DO-12345] — opens a ticket. Use when the user wants to open/load/go to a ticket you know the key of.\n"
        "- [SEARCH:keywords here] — searches Jira. Use when the user wants to find tickets, filter, or look something up.\n\n"
        "CRITICAL BEHAVIOR:\n"
        "- When the user asks to find, search, filter, or open tickets by person/description/keyword, "
        "DO NOT tell them to retype anything. Instead, embed [SEARCH:their keywords] in your response and "
        "the app will automatically search for them.\n"
        "- Examples: 'can you open joshua tapia tickets' → respond with 'Searching for Joshua Tapia's tickets... [SEARCH:joshua tapia]'\n"
        "- 'find power cycle tickets' → respond with 'Looking for power cycle tickets... [SEARCH:power cycle]'\n"
        "- 'open my last ticket' → respond with [LOAD:DO-90226] (using the actual key from context)\n"
        "- When the user says 'yes' or confirms after you suggest something, DO IT — don't ask again.\n"
        "- Be proactive. If you can figure out what they want, just do it."
    )

    messages = [{"role": "system", "content": enhanced_system}]
    if context_text:
        messages.append({"role": "user", "content": f"Here is my current context:\n\n{context_text}"})
        messages.append({"role": "assistant", "content": "Got it. I can see the context. What would you like to know?"})

    print(f"\n  {CYAN}{BOLD}{'─' * 40}{RESET}")
    print(f"  {CYAN}{BOLD}AI Chat{RESET} {DIM}— {label}{RESET}")
    print(f"  {DIM}Type 'back' to exit  |  'find <desc>' to search tickets{RESET}")
    print(f"  {CYAN}{BOLD}{'─' * 40}{RESET}")

    found_key = None

    # Handle initial message (from unrecognized menu input)
    if initial_msg:
        print(f"\n  {GREEN}You:{RESET} {initial_msg}")
        messages.append({"role": "user", "content": initial_msg})
        response = _ai_chat(messages)
        if response:
            messages.append({"role": "assistant", "content": response})
            load_match = re.search(r'\[LOAD:([A-Z]+-\d+)\]', response)
            if load_match:
                found_key = load_match.group(1)
                print(f"\n  {GREEN}Opening {found_key}...{RESET}")
                return found_key
            search_match = re.search(r'\[SEARCH:([^\]]+)\]', response)
            if search_match and email and token:
                search_terms = search_match.group(1).strip()
                fk = _ai_find_ticket(search_terms, email, token)
                if fk:
                    return fk

    while True:
        try:
            user_input = input(f"\n  {GREEN}You:{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not user_input or user_input.lower() in ("back", "quit", "q", "exit", "b"):
            break

        # Direct ticket key — return it for the caller to load
        if JIRA_KEY_PATTERN.match(user_input.upper()):
            found_key = user_input.upper()
            print(f"\n  {GREEN}Loading {found_key}...{RESET}")
            break

        # "find <description>" — search for a ticket
        if user_input.lower().startswith("find ") and email and token:
            desc = user_input[5:].strip()
            if desc:
                fk = _ai_find_ticket(desc, email, token)
                if fk:
                    found_key = fk
                    break
            continue

        if user_input.lower() == "clear":
            messages = [{"role": "system", "content": enhanced_system}]
            if context_text:
                messages.append({"role": "user", "content": f"Here is my current context:\n\n{context_text}"})
                messages.append({"role": "assistant", "content": "Context reloaded. What would you like to know?"})
            print(f"  {DIM}Chat history cleared.{RESET}")
            continue

        if user_input.lower() == "summary" and ctx:
            _ai_summarize(ctx)
            continue

        messages.append({"role": "user", "content": user_input})
        response = _ai_chat(messages)
        if response:
            messages.append({"role": "assistant", "content": response})

            # Check if AI wants to load a ticket via [LOAD:XX-NNNNN]
            load_match = re.search(r'\[LOAD:([A-Z]+-\d+)\]', response)
            if load_match:
                found_key = load_match.group(1)
                print(f"\n  {GREEN}Opening {found_key}...{RESET}")
                break

            # Check if AI wants to search via [SEARCH:keywords]
            search_match = re.search(r'\[SEARCH:([^\]]+)\]', response)
            if search_match and email and token:
                search_terms = search_match.group(1).strip()
                fk = _ai_find_ticket(search_terms, email, token)
                if fk:
                    found_key = fk
                    break

        # Cap history at 20 messages (keep system + context)
        while len(messages) > 22:
            start_idx = 3 if context_text else 1
            if len(messages) > start_idx + 2:
                del messages[start_idx]
                del messages[start_idx]

    print(f"\n  {DIM}Chat ended.{RESET}")
    return found_key


def _ai_dispatch(ctx: dict = None, email: str = "", token: str = "",
                 queue_info: str = "", initial_msg: str = ""):
    """Universal AI entry point — goes straight into chat.

    If initial_msg is provided, sends it as the first message (for
    unrecognized main menu input routed to AI).
    Returns a ticket key if the user finds one via AI, else None.
    """
    if not _ai_available():
        print(f"\n  {YELLOW}AI not available.{RESET}", end="")
        if not _HAS_OPENAI:
            print(f" Install: {WHITE}pip install openai{RESET}")
        else:
            print(f" Set {WHITE}OPENAI_API_KEY{RESET} in your .env file")
        _brief_pause(1.5)
        return None

    found_key = _ai_chat_loop(ctx=ctx, queue_info=queue_info,
                               email=email, token=token,
                               initial_msg=initial_msg)
    return found_key


# ---------------------------------------------------------------------------
# Core — lookup
# ---------------------------------------------------------------------------

def _fetch_and_show(identifier: str, email: str, token: str,
                    quiet: bool = False) -> dict | None:
    """Fetch a single issue by key or search term, return context dict.

    In interactive mode (quiet=False), shows the pretty output and returns
    the context so callers can offer follow-up actions.
    Returns None if nothing was found or user cancelled.
    """
    # Direct fetch if it looks like a Jira key
    if JIRA_KEY_PATTERN.match(identifier):
        issue = _jira_get_issue(identifier, email, token)
        return _build_context(identifier, issue, email, token)

    # Otherwise, search by text (serial, hostname, etc.)
    if not quiet:
        print(f"  Searching DO/HO for '{identifier}'...\n")

    issues = _search_by_text(identifier, email, token)

    if not issues:
        if not quiet:
            print(f"  {YELLOW}{BOLD}No results{RESET} {DIM}for '{identifier}'.{RESET}")
        return None

    if quiet:
        chosen = issues[0]
    elif len(issues) == 1:
        chosen = issues[0]
    else:
        def _label(i, iss):
            f = iss.get("fields", {})
            st = f.get("status", {}).get("name", "?")
            sc, sd = _status_color(st)
            return f"  {BOLD}{i}.{RESET}  {iss['key']}  {sc}{sd} {st:<18}{RESET} {f.get('summary', '')}"

        print(f"  Found {len(issues)} matches:\n")
        chosen = _prompt_select(issues, _label)
        if chosen in ("refresh", "menu", "quit"):
            return None
        if not chosen:
            return None

    if not quiet:
        print(f"\n  Fetching {chosen['key']}...\n")

    issue = _jira_get_issue(chosen['key'], email, token)
    return _build_context(identifier, issue, email, token)


def get_node_context(identifier: str, quiet: bool = False) -> dict:
    """Public API: given an identifier, return a context dict or exit."""
    email, token = _get_credentials()
    ctx = _fetch_and_show(identifier, email, token, quiet=quiet)
    if ctx is None:
        sys.exit(1)
    return ctx


# ---------------------------------------------------------------------------
# Core — queue
# ---------------------------------------------------------------------------

def _run_stale_verification(stale_issues: list, email: str, token: str) -> str:
    """Show only stale (>48h) verification tickets with age, let user drill in.

    Returns "quit" or "menu" to propagate upward. "back" is handled
    internally by re-displaying the stale list.
    """
    while True:
        _clear_screen()
        print(f"\n  {RED}{BOLD}Stale Verification{RESET} {DIM}— {len(stale_issues)} tickets need action{RESET}")
        print(f"  {'━' * 54}\n")

        def _stale_label(i, iss):
            f = iss.get("fields", {})
            tag = _unwrap_field(f.get("customfield_10193")) or "\u2014"
            summary = f.get("summary", "")[:40]
            age_secs = _parse_jira_timestamp(f.get("statuscategorychangedate"))
            age_fmt = _format_age(age_secs)
            if age_secs > 5 * 86400:
                ac = RED
            else:
                ac = YELLOW
            return (
                f"  {BOLD}{i:>2}.{RESET}  {iss['key']}  "
                f"{ac}{BOLD}{age_fmt:>7}{RESET}  "
                f"{CYAN}{tag:<16}{RESET} "
                f"{DIM}{summary}{RESET}"
            )

        _extra = f", {BOLD}e{RESET} export for Slack"
        chosen = _prompt_select(stale_issues, _stale_label, extra_hint=_extra)

        if chosen == "e":
            # Build a Slack-ready message for engineers
            lines = [
                f":warning: Stale Verification Tickets ({len(stale_issues)})",
                f"These DOs have been in Verification >48h and need review.",
                f"Can the engineers take a look and confirm if these can be closed when you get a chance?\n",
            ]
            for iss in stale_issues:
                lines.append(f"https://coreweave.atlassian.net/browse/{iss['key']}")

            lines.append("")
            lines.append("DCT side work is complete on all of the above. Happy to close any that are confirmed done, or re-engage if something needs follow-up.")
            slack_msg = "\n".join(lines)

            # Copy to clipboard
            try:
                import subprocess as _sp
                _sp.run(["pbcopy"], input=slack_msg.encode(), check=True)
                print(f"\n  {GREEN}{BOLD}Copied to clipboard!{RESET} Paste into your site ops Slack channel.")
            except Exception:
                # Fallback: print it
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

    while True:
        _clear_screen()
        _count_hint = f"top {limit}" if limit <= 20 else f"showing {limit}"
        print(f"\n  {BOLD}{project} queue for {site or 'all sites'}{mine_label}{RESET}  ({_count_hint}){filter_label}\n")

        issues = _search_queue(site, email, token, mine_only=mine_only, limit=limit,
                               status_filter=status_filter, project=project)

        # Record this queue view for suggestions
        if issues:
            _record_queue_view(_load_user_state(), project, site, status_filter, mine_only)

        if not issues:
            filter_display = status_filter.replace("_", " ").title() if status_filter != "all" else "All"
            site_display = site or "all sites"
            print(f"  {YELLOW}{BOLD}{filter_display}{RESET} — {DIM}no {project} tickets for {site_display}{RESET}")
            print(f"\n  {DIM}Try a different status filter or site.{RESET}")
            return "menu"

        def _safe_str(val, default="?"):
            """Force any value to a plain string (unwrap lists, dicts)."""
            if val is None:
                return default
            if isinstance(val, list):
                return str(val[0]) if val else default
            if isinstance(val, dict):
                return str(val.get("name") or val.get("displayName") or val.get("value") or default)
            return str(val)

        def _queue_label(i, iss):
            f = iss.get("fields") or {}
            if not isinstance(f, dict):
                f = {}
            key = _safe_str(iss.get("key"))
            st_obj = f.get("status")
            st = _safe_str(st_obj.get("name") if isinstance(st_obj, dict) else st_obj)
            tag = _safe_str(f.get("customfield_10193"), "\u2014")
            # Unwrap tag if it's nested
            if isinstance(f.get("customfield_10193"), list):
                tag = _safe_str(f["customfield_10193"])
            sc, sd = _status_color(st)
            assignee_obj = f.get("assignee")
            assignee = ""
            if isinstance(assignee_obj, dict):
                assignee = assignee_obj.get("displayName", "")
            elif assignee_obj:
                assignee = _safe_str(assignee_obj, "")
            assignee_str = f"  {DIM}{assignee}{RESET}" if assignee else ""
            # Status age with color
            age_secs = _parse_jira_timestamp(f.get("statuscategorychangedate"))
            if age_secs > 5 * 86400:
                age_str = f"{RED}{_format_age(age_secs):>7}{RESET}"
            elif age_secs > 86400:
                age_str = f"{YELLOW}{_format_age(age_secs):>7}{RESET}"
            elif age_secs > 0:
                age_str = f"{GREEN}{_format_age(age_secs):>7}{RESET}"
            else:
                age_str = "       "
            return (
                f"  {BOLD}{i:>2}.{RESET}  {key}  "
                f"{sc}{sd} {st:<16}{RESET} "
                f"{age_str} "
                f"{CYAN}{tag:<16}{RESET}"
                f"{assignee_str}"
            )

        # Prefetch top tickets in background while user reads the list
        _prefetch_keys = [iss.get("key") for iss in issues[:5] if iss.get("key")]
        for _pk in _prefetch_keys:
            if _pk not in _issue_cache:
                _executor.submit(_jira_get_issue, _pk, email, token)

        # Check if this queue is already bookmarked
        _q_params = {"project": project, "site": site,
                     "status_filter": status_filter, "mine_only": mine_only}
        _q_bookmarked = any(
            b.get("type") == "queue" and b.get("params") == _q_params
            for b in _load_user_state().get("bookmarks", [])
        )
        _bm_hint = f", {BOLD}*{RESET} remove bookmark" if _q_bookmarked else f", {BOLD}*{RESET} bookmark this queue"
        _page_hint = ""
        if len(issues) >= limit:
            _page_hint = f", {BOLD}n{RESET} next page, {BOLD}a{RESET} load all"
        chosen = _prompt_select(issues, _queue_label, extra_hint=_bm_hint + _page_hint)

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

        # Record this ticket view in persistent state
        st = _load_user_state()
        st = _record_ticket_view(st, ctx["issue_key"], ctx.get("summary", ""),
                                    assignee=ctx.get("assignee"), updated=ctx.get("updated"))
        _save_user_state(st)

        _clear_screen()
        _print_pretty(ctx)

        # After viewing, offer follow-up actions
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


# ---------------------------------------------------------------------------
# Core — node history
# ---------------------------------------------------------------------------

def _search_node_history(identifier: str, email: str, token: str,
                         limit: int = 20) -> list:
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
        jql, email, token, max_results=limit,
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
    while True:
        _clear_screen()
        print(f"\n  {BOLD}Node history for '{identifier}'{RESET}  (limit {limit})\n")

        issues = _search_node_history(identifier, email, token, limit=limit)

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
        issue = _jira_get_issue(key, email, token)
        ctx = _build_context(key, issue, email, token)

        # Record this ticket view in persistent state
        st = _load_user_state()
        st = _record_ticket_view(st, ctx["issue_key"], ctx.get("summary", ""),
                                    assignee=ctx.get("assignee"), updated=ctx.get("updated"))
        _save_user_state(st)

        _clear_screen()
        _print_pretty(ctx)

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


# ---------------------------------------------------------------------------
# Queue watcher (foreground, with macOS notifications)
# ---------------------------------------------------------------------------

def _macos_notify(title: str, subtitle: str, message: str):
    """Pop a macOS notification via osascript. Silent no-op on failure."""
    try:
        script = (
            f'display notification "{message}" '
            f'with title "{title}" subtitle "{subtitle}" sound name "Glass"'
        )
        subprocess.run(["osascript", "-e", script],
                       capture_output=True, timeout=5)
    except Exception:
        pass


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
        if line and re.match(r"^[A-Z]+-\d+$", line):
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
    print(f"  {DIM}Type a ticket key anytime to open it{RESET}")
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

                    # Show grab card for each new ticket
                    for key in sorted(new_keys):
                        iss = issue_map.get(key, {})
                        action = _show_grab_card(iss, email, token)
                        if action == "grab":
                            print(f"\n  {DIM}Assigning {key} to you...{RESET}", end="", flush=True)
                            grabbed = _grab_ticket(key, email, token)
                            _issue_cache.pop(key, None)
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

            # Wait for next poll — user can type a ticket key to open it
            ticket_key = _watcher_wait(interval, has_new)
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


# ---------------------------------------------------------------------------
# Weekend auto-assignment (round-robin)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Background watcher (runs in daemon thread, pushes to _watcher_queue)
# ---------------------------------------------------------------------------

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
                            notify_q.put(iss)
                            # macOS notification
                            f = iss.get("fields", {})
                            tag = _unwrap_field(f.get("customfield_10193")) or ""
                            summary = f.get("summary", "")[:50]
                            _macos_notify("CW Node Helper",
                                          f"New {project} ticket",
                                          f"{key} {tag} {summary}")
                known_keys = current_keys
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
    global _watcher_thread, _watcher_site, _watcher_project, _watcher_interval

    if _watcher_thread and _watcher_thread.is_alive():
        return False  # already running

    _watcher_stop_event.clear()
    # Drain any old items from the queue
    while not _watcher_queue.empty():
        try:
            _watcher_queue.get_nowait()
        except queue_mod.Empty:
            break

    _watcher_site = site
    _watcher_project = project
    _watcher_interval = interval

    _watcher_thread = threading.Thread(
        target=_background_watcher_loop,
        args=(email, token, site, project, interval,
              _watcher_stop_event, _watcher_queue, auto_assign_group),
        daemon=True,
    )
    _watcher_thread.start()
    return True


def _stop_background_watcher():
    """Signal the background watcher to stop."""
    global _watcher_thread
    _watcher_stop_event.set()
    if _watcher_thread:
        _watcher_thread.join(timeout=3)
        _watcher_thread = None


def _is_watcher_running() -> bool:
    """Check if the background watcher is alive."""
    return _watcher_thread is not None and _watcher_thread.is_alive()


def _drain_new_tickets() -> list[dict]:
    """Non-blocking: pull all pending new-ticket notifications from the queue."""
    tickets = []
    while True:
        try:
            tickets.append(_watcher_queue.get_nowait())
        except queue_mod.Empty:
            break
    return tickets


def _show_grab_card(issue: dict, email: str, token: str) -> str:
    """Display an inline notification card for a new ticket.

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

    print()
    print(f"  {GREEN}{BOLD}┌─ NEW TICKET ──────────────────────────────────────┐{RESET}")
    print(f"  {GREEN}{BOLD}│{RESET}  {BOLD}{key}{RESET}  {sc}{sd} {status}{RESET}   {CYAN}{tag}{RESET}  {DIM}{rack}{RESET}")
    print(f"  {GREEN}{BOLD}│{RESET}  {summary}")
    if assignee:
        print(f"  {GREEN}{BOLD}│{RESET}  {DIM}Assigned: {assignee}{RESET}")
    else:
        print(f"  {GREEN}{BOLD}│{RESET}  {DIM}Unassigned{RESET}")
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
            _issue_cache.pop(key, None)
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
# Inline display helpers (triggered by hotkeys in action panel)
# ---------------------------------------------------------------------------

def _print_connections_inline(ctx: dict):
    """Print network connections with speed, short names, and NetBox links."""
    netbox = ctx.get("netbox", {})
    if not (netbox and netbox.get("interfaces")):
        return

    ifaces = netbox["interfaces"]

    # Group by role, preserving order
    groups = {}
    for iface in ifaces:
        role = iface.get("role", "\u2014")
        groups.setdefault(role, []).append(iface)

    role_order = ["BMC", "DPU", "IB", "NIC", "\u2014"]
    role_colors = {"BMC": YELLOW, "DPU": MAGENTA, "IB": GREEN, "NIC": CYAN}
    role_hints = {"BMC": "management", "DPU": "data fabric", "IB": "InfiniBand", "NIC": "network"}

    # Split cabled vs uncabled
    cabled_groups = {}
    uncabled_ib = []
    for iface in ifaces:
        if iface.get("_uncabled"):
            uncabled_ib.append(iface)
        else:
            role = iface.get("role", "\u2014")
            cabled_groups.setdefault(role, []).append(iface)

    print(f"\n  {BOLD}Connections{RESET}")
    print(f"  {'━' * 54}")

    num = 0
    all_ifaces = []  # flat list for numbered selection
    for role in role_order:
        if role not in cabled_groups:
            continue
        color = role_colors.get(role, DIM)
        hint = role_hints.get(role, "")
        hint_str = f"  {DIM}({hint}){RESET}" if hint else ""
        print(f"\n  {color}{BOLD}{role}{RESET}{hint_str}")

        for iface in cabled_groups[role]:
            num += 1
            all_ifaces.append(iface)
            port = iface.get("name", "?")
            speed = iface.get("speed", "")
            peer = iface.get("peer_device", "?")
            peer_port = iface.get("peer_port", "?")
            peer_rack = iface.get("peer_rack", "")

            spd = f"{speed:<5}" if speed else "     "
            rack_tag = f" ({peer_rack})" if peer_rack else ""

            print(f"    {BOLD}{num}.{RESET}  {port:<10} {DIM}{spd}{RESET} {DIM}\u2192{RESET}  {BOLD}{peer}{RESET}{DIM}{rack_tag}{RESET}  {DIM}{peer_port}{RESET}")

    # Show IB connections from topology cutsheet (if no cabled IB from NetBox)
    has_netbox_ib = "IB" in cabled_groups
    if not has_netbox_ib:
        hostname = ctx.get("hostname") or netbox.get("device_name") or ""
        ib_topo = _lookup_ib_connections(hostname, ctx.get("rack_location"))
        if ib_topo:
            print(f"\n  {GREEN}{BOLD}IB{RESET}  {DIM}(from cutsheet — {len(ib_topo)} ports){RESET}")
            for conn in ib_topo:
                port = conn.get("port", "?")
                leaf_rack = conn.get("leaf_rack", "?")
                leaf_id = conn.get("leaf_id", "?")
                leaf_port = conn.get("leaf_port", "?")
                print(f"    {DIM}\u25cb{RESET}  {port:<6} {DIM}400G{RESET}  {DIM}\u2192{RESET}  {BOLD}Leaf {leaf_id}{RESET} {DIM}(R{leaf_rack}){RESET}  {DIM}port {leaf_port}{RESET}")
        elif uncabled_ib:
            print(f"\n  {DIM}IB  (not cabled in NetBox — {len(uncabled_ib)} ports){RESET}")
            uncabled_names = sorted([i.get("name", "?") for i in uncabled_ib])
            print(f"    {DIM}{', '.join(uncabled_names)}{RESET}")
        else:
            # No IB data from any source — hint about cutsheet
            role = (netbox.get("device_role") or "").lower()
            if "node" in role or "gpu" in role or not role:
                print(f"\n  {DIM}IB  No IB port data available for this site.{RESET}")
                print(f"    {DIM}Ask admin to upload the IB cutsheet for this data hall.{RESET}")

    # Footer with NetBox cable link hint
    has_cables = any(i.get("cable_id") for i in all_ifaces)
    print(f"\n  {DIM}{len(all_ifaces)} connections{RESET}", end="")
    if has_cables:
        print(f"  {DIM}\u2502  Type # to open cable in NetBox{RESET}")
    else:
        print()

    # Interactive: let user open a cable in NetBox, then clear+reprint
    if has_cables:
        try:
            raw = input(f"\n  > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(all_ifaces):
                cable_id = all_ifaces[idx].get("cable_id")
                if cable_id:
                    api_base = os.environ.get("NETBOX_API_URL", "").strip().rstrip("/")
                    nb_base = api_base.rsplit("/api", 1)[0] if "/api" in api_base else api_base
                    url = f"{nb_base}/dcim/cables/{cable_id}/"
                    print(f"  {DIM}Opening {url}{RESET}")
                    webbrowser.open(url)
        # After interaction, clear and reprint ticket info
        _clear_screen()
        _print_pretty(ctx)
    print()


def _print_linked_inline(ctx: dict):
    """Print linked tickets."""
    if not ctx.get("linked_issues"):
        return
    print()
    print(f"  {BOLD}Linked tickets{RESET}")
    for link in ctx["linked_issues"]:
        lc, ld = _status_color(link["status"])
        print(f"    {BOLD}{link['key']:<12}{RESET} {lc}{ld} {link['status']:<14}{RESET} {DIM}\u2192{RESET} {link['relationship']}")
    print()


def _print_diagnostics_inline(ctx: dict):
    """Print diagnostic links with numbered buttons to open in browser."""
    links = ctx.get("diag_links", [])
    if not links:
        return

    line = "\u2500" * 50
    print(f"\n  {DIM}{line}{RESET}")
    print(f"  {BOLD}{BLUE}Diagnostics{RESET}  {DIM}({len(links)} link{'s' if len(links) != 1 else ''}){RESET}\n")

    for i, dl in enumerate(links, 1):
        label = dl.get("label", "link")
        url = dl.get("url", "")
        # Color-code by link type
        if "grafana" in url.lower():
            color = GREEN
        elif "sherlock" in url.lower() or "sheriff" in url.lower():
            color = YELLOW
        elif "netbox" in url.lower():
            color = CYAN
        else:
            color = MAGENTA
        print(f"    {color}{BOLD}[{i}]{RESET} {WHITE}{label}{RESET}")
        print(f"        {DIM}{url[:80]}{RESET}")
        print()

    print(f"  {DIM}Press [1-{len(links)}] to open in browser, or any key to go back{RESET}\n")

    try:
        pick = input("  > ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return

    if pick.isdigit():
        idx = int(pick) - 1
        if 0 <= idx < len(links):
            url = links[idx]["url"]
            print(f"  {DIM}Opening {links[idx]['label']}...{RESET}")
            webbrowser.open(url)


def _print_sla_detail(ctx: dict):
    """Show detailed SLA breakdown with personalized, ticket-specific DCT guidance."""
    sla_values = ctx.get("sla", [])

    status = ctx.get("status", "")
    age_secs = ctx.get("status_age_seconds", 0)
    age_str = _format_age(age_secs) if age_secs else "unknown"
    issue_key = ctx.get("issue_key", "?")

    # Ticket created age
    created_age = _parse_jira_timestamp(ctx.get("created", ""))
    created_str = _format_age(created_age) if created_age else "unknown"

    # ── Personalization signals (all from ctx, no new API calls) ──────────
    assignee = ctx.get("assignee")
    ho = ctx.get("ho_context")  # {key, status, summary, last_note, hint} or None

    # Last comment info
    last_cmt_age = None
    last_cmt_author = None
    last_cmt_str = None
    raw_comments = ctx.get("_raw_comments", [])
    comment_count = ctx.get("_comment_count", 0) or len(raw_comments)
    if raw_comments:
        _lc = raw_comments[-1]
        last_cmt_author = (_lc.get("author") or {}).get("displayName")
        _lc_ts = _lc.get("created")
        if _lc_ts:
            last_cmt_age = _parse_jira_timestamp(_lc_ts)
            last_cmt_str = _format_age(last_cmt_age)

    # Last modified
    updated_age = _parse_jira_timestamp(ctx.get("updated", ""))
    updated_str = _format_age(updated_age) if updated_age else None

    # Device identifiers for the header
    stag = ctx.get("service_tag") or ctx.get("hostname") or ""

    W = 58  # panel width
    bar = "\u2501" * W
    thin = "\u2500" * W

    # ── Header ────────────────────────────────────────────────────────────
    print()
    title = f"{issue_key} \u2014 {stag}" if stag else issue_key
    print(f"  {BOLD}{WHITE}\u2503 SLA & Ticket Health{RESET}  {DIM}{title}{RESET}")
    print(f"  {bar}")

    # Status + age row
    sc, _ = _status_color(status)
    print(f"  {sc}{BOLD}{status}{RESET}  {DIM}for {age_str}{RESET}  {DIM}\u2502{RESET}  Ticket age: {DIM}{created_str}{RESET}")

    # ── Ticket snapshot ───────────────────────────────────────────────────
    print(f"  {DIM}{thin}{RESET}")
    snap_rows = []
    if assignee:
        snap_rows.append(("Assignee", assignee))
    else:
        snap_rows.append(("Assignee", f"{YELLOW}Unassigned{RESET}"))
    if last_cmt_str:
        who = f"  by {last_cmt_author}" if last_cmt_author else ""
        snap_rows.append(("Last comment", f"{last_cmt_str} ago{who}"))
    else:
        snap_rows.append(("Last comment", f"{YELLOW}None{RESET}"))
    if updated_str:
        snap_rows.append(("Last modified", f"{updated_str} ago"))
    if ho:
        ho_sc, _ = _status_color(ho["status"])
        snap_rows.append(("Linked HO", f"{ho['key']} ({ho_sc}{ho['status']}{RESET})"))
    elif ctx.get("linked_issues"):
        # Has linked issues but no HO specifically
        other_keys = [l["key"] for l in ctx["linked_issues"][:3]]
        snap_rows.append(("Linked", ", ".join(other_keys)))

    for label, value in snap_rows:
        print(f"  {DIM}{label:<14}{RESET} {value}")
    print(f"  {DIM}{thin}{RESET}")
    print()

    # ── Detect ticket category from summary ────────────────────────────────
    summary = ctx.get("summary", "")
    _cat_tag = ""
    import re as _re_sla
    _cat_m = _re_sla.match(r"^DO Ticket:\s*(\S+)\s*-", summary)
    if _cat_m:
        _cat_tag = _cat_m.group(1).upper()
    _s_low = summary.lower()
    diag_links = ctx.get("diag_links") or []
    _diag_text = " ".join(d.get("label", "") for d in diag_links)

    if _cat_tag == "POWER_CYCLE" or "power cycle" in _s_low or "power drain" in _s_low:
        ticket_category = "POWER_CYCLE"
    elif _cat_tag == "PSU_RESEAT" or "psu" in _s_low or "power supply" in _s_low or "psu-health" in _diag_text:
        ticket_category = "PSU_RESEAT"
    elif _re_sla.search(r"low.?light|light.?level|optic", _s_low):
        ticket_category = "LOW_LIGHT"
    elif "flap" in _s_low:
        ticket_category = "PORT_FLAPPING"
    elif _cat_tag == "NETWORK" or _re_sla.search(r"network|nic|link.?down|cable|transceiver|sfp", _s_low):
        ticket_category = "NETWORK"
    elif _cat_tag == "DEVICE" or "device" in _s_low or "hardware" in _s_low:
        ticket_category = "DEVICE"
    elif "failed state" in _s_low:
        ticket_category = "FAILED_STATE"
    elif "coolant" in _s_low or "cdu" in _s_low:
        ticket_category = "COOLING"
    elif "leak" in _s_low:
        ticket_category = "LEAK"
    elif "nvme" in _s_low or "drive" in _s_low or "disk" in _s_low or "ssd" in _s_low:
        ticket_category = "DRIVE"
    elif "gpu" in _s_low or "cold plate" in _s_low:
        ticket_category = "GPU_HARDWARE"
    else:
        ticket_category = _cat_tag if _cat_tag else "OTHER"

    # Category display labels and descriptions
    _CATEGORY_INFO = {
        "POWER_CYCLE": {
            "label": "POWER CYCLE",
            "color": YELLOW,
            "verify_tips": [
                "Confirm node came back online \u2014 ping BMC + OS IP",
                "Check Grafana for pre-cycle metrics (thermal, GPU errors, kernel panics)",
                "If repeat offender (>2 cycles) \u2192 escalate to hardware (suspect PSU, mobo, or BMC FW)",
                "If no POST after cycle \u2192 AC power drain (pull both PSU cords 30s)",
            ],
            "work_tips": [
                "Power drain node for duration specified in description",
                "Reseat PSU cords firmly after drain period",
                "Verify BMC comes back, then OS boots",
                "Add comment with result: did it POST? BMC reachable? OS up?",
            ],
        },
        "PSU_RESEAT": {
            "label": "PSU RESEAT",
            "color": YELLOW,
            "verify_tips": [
                "Verify both PSUs show green/healthy in iDRAC after reseat",
                "Check PDU outlet is live if PSU still shows fault",
                "If PSU fault persists after reseat \u2192 RMA the PSU",
                "Confirm node has redundant power (both PSUs active)",
            ],
            "work_tips": [
                "Fully remove PSU, inspect bay for dust/debris",
                "Reseat firmly until latch clicks",
                "Check the other PSU too \u2014 single-PSU = degraded redundancy",
                "Verify both PSUs healthy in iDRAC, then move to verification",
            ],
        },
        "NETWORK": {
            "label": "NETWORK",
            "color": CYAN,
            "verify_tips": [
                "Confirm link is up and stable (no flapping in last 24h)",
                "Check TOR switch port for CRC errors, drops",
                "Verify no new NETWORK tickets for same rack (shared TOR issue?)",
                "Check IB connectivity if applicable",
            ],
            "work_tips": [
                "Check TOR switch port status \u2014 link up/down?",
                "Reseat cable at both NIC and TOR side",
                "If single-port: reseat cable \u2192 test \u2192 swap cable \u2192 swap SFP",
                "If multi-port same switch: escalate as TOR issue",
            ],
        },
        "LOW_LIGHT": {
            "label": "LOW LIGHT",
            "color": MAGENTA,
            "verify_tips": [
                "Confirm light levels are within spec after cleaning/swap",
                "Check both TX and RX \u2014 one side low = dirty, both = bad optic",
                "If multiple ports on same switch had low light, verify all are clean",
            ],
            "work_tips": [
                "Clean fiber ends with IPA wipes (both ends)",
                "Reseat SFP modules on both ends",
                "If still low after clean \u2192 replace the optic",
                "Use optical power meter if available to verify dB levels",
            ],
        },
        "PORT_FLAPPING": {
            "label": "PORT FLAPPING",
            "color": MAGENTA,
            "verify_tips": [
                "Confirm port has been stable for 24h+ (no flap events in logs)",
                "Check switch logs: show log | grep swpN",
                "Verify remote end (node NIC) is also stable",
            ],
            "work_tips": [
                "Check cable integrity \u2014 swap with known-good",
                "Look for bent pins on SFP/port",
                "Check if remote end (node NIC) is also flapping",
                "If persists after cable swap \u2192 try different switch port",
            ],
        },
        "DEVICE": {
            "label": "DEVICE",
            "color": WHITE,
            "verify_tips": [
                "Ping BMC + OS IP \u2014 both reachable?",
                "Check Grafana node_details dashboard for health overview",
                "Verify no new alerts since your fix",
                "If multiple DEVICE tickets in same rack \u2192 check shared infra",
            ],
            "work_tips": [
                "Ping BMC/oob_ip \u2014 if unreachable, physical visit needed",
                "Pull Grafana node_details for health overview",
                "Check NetBox status (Active? Decommissioning?)",
                "Look at linked HO tickets for upstream context",
            ],
        },
        "OTHER": {
            "label": "OTHER",
            "color": DIM,
            "verify_tips": [
                "Read description + comments \u2014 'OTHER' = automation couldn't classify",
                "Could be firmware, config change, or multi-symptom",
                "Check linked tickets for additional context",
            ],
            "work_tips": [
                "Read the description carefully for the actual ask",
                "Check linked tickets and comments for context",
                "May need manual triage \u2014 look at node in Grafana",
            ],
        },
        "FAILED_STATE": {
            "label": "FAILED STATE",
            "color": RED,
            "verify_tips": [
                "Node should be back online and healthy in Grafana",
                "Check BMC reachability + OS IP",
                "If HO is still open, the fix may not be final yet",
            ],
            "work_tips": [
                "Check BMC reachability first \u2014 if down, likely POWER_CYCLE or PSU",
                "If BMC up \u2192 check Grafana for root cause (GPU, NVMe, memory, network)",
                "If node up but degraded \u2192 likely DEVICE issue",
                "This ticket will likely convert to DEVICE, POWER_CYCLE, or NETWORK",
            ],
        },
        "COOLING": {
            "label": "COOLING / CDU",
            "color": CYAN,
            "verify_tips": [
                "CDU issues stay in HO \u2014 DCT assists with physical access only",
                "Verify coolant levels are back to normal",
                "Do NOT power cycle nodes on the affected cooling loop",
            ],
            "work_tips": [
                "CDU / cooling \u2014 typically HO-only, not a DO task",
                "DCT may need to provide physical access or visual inspection",
                "Do NOT power cycle nodes on the affected cooling loop",
            ],
        },
        "LEAK": {
            "label": "LEAK",
            "color": RED,
            "verify_tips": [
                "Confirm no active liquid present after cleanup",
                "Verify affected components are dry and undamaged",
                "Check neighboring nodes for splash damage",
            ],
            "work_tips": [
                "Pull node from rack ASAP \u2014 safety first",
                "Check for liquid damage on motherboard, GPU trays, NVMe slots",
                "Likely full node RMA \u2014 document with photos",
                "Check if leak source is CDU, cold plate, or fitting",
            ],
        },
        "DRIVE": {
            "label": "DRIVE / NVMe",
            "color": MAGENTA,
            "verify_tips": [
                "Confirm drive is detected and reporting in OS",
                "Check Grafana for disk health metrics post-swap",
                "Verify no SMART errors on the replacement",
            ],
            "work_tips": [
                "NVMe reseat first \u2014 reseat in slot, verify in OS",
                "If still not reporting \u2192 swap with known-good from parts stock",
                "RMA the dead drive via HO workflow",
                "Check for multi-drive failures (could indicate backplane issue)",
            ],
        },
        "GPU_HARDWARE": {
            "label": "GPU HARDWARE",
            "color": YELLOW,
            "verify_tips": [
                "Confirm GPU is detected and healthy in Grafana post-fix",
                "Check cold plate sensor readings are normal",
                "Verify no thermal throttling events",
            ],
            "work_tips": [
                "Cold plate sensor fail = cooling loop issue on GPU tray",
                "Reseat GPU tray \u2014 check thermal paste / cold plate contact",
                "If sensor still fails after reseat \u2192 RMA GPU + cold plate assembly",
                "Check cooling loop for leaks or low flow",
            ],
        },
    }

    cat_info = _CATEGORY_INFO.get(ticket_category, _CATEGORY_INFO["OTHER"])

    def _print_category_badge():
        """Print the detected category as a colored badge."""
        c = cat_info["color"]
        print()
        print(f"  {c}{BOLD}[{cat_info['label']}]{RESET}")
        print()

    def _print_category_tips(tip_key: str):
        """Print category-specific tips (verify_tips or work_tips)."""
        tips = cat_info.get(tip_key, [])
        if tips:
            print(f"  {DIM}{thin}{RESET}")
            print(f"  {BOLD}{WHITE}Category Tips \u2014 {cat_info['label']}{RESET}")
            print()
            for tip in tips:
                print(f"    {DIM}\u25b8 {tip}{RESET}")
            print()
            print(f"  {DIM}{thin}{RESET}")
            print()

    # ── Status-based personalized guidance ────────────────────────────────
    status_lower = status.lower()

    # Helper: build a list of personalized observations as bullet points
    def _observations() -> list:
        """Return ticket-specific observation strings for guidance."""
        obs = []
        # Comment activity
        if not raw_comments:
            obs.append(f"{YELLOW}\u25b8{RESET} No comments on this ticket at all")
        elif last_cmt_age is not None and last_cmt_age > 7 * 86400:
            obs.append(
                f"{YELLOW}\u25b8{RESET} Last comment was {last_cmt_str} ago"
                f" \u2014 nobody has asked to keep it open")
        elif last_cmt_age is not None and last_cmt_age > 48 * 3600:
            obs.append(
                f"{DIM}\u25b8{RESET} Last comment {last_cmt_str} ago by {last_cmt_author or '?'}")
        else:
            obs.append(
                f"{GREEN}\u25b8{RESET} Recent comment {last_cmt_str} ago"
                f" by {last_cmt_author or '?'}")

        # HO status
        if ho:
            ho_st = ho["status"].lower()
            if ho_st in ("closed", "done", "resolved"):
                obs.append(
                    f"{GREEN}\u25b8{RESET} Linked {ho['key']} is {GREEN}Closed{RESET}"
                    f" \u2014 nothing blocking")
            elif ho_st in ("on hold", "waiting for support"):
                obs.append(
                    f"{BLUE}\u25b8{RESET} Linked {ho['key']} is {BLUE}On Hold{RESET}"
                    f" \u2014 check if they need this DO open")
            else:
                obs.append(
                    f"{DIM}\u25b8{RESET} Linked {ho['key']} is {ho['status']}")
        else:
            obs.append(f"{DIM}\u25b8{RESET} No linked HO \u2014 this DO stands on its own")

        # Assignee
        if not assignee:
            obs.append(
                f"{YELLOW}\u25b8{RESET} Unassigned \u2014 claim with [a] before taking action")

        return obs

    # ·· Verification — stale (>7 days) ····································
    if status_lower == "verification" and age_secs > 7 * 86400:
        days = age_secs / 86400
        print(f"  {RED}{BOLD}\u26a0  STALE VERIFICATION{RESET}  {DIM}({days:.0f} days){RESET}")
        _print_category_badge()
        print()

        # Personalized assessment
        # Determine recommendation based on signals
        ho_blocking = ho and ho["status"].lower() not in ("closed", "done", "resolved")
        recent_comments = last_cmt_age is not None and last_cmt_age < 48 * 3600
        should_investigate = ho_blocking or recent_comments

        if should_investigate:
            print(f"  {YELLOW}{BOLD}Recommendation: INVESTIGATE BEFORE CLOSING{RESET}")
        else:
            print(f"  {GREEN}{BOLD}Recommendation: CLOSE THIS TICKET{RESET}")
        print()

        # Show personalized observations
        print(f"  {BOLD}What I see on {issue_key}:{RESET}")
        for obs in _observations():
            print(f"    {obs}")
        print()

        if not should_investigate:
            print(f"  {WHITE}This DO has been in Verification for {days:.0f} days with no{RESET}")
            if not raw_comments:
                print(f"  {WHITE}comments at all. Your work appears done.{RESET}")
            elif last_cmt_age and last_cmt_age > 7 * 86400:
                print(f"  {WHITE}activity in {last_cmt_str}. Your work appears done.{RESET}")
            else:
                print(f"  {WHITE}recent blockers. Your work appears done.{RESET}")
            print()
            print(f"  {BOLD}Close it:{RESET}")
            print(f"    {GREEN}\u25b8{RESET} Press {GREEN}{BOLD}[k]{RESET} to close. Suggested comment:")
            print(f"    {DIM}\u250c{'─' * 52}{RESET}")
            print(f"    {DIM}\u2502 Work performed: [short summary of what you did].{RESET}")
            print(f"    {DIM}\u2502 Current state: LEDs normal; Grafana/IB healthy;{RESET}")
            print(f"    {DIM}\u2502 no recurring alerts during >{days:.0f}d in Verification.{RESET}")
            print(f"    {DIM}\u2502 Closing DO per DCT workflow. If issue returns,{RESET}")
            print(f"    {DIM}\u2502 please reopen or submit a new DO.{RESET}")
            print(f"    {DIM}\u2514{'─' * 52}{RESET}")
        else:
            print(f"  {WHITE}This DO has been in Verification for {days:.0f} days but{RESET}")
            if ho_blocking:
                print(f"  {WHITE}linked {ho['key']} is still {ho['status']} \u2014 check{RESET}")
                print(f"  {WHITE}if they need this DO to stay open.{RESET}")
            if recent_comments:
                print(f"  {WHITE}there was a recent comment ({last_cmt_str} ago) \u2014{RESET}")
                print(f"  {WHITE}read it before closing. Press [c] to view.{RESET}")
            print()
            print(f"  {BOLD}If everything is actually fine:{RESET}")
            print(f"    {GREEN}\u25b8{RESET} Press {GREEN}{BOLD}[k]{RESET} to close with a summary comment")
            print()
            print(f"  {BOLD}If something is still active:{RESET}")
            print(f"    {YELLOW}\u25b8{RESET} Press {YELLOW}{BOLD}[z]{RESET} to resume In Progress")
            print(f"    {YELLOW}\u25b8{RESET} Or {YELLOW}{BOLD}[y]{RESET} On Hold + comment + @ the right engineer")

        # Escalation guidance for stale tickets
        print()
        print(f"  {BOLD}Escalate stale tickets:{RESET}")
        print(f"    {CYAN}\u25b8{RESET} Post in your site ops channel with a list of stale DOs")
        print(f"    {CYAN}\u25b8{RESET} For each: confirm what you verified (power, cabling, diags)")
        print(f"    {CYAN}\u25b8{RESET} Ping the owning engineer/SOE by name if blocked on them")
        print(f"    {DIM}  Don\u2019t just wait \u2014 Slack escalation is expected for old tickets.{RESET}")
        print(f"    {DIM}  Use the stale list (press v from main menu) to export all at once.{RESET}")
        print()
        _print_category_tips("verify_tips")

    # ·· Verification — past 48h window ····································
    elif status_lower == "verification" and age_secs > 48 * 3600:
        days = age_secs / 86400
        print(f"  {YELLOW}{BOLD}\u25b2  VERIFICATION > 48h{RESET}  {DIM}({days:.1f} days){RESET}")
        _print_category_badge()
        print(f"  {WHITE}{issue_key} is past the standard 24\u201348h observation window.{RESET}")
        print(f"  {WHITE}If your runbook work is done and the node is healthy,{RESET}")
        print(f"  {WHITE}it\u2019s normal and expected for you to close this DO yourself.{RESET}")
        print()

        # Show personalized observations
        print(f"  {BOLD}What I see:{RESET}")
        for obs in _observations():
            print(f"    {obs}")
        print()

        # Quick recommendation
        ho_blocking = ho and ho["status"].lower() not in ("closed", "done", "resolved")
        if ho_blocking:
            print(f"  {YELLOW}\u25b8{RESET} {ho['key']} is still {ho['status']} \u2014 verify it doesn\u2019t need this DO open")
        if not raw_comments:
            print(f"  {DIM}\u25b8 No comments \u2014 nobody has asked to keep it open.{RESET}")
        print()
        print(f"    {GREEN}\u25b8{RESET} All good \u2192 Press {GREEN}{BOLD}[k]{RESET} to close")
        print(f"    {YELLOW}\u25b8{RESET} Unsure  \u2192 Press {YELLOW}{BOLD}[z]{RESET} to resume and investigate")
        print()
        _print_category_tips("verify_tips")

    # ·· Verification — within normal window ·······························
    elif status_lower == "verification":
        print(f"  {GREEN}{BOLD}\u25cf  PENDING VERIFICATION{RESET}")
        _print_category_badge()
        print(f"  {WHITE}{issue_key} is within the 24\u201348h observation window.{RESET}")
        print(f"  {WHITE}You\u2019ve done the runbook. Now confirm the fix is holding:{RESET}")
        print(f"    {DIM}\u25b8 LEDs look good{RESET}")
        print(f"    {DIM}\u25b8 Grafana / IB dashboards are clean{RESET}")
        print(f"    {DIM}\u25b8 No new alerts{RESET}")
        print()
        _print_category_tips("verify_tips")
        if assignee:
            print(f"  {DIM}Assigned to {assignee}.{RESET}")
        if created_age and created_age < 4 * 3600:
            print(f"  {GREEN}\u2605{RESET}  {GREEN}Moving fast \u2014 ticket is under 4 hours old. Keep it up.{RESET}")
        elif created_age and created_age < 24 * 3600:
            print(f"  {DIM}\u25b8 Good pace \u2014 same-day verification.{RESET}")
        print(f"  {DIM}Once confirmed healthy \u2192 close the ticket with [k].{RESET}")
        print(f"  {DIM}Don\u2019t let it sit here for weeks \u2014 that\u2019s a smell.{RESET}")

    # ·· In Progress — long-running (>24h) ·································
    elif status_lower == "in progress" and age_secs > 24 * 3600:
        print(f"  {YELLOW}{BOLD}\u25b2  LONG RUNNING{RESET}  {DIM}In Progress for {age_str}{RESET}")
        _print_category_badge()
        print(f"  {WHITE}{issue_key} has been In Progress for over 24 hours.{RESET}")
        print()

        # Personalized context
        if not raw_comments:
            print(f"  {YELLOW}\u25b8{RESET} No comments yet \u2014 add one noting your progress or blocker.")
        elif last_cmt_age and last_cmt_age > 24 * 3600:
            print(f"  {DIM}\u25b8 Last comment {last_cmt_str} ago by {last_cmt_author or '?'}.{RESET}")
        if assignee:
            print(f"  {DIM}\u25b8 Assigned to {assignee}.{RESET}")
        print()

        print(f"  {BOLD}Are you blocked?{RESET}")
        print(f"    {YELLOW}\u25b8{RESET} Waiting on parts/vendor \u2192 {YELLOW}{BOLD}[y]{RESET} On Hold {DIM}(pauses SLA clock){RESET}")
        print(f"    {DIM}  Add a comment: what you\u2019re waiting on and from whom.{RESET}")
        print()
        print(f"  {BOLD}Is the work done?{RESET}")
        print(f"    {GREEN}\u25b8{RESET} Runbook complete \u2192 {GREEN}{BOLD}[v]{RESET} Verify {DIM}(start 24\u201348h watch window){RESET}")
        print()
        print(f"  {BOLD}Still actively working?{RESET}")
        print(f"    {DIM}\u25b8 No action needed \u2014 just be aware the SLA clock is running.{RESET}")
        print()
        _print_category_tips("work_tips")

    # ·· In Progress — normal ··············································
    elif status_lower == "in progress":
        print(f"  {CYAN}{BOLD}\u25cf  IN PROGRESS{RESET}")
        _print_category_badge()
        if assignee:
            print(f"  {DIM}{issue_key} assigned to {assignee}. SLA clock is running.{RESET}")
        else:
            print(f"  {YELLOW}\u25b8{RESET} {issue_key} is unassigned \u2014 claim with {BOLD}[a]{RESET} first.")
        print()
        _print_category_tips("work_tips")
        if created_age and age_secs:
            if age_secs < 1800:
                print(f"  {GREEN}\u2605{RESET}  {GREEN}Jumped on it fast \u2014 started within 30 min. SLA happy.{RESET}")
            elif age_secs < 2 * 3600:
                print(f"  {DIM}\u25b8 In progress for {_format_age(age_secs)} \u2014 you\u2019re on it.{RESET}")
        print(f"  {DIM}Complete the runbook work, then:{RESET}")
        print(f"    {GREEN}\u25b8{RESET} {GREEN}{BOLD}[v]{RESET} Verify  {DIM}\u2014 move to Verification for 24\u201348h watch{RESET}")
        print(f"    {YELLOW}\u25b8{RESET} {YELLOW}{BOLD}[y]{RESET} Hold    {DIM}\u2014 if waiting on parts/vendor (pauses clock){RESET}")

    # ·· On Hold / Waiting ·················································
    elif status_lower in ("on hold", "waiting for support"):
        print(f"  {BLUE}{BOLD}\u25a0  PAUSED{RESET}  {DIM}On Hold for {age_str}{RESET}")
        _print_category_badge()
        if assignee:
            print(f"  {DIM}{issue_key} assigned to {assignee}. SLA clock is paused.{RESET}")
        else:
            print(f"  {DIM}{issue_key} \u2014 SLA clock is paused.{RESET}")
        print()

        # Personalized nudge if on hold for a long time
        if age_secs > 7 * 86400:
            days = age_secs / 86400
            print(f"  {YELLOW}\u25b8{RESET} On Hold for {days:.0f} days \u2014 has the blocker been resolved?")
            if last_cmt_str:
                print(f"  {DIM}\u25b8 Last comment {last_cmt_str} ago \u2014 follow up if needed.{RESET}")
            print()

        print(f"  {BOLD}When ready to continue:{RESET}")
        print(f"    {GREEN}\u25b8{RESET} Press {GREEN}{BOLD}[z]{RESET} to resume In Progress")
        print(f"    {DIM}\u25b8 Add a comment noting what unblocked you.{RESET}")
        print()
        print(f"  {BOLD}If the blocker resolves the issue entirely:{RESET}")
        print(f"    {DIM}\u25b8 Resume \u2192 Verify \u2192 Close (normal flow).{RESET}")
        print(f"    {DIM}\u25b8 Don\u2019t skip Verification \u2014 watch for 24\u201348h to confirm.{RESET}")

    # ·· New / To Do ·······················································
    elif status_lower in ("to do", "new", "waiting for triage"):
        print(f"  {WHITE}{BOLD}\u25cb  NOT STARTED{RESET}")
        _print_category_badge()
        if assignee:
            print(f"  {DIM}{issue_key} is assigned to {assignee} but hasn\u2019t been started.{RESET}")
        else:
            print(f"  {DIM}{issue_key} is waiting for someone to pick it up.{RESET}")
        if created_age and created_age > 4 * 3600:
            print(f"  {RED}\u25b8{RESET} {RED}Created {created_str} ago \u2014 this is way past the 30 min SLA target.{RESET}")
            print(f"    {DIM}Grab it now or flag it in your site channel.{RESET}")
        elif created_age and created_age > 1800:
            print(f"  {YELLOW}\u25b8{RESET} Created {created_str} ago \u2014 SLA target is < 30 min to start.")
        else:
            print(f"  {GREEN}\u25b8{RESET} {GREEN}Fresh ticket \u2014 just came in. Grab it before someone else does.{RESET}")
        print(f"    {GREEN}\u25b8{RESET} Press {GREEN}{BOLD}[s]{RESET} to start work {DIM}(auto-assigns to you){RESET}")
        print()
        _print_category_tips("work_tips")

    # ·· Closed ····························································
    elif status_lower in ("closed", "done", "resolved"):
        print(f"  {GREEN}{BOLD}\u2713  CLOSED{RESET}  {DIM}No action needed{RESET}")
        _print_category_badge()
        print(f"  {DIM}{issue_key}")
        if assignee:
            print(f"  Closed by / last assigned to: {assignee}{RESET}")
        if last_cmt_str:
            print(f"  {DIM}Last comment {last_cmt_str} ago by {last_cmt_author or '?'}{RESET}")
        print(f"  {DIM}If the issue returns, reopen or create a new DO.{RESET}")
        print()
        # Timing feedback based on total ticket lifetime
        if created_age:
            if created_age < 4 * 3600:
                print(f"  {GREEN}{BOLD}\u2605{RESET}  {GREEN}Crushed it \u2014 closed in under 4 hours. Fast hands.{RESET}")
            elif created_age < 24 * 3600:
                print(f"  {GREEN}\u2605{RESET}  {WHITE}Solid turnaround \u2014 opened and closed same day.{RESET}")
            elif created_age < 3 * 86400:
                print(f"  {DIM}\u25b8 Closed within 3 days \u2014 right on track.{RESET}")
            elif created_age < 7 * 86400:
                print(f"  {YELLOW}\u25b8{RESET} {DIM}Took about a week. Not bad, but could be tighter.{RESET}")
            elif created_age < 14 * 86400:
                print(f"  {YELLOW}\u25b8{RESET} {YELLOW}This one dragged \u2014 {created_age / 86400:.0f} days from open to close.{RESET}")
                print(f"    {DIM}Next time: if blocked, put it On Hold sooner so it\u2019s visible.{RESET}")
            else:
                print(f"  {RED}\u25b8{RESET} {RED}{created_age / 86400:.0f} days to close \u2014 that\u2019s a long time for a DO.{RESET}")
                print(f"    {DIM}If this was waiting on HO/vendor, On Hold keeps the queue clean.{RESET}")
                print(f"    {DIM}If it was forgotten, the stale list (v) catches these early.{RESET}")

    else:
        print(f"  {DIM}Status: {status}{RESET}")

    print()
    print(f"  {DIM}{thin}{RESET}")

    # ── Jira SLA timers ───────────────────────────────────────────────────
    if sla_values:
        print(f"  {BOLD}Jira SLA Timers{RESET}")
        print()

        for sla in sla_values:
            sla_name = sla.get("name", "?")
            ongoing = sla.get("ongoingCycle")
            completed = sla.get("completedCycles", [])

            if not ongoing and completed:
                last = completed[-1]
                breached = last.get("breached", False)
                elapsed = (last.get("elapsedTime") or {}).get("friendly", "?")
                goal = (last.get("goalDuration") or {}).get("friendly", "?")
                if breached:
                    print(f"  {RED}\u2716 BREACHED{RESET}  {WHITE}{sla_name}{RESET}")
                    print(f"             {DIM}Goal: {goal}  \u2502  Took: {elapsed}{RESET}")
                else:
                    print(f"  {GREEN}\u2714 MET{RESET}       {WHITE}{sla_name}{RESET}")
                    print(f"             {DIM}Goal: {goal}  \u2502  Completed in: {elapsed}{RESET}")

            elif ongoing:
                elapsed = (ongoing.get("elapsedTime") or {}).get("friendly", "?")
                remaining = (ongoing.get("remainingTime") or {}).get("friendly", "?")
                goal = (ongoing.get("goalDuration") or {}).get("friendly", "?")

                if ongoing.get("breached"):
                    print(f"  {RED}\u2716 BREACHED{RESET}  {WHITE}{sla_name}{RESET}")
                    print(f"             {DIM}Goal: {goal}  \u2502  Elapsed: {elapsed}  \u2502  Over by: {remaining}{RESET}")
                elif ongoing.get("paused"):
                    print(f"  {BLUE}\u25a0 PAUSED{RESET}    {WHITE}{sla_name}{RESET}")
                    print(f"             {DIM}Goal: {goal}  \u2502  Remaining: {remaining}  \u2502  Clock stopped{RESET}")
                else:
                    remaining_ms = (ongoing.get("remainingTime") or {}).get("millis", 0)
                    goal_ms = (ongoing.get("goalDuration") or {}).get("millis", 1)
                    pct = remaining_ms / goal_ms if goal_ms else 0
                    if pct < 0.25:
                        color, icon, label = RED, "\u25bc", "CRITICAL"
                    elif pct < 0.50:
                        color, icon, label = YELLOW, "\u25b2", "WARNING"
                    else:
                        color, icon, label = GREEN, "\u25cf", "OK"
                    print(f"  {color}{icon} {label}{RESET}     {WHITE}{sla_name}{RESET}")
                    print(f"             {DIM}Goal: {goal}  \u2502  Remaining: {remaining}  \u2502  Elapsed: {elapsed}{RESET}")

            print()

        print(f"  {DIM}{thin}{RESET}")
    else:
        print(f"  {DIM}No Jira SLA timer attached to this ticket.{RESET}")
        print(f"  {DIM}Guidance above is based on status and ticket age.{RESET}")
        print()

    # ── DCT Reference Card ────────────────────────────────────────────────
    print(f"  {BOLD}DCT Quick Reference{RESET}")
    print()
    print(f"  {BOLD}SLA Targets{RESET}")
    print(f"    {DIM}New \u2192 In Progress     {RESET}{CYAN}< 30 min{RESET}")
    print(f"    {DIM}In Progress \u2192 Verify  {RESET}{CYAN}< 4 hours{RESET}  {DIM}(routine){RESET}")
    print(f"    {DIM}Verify \u2192 Close        {RESET}{CYAN}< 48 hours{RESET}")
    print()
    print(f"  {BOLD}Clock Rules{RESET}")
    print(f"    {GREEN}\u25b6{RESET} {DIM}Running{RESET}   In Progress, Verification")
    print(f"    {BLUE}\u23f8{RESET} {DIM}Paused{RESET}    On Hold, Waiting for Support/Vendor")
    print(f"    {WHITE}\u23f9{RESET} {DIM}Stopped{RESET}   Closed, Done, Resolved")
    print()
    print(f"  {BOLD}Verification \u2014 What It Means{RESET}")
    print(f"    {DIM}You\u2019ve done the runbook (reseat/swap/clean/recable/etc.).{RESET}")
    print(f"    {DIM}Now you\u2019re watching 24\u201348h to confirm the fix holds:{RESET}")
    print(f"    {DIM}LEDs good, Grafana clean, IB healthy, no new alerts.{RESET}")
    print()
    print(f"  {BOLD}Rule of Thumb{RESET}")
    print(f"    {DIM}DO = your work. If your part is done and stable, close it.{RESET}")
    print(f"    {DIM}Don\u2019t park DOs in Verification for weeks \u2014 it clutters{RESET}")
    print(f"    {DIM}the queue and keeps the SLA clock running for nothing.{RESET}")
    print(f"    {DIM}After 24\u201348h clean: close. After days/weeks: close or escalate.{RESET}")
    print()
    print(f"  {BOLD}Stale Ticket Escalation{RESET}")
    print(f"    {DIM}1. Post in your site ops channel (e.g. #ops-us-central-07a-...){RESET}")
    print(f"    {DIM}   with a list of DOs \u226530 days in Verification.{RESET}")
    print(f"    {DIM}2. For each: note what you verified (power, cabling, diags).{RESET}")
    print(f"    {DIM}3. Ping the owning engineer/SOE by name \u2014 don\u2019t just wait.{RESET}")
    print(f"    {DIM}4. In Jira: add a comment, then close or move to Failed Verification.{RESET}")
    print(f"    {DIM}Use the stale list (v from main menu, then e to export) to share.{RESET}")
    print()


def _find_linked_ho(ctx: dict, email: str, token: str) -> dict | None:
    """Find the HO ticket linked to a DO. Returns HO issue dict or None.

    1. Check issuelinks for HO- keys (instant, no API call)
    2. Fallback: JQL search by service tag + site
    """
    # Check direct links first
    for link in ctx.get("linked_issues", []):
        key = link.get("key", "")
        if key.startswith("HO-"):
            try:
                return _jira_get_issue(key, email, token)
            except Exception:
                pass

    # Fallback: search by service tag + site
    tag = ctx.get("service_tag") or ""
    site = ctx.get("site") or ""
    if tag:
        try:
            jql = f'project = HO AND cf[10193] ~ "{tag}"'
            if site:
                jql += f' AND cf[10194] ~ "{site}"'
            jql += " ORDER BY created DESC"
            issues = _jql_search(jql, email, token, max_results=1,
                                 fields=ISSUE_DETAIL_FIELDS)
            if issues:
                return issues[0]
        except Exception:
            pass

    return None


def _summarize_ho_for_dct(ho_issue: dict) -> dict:
    """Build a compact HO summary dict for DCT display."""
    f = ho_issue.get("fields", {})
    key = ho_issue.get("key", "?")
    status = (f.get("status") or {}).get("name", "?")
    summary = f.get("summary", "")[:80]

    # Status-based hint
    sl = status.lower()
    if any(s in sl for s in ["rma-initiate", "sent to dct uc", "uncable"]):
        hint = "Uncable/Unrack RMA phase — expect or check for an Uncable DO."
    elif any(s in sl for s in ["sent to dct rc", "recable", "ready for verification"]):
        hint = "Recable/verification phase — expect or check for a Recable DO."
    elif "rma" in sl:
        hint = "RMA flow in progress — vendor/FROps handling parts."
    else:
        hint = "HO tracks full node history and vendor/RMA workflow."

    # Last comment (first line only)
    last_note = ""
    comments_container = f.get("comment", {})
    comments = comments_container.get("comments", []) if isinstance(comments_container, dict) else []
    if comments:
        body = comments[-1].get("body", "")
        if isinstance(body, dict):
            # ADF — extract first text node
            for node in (body.get("content") or []):
                for child in (node.get("content") or []):
                    if child.get("type") == "text" and child.get("text", "").strip():
                        last_note = child["text"].strip()[:80]
                        break
                if last_note:
                    break
        elif isinstance(body, str):
            last_note = body.strip().split("\n")[0][:80]

    return {
        "key": key,
        "status": status,
        "summary": summary,
        "hint": hint,
        "last_note": last_note,
    }


def _show_mrb_for_node(ctx: dict, email: str, token: str):
    """Search MRB project for RMA/parts tickets related to the current node."""
    tag = ctx.get("service_tag") or ""
    host = ctx.get("hostname") or ""
    site = ctx.get("site") or ""
    search_term = tag or host
    if not search_term:
        print(f"\n  {DIM}No service tag or hostname to search MRB.{RESET}")
        return

    # Build JQL: search MRB by service tag (or hostname) + site
    jql = f'project = MRB AND text ~ "{_escape_jql(search_term)}"'
    if site:
        jql += f' AND cf[10194] ~ "{_escape_jql(site)}"'
    jql += " ORDER BY created DESC"

    print(f"\n  {DIM}Searching MRB for '{search_term}'...{RESET}")
    issues = _jql_search(jql, email, token, max_results=10,
                         fields=["summary", "status", "assignee",
                                 "customfield_10193", "customfield_10194"])

    if not issues:
        print(f"\n  {YELLOW}{BOLD}No MRB tickets{RESET} {DIM}found for this node.{RESET}")
        return

    print(f"\n  {BOLD}MRB tickets{RESET}  {DIM}({len(issues)} found for {search_term}){RESET}\n")
    for i, iss in enumerate(issues, 1):
        f = iss.get("fields", {})
        key = iss.get("key", "?")
        st = f.get("status", {}).get("name", "?")
        sc, sd = _status_color(st)
        summary = f.get("summary", "")[:50]
        assignee = (f.get("assignee") or {}).get("displayName") or ""
        asg = f"  {DIM}{assignee}{RESET}" if assignee else ""
        print(f"    {BOLD}{i:>2}.{RESET}  {BOLD}{key:<12}{RESET} {sc}{sd} {st}{RESET}  {DIM}{summary}{RESET}{asg}")

    url_base = f"{JIRA_BASE_URL}/browse/"
    print(f"\n  {DIM}Open in Jira: {url_base}<KEY>{RESET}")


def _show_sdx_for_ticket(ctx: dict, email: str, token: str):
    """Find the originating SDx (customer) ticket for a DO/HO."""
    # 1. Check directly linked issues for SDx projects
    sdx_links = []
    for link in ctx.get("linked_issues", []):
        key = link.get("key", "")
        proj = key.split("-")[0] if "-" in key else ""
        if proj in SDX_PROJECTS:
            sdx_links.append(link)

    if sdx_links:
        print(f"\n  {BOLD}Linked SDx tickets{RESET}\n")
        for link in sdx_links:
            sc, sd = _status_color(link.get("status", ""))
            print(f"    {BOLD}{link['key']:<12}{RESET} {sc}{sd} {link['status']}{RESET}  {DIM}{link.get('summary', '')[:50]}{RESET}")
            print(f"    {DIM}{JIRA_BASE_URL}/browse/{link['key']}{RESET}")
        print()
        return

    # 2. Fallback: search SDx by service tag + site
    tag = ctx.get("service_tag") or ""
    site = ctx.get("site") or ""
    search_term = tag or ctx.get("hostname") or ""
    if not search_term:
        print(f"\n  {DIM}No SDx link found and no service tag to search.{RESET}")
        return

    jql = f'project in (SDA, SDE, SDO, SDP, SDS) AND text ~ "{_escape_jql(search_term)}"'
    if site:
        jql += f' AND cf[10194] ~ "{_escape_jql(site)}"'
    jql += " ORDER BY created DESC"

    print(f"\n  {DIM}No direct SDx link. Searching by '{search_term}'...{RESET}")
    issues = _jql_search(jql, email, token, max_results=5,
                         fields=["summary", "status", "assignee", "reporter",
                                 "customfield_10193", "customfield_10194"])

    if not issues:
        print(f"\n  {YELLOW}{BOLD}No SDx ticket{RESET} {DIM}found (no direct link and no match on service tag + site).{RESET}")
        return

    print(f"\n  {BOLD}SDx tickets{RESET}  {DIM}(matched by search){RESET}\n")
    for i, iss in enumerate(issues, 1):
        f = iss.get("fields", {})
        key = iss.get("key", "?")
        st = f.get("status", {}).get("name", "?")
        sc, sd = _status_color(st)
        summary = f.get("summary", "")[:50]
        reporter = (f.get("reporter") or {}).get("displayName") or ""
        rep = f"  {DIM}Reporter: {reporter}{RESET}" if reporter else ""
        print(f"    {BOLD}{i:>2}.{RESET}  {BOLD}{key:<12}{RESET} {sc}{sd} {st}{RESET}  {DIM}{summary}{RESET}{rep}")
        print(f"         {DIM}{JIRA_BASE_URL}/browse/{key}{RESET}")
    print()


def _print_rack_neighbors(devices: list, current_device_name: str | None,
                          show_netbox_hint: bool = False):
    """Display a numbered list of rack neighbors and let the user pick one.

    Returns the chosen device dict, "x" for NetBox, or None if cancelled.
    """
    if not devices:
        print(f"\n  {DIM}No devices found in this rack.{RESET}")
        return None

    print(f"\n  {BOLD}Devices in rack{RESET}  {DIM}({len(devices)} devices){RESET}\n")

    def _label(i, dev):
        name = dev.get("name") or dev.get("display") or "?"
        short = _short_device_name(name)
        pos = dev.get("position")
        pos_str = f"U{int(pos):<3}" if pos else "U?  "
        status_label = (dev.get("status") or {}).get("label") or "?"
        sc, sd = _status_color(status_label)

        is_current = current_device_name and name == current_device_name
        marker = f"  {YELLOW}<-- you{RESET}" if is_current else ""
        name_fmt = f"{CYAN}{BOLD}{short}{RESET}" if is_current else short

        return (
            f"    {BOLD}{i:>2}.{RESET}  {DIM}{pos_str}{RESET} "
            f"{name_fmt:<18}"
            f"{sc}{sd} {status_label}{RESET}"
            f"{marker}"
        )

    extra = f", {BOLD}x{RESET} for NetBox" if show_netbox_hint else ""
    return _prompt_select(devices, _label, extra_hint=extra)


def _draw_neighbor_panel(neighbor_data: dict) -> dict:
    """Draw the adjacent-racks panel below the current rack's device list.

    neighbor_data has keys "left" and "right", each either None or
    {"rack_num": int, "rack_id": int|None, "devices": list}.

    Returns {"L": [devices], "R": [devices]} for prompt selection mapping.
    """
    import shutil
    term_w = shutil.get_terminal_size((80, 24)).columns
    side_by_side = term_w >= 100

    left = neighbor_data.get("left")
    right = neighbor_data.get("right")
    if not left and not right:
        return {"L": [], "R": []}

    print(f"\n  {DIM}{'─' * 2} Adjacent Racks {'─' * (min(term_w, 70) - 20)}{RESET}")

    # Build header
    left_hdr = ""
    right_hdr = ""
    if left:
        n_devs = len(left.get("devices", []))
        lbl = f"R{left['rack_num']}" if left["rack_num"] else "?"
        left_hdr = f"  {BOLD}<<{RESET} {BOLD}{lbl}{RESET}  {DIM}({n_devs} devices){RESET}"
    if right:
        n_devs = len(right.get("devices", []))
        lbl = f"R{right['rack_num']}" if right["rack_num"] else "?"
        right_hdr = f"{BOLD}{lbl}{RESET}  {DIM}({n_devs} devices){RESET} {BOLD}>>{RESET}"

    if side_by_side:
        # Pad left header to ~half width
        pad = max(2, (term_w // 2) - 20)
        print(f"\n  {left_hdr}{'':>{pad}}{right_hdr}" if left_hdr and right_hdr
              else f"\n  {left_hdr}{right_hdr}")
    else:
        if left_hdr:
            print(f"\n  {left_hdr}")
        if right_hdr:
            print(f"  {right_hdr}")

    # Build device lines for each side
    def _dev_lines(devices, prefix):
        lines = []
        for i, dev in enumerate(devices, 1):
            name = dev.get("name") or dev.get("display") or "?"
            short = _short_device_name(name)
            pos = dev.get("position")
            pos_str = f"U{int(pos):<3}" if pos else "U?  "
            status_label = (dev.get("status") or {}).get("label") or "?"
            sc, _ = _status_color(status_label)
            lines.append(
                f"   {BOLD}{prefix}{i}.{RESET} {DIM}{pos_str}{RESET} "
                f"{short:<16} {sc}{status_label}{RESET}"
            )
        return lines

    left_lines = _dev_lines(left["devices"], "L") if left and left.get("devices") else []
    right_lines = _dev_lines(right["devices"], "R") if right and right.get("devices") else []

    if side_by_side:
        # Print left and right side by side
        max_rows = max(len(left_lines), len(right_lines))
        # Calculate raw width of a left line (approx 40 visible chars)
        col_w = max(38, (term_w // 2) - 2)
        print()
        for row_i in range(max_rows):
            l_str = left_lines[row_i] if row_i < len(left_lines) else ""
            r_str = right_lines[row_i] if row_i < len(right_lines) else ""
            if l_str and r_str:
                # Pad left column using visible length
                visible_len = len(l_str.encode("ascii", "ignore").decode())
                # ANSI escapes make raw len > visible len; estimate padding
                print(f"{l_str}{'':>{max(2, col_w - 35)}}{r_str}")
            else:
                if l_str:
                    print(l_str)
                elif r_str:
                    print(f"{'':>{col_w}}{r_str}")
    else:
        # Stacked: left first, then right
        if left_lines:
            print()
            for ln in left_lines:
                print(ln)
        if right_lines:
            print()
            for ln in right_lines:
                print(ln)

    # Show empty-rack messages
    if left and not left.get("devices"):
        if left.get("rack_id") is None:
            print(f"\n   {DIM}R{left['rack_num']} — not found in NetBox{RESET}")
        else:
            print(f"\n   {DIM}R{left['rack_num']} — empty{RESET}")
    if right and not right.get("devices"):
        if right.get("rack_id") is None:
            print(f"   {DIM}R{right['rack_num']} — not found in NetBox{RESET}")
        else:
            print(f"   {DIM}R{right['rack_num']} — empty{RESET}")

    print()
    return {
        "L": left["devices"] if left else [],
        "R": right["devices"] if right else [],
    }


def _print_netbox_info_inline(device: dict, email: str = "", token: str = ""):
    """Rich view for a device with no open Jira tickets.

    Builds a lightweight ctx from the raw NetBox device dict, then uses
    _print_pretty and the full action panel so the user gets rack map,
    connections, elevation, etc. — everything except Jira ticket data.
    """
    dev_name = device.get("name") or device.get("display") or "?"
    serial = device.get("serial") or ""
    site_obj = device.get("site") or {}
    rack_obj = device.get("rack") or {}
    rack_id = rack_obj.get("id")
    position = device.get("position")
    primary_ip = (device.get("primary_ip") or {}).get("address", "").split("/")[0]
    oob_ip = (device.get("oob_ip") or {}).get("address", "").split("/")[0]
    status_label = (device.get("status") or {}).get("label") or "?"
    device_id = device.get("id")
    device_type_obj = device.get("device_type") or {}
    manufacturer_obj = device_type_obj.get("manufacturer") or {}

    # Build a ctx that _print_pretty and _post_detail_prompt can use
    netbox_ctx = {
        "device_name": dev_name,
        "device_id": device_id,
        "serial": serial,
        "asset_tag": device.get("asset_tag"),
        "site": site_obj.get("display") or site_obj.get("name"),
        "rack": rack_obj.get("display") or rack_obj.get("name"),
        "rack_id": rack_id,
        "position": position,
        "primary_ip": (device.get("primary_ip") or {}).get("address"),
        "primary_ip4": (device.get("primary_ip4") or {}).get("address"),
        "primary_ip6": (device.get("primary_ip6") or {}).get("address"),
        "oob_ip": (device.get("oob_ip") or {}).get("address"),
        "status": status_label,
        "device_role": (device.get("role") or device.get("device_role") or {}).get("display"),
        "platform": (device.get("platform") or {}).get("display"),
        "manufacturer": manufacturer_obj.get("display") or manufacturer_obj.get("name"),
        "model": device_type_obj.get("display") or device_type_obj.get("model"),
        "interfaces": [],
        "site_slug": site_obj.get("slug") or "",
    }

    # Fetch interfaces for connections
    if device_id and _netbox_available():
        try:
            ifaces = _netbox_get_interfaces(device_id)
            cabled_names = set()
            for iface in ifaces:
                cable = iface.get("cable")
                link_peers = iface.get("link_peers") or []
                full_name = iface.get("display") or iface.get("name") or "?"
                port_name = full_name.split(":")[-1] if ":" in full_name else full_name
                if not cable or not link_peers:
                    continue
                cabled_names.add(port_name)
                peer = link_peers[0]
                peer_dev = peer.get("device", {})
                peer_name_full = peer_dev.get("display") or peer_dev.get("name") or "?"
                peer_port = peer.get("display") or peer.get("name") or "?"
                peer_port_short = peer_port.split(":")[-1] if ":" in peer_port else peer_port
                peer_short = _short_device_name(peer_name_full)
                rack_match = re.search(r"-r(\d{2,4})", peer_name_full.lower())
                peer_rack = f"R{rack_match.group(1).lstrip('0') or '0'}" if rack_match else ""
                cable_id = cable.get("id") if isinstance(cable, dict) else None
                role = _classify_port_role(port_name)
                speed = _parse_iface_speed(iface.get("type"))
                netbox_ctx["interfaces"].append({
                    "name": port_name, "role": role, "speed": speed,
                    "peer_device": peer_short, "peer_device_full": peer_name_full,
                    "peer_port": peer_port_short, "peer_rack": peer_rack,
                    "cable_id": cable_id,
                    "connected_to": f"{peer_name_full}:{peer_port}",
                })
            # Include uncabled IB ports so DCTs can see they exist
            for iface in ifaces:
                full_name = iface.get("display") or iface.get("name") or "?"
                port_name = full_name.split(":")[-1] if ":" in full_name else full_name
                if port_name in cabled_names:
                    continue
                role = _classify_port_role(port_name)
                if role != "IB":
                    continue
                speed = _parse_iface_speed(iface.get("type"))
                netbox_ctx["interfaces"].append({
                    "name": port_name, "role": role, "speed": speed,
                    "peer_device": None, "peer_device_full": None,
                    "peer_port": None, "peer_rack": "",
                    "cable_id": None,
                    "connected_to": None,
                    "_uncabled": True,
                })
        except Exception:
            pass

    # Build a pseudo-ctx for _print_pretty and the action panel
    grafana = _build_grafana_urls(None, dev_name, serial or None, dev_name)

    # Try to find rack_location from NetBox rack name + position
    rack_location = ""
    site_code = site_obj.get("name") or ""
    rack_name = rack_obj.get("name") or ""
    if rack_name and position:
        rack_location = f"{site_code}.DH1.R{rack_name}.RU{int(position)}"

    ctx = {
        "source": "netbox",
        "identifier": serial or dev_name,
        "issue_key": f"{GREEN}{BOLD}No open tickets{RESET}",
        "summary": _short_device_name(dev_name),
        "status": status_label,
        "issue_type": "NetBox Device",
        "project": "\u2014",
        "assignee": None,
        "reporter": None,
        "rack_location": rack_location,
        "service_tag": serial,
        "hostname": dev_name,
        "site": site_obj.get("display") or site_obj.get("name"),
        "ip_address": primary_ip,
        "vendor": netbox_ctx.get("manufacturer"),
        "rma_reason": None,
        "node_name": None,
        "diag_links": [],
        "comments": [],
        "linked_issues": [],
        "grafana": grafana,
        "netbox": netbox_ctx,
        "_portal_url": None,
        "raw_issue": {},
    }

    _clear_screen()
    _print_pretty(ctx)

    # Recent history
    if email and token and serial:
        try:
            issues = _search_node_history(serial, email, token, limit=3)
            if issues:
                print(f"  {BOLD}Recent history{RESET}\n")
                for iss in issues[:3]:
                    f = iss.get("fields", {})
                    key = iss.get("key", "?")
                    st = f.get("status", {}).get("name", "?")
                    created = f.get("created", "")[:10]
                    summary = f.get("summary", "")[:45]
                    isc, isd = _status_color(st)
                    print(f"    {BOLD}{key:<12}{RESET} {isc}{isd} {st:<14}{RESET} {DIM}{created}  {summary}{RESET}")
                print()
        except Exception:
            pass

    # Give user the full action panel so they can use rack map, connections, etc.
    if email and token:
        action = _post_detail_prompt(ctx, email, token)
        # Only propagate "quit"; everything else returns to the caller
        if action == "quit":
            return "quit"
    else:
        input(f"  {DIM}Press ENTER to return...{RESET}")

    return None


def _handle_rack_neighbors(ctx: dict, email: str, token: str) -> str | None:
    """Handle the [w] Rack Neighbors flow.

    Returns a navigation action string, or None to stay in the action panel.
    """
    netbox = ctx.get("netbox", {})
    rack_id = netbox.get("rack_id")
    if not rack_id:
        print(f"\n  {DIM}No rack info available.{RESET}")
        return None

    rack_name = netbox.get("rack") or f"rack {rack_id}"
    current_device = netbox.get("device_name")

    print(f"\n  {DIM}Loading devices in {rack_name}...{RESET}")
    devices = _netbox_get_rack_devices(rack_id)

    if not devices:
        print(f"\n  {DIM}No devices found in {rack_name}.{RESET}")
        return None

    chosen = _print_rack_neighbors(devices, current_device)
    if not chosen:
        return None

    # Build search identifier: prefer serial, fall back to device name
    chosen_serial = chosen.get("serial")
    chosen_name = chosen.get("name") or chosen.get("display")
    search_term = chosen_serial or chosen_name

    if not search_term:
        print(f"\n  {DIM}Selected device has no serial or name to search.{RESET}")
        return None

    # Search Jira for this device
    print(f"\n  {DIM}Searching Jira for '{search_term}'...{RESET}")
    issues = _search_node_history(search_term, email, token, limit=20)

    if issues:
        print(f"  Found {len(issues)} ticket(s).\n")
        result = _run_history_interactive(email, token, search_term)
        return result if result == "quit" else None
    else:
        _print_netbox_info_inline(chosen, email, token)
        return None


def _handle_rack_view(ctx: dict, email: str, token: str) -> str | None:
    """Combined rack elevation + neighbor selection + NetBox link.

    Shows the visual rack elevation, then a numbered device list below,
    followed by adjacent-rack panels with L/R navigation.
    Returns a navigation action string, or None to stay in the action panel.
    """
    # Allow re-centering on neighbor racks via < / >
    current_ctx = ctx
    while True:
        netbox = current_ctx.get("netbox", {})
        rack_id = netbox.get("rack_id")
        if not rack_id:
            print(f"\n  {DIM}No rack info available.{RESET}")
            return None

        current_device = netbox.get("device_name")

        # Draw the visual elevation (also fetches and returns devices)
        devices = _draw_rack_elevation(current_ctx)
        if devices:
            print(f"\n  {BOLD}{len(devices)} devices{RESET} {DIM}loaded{RESET}")
        else:
            # CDU / empty rack — show minimal header, continue to neighbors
            rack_label = netbox.get("rack") or "?"
            print(f"\n  {DIM}R{rack_label} — no devices in NetBox (CDU / sidecar){RESET}")

        # --- Fetch and show adjacent racks ---
        neighbor_map = {"L": [], "R": []}
        neighbor_data = None
        rack_loc = current_ctx.get("rack_location", "")
        parsed = _parse_rack_location(rack_loc)
        # Fallback: derive rack number from NetBox rack name (e.g. "64")
        if not parsed:
            rname = netbox.get("rack", "")
            if rname and rname.isdigit():
                site = current_ctx.get("site") or ""
                parsed = {"site_code": site.split(".")[0] if "." in site else site,
                          "dh": "DH1", "rack": int(rname), "ru": None}

        if parsed:
            site_code = parsed["site_code"]
            dh = parsed["dh"]
            layout = _get_dh_layout(site_code, dh)
            if layout is None and dh.upper() == "DH1" and any(
                s in site_code.upper() for s in ("EVI01", "CENTRAL-07")
            ):
                layout = {
                    "racks_per_row": 10,
                    "columns": [
                        {"label": "Left",  "start": 1,   "num_rows": 14},
                        {"label": "Right", "start": 141,  "num_rows": 18},
                    ],
                    "serpentine": True,
                    "entrance": "bottom-right",
                }
            if layout:
                site_slug = netbox.get("site_slug")
                if not site_slug and parsed.get("site_code"):
                    site_slug = parsed["site_code"].lower()
                try:
                    neighbor_data = _fetch_neighbor_devices(
                        parsed["rack"], layout, site_slug)
                    neighbor_map = _draw_neighbor_panel(neighbor_data)
                except Exception:
                    pass  # graceful degradation — skip neighbors

        # --- Enhanced prompt ---
        has_left = bool(neighbor_map.get("L"))
        has_right = bool(neighbor_map.get("R"))

        # Build button-style prompt (matches ticket action panel)
        def btn(key_char, label, color):
            return f"{color}{BOLD}[{key_char}]{RESET} {WHITE}{label}{RESET}"

        hint_parts = []
        if devices:
            hint_parts.append(btn("d", "Pick Device", CYAN))
        if has_left:
            hint_parts.append(btn(f"L1\u2011L{len(neighbor_map['L'])}", "Left", YELLOW))
        if has_right:
            hint_parts.append(btn(f"R1\u2011R{len(neighbor_map['R'])}", "Right", YELLOW))
        if neighbor_data and neighbor_data.get("left"):
            hint_parts.append(btn("<", "Move Left", MAGENTA))
        if neighbor_data and neighbor_data.get("right"):
            hint_parts.append(btn(">", "Move Right", MAGENTA))
        hint_parts.append(btn("#", "Go to Cab", DIM))
        hint_parts.append(btn("x", "NetBox", GREEN))
        hint_parts.append(btn("\u21b5", "Back", DIM))

        line = "\u2500" * 50
        prompt_text = f"\n  {DIM}{line}{RESET}\n\n  {'   '.join(hint_parts)}\n\n  > "

        for _ in range(3):
            try:
                raw = input(prompt_text).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return None

            if raw.lower() in ("q", "quit", "exit", "b", "back", ""):
                return None

            # Device picker — show full list on clean screen
            if raw.lower() == "d" and devices:
                _clear_screen()
                rack_label = netbox.get("rack") or "?"
                print(f"\n  {BOLD}R{rack_label} — Pick a device{RESET}  {DIM}({len(devices)} devices){RESET}\n")
                for i, dev in enumerate(devices, 1):
                    dname = dev.get("name") or dev.get("display") or "?"
                    short = _short_device_name(dname)
                    pos = dev.get("position")
                    pos_str = f"U{int(pos):<3}" if pos else "U?  "
                    status_label = (dev.get("status") or {}).get("label") or "?"
                    sc, sd = _status_color(status_label)
                    is_current = current_device and dname == current_device
                    marker = f"  {YELLOW}<-- you{RESET}" if is_current else ""
                    name_fmt = f"{CYAN}{BOLD}{short}{RESET}" if is_current else short
                    print(
                        f"    {BOLD}{i:>2}.{RESET}  {DIM}{pos_str}{RESET} "
                        f"{name_fmt:<18}{sc}{sd} {status_label}{RESET}{marker}"
                    )
                print()
                try:
                    pick = input(f"  {DIM}Pick 1-{len(devices)}, or ENTER to go back:{RESET} ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    return None
                if not pick:
                    _clear_screen()
                    break  # re-render the rack view
                try:
                    idx = int(pick)
                    if 1 <= idx <= len(devices):
                        chosen = devices[idx - 1]
                        chosen_serial = chosen.get("serial")
                        chosen_name = chosen.get("name") or chosen.get("display")
                        search_term = chosen_serial or chosen_name
                        if search_term:
                            print(f"\n  {DIM}Searching Jira for '{search_term}'...{RESET}")
                            issues = _search_node_history(search_term, email, token, limit=20)
                            if issues:
                                print(f"  Found {len(issues)} ticket(s).\n")
                                result = _run_history_interactive(email, token, search_term)
                                return result if result == "quit" else None
                            else:
                                _print_netbox_info_inline(chosen, email, token)
                                return None
                except ValueError:
                    pass
                _clear_screen()
                break  # re-render the rack view

            # NetBox shortcut
            if raw.lower() == "x":
                api_base = os.environ.get("NETBOX_API_URL", "").strip().rstrip("/")
                nb_base = api_base.rsplit("/api", 1)[0] if "/api" in api_base else api_base
                url = f"{nb_base}/dcim/racks/{rack_id}/"
                print(f"  {DIM}Opening {url}{RESET}")
                webbrowser.open(url)
                return None

            # Navigation: re-center on neighbor rack
            if raw in ("<", ">"):
                side = "left" if raw == "<" else "right"
                nd = neighbor_data.get(side) if neighbor_data else None
                if not nd or not nd.get("rack_id"):
                    print(f"  {DIM}No {side} neighbor to navigate to.{RESET}")
                    continue
                # Build a lightweight ctx for the neighbor rack
                current_ctx = dict(current_ctx)  # shallow copy
                nb_copy = dict(netbox)
                nb_copy["rack_id"] = nd["rack_id"]
                nb_copy["rack"] = str(nd["rack_num"])
                nb_copy["device_name"] = None  # no highlighted device
                nb_copy["position"] = None
                current_ctx["netbox"] = nb_copy
                # Update rack_location to match new rack
                if parsed:
                    rl = f"{parsed['site_code']}.{parsed['dh']}.R{nd['rack_num']}"
                    current_ctx["rack_location"] = rl
                _clear_screen()
                break  # break inner prompt loop → re-enter outer while loop
            else:
                # Parse selection: L3, R1, or plain number
                chosen = None
                raw_upper = raw.upper()
                if raw_upper.startswith("L") and raw_upper[1:].isdigit():
                    idx = int(raw_upper[1:])
                    devs = neighbor_map.get("L", [])
                    if 1 <= idx <= len(devs):
                        chosen = devs[idx - 1]
                    else:
                        print(f"  Out of range. Left rack has {len(devs)} devices.")
                        continue
                elif raw_upper.startswith("R") and raw_upper[1:].isdigit():
                    idx = int(raw_upper[1:])
                    devs = neighbor_map.get("R", [])
                    if 1 <= idx <= len(devs):
                        chosen = devs[idx - 1]
                    else:
                        print(f"  Out of range. Right rack has {len(devs)} devices.")
                        continue
                elif raw.isdigit():
                    # Jump to any cab by number
                    target_rack = int(raw)
                    site_slug = netbox.get("site_slug")
                    if not site_slug and parsed and parsed.get("site_code"):
                        site_slug = parsed["site_code"].lower()
                    rack_obj = _netbox_find_rack_by_name(str(target_rack), site_slug)
                    if rack_obj and rack_obj.get("id"):
                        current_ctx = dict(current_ctx)
                        nb_copy = dict(netbox)
                        nb_copy["rack_id"] = rack_obj["id"]
                        nb_copy["rack"] = rack_obj.get("name") or str(target_rack)
                        nb_copy["device_name"] = None
                        nb_copy["position"] = None
                        current_ctx["netbox"] = nb_copy
                        if parsed:
                            current_ctx["rack_location"] = f"{parsed['site_code']}.{parsed['dh']}.R{target_rack}"
                        _clear_screen()
                        break  # re-enter outer while loop with new rack
                    else:
                        print(f"  {DIM}Rack {target_rack} not found in NetBox.{RESET}")
                        continue
                else:
                    print(f"  {DIM}Try d, L#, R#, <, >, cab#, or ENTER.{RESET}")
                    continue

                if chosen:
                    chosen_serial = chosen.get("serial")
                    chosen_name = chosen.get("name") or chosen.get("display")
                    search_term = chosen_serial or chosen_name
                    if not search_term:
                        print(f"\n  {DIM}Selected device has no serial or name to search.{RESET}")
                        return None
                    print(f"\n  {DIM}Searching Jira for '{search_term}'...{RESET}")
                    issues = _search_node_history(search_term, email, token, limit=20)
                    if issues:
                        print(f"  Found {len(issues)} ticket(s).\n")
                        result = _run_history_interactive(email, token, search_term)
                        return result if result == "quit" else None
                    else:
                        _print_netbox_info_inline(chosen, email, token)
                        return None
                return None
        else:
            # Exhausted prompt retries
            return None


# ---------------------------------------------------------------------------
# Action panel (visually prominent hotkey buttons)
# ---------------------------------------------------------------------------

def _print_action_panel(ctx: dict):
    """Render the color-coded action button panel below ticket info."""
    line = "\u2500" * 50

    def btn(key_char, label, color):
        """Render a single button:  [x] Label  with colored bold bracket."""
        return f"{color}{BOLD}[{key_char}]{RESET} {WHITE}{label}{RESET}"

    print(f"  {DIM}{line}{RESET}")
    print()

    # --- Group 1: Inline views ---
    view_items = []
    if ctx.get("rack_location"):
        view_items.append(btn("r", "Rack Map", CYAN))
    netbox = ctx.get("netbox", {})
    if netbox and netbox.get("interfaces"):
        view_items.append(btn("n", "Connections", MAGENTA))
    if ctx.get("description_text"):
        desc_label = "Close Description" if ctx.get("_show_desc") else "Description"
        view_items.append(btn("w", desc_label, WHITE))
    if ctx.get("linked_issues"):
        view_items.append(btn("l", "Linked", YELLOW))
    if ctx.get("diag_links"):
        diag_label = "Close Diags" if ctx.get("_show_diags") else "Diags"
        view_items.append(btn("d", diag_label, BLUE))
    _cc = ctx.get("_comment_count") or len(ctx.get("comments") or [])
    if _cc:
        cmt_label = "Close Comments" if ctx.get("_show_comments") else f"Comments ({_cc})"
        view_items.append(btn("c", cmt_label, GREEN))
    if (netbox and netbox.get("rack_id")) or ctx.get("rack_location"):
        view_items.append(btn("e", "Rack View", YELLOW))
    is_ticket = ctx.get("source") != "netbox"
    if ctx.get("_mrb_count", 0) > 0:
        view_items.append(btn("f", f"MRB ({ctx['_mrb_count']})", YELLOW))
    if is_ticket:
        sla_label = "Close SLA" if ctx.get("_show_sla") else "SLA"
        view_items.append(btn("u", sla_label, RED))

    if view_items:
        print(f"  {BOLD}{WHITE}View{RESET}")
        print(f"    {'   '.join(view_items)}")
        print()

    # --- Group 1b: Actions (Jira tickets only, not NetBox device views) ---
    action_items = []
    if is_ticket:
        current_assignee = ctx.get("assignee")
        already_mine = False
        if current_assignee:
            if _my_display_name and _my_display_name.lower() == current_assignee.lower():
                already_mine = True
            if not already_mine and _my_account_id and ctx.get("_assignee_account_id") == _my_account_id:
                already_mine = True
            if not already_mine:
                my_email = os.environ.get("JIRA_EMAIL", "")
                my_name = " ".join(w.capitalize() for w in my_email.split("@")[0].split("."))
                if my_name and my_name.lower() == current_assignee.lower():
                    already_mine = True
        if already_mine:
            action_items.append(btn("a", "Unassign from me", BLUE))
        elif current_assignee:
            action_items.append(btn("a", f"Take from {current_assignee}", YELLOW))
        else:
            action_items.append(btn("a", "Grab (assign to me)", GREEN))
        # Check if already bookmarked
        _bm_key = ctx.get("issue_key", "")
        _is_bookmarked = any(
            b.get("type") == "ticket" and b.get("params", {}).get("key") == _bm_key
            for b in _load_user_state().get("bookmarks", [])
        )
        if _is_bookmarked:
            action_items.append(btn("*", "Remove Bookmark", RED))
        else:
            action_items.append(btn("*", "Bookmark", YELLOW))
    if action_items:
        print(f"  {BOLD}{WHITE}Actions{RESET}")
        print(f"    {'   '.join(action_items)}")
        print()

    # --- Group 1c: Status transitions (only for Jira tickets) ---
    status_items = []
    if is_ticket:
        mine = _is_mine(ctx)
        unassigned = not ctx.get("assignee")
        status_lower = ctx.get("status", "").lower()

        if status_lower in ("to do", "new", "open", "waiting for triage",
                            "awaiting triage", "awaiting support",
                            "reopened") and (mine or unassigned):
            status_items.append(btn("s", "Start Work", GREEN))

        if status_lower == "in progress" and mine:
            status_items.append(btn("v", "Verification", BLUE))
            status_items.append(btn("y", "On Hold", YELLOW))

        if status_lower in ("on hold", "blocked", "paused",
                            "waiting for support", "awaiting support") and mine:
            status_items.append(btn("z", "Resume", CYAN))

        if status_lower == "verification" and mine:
            status_items.append(btn("z", "Back to In Progress", CYAN))
            status_items.append(btn("k", "Close Ticket", RED))

    if status_items:
        print(f"  {BOLD}{WHITE}Status{RESET}")
        print(f"    {'   '.join(status_items)}")
        print()

    # --- Group 2: External links (open in browser) ---
    open_items = []
    if is_ticket:
        open_items.append(btn("j", "Jira", CYAN))
        if ctx.get("_portal_url"):
            open_items.append(btn("p", "Portal", CYAN))
    netbox = ctx.get("netbox", {})
    if netbox and netbox.get("device_id"):
        open_items.append(btn("x", "NetBox", YELLOW))
    if ctx.get("grafana", {}).get("node_details"):
        open_items.append(btn("g", "Grafana", GREEN))
    if ctx.get("grafana", {}).get("ib_node_search"):
        open_items.append(btn("i", "IB", GREEN))
    if netbox and netbox.get("device_name") and netbox.get("site_slug"):
        open_items.append(btn("t", "Remote Console (BMC)", MAGENTA))
    if ctx.get("ho_context"):
        open_items.append(btn("o", f"View {ctx['ho_context']['key']}", MAGENTA))
    if open_items:
        print(f"  {BOLD}{WHITE}Open{RESET}")
    print(f"    {'   '.join(open_items)}")
    print()

    # --- Group 3: Navigation ---
    has_node_id = ctx.get("service_tag") or ctx.get("hostname")
    is_ticket2 = ctx.get("source") != "netbox"
    nav_items = [btn("b", "Back", DIM), btn("m", "Menu", DIM)]
    if has_node_id:
        tag = ctx.get("service_tag") or ctx.get("hostname")
        nav_items.append(btn("h", "Node Ticket History", DIM))
    if is_ticket2:
        nav_items.append(btn("=", "Refresh", CYAN))
    nav_items.append(btn("q", "Quit", DIM))
    nav_items.append(btn("?", "Help", CYAN))
    if _ai_available():
        nav_items.append(btn("ai", "AI", CYAN))
    print(f"  {DIM}Nav{RESET}  {'   '.join(nav_items)}")
    print()


# ---------------------------------------------------------------------------
# Bookmark manager
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Interactive menu loop
# ---------------------------------------------------------------------------

def _post_detail_prompt(ctx: dict = None, email: str = None, token: str = None,
                        state: dict = None) -> str:
    """After viewing a ticket detail, ask what to do next.
    Returns "back", "menu", "quit", or "history".
    Also handles opening URLs in browser and inline display via hotkeys."""
    has_node_id = ctx and (ctx.get("service_tag") or ctx.get("hostname"))
    has_grafana = ctx and ctx.get("grafana", {}).get("node_details")
    _show_desc = False  # toggle for [w] description view

    while True:
        # Render the action panel
        if ctx:
            _print_action_panel(ctx)

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
                    # Invalidate cache and refresh comments
                    _issue_cache.pop(key, None)
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

        # --- Help ---
        if choice == "?":
            _clear_screen()
            print(f"""
  {BOLD}{WHITE}  Ticket View — Hotkey Reference{RESET}
  {BOLD}{CYAN}{'━' * 50}{RESET}

  {BOLD}{WHITE}View{RESET}  {DIM}(toggle on/off){RESET}
    {CYAN}[r]{RESET} {WHITE}Rack Map{RESET}        {DIM}Visual data hall map with walking route{RESET}
    {MAGENTA}[n]{RESET} {WHITE}Connections{RESET}    {DIM}Network cables — your ports to switch ports{RESET}
    {WHITE}[w]{RESET} {WHITE}Description{RESET}    {DIM}Full ticket description text{RESET}
    {YELLOW}[l]{RESET} {WHITE}Linked{RESET}         {DIM}Related DO/HO tickets{RESET}
    {BLUE}[d]{RESET} {WHITE}Diags{RESET}          {DIM}Diagnostic links from the description{RESET}
    {GREEN}[c]{RESET} {WHITE}Comments{RESET}       {DIM}Jira comments (latest first){RESET}
    {YELLOW}[e]{RESET} {WHITE}Rack View{RESET}      {DIM}Rack elevation + adjacent racks + device drill-in{RESET}
    {YELLOW}[f]{RESET} {WHITE}MRB / Parts{RESET}    {DIM}RMA parts tickets (optics, PSUs, etc.){RESET}
    {RED}[u]{RESET} {WHITE}SLA{RESET}            {DIM}SLA timer details{RESET}

  {BOLD}{WHITE}Actions{RESET}
    {YELLOW}[a]{RESET} {WHITE}Assign{RESET}         {DIM}Grab unassigned / take from someone / unassign{RESET}
    {YELLOW}[*]{RESET} {WHITE}Bookmark{RESET}       {DIM}Save or remove this ticket as a main menu shortcut{RESET}

  {BOLD}{WHITE}Status Transitions{RESET}  {DIM}(only shown when available for current status){RESET}
    {GREEN}[s]{RESET} {WHITE}Start Work{RESET}     {DIM}New/To Do → In Progress (auto-assigns to you){RESET}
    {BLUE}[v]{RESET} {WHITE}Verify{RESET}         {DIM}In Progress → Verification (optional comment){RESET}
    {YELLOW}[y]{RESET} {WHITE}Hold{RESET}           {DIM}In Progress → On Hold (reason required){RESET}
    {CYAN}[z]{RESET} {WHITE}Resume{RESET}         {DIM}On Hold/Verification → back to In Progress{RESET}
    {RED}[k]{RESET} {WHITE}Close Ticket{RESET}   {DIM}→ Closed (comment + confirmation required){RESET}

  {BOLD}{WHITE}Open in Browser{RESET}
    {CYAN}[j]{RESET} {WHITE}Jira{RESET}           {DIM}Open this ticket in Jira{RESET}
    {CYAN}[p]{RESET} {WHITE}Portal{RESET}         {DIM}Service Desk customer portal{RESET}
    {YELLOW}[x]{RESET} {WHITE}NetBox{RESET}         {DIM}Open device in NetBox{RESET}
    {GREEN}[g]{RESET} {WHITE}Grafana{RESET}        {DIM}Node monitoring dashboard{RESET}
    {GREEN}[i]{RESET} {WHITE}IB{RESET}             {DIM}InfiniBand fabric search{RESET}
    {MAGENTA}[t]{RESET} {WHITE}Remote Console{RESET} {DIM}BMC / Teleport session{RESET}
    {BLUE}[o]{RESET} {WHITE}HO in Jira{RESET}     {DIM}Open linked HO ticket{RESET}

  {BOLD}{WHITE}Navigation{RESET}
    {DIM}[b]{RESET} {WHITE}Back{RESET}           {DIM}Return to previous list / queue{RESET}
    {DIM}[m]{RESET} {WHITE}Menu{RESET}           {DIM}Jump to main menu{RESET}
    {DIM}[h]{RESET} {WHITE}History{RESET}        {DIM}All DO/HO tickets for this node{RESET}
    {DIM}[q]{RESET} {WHITE}Quit{RESET}           {DIM}Exit the tool{RESET}

  {BOLD}{CYAN}{'━' * 50}{RESET}
""")
            try:
                input(f"  {DIM}Press ENTER to go back to ticket...{RESET}")
            except (EOFError, KeyboardInterrupt):
                pass
            _clear_screen()
            if ctx:
                _print_pretty(ctx)
            continue

        # --- Navigation ---
        if choice in ("q", "quit", "exit"):
            return "quit"
        if choice in ("m", "menu"):
            return "menu"
        if choice in ("h", "history") and has_node_id:
            return "history"
        if choice in ("b", "back"):
            return "back"

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
            already_mine = False
            if current_assignee:
                if _my_display_name and _my_display_name.lower() == current_assignee.lower():
                    already_mine = True
                elif _my_account_id and ctx.get("_assignee_account_id") == _my_account_id:
                    already_mine = True
                else:
                    my_email_local = os.environ.get("JIRA_EMAIL", "")
                    my_name_local = " ".join(w.capitalize() for w in my_email_local.split("@")[0].split("."))
                    if my_name_local and my_name_local.lower() == current_assignee.lower():
                        already_mine = True

            if already_mine:
                # Unassign from self
                print(f"\n  {DIM}Unassigning {key} from you...{RESET}", end="", flush=True)
                resp = _jira_put(f"/rest/api/3/issue/{key}/assignee", email, token,
                                 body={"accountId": None})
                if resp and resp.status_code == 204:
                    print(f"\r  {BLUE}{BOLD}Unassigned {key}{RESET}                    ")
                    ctx["assignee"] = None
                    _issue_cache.pop(key, None)
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
                        ctx["assignee"] = "(you)"
                        _issue_cache.pop(key, None)
                    else:
                        print(f"\r  {YELLOW}Could not reassign {key}.{RESET}              ")
            else:
                # Grab unassigned ticket
                print(f"\n  {DIM}Assigning {key} to you...{RESET}", end="", flush=True)
                if _grab_ticket(key, email, token):
                    print(f"\r  {GREEN}{BOLD}Grabbed {key}!{RESET}                    ")
                    ctx["assignee"] = "(you)"
                    _issue_cache.pop(key, None)
                else:
                    print(f"\r  {YELLOW}Could not grab {key}.{RESET}              ")

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
            if dev and slug:
                url = f"https://bmc-{dev}.teleport.{slug}.int.coreweave.com/"
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

        # --- Inline views (clear + ticket info + inline content) ---
        if choice == "c" and ctx and (ctx.get("comments") or ctx.get("_raw_comments")):
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
                import re as _re
                _rm = _re.search(r"-r(\d{2,4})", rl.lower())
                if _rm:
                    _rn = int(_rm.group(1).lstrip("0") or "0")
                    _site = ctx.get("site") or ""
                    # Determine DH/sector from netbox or default
                    _nb = ctx.get("netbox") or {}
                    _dh = "SEC1"  # default for hostname-derived
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
            _show_desc = not _show_desc
            ctx["_show_desc"] = _show_desc
            _clear_screen()
            _print_pretty(ctx)
            if _show_desc:
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
        if choice == "e" and ctx and email and token:
            netbox = ctx.get("netbox") or {}
            # Resolve rack_id from rack_location if NetBox didn't have it
            if not netbox.get("rack_id") and (ctx.get("rack_location") or ctx.get("hostname")):
                rl = ctx.get("rack_location", "") or ""
                parsed_rl = _parse_rack_location(rl)
                # Fallback: extract rack number from hostname pattern (s1-r027-..., dh1-r064-...)
                if not parsed_rl:
                    import re as _re
                    rack_match = _re.search(r"-r(\d{2,4})", (rl + " " + (ctx.get("hostname") or "")).lower())
                    site_match = _re.search(r"(us-\S+)$", (rl + " " + (ctx.get("hostname") or "")).lower())
                    if rack_match:
                        rack_num = int(rack_match.group(1).lstrip("0") or "0")
                        site_code = ctx.get("site") or (site_match.group(1) if site_match else "")
                        parsed_rl = {"site_code": site_code, "dh": "SEC1", "rack": rack_num, "ru": None}
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
                url = f"{JIRA_BASE_URL}/issues/?jql={requests.utils.quote(jql)}"
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
            print(f"\n  {DIM}Add a comment (ENTER to skip):{RESET}")
            try:
                comment = input("  > ").strip()
            except (EOFError, KeyboardInterrupt):
                comment = ""

            print(f"  {DIM}Moving {key} to Verification...{RESET}",
                  end="", flush=True)
            if _execute_transition(ctx, "verify", email, token,
                                   comment_text=comment or None):
                print(f"\r  {BLUE}{BOLD}{key} \u2192 Verification"
                      f"{RESET}                    ")
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
            print(f"\n  {YELLOW}{BOLD}Reason for hold{RESET} "
                  f"{DIM}(required):{RESET}")
            try:
                comment = input("  > ").strip()
            except (EOFError, KeyboardInterrupt):
                comment = ""
            if not comment:
                print(f"  {YELLOW}Comment required. Cancelled.{RESET}")
                _brief_pause()
                _clear_screen()
                _print_pretty(ctx)
                continue

            print(f"  {DIM}Putting {key} on hold...{RESET}",
                  end="", flush=True)
            if _execute_transition(ctx, "hold", email, token,
                                   comment_text=comment):
                print(f"\r  {YELLOW}{BOLD}{key} \u2192 On Hold"
                      f"{RESET}                    ")
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
            if _execute_transition(ctx, "resume", email, token,
                                   comment_text=comment or None):
                print(f"\r  {CYAN}{BOLD}{key} \u2192 In Progress"
                      f"{RESET}                    ")
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
            print(f"  {DIM}A closing comment is required. Example:{RESET}")
            print(f"  {DIM}  Work performed: [summary]. "
                  f"No recurrence during verification.{RESET}")
            print()
            try:
                comment = input("  > ").strip()
            except (EOFError, KeyboardInterrupt):
                comment = ""
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
            if _execute_transition(ctx, "close", email, token,
                                   comment_text=comment):
                print(f"\r  {GREEN}{BOLD}{key} \u2192 Closed"
                      f"{RESET}                    ")
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


def _clear_screen():
    """Clear terminal (works on macOS/Linux)."""
    os.system("clear" if os.name != "nt" else "cls")


def _print_banner(greeting: str = ""):
    """Print the app header with optional personalized greeting."""
    # Derive full name from JIRA_EMAIL for display
    my_email = os.environ.get("JIRA_EMAIL", "")
    full_name = " ".join(w.capitalize() for w in my_email.split("@")[0].split("."))

    print()
    print(f"  {BOLD}{CYAN}{'━' * 42}{RESET}")
    print(f"  {BOLD}{CYAN}┃{RESET}  {BOLD}{WHITE}CoreWeave  DCT  Node  Helper{RESET}  {DIM}v{APP_VERSION}{RESET} {BOLD}{CYAN}┃{RESET}")
    if full_name:
        print(f"  {BOLD}{CYAN}┃{RESET}  {GREEN}{BOLD}{full_name}{RESET}  {DIM}logged in{RESET}            {BOLD}{CYAN}┃{RESET}")
    print(f"  {BOLD}{CYAN}{'━' * 42}{RESET}")


def _print_help():
    """Display a full help guide explaining every menu option and hotkey."""
    print(f"""
  {BOLD}{CYAN}{'━' * 54}{RESET}
  {BOLD}{WHITE}  Quick Guide — CoreWeave DCT Node Helper{RESET}
  {BOLD}{CYAN}{'━' * 54}{RESET}

  {BOLD}{WHITE}WHAT'S NEW  {RESET}{DIM}v{APP_VERSION}{RESET}
  {DIM}{'─' * 54}{RESET}

  {BOLD}{YELLOW}v6.3.0{RESET}
    {GREEN}+{RESET} {WHITE}Ticket categorizer{RESET}
      {DIM}Scans last 50 queue tickets and groups them by
      type (DEVICE, NETWORK, POWER_CYCLE, PSU_RESEAT,
      LOW_LIGHT, PORT_FLAPPING, OTHER) with boolean
      matching on summary keywords and diag links.
      Includes troubleshooting tips per category.{RESET}

  {BOLD}{YELLOW}v6.2.0{RESET}
    {GREEN}+{RESET} {WHITE}SDA queue support{RESET}
      {DIM}Browse SDA project tickets with Triage and
      Customer Verification status filters.{RESET}
    {GREEN}+{RESET} {WHITE}Beginner setup guide{RESET}
      {DIM}Step-by-step install guide at site/setup-guide.html
      and live on CodePen.{RESET}
    {GREEN}+{RESET} {WHITE}start.sh launcher{RESET}
      {DIM}One-click launcher script for non-tech users.{RESET}

  {BOLD}{YELLOW}v6.1.0{RESET}
    {GREEN}+{RESET} {WHITE}Rack map visualization{RESET}
      {DIM}ASCII data hall maps with animated walking routes.{RESET}
    {GREEN}+{RESET} {WHITE}MRB / Parts search [f]{RESET}
      {DIM}Find RMA and parts tickets from ticket detail view.{RESET}
    {GREEN}+{RESET} {WHITE}Bookmark shortcuts [a-e]{RESET}
      {DIM}Save tickets and queue searches to main menu.{RESET}

  {BOLD}{WHITE}MAIN MENU{RESET}
  {DIM}{'─' * 54}{RESET}

  {BOLD}1{RESET}  {WHITE}Lookup ticket{RESET}
     {DIM}Enter a Jira key like DO-12345 or HO-67890.
     Pulls up full ticket detail with node info, NetBox
     data, and all available actions.
     Use when: you have a ticket number and need context.{RESET}

  {BOLD}2{RESET}  {WHITE}Node info{RESET}
     {DIM}Enter a service tag (e.g. S948338X5A04781) or
     hostname. Finds ALL tickets for that node across
     DO and HO projects.
     Use when: you're at a rack and need the node's
     full ticket history.{RESET}

  {BOLD}3{RESET}  {WHITE}Browse queue{RESET}
     {DIM}Pick DO (Data Operations) or HO (Hardware Operations),
     then filter by site and status.
     HO includes a "Radar" filter showing tickets likely
     to spawn DOs (RMA-initiate, Sent to DCT, etc.).
     Use when: starting your shift and need to see
     what's in the queue.{RESET}

  {BOLD}4{RESET}  {WHITE}My tickets{RESET}
     {DIM}Shows only tickets assigned to you.
     Use when: checking your personal workload.{RESET}

  {BOLD}5{RESET}  {WHITE}Watch queue{RESET}
     {DIM}Polls for new tickets every N seconds and sends
     macOS notifications when new ones appear.
     Use when: you want to be alerted about new tickets
     without refreshing manually.{RESET}

  {BOLD}6{RESET}  {WHITE}Rack map{RESET}
     {DIM}Shows a visual data hall map with your rack
     highlighted and a yellow walking route from the
     entrance. Animated in terminal.
     Use when: you need to physically find a rack on
     the floor.{RESET}

  {BOLD}7{RESET}  {WHITE}Bookmarks{RESET}
     {DIM}Save frequently used tickets or searches as
     shortcuts (a-e) on the main menu.
     Use when: you have tickets you check repeatedly.{RESET}

  {BOLD}{WHITE}TICKET VIEW HOTKEYS{RESET}
  {DIM}{'─' * 54}{RESET}
  {DIM}These appear after viewing a ticket detail:{RESET}

  {BOLD}{WHITE}View{RESET}
    {CYAN}[r]{RESET} {WHITE}Rack Map{RESET}       {DIM}Data hall map with walking route{RESET}
    {MAGENTA}[n]{RESET} {WHITE}Connections{RESET}   {DIM}Network cables: your ports → switch ports{RESET}
    {YELLOW}[l]{RESET} {WHITE}Linked{RESET}        {DIM}Related DO/HO tickets{RESET}
    {BLUE}[d]{RESET} {WHITE}Diags{RESET}         {DIM}Diagnostic links from description{RESET}
    {GREEN}[c]{RESET} {WHITE}Comments{RESET}      {DIM}Latest Jira comments{RESET}
    {YELLOW}[e]{RESET} {WHITE}Rack View{RESET}     {DIM}Visual rack elevation + pick a neighbor
                      to search Jira. Type 'x' for NetBox.{RESET}
    {YELLOW}[f]{RESET} {WHITE}MRB / Parts{RESET}   {DIM}Find RMA/parts tickets for this node
                      in the MRB project (optics, PSUs, etc.){RESET}

  {BOLD}{WHITE}Status transitions{RESET}  {DIM}(shown based on current status){RESET}
    {GREEN}[s]{RESET} {WHITE}Start Work{RESET}    {DIM}Grab & begin (New/To Do → In Progress){RESET}
    {BLUE}[v]{RESET} {WHITE}Verification{RESET}  {DIM}Move to verification (optional comment){RESET}
    {YELLOW}[y]{RESET} {WHITE}On Hold{RESET}       {DIM}Pause work (reason required){RESET}
    {CYAN}[z]{RESET} {WHITE}Resume{RESET}        {DIM}Back to In Progress{RESET}
    {RED}[k]{RESET} {WHITE}Close{RESET}         {DIM}Close ticket (comment + confirm required){RESET}

  {BOLD}{WHITE}Open in browser{RESET}
    {CYAN}[j]{RESET} {WHITE}Jira{RESET}          {DIM}Open ticket in Jira{RESET}
    {CYAN}[p]{RESET} {WHITE}Portal{RESET}        {DIM}Open Service Desk portal{RESET}
    {YELLOW}[x]{RESET} {WHITE}NetBox{RESET}        {DIM}Open device in NetBox{RESET}
    {GREEN}[g]{RESET} {WHITE}Grafana{RESET}       {DIM}Open node dashboard{RESET}
    {GREEN}[i]{RESET} {WHITE}IB{RESET}            {DIM}InfiniBand search{RESET}

  {BOLD}{WHITE}Navigation{RESET}
    {DIM}[b]{RESET} {WHITE}Back{RESET}          {DIM}Return to queue list{RESET}
    {DIM}[m]{RESET} {WHITE}Menu{RESET}          {DIM}Return to main menu{RESET}
    {DIM}[h]{RESET} {WHITE}History{RESET}       {DIM}All tickets for this node{RESET}
    {DIM}[q]{RESET} {WHITE}Quit{RESET}          {DIM}Exit the tool{RESET}

  {BOLD}{CYAN}{'━' * 54}{RESET}

  {DIM}Scroll up to see the full guide.{RESET}
  {DIM}Press {RESET}{CYAN}{BOLD}w{RESET}{DIM} to open the visual docs in your browser.{RESET}
""")
    try:
        pick = input(f"\n  Press {BOLD}w{RESET} for web docs, or ENTER to go back: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return
    if pick == "w":
        import os as _os
        site_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "site", "index.html")
        if _os.path.exists(site_path):
            import webbrowser
            webbrowser.open(f"file://{site_path}")
            print(f"  {DIM}Opening docs in browser...{RESET}")
        else:
            print(f"  {DIM}No site/index.html found.{RESET}")


def _ask_site() -> str:
    """Prompt the user to pick a site from a numbered list or type one."""
    print(f"\n  {DIM}Sites:{RESET}")
    print(f"    {BOLD}0{RESET} All sites {DIM}(no filter){RESET}")
    for i, s in enumerate(KNOWN_SITES, start=1):
        print(f"    {BOLD}{i}{RESET} {s}")
    print(f"    {DIM}Or type a site name directly{RESET}")

    raw = input(f"  Site [0-{len(KNOWN_SITES)}] or ENTER for all: ").strip()

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
    try:
        if prompt_site:
            site = _ask_site()
        else:
            site = ""

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

        sf_input = input(f"  Filter [1-{max_opt}] or ENTER for All: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
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


def _interactive_menu():
    """Main interactive loop. Keeps running until user quits."""
    global _AI_ENABLED
    email, token = _get_credentials()

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

    while True:
        # Reload state each iteration (sub-flows may have modified it)
        state = _load_user_state()

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

        # Auto-check stale verification tickets (non-blocking banner)
        _stale_count = 0
        _stale_cache = []
        try:
            _vr = _jira_post("/rest/api/3/search/jql", email, token, body={
                "jql": 'project in ("DO", "HO") AND assignee = currentUser() AND status = "Verification" ORDER BY updated ASC',
                "maxResults": 30,
                "fields": ["key", "summary", "statuscategorychangedate",
                            "customfield_10193", "customfield_10194",
                            "customfield_10192", "customfield_10207"],
            })
            _vi = _vr.json().get("issues", []) if _vr and _vr.ok else []
            _stale_cache = [iss for iss in _vi
                            if _parse_jira_timestamp(iss.get("fields", {}).get("statuscategorychangedate")) > 48 * 3600]
            _stale_count = len(_stale_cache)
        except Exception:
            pass
        if _stale_count:
            _sv_label = f"⚠ {_stale_count} STALE"
            _sv_plural = "s" if _stale_count != 1 else ""
            # Build the visible (no-ANSI) content to measure it
            _sv_text = f"  ticket{_sv_plural} in Verification >48h  — press v to review  "
            _sv_box_w = max(len(_sv_text), len(_sv_label) + 5)
            _sv_pad = _sv_box_w - len(_sv_text)
            print(f"\n  {RED}{BOLD}┌─ {_sv_label} {'─' * (_sv_box_w - len(_sv_label) - 3)}┐{RESET}")
            print(f"  {RED}{BOLD}│{RESET}  ticket{_sv_plural} in Verification >48h  {DIM}—{RESET} press {CYAN}{BOLD}v{RESET} {YELLOW}to review{RESET}{' ' * _sv_pad}{RED}{BOLD}│{RESET}")
            print(f"  {RED}{BOLD}└{'─' * _sv_box_w}┘{RESET}")

        # Watcher status line
        if _is_watcher_running():
            site_label = _watcher_site or "all sites"
            print(f"  {GREEN}{BOLD}[WATCHING]{RESET} {_watcher_project} @ {site_label} {DIM}— every {_watcher_interval}s{RESET}")

        # Build option 5 label based on watcher state
        if _is_watcher_running():
            opt5_label = f"  {BOLD}5{RESET}  {YELLOW}Stop watching{RESET}   {DIM}(background watcher is running){RESET}"
        else:
            opt5_label = f"  {BOLD}5{RESET}  Watch queue       {DIM}(background — grab tickets live){RESET}"

        # --- Last ticket "go back" shortcut ---
        last_key = state.get("last_ticket")
        if last_key:
            last_summary = ""
            for r in state.get("recent_tickets", []):
                if r.get("key") == last_key:
                    last_summary = r.get("summary", "")
                    break
            snip = f"  {last_summary[:50]}" if last_summary else ""
            print(f"\n  {BOLD}0{RESET}  {CYAN}↩{RESET} {DIM}Last viewed:{RESET} {CYAN}{BOLD}{last_key}{RESET}{DIM}{snip}{RESET}")

        print(f"""
  {BOLD}1{RESET}  Lookup ticket     {DIM}(DO-12345, HO-67890, SDA-111){RESET}
  {BOLD}2{RESET}  Node info         {DIM}(service tag or hostname -> all tickets){RESET}
  {BOLD}3{RESET}  Browse queue      {DIM}(DO, HO, or SDA — by site + status){RESET}
  {BOLD}4{RESET}  My tickets        {DIM}(DO tickets assigned to you){RESET}
{opt5_label}
  {BOLD}6{RESET}  Rack map          {DIM}(visual location in data hall){RESET}
  {BOLD}7{RESET}  Bookmarks         {DIM}(manage saved shortcuts){RESET}

  {BOLD}v{RESET}  Stale verifications {DIM}(>48h in Verification){RESET}
  {BOLD}q{RESET}  Quit   {CYAN}{BOLD}?{RESET} Help"""
        + (f"   {CYAN}{BOLD}ai{RESET} {'on' if _AI_ENABLED else 'off'} {DIM}— type anything to chat{RESET}" if _ai_available() else "")
        )

        # --- Bookmark shortcuts (a-e) ---
        bookmarks = state.get("bookmarks", [])
        bm_keys = "abcde"
        if bookmarks:
            print(f"\n  {DIM}Shortcuts{RESET}")
            for i, bm in enumerate(bookmarks):
                if i >= len(bm_keys):
                    break
                print(f"    {BOLD}{bm_keys[i]}{RESET}  {bm.get('label', '?')}")
        print()

        # --- Check for new tickets from background watcher ---
        if _is_watcher_running():
            result = _handle_new_tickets(email, token)
            if result == "quit":
                _stop_background_watcher()
                print(f"\n  {DIM}Goodbye.{RESET}\n")
                return

        # Build prompt hint
        bm_hint = f", [{bm_keys[0]}-{bm_keys[len(bookmarks)-1]}]" if bookmarks else ""
        watcher_hint = ""
        if _is_watcher_running():
            pending = _watcher_queue.qsize()
            if pending > 0:
                watcher_hint = (
                    f"\n  {YELLOW}{BOLD}{'━' * 50}{RESET}"
                    f"\n  {YELLOW}{BOLD}  {pending} NEW TICKET{'S' if pending != 1 else ''} FOUND!"
                    f"  Press ENTER to view{RESET}"
                    f"\n  {YELLOW}{BOLD}{'━' * 50}{RESET}\n"
                )
            else:
                watcher_hint = f"\n  {DIM}Watching... press ENTER to refresh{RESET}"
        try:
            choice = input(f"  Select [1-7]{bm_hint}, ticket key, or q: {watcher_hint}").strip().lower()
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
        # --- AI chat (explicit) --------------------------------------------
        if choice == "ai":
            found_key = _ai_dispatch(email=email, token=token)
            if found_key and JIRA_KEY_PATTERN.match(found_key):
                ctx = _fetch_and_show(found_key, email, token)
                if ctx:
                    state = _record_ticket_view(state, ctx["issue_key"], ctx.get("summary", ""),
                                           assignee=ctx.get("assignee"), updated=ctx.get("updated"))
                    _save_user_state(state)
                    _clear_screen()
                    _print_pretty(ctx)
                    action = _post_detail_prompt(ctx, email, token, state=state)
                    if action == "quit":
                        print(f"\n  {DIM}Goodbye.{RESET}\n")
                        return
                    while action == "history":
                        tag = ctx.get("service_tag") or ctx.get("hostname")
                        if not tag:
                            break
                        h_action = _run_history_interactive(email, token, tag)
                        if h_action == "quit":
                            print(f"\n  {DIM}Goodbye.{RESET}\n")
                            return
                        _clear_screen()
                        _print_pretty(ctx)
                        action = _post_detail_prompt(ctx, email, token, state=state)
            continue

        # --- 0: Return to last ticket ------------------------------------
        if choice == "0" and state.get("last_ticket"):
            ctx = _fetch_and_show(state["last_ticket"], email, token)
            if ctx:
                state = _record_ticket_view(state, ctx["issue_key"], ctx.get("summary", ""),
                                       assignee=ctx.get("assignee"), updated=ctx.get("updated"))
                _save_user_state(state)
                _clear_screen()
                _print_pretty(ctx)
                action = _post_detail_prompt(ctx, email, token, state=state)
                if action == "quit":
                    print(f"\n  {DIM}Goodbye.{RESET}\n")
                    return
                while action == "history":
                    tag = ctx.get("service_tag") or ctx.get("hostname")
                    if not tag:
                        break
                    h_action = _run_history_interactive(email, token, tag)
                    if h_action == "quit":
                        print(f"\n  {DIM}Goodbye.{RESET}\n")
                        return
                    # "back" from history → re-render ticket
                    _clear_screen()
                    _print_pretty(ctx)
                    action = _post_detail_prompt(ctx, email, token, state=state)
            continue

        # --- Direct ticket key at main menu (e.g. DO-12345) --------------
        if JIRA_KEY_PATTERN.match(choice.upper()):
            ctx = _fetch_and_show(choice.upper(), email, token)
            if ctx:
                state = _record_ticket_view(state, ctx["issue_key"], ctx.get("summary", ""),
                                       assignee=ctx.get("assignee"), updated=ctx.get("updated"))
                _save_user_state(state)
                _clear_screen()
                _print_pretty(ctx)
                action = _post_detail_prompt(ctx, email, token, state=state)
                if action == "quit":
                    print(f"\n  {DIM}Goodbye.{RESET}\n")
                    return
                while action == "history":
                    tag = ctx.get("service_tag") or ctx.get("hostname")
                    if not tag:
                        break
                    h_action = _run_history_interactive(email, token, tag)
                    if h_action == "quit":
                        print(f"\n  {DIM}Goodbye.{RESET}\n")
                        return
                    # "back" from history → re-render ticket
                    _clear_screen()
                    _print_pretty(ctx)
                    action = _post_detail_prompt(ctx, email, token, state=state)
            continue

        # --- Bookmark shortcuts (a-e) ------------------------------------
        if choice in bm_keys and bm_keys.index(choice) < len(bookmarks):
            bm = bookmarks[bm_keys.index(choice)]
            bm_type = bm.get("type")
            params = bm.get("params", {})

            if bm_type == "ticket":
                ctx = _fetch_and_show(params["key"], email, token)
                if ctx:
                    state = _record_ticket_view(state, ctx["issue_key"], ctx.get("summary", ""),
                                       assignee=ctx.get("assignee"), updated=ctx.get("updated"))
                    _save_user_state(state)
                    _clear_screen()
                    _print_pretty(ctx)
                    action = _post_detail_prompt(ctx, email, token, state=state)
                    if action == "quit":
                        print(f"\n  {DIM}Goodbye.{RESET}\n")
                        return
                    while action == "history":
                        tag = ctx.get("service_tag") or ctx.get("hostname")
                        if not tag:
                            break
                        h_action = _run_history_interactive(email, token, tag)
                        if h_action == "quit":
                            print(f"\n  {DIM}Goodbye.{RESET}\n")
                            return
                        _clear_screen()
                        _print_pretty(ctx)
                        action = _post_detail_prompt(ctx, email, token, state=state)
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

        # --- 1: Lookup by ticket key --------------------------------------
        if choice == "1":
            recents = list(state.get("recent_tickets", []))

            # Always show 5 picks — backfill from user's queue if needed
            if len(recents) < 5:
                print(f"\n  {DIM}Loading your recent tickets...{RESET}", end="", flush=True)
                try:
                    my_issues = _search_queue("", email, token, mine_only=True,
                                              limit=5, status_filter="all", project="DO")
                    my_issues += _search_queue("", email, token, mine_only=True,
                                               limit=5, status_filter="all", project="HO")
                    my_issues += _search_queue("", email, token, mine_only=True,
                                               limit=5, status_filter="all", project="SDA")
                    seen = {r["key"] for r in recents}
                    for iss in sorted(my_issues, key=lambda x: x.get("key", ""), reverse=True):
                        if len(recents) >= 5:
                            break
                        k = iss["key"]
                        if k not in seen:
                            seen.add(k)
                            f_ = iss.get("fields", {})
                            summary = f_.get("summary", "")
                            assignee_obj = f_.get("assignee")
                            entry = {"key": k, "summary": summary[:80], "_backfill": True}
                            if assignee_obj:
                                entry["assignee"] = assignee_obj.get("displayName")
                            if f_.get("updated"):
                                entry["updated"] = f_["updated"]
                            recents.append(entry)
                except Exception:
                    pass
                print(f"\r{'':60}\r", end="")  # clear the loading message

            print(f"\n  {DIM}Recent tickets:{RESET}")
            for i, r in enumerate(recents[:5], 1):
                label = r.get('summary', '')[:42]
                # Assignee display
                assignee = r.get("assignee")
                if assignee:
                    # Show first name only to save space
                    asgn_short = assignee.split()[0] if " " in assignee else assignee
                    asgn_str = f" {CYAN}{asgn_short}{RESET}"
                else:
                    asgn_str = f" {RED}unassigned{RESET}"
                # Last updated age
                upd = r.get("updated", "")
                if upd:
                    upd_secs = _parse_jira_timestamp(upd)
                    upd_str = f" {DIM}upd {_format_age(upd_secs)}{RESET}"
                else:
                    upd_str = ""
                dim = f" {DIM}(queue){RESET}" if r.get("_backfill") else ""
                print(f"    {BOLD}{i}{RESET}. {r['key']}  {DIM}{label}{RESET}{asgn_str}{upd_str}{dim}")
            print()

            try:
                prompt = "  Enter Jira ticket"
                if recents:
                    prompt += f", pick [1-{len(recents)}]"
                prompt += ", or ENTER to go back: "
                raw = input(prompt).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                continue

            if not raw:
                continue

            # Check if user picked a recent by number
            key = None
            try:
                idx = int(raw)
                if 1 <= idx <= len(recents):
                    key = recents[idx - 1]["key"]
            except ValueError:
                pass

            if not key:
                key = raw.upper()

            ctx = _fetch_and_show(key, email, token)
            if ctx:
                state = _record_ticket_view(state, ctx["issue_key"], ctx.get("summary", ""),
                                       assignee=ctx.get("assignee"), updated=ctx.get("updated"))
                _save_user_state(state)
                _clear_screen()
                _print_pretty(ctx)
                action = _post_detail_prompt(ctx, email, token, state=state)
                if action == "quit":
                    print(f"\n  {DIM}Goodbye.{RESET}\n")
                    return
                while action == "history":
                    tag = ctx.get("service_tag") or ctx.get("hostname")
                    if not tag:
                        break
                    h_action = _run_history_interactive(email, token, tag)
                    if h_action == "quit":
                        print(f"\n  {DIM}Goodbye.{RESET}\n")
                        return
                    # "back" from history → re-render ticket
                    _clear_screen()
                    _print_pretty(ctx)
                    action = _post_detail_prompt(ctx, email, token, state=state)

        # --- 2: Search node (goes to history view, not single ticket) ------
        elif choice == "2":
            recent_nodes = list(state.get("recent_nodes", []))

            # Always show 5 picks — backfill from user's queue if needed
            if len(recent_nodes) < 5:
                print(f"\n  {DIM}Loading your recent nodes...{RESET}", end="", flush=True)
                try:
                    my_issues = _search_queue("", email, token, mine_only=True,
                                              limit=10, status_filter="all", project="DO")
                    my_issues += _search_queue("", email, token, mine_only=True,
                                               limit=10, status_filter="all", project="HO")
                    my_issues += _search_queue("", email, token, mine_only=True,
                                               limit=10, status_filter="all", project="SDA")
                    seen = {n["term"].lower() for n in recent_nodes}
                    for iss in my_issues:
                        if len(recent_nodes) >= 5:
                            break
                        f = iss.get("fields", {})
                        tag = _unwrap_field(f.get("customfield_10193"))  # service_tag
                        if tag and tag.lower() not in seen:
                            seen.add(tag.lower())
                            hn = _unwrap_field(f.get("customfield_10192")) or ""
                            site = _unwrap_field(f.get("customfield_10194")) or ""
                            entry = {"term": tag, "_backfill": True}
                            if hn:
                                entry["hostname"] = hn
                            if site:
                                entry["site"] = site
                            entry["last_ticket"] = iss.get("key", "")
                            recent_nodes.append(entry)
                except Exception:
                    pass
                print(f"\r{'':60}\r", end="")  # clear the loading message

            print(f"\n  {DIM}Recent nodes:{RESET}")
            for i, n in enumerate(recent_nodes[:5], 1):
                dim = f"  {DIM}(from queue){RESET}" if n.get("_backfill") else ""
                extras = []
                if n.get("hostname"):
                    extras.append(n["hostname"])
                if n.get("site"):
                    extras.append(n["site"])
                if n.get("last_ticket"):
                    extras.append(n["last_ticket"])
                extra_str = f"  {DIM}{' │ '.join(extras)}{RESET}" if extras else ""
                print(f"    {BOLD}{i}{RESET}. {n['term']}{extra_str}{dim}")
            print()

            try:
                prompt = "  Enter service tag or hostname"
                if recent_nodes:
                    prompt += f", pick [1-{len(recent_nodes)}]"
                prompt += ", or ENTER to go back: "
                raw = input(prompt).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                continue

            if not raw or raw.lower() in ("q", "quit", "b", "back"):
                continue

            # Check if user picked a recent by number
            term = None
            try:
                idx = int(raw)
                if 1 <= idx <= len(recent_nodes):
                    term = recent_nodes[idx - 1]["term"]
            except ValueError:
                pass

            if not term:
                term = raw

            # Enrich with hostname/site from first matching ticket
            _node_hn, _node_site, _node_ticket = None, None, None
            try:
                _node_issues = _search_node_history(term, email, token, limit=1)
                if _node_issues:
                    _nf = _node_issues[0].get("fields", {})
                    _node_hn = _unwrap_field(_nf.get("customfield_10192")) or None
                    _node_site = _unwrap_field(_nf.get("customfield_10194")) or None
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

        # --- 3: Browse queue (DO, HO, or SDA) --------------------------------
        elif choice == "3":
            print(f"\n  {DIM}Project:{RESET}")
            print(f"    {BOLD}1{RESET} DO  — Data Operations {DIM}(hands-on: reseat, swap, cable){RESET}")
            print(f"    {BOLD}2{RESET} HO  — Hardware Operations {DIM}(RMA lifecycle, vendor, parts){RESET}")
            print(f"    {BOLD}3{RESET} SDA — Service Desk Albatross {DIM}(Albatross hardware incidents){RESET}")
            try:
                proj_input = input(f"  Project [1-3] or ENTER for DO: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
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

        # --- 4: My tickets (DO only — HO tickets aren't assigned to DCTs) ---
        elif choice == "4":
            action = _run_queue_interactive(
                email, token, "",
                mine_only=True, status_filter="all", project="DO")
            if action == "quit":
                print(f"\n  {DIM}Goodbye.{RESET}\n")
                return

        # --- 5: Watch queue (toggle background watcher) --------------------
        elif choice == "5":
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
                proj_input = input(f"  Project [1-3] or ENTER for DO: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                continue
            proj = {"2": "HO", "3": "SDA"}.get(proj_input, "DO")

            site = _ask_site()

            print(f"\n  {DIM}Poll interval:{RESET}")
            print(f"    {BOLD}1{RESET} Every 30 seconds")
            print(f"    {BOLD}2{RESET} Every 45 seconds")
            print(f"    {BOLD}3{RESET} Every 60 seconds {DIM}(default){RESET}")
            try:
                int_input = input(f"  Interval [1-3] or ENTER for 60s: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                continue

            interval_map = {"1": 30, "2": 45, "3": 60, "": 60}
            interval = interval_map.get(int_input, 60)

            started = _start_background_watcher(
                email, token, site, project=proj, interval=interval)
            if started:
                site_label = site or "all sites"
                print(f"\n  {GREEN}{BOLD}Watcher started!{RESET} {proj} @ {site_label} — every {interval}s")
                print(f"  {DIM}New tickets will appear inline. Use option 5 to stop.{RESET}")
                _brief_pause()
            else:
                print(f"\n  {DIM}Watcher is already running.{RESET}")
                _brief_pause()

        # --- 6: Rack map -------------------------------------------------------
        elif choice == "6":
            recent_racks = list(state.get("recent_racks", []))

            # Backfill from user's queue if fewer than 5
            if len(recent_racks) < 5:
                print(f"\n  {DIM}Loading recent rack locations...{RESET}", end="", flush=True)
                try:
                    my_issues = _search_queue("", email, token, mine_only=True,
                                              limit=10, status_filter="all", project="DO")
                    my_issues += _search_queue("", email, token, mine_only=True,
                                               limit=10, status_filter="all", project="HO")
                    my_issues += _search_queue("", email, token, mine_only=True,
                                               limit=10, status_filter="all", project="SDA")
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

        # --- 7: Bookmark manager ----------------------------------------------
        elif choice == "7":
            state = _manage_bookmarks(state, email, token)

        # --- 9: Legacy alias (HO radar is now option 3 → HO → Radar filter) ---
        elif choice == "9":
            print(f"\n  {DIM}HO radar moved: use option 3 → pick HO → status filter 'Radar'.{RESET}")
            _brief_pause()

        # --- v: Stale verification review (uses cached data from banner check) ---
        elif choice == "v":
            if _stale_cache:
                action = _run_stale_verification(_stale_cache, email, token)
                if action == "quit":
                    _stop_background_watcher()
                    print(f"\n  {DIM}Goodbye.{RESET}\n")
                    return
            else:
                print(f"\n  {GREEN}No stale verification tickets!{RESET}")
                time.sleep(1)

        elif choice in ("?", "h", "help"):
            _clear_screen()
            _print_help()
            input(f"  {DIM}Press ENTER to return to menu...{RESET}")
            _clear_screen()

        else:
            # AI default-on: route unrecognized input to AI chat
            if _AI_ENABLED and _ai_available() and len(choice) > 1:
                found_key = _ai_dispatch(email=email, token=token, initial_msg=choice)
                if found_key and JIRA_KEY_PATTERN.match(found_key):
                    ctx = _fetch_and_show(found_key, email, token)
                    if ctx:
                        state = _record_ticket_view(state, ctx["issue_key"], ctx.get("summary", ""),
                                               assignee=ctx.get("assignee"), updated=ctx.get("updated"))
                        _save_user_state(state)
                        _clear_screen()
                        _print_pretty(ctx)
                        action = _post_detail_prompt(ctx, email, token, state=state)
                        if action == "quit":
                            print(f"\n  {DIM}Goodbye.{RESET}\n")
                            return
                        while action == "history":
                            tag = ctx.get("service_tag") or ctx.get("hostname")
                            if not tag:
                                break
                            h_action = _run_history_interactive(email, token, tag)
                            if h_action == "quit":
                                print(f"\n  {DIM}Goodbye.{RESET}\n")
                                return
                            _clear_screen()
                            _print_pretty(ctx)
                            action = _post_detail_prompt(ctx, email, token, state=state)
            else:
                print(f"\n  {DIM}Invalid choice. Try 1-7, v, ?, or q.{RESET}")


# ---------------------------------------------------------------------------
# Data hall layout config
# ---------------------------------------------------------------------------


def _load_dh_layouts() -> dict:
    """Load saved DH layouts from JSON config file."""
    if os.path.exists(_DH_CONFIG_PATH):
        try:
            with open(_DH_CONFIG_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_dh_layouts(layouts: dict):
    """Persist DH layouts to JSON config file."""
    with open(_DH_CONFIG_PATH, "w") as f:
        json.dump(layouts, f, indent=2)
    try:
        os.chmod(_DH_CONFIG_PATH, 0o600)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Persistent user state (recents, bookmarks, greeting cache)
# ---------------------------------------------------------------------------

_DEFAULT_STATE = {
    "version": 1,
    "user": {},
    "recent_tickets": [],
    "recent_nodes": [],
    "recent_racks": [],
    "recent_queues": [],
    "last_ticket": None,
    "bookmarks": [],
    "weekend_robin": {},
}


def _load_user_state() -> dict:
    """Load persistent user state (recents, bookmarks, greeting) from JSON."""
    if os.path.exists(_USER_STATE_PATH):
        try:
            with open(_USER_STATE_PATH) as f:
                data = json.load(f)
            # Ensure all expected keys exist (forward compat)
            for k, v in _DEFAULT_STATE.items():
                data.setdefault(k, v if not isinstance(v, (list, dict)) else type(v)())
            return data
        except (json.JSONDecodeError, OSError):
            pass
    return {k: (v if not isinstance(v, (list, dict)) else type(v)()) for k, v in _DEFAULT_STATE.items()}


def _save_user_state(state: dict):
    """Persist user state to JSON file."""
    state["version"] = 1
    with open(_USER_STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)
    try:
        os.chmod(_USER_STATE_PATH, 0o600)
    except OSError:
        pass


def _record_ticket_view(state: dict, key: str, summary: str,
                        assignee: str = None, updated: str = None) -> dict:
    """Record a ticket view — updates last_ticket and recent_tickets (max 5)."""
    import datetime
    now = datetime.datetime.utcnow().isoformat() + "Z"
    state["last_ticket"] = key
    recents = state.get("recent_tickets", [])
    recents = [r for r in recents if r.get("key") != key]
    entry = {"key": key, "summary": (summary or "")[:80], "ts": now}
    if assignee:
        entry["assignee"] = assignee
    if updated:
        entry["updated"] = updated
    recents.insert(0, entry)
    state["recent_tickets"] = recents[:5]
    return state


def _record_node_lookup(state: dict, term: str,
                        hostname: str = None, last_ticket: str = None,
                        site: str = None) -> dict:
    """Record a node lookup — updates recent_nodes (max 5)."""
    import datetime
    now = datetime.datetime.utcnow().isoformat() + "Z"
    normalized = term.strip()
    recents = state.get("recent_nodes", [])
    # Preserve existing extra info if we're re-recording the same term
    old = next((r for r in recents if r.get("term", "").lower() == normalized.lower()), None)
    recents = [r for r in recents if r.get("term", "").lower() != normalized.lower()]
    entry = {"term": normalized, "ts": now}
    if hostname:
        entry["hostname"] = hostname
    elif old and old.get("hostname"):
        entry["hostname"] = old["hostname"]
    if last_ticket:
        entry["last_ticket"] = last_ticket
    elif old and old.get("last_ticket"):
        entry["last_ticket"] = old["last_ticket"]
    if site:
        entry["site"] = site
    elif old and old.get("site"):
        entry["site"] = old["site"]
    recents.insert(0, entry)
    state["recent_nodes"] = recents[:5]
    return state


def _record_queue_view(state: dict, project: str, site: str,
                       status_filter: str, mine_only: bool = False) -> dict:
    """Record a queue browse — updates recent_queues (max 5)."""
    import datetime
    now = datetime.datetime.utcnow().isoformat() + "Z"
    params = {"project": project, "site": site,
              "status_filter": status_filter, "mine_only": mine_only}
    sig = json.dumps(params, sort_keys=True)
    recents = state.get("recent_queues", [])
    recents = [r for r in recents if json.dumps(r.get("params", {}), sort_keys=True) != sig]
    site_label = site or "all sites"
    label = f"{project} {status_filter} @ {site_label}"
    if mine_only:
        label += " (mine)"
    recents.insert(0, {"label": label, "params": params, "ts": now})
    state["recent_queues"] = recents[:5]
    _save_user_state(state)
    return state


def _record_rack_view(state: dict, loc: str, tag: str = "") -> dict:
    """Record a rack map view — updates recent_racks (max 5)."""
    import datetime
    now = datetime.datetime.utcnow().isoformat() + "Z"
    recents = state.get("recent_racks", [])
    recents = [r for r in recents if r.get("loc", "").lower() != loc.lower()]
    recents.insert(0, {"loc": loc, "tag": tag, "ts": now})
    state["recent_racks"] = recents[:5]
    return state


def _add_bookmark(state: dict, label: str, bm_type: str, params: dict) -> dict:
    """Add a bookmark. Deduplicates by type+params. Max 5 bookmarks."""
    bookmarks = state.get("bookmarks", [])
    # Remove existing bookmark with same type+params
    bookmarks = [b for b in bookmarks if not (b.get("type") == bm_type and b.get("params") == params)]
    bookmarks.append({"label": label, "type": bm_type, "params": params})
    state["bookmarks"] = bookmarks[:5]
    return state


def _remove_bookmark(state: dict, index: int) -> dict:
    """Remove a bookmark by index."""
    bookmarks = state.get("bookmarks", [])
    if 0 <= index < len(bookmarks):
        bookmarks.pop(index)
    state["bookmarks"] = bookmarks
    return state


def _get_dh_layout(site_code: str, dh: str) -> dict | None:
    """Look up a saved layout for a site+dh combo (e.g. 'US-EVI01', 'DH1').

    Returns dict with keys: columns (list of {"label", "start", "num_rows"}),
    racks_per_row, serpentine, entrance.  Or None if not configured.
    """
    layouts = _load_dh_layouts()
    key = f"{site_code}.{dh}"
    return layouts.get(key)


def _setup_dh_layout(site_code: str, dh: str) -> dict | None:
    """Prompt user to manually edit dh_layouts.json for a new data hall.

    Returns None (user must edit the file and re-run).
    """
    key = f"{site_code}.{dh}"
    print(f"\n  {BOLD}No layout saved for {key}{RESET}")
    print(f"  {DIM}To add one, edit {os.path.basename(_DH_CONFIG_PATH)} and add:{RESET}\n")
    example = json.dumps({key: {
        "racks_per_row": 10,
        "columns": [
            {"label": "Left", "start": 1, "num_rows": 16},
            {"label": "Right", "start": 161, "num_rows": 15},
        ],
        "serpentine": True,
        "entrance": "bottom-right",
        "total_racks": 310,
    }}, indent=2)
    print(f"  {DIM}{example}{RESET}\n")
    print(f"  {DIM}Notes:{RESET}")
    print(f"  {DIM}  - serpentine: true = zig-zag rows (like EVI01 DH1), false = straight rows{RESET}")
    print(f"  {DIM}  - columns: can have 2+ blocks (e.g. A/B/C for 3-column halls like SEC1){RESET}")
    print(f"  {DIM}  - Each column: start = first rack #, num_rows = rows in that block{RESET}\n")
    print(f"  {DIM}File: {_DH_CONFIG_PATH}{RESET}\n")

    # Create the file with the example if it doesn't exist
    if not os.path.exists(_DH_CONFIG_PATH):
        try:
            with open(_DH_CONFIG_PATH, "w") as f:
                f.write(example)
        except OSError:
            pass

    try:
        raw = input(f"  Press {BOLD}o{RESET} to open in editor, or ENTER to go back: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if raw == "o":
        import subprocess
        editor = os.environ.get("EDITOR", "")
        if not editor:
            # Try common editors
            import shutil as _shutil
            for e in ("code", "nano", "vim", "vi"):
                if _shutil.which(e):
                    editor = e
                    break
        if editor:
            print(f"  {DIM}Opening {_DH_CONFIG_PATH} with {editor}...{RESET}")
            try:
                subprocess.run([editor, _DH_CONFIG_PATH])
            except Exception:
                print(f"  {DIM}Could not open editor. Edit manually: {_DH_CONFIG_PATH}{RESET}")
        else:
            print(f"  {DIM}No editor found. Edit manually: {_DH_CONFIG_PATH}{RESET}")
    return None


# ---------------------------------------------------------------------------
# Output — mini data hall map
# ---------------------------------------------------------------------------

def _draw_mini_dh_map(rack_loc: str):
    """Draw a miniature data hall map with per-dash rack display and walking route.

    Uses saved DH layout config when available, falls back to built-in DH1
    layout for US-EVI01/US-CENTRAL-07A.  For unknown data halls, offers to
    run the setup wizard.

    Each dash = 1 cab.  Target rack highlighted in cyan.
    Yellow walking route from entrance (bottom) to target rack.
    Blank line between every 2 rows = walking aisle.
    """
    parsed = _parse_rack_location(rack_loc)
    if not parsed:
        return

    target = parsed["rack"]
    site_code = parsed["site_code"]
    dh = parsed["dh"]

    # --- Resolve layout: saved config > built-in DH1 > offer setup ---
    layout = _get_dh_layout(site_code, dh)

    if layout is None:
        # Built-in fallback for DH1 at known EVI/CENTRAL sites
        if dh.upper() == "DH1" and any(
            s in site_code.upper() for s in ("EVI01", "CENTRAL-07")
        ):
            layout = {
                "racks_per_row": 10,
                "columns": [
                    {"label": "Left",  "start": 1,   "num_rows": 14},
                    {"label": "Right", "start": 141,  "num_rows": 18},
                ],
                "serpentine": True,
                "entrance": "bottom-right",
            }
        else:
            print(f"\n  {DIM}No data hall map configured for {site_code} {dh}.{RESET}")
            raw = input(f"  Would you like to set one up? [y/{CYAN}n{RESET}]: ").strip().lower()
            if raw == "y":
                layout = _setup_dh_layout(site_code, dh)
            if layout is None:
                return

    cols = layout["columns"]
    default_per_row = layout["racks_per_row"]
    serpentine = layout.get("serpentine", True)
    entrance = layout.get("entrance", "bottom-right")

    # --- Rack-at helper (serpentine or sequential, per-column width) ---
    def rack_at(col_start, row, pos, col_per_row=None):
        pr = col_per_row or default_per_row
        base = col_start + row * pr
        if serpentine and row % 2 == 1:
            return base + (pr - 1 - pos)
        return base + pos

    # Use default per_row for visual rendering width
    per_row = default_per_row

    def build_row(col_start, row, col_per_row=None):
        pr = col_per_row or default_per_row
        chars = []
        for pos in range(pr):
            if rack_at(col_start, row, pos) == target:
                chars.append(f"{CYAN}{BOLD}#{RESET}")
            else:
                chars.append("-")
        return "".join(chars)

    # --- Find which column and row the target is in ---
    target_col_idx = -1
    target_row = -1
    side = "?"
    for ci, col in enumerate(cols):
        col_pr = col.get("racks_per_row", default_per_row)
        col_end = col["start"] + col["num_rows"] * col_pr - 1
        if col["start"] <= target <= col_end:
            target_col_idx = ci
            target_row = (target - col["start"]) // col_pr
            side = col["label"].upper()
            break

    max_rows = max(c["num_rows"] for c in cols)
    COL_GAP = "       "  # 7 spaces between column pairs (consistent for any # of columns)

    # Walking route only enabled for EVI01 DH1 (well-tested 2-column serpentine).
    # Other sites show the map with # marker and row-end labels only.
    _is_evi01_dh1 = dh.upper() == "DH1" and any(
        s in site_code.upper() for s in ("EVI01", "CENTRAL-07")
    )
    has_route = _is_evi01_dh1 and target_row >= 0 and len(cols) >= 2

    # --- Compute route gap position for animated rendering ---
    if has_route:
        if target_col_idx < len(cols) - 1:
            route_gap_idx = target_col_idx
        else:
            route_gap_idx = len(cols) - 2
    else:
        route_gap_idx = 0

    # --- Build all display lines ---
    header1 = f"\n  {BOLD}{site_code} {dh}{RESET} {DIM}— Rack R{target}{RESET}"
    hdr_parts = []
    for c in cols:
        end = c["start"] + c["num_rows"] * per_row - 1
        hdr_parts.append(f"{c['label']} (R{c['start']}-R{end})")
    header2 = f"  {DIM}{COL_GAP.join(hdr_parts)}{RESET}"

    body = []  # list of dicts: {plain, on_path, is_turn}
    for row in range(max_rows):
        col_strs = []
        for ci, col in enumerate(cols):
            if row < col["num_rows"]:
                col_strs.append(build_row(col["start"], row, col.get("racks_per_row")))
            else:
                col_strs.append(" " * per_row)

        on_path = target_row >= 0 and row >= target_row
        is_turn = target_row >= 0 and row == target_row

        # Plain line with optional row reference numbers
        if not has_route:
            # Left label: first rack of first visible column
            first_col = None
            for ci in range(len(cols)):
                if row < cols[ci]["num_rows"]:
                    first_col = cols[ci]
                    break
            if first_col:
                fc_pr = first_col.get("racks_per_row")
                start_rack = rack_at(first_col["start"], row, 0, fc_pr)
                plain = f"  {DIM}R{start_rack:<4}{RESET} {col_strs[0]}"
            else:
                plain = f"        {col_strs[0]}"
        else:
            plain = f"  {col_strs[0]}"
        for ci in range(1, len(cols)):
            plain += COL_GAP + col_strs[ci]

        # Right label: last rack of last visible column (non-route only)
        if not has_route:
            last_col = None
            for ci in range(len(cols) - 1, -1, -1):
                if row < cols[ci]["num_rows"]:
                    last_col = cols[ci]
                    break
            if last_col:
                lc_pr = last_col.get("racks_per_row", default_per_row)
                end_rack = rack_at(last_col["start"], row, lc_pr - 1, lc_pr)
                plain += f"  {DIM}R{end_rack}{RESET}"

        body.append({"plain": plain, "on_path": on_path, "is_turn": is_turn})

        # Aisle line
        if row % 2 == 1 and row < max_rows - 1:
            body.append({"plain": "", "on_path": on_path, "is_turn": False})

    # Entrance line — spans from the route corridor gap all the way to the right edge
    entrance_line = ""
    if has_route:
        # The corridor is in the gap after column route_gap_idx
        # Entrance spans from that gap to the right edge of the last column
        gap_char_start = (route_gap_idx + 1) * per_row + route_gap_idx * len(COL_GAP)
        total_width = len(cols) * per_row + (len(cols) - 1) * len(COL_GAP)
        entrance_width = total_width - gap_char_start
        entrance_line = f"  {' ' * gap_char_start}{YELLOW}{BOLD}{'=' * entrance_width}{RESET}"

    if has_route:
        footer = [
            "",
            "",
            f"  {CYAN}{BOLD}#{RESET} = R{target} ({side} column)  {YELLOW}{BOLD}==={RESET} walking route",
            "",
        ]
    else:
        footer = [
            "",
            "",
            f"  {CYAN}{BOLD}#{RESET} = R{target} ({side} column)",
            "",
        ]

    # --- Render (animated if terminal, static if piped) ---
    animate = sys.stdout.isatty() and has_route

    if not animate:
        # Static: use pre-built body lines (includes row-end labels, no route for non-DH1)
        print(header1)
        print(header2)
        print()
        for bl in body:
            print(bl["plain"])
        if entrance_line:
            print(entrance_line)
        for fl in footer:
            print(fl)
        return

    # ── Animated render ──────────────────────────────────────────────
    ROW_DELAY = 0.015 if _ANIMATE else 0    # map rows appear top→bottom
    ROUTE_DELAY = 0.02 if _ANIMATE else 0  # route traces bottom→top

    # ANSI column positions (1-indexed) — computed dynamically for any # of columns
    # The route corridor runs through the gap at route_gap_idx.
    # Gap i starts at: indent(2) + (i+1)*per_row + i*len(COL_GAP) + 1
    gap_start = 2 + (route_gap_idx + 1) * per_row + route_gap_idx * len(COL_GAP) + 1
    gap_w = len(COL_GAP)
    corridor_col = gap_start + gap_w // 2  # middle of the gap

    # Phase 1 — map loads (no route)
    print(header1)
    time.sleep(0.08)
    print(header2)
    print()
    time.sleep(0.05)

    for bl in body:
        print(bl["plain"])
        sys.stdout.flush()
        time.sleep(ROW_DELAY)

    # Phase 2 — route traces from entrance upward
    if _ANIMATE:
        time.sleep(0.15)
    print(entrance_line)
    sys.stdout.flush()
    time.sleep(0.15)

    # Cursor is now 1 line below entrance.
    # body[i] is (len(body) - i + 1) lines up from cursor.
    n = len(body)
    half_gap = gap_w // 2
    for i in range(n - 1, -1, -1):
        bl = body[i]
        if not bl["on_path"]:
            continue
        lines_up = n - i + 1  # +1 for entrance line

        if bl["is_turn"]:
            # Paint the turn marker in the route gap
            sys.stdout.write(f"\033[{lines_up}A\033[{gap_start}G")
            if target_col_idx <= route_gap_idx:
                # Target is left of the gap — turn left
                sys.stdout.write(f"{YELLOW}{BOLD}{'=' * half_gap}+{RESET}{' ' * half_gap}")
            else:
                # Target is right of the gap — turn right
                sys.stdout.write(f"{' ' * half_gap}{YELLOW}{BOLD}+{'=' * half_gap}{RESET}")
            sys.stdout.write(f"\033[{lines_up}B\r")
        else:
            # Paint corridor |
            sys.stdout.write(f"\033[{lines_up}A\033[{corridor_col}G")
            sys.stdout.write(f"{YELLOW}|{RESET}")
            sys.stdout.write(f"\033[{lines_up}B\r")

        sys.stdout.flush()
        time.sleep(ROUTE_DELAY)

    # Footer
    for fl in footer:
        print(fl)


# ---------------------------------------------------------------------------
# Output — rack elevation view
# ---------------------------------------------------------------------------


def _fetch_device_type_heights(devices: list) -> dict:
    """Bulk-fetch u_height for all device types in a list of devices.

    Returns {device_type_id: u_height}.  One API call instead of N.
    """
    dt_ids = set()
    for dev in devices:
        dt = dev.get("device_type") or {}
        if dt.get("id"):
            dt_ids.add(dt["id"])
    if not dt_ids:
        return {}
    data = _netbox_get("/dcim/device-types/", params={"id": list(dt_ids), "limit": 50})
    if not data:
        return {}
    return {dt["id"]: dt.get("u_height", 1) for dt in data.get("results", [])}


def _draw_rack_elevation(ctx: dict) -> list:
    """Draw a visual rack elevation showing devices at their RU positions.

    Fetches all devices in the rack from NetBox, resolves u_height per
    device type, and renders a cabinet view with the current device
    highlighted.  Animates top-to-bottom when running in a terminal.

    Returns the list of devices in the rack (for use by the combined
    rack view handler), or an empty list on failure.
    """
    netbox = ctx.get("netbox", {})
    rack_id = netbox.get("rack_id")
    if not rack_id:
        print(f"\n  {DIM}No rack info available from NetBox.{RESET}")
        return []

    rack_name = netbox.get("rack") or f"Rack {rack_id}"
    current_device = netbox.get("device_name")
    current_pos = int(netbox["position"]) if netbox.get("position") else None
    site = ctx.get("site") or ""

    print(f"\n  {DIM}Loading rack elevation...{RESET}")
    devices = _netbox_get_rack_devices(rack_id)
    if not devices:
        print(f"\n  {DIM}No devices found in {rack_name}.{RESET}")
        return []

    # Bulk-fetch device type u_heights
    dt_heights = _fetch_device_type_heights(devices)

    # Build slot map: {ru_number: device}
    slots = {}          # ru -> device dict
    top_ru = {}         # device_name -> highest RU (top of device, where label goes)
    device_height = {}  # device_name -> u_height

    for dev in devices:
        pos = dev.get("position")
        if not pos:
            continue
        pos = int(pos)  # NetBox may return float (e.g. 34.0)
        dt_id = (dev.get("device_type") or {}).get("id")
        height = int(dt_heights.get(dt_id, 1))
        name = dev.get("name") or dev.get("display") or "?"
        device_height[name] = height
        top_ru[name] = pos + height - 1  # label at top of device block
        for u in range(pos, pos + height):
            slots[u] = dev

    # Fetch actual rack height from NetBox
    rack_data = _netbox_get(f"/dcim/racks/{rack_id}/")
    rack_height = int(rack_data.get("u_height", 42)) if rack_data else 42
    # Safety: ensure we at least cover all occupied slots
    if slots:
        rack_height = max(rack_height, max(slots.keys()))

    # Count stats
    unique_devices = {(d.get("name") or d.get("display") or id(d)) for d in devices if d.get("position")}
    occupied_u = len(slots)

    # --- Rendering ---
    COL_WIDTH = 50  # inner width of the rack frame
    animate = sys.stdout.isatty() and _ANIMATE
    ROW_DELAY = 0.01

    def _device_label(dev, is_first):
        """Build the label for a rack slot."""
        name = dev.get("name") or dev.get("display") or "?"
        short = _short_device_name(name)
        is_current = current_device and name == current_device

        if not is_first:
            # Continuation RU of a multi-U device
            marker = f"{CYAN}┆┆{RESET}" if is_current else f"{DIM}┆{RESET}"
            return marker, is_current

        # Top RU — show short name + role + status
        role = (dev.get("role") or dev.get("device_role") or {}).get("display") or ""
        status_label = (dev.get("status") or {}).get("label") or ""
        role_short = role[:12] if role else ""
        status_short = status_label[:10] if status_label else ""

        if is_current:
            label = f"{CYAN}{BOLD}{short}{RESET}"
            suffix = f"  {DIM}{role_short}  {status_short}{RESET}"
            marker_text = f"{CYAN}{BOLD}>>{RESET}  {label}{suffix}"
        else:
            suffix = f"  {DIM}{role_short}  {status_short}{RESET}"
            marker_text = f"    {short}{suffix}"

        return marker_text, is_current

    # Clear the "loading" message
    sys.stdout.write("\033[A\033[K")
    sys.stdout.flush()

    # Header
    header = f"\n  {BOLD}{rack_name}{RESET}  {DIM}{site}{RESET}  {DIM}{rack_height}U{RESET}"
    top_border = f"  ┌{'─' * COL_WIDTH}┐"
    bottom_border = f"  └{'─' * COL_WIDTH}┘"

    print(header)
    if animate:
        time.sleep(0.08)
    print(top_border)
    if animate:
        time.sleep(0.03)

    # Track lines for animation (highlighting current device after draw)
    body_lines = []

    # Draw rows top-to-bottom
    u = rack_height
    while u >= 1:
        dev = slots.get(u)
        u_label = f"U{u:<3}"

        if dev:
            name = dev.get("name") or dev.get("display") or "?"
            is_top = (top_ru.get(name) == u) if name != "?" else True
            label_text, is_current = _device_label(dev, is_top)

            if is_current:
                line = f"  │ {CYAN}{u_label}{RESET} {label_text}"
            else:
                line = f"  │ {DIM}{u_label}{RESET} {label_text}"
        else:
            # Empty slot — check for runs of empties to compress
            empty_start = u
            while u - 1 >= 1 and u - 1 not in slots:
                u -= 1
            empty_end = u

            if empty_start - empty_end >= 3:
                # Compress large empty runs
                line = f"  │ {DIM}{u_label}  ...  (empty U{empty_start}-U{empty_end}){RESET}"
            elif empty_start == empty_end:
                line = f"  │ {DIM}{u_label}{RESET}"
            else:
                # Small gap: print individually
                for uu in range(empty_start, empty_end, -1):
                    body_lines.append(f"  │ {DIM}U{uu:<3}{RESET}")
                line = f"  │ {DIM}U{empty_end:<3}{RESET}"

        body_lines.append(line)
        u -= 1

    # Print body lines
    for bl in body_lines:
        print(bl)
        if animate:
            sys.stdout.flush()
            time.sleep(ROW_DELAY)

    print(bottom_border)

    # Footer
    print(f"\n  {len(unique_devices)} devices  {DIM}│{RESET}  {occupied_u}/{rack_height}U occupied")
    if current_device and current_pos:
        h = device_height.get(current_device, 1)
        top_u = current_pos + h - 1
        pos_range = f"U{current_pos}-U{top_u}" if h > 1 else f"U{current_pos}"
        print(f"  {CYAN}{BOLD}>>{RESET} = {CYAN}{current_device}{RESET} at {pos_range}")
    print()

    return devices


# ---------------------------------------------------------------------------
# Output — pretty (default)
# ---------------------------------------------------------------------------

def _print_pretty(ctx: dict):
    """Print a clean, readable summary with color and icons."""
    status = ctx["status"]
    sc, sd = _status_color(status)

    key = ctx["issue_key"]
    line = "\u2500" * 50

    status_upper = status.upper()

    if ctx.get("source") == "netbox":
        # Device view — no Jira ticket
        print()
        print(f"  {sc}{'━' * 50}{RESET}")
        print(f"  {sc}{BOLD}{sd}{RESET}  {WHITE}{BOLD}{ctx['summary']}{RESET}    {sc}{BOLD}{status_upper}{RESET}")
        print(f"     {DIM}{ctx.get('hostname', '')}{RESET}")
        print(f"  {sc}{'━' * 50}{RESET}")
        print(f"  {GREEN}No open Jira tickets for this device{RESET}")
        print(f"  {DIM}{line}{RESET}")
    else:
        # Ticket view — bold key + status badge + assignee
        node_tag = f"  {DIM}({ctx['node_name']}){RESET}" if ctx.get("node_name") else ""
        assignee = ctx["assignee"] or "Unassigned"
        ac = f"{MAGENTA}{BOLD}{assignee}{RESET}" if ctx["assignee"] else f"{DIM}{assignee}{RESET}"

        print()
        print(f"  {sc}{'━' * 50}{RESET}")
        print(f"  {sc}{BOLD}{sd}{RESET}  {WHITE}{BOLD}{key}{RESET}    {sc}{BOLD}{status_upper}{RESET}{node_tag}")
        print(f"     {DIM}{ctx['summary']}{RESET}")
        print(f"  {sc}{'━' * 50}{RESET}")
        # Ticket age in current status
        age_secs = ctx.get("status_age_seconds", 0)
        if age_secs > 0:
            age_str = _format_age(age_secs)
            if age_secs > 48 * 3600:
                age_color = RED
            elif age_secs > 24 * 3600:
                age_color = YELLOW
            else:
                age_color = GREEN
            print(f"  {age_color}In {status} for {age_str}{RESET}")

        reporter = ctx.get("reporter") or ""
        rep_part = f"  {DIM}\u2502{RESET}  Reporter  {DIM}{reporter}{RESET}" if reporter else ""
        print(f"  Project  {BOLD}{ctx['project']}{RESET}  {DIM}\u2502{RESET}  Type  {ctx['issue_type']}  {DIM}\u2502{RESET}  Assignee  {ac}{rep_part}")
        print(f"  {DIM}{line}{RESET}")

    # Unified node info — Jira fields + NetBox enrichment in one block
    netbox = ctx.get("netbox", {})

    # Build combined rows: Jira data with NetBox extras mixed in
    node_rows = [
        ("Site",        ctx["site"]),
        ("Rack",        ctx["rack_location"]),
        ("Service Tag", ctx["service_tag"]),
        ("Hostname",    ctx["hostname"]),
        ("Vendor",      ctx["vendor"]),
    ]
    # Add IPs only when they have real values
    ip = ctx.get("ip_address")
    if ip and ip != "0.0.0.0":
        node_rows.append(("IP", ip))
    if netbox:
        oob_raw = netbox.get("oob_ip")
        if oob_raw:
            node_rows.append(("BMC IP", oob_raw.split("/")[0]))
        ip6_raw = netbox.get("primary_ip6")
        if ip6_raw:
            node_rows.append(("IPv6", ip6_raw.split("/")[0]))
    # Add NetBox-only fields inline (no separate section)
    if netbox:
        if netbox.get("asset_tag"):
            node_rows.append(("Asset Tag", netbox["asset_tag"]))
        if netbox.get("position"):
            node_rows.append(("RU", f"U{netbox['position']}"))
        if netbox.get("model"):
            node_rows.append(("Model", netbox["model"]))
        if netbox.get("device_role"):
            node_rows.append(("Role", netbox["device_role"]))
        if netbox.get("status"):
            node_rows.append(("NB Status", netbox["status"]))

    for label, value in node_rows:
        if not value:
            continue  # hide rows with no data
        print(f"  {label:<14} {CYAN}{value}{RESET}")

    # Parsed location breakdown (from rack_location or hostname)
    _parsed = _parse_rack_location(ctx.get("rack_location", ""))
    if _parsed:
        _loc_parts = []
        if _parsed.get("dh"):
            _loc_parts.append(f"Data Hall  {CYAN}{_parsed['dh']}{RESET}")
        if _parsed.get("rack") is not None:
            _loc_parts.append(f"Rack #  {CYAN}{_parsed['rack']}{RESET}")
        if _parsed.get("ru"):
            _loc_parts.append(f"RU  {CYAN}{_parsed['ru']}{RESET}")
        # Extract node position from hostname (e.g. dh1-r244-node-08-us-central-07a → 08)
        _hn = ctx.get("hostname", "")
        _node_match = re.search(r'-node-(\d+)-', _hn)
        if _node_match:
            _loc_parts.append(f"Node  {CYAN}{_node_match.group(1)}{RESET}")
        if _loc_parts:
            print(f"  {DIM}{'─' * 50}{RESET}")
            print(f"  {DIM}Location:{RESET}  {'  │  '.join(_loc_parts)}")

    # RMA reason + node name
    if ctx.get("rma_reason") or ctx.get("node_name"):
        print(f"  {DIM}{line}{RESET}")
        if ctx.get("rma_reason"):
            print(f"  {'RMA Reason':<14} {YELLOW}{ctx['rma_reason']}{RESET}")
        if ctx.get("node_name"):
            print(f"  {'Node':<14} {CYAN}{ctx['node_name']}{RESET}")

    # HO context (linked HO for this DO)
    ho = ctx.get("ho_context")
    if ho and ctx.get("source") != "netbox":
        print(f"  {DIM}{line}{RESET}")
        hsc, hsd = _status_color(ho["status"])
        print(f"  {BOLD}HO Context{RESET}  {hsc}{hsd} {ho['key']}{RESET}  {hsc}{ho['status']}{RESET}")
        print(f"  {DIM}{ho['summary']}{RESET}")
        print(f"  {MAGENTA}{ho['hint']}{RESET}")
        if ho.get("last_note"):
            print(f"  {DIM}Last note: {ho['last_note']}{RESET}")

    # SLA timers (Jira tickets only)
    sla_values = ctx.get("sla", [])
    if sla_values and ctx.get("source") != "netbox":
        print(f"  {DIM}{line}{RESET}")
        print(f"  {BOLD}SLA{RESET}")
        for sla in sla_values:
            sla_name = sla.get("name", "?")
            ongoing = sla.get("ongoingCycle")
            completed = sla.get("completedCycles", [])

            if not ongoing and completed:
                last = completed[-1]
                breached = last.get("breached", False)
                elapsed = (last.get("elapsedTime") or {}).get("friendly", "?")
                if breached:
                    print(f"    {RED}\u25cf Breached{RESET}  {DIM}{sla_name}  (took {elapsed}){RESET}")
                else:
                    print(f"    {GREEN}\u25cf Met{RESET}       {DIM}{sla_name}  (in {elapsed}){RESET}")
            elif ongoing:
                if ongoing.get("breached"):
                    elapsed = (ongoing.get("elapsedTime") or {}).get("friendly", "?")
                    print(f"    {RED}\u25cf Breached{RESET}  {sla_name}  {DIM}({elapsed} elapsed){RESET}")
                elif ongoing.get("paused"):
                    remaining = (ongoing.get("remainingTime") or {}).get("friendly", "?")
                    print(f"    {BLUE}\u25cf Paused{RESET}    {sla_name}  {DIM}({remaining} remaining){RESET}")
                else:
                    remaining_ms = (ongoing.get("remainingTime") or {}).get("millis", 0)
                    goal_ms = (ongoing.get("goalDuration") or {}).get("millis", 1)
                    remaining_str = (ongoing.get("remainingTime") or {}).get("friendly", "?")
                    pct = remaining_ms / goal_ms if goal_ms else 0
                    if pct < 0.25:
                        color = RED
                    elif pct < 0.50:
                        color = YELLOW
                    else:
                        color = GREEN
                    print(f"    {color}\u25cf {remaining_str}{RESET}  {sla_name}  {DIM}remaining{RESET}")

    print()


# ---------------------------------------------------------------------------
# Output — JSON / raw
# ---------------------------------------------------------------------------

def _print_json(ctx: dict):
    """Print only a clean JSON object to stdout.
    Includes all fields except raw_issue and internal _-prefixed keys."""
    out = {k: v for k, v in ctx.items() if not k.startswith("_") and k != "raw_issue"}
    print(json.dumps(out, indent=2))


def _print_raw(ctx: dict):
    """Print the full raw Jira issue JSON to stdout."""
    print(json.dumps(ctx["raw_issue"], indent=2))


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    """Dispatch: no args = interactive menu, with args = one-shot mode."""
    raw_args = sys.argv[1:]

    # No arguments at all → launch interactive menu
    if not raw_args:
        _interactive_menu()
        return

    # -h / --help → print help and exit
    if raw_args[0] in ("-h", "--help"):
        _print_cli_help()
        return

    # "queue" subcommand (one-shot, scriptable)
    if raw_args[0] == "queue":
        _cli_queue(raw_args[1:])
        return

    # "history" subcommand
    if raw_args[0] == "history":
        _cli_history(raw_args[1:])
        return

    # "watch" subcommand
    if raw_args[0] == "watch":
        _cli_watch(raw_args[1:])
        return

    # "weekend-assign" subcommand
    if raw_args[0] == "weekend-assign":
        _cli_weekend_assign(raw_args[1:])
        return

    # Anything else → one-shot lookup
    _cli_lookup(raw_args)


def _print_cli_help():
    """Print help text for CLI one-shot mode."""
    print(f"""
  CoreWeave DCT Node Helper  v{APP_VERSION}

  USAGE
    python3 get_node_context.py                           # interactive menu
    python3 get_node_context.py <identifier> [options]    # one-shot lookup
    python3 get_node_context.py queue --site <SITE>       # one-shot queue
    python3 get_node_context.py history <identifier>      # node ticket history
    python3 get_node_context.py watch --site <SITE>       # live queue watcher
    python3 get_node_context.py weekend-assign --site <SITE> --group <GROUP>

  IDENTIFIER (pick one)
    DO-12345        Jira ticket key (DO or HO)
    10NQ724         Dell service tag
    d0001142        Hostname

  OPTIONS
    --json          Output structured JSON only
    --raw           Output full raw Jira JSON
    -h, --help      Show this help

  QUEUE OPTIONS
    --site, -s      Site to filter (e.g. US-EAST-03). Omit for all sites
    --status        open, closed, verification, "in progress", waiting, all
    --project, -p   DO or HO (default: DO)
    --mine, -m      Only your tickets
    --limit, -l     Max results (default: 20)
    --json          Output queue as JSON

  WATCH OPTIONS
    --site, -s      Site to filter (e.g. US-CENTRAL-07A)
    --project, -p   DO or HO (default: DO)
    --interval, -i  Seconds between checks (default: 300 = 5 min)
    --weekend-group Jira group for weekend auto-assignment round-robin

  WEEKEND-ASSIGN OPTIONS
    --site, -s      Site to filter (required)
    --group, -g     Jira group name for team roster (required)
    --project, -p   DO or HO (default: DO)
    --dry-run       Show what would be assigned without making changes
    --force         Run even on weekdays (for testing)
    --json          Output results as JSON

  EXAMPLES
    python3 get_node_context.py                                          # interactive
    python3 get_node_context.py DO-12345                                 # lookup
    python3 get_node_context.py 10NQ724                                  # search
    python3 get_node_context.py DO-12345 --json                          # JSON output
    python3 get_node_context.py queue --site US-EAST-03                  # open DO
    python3 get_node_context.py queue --site US-EVI01 --status closed    # closed DO
    python3 get_node_context.py queue --site US-EVI01 --project HO       # HO queue
    python3 get_node_context.py queue --status verification --mine       # my verification
    python3 get_node_context.py history 10NQ724                          # node history
    python3 get_node_context.py history d0001142 --json                  # history as JSON
    python3 get_node_context.py watch --site US-CENTRAL-07A             # watch queue
    python3 get_node_context.py watch --site US-CENTRAL-07A -i 180      # every 3 min
    python3 get_node_context.py weekend-assign -s US-CENTRAL-07A -g dct-ops          # auto-assign
    python3 get_node_context.py weekend-assign -s US-CENTRAL-07A -g dct-ops --dry-run --force

  SETUP (first time only)
    1. Generate a Jira API token:
       https://id.atlassian.com/manage-profile/security/api-tokens
    2. Set env vars (add to ~/.zshrc to keep them):
       export JIRA_EMAIL="you@coreweave.com"
       export JIRA_API_TOKEN="your-token"
    3. pip3 install requests
""")


def _cli_queue(args_list: list):
    """Handle: python3 get_node_context.py queue --site X [--status Y] [--project Z]"""
    parser = argparse.ArgumentParser(prog="get_node_context.py queue")
    parser.add_argument("--site", "-s", default="",
                        help="site filter (e.g. US-EAST-03). Omit for all sites")
    parser.add_argument("--mine", "-m", action="store_true")
    parser.add_argument("--limit", "-l", type=int, default=20)
    parser.add_argument("--status", default="open",
                        help="open, closed, verification, 'in progress', waiting, radar (HO only), all (default: open)")
    parser.add_argument("--project", "-p", default="DO",
                        help="DO or HO (default: DO)")
    parser.add_argument("--json", action="store_true", dest="json_mode")
    args = parser.parse_args(args_list)

    email, token = _get_credentials()

    if args.json_mode:
        _run_queue_json(email, token, args.site, args.mine, args.limit,
                        args.status, args.project.upper())
    else:
        _run_queue_interactive(email, token, args.site, args.mine, args.limit,
                               args.status, args.project.upper())


def _cli_history(args_list: list):
    """Handle: python3 get_node_context.py history <identifier> [--json] [--limit N]"""
    parser = argparse.ArgumentParser(prog="get_node_context.py history")
    parser.add_argument("identifier", help="service tag (10NQ724) or hostname (d0001142)")
    parser.add_argument("--limit", "-l", type=int, default=20)
    parser.add_argument("--json", action="store_true", dest="json_mode")
    args = parser.parse_args(args_list)

    email, token = _get_credentials()

    if args.json_mode:
        _run_history_json(email, token, args.identifier, args.limit)
    else:
        _run_history_interactive(email, token, args.identifier, args.limit)


def _cli_watch(args_list: list):
    """Handle: python3 get_node_context.py watch --site X [--project Y] [--interval N]"""
    parser = argparse.ArgumentParser(prog="get_node_context.py watch")
    parser.add_argument("--site", "-s", default="",
                        help="site filter (e.g. US-CENTRAL-07A). Omit for all sites")
    parser.add_argument("--project", "-p", default="DO",
                        help="DO or HO (default: DO)")
    parser.add_argument("--interval", "-i", type=int, default=300,
                        help="seconds between checks (default: 300 = 5 min)")
    parser.add_argument("--weekend-group", default="",
                        help="Jira group for weekend auto-assignment round-robin")
    args = parser.parse_args(args_list)

    email, token = _get_credentials()
    _run_queue_watcher(email, token, args.site, project=args.project.upper(),
                       interval=args.interval,
                       auto_assign_group=args.weekend_group)


def _cli_weekend_assign(args_list: list):
    """Handle: python3 get_node_context.py weekend-assign --site X --group Y"""
    parser = argparse.ArgumentParser(prog="get_node_context.py weekend-assign")
    parser.add_argument("--site", "-s", required=True,
                        help="site filter (e.g. US-CENTRAL-07A)")
    parser.add_argument("--group", "-g", required=True,
                        help="Jira group name for team roster")
    parser.add_argument("--project", "-p", default="DO",
                        help="DO or HO (default: DO)")
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would be assigned without making changes")
    parser.add_argument("--force", action="store_true",
                        help="run even on weekdays (for testing)")
    parser.add_argument("--json", action="store_true", dest="json_mode",
                        help="output results as JSON")
    args = parser.parse_args(args_list)

    email, token = _get_credentials()

    results = _weekend_auto_assign(
        site=args.site,
        group_name=args.group,
        email=email,
        token=token,
        project=args.project.upper(),
        dry_run=args.dry_run,
        force_weekend=args.force,
    )

    if args.json_mode:
        print(json.dumps(results, indent=2))
    else:
        if not results:
            print(f"\n  {DIM}No assignments made.{RESET}\n")
        else:
            prefix = "[DRY RUN] " if args.dry_run else ""
            print(f"\n  {BOLD}{prefix}{len(results)} ticket(s) assigned:{RESET}")
            for r in results:
                print(f"    {r['key']}  ->  {r['assigned_to']}  ({r['ts'][:16]})")
            print()


def _cli_lookup(raw_args: list):
    """Handle: python3 get_node_context.py <identifier> [--json] [--raw]"""
    identifier = None
    flags = []
    for arg in raw_args:
        if arg.startswith("-"):
            flags.append(arg)
        elif identifier is None:
            identifier = arg

    if not identifier:
        _print_cli_help()
        sys.exit(1)

    parser = argparse.ArgumentParser(prog="get_node_context.py")
    parser.add_argument("--json", action="store_true", dest="json_mode")
    parser.add_argument("--raw", action="store_true", dest="raw_mode")
    args = parser.parse_args(flags)

    identifier = identifier.strip()
    if re.match(r"^[A-Za-z]+-\d+$", identifier):
        identifier = identifier.upper()

    quiet = args.json_mode or args.raw_mode
    ctx = get_node_context(identifier, quiet=quiet)

    # --raw wins over --json
    if args.raw_mode:
        _print_raw(ctx)
    elif args.json_mode:
        _print_json(ctx)
    else:
        _print_pretty(ctx)


if __name__ == "__main__":
    main()
