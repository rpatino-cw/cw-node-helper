"""Rich-based display module — replaces raw ANSI print calls throughout cwhelper."""
from __future__ import annotations

import os
import re

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.rule import Rule
from rich.padding import Padding
from rich import box as rich_box

__all__ = [
    "console",
    "_rich_status",
    "_rich_print_banner",
    "_rich_print_ticket",
    "_rich_print_queue_table",
    "_rich_queue_prompt",
    "_rich_print_menu",
    "_rich_print_menu_compact",
    "_rich_print_menu_full",
]

console = Console(highlight=False)

# ---------------------------------------------------------------------------
# Status styling
# ---------------------------------------------------------------------------

_STATUS_MAP: dict[str, tuple[str, str]] = {
    "closed":              ("green",   "●"),
    "done":                ("green",   "●"),
    "resolved":            ("green",   "●"),
    "canceled":            ("green",   "●"),
    "in progress":         ("yellow",  "●"),
    "open":                ("yellow",  "●"),
    "reopened":            ("yellow",  "●"),
    "on hold":             ("magenta", "○"),
    "blocked":             ("magenta", "○"),
    "paused":              ("magenta", "○"),
    "verification":        ("blue",    "●"),
    "waiting for support": ("blue",    "●"),
    "awaiting triage":     ("cyan",    "○"),
    "to do":               ("dim",     "○"),
    "new":                 ("dim",     "○"),
}


def _rich_status(status: str) -> tuple[str, str]:
    """Return (rich_style, dot_char) for a status name."""
    return _STATUS_MAP.get(status.lower(), ("dim", "●"))


# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------

def _rich_print_banner(name: str = ""):
    from cwhelper.config import APP_VERSION

    left_str = f"CWHELPER  v{APP_VERSION}"
    t = Text("  ")
    t.append("CWHELPER", style="bold white")
    t.append(f"  v{APP_VERSION}", style="dim")

    if name:
        try:
            width = os.get_terminal_size().columns
        except Exception:
            width = 80
        pad = max(4, width - 2 - len(left_str) - len(name))
        t.append(" " * pad)
        t.append(name, style="bold green")

    console.print()
    console.print(t)


# ---------------------------------------------------------------------------
# Ticket detail
# ---------------------------------------------------------------------------

