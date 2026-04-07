"""Rack ticket report — group queue tickets by rack, sorted by count."""
from __future__ import annotations

import json
import re
from collections import defaultdict

from cwhelper.services.search import _search_queue
from cwhelper.services.context import _unwrap_field
from cwhelper.tui.rich_console import console, Table, rich_box, Text


def _extract_rack_num(f: dict) -> int | None:
    """Extract rack number from ticket fields."""
    rack_loc = str(f.get("customfield_10207") or "")
    hostname = str(f.get("customfield_10192") or "")
    summary = str(f.get("summary") or "")

    m = (re.search(r'\.R(\d+)\.', rack_loc)
         or re.search(r':(\d+)(?::|$)', rack_loc)
         or re.search(r'\bR(\d+)\b', rack_loc, re.IGNORECASE)
         or re.search(r'\br(\d+)\b', hostname, re.IGNORECASE)
         or re.search(r'\bR(\d+)\b', summary, re.IGNORECASE))
    return int(m.group(1)) if m else None


def _run_rack_report(email: str, token: str, site: str,
                     mine_only: bool = False, limit: int = 200,
                     status_filter: str = "open", project: str = "DO",
                     json_mode: bool = False):
    """Fetch queue tickets and display a per-rack breakdown sorted by count."""
    issues = _search_queue(site, email, token, mine_only=mine_only, limit=limit,
                           status_filter=status_filter, project=project)

    if not issues:
        console.print("\n  [dim]No tickets found.[/dim]\n")
        return

    # Group by rack
    by_rack: dict[int, list] = defaultdict(list)
    no_rack: list = []

    for iss in issues:
        f = iss.get("fields", {})
        rack = _extract_rack_num(f)
        if rack is not None:
            by_rack[rack].append(iss)
        else:
            no_rack.append(iss)

    # Sort racks by ticket count descending
    sorted_racks = sorted(by_rack.items(), key=lambda x: len(x[1]), reverse=True)

    if json_mode:
        out = []
        for rack_num, tickets in sorted_racks:
            out.append({
                "rack": f"R{rack_num}",
                "count": len(tickets),
                "tickets": [
                    {
                        "key": t["key"],
                        "status": t.get("fields", {}).get("status", {}).get("name", "?"),
                        "summary": t.get("fields", {}).get("summary", "")[:60],
                        "assignee": (t.get("fields", {}).get("assignee") or {}).get("displayName"),
                    }
                    for t in tickets
                ],
            })
        if no_rack:
            out.append({
                "rack": "Unknown",
                "count": len(no_rack),
                "tickets": [
                    {
                        "key": t["key"],
                        "status": t.get("fields", {}).get("status", {}).get("name", "?"),
                        "summary": t.get("fields", {}).get("summary", "")[:60],
                        "assignee": (t.get("fields", {}).get("assignee") or {}).get("displayName"),
                    }
                    for t in no_rack
                ],
            })
        print(json.dumps(out, indent=2))
        return

    # Rich table output
    site_label = site or "All Sites"
    status_label = status_filter.title()
    console.print(f"\n  [bold]Rack Report[/bold]  [dim]{site_label} · {status_label} · {project}[/dim]\n")

    table = Table(box=rich_box.SIMPLE_HEAVY, show_header=True, padding=(0, 1))
    table.add_column("#", style="dim", width=4, justify="right")
    table.add_column("Rack", style="bold cyan", width=6)
    table.add_column("Tickets", justify="right", width=8)
    table.add_column("Bar", width=30)
    table.add_column("Top Ticket", style="dim", width=40)

    max_count = sorted_racks[0][1].__len__() if sorted_racks else 1

    for i, (rack_num, tickets) in enumerate(sorted_racks, 1):
        count = len(tickets)
        bar_len = max(1, int(28 * count / max_count))
        bar = Text("█" * bar_len, style="green" if count <= 3 else "yellow" if count <= 6 else "red")
        top = tickets[0]
        top_label = f"{top['key']}  {top.get('fields', {}).get('summary', '')[:30]}"
        table.add_row(str(i), f"R{rack_num}", str(count), bar, top_label)

    console.print(table)

    if no_rack:
        console.print(f"\n  [dim]{len(no_rack)} ticket(s) with no rack identified[/dim]")

    total = sum(len(t) for t in by_rack.values()) + len(no_rack)
    console.print(f"\n  [bold]{total}[/bold] tickets across [bold]{len(by_rack)}[/bold] racks\n")
