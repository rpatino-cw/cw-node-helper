"""Learn engine — AST code reader, static question bank, and scoring."""
from __future__ import annotations

import ast
import datetime
import json
import os
import random
from pathlib import Path

from cwhelper.config import _PROJECT_ROOT

__all__ = [
    "_build_code_map",
    "_get_static_questions",
    "_get_module_names",
    "_load_learn_state",
    "_save_learn_state",
    "_score_answer",
    "_rank_from_xp",
    "_DEFAULT_LEARN_STATE",
]

# ---------------------------------------------------------------------------
# Learn state persistence
# ---------------------------------------------------------------------------

_LEARN_STATE_PATH = os.path.join(_PROJECT_ROOT, ".cwhelper_learn.json")

_DEFAULT_LEARN_STATE = {
    "version": 1,
    "total_xp": 0,
    "streak": 0,
    "best_streak": 0,
    "games_played": 0,
    "modules": {},        # module_name -> {"correct": N, "total": N, "last_played": iso}
    "history": [],        # last 20 game results
}

_RANKS = [
    (0,   "Intern"),
    (50,  "Junior DCT"),
    (150, "DCT"),
    (300, "Senior DCT"),
    (500, "Lead DCT"),
    (750, "Maintainer"),
]


def _rank_from_xp(xp: int) -> str:
    """Return rank title based on XP."""
    rank = _RANKS[0][1]
    for threshold, title in _RANKS:
        if xp >= threshold:
            rank = title
    return rank


def _load_learn_state() -> dict:
    """Load learn state from JSON file."""
    if os.path.exists(_LEARN_STATE_PATH):
        try:
            with open(_LEARN_STATE_PATH) as f:
                data = json.load(f)
            for k, v in _DEFAULT_LEARN_STATE.items():
                data.setdefault(k, v if not isinstance(v, (list, dict)) else type(v)())
            return data
        except (json.JSONDecodeError, OSError):
            pass
    return {k: (v if not isinstance(v, (list, dict)) else type(v)()) for k, v in _DEFAULT_LEARN_STATE.items()}


def _save_learn_state(state: dict):
    """Persist learn state to JSON file."""
    state["version"] = 1
    with open(_LEARN_STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)
    try:
        os.chmod(_LEARN_STATE_PATH, 0o600)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# AST Code Map Builder
# ---------------------------------------------------------------------------

def _build_code_map(module_filter: str | None = None) -> dict:
    """Parse cwhelper source files and build a lightweight code map.

    Returns dict:
        {
            "modules": {
                "services/ai.py": {
                    "path": "cwhelper/services/ai.py",
                    "functions": [{"name": "_ai_chat", "args": [...], "lines": (10, 45), "docstring": "..."}],
                    "imports": ["os", "json", ...],
                    "classes": [],
                    "line_count": 874,
                },
                ...
            },
            "total_functions": N,
            "total_lines": N,
        }
    """
    pkg_root = os.path.join(_PROJECT_ROOT, "cwhelper")
    code_map = {"modules": {}, "total_functions": 0, "total_lines": 0}

    for py_file in sorted(Path(pkg_root).rglob("*.py")):
        rel = str(py_file.relative_to(_PROJECT_ROOT))
        short = str(py_file.relative_to(pkg_root))

        if module_filter and module_filter not in short:
            continue

        try:
            source = py_file.read_text()
            tree = ast.parse(source, filename=str(py_file))
        except (SyntaxError, OSError):
            continue

        line_count = len(source.splitlines())
        functions = []
        classes = []
        imports = []

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                args = [a.arg for a in node.args.args]
                doc = ast.get_docstring(node) or ""
                functions.append({
                    "name": node.name,
                    "args": args,
                    "lines": (node.lineno, node.end_lineno or node.lineno),
                    "docstring": doc[:120] if doc else "",
                })
            elif isinstance(node, ast.ClassDef):
                classes.append(node.name)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imports.append(alias.name)
                elif node.module:
                    imports.append(node.module)

        code_map["modules"][short] = {
            "path": rel,
            "functions": functions,
            "imports": list(set(imports)),
            "classes": classes,
            "line_count": line_count,
        }
        code_map["total_functions"] += len(functions)
        code_map["total_lines"] += line_count

    return code_map