def _rich_print_ticket(ctx: dict):
    """Rich replacement for _print_pretty. Answers: where, what, which device."""
    from cwhelper.services.context import _parse_rack_location, _format_age

    status   = ctx.get("status", "")
    key      = ctx.get("issue_key", "")
    style, dot = _rich_status(status)

    # --- Location breadcrumb (the first question) ---
    parsed = _parse_rack_location(ctx.get("rack_location", ""))
    loc_parts: list[str] = []
    if parsed:
        if parsed.get("dh"):
            loc_parts.append(parsed["dh"])
        if parsed.get("rack") is not None:
            loc_parts.append(f"R{parsed['rack']}")
        if parsed.get("ru"):
            loc_parts.append(f"RU{parsed['ru']}")
    hn = ctx.get("hostname") or ""
    node_m = re.search(r"-node-(\d+)-", hn)
    if node_m:
        loc_parts.append(f"Node {node_m.group(1)}")
    loc_str = " › ".join(loc_parts) if loc_parts else ctx.get("rack_location", "")

    # --- Action line (the second question) ---
    psu = ctx.get("psu_info")
    if psu:
        psu_id = psu.get("psu_id", "?")
        if psu.get("all_psu_ids") and len(psu["all_psu_ids"]) > 1:
            all_ids = ", ".join(f"PSU {p}" for p in psu["all_psu_ids"])
            action_line = f"⚡ {all_ids} — FAILED"
        else:
            action_line = f"⚡ PSU {psu_id} — REMOVE AND REPLACE"
        action_style = "bold yellow"
    else:
        action_line = ctx.get("summary", "")
        action_style = "bold white"

    # --- Service tag (the third question) ---
    service_tag = ctx.get("service_tag", "")

    # --- Header panel content ---
    content = Text()
    content.append(f"{loc_str}\n", style="bold cyan")
    content.append(f"{action_line}\n", style=action_style)
    if service_tag:
        content.append(service_tag, style="dim")

    # --- Panel title: ticket key + status + age + assignee ---
    age_secs  = ctx.get("status_age_seconds", 0)
    assignee  = ctx.get("assignee") or "Unassigned"
    is_mine   = bool(ctx.get("assignee"))

    title = Text()
    title.append(f"{dot} {key}", style=f"bold {style}")
    title.append(f"  {status.upper()}", style=style)
    if age_secs > 0:
        age_color = "red" if age_secs > 48 * 3600 else "yellow" if age_secs > 24 * 3600 else "green"
        title.append(f"  ·  {_format_age(age_secs)}", style=age_color)
    title.append("  ·  ", style="dim")
    title.append(assignee, style="magenta bold" if is_mine else "dim")

    console.print()
    console.print(Panel(
        content,
        title=title,
        title_align="left",
        border_style=style,
        padding=(0, 2),
    ))

    # --- Node data table ---
    netbox = ctx.get("netbox") or {}

    node_table = Table(
        show_header=False,
        box=rich_box.SIMPLE,
        padding=(0, 1),
        show_edge=False,
    )
    node_table.add_column("Label", style="dim",  min_width=12, max_width=14)
    node_table.add_column("Value", style="cyan", min_width=20)

    rows: list[tuple[str, str]] = [
        ("Site",     ctx.get("site", "")),
        ("Rack",     ctx.get("rack_location", "")),
        ("Hostname", hn),
        ("Vendor",   ctx.get("vendor", "")),
    ]
    ip = ctx.get("ip_address")
    if ip and ip != "0.0.0.0":
        rows.append(("IP", ip))
    if netbox:
        oob = netbox.get("oob_ip", "")
        if oob:
            rows.append(("BMC IP", oob.split("/")[0]))
        ip6 = netbox.get("primary_ip6", "")
        if ip6:
            rows.append(("IPv6", ip6.split("/")[0]))
        if netbox.get("asset_tag"):
            rows.append(("Asset Tag", netbox["asset_tag"]))
        if netbox.get("position"):
            rows.append(("RU",        f"U{netbox['position']}"))
        if netbox.get("model"):
            rows.append(("Model",     netbox["model"]))
        if netbox.get("device_role"):
            rows.append(("Role",      netbox["device_role"]))
        if netbox.get("status"):
            rows.append(("NB Status", netbox["status"]))

    for label, value in rows:
        if value:
            node_table.add_row(label, value)

    console.print(node_table)

    # --- Hostname/site mismatch warning ---
    site = ctx.get("site", "")
    if hn and site:
        # Extract site slug from hostname (e.g., "ca-east-01a" from "dh1000-r199-...-ca-east-01a")
        # Compare against the ticket's site field
        _site_lower = site.lower().replace("-", "").replace("_", "")
        _hn_lower = hn.lower()
        # Check common site indicators in hostname that don't match the ticket site
        _hn_site_m = re.search(r'-((?:us|ca|eu|ap|sa)-[a-z]+-\d+[a-z]?)$', _hn_lower)
        if _hn_site_m:
            _hn_site = _hn_site_m.group(1).replace("-", "")
            if _hn_site != _site_lower and _hn_site not in _site_lower and _site_lower not in _hn_site:
                console.print(f"  [bold red]⚠ SITE MISMATCH[/]  hostname → [cyan]{_hn_site_m.group(1)}[/]  ticket → [cyan]{site}[/]")

    # --- RMA reason ---
    if ctx.get("rma_reason") or ctx.get("node_name"):
        console.print(Rule(style="dim"))
        if ctx.get("rma_reason"):
            console.print(f"  [dim]RMA Reason[/]  [yellow]{ctx['rma_reason']}[/]")
        if ctx.get("node_name"):
            console.print(f"  [dim]Node[/]  [cyan]{ctx['node_name']}[/]")

    # --- PSU Grafana link (action already shown in header) ---
    if psu:
        try:
            from cwhelper.clients.grafana import _find_psu_dashboard_url
            psu_url = _find_psu_dashboard_url(ctx)
            if psu_url:
                console.print(f"  [dim]PSU Dashboard:[/dim] [dim]{psu_url[:90]}[/dim]")
        except Exception:
            pass

    # --- HO context ---
    ho = ctx.get("ho_context")
    if ho and ctx.get("source") != "netbox":
        ho_style, ho_dot = _rich_status(ho["status"])
        console.print(Rule("HO Context", style="dim"))
        ho_line = Text()
        ho_line.append(f"{ho_dot} {ho['key']}", style=f"bold {ho_style}")
        ho_line.append(f"  {ho['status']}", style=ho_style)
        ho_line.append(f"\n{ho['summary']}", style="dim")
        ho_line.append(f"\n{ho.get('hint', '')}", style="magenta")
        if ho.get("last_note"):
            ho_line.append(f"\nLast note: {ho['last_note']}", style="dim")
        console.print(Padding(ho_line, (0, 2)))

    # --- SLA timers ---
    sla_values = ctx.get("sla", [])
    if sla_values and ctx.get("source") != "netbox":
        console.print(Rule("SLA", style="dim"))
        for sla in sla_values:
            sla_name = sla.get("name", "?")
            ongoing   = sla.get("ongoingCycle")
            completed = sla.get("completedCycles", [])
            if not ongoing and completed:
                last     = completed[-1]
                breached = last.get("breached", False)
                elapsed  = (last.get("elapsedTime") or {}).get("friendly", "?")
                if breached:
                    console.print(f"  [red]● Breached[/]  [dim]{sla_name}  (took {elapsed})[/]")
                else:
                    console.print(f"  [green]● Met[/]       [dim]{sla_name}  (in {elapsed})[/]")
            elif ongoing:
                if ongoing.get("breached"):
                    elapsed = (ongoing.get("elapsedTime") or {}).get("friendly", "?")
                    console.print(f"  [red]● Breached[/]  {sla_name}  [dim]({elapsed} elapsed)[/]")
                elif ongoing.get("paused"):
                    remaining = (ongoing.get("remainingTime") or {}).get("friendly", "?")
                    console.print(f"  [blue]● Paused[/]    {sla_name}  [dim]({remaining} remaining)[/]")
                else:
                    remaining_ms = (ongoing.get("remainingTime") or {}).get("millis", 0)
                    goal_ms      = (ongoing.get("goalDuration") or {}).get("millis", 1)
                    remaining_str = (ongoing.get("remainingTime") or {}).get("friendly", "?")
                    pct   = remaining_ms / goal_ms if goal_ms else 0
                    color = "red" if pct < 0.25 else "yellow" if pct < 0.50 else "green"
                    console.print(f"  [{color}]● {remaining_str}[/]  {sla_name}  [dim]remaining[/]")

    # --- Linked tickets ---
    linked = ctx.get("linked_issues", [])
    if linked and ctx.get("source") != "netbox":
        console.print(Rule("Linked", style="dim"))
        for lnk in linked:
            lnk_style, lnk_dot = _rich_status(lnk["status"])
            lnk_line = Text()
            lnk_line.append(f"{lnk_dot} {lnk['key']:<12}", style=f"bold {lnk_style}")
            lnk_line.append(f"  {lnk['status']:<18}", style=lnk_style)
            lnk_line.append(f"  {lnk.get('relationship', '')}  ", style="dim")
            if lnk.get("summary"):
                lnk_line.append(lnk["summary"][:60], style="dim")
            console.print(Padding(lnk_line, (0, 2)))

    console.print()


