"""CWHelper TUI — Cronboard skeleton + Bagels skin.

Launch: cwhelper tui

Ticket management dashboard with KPI cards, color-coded queue table,
detail panel with ticket actions, and command input.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import partial

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import DataTable, Footer, Header, Input, RichLog, Static
from textual.message import Message

from rich.text import Text

# ---------------------------------------------------------------------------
# Tokyo Night palette
# ---------------------------------------------------------------------------
TN = {
    "bg": "#1a1b26", "bg_dark": "#16161e", "fg": "#c0caf5",
    "blue": "#7aa2f7", "cyan": "#7dcfff", "green": "#9ece6a",
    "magenta": "#bb9af7", "red": "#f7768e", "yellow": "#e0af68",
    "comment": "#565f89", "border": "#3b4261", "surface": "#1e2030",
}

_STATUS_STYLE = {
    "open":              ("\u25cf", TN["red"]),
    "to do":             ("\u25cf", TN["red"]),
    "new":               ("\u25cf", TN["red"]),
    "in progress":       ("\u25cf", TN["yellow"]),
    "verification":      ("\u25cf", TN["blue"]),
    "on hold":           ("\u25cf", TN["magenta"]),
    "waiting for support": ("\u25cf", TN["magenta"]),
    "closed":            ("\u25cb", TN["green"]),
    "done":              ("\u25cb", TN["green"]),
    "resolved":          ("\u25cb", TN["green"]),
}


def _status_badge(status: str) -> Text:
    key = status.lower()
    dot, color = _STATUS_STYLE.get(key, ("\u25cf", TN["comment"]))
    t = Text()
    t.append(f"{dot} ", style=f"bold {color}")
    t.append(status[:12], style=color)
    return t


def _age_text(secs: int) -> Text:
    if secs <= 0:
        return Text("\u2014", style=TN["comment"])
    if secs < 3600:
        label = f"{secs // 60}m"
        color = TN["green"]
    elif secs < 86400:
        label = f"{secs // 3600}h"
        color = TN["green"] if secs < 43200 else TN["yellow"]
    else:
        days = secs // 86400
        label = f"{days}d"
        color = TN["red"] if days >= 5 else TN["yellow"] if days >= 1 else TN["green"]
    return Text(label, style=f"bold {color}")


def _parse_ts(ts: str) -> int:
    if not ts:
        return 0
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
    except Exception:
        return 0


def _unwrap(val) -> str:
    if isinstance(val, dict):
        return val.get("value", "") or val.get("name", "")
    return str(val) if val else ""


def _short_loc(loc: str) -> str:
    m = re.search(r'\.R(\d+)\.RU?(\d+)', loc)
    if m:
        return f"R{m.group(1)}\u00b7U{m.group(2)}"
    m2 = re.search(r'R(\d+)', loc)
    if m2:
        return f"R{m2.group(1)}"
    return loc[-12:] if loc else "\u2014"


# ---------------------------------------------------------------------------
# Thread pool + sync fetchers
# ---------------------------------------------------------------------------
_pool = ThreadPoolExecutor(max_workers=2)


def _check_credentials() -> tuple[str, str] | None:
    """Check if credentials are available. Returns (email, token) or None."""
    email = os.environ.get("JIRA_EMAIL", "").strip()
    token = os.environ.get("JIRA_API_TOKEN", "").strip()
    if email and token:
        return email, token
    # Try loading .env
    try:
        from cwhelper.config import _load_dotenv
        _load_dotenv()
        email = os.environ.get("JIRA_EMAIL", "").strip()
        token = os.environ.get("JIRA_API_TOKEN", "").strip()
        if email and token:
            return email, token
    except Exception:
        pass
    return None


def _check_jira_health(email: str, token: str) -> bool:
    try:
        from cwhelper.clients.jira import _jira_health_check
        return _jira_health_check(email, token)
    except Exception:
        return False


def _fetch_queue_sync(site: str = "", status_filter: str = "open",
                      project: str = "DO", mine_only: bool = False,
                      limit: int = 30) -> list[dict] | str:
    """Returns issues list on success, error string on failure."""
    try:
        from cwhelper.clients.jira import _get_credentials
        from cwhelper.services.search import _search_queue
        email, token = _get_credentials()
        return _search_queue(site, email, token, mine_only=mine_only,
                            limit=limit, status_filter=status_filter,
                            project=project, use_cache=False)
    except SystemExit:
        return "NO_CREDENTIALS"
    except Exception as e:
        return f"ERROR: {e}"


def _fetch_ticket_sync(key: str) -> dict:
    try:
        from cwhelper.services.context import get_node_context
        return get_node_context(key, quiet=True)
    except Exception as e:
        return {"error": str(e)}


def _execute_transition_sync(key: str, action: str) -> str:
    """Run a ticket transition. Returns success/error message."""
    try:
        from cwhelper.clients.jira import _get_credentials, _execute_transition
        email, token = _get_credentials()
        ctx = {"issue_key": key, "_transitions": None}
        if _execute_transition(ctx, action, email, token):
            return f"OK: {key} \u2192 {action}"
        return f"FAIL: could not {action} {key}"
    except Exception as e:
        return f"ERROR: {e}"


# ---------------------------------------------------------------------------
# Help Screen (modal)
# ---------------------------------------------------------------------------

class HelpScreen(ModalScreen):
    """Full-screen help overlay."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("question_mark", "dismiss", "Close"),
        Binding("q", "dismiss", "Close"),
    ]

    DEFAULT_CSS = f"""
    HelpScreen {{
        align: center middle;
    }}
    #help-content {{
        width: 70;
        max-width: 80%;
        height: auto;
        max-height: 85%;
        background: {TN['bg']};
        border: round {TN['blue']};
        padding: 1 2;
        overflow-y: auto;
    }}
    """

    def compose(self) -> ComposeResult:
        yield Static(self._build_help(), id="help-content")

    def _build_help(self) -> Text:
        t = Text()
        t.append("  CWHELPER TUI \u2014 Help\n\n", style=f"bold {TN['blue']}")

        sections = [
            ("NAVIGATION", [
                ("j / k", "Move up / down in ticket table"),
                ("Enter", "Load full ticket detail"),
                ("g / G", "Jump to top / bottom"),
                ("Tab", "Cycle focus between panels"),
                ("Esc", "Return focus to ticket table"),
            ]),
            ("SHORTCUTS", [
                ("r", "Refresh queue"),
                ("m", "My tickets (assigned to me)"),
                ("f", "Filter by site"),
                ("/", "Focus command input"),
                ("?", "This help screen"),
                ("q", "Quit"),
            ]),
            ("TICKET ACTIONS (in detail panel)", [
                ("s", "Start work (grab + In Progress)"),
                ("v", "Move to Verification"),
                ("y", "Put On Hold"),
                ("c", "Close ticket"),
            ]),
            ("COMMANDS (type in command bar)", [
                ("queue", "Load open ticket queue"),
                ("queue --mine", "My assigned tickets"),
                ("queue -s US-EAST-03", "Filter by site"),
                ("queue -p HO", "HO project queue"),
                ("DO-12345", "Look up a specific ticket"),
                ("history <device>", "Ticket history for a device"),
                ("config", "Show feature config"),
                ("doctor", "Health check"),
            ]),
        ]

        for title, items in sections:
            t.append(f"  {title}\n", style=f"bold {TN['yellow']}")
            for key, desc in items:
                t.append(f"    {key:<28}", style=f"bold {TN['cyan']}")
                t.append(f"{desc}\n", style=TN["fg"])
            t.append("\n")

        t.append("  Press Esc or ? to close\n", style=f"italic {TN['comment']}")
        return t