def _get_module_names() -> list[str]:
    """Return list of cwhelper module short names (e.g. 'services/ai.py')."""
    code_map = _build_code_map()
    return sorted(code_map["modules"].keys())


# ---------------------------------------------------------------------------
# Static Question Bank
# ---------------------------------------------------------------------------

# Each question: {
#   "id": unique str,
#   "module": module short name or "architecture",
#   "question": the question text,
#   "choices": ["A", "B", "C", "D"],
#   "answer": 0-3 (index into choices),
#   "explanation": why the correct answer is correct,
#   "difficulty": 1-3 (1=easy, 2=medium, 3=hard),
# }

_STATIC_QUESTIONS: list[dict] = [
    # --- config.py ---
    {
        "id": "cfg-01",
        "module": "config.py",
        "question": "What is the ONLY runtime dependency listed in requirements.txt?",
        "choices": ["rich", "requests", "click", "openai"],
        "answer": 1,
        "explanation": "requests>=2.28.0 is the sole runtime dependency. Rich, openai, and Pillow are optional.",
        "difficulty": 1,
    },
    {
        "id": "cfg-02",
        "module": "config.py",
        "question": "Where does cwhelper look for API keys FIRST before checking the local .env?",
        "choices": ["Environment variables", "~/.config/keys/global.env", "/etc/cwhelper/keys", "~/.cwhelper/keys"],
        "answer": 1,
        "explanation": "_load_dotenv() loads the global vault (~/.config/keys/global.env) first, then the local .env overrides.",
        "difficulty": 2,
    },
    {
        "id": "cfg-03",
        "module": "config.py",
        "question": "What is the default AI model used when OPENAI_BASE_URL is not set?",
        "choices": ["claude-3-opus", "gpt-4o", "gpt-3.5-turbo", "claude-3-haiku"],
        "answer": 1,
        "explanation": "AI_MODEL defaults to os.environ.get('AI_MODEL', 'gpt-4o').",
        "difficulty": 1,
    },
    {
        "id": "cfg-04",
        "module": "config.py",
        "question": "What custom field ID maps to 'service_tag' in CUSTOM_FIELDS?",
        "choices": ["customfield_10207", "customfield_10193", "customfield_10192", "customfield_10194"],
        "answer": 1,
        "explanation": "customfield_10193 maps to service_tag (e.g. '10NQ724').",
        "difficulty": 2,
    },

    # --- cli.py ---
    {
        "id": "cli-01",
        "module": "cli.py",
        "question": "What happens when you run `cwhelper` with no arguments?",
        "choices": ["Prints help", "Launches interactive menu", "Exits with error", "Runs queue browser"],
        "answer": 1,
        "explanation": "No args → _interactive_menu() is called directly.",
        "difficulty": 1,
    },
    {
        "id": "cli-02",
        "module": "cli.py",
        "question": "How does cli.py dispatch subcommands?",
        "choices": ["argparse subparsers", "if/elif chain on raw_args[0]", "click decorators", "dictionary dispatch"],
        "answer": 1,
        "explanation": "cli.py uses a simple if/elif chain checking raw_args[0] against known subcommands.",
        "difficulty": 1,
    },

    # --- clients/jira.py ---
    {
        "id": "jira-01",
        "module": "clients/jira.py",
        "question": "What authentication method does cwhelper use for Jira Cloud?",
        "choices": ["OAuth 2.0", "API token + email (Basic Auth)", "Session cookies", "Personal access token"],
        "answer": 1,
        "explanation": "Jira Cloud REST uses email + API token via HTTP Basic Auth.",
        "difficulty": 1,
    },
    {
        "id": "jira-02",
        "module": "clients/jira.py",
        "question": "What env vars hold the Jira credentials?",
        "choices": ["JIRA_USER + JIRA_PASS", "JIRA_EMAIL + JIRA_API_TOKEN", "JIRA_KEY + JIRA_SECRET", "ATLASSIAN_TOKEN"],
        "answer": 1,
        "explanation": "JIRA_EMAIL and JIRA_API_TOKEN are the required env vars.",
        "difficulty": 1,
    },

    # --- clients/netbox.py ---
    {
        "id": "nb-01",
        "module": "clients/netbox.py",
        "question": "What happens to cwhelper when NetBox is unreachable?",
        "choices": ["App crashes", "Jira-only mode (graceful degradation)", "Retries 10 times", "Switches to mock data"],
        "answer": 1,
        "explanation": "Graceful degradation — NetBox down = Jira-only mode, no crash.",
        "difficulty": 1,
    },

    # --- services/context.py ---
    {
        "id": "ctx-01",
        "module": "services/context.py",
        "question": "What is the central data structure passed through all cwhelper functions?",
        "choices": ["A Ticket object", "A ctx dict", "A pandas DataFrame", "A NamedTuple"],
        "answer": 1,
        "explanation": "The ctx dict is the universal data carrier — accumulated with Jira, NetBox, and Grafana enrichments.",
        "difficulty": 1,
    },
    {
        "id": "ctx-02",
        "module": "services/context.py",
        "question": "What does _build_context() combine into the ctx dict?",
        "choices": [
            "Only Jira fields",
            "Jira + NetBox + Grafana enrichments",
            "Jira + Slack messages",
            "NetBox + Kubernetes status"
        ],
        "answer": 1,
        "explanation": "_build_context() fetches Jira issue data, enriches with NetBox device info, and adds Grafana URL generation.",
        "difficulty": 2,
    },

    # --- services/queue.py ---
    {
        "id": "q-01",
        "module": "services/queue.py",
        "question": "What does 'stale verification' mean in cwhelper?",
        "choices": [
            "Tickets that fail automated checks",
            "Tickets stuck in Verification status for >48 hours",
            "Tickets with missing custom fields",
            "Tickets assigned to inactive users"
        ],
        "answer": 1,
        "explanation": "Stale verification = tickets in Verification status older than 48 hours (configurable).",
        "difficulty": 2,
    },

    # --- services/rack.py ---
    {
        "id": "rack-01",
        "module": "services/rack.py",
        "question": "What layout pattern does the DH map use for rack numbering?",
        "choices": ["Linear rows", "Serpentine (zig-zag)", "Spiral", "Random hash"],
        "answer": 1,
        "explanation": "DH maps use a serpentine (zig-zag) pattern — odd rows left-to-right, even rows right-to-left.",
        "difficulty": 2,
    },

    # --- services/walkthrough.py ---
    {
        "id": "walk-01",
        "module": "services/walkthrough.py",
        "question": "What is the largest module in cwhelper by line count?",
        "choices": ["services/ai.py", "tui/actions.py", "services/walkthrough.py", "tui/menu.py"],
        "answer": 2,
        "explanation": "walkthrough.py is ~2,739 lines — the most complex module, handling DH tours, notes, RMA, and export.",
        "difficulty": 1,
    },
    {
        "id": "walk-02",
        "module": "services/walkthrough.py",
        "question": "What data source does walkthrough mode use for RMA tracking?",
        "choices": ["Jira custom fields", "Google Sheets", "NetBox API", "Local CSV files"],
        "answer": 1,
        "explanation": "Walkthrough mode fetches RMA tracking data from Google Sheets via clients/gsheets.py.",
        "difficulty": 2,
    },
    {
        "id": "walk-03",
        "module": "services/walkthrough.py",
        "question": "Can you resume a walkthrough session after closing cwhelper?",
        "choices": ["No, sessions are in-memory only", "Yes, via .cwhelper_state.json", "Yes, via a SQLite database", "Only if you export first"],
        "answer": 1,
        "explanation": "Walkthrough sessions persist to .cwhelper_state.json and can be resumed across app restarts.",
        "difficulty": 2,
    },

    # --- services/ai.py ---
    {
        "id": "ai-01",
        "module": "services/ai.py",
        "question": "What does _ai_available() check to determine if AI features can be used?",
        "choices": [
            "Only OPENAI_API_KEY",
            "_HAS_OPENAI import + (AI_BASE_URL or OPENAI_API_KEY)",
            "A health check endpoint",
            "ANTHROPIC_API_KEY"
        ],
        "answer": 1,
        "explanation": "Checks if openai module imported successfully AND either AI_BASE_URL (Ollama) or OPENAI_API_KEY is set.",
        "difficulty": 2,
    },
    {
        "id": "ai-02",
        "module": "services/ai.py",
        "question": "What local AI provider does cwhelper support besides OpenAI?",
        "choices": ["LLaMA.cpp", "Ollama (via OPENAI_BASE_URL)", "vLLM", "Text Generation WebUI"],
        "answer": 1,
        "explanation": "Ollama is supported via the OPENAI_BASE_URL env var — it's OpenAI-compatible, no special API key needed.",
        "difficulty": 2,
    },

    # --- services/watcher.py ---
    {
        "id": "watch-01",
        "module": "services/watcher.py",
        "question": "How many background threads does the watcher system use?",
        "choices": ["1 (shared)", "2 (DO/HO queue + HO radar)", "3 (queue + radar + notifications)", "4"],
        "answer": 1,
        "explanation": "Dual-threaded: one for DO/HO queue polling, one for HO radar (pre-DO awareness).",
        "difficulty": 2,
    },
    {
        "id": "watch-02",
        "module": "services/watcher.py",
        "question": "What is the default polling interval for the queue watcher?",
        "choices": ["60 seconds", "180 seconds", "300 seconds", "600 seconds"],
        "answer": 2,
        "explanation": "Default interval is 300 seconds (5 minutes), configurable via --interval flag.",
        "difficulty": 1,
    },

    # --- state.py ---
    {
        "id": "state-01",
        "module": "state.py",
        "question": "How does _load_user_state() handle forward compatibility when new keys are added?",
        "choices": [
            "It doesn't — old state files break",
            "data.setdefault() fills missing keys from _DEFAULT_STATE",
            "It migrates via version-based schema checks",
            "It deletes and recreates the file"
        ],
        "answer": 1,
        "explanation": "Uses setdefault() to fill any missing keys from _DEFAULT_STATE — new keys added automatically.",
        "difficulty": 2,
    },
    {
        "id": "state-02",
        "module": "state.py",
        "question": "What file permissions does cwhelper set on state files after writing?",
        "choices": ["0o644", "0o600", "0o755", "No chmod"],
        "answer": 1,
        "explanation": "chmod 0o600 — owner read/write only. Protects state from other users on shared machines.",
        "difficulty": 2,
    },

    # --- cache.py ---
    {
        "id": "cache-01",
        "module": "cache.py",
        "question": "What is the TTL (time-to-live) for cached JQL query results?",
        "choices": ["30 seconds", "60 seconds", "300 seconds", "No expiry"],
        "answer": 1,
        "explanation": "_JQL_CACHE_TTL = 60 seconds. Prevents hammering Jira with repeated identical queries.",
        "difficulty": 2,
    },
    {
        "id": "cache-02",
        "module": "cache.py",
        "question": "What eviction strategy do the in-memory caches use?",
        "choices": ["LRU with max size", "FIFO", "Time-based only", "No eviction"],
        "answer": 0,
        "explanation": "LRU (Least Recently Used) with max entries: 100 issues, 50 NetBox, 200 JQL.",
        "difficulty": 3,
    },

    # --- tui/display.py ---
    {
        "id": "tui-01",
        "module": "tui/display.py",
        "question": "What 3 questions does the ticket detail header answer (in order)?",
        "choices": [
            "Who → When → What",
            "Where → What to do → Which device",
            "Status → Priority → Assignee",
            "Key → Summary → Site"
        ],
        "answer": 1,
        "explanation": "The header is designed for DCT workflow: Where (rack location) → What to do (summary) → Which device (hostname/tag).",
        "difficulty": 2,
    },

    # --- tui/actions.py ---
    {
        "id": "act-01",
        "module": "tui/actions.py",
        "question": "What is the main dispatch mechanism in tui/actions.py?",
        "choices": ["Switch statement", "Hotkey-based if/elif dispatch", "Command pattern objects", "Event system"],
        "answer": 1,
        "explanation": "actions.py uses a hotkey-driven if/elif dispatch loop for ticket detail actions.",
        "difficulty": 1,
    },

    # --- tui/rich_console.py ---
    {
        "id": "rich-01",
        "module": "tui/rich_console.py",
        "question": "What does _rich_status('in progress') return?",
        "choices": [
            "('green', '●')",
            "('yellow', '●')",
            "('blue', '●')",
            "('dim', '○')"
        ],
        "answer": 1,
        "explanation": "In progress maps to yellow with a filled dot (●).",
        "difficulty": 2,
    },

    # --- tui/menu.py ---
    {
        "id": "menu-01",
        "module": "tui/menu.py",
        "question": "What key do you press to open the last viewed ticket from the main menu?",
        "choices": ["L", "0", "R", "Enter"],
        "answer": 1,
        "explanation": "Pressing 0 re-opens state['last_ticket'] — the most recently viewed ticket.",
        "difficulty": 1,
    },
    {
        "id": "menu-02",
        "module": "tui/menu.py",
        "question": "What are the bookmark shortcut keys in the main menu?",
        "choices": ["1-5", "a-e", "F1-F5", "Ctrl+1 through Ctrl+5"],
        "answer": 1,
        "explanation": "Bookmarks use letters a through e (up to 5 bookmarks).",
        "difficulty": 1,
    },

    # --- Architecture ---
    {
        "id": "arch-01",
        "module": "architecture",
        "question": "What is the correct layer order from top to bottom in cwhelper?",
        "choices": [
            "clients → services → tui → cli",
            "cli → tui → services → clients",
            "services → clients → tui → cli",
            "tui → cli → clients → services"
        ],
        "answer": 1,
        "explanation": "cli.py → tui/ (menu, display) → services/ (business logic) → clients/ (API calls).",
        "difficulty": 1,
    },
    {
        "id": "arch-02",
        "module": "architecture",
        "question": "Which layer are clients/ modules NOT allowed to import from?",
        "choices": [
            "Only config",
            "tui/ and services/ (they are leaf layer)",
            "Only other clients",
            "No restrictions"
        ],
        "answer": 1,
        "explanation": "clients/ is the leaf layer — no imports from tui/ or services/. Only config and stdlib.",
        "difficulty": 2,
    },
    {
        "id": "arch-03",
        "module": "architecture",
        "question": "What naming convention do ALL cwhelper functions follow?",
        "choices": [
            "camelCase",
            "_private_prefix (underscore prefix)",
            "UPPER_CASE",
            "No convention"
        ],
        "answer": 1,
        "explanation": "All functions use _private_prefix naming — consistent across the entire codebase.",
        "difficulty": 1,
    },
    {
        "id": "arch-04",
        "module": "architecture",
        "question": "How many unit tests does cwhelper have?",
        "choices": ["24", "50", "74", "120"],
        "answer": 2,
        "explanation": "74 tests in test_integrity.py — all API calls mocked, no real network requests.",
        "difficulty": 1,
    },
    {
        "id": "arch-05",
        "module": "architecture",
        "question": "What retry strategy does _request_with_retry() use?",
        "choices": [
            "Exponential backoff with jitter",
            "2 retries with 1s/2s fixed backoff",
            "Infinite retries with 5s delay",
            "No retry — single attempt"
        ],
        "answer": 1,
        "explanation": "2 retries with fixed 1s then 2s backoff. Simple and predictable for DCT terminal use.",
        "difficulty": 3,
    },

    # --- services/brief.py ---
    {
        "id": "brief-01",
        "module": "services/brief.py",
        "question": "What does the shift brief command generate?",
        "choices": [
            "A JSON export of all tickets",
            "An AI-prioritized summary of what to work on first",
            "A PDF report of completed work",
            "A Slack message to the team"
        ],
        "answer": 1,
        "explanation": "Shift brief uses AI to analyze the queue and generate a prioritized summary for the DCT's shift.",
        "difficulty": 1,
    },

    # --- services/radar.py ---
    {
        "id": "radar-01",
        "module": "services/radar.py",
        "question": "What does the HO radar show?",
        "choices": [
            "Hardware failures across all sites",
            "RMA tickets in pre-DO states (awaiting DCT pickup)",
            "Network topology issues",
            "Server temperature readings"
        ],
        "answer": 1,
        "explanation": "HO radar tracks RMA tickets in states like 'RMA-initiate' or 'Sent to DCT' — pre-DO awareness.",
        "difficulty": 2,
    },

    # --- services/notifications.py ---
    {
        "id": "notif-01",
        "module": "services/notifications.py",
        "question": "What push notification service does cwhelper use?",
        "choices": ["Pushover", "ntfy.sh", "Slack webhooks", "macOS native"],
        "answer": 1,
        "explanation": "ntfy.sh — simple HTTP POST to send push notifications. Configured via NTFY_TOPIC env var.",
        "difficulty": 1,
    },

    # --- clients/grafana.py ---
    {
        "id": "graf-01",
        "module": "clients/grafana.py",
        "question": "Does cwhelper make actual Grafana API calls?",
        "choices": [
            "Yes, it queries metrics",
            "No, it only generates dashboard URLs with pre-filled variables",
            "Yes, it pulls alert status",
            "It uses Grafana's embed API"
        ],
        "answer": 1,
        "explanation": "grafana.py is URL generation only (~82 lines) — no API calls, just builds parameterized dashboard links.",
        "difficulty": 2,
    },

    # --- services/verify.py ---
    {
        "id": "ver-01",
        "module": "services/verify.py",
        "question": "What does 'self-service verification' let a DCT do?",
        "choices": [
            "Auto-close tickets",
            "Check their own work against required field validations",
            "Bypass manager approval",
            "Run automated hardware diagnostics"
        ],
        "answer": 1,
        "explanation": "DCTs can verify their own tickets — checks required fields are filled before status transition.",
        "difficulty": 2,
    },

    # --- services/session_log.py ---
    {
        "id": "sess-01",
        "module": "services/session_log.py",
        "question": "What format does the session log use for export to Slack?",
        "choices": [
            "JSON dump",
            "Formatted table copied to clipboard",
            "CSV attachment",
            "Markdown file"
        ],
        "answer": 1,
        "explanation": "Session log exports as a formatted table copied to the clipboard for pasting into Slack.",
        "difficulty": 2,
    },

    # --- Deeper architecture ---
    {
        "id": "arch-06",
        "module": "architecture",
        "question": "What projects does cwhelper search when looking up by serial/hostname?",
        "choices": [
            "DO only",
            "DO, HO, SDA",
            "All Jira projects",
            "DO and HO"
        ],
        "answer": 1,
        "explanation": "SEARCH_PROJECTS = ['DO', 'HO', 'SDA'] — covers ops, hardware ops, and service desk.",
        "difficulty": 2,
    },
    {
        "id": "arch-07",
        "module": "architecture",
        "question": "What display library does cwhelper use for all new TUI code?",
        "choices": ["curses", "Rich", "blessed", "prompt_toolkit"],
        "answer": 1,
        "explanation": "Python Rich via a shared Console instance in tui/rich_console.py. No raw ANSI for new code.",
        "difficulty": 1,
    },
]


