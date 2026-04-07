"""Constants and configuration for CW Node Helper."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Load keys: vault first, then local .env as override
# ---------------------------------------------------------------------------
def _load_dotenv():
    # 1. Load from global vault (~/.config/keys/global.env)
    sys.path.insert(0, str(Path.home() / '.config' / 'keys'))
    try:
        from keys import load_into_env
        load_into_env()
    except Exception:
        pass

    # 2. Load local .env as override (project-specific values take precedence)
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _key, _val = _line.split("=", 1)
            _key = _key.strip()
            _val = _val.strip().strip('"').strip("'")
            if _key and _val:
                os.environ[_key] = _val  # local .env always wins

_load_dotenv()
__all__ = [
    'APP_VERSION', 'JIRA_BASE_URL', 'JIRA_KEY_PATTERN', 'CUSTOM_FIELDS',
    'SEARCH_PROJECTS', 'SDX_PROJECTS', 'ISSUE_DETAIL_FIELDS', 'KNOWN_SITES',
    'BOLD', 'DIM', 'RESET', 'RED', 'GREEN', 'YELLOW', 'CYAN', 'WHITE',
    'MAGENTA', 'BLUE', 'UNDERLINE', 'TRANSITION_MAP',
    'AI_MODEL', 'AI_BASE_URL', 'AI_MAX_TOKENS', 'AI_TEMPERATURE',
    '_AI_DOMAIN_KNOWLEDGE', 'AI_SYSTEM_PROMPT_TICKET', 'AI_SYSTEM_PROMPT_FINDER',
    'AI_SYSTEM_PROMPT_CHAT',
    '_PROJECT_ROOT', '_DH_CONFIG_PATH', '_USER_STATE_PATH',
    '_JQL_CACHE_TTL', '_ISSUE_CACHE_MAX', '_NETBOX_CACHE_MAX', '_JQL_CACHE_MAX',
    'HO_RADAR_STATUSES', 'PROCEDURE_KITS',
    'QUEUE_FILTERS', 'STALE_UNASSIGNED_HOURS', '_DEFAULT_STATE',
    '_FEATURE_REGISTRY', 'FEATURES', '_is_feature_enabled', '_load_features', '_save_features',
    '_HAS_OPENAI', '_openai_mod', '_HAS_PILLOW',
    '_session', '_executor',
    '_issue_cache', '_netbox_cache', '_jql_cache',
    '_ANIMATE', '_VISUAL_MAPS', '_AI_ENABLED', 'NTFY_TOPIC', '_NTFY_ENABLED',
    '_my_account_id', '_my_display_name', '_relates_link_type',
    '_watcher_thread', '_watcher_stop_event', '_watcher_queue',
    '_watcher_site', '_watcher_project', '_watcher_interval',
    '_ntfy_alerted',
    '_radar_thread', '_radar_stop_event', '_radar_queue',
    '_radar_known_keys', '_radar_interval',
]



try:
    import openai as _openai_mod
    _HAS_OPENAI = True
except ImportError:
    _openai_mod = None
    _HAS_OPENAI = False

try:
    from PIL import Image as _pil_Image, ImageDraw as _pil_ImageDraw
    _HAS_PILLOW = True
except ImportError:
    _HAS_PILLOW = False

# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

APP_VERSION = "6.5.0"

JIRA_BASE_URL = os.environ.get("JIRA_BASE_URL", "https://your-org.atlassian.net")

# Jira issue key pattern: uppercase letters, dash, digits (e.g. DO-12345)
JIRA_KEY_PATTERN = re.compile(r"^[A-Z]+-\d+$")

# Known custom field IDs discovered from real DO/HO ticket JSON.
CUSTOM_FIELDS = {
    "customfield_10207": "rack_location",   # cf[10207] — e.g. "US-SITE01.DC7.R297.RU18"
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
    "created", "updated", "statuscategorychangedate", "attachment",
]

# Known site strings (from Jira cf[10194] values seen in real tickets).
KNOWN_SITES = [s.strip() for s in os.environ.get("KNOWN_SITES", "").split(",") if s.strip()]

# ---------------------------------------------------------------------------
# ANSI colors
# ---------------------------------------------------------------------------
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
# Status transition mapping
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
    "revert_verify": {
        "target_status": ["verification"],
        "transition_hints": ["verif", "review", "back to verif"],
    },
    "close": {
        "target_status": ["closed", "done", "resolved"],
        "transition_hints": ["close", "done", "resolve", "complete"],
    },
}

# ---------------------------------------------------------------------------
# AI Assistant configuration
# ---------------------------------------------------------------------------
AI_MODEL = os.environ.get("AI_MODEL", "gpt-4o")
AI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "").strip() or None
AI_MAX_TOKENS = 1024
AI_TEMPERATURE = 0.3

_AI_DOMAIN_KNOWLEDGE = (
    "\n\n--- Jira Ticket System ---\n"
    "PROJECTS:\n"
    "- DO (Data Operations): Hands-on DCT work — reseat, swap, cable, power cycle, inspections. "
    "Created when physical site work is needed. DCTs pick these up.\n"
    "- HO (HPC Ops): Central 'home' ticket for a node's hardware problem, troubleshooting, and RMA history. "
    "All work, logs, and vendor interactions for that asset should live in a single HO. "
    "Usually auto-created when a node enters triage. "
    "Can also be manually created when a node only exists in FLCC mgmt (never joined k8s). "
    "Must include: clear issue description, node ID/serial, slot, failed component details, PCI lanes, "
    "serials, and required logs (FD logs, AWX bundles) so FRR/vendor can act without back-and-forth. "
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
    "CABLE_REPLACE (copper or fiber cable replacement):\n"
    "1. Verify the bad cable end-to-end by label or by tracing it physically\n"
    "2. Check if the cable has a length label — if yes, grab matching length from stock. "
    "If no label, measure the cable path A-to-Z and grab correct length\n"
    "3. Run the new cable along the same path as the old cable. Leave ends hanging by the ports — do NOT connect yet\n"
    "4. Create and apply labels to both ends of the new cable\n"
    "5. Notify the requester that the new cable is run and ready for swap\n"
    "6. Once requester approves, disconnect the old cable ends and connect the new cable\n"
    "7. Have the requester verify the connection is good\n"
    "8. Once confirmed good, remove the old cable from the cable path to reduce clutter\n"
    "9. Update cable inventory with what was used\n"
    "10. Comment on ticket and move to Verification\n"
    "PPE: Hearing protection required\n\n"
    "PSU_SWAP (power supply replacement):\n"
    "1. Check redundancy FIRST — confirm all other PSUs in the chassis are present and solid green. "
    "Never pull a PSU if doing so would drop below the required PSU count for the chassis.\n"
    "2. Identify the correct PSU — use chassis silkscreen, labels, and/or BMC PSU view to confirm "
    "WHICH module is 'PSU 1' (or whichever is flagged). Do NOT guess.\n"
    "3. Quick cable check before pulling — firmly reseat the power cable at both the PDU and PSU. "
    "If the LED/state normalizes after reseating, document and move to Verification without swapping.\n"
    "4. If the PSU is truly bad — hot-remove only if the chassis supports hot-swap AND redundancy is confirmed. "
    "If unsure, power down node first via BMC.\n"
    "5. Replace with a known-good spare of the SAME MODEL. Note old and new PSU serial numbers.\n"
    "6. Confirm replacement LED goes to steady green (not blinking fault pattern).\n"
    "7. Wait 5-10 min then verify in Grafana PSU Health dashboard — metrics can lag several minutes.\n"
    "8. If new PSU ALSO shows fault — escalate as a suspect slot/backplane issue, not just a bad PSU. "
    "Document both PSU serials, LED behavior, and Grafana status.\n"
    "9. Multiple PSU tickets for the same node at the same time = auto-created by monitoring, one per PSU slot. "
    "Link them in comments and work them together. The root cause is usually one bad PSU or a power event.\n"
    "10. Comment: PSU slot, old serial, new serial, LED state before/after, Grafana confirmation. "
    "Move to Verification.\n\n"
    "PSU TRIAGE NOTES:\n"
    "- 'PSU X <- REMOVE THIS ONE' in the DO panel = monitoring identified PSU X as the suspect. Trust it.\n"
    "- Blinking amber LED = fault. Blinking green = normal power-on. Solid green = healthy.\n"
    "- If IB ports show dark/off while Ethernet is solid, this can be expected depending on node/fabric state. "
    "Log the observation but do NOT chase IB unless there is a specific IB alert or SOP step.\n"
    "- Multiple PSU tickets created within minutes of each other on the same node = likely one event triggered all. "
    "Reference sibling tickets in your comment.\n\n"
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
    "You help DCT technicians understand Jira tickets and troubleshoot node issues.\n\n"
    "Context you receive:\n"
    "- Jira ticket details (key, summary, status, assignee, description, comments)\n"
    "- NetBox device data (rack location, interfaces, IPs, model)\n"
    "- Grafana dashboard links\n\n"
    "Rules:\n"
    "- Be concise. Technicians are on the data center floor.\n"
    "- Use plain English, no jargon unless the ticket uses it.\n"
    "- When summarizing, lead with: what the issue is, what has been done, what the likely next step is.\n"
    "- When troubleshooting, reference specific fields from the ticket context.\n"
    "- If you don't know something, say so. Never invent ticket data.\n"
    "- FORMATTING: This is a TERMINAL — NOT a chat UI. Absolutely NEVER use markdown.\n"
    "  BANNED characters/patterns: ** (bold), * (italic), ## (headers), ``` (code blocks), > (blockquotes), bullet dots (•).\n"
    "  Instead use: dashes (-) for lists, ALL CAPS for emphasis, indentation for structure, and plain text only.\n"
    "  If you use a single asterisk anywhere in your response, you have failed."
    + _AI_DOMAIN_KNOWLEDGE
)

AI_SYSTEM_PROMPT_FINDER = (
    "You are a search assistant for Jira tickets. "
    "The user will describe what they remember about a ticket. Your job is to:\n"
    "1. Extract search keywords from their description.\n"
    "2. After seeing search results, rank them by relevance and explain why each might be the one.\n"
    "Be concise. NEVER use markdown — no **, no *, no ##, no ```, no >. "
    "Plain text ONLY. Dashes (-) for lists, ALL CAPS for emphasis. Any asterisk = failure."
    + _AI_DOMAIN_KNOWLEDGE
)

AI_SYSTEM_PROMPT_CHAT = (
    "You are a helpful assistant for data center technicians. "
    "You have access to the current ticket context if provided. Answer questions naturally. "
    "Be concise — this is a terminal interface. "
    "FORMATTING: NEVER use markdown — no **, no *, no ##, no ```, no >. no bullet dots. "
    "Plain text ONLY. Dashes (-) for lists, ALL CAPS for emphasis, indentation for structure. Any asterisk = failure."
    + _AI_DOMAIN_KNOWLEDGE
)

# ---------------------------------------------------------------------------
# File paths (relative to project root)
# ---------------------------------------------------------------------------
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DH_CONFIG_PATH = os.path.join(_PROJECT_ROOT, "dh_layouts.json")
_USER_STATE_PATH = os.path.join(_PROJECT_ROOT, ".cwhelper_state.json")

# ---------------------------------------------------------------------------
# Cache size limits
# ---------------------------------------------------------------------------
_JQL_CACHE_TTL = 60  # seconds
_ISSUE_CACHE_MAX = 100
_NETBOX_CACHE_MAX = 50
_JQL_CACHE_MAX = 200

# ---------------------------------------------------------------------------
# Queue filters
# ---------------------------------------------------------------------------
# HO statuses that indicate a DO is imminent — used by radar watcher, brief, dashboard.
HO_RADAR_STATUSES = [
    "RMA-initiate",
    "Sent to DCT UC",
    "Sent to DCT RC",
    "Awaiting Parts",
]

# Standard tool/parts kits by procedure type — used by prep brief.
PROCEDURE_KITS = {
    "recable":    ["QSFP-DD optics", "IB cables", "cable labels", "cutsheet"],
    "uncable":    ["cable labels", "zip ties", "velcro"],
    "rma swap":   ["ESD wrist strap", "replacement part (check HO)", "torx set"],
    "psu swap":   ["replacement PSU (same model)", "ESD wrist strap"],
    "gpu reseat": ["ESD wrist strap", "torx set", "thermal paste (if needed)"],
    "dimm swap":  ["replacement DIMM", "ESD wrist strap"],
    "cable":      ["replacement cable (check length)", "cable labels", "optics"],
    "inspection": ["flashlight", "inspection checklist"],
    "default":    ["ESD wrist strap", "flashlight"],
}

QUEUE_FILTERS = {
    "open":         'statusCategory != Done',
    "closed":       'status = "Closed"',
    "verification": 'status = "Verification"',
    "in progress":  'status = "In Progress"',
    "waiting":      'status = "Waiting For Support"',
    "radar":        'status in ("RMA-initiate", "Sent to DCT UC", "Sent to DCT RC", "Awaiting Parts")',
    "triage":       'status = "Awaiting Triage"',
    "cust verify":  'status = "Customer Verification"',
    "all":          None,   # no status filter
}

# ---------------------------------------------------------------------------
# Notification threshold
# ---------------------------------------------------------------------------
STALE_UNASSIGNED_HOURS = 2

# ---------------------------------------------------------------------------
# Default persistent state shape
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
    "walkthrough_notes": [],
    "walkthrough_session": None,
    "walkthrough_carryover": [],
    "walkthrough_checklist": {},
    "walkthrough_history": [],   # rolling list of past session dicts
    "features": {},              # persisted feature toggles (overrides defaults)
}

# ---------------------------------------------------------------------------
# Feature flag registry
# ---------------------------------------------------------------------------
# Each feature has: label, cli_cmd (for CLI gating), menu_keys (for TUI gating),
# deps (informational), default (initial state for new installs).
# Only "ticket_lookup" starts enabled — all others disabled until tested.

_FEATURE_REGISTRY = {
    "ticket_lookup":   {"label": "Ticket lookup",              "cli_cmd": None,             "menu_keys": [],         "deps": ["jira", "netbox"], "default": True},
    "queue":           {"label": "Queue browser",              "cli_cmd": "queue",          "menu_keys": ["1"],      "deps": ["jira"],           "default": False},
    "my_tickets":      {"label": "My tickets",                 "cli_cmd": None,             "menu_keys": ["2"],      "deps": ["jira"],           "default": False},
    "node_history":    {"label": "Node history",               "cli_cmd": "history",        "menu_keys": [],         "deps": ["jira"],           "default": False},
    "shift_brief":     {"label": "Shift brief",               "cli_cmd": "brief",          "menu_keys": ["b"],      "deps": ["jira", "ai"],     "default": False},
    "verify":          {"label": "Verification flows",         "cli_cmd": "verify",         "menu_keys": [],         "deps": ["jira", "netbox"], "default": False},
    "watcher":         {"label": "Queue watcher",              "cli_cmd": "watch",          "menu_keys": ["3"],      "deps": ["jira"],           "default": False},
    "rack_report":     {"label": "Rack report",               "cli_cmd": "rack-report",    "menu_keys": ["r"],      "deps": ["jira"],           "default": False},
    "ibtrace":         {"label": "IB trace",                   "cli_cmd": "ibtrace",        "menu_keys": [],         "deps": ["ib_topology"],    "default": False},
    "learn":           {"label": "Code quiz",                  "cli_cmd": "learn",          "menu_keys": ["L"],      "deps": [],                 "default": False},
    "rack_map":        {"label": "Rack map",                   "cli_cmd": None,             "menu_keys": ["4"],      "deps": ["netbox"],         "default": False},
    "bookmarks":       {"label": "Bookmarks",                  "cli_cmd": None,             "menu_keys": ["5"],      "deps": ["jira"],           "default": False},
    "bulk_start":      {"label": "Bulk start tickets",         "cli_cmd": None,             "menu_keys": ["p", "P"], "deps": ["jira"],           "default": False},
    "activity":        {"label": "Activity log",               "cli_cmd": None,             "menu_keys": ["l"],      "deps": ["jira"],           "default": False},
    "walkthrough":     {"label": "Walkthrough",                "cli_cmd": None,             "menu_keys": ["w"],      "deps": ["jira", "netbox"], "default": False},
    "weekend_assign":  {"label": "Weekend auto-assign",        "cli_cmd": "weekend-assign", "menu_keys": [],         "deps": ["jira"],           "default": False},
    "ai_chat":         {"label": "AI chat / ticket finder",    "cli_cmd": None,             "menu_keys": ["ai"],     "deps": ["ai"],             "default": False},
}

# Runtime feature state — populated by _load_features() at startup, checked everywhere.
FEATURES: dict[str, bool] = {k: v["default"] for k, v in _FEATURE_REGISTRY.items()}


def _is_feature_enabled(feature_id: str) -> bool:
    """Check if a feature is enabled. Returns False for unknown features."""
    return FEATURES.get(feature_id, False)


def _load_features(state: dict) -> None:
    """Populate FEATURES from persisted state, falling back to registry defaults."""
    saved = state.get("features", {})
    for fid, meta in _FEATURE_REGISTRY.items():
        FEATURES[fid] = saved.get(fid, meta["default"])


def _save_features(state: dict) -> dict:
    """Write current FEATURES into state dict. Caller must call _save_user_state()."""
    state["features"] = dict(FEATURES)
    return state


def _enabled_menu_keys() -> set[str]:
    """Return set of menu keys whose feature is enabled."""
    keys: set[str] = set()
    for fid, meta in _FEATURE_REGISTRY.items():
        if FEATURES.get(fid, False):
            keys.update(meta.get("menu_keys", []))
    return keys


# ---------------------------------------------------------------------------
# Mutable runtime globals (shared across modules)
# ---------------------------------------------------------------------------
import queue as queue_mod  # noqa: E402
import threading  # noqa: E402
try:
    import requests as _requests_mod
except ImportError:
    _requests_mod = None

# HTTP session (reuses TCP connections via keep-alive)
_session = _requests_mod.Session() if _requests_mod else None

# Thread pool for parallel API calls
from concurrent.futures import ThreadPoolExecutor  # noqa: E402
_executor = ThreadPoolExecutor(max_workers=3)

# In-memory caches
_issue_cache: dict[str, dict] = {}
_netbox_cache: dict[str, dict] = {}
_jql_cache: dict[str, tuple[float, list]] = {}

# Animation toggle
_ANIMATE = os.environ.get("CWHELPER_ANIMATE", "1") != "0"
_VISUAL_MAPS = os.environ.get("CWHELPER_VISUAL_MAPS", "1") != "0"

# AI toggle
_AI_ENABLED = True

# ntfy.sh push notifications
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
_NTFY_ENABLED = True

# User identity (lazy-loaded)
_my_account_id: str | None = None
_my_display_name: str | None = None

# Cached Jira link type name for "Relates" (discovered on first use)
_relates_link_type: str | None = None

# Background watcher state
_watcher_thread: threading.Thread | None = None
_watcher_stop_event = threading.Event()
_watcher_queue: queue_mod.Queue = queue_mod.Queue()
_watcher_site: str = ""
_watcher_project: str = ""
_watcher_interval: int = 45

# Notification alert tracking
_ntfy_alerted: set = set()

# HO Radar watcher state (tracks HO tickets in pre-DO statuses)
_radar_thread: threading.Thread | None = None
_radar_stop_event = threading.Event()
_radar_queue: queue_mod.Queue = queue_mod.Queue()
_radar_known_keys: dict[str, dict] = {}  # HO key -> last-fetched issue dict
_radar_interval: int = 120
