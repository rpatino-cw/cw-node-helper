"""CWHelper TUI — Cronboard skeleton + Bagels skin.

Launch: cwhelper tui
Requires: pip install cw-node-helper[tui]

Ticket management dashboard with KPI cards, color-coded queue table,
detail panel, and command input. Uses cwhelper's existing service layer
directly (no subprocess shelling).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import partial

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, Container
from textual.widgets import DataTable, Footer, Header, Input, RichLog, Static
from textual.widget import Widget
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

# Status badge config
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
    """ISO timestamp -> seconds ago."""
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
# Thread pool for running sync cwhelper code off the event loop
# ---------------------------------------------------------------------------
_pool = ThreadPoolExecutor(max_workers=2)


def _fetch_queue_sync(site: str = "", status_filter: str = "open",
                      project: str = "DO", mine_only: bool = False,
                      limit: int = 30) -> list[dict]:
    """Run _search_queue synchronously (called from thread pool)."""
    try:
        from cwhelper.clients.jira import _get_credentials
        from cwhelper.services.search import _search_queue
        email, token = _get_credentials()
        return _search_queue(site, email, token, mine_only=mine_only,
                            limit=limit, status_filter=status_filter,
                            project=project, use_cache=False)
    except Exception:
        return []


def _fetch_ticket_sync(key: str) -> dict:
    """Fetch a single ticket context synchronously."""
    try:
        from cwhelper.clients.jira import _get_credentials
        from cwhelper.services.context import get_node_context
        return get_node_context(key, quiet=True)
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# KPI Bar
# ---------------------------------------------------------------------------

class KPIBar(Static):
    """Horizontal row of summary metric cards."""

    def compose(self) -> ComposeResult:
        yield Static("\u2014", id="kpi-open", classes="kpi-card")
        yield Static("\u2014", id="kpi-unassigned", classes="kpi-card")
        yield Static("\u2014", id="kpi-verify", classes="kpi-card")
        yield Static("\u2014", id="kpi-progress", classes="kpi-card")

    def update_from_issues(self, issues: list[dict]) -> None:
        total = len(issues)
        unassigned = sum(1 for iss in issues if not iss.get("fields", {}).get("assignee"))
        verify = 0
        progress = 0
        for iss in issues:
            st = _unwrap(iss.get("fields", {}).get("status", "")).lower()
            if st == "verification":
                verify += 1
            if st == "in progress":
                progress += 1

        self._set("kpi-open", f"Open  {total}", TN["blue"])
        self._set("kpi-unassigned", f"Unassigned  {unassigned}",
                  TN["red"] if unassigned else TN["comment"])
        self._set("kpi-verify", f"Verify  {verify}",
                  TN["yellow"] if verify else TN["comment"])
        self._set("kpi-progress", f"In Progress  {progress}",
                  TN["green"] if progress else TN["comment"])

    def _set(self, card_id: str, label: str, color: str) -> None:
        try:
            self.query_one(f"#{card_id}", Static).update(
                Text(label, style=f"bold {color}"))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Ticket Table
# ---------------------------------------------------------------------------

class TicketTable(DataTable):
    """Color-coded ticket queue with vim navigation."""

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

    def on_mount(self) -> None:
        self.add_columns("STATUS", "TICKET", "DEVICE", "LOC", "AGE", "ASSIGNEE")

    def load_issues(self, issues: list[dict]) -> None:
        self._issues = issues
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

    def action_scroll_home(self) -> None:
        self.move_cursor(row=0)

    def action_scroll_end(self) -> None:
        if self._issues:
            self.move_cursor(row=len(self._issues) - 1)


# ---------------------------------------------------------------------------
# Detail Panel
# ---------------------------------------------------------------------------

class DetailPanel(Static):
    """Right-side ticket context panel."""

    def show_empty(self) -> None:
        t = Text()
        t.append("  Select a ticket\n\n", style=f"bold {TN['comment']}")
        t.append("  j/k  ", style=f"bold {TN['blue']}")
        t.append("navigate\n", style=TN["comment"])
        t.append("  Enter ", style=f"bold {TN['blue']}")
        t.append("load detail\n", style=TN["comment"])
        t.append("  /     ", style=f"bold {TN['blue']}")
        t.append("command\n", style=TN["comment"])
        t.append("  r     ", style=f"bold {TN['blue']}")
        t.append("refresh\n", style=TN["comment"])
        self.update(t)

    def show_loading(self, key: str) -> None:
        self.update(Text(f"  Loading {key}...", style=f"italic {TN['comment']}"))

    def show_ticket(self, data: dict) -> None:
        t = Text()
        key = data.get("issue_key", data.get("key", "?"))

        # Header
        t.append(f"\n  {key}\n", style=f"bold {TN['blue']}")

        # Status badge
        status = data.get("status", "?")
        dot, color = _STATUS_STYLE.get(status.lower(), ("\u25cf", TN["comment"]))
        t.append(f"  {dot} {status}\n", style=color)

        # Summary
        summary = data.get("summary", "")
        if summary:
            t.append(f"\n  {summary}\n", style=f"bold {TN['fg']}")

        # Location
        loc = data.get("rack_location", "")
        if loc:
            t.append(f"\n  \u250c Location\n", style=f"bold {TN['comment']}")
            t.append(f"  \u2502 {loc}\n", style=TN["fg"])

        # Device info
        fields = [("Service Tag", "service_tag"), ("Hostname", "hostname"),
                  ("Vendor", "vendor"), ("Model", "model")]
        device_lines = [(l, data.get(f, "")) for l, f in fields if data.get(f)]
        if device_lines:
            t.append(f"\n  \u250c Device\n", style=f"bold {TN['comment']}")
            for label, val in device_lines:
                t.append(f"  \u2502 {label}: ", style=TN["comment"])
                t.append(f"{val}\n", style=TN["fg"])

        # Assignee
        assignee = data.get("assignee", "")
        t.append(f"\n  Assignee: ", style=TN["comment"])
        if assignee:
            t.append(f"{assignee}\n", style=f"bold {TN['green']}")
        else:
            t.append("Unassigned\n", style=f"bold {TN['red']}")

        # Age
        updated = data.get("updated", "")
        if updated:
            age_secs = _parse_ts(updated)
            t.append(f"  Updated: ", style=TN["comment"])
            t.append(f"{_age_text(age_secs).plain} ago\n", style=TN["yellow"])

        # Description preview
        desc = data.get("description_text", "")
        if desc:
            t.append(f"\n  \u250c Description\n", style=f"bold {TN['comment']}")
            for line in desc[:300].split("\n")[:6]:
                t.append(f"  \u2502 {line}\n", style=TN["fg"])

        # Comments
        comments = data.get("comments", [])
        if comments:
            t.append(f"\n  \u250c {len(comments)} comment{'s' if len(comments) != 1 else ''}\n",
                    style=f"bold {TN['comment']}")
            last = comments[-1]
            author = last.get("author", "?")
            body = last.get("body", "")[:120].replace("\n", " ")
            t.append(f"  \u2502 {author}: ", style=f"bold {TN['cyan']}")
            t.append(f"{body}\n", style=TN["comment"])

        # Linked
        linked = data.get("linked_issues", [])
        if linked:
            t.append(f"\n  Linked: ", style=TN["comment"])
            t.append(", ".join(lnk.get("key", "?") for lnk in linked[:5]),
                    style=TN["cyan"])
            t.append("\n")

        # Grafana
        grafana = data.get("grafana", {})
        if grafana.get("node_details"):
            t.append(f"\n  Grafana: ", style=TN["comment"])
            t.append("available\n", style=TN["green"])

        # NetBox
        netbox = data.get("netbox", {})
        if netbox.get("device_id"):
            t.append(f"  NetBox:  ", style=TN["comment"])
            t.append(f"device #{netbox['device_id']}\n", style=TN["green"])

        self.update(t)

    def show_error(self, msg: str) -> None:
        self.update(Text(f"  {msg}", style=TN["red"]))


# ---------------------------------------------------------------------------
# Main App
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
"""