# ---------------------------------------------------------------------------
# Queue list
# ---------------------------------------------------------------------------

def _rich_print_queue_table(issues: list, title: str = "", page_info: str = ""):
    """Print the queue as a Rich table. Returns nothing — caller handles prompt."""
    from cwhelper.services.context import _parse_jira_timestamp, _format_age

    def _safe(val, default="—"):
        if val is None:
            return default
        if isinstance(val, list):
            return str(val[0]) if val else default
        if isinstance(val, dict):
            return str(val.get("name") or val.get("displayName") or val.get("value") or default)
        return str(val)

    if title:
        console.print()
        console.print(Rule(f"[bold]{title}[/]", style="dim"))

    table = Table(
        show_header=True,
        header_style="dim",
        box=rich_box.SIMPLE,
        show_edge=False,
        padding=(0, 1),
    )
    table.add_column("#",          style="dim",     width=3,  no_wrap=True)
    table.add_column("Ticket",     style="bold",    width=10, no_wrap=True)
    table.add_column("Status",                      width=16, no_wrap=True)
    table.add_column("Age",        style="dim",     width=8,  no_wrap=True)
    table.add_column("Service Tag",style="cyan",    width=18, no_wrap=True)
    table.add_column("Loc",        style="cyan dim",width=8,  no_wrap=True)
    table.add_column("Assignee",   style="dim",     no_wrap=True)

    for i, iss in enumerate(issues, start=1):
        f   = iss.get("fields") or {}
        key = _safe(iss.get("key"))

        st_obj = f.get("status")
        st     = _safe(st_obj.get("name") if isinstance(st_obj, dict) else st_obj)
        s_style, s_dot = _rich_status(st)

        tag = _safe(f.get("customfield_10193"))

        assignee_obj = f.get("assignee")
        assignee = ""
        if isinstance(assignee_obj, dict):
            assignee = assignee_obj.get("displayName", "")
        elif assignee_obj:
            assignee = _safe(assignee_obj, "")

        age_secs = _parse_jira_timestamp(f.get("statuscategorychangedate"))
        if age_secs > 5 * 86400:
            age_display = f"[red]{_format_age(age_secs)}[/]"
        elif age_secs > 86400:
            age_display = f"[yellow]{_format_age(age_secs)}[/]"
        elif age_secs > 0:
            age_display = f"[green]{_format_age(age_secs)}[/]"
        else:
            age_display = ""

        rack_loc = _safe(f.get("customfield_10207"), "")
        hostname = _safe(f.get("customfield_10192"), "")
        summary  = _safe(f.get("summary"), "")
        desc_raw = f.get("description") or ""
        # Jira Cloud returns description as ADF (dict); extract text from it
        if isinstance(desc_raw, dict):
            _desc_parts = []
            for _blk in desc_raw.get("content", []):
                for _inl in _blk.get("content", []):
                    if _inl.get("type") == "text":
                        _desc_parts.append(_inl.get("text", ""))
            desc_text = " ".join(_desc_parts)
        else:
            desc_text = str(desc_raw)
        # Extract rack number from multiple formats:
        #   dot:   US-EVI01.DH1.R317.RU26
        #   colon: US-EVI01:dh1:317:26
        #   bare:  R307 or r307 anywhere in the string
        #   hostname: dh1000-r199-nvl-mgmt-...
        #   summary:  "DH2 › R64 › RU10" or "DH2 > R64 > RU10"
        #   description: rack info embedded in ticket body
        rack_m = (
            re.search(r"\.R(\d+)(?:\.|$)", rack_loc)
            or re.search(r":(\d+)(?::|$)", rack_loc)
            or re.search(r"\bR(\d+)\b", rack_loc, re.IGNORECASE)
            or re.search(r"\br(\d+)\b", hostname, re.IGNORECASE)
            or re.search(r"\bR(\d+)\b", summary, re.IGNORECASE)
            or re.search(r"\.R(\d+)(?:\.|$)", desc_text)
            or re.search(r"\bR(\d+)\b", desc_text, re.IGNORECASE)
        )
        node_m   = re.search(r"-node-(\d+)", hostname)
        loc_parts = []
        if rack_m:
            loc_parts.append(f"R{rack_m.group(1)}")
        if node_m:
            loc_parts.append(f"N{node_m.group(1)}")
        loc_str = "·".join(loc_parts)

        status_cell = Text()
        status_cell.append(f"{s_dot} ", style=s_style)
        status_cell.append(st, style=s_style)

        table.add_row(
            str(i),
            key,
            status_cell,
            age_display,
            tag,
            loc_str,
            assignee,
        )

    console.print(table)
    if page_info:
        console.print(f"  [dim]{page_info}[/]")


