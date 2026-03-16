"""Radar dashboard — HO tickets in pre-DO statuses for shift planning."""
from __future__ import annotations

import re

from cwhelper import config as _cfg
from cwhelper.config import *  # noqa: F401,F403
__all__ = ['_fetch_radar_queue', '_run_radar_interactive']
from cwhelper.services.search import _search_queue
from cwhelper.services.watcher import _infer_procedure
from cwhelper.services.context import _build_prep_brief, _format_age, _parse_jira_timestamp, _unwrap_field


# Urgency tiers for display ordering
_URGENCY = {
    "sent to dct uc": 1,
    "sent to dct rc": 1,
    "rma-initiate":   2,
    "awaiting parts": 3,
}


def _urgency_rank(status: str) -> int:
    """Return urgency rank (1=imminent, 2=soon, 3=eventual)."""
    return _URGENCY.get(status.lower(), 9)


def _fetch_radar_queue(email: str, token: str, site: str = "") -> list:
    """Fetch HO tickets in pre-DO statuses, sorted by urgency then rack."""
    issues = _search_queue(site, email, token, limit=50,
                           status_filter="radar", project="HO",
                           use_cache=False)

    # Sort: urgency first, then rack number for physical clustering
    def sort_key(iss):
        f = iss.get("fields", {})
        status = (f.get("status") or {}).get("name", "")
        rack = _unwrap_field(f.get("customfield_10207")) or ""
        rack_m = re.search(r"R(\d+)", rack)
        rack_num = int(rack_m.group(1)) if rack_m else 9999
        return (_urgency_rank(status), rack_num)

    issues.sort(key=sort_key)
    return issues