class CWHelperApp(App):
    """CWHelper TUI — Cronboard skeleton + Bagels skin."""

    TITLE = "CWHELPER"
    SUB_TITLE = "DCT Node Helper"
    CSS = CWHELPER_CSS

    BINDINGS = [
        Binding("r", "refresh_queue", "Refresh", show=True),
        Binding("slash", "focus_input", "Command", show=True),
        Binding("escape", "focus_table", "Table", show=False),
        Binding("q", "quit", "Quit", show=True),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._queue_data: list[dict] = []

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
                placeholder="cwhelper> DO-12345 | queue [--mine] | history <device> | help",
                id="cmd-input",
            )
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
        detail.show_empty()

        log = self.query_one("#cmd-output", RichLog)
        log.write(Text("  Loading queue...", style=f"italic {TN['comment']}"))

        await self._load_queue()

    async def _load_queue(self, site: str = "", status_filter: str = "open",
                          project: str = "DO", mine_only: bool = False) -> None:
        log = self.query_one("#cmd-output", RichLog)
        log.clear()
        log.write(Text("  Loading queue...", style=f"italic {TN['comment']}"))

        loop = asyncio.get_event_loop()
        issues = await loop.run_in_executor(
            _pool,
            partial(_fetch_queue_sync, site, status_filter, project, mine_only),
        )

        self._queue_data = issues
        table = self.query_one("#ticket-table", TicketTable)
        table.load_issues(issues)
        kpi = self.query_one("#kpi-bar", KPIBar)
        kpi.update_from_issues(issues)

        log.clear()
        if issues:
            log.write(Text(f"  {len(issues)} tickets loaded", style=TN["green"]))
        else:
            log.write(Text("  No tickets found", style=TN["comment"]))

    async def _load_ticket(self, key: str) -> None:
        detail = self.query_one("#detail-panel", DetailPanel)
        detail.show_loading(key)

        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(_pool, partial(_fetch_ticket_sync, key))

        if data.get("error"):
            detail.show_error(data["error"])
        else:
            detail.show_ticket(data)

    # --- Events ---

    async def on_ticket_table_ticket_selected(self, event: TicketTable.TicketSelected) -> None:
        await self._load_ticket(event.key)

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "cmd-input":
            return
        query = event.value.strip()
        if not query:
            return
        event.input.value = ""

        log = self.query_one("#cmd-output", RichLog)
        log.clear()
        log.write(Text(f"  $ cwhelper {query}", style=f"dim {TN['cyan']}"))

        parts = query.split()
        cmd = parts[0].lower() if parts else ""

        # Queue command → reload table
        if cmd == "queue":
            site = ""
            mine = "--mine" in parts or "-m" in parts
            status = "open"
            project = "DO"
            for i, p in enumerate(parts):
                if p in ("--site", "-s") and i + 1 < len(parts):
                    site = parts[i + 1]
                if p in ("--status",) and i + 1 < len(parts):
                    status = parts[i + 1]
                if p in ("--project", "-p") and i + 1 < len(parts):
                    project = parts[i + 1].upper()
            await self._load_queue(site, status, project, mine)
            return

        # Ticket key → load detail
        if re.match(r'^[A-Za-z]+-\d+$', cmd):
            await self._load_ticket(cmd.upper())
            log.write(Text(f"  Loaded {cmd.upper()}", style=TN["green"]))
            return

        # History → load as queue-style list
        if cmd == "history" and len(parts) > 1:
            log.write(Text("  Loading history...", style=f"italic {TN['comment']}"))
            loop = asyncio.get_event_loop()
            try:
                from cwhelper.clients.jira import _get_credentials
                from cwhelper.services.queue import _search_node_history
                email, token = _get_credentials()
                issues = await loop.run_in_executor(
                    _pool,
                    partial(_search_node_history, parts[1], email, token, limit=20),
                )
                if issues:
                    table = self.query_one("#ticket-table", TicketTable)
                    table.load_issues(issues)
                    log.clear()
                    log.write(Text(f"  {len(issues)} tickets for {parts[1]}", style=TN["green"]))
                else:
                    log.clear()
                    log.write(Text(f"  No tickets found for {parts[1]}", style=TN["comment"]))
            except Exception as e:
                log.clear()
                log.write(Text(f"  Error: {e}", style=TN["red"]))
            return

        # Config / doctor / help → show in log
        if cmd in ("config", "doctor", "help", "--help", "setup"):
            import subprocess
            try:
                args = ["cwhelper"] + parts
                if cmd == "help":
                    args = ["cwhelper", "--help"]
                result = subprocess.run(args, capture_output=True, text=True, timeout=10,
                                       env={**os.environ, "NO_COLOR": "1"})
                log.clear()
                log.write(result.stdout or result.stderr or "(no output)")
            except Exception as e:
                log.write(Text(f"  Error: {e}", style=TN["red"]))
            return

        # Default: try as device lookup
        await self._load_ticket(query)

    # --- Actions ---

    async def action_refresh_queue(self) -> None:
        await self._load_queue()

    def action_focus_input(self) -> None:
        self.query_one("#cmd-input", Input).focus()

    def action_focus_table(self) -> None:
        self.query_one("#ticket-table", TicketTable).focus()