def _rich_queue_prompt(n_issues: int, extra_hints: list[str] = None) -> str:
    """Show the queue selection prompt and return raw input."""
    hints = ", ".join(extra_hints) if extra_hints else ""
    hint_str = f"  [{hints}]" if hints else ""
    prompt = f"  Type a number (1-{n_issues}){hint_str}, b back, m menu, or ENTER to refresh: "
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return "q"


# ---------------------------------------------------------------------------
# Main menu
# ---------------------------------------------------------------------------

_COMPACT_LABELS: dict[str, str] = {
    "1": "queue",   "2": "mine",    "3": "map",     "4": "bookmarks",
    "p": "scripts",  "l": "activity", "w": "walk",
}

_COL_W = 14   # chars per inline-menu cell


def _rich_print_menu(
    options: list[tuple[str, str, str]],   # (key, label, hint)
    shortcuts: list[tuple[str, str]] = None,
    stale_count: int = 0,
    last_ticket: tuple[str, str] = None,   # (key, summary)
    watcher_info: str = "",
    ai_enabled: bool = False,
    ai_available: bool = False,
    compact: bool = False,
):
    """3D-2D minimal menu: no panels, no section labels, structure from whitespace only."""

    INDENT = "   "

    def _row(pairs: list[tuple[str, str]]) -> Text:
        t = Text(INDENT)
        for key, label in pairs:
            t.append(key,        style="bold")
            t.append(f"  {label}", style="dim")
            pad = max(1, _COL_W - len(key) - 2 - len(label))
            t.append(" " * pad)
        return t

    console.print()

    # Stale alert — plain text, no panel
    if stale_count:
        plural = "s" if stale_count != 1 else ""
        t = Text(INDENT)
        t.append(f"⚠  {stale_count} stale ticket{plural}", style="bold red")
        t.append("   → ", style="dim")
        t.append("3", style="bold yellow")
        console.print(t)
        console.print()

    # Watcher status — plain text
    if watcher_info:
        console.print(f"{INDENT}[bold green]◉  watching[/]  [dim]{watcher_info}[/]")
        console.print()

    # Last viewed shortcut — plain text
    if last_ticket:
        lk, ls = last_ticket
        snip = f"  {ls[:48]}" if ls else ""
        t = Text(INDENT)
        t.append("0", style="bold")
        t.append("  ↩  ", style="dim")
        t.append(lk,   style="bold cyan")
        t.append(snip, style="dim")
        console.print(t)

    console.print()

    # Numbered options — row 1 (1-4), row 2 (5-7)
    numbered = [
        (k, _COMPACT_LABELS.get(k, l.split()[0].lower()))
        for k, l, _ in options if k.strip() and k.isdigit()
    ]
    console.print(_row(numbered[:4]))
    if numbered[4:]:
        console.print(_row(numbered[4:]))

    console.print()

    # Lettered utility options (p, l, mj, v)
    lettered = [
        (k, _COMPACT_LABELS.get(k, l.split()[0].lower()))
        for k, l, _ in options if k.strip() and not k.isdigit()
    ]
    if lettered:
        console.print(_row(lettered))

    console.print()
    console.print()

    # Nav row: q quit  ? help  ai on/off
    nav = Text(INDENT)
    nav.append("q",      style="bold")
    nav.append("  quit", style="dim")
    nav.append(" " * max(1, _COL_W - 7))
    nav.append("?",      style="bold cyan")
    nav.append("  help", style="dim")
    if ai_available:
        ai_style = "bold cyan" if ai_enabled else "dim"
        nav.append(" " * max(1, _COL_W - 7))
        nav.append("ai",   style=ai_style)
        nav.append(f"  {'on' if ai_enabled else 'off'}", style="dim")
    console.print(nav)

    # Feature count hint — show when not all features are enabled
    from cwhelper.config import FEATURES as _FEATURES
    _n_on = sum(1 for v in _FEATURES.values() if v)
    _n_total = len(_FEATURES)
    if _n_on < _n_total:
        feat = Text(INDENT)
        feat.append(f"{_n_on}/{_n_total} features enabled", style="dim italic")
        console.print(feat)

    # Bookmarks
    if shortcuts:
        console.print()
        console.print()
        bm = Text(INDENT)
        for i, (bk, bl) in enumerate(shortcuts):
            bm.append(bk,        style="bold")
            bm.append(f"  {bl}", style="dim")
            if i < len(shortcuts) - 1:
                bm.append("    ")
        console.print(bm)

    console.print()


def _rich_print_menu_compact(
    options: list[tuple[str, str, str]],
    shortcuts: list[tuple[str, str]] = None,
    ai_enabled: bool = False,
    ai_available: bool = False,
):
    """Legacy compact grid — delegates to _rich_print_menu."""
    _rich_print_menu(options, shortcuts, ai_enabled=ai_enabled, ai_available=ai_available)


def _rich_print_menu_full(
    options: list[tuple[str, str, str]],
    shortcuts: list[tuple[str, str]] = None,
    ai_enabled: bool = False,
    ai_available: bool = False,
):
    """Legacy full table — delegates to _rich_print_menu."""
    _rich_print_menu(options, shortcuts, ai_enabled=ai_enabled, ai_available=ai_available)