def _radar_summary_line(issues: list) -> str:
    """Build a one-line summary: '3 imminent DOs, 2 waiting on parts. Hottest area: R60-R70.'"""
    imminent = sum(1 for i in issues
                   if _urgency_rank((i.get("fields", {}).get("status") or {}).get("name", "")) == 1)
    soon = sum(1 for i in issues
               if _urgency_rank((i.get("fields", {}).get("status") or {}).get("name", "")) == 2)
    eventual = sum(1 for i in issues
                   if _urgency_rank((i.get("fields", {}).get("status") or {}).get("name", "")) == 3)

    parts = []
    if imminent:
        parts.append(f"{RED}{BOLD}{imminent} imminent{RESET}")
    if soon:
        parts.append(f"{YELLOW}{soon} soon{RESET}")
    if eventual:
        parts.append(f"{DIM}{eventual} awaiting parts{RESET}")

    # Find hottest rack area (most tickets)
    rack_counts: dict[str, int] = {}
    for iss in issues:
        f = iss.get("fields", {})
        rack = _unwrap_field(f.get("customfield_10207")) or ""
        m = re.search(r"R(\d+)", rack)
        if m:
            # Group by tens (R60-R69, R70-R79, etc.)
            base = (int(m.group(1)) // 10) * 10
            bucket = f"R{base:02d}-R{base + 9:02d}"
            rack_counts[bucket] = rack_counts.get(bucket, 0) + 1

    hottest = ""
    if rack_counts:
        top = max(rack_counts, key=rack_counts.get)
        if rack_counts[top] > 1:
            hottest = f"  Hottest area: {BOLD}{top}{RESET} ({rack_counts[top]} tickets)"

    return ", ".join(parts) + "." + hottest if parts else "No radar tickets."


def _print_radar_table(issues: list) -> None:
    """Print the radar dashboard as a formatted table."""
    from cwhelper.tui.rich_console import console, _rich_status, Table, Text, Rule
    from rich import box as rich_box

    console.print()
    console.print(Rule("[bold]HO Radar — Incoming Work[/]", style="yellow"))
    console.print(f"  {_radar_summary_line(issues)}")
    console.print()

    table = Table(
        show_header=True,
        header_style="dim",
        box=rich_box.SIMPLE,
        show_edge=False,
        padding=(0, 1),
    )
    table.add_column("#",          style="dim",     width=3,  no_wrap=True)
    table.add_column("HO Ticket",  style="bold",    width=10, no_wrap=True)
    table.add_column("Status",                      width=18, no_wrap=True)
    table.add_column("Procedure",  style="cyan",    width=12, no_wrap=True)
    table.add_column("Service Tag",style="cyan",    width=12, no_wrap=True)
    table.add_column("Rack",       style="dim",     width=10, no_wrap=True)
    table.add_column("Age",        style="dim",     width=8,  no_wrap=True)
    table.add_column("Summary",    style="dim",     no_wrap=True, max_width=40)

    for i, iss in enumerate(issues, start=1):
        f = iss.get("fields") or {}
        key = iss.get("key", "?")
        status = (f.get("status") or {}).get("name", "?")
        s_style, s_dot = _rich_status(status)
        proc, _ = _infer_procedure(status)
        tag = _unwrap_field(f.get("customfield_10193")) or "—"
        if isinstance(tag, list):
            tag = tag[0] if tag else "—"

        rack_raw = _unwrap_field(f.get("customfield_10207")) or ""
        if isinstance(rack_raw, list):
            rack_raw = rack_raw[0] if rack_raw else ""
        rack_short = ""
        if "." in rack_raw:
            parts = rack_raw.split(".")
            rack_short = ".".join(parts[-2:]) if len(parts) >= 2 else rack_raw

        age_secs = _parse_jira_timestamp(f.get("statuscategorychangedate"))
        if age_secs > 5 * 86400:
            age_display = f"[red]{_format_age(age_secs)}[/]"
        elif age_secs > 86400:
            age_display = f"[yellow]{_format_age(age_secs)}[/]"
        elif age_secs > 0:
            age_display = f"[green]{_format_age(age_secs)}[/]"
        else:
            age_display = ""

        summary = (f.get("summary") or "")[:40]

        status_cell = Text()
        status_cell.append(f"{s_dot} ", style=s_style)
        status_cell.append(status, style=s_style)

        # Urgency color for procedure
        urg = _urgency_rank(status)
        proc_style = "red bold" if urg == 1 else ("yellow" if urg == 2 else "dim")

        table.add_row(
            str(i), key, status_cell, f"[{proc_style}]{proc}[/]",
            str(tag), rack_short, age_display, summary,
        )

    console.print(Padding(table, (0, 2)))
    console.print()


def _run_radar_interactive(email: str, token: str, site: str = "") -> str:
    """Interactive radar dashboard. Returns "back", "quit", or "menu".

    User can pick a number to drill into an HO ticket with prep brief.
    """
    from cwhelper.tui.display import _clear_screen, _print_pretty, _print_prep_brief
    from cwhelper.tui.actions import _post_detail_prompt
    from cwhelper.services.context import _fetch_and_show

    while True:
        _clear_screen()
        issues = _fetch_radar_queue(email, token, site=site)

        if not issues:
            print(f"\n  {GREEN}Radar clear — no HO tickets in pre-DO statuses.{RESET}\n")
            try:
                input(f"  {DIM}Press ENTER to go back{RESET}")
            except (EOFError, KeyboardInterrupt):
                pass
            return "back"

        _print_radar_table(issues)

        try:
            choice = input(
                f"  {DIM}Pick # for prep brief, or{RESET} "
                f"{BOLD}[b]{RESET}ack {BOLD}[r]{RESET}efresh {BOLD}[q]{RESET}uit: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return "back"

        if choice in ("b", "back", ""):
            return "back"
        if choice in ("q", "quit"):
            return "quit"
        if choice in ("r", "refresh"):
            continue

        # Numeric pick — drill into HO with prep brief
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(issues):
                iss = issues[idx]
                key = iss.get("key", "?")

                # Build and show prep brief
                print(f"\n  {DIM}Building prep brief for {key}...{RESET}")
                prep = _build_prep_brief(iss, email, token)
                _print_prep_brief(prep)

                try:
                    drill = input(
                        f"  {BOLD}[v]{RESET} View full HO   "
                        f"{BOLD}[b]{RESET} Back to radar: "
                    ).strip().lower()
                except (EOFError, KeyboardInterrupt):
                    drill = ""

                if drill in ("v", "view"):
                    ctx = _fetch_and_show(key, email, token)
                    if ctx:
                        _clear_screen()
                        _print_pretty(ctx)
                        result = _post_detail_prompt(ctx, email, token)
                        if result == "quit":
                            return "quit"
        except ValueError:
            pass