# ---------------------------------------------------------------------------
# Site Filter Screen (modal)
# ---------------------------------------------------------------------------

class SiteFilterScreen(ModalScreen[str]):
    """Quick site picker from KNOWN_SITES."""

    BINDINGS = [
        Binding("escape", "dismiss_empty", "Cancel"),
    ]

    DEFAULT_CSS = f"""
    SiteFilterScreen {{
        align: center middle;
    }}
    #site-list {{
        width: 40;
        max-height: 70%;
        background: {TN['bg']};
        border: round {TN['blue']};
        padding: 1 2;
        overflow-y: auto;
    }}
    """

    def compose(self) -> ComposeResult:
        yield Static(self._build_list(), id="site-list")

    def _build_list(self) -> Text:
        t = Text()
        t.append("  Filter by Site\n\n", style=f"bold {TN['blue']}")
        t.append("  0  ", style=f"bold {TN['cyan']}")
        t.append("All sites (clear filter)\n", style=TN["fg"])

        try:
            from cwhelper.config import KNOWN_SITES
            sites = KNOWN_SITES
        except Exception:
            sites = []

        if sites:
            for i, site in enumerate(sites, 1):
                t.append(f"  {i:<3}", style=f"bold {TN['cyan']}")
                t.append(f"{site}\n", style=TN["fg"])
        else:
            t.append("\n  No sites configured.\n", style=TN["comment"])
            t.append("  Set KNOWN_SITES in .env\n", style=TN["comment"])

        t.append(f"\n  Type number + Enter, or Esc to cancel\n", style=f"italic {TN['comment']}")
        return t

    def on_key(self, event) -> None:
        key = event.key
        if key == "0":
            self.dismiss("")
            return
        try:
            from cwhelper.config import KNOWN_SITES
            idx = int(key)
            if 1 <= idx <= len(KNOWN_SITES):
                self.dismiss(KNOWN_SITES[idx - 1])
        except (ValueError, ImportError):
            pass

    def action_dismiss_empty(self) -> None:
        self.dismiss("")