def _get_static_questions(
    module: str | None = None,
    difficulty: int | None = None,
    count: int | None = None,
    exclude_ids: set | None = None,
) -> list[dict]:
    """Return filtered questions from the static bank.

    Args:
        module: Filter by module name (e.g. 'services/ai.py', 'architecture').
        difficulty: Filter by difficulty (1=easy, 2=medium, 3=hard).
        count: Max number of questions to return (random sample).
        exclude_ids: Set of question IDs to exclude (already asked).
    """
    pool = list(_STATIC_QUESTIONS)

    if module:
        pool = [q for q in pool if q["module"] == module]
    if difficulty:
        pool = [q for q in pool if q["difficulty"] == difficulty]
    if exclude_ids:
        pool = [q for q in pool if q["id"] not in exclude_ids]

    if count and len(pool) > count:
        pool = random.sample(pool, count)
    elif count is None:
        random.shuffle(pool)

    return pool


def _score_answer(state: dict, question: dict, user_answer: int) -> tuple[bool, dict]:
    """Score an answer and update learn state.

    Returns (correct: bool, updated_state: dict).
    """
    correct = user_answer == question["answer"]
    module = question["module"]

    # Update module stats
    if module not in state["modules"]:
        state["modules"][module] = {"correct": 0, "total": 0, "last_played": ""}

    state["modules"][module]["total"] += 1
    state["modules"][module]["last_played"] = datetime.datetime.utcnow().isoformat() + "Z"

    if correct:
        state["modules"][module]["correct"] += 1
        state["total_xp"] += question.get("difficulty", 1) * 10
        state["streak"] += 1
        if state["streak"] > state["best_streak"]:
            state["best_streak"] = state["streak"]
    else:
        state["streak"] = 0

    return correct, state