# ---------------------------------------------------------------------------
# KPI Bar
# ---------------------------------------------------------------------------

class KPIBar(Static):
    def compose(self) -> ComposeResult:
        yield Static("\u2014", id="kpi-open", classes="kpi-card")
        yield Static("\u2014", id="kpi-unassigned", classes="kpi-card")
        yield Static("\u2014", id="kpi-verify", classes="kpi-card")
        yield Static("\u2014", id="kpi-progress", classes="kpi-card")

    def update_from_issues(self, issues: list[dict]) -> None:
        total = len(issues)
        unassigned = sum(1 for iss in issues if not iss.get("fields", {}).get("assignee"))
        verify = sum(1 for iss in issues if _unwrap(iss.get("fields", {}).get("status", "")).lower() == "verification")
        progress = sum(1 for iss in issues if _unwrap(iss.get("fields", {}).get("status", "")).lower() == "in progress")

        self._set("kpi-open", f"Open  {total}", TN["blue"])
        self._set("kpi-unassigned", f"Unassigned  {unassigned}", TN["red"] if unassigned else TN["comment"])
        self._set("kpi-verify", f"Verify  {verify}", TN["yellow"] if verify else TN["comment"])
        self._set("kpi-progress", f"In Progress  {progress}", TN["green"] if progress else TN["comment"])

    def _set(self, card_id: str, label: str, color: str) -> None:
        try:
            self.query_one(f"#{card_id}", Static).update(Text(label, style=f"bold {color}"))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Ticket Table
# ---------------------------------------------------------------------------

class TicketTable(DataTable):
    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("G", "scroll_end", "Bottom", show=False),
        Binding("g", "scroll_home", "Top", show=False),
    ]

    class TicketSelected(Message):
        def __init__(self, key: str) -> None:
            self.key = key
            super().__init__()

    def __init__(self, **kwargs):
        super().__init__(cursor_type="row", zebra_stripes=True, **kwargs)
        self._issues: list[dict] = []
        self._all_issues: list[dict] = []  # unfiltered copy for search

    def on_mount(self) -> None:
        self.add_columns("STATUS", "TICKET", "DEVICE", "LOC", "AGE", "ASSIGNEE")

    def load_issues(self, issues: list[dict]) -> None:
        self._all_issues = list(issues)
        self._issues = issues
        self._render_rows(issues)

    def filter_issues(self, query: str) -> None:
        """Filter visible rows by query string matching key, tag, hostname, assignee."""
        if not query:
            self._issues = list(self._all_issues)
        else:
            q = query.lower()
            self._issues = [
                iss for iss in self._all_issues
                if q in iss.get("key", "").lower()
                or q in _unwrap(iss.get("fields", {}).get("customfield_10193")).lower()
                or q in _unwrap(iss.get("fields", {}).get("customfield_10192")).lower()
                or q in (iss.get("fields", {}).get("assignee", {}) or {}).get("displayName", "").lower()
                or q in (iss.get("fields", {}).get("summary", "") or "").lower()
            ]
        self._render_rows(self._issues)

    def _render_rows(self, issues: list[dict]) -> None:
        self.clear()
        for iss in issues:
            f = iss.get("fields", {})
            status_name = _unwrap(f.get("status", "?"))
            key = iss.get("key", "?")
            tag = _unwrap(f.get("customfield_10193")) or "\u2014"
            loc = _short_loc(_unwrap(f.get("customfield_10207")) or "")
            age_secs = _parse_ts(f.get("statuscategorychangedate") or f.get("created", ""))
            assignee_obj = f.get("assignee")
            assignee = (assignee_obj.get("displayName", "").split()[0]
                       if isinstance(assignee_obj, dict) else "\u2014")
            self.add_row(
                _status_badge(status_name),
                Text(key, style=f"bold {TN['cyan']}"),
                Text(tag[:16], style=TN["fg"]),
                Text(loc, style=TN["comment"]),
                _age_text(age_secs),
                Text(assignee, style=TN["fg"] if assignee != "\u2014" else TN["comment"]),
                key=key,
            )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        try:
            idx = event.cursor_row
            if 0 <= idx < len(self._issues):
                key = self._issues[idx].get("key", "")
                if key:
                    self.post_message(self.TicketSelected(key))
        except Exception:
            pass

    def get_selected_key(self) -> str | None:
        if self.cursor_row is not None and self._issues:
            try:
                return self._issues[self.cursor_row].get("key")
            except IndexError:
                pass
        return None

    def action_scroll_home(self) -> None:
        self.move_cursor(row=0)

    def action_scroll_end(self) -> None:
        if self._issues:
            self.move_cursor(row=len(self._issues) - 1)


# ---------------------------------------------------------------------------
# Detail Panel with ticket actions
# ---------------------------------------------------------------------------

class DetailPanel(Static):
    _current_key: str | None = None
    _current_data: dict | None = None

    BINDINGS = [
        Binding("s", "ticket_start", "Start", show=False),
        Binding("v", "ticket_verify", "Verify", show=False),
        Binding("y", "ticket_hold", "Hold", show=False),
        Binding("c", "ticket_close", "Close", show=False),
    ]

    class TicketAction(Message):
        def __init__(self, key: str, action: str) -> None:
            self.key = key
            self.action = action
            super().__init__()

    def show_empty(self) -> None:
        t = Text()
        t.append("\n  Select a ticket\n\n", style=f"bold {TN['comment']}")
        t.append("  j/k  ", style=f"bold {TN['blue']}")
        t.append("navigate\n", style=TN["comment"])
        t.append("  Enter ", style=f"bold {TN['blue']}")
        t.append("load detail\n", style=TN["comment"])
        t.append("  m     ", style=f"bold {TN['blue']}")
        t.append("my tickets\n", style=TN["comment"])
        t.append("  f     ", style=f"bold {TN['blue']}")
        t.append("filter by site\n", style=TN["comment"])
        t.append("  r     ", style=f"bold {TN['blue']}")
        t.append("refresh\n", style=TN["comment"])
        t.append("  /     ", style=f"bold {TN['blue']}")
        t.append("command\n", style=TN["comment"])
        t.append("  ?     ", style=f"bold {TN['blue']}")
        t.append("help\n", style=TN["comment"])
        self.update(t)
        self._current_key = None
        self._current_data = None

    def show_loading(self, key: str) -> None:
        self.update(Text(f"\n  Loading {key}...", style=f"italic {TN['comment']}"))
        self._current_key = key

    def show_ticket(self, data: dict) -> None:
        t = Text()
        key = data.get("issue_key", data.get("key", "?"))
        self._current_key = key
        self._current_data = data

        status = data.get("status", "?")
        dot, color = _STATUS_STYLE.get(status.lower(), ("\u25cf", TN["comment"]))

        # Header
        t.append(f"\n  {key}", style=f"bold {TN['blue']}")
        t.append(f"  {dot} {status}\n", style=color)

        # Summary
        summary = data.get("summary", "")
        if summary:
            t.append(f"  {summary}\n", style=f"bold {TN['fg']}")

        # Location
        loc = data.get("rack_location", "")
        if loc:
            t.append(f"\n  {loc}\n", style=TN["fg"])

        # Device
        for label, field in [("Tag", "service_tag"), ("Host", "hostname"),
                             ("Vendor", "vendor"), ("Model", "model")]:
            val = data.get(field, "")
            if val:
                t.append(f"  {label}: ", style=TN["comment"])
                t.append(f"{val}\n", style=TN["fg"])

        # Assignee
        assignee = data.get("assignee", "")
        t.append(f"\n  Assignee: ", style=TN["comment"])
        if assignee:
            t.append(f"{assignee}\n", style=f"bold {TN['green']}")
        else:
            t.append("Unassigned\n", style=f"bold {TN['red']}")

        # Updated
        updated = data.get("updated", "")
        if updated:
            t.append(f"  Updated: ", style=TN["comment"])
            t.append(f"{_age_text(_parse_ts(updated)).plain} ago\n", style=TN["yellow"])

        # Description
        desc = data.get("description_text", "")
        if desc:
            t.append(f"\n  Description\n", style=f"bold {TN['comment']}")
            for line in desc[:300].split("\n")[:5]:
                t.append(f"  {line}\n", style=TN["fg"])

        # Comments
        comments = data.get("comments", [])
        if comments:
            last = comments[-1]
            t.append(f"\n  {len(comments)} comment{'s' if len(comments) != 1 else ''}", style=TN["comment"])
            t.append(f" \u2014 {last.get('author', '?')}\n", style=TN["cyan"])

        # Linked
        linked = data.get("linked_issues", [])
        if linked:
            t.append(f"\n  Linked: ", style=TN["comment"])
            t.append(", ".join(lnk.get("key", "?") for lnk in linked[:5]), style=TN["cyan"])
            t.append("\n")

        # Actions hint
        t.append(f"\n  \u2500\u2500 Actions \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n", style=TN["border"])
        actions = []
        sl = status.lower()
        if sl in ("open", "to do", "new"):
            actions.append(("s", "Start work"))
        if sl == "in progress":
            actions.extend([("v", "Verify"), ("y", "Hold")])
        if sl == "verification":
            actions.extend([("s", "Resume"), ("c", "Close")])
        if sl in ("on hold", "waiting for support"):
            actions.append(("s", "Resume"))
        if not actions:
            actions.append(("\u2014", "No actions available"))

        for k, label in actions:
            t.append(f"  {k}  ", style=f"bold {TN['cyan']}")
            t.append(f"{label}\n", style=TN["fg"])

        self.update(t)

    def show_error(self, msg: str) -> None:
        t = Text()
        t.append(f"\n  \u2716 Error\n\n", style=f"bold {TN['red']}")
        t.append(f"  {msg}\n", style=TN["fg"])

        if "credential" in msg.lower() or "JIRA_EMAIL" in msg:
            t.append(f"\n  Run: cwhelper setup\n", style=TN["yellow"])
        elif "timeout" in msg.lower() or "connect" in msg.lower():
            t.append(f"\n  Check network connection\n", style=TN["yellow"])

        self.update(t)
        self._current_key = None
        self._current_data = None

    def show_no_credentials(self) -> None:
        t = Text()
        t.append(f"\n  \u2716 No Credentials\n\n", style=f"bold {TN['red']}")
        t.append(f"  Jira credentials not found.\n\n", style=TN["fg"])
        t.append(f"  To set up, quit and run:\n", style=TN["comment"])
        t.append(f"  cwhelper setup\n\n", style=f"bold {TN['cyan']}")
        t.append(f"  This will create a .env file\n", style=TN["comment"])
        t.append(f"  with your Jira email + token.\n", style=TN["comment"])
        self.update(t)

    def show_jira_unreachable(self) -> None:
        t = Text()
        t.append(f"\n  \u25cf Jira Unreachable\n\n", style=f"bold {TN['yellow']}")
        t.append(f"  Could not connect to Jira.\n", style=TN["fg"])
        t.append(f"  Check your network connection.\n\n", style=TN["comment"])
        t.append(f"  Press r to retry.\n", style=f"bold {TN['cyan']}")
        self.update(t)

    def _post_action(self, action: str) -> None:
        if self._current_key:
            self.post_message(self.TicketAction(self._current_key, action))

    def action_ticket_start(self) -> None:
        self._post_action("start")

    def action_ticket_verify(self) -> None:
        self._post_action("verify")

    def action_ticket_hold(self) -> None:
        self._post_action("hold")

    def action_ticket_close(self) -> None:
        self._post_action("close")


# ---------------------------------------------------------------------------
# Status Bar
# ---------------------------------------------------------------------------

class StatusBar(Static):
    """Bottom status bar with connection health + last refresh time."""

    def set_status(self, msg: str, style: str = TN["comment"]) -> None:
        self.update(Text(f" {msg}", style=style))

    def set_connected(self, site_filter: str = "", last_refresh: float = 0) -> None:
        t = Text()
        t.append(" \u25cf ", style=f"bold {TN['green']}")
        t.append("Jira connected", style=TN["comment"])
        if site_filter:
            t.append(f"  \u2502  Site: {site_filter}", style=TN["fg"])
        if last_refresh:
            ago = int(time.time() - last_refresh)
            if ago < 60:
                t.append(f"  \u2502  Refreshed {ago}s ago", style=TN["comment"])
            else:
                t.append(f"  \u2502  Refreshed {ago // 60}m ago", style=TN["yellow"])
        t.append("  \u2502  ? help", style=TN["comment"])
        self.update(t)

    def set_disconnected(self, reason: str = "disconnected") -> None:
        t = Text()
        t.append(" \u25cf ", style=f"bold {TN['red']}")
        t.append(reason, style=TN["red"])
        t.append("  \u2502  r to retry  \u2502  ? help", style=TN["comment"])
        self.update(t)


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

CWHELPER_CSS = f"""
Screen {{
    background: {TN['bg']};
}}
Header {{
    background: {TN['bg_dark']};
    color: {TN['blue']};
}}
Footer {{
    background: {TN['bg_dark']};
}}
#kpi-bar {{
    layout: horizontal;
    height: 3;
    padding: 0 1;
    background: {TN['bg_dark']};
}}
.kpi-card {{
    width: 1fr;
    height: 3;
    content-align: center middle;
    text-align: center;
    border: round {TN['border']};
    margin: 0 1 0 0;
    background: {TN['bg']};
}}
.kpi-card:focus {{
    border: round {TN['blue']};
}}
#body {{
    layout: horizontal;
    height: 1fr;
}}
#left {{
    width: 1fr;
    height: 1fr;
}}
#ticket-table {{
    height: 1fr;
    border: round {TN['border']};
    background: {TN['bg']};
}}
#ticket-table:focus {{
    border: round {TN['blue']};
}}
#ticket-table > .datatable--header {{
    background: {TN['bg_dark']};
    color: {TN['comment']};
    text-style: bold;
}}
#ticket-table > .datatable--cursor {{
    background: {TN['surface']};
    color: {TN['fg']};
}}
#detail-panel {{
    width: 42;
    min-width: 32;
    height: 1fr;
    border: round {TN['border']};
    background: {TN['bg']};
    padding: 0 1;
    overflow-y: auto;
}}
#detail-panel:focus {{
    border: round {TN['blue']};
}}
#bottom {{
    height: auto;
    max-height: 8;
    padding: 0 1;
}}
#cmd-output {{
    height: 1fr;
    min-height: 2;
    max-height: 5;
    background: {TN['bg']};
    border: round {TN['border']};
    padding: 0 1;
    display: none;
}}
#cmd-output.visible {{
    display: block;
}}
#cmd-input {{
    height: 3;
    background: {TN['surface']};
    color: {TN['fg']};
    border: round {TN['border']};
}}
#cmd-input:focus {{
    border: round {TN['blue']};
}}
#status-bar {{
    height: 1;
    background: {TN['bg_dark']};
    color: {TN['comment']};
    dock: bottom;
}}
"""


# ---------------------------------------------------------------------------
# Main App
# ---------------------------------------------------------------------------

class CWHelperApp(App):
    """CWHelper TUI — Cronboard skeleton + Bagels skin."""

    TITLE = "CWHELPER"
    SUB_TITLE = "DCT Node Helper"
    CSS = CWHELPER_CSS

    BINDINGS = [
        Binding("r", "refresh_queue", "Refresh", show=True),
        Binding("m", "my_tickets", "Mine", show=True),
        Binding("f", "filter_site", "Site", show=True),
        Binding("slash", "focus_input", "/Cmd", show=True),
        Binding("question_mark", "show_help", "?Help", show=True),
        Binding("escape", "focus_table", "Table", show=False),
        Binding("q", "quit", "Quit", show=True),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._queue_data: list[dict] = []
        self._site_filter: str = ""
        self._mine_only: bool = False
        self._last_refresh: float = 0
        self._auto_refresh_timer: Timer | None = None
        self._cmd_history: list[str] = []
        self._history_idx: int = -1

    def compose(self) -> ComposeResult:
        yield Header()
        yield KPIBar(id="kpi-bar")
        with Horizontal(id="body"):
            with Vertical(id="left"):
                yield TicketTable(id="ticket-table")
            yield DetailPanel(id="detail-panel")
        with Vertical(id="bottom"):
            yield RichLog(highlight=False, markup=False, wrap=True, id="cmd-output")
            yield Input(
                placeholder="cwhelper> DO-12345 | queue --mine | history <device> | ? help",
                id="cmd-input",
            )
        yield StatusBar(id="status-bar")
        yield Footer()

    async def on_mount(self) -> None:
        # Load features
        try:
            from cwhelper.state import _load_user_state
            from cwhelper.config import _load_features
            state = _load_user_state()
            _load_features(state)
        except Exception:
            pass

        detail = self.query_one("#detail-panel", DetailPanel)
        status = self.query_one("#status-bar", StatusBar)

        # Check credentials first
        creds = _check_credentials()
        if not creds:
            detail.show_no_credentials()
            status.set_disconnected("No credentials \u2014 run: cwhelper setup")
            return

        # Check Jira connectivity
        status.set_status("Connecting to Jira...", TN["comment"])
        loop = asyncio.get_event_loop()
        healthy = await loop.run_in_executor(_pool, partial(_check_jira_health, *creds))

        if not healthy:
            detail.show_jira_unreachable()
            status.set_disconnected("Jira unreachable \u2014 check network")
            return

        detail.show_empty()
        await self._load_queue()

        # Auto-refresh every 60s
        self._auto_refresh_timer = self.set_interval(60, self._auto_refresh)

    async def _auto_refresh(self) -> None:
        """Background auto-refresh — update status bar timer and reload queue."""
        status = self.query_one("#status-bar", StatusBar)
        status.set_connected(self._site_filter, self._last_refresh)
        # Reload data every 60s
        if time.time() - self._last_refresh >= 60:
            await self._load_queue_silent()

    async def _load_queue(self, site: str = "", status_filter: str = "open",
                          project: str = "DO", mine_only: bool = False) -> None:
        self._site_filter = site
        self._mine_only = mine_only
        status_bar = self.query_one("#status-bar", StatusBar)
        status_bar.set_status("Loading queue...", TN["comment"])

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            _pool, partial(_fetch_queue_sync, site, status_filter, project, mine_only),
        )

        # Handle error strings from fetcher
        if isinstance(result, str):
            detail = self.query_one("#detail-panel", DetailPanel)
            if result == "NO_CREDENTIALS":
                detail.show_no_credentials()
                status_bar.set_disconnected("No credentials")
            else:
                detail.show_error(result)
                status_bar.set_disconnected("Error loading queue")
            return

        self._queue_data = result
        self._last_refresh = time.time()

        table = self.query_one("#ticket-table", TicketTable)
        table.load_issues(result)
        kpi = self.query_one("#kpi-bar", KPIBar)
        kpi.update_from_issues(result)

        mine_label = " (mine)" if mine_only else ""
        site_label = f" @ {site}" if site else ""
        status_bar.set_connected(self._site_filter, self._last_refresh)

        self._show_output(f"{len(result)} tickets{mine_label}{site_label}" if result else "No tickets found")

    async def _load_queue_silent(self) -> None:
        """Refresh queue without clearing the detail panel."""
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            _pool, partial(_fetch_queue_sync, self._site_filter, "open", "DO", self._mine_only),
        )
        if isinstance(result, list):
            self._queue_data = result
            self._last_refresh = time.time()
            table = self.query_one("#ticket-table", TicketTable)
            table.load_issues(result)
            kpi = self.query_one("#kpi-bar", KPIBar)
            kpi.update_from_issues(result)
            status_bar = self.query_one("#status-bar", StatusBar)
            status_bar.set_connected(self._site_filter, self._last_refresh)

    async def _load_ticket(self, key: str) -> None:
        detail = self.query_one("#detail-panel", DetailPanel)
        detail.show_loading(key)
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(_pool, partial(_fetch_ticket_sync, key))
        if data.get("error"):
            detail.show_error(data["error"])
        else:
            detail.show_ticket(data)

    def _show_output(self, msg: str, style: str = TN["green"]) -> None:
        log = self.query_one("#cmd-output", RichLog)
        log.clear()
        log.write(Text(f"  {msg}", style=style))

    # --- Events ---

    async def on_ticket_table_ticket_selected(self, event: TicketTable.TicketSelected) -> None:
        await self._load_ticket(event.key)

    async def on_detail_panel_ticket_action(self, event: DetailPanel.TicketAction) -> None:
        """Execute a ticket transition from the detail panel."""
        self._show_output(f"Executing: {event.action} on {event.key}...", TN["comment"])
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            _pool, partial(_execute_transition_sync, event.key, event.action),
        )
        if result.startswith("OK"):
            self._show_output(result, TN["green"])
            # Reload ticket and queue
            await self._load_ticket(event.key)
            await self._load_queue_silent()
        else:
            self._show_output(result, TN["red"])

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "cmd-input":
            return
        query = event.value.strip()
        if not query:
            return
        event.input.value = ""
        self._cmd_history.append(query)
        self._history_idx = -1

        # Show output panel
        self.query_one("#cmd-output", RichLog).add_class("visible")

        parts = query.split()
        cmd = parts[0].lower() if parts else ""

        # Queue
        if cmd == "queue":
            site = ""
            mine = "--mine" in parts or "-m" in parts
            status = "open"
            project = "DO"
            for i, p in enumerate(parts):
                if p in ("--site", "-s") and i + 1 < len(parts):
                    site = parts[i + 1]
                if p == "--status" and i + 1 < len(parts):
                    status = parts[i + 1]
                if p in ("--project", "-p") and i + 1 < len(parts):
                    project = parts[i + 1].upper()
            await self._load_queue(site, status, project, mine)
            self.action_focus_table()
            return

        # Ticket key
        if re.match(r'^[A-Za-z]+-\d+$', cmd):
            await self._load_ticket(cmd.upper())
            return

        # History
        if cmd == "history" and len(parts) > 1:
            self._show_output(f"Loading history for {parts[1]}...", TN["comment"])
            loop = asyncio.get_event_loop()
            try:
                from cwhelper.clients.jira import _get_credentials
                from cwhelper.services.queue import _search_node_history
                email, token = _get_credentials()
                issues = await loop.run_in_executor(
                    _pool, partial(_search_node_history, parts[1], email, token, limit=20),
                )
                if issues:
                    self.query_one("#ticket-table", TicketTable).load_issues(issues)
                    self._show_output(f"{len(issues)} tickets for {parts[1]}")
                else:
                    self._show_output(f"No tickets for {parts[1]}", TN["comment"])
            except Exception as e:
                self._show_output(f"Error: {e}", TN["red"])
            return

        # Filter table
        if cmd == "filter" and len(parts) > 1:
            self.query_one("#ticket-table", TicketTable).filter_issues(" ".join(parts[1:]))
            self._show_output(f"Filtered: {' '.join(parts[1:])}")
            return

        # Config/doctor/help via subprocess
        if cmd in ("config", "doctor", "help", "setup"):
            import subprocess
            try:
                args = ["cwhelper"] + (["--help"] if cmd == "help" else parts)
                result = subprocess.run(args, capture_output=True, text=True, timeout=10,
                                       env={**os.environ, "NO_COLOR": "1"})
                log = self.query_one("#cmd-output", RichLog)
                log.clear()
                log.write(result.stdout or result.stderr or "(no output)")
            except Exception as e:
                self._show_output(f"Error: {e}", TN["red"])
            return

        # Default: ticket lookup
        if len(query) >= 4:
            await self._load_ticket(query)

    # --- Actions ---

    async def action_refresh_queue(self) -> None:
        await self._load_queue(self._site_filter, "open", "DO", self._mine_only)

    async def action_my_tickets(self) -> None:
        self._mine_only = not self._mine_only
        label = "my tickets" if self._mine_only else "all tickets"
        self._show_output(f"Switching to {label}...", TN["comment"])
        await self._load_queue(self._site_filter, "open", "DO", self._mine_only)

    def action_filter_site(self) -> None:
        def _on_site(site: str) -> None:
            self._site_filter = site
            asyncio.ensure_future(self._load_queue(site, "open", "DO", self._mine_only))
        self.push_screen(SiteFilterScreen(), _on_site)

    def action_show_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_focus_input(self) -> None:
        self.query_one("#cmd-input", Input).focus()

    def action_focus_table(self) -> None:
        self.query_one("#ticket-table", TicketTable).focus()

    def refresh_feed(self) -> None:
        """Called by cockpit Ctrl+R."""
        asyncio.ensure_future(self.action_refresh_queue())
