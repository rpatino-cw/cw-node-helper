"""Shift brief — AI-generated priority summary from live Jira queue."""
from __future__ import annotations

import os
import textwrap
from datetime import datetime, timezone

import requests

from cwhelper import config as _cfg
from cwhelper.config import (
    BOLD, DIM, RESET, GREEN, YELLOW, CYAN, RED,
)
from cwhelper.services.search import _search_queue

__all__ = ["run_shift_brief"]

_ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
_BRIEF_MODEL = "claude-haiku-4-5-20251001"
_BRIEF_MAX_TOKENS = 1024

_BRIEF_FIELDS = [
    "summary", "status", "assignee", "priority", "updated",
    "customfield_10193",  # service_tag
    "customfield_10192",  # hostname
    "customfield_10194",  # site
    "customfield_10207",  # rack_location
]


# ---------------------------------------------------------------------------
# Data fetch
# ---------------------------------------------------------------------------

def _fetch_brief_queue(email: str, token: str, site: str = "",
                       mine_first: bool = False) -> tuple[list, list]:
    """Fetch open DO + HO tickets for the shift brief. Fresh data, no cache.

    If mine_first=True, fetches assigned tickets separately and puts them
    at the top of the list so Claude sees them with higher priority context.

    Returns (open_issues, radar_issues) — radar is HO tickets in pre-DO
    statuses that haven't yet spawned a DO.
    """
    results = []

    if mine_first:
        # Assigned tickets first (Claude will see them at top of prompt)
        for project in ("DO", "HO"):
            try:
                mine = _search_queue(
                    site, email, token,
                    mine_only=True,
                    limit=20,
                    status_filter="open",
                    project=project,
                    use_cache=False,
                )
                results.extend(mine)
            except Exception:
                pass

    # All open tickets (deduped below)
    seen = {i["key"] for i in results}
    for project, limit in (("DO", 40), ("HO", 20)):
        try:
            issues = _search_queue(
                site, email, token,
                mine_only=False,
                limit=limit,
                status_filter="open",
                project=project,
                use_cache=False,
            )
            for iss in issues:
                if iss["key"] not in seen:
                    seen.add(iss["key"])
                    results.append(iss)
        except Exception:
            pass

    # Radar: HO tickets in pre-DO statuses (may overlap with open HO above)
    radar = []
    try:
        radar_issues = _search_queue(
            site, email, token,
            mine_only=False,
            limit=20,
            status_filter="radar",
            project="HO",
            use_cache=False,
        )
        radar_seen = set()
        for iss in radar_issues:
            k = iss["key"]
            if k not in radar_seen:
                radar_seen.add(k)
                radar.append(iss)
                # Also add to main list if not already there
                if k not in seen:
                    seen.add(k)
                    results.append(iss)
    except Exception:
        pass

    return results, radar


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------

def _unwrap(val) -> str:
    """Safely extract string from Jira field (None, str, or dict)."""
    if val is None:
        return ""
    if isinstance(val, dict):
        return val.get("value") or val.get("name") or val.get("displayName") or ""
    return str(val)


def _format_tickets_for_prompt(issues: list) -> str:
    """Compact table for the Claude prompt."""
    lines = []
    for iss in issues:
        key = iss.get("key", "?")
        f = iss.get("fields", {})
        summary  = (f.get("summary") or "").strip()[:72]
        status   = (f.get("status") or {}).get("name", "?")
        assignee = (f.get("assignee") or {}).get("displayName", "Unassigned")
        priority = (f.get("priority") or {}).get("name", "")
        svc_tag  = _unwrap(f.get("customfield_10193"))
        hostname = _unwrap(f.get("customfield_10192"))
        rack     = _unwrap(f.get("customfield_10207"))
        updated  = (f.get("updated") or "")[:10]

        node_id  = svc_tag or hostname or "—"
        location = rack.split(".")[-2] + "." + rack.split(".")[-1] if "." in rack else rack or "—"

        lines.append(
            f"{key:<12} | {status:<22} | {priority:<8} | {assignee:<20} | "
            f"{node_id:<12} | {location:<14} | {updated} | {summary}"
        )
    return "\n".join(lines) if lines else "(no open tickets found)"


def _build_prompt(tickets_text: str, site: str = "",
                  mine_first: bool = False,
                  radar_text: str = "") -> str:
    site_label = site or "all sites"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    mine_note = (
        "\nNote: tickets assigned to you appear at the TOP of the list. "
        "Prioritize those unless a more urgent unassigned ticket warrants attention.\n"
        if mine_first else ""
    )
    radar_section = ""
    if radar_text:
        radar_section = f"""

RADAR — HO tickets in pre-DO statuses (work coming soon, no DO yet):
KEY          | STATUS                 | PRIORITY | ASSIGNEE             | NODE         | RACK          | UPDATED    | SUMMARY
{radar_text}

Status meanings:
- "Sent to DCT UC" = uncable DO imminent
- "Sent to DCT RC" = recable DO imminent
- "RMA-initiate" = RMA swap DO coming soon
- "Awaiting Parts" = DO when parts arrive
"""
    return f"""You are generating a shift brief for a CoreWeave Data Center Technician (DCT) at {site_label}.

DCT context:
- DO tickets = hands-on physical work (reseat, swap, cable, power cycle). DCTs execute these on the floor.
- HO tickets = hardware problem tracker, RMA lifecycle. Linked to DO tickets.
- DCTs walk to racks — first-move recommendations should be physical and specific.
{mine_note}
Current time: {now}

Live Jira queue (DO + HO, all open/in-progress):
KEY          | STATUS                 | PRIORITY | ASSIGNEE             | NODE         | RACK          | UPDATED    | SUMMARY
{tickets_text}
{radar_section}
Generate a concise shift brief using exactly this format:

**Priority Tickets** (top 3 most urgent — one sentence each on why):
1. [KEY] — [reason]
2. [KEY] — [reason]
3. [KEY] — [reason]

**Incoming Work** (HO tickets about to spawn DOs — what to prepare for, omit if no radar tickets):
- [HO KEY] — [procedure type, rack, what to stage/bring]

**Watch List** (nodes in multiple tickets, or repeated failure patterns — omit if none):
- [node or pattern]: [1 sentence]

**Suggested First Move**: [1 sentence — specific, physical, where to walk and what to check first]

Rules:
- Use real ticket keys from the list above only
- Be direct and specific — no padding
- If fewer than 3 clearly urgent tickets exist, list what's there and say so
- If queue is empty, say "Queue is clear — no open tickets"
- For Incoming Work, infer the procedure type from the HO status and tell the DCT what to stage
"""


# ---------------------------------------------------------------------------
# Anthropic API call
# ---------------------------------------------------------------------------

def _call_anthropic(prompt: str) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return (
            "  ANTHROPIC_API_KEY not set.\n"
            "  Add it to ~/.config/keys/global.env:\n"
            "    ANTHROPIC_API_KEY=sk-ant-...\n\n"
            "  Get a key: https://console.anthropic.com/settings/keys"
        )

    try:
        resp = requests.post(
            _ANTHROPIC_API_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": _BRIEF_MODEL,
                "max_tokens": _BRIEF_MAX_TOKENS,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
    except requests.exceptions.Timeout:
        return "  Claude API timed out (30s). Check your connection."
    except requests.exceptions.RequestException as exc:
        return f"  Claude API request failed: {exc}"

    if resp.status_code == 401:
        return "  Anthropic API key rejected (401). Check ANTHROPIC_API_KEY."
    if resp.status_code == 529:
        return "  Anthropic API overloaded (529). Try again in a moment."

    try:
        data = resp.json()
    except Exception:
        data = {}

    # Anthropic returns 400 for low credit balance (not 402)
    if resp.status_code == 400:
        msg = data.get("error", {}).get("message", "")
        if "credit" in msg.lower() or "billing" in msg.lower():
            return (
                "  Anthropic API credits exhausted.\n"
                "  Top up at: https://console.anthropic.com/settings/billing"
            )
        return f"  Anthropic API error (400): {msg or resp.text[:200]}"

    try:
        resp.raise_for_status()
        return data["content"][0]["text"]
    except Exception as exc:
        return f"  Unexpected Claude response ({resp.status_code}): {exc}"


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def _print_brief(text: str) -> None:
    """Print the Claude response with light ANSI formatting."""
    width = 70
    sep = f"  {DIM}{'─' * width}{RESET}"
    print(sep)
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            print()
            continue
        # Bold section headers: **Foo Bar**
        if stripped.startswith("**") and stripped.endswith("**"):
            label = stripped.strip("*")
            print(f"\n  {BOLD}{label}{RESET}")
        elif stripped.startswith("**"):
            # Inline bold at start of line — strip markdown markers
            clean = stripped.replace("**", "")
            print(f"\n  {BOLD}{clean}{RESET}")
        elif stripped[:2] in ("1.", "2.", "3."):
            print(f"  {CYAN}{stripped}{RESET}")
        elif stripped.startswith("-") or stripped.startswith("•"):
            print(f"  {YELLOW}{stripped}{RESET}")
        else:
            # Wrap long lines
            wrapped = textwrap.fill(
                stripped, width=width,
                initial_indent="  ", subsequent_indent="    "
            )
            print(wrapped)
    print(sep)
    print()


# ---------------------------------------------------------------------------
# Demo mode — realistic mock data, no credentials required
# ---------------------------------------------------------------------------

_DEMO_TICKETS_TEXT = """\
DO-48823     | In Progress          | High     | Romeo Patino         | 10PQR847    | R04.U19       | 2026-03-09 | GPU node hard down — NVLink errors, node offline during active job
HO-23847     | Radar                | High     | Fleet Ops            | 10PQR847    | R04.U19       | 2026-03-09 | GPU node RMA — FD logs submitted, awaiting vendor RMA number
DO-48991     | Verification         | Medium   | Romeo Patino         | 10NMK823    | R09.U14       | 2026-03-07 | Memory DIMM swap verification — customer SLA window active
DO-49201     | Open                 | High     | Unassigned           | 10MRX994    | R12.U06       | 2026-03-08 | PSU replacement — parts received, needs scheduling
DO-49102     | Open                 | Medium   | Unassigned           | 10NQT291    | R07.U22       | 2026-03-09 | IB link flap on QM9790 port 14 — 3 nodes impacted
HO-23901     | In Progress          | High     | FRR Team             | 10NQT291    | R07.U22       | 2026-03-09 | IB QM9790 fabric investigation — R07 backbone link affected
DO-49089     | Open                 | Medium   | Unassigned           | 10PLM102    | R11.U08       | 2026-03-08 | Server accessible via OOB only — no SSH, no console
DO-49310     | Open                 | Low      | Unassigned           | 10QRS441    | R02.U31       | 2026-03-08 | Cable inspection — loose SFP port flagged by Grafana alert"""

_DEMO_BRIEF = """\
**Priority Tickets**
1. DO-48823 — Your ticket: GPU node 10PQR847 at R04.U19 is actively down with NVLink errors and has a linked RMA (HO-23847) already open — head there first, the node is impacting live jobs.
2. DO-48991 — Your verification ticket is 52 hours overdue; close or escalate before end of shift — this is inside SLA breach window with an active customer.
3. DO-49201 — PSU replacement parts marked received yesterday at R12.U06; confirm delivery with receiving and schedule the swap today.

**Watch List**
- 10PQR847 (R04.U19): Flagged in DO-48823 (active, down) and HO-23847 (RMA open) — recurring power/GPU instability. Treat as high-risk. Do not clear without FRR sign-off.
- 10NQT291 (R07.U22): DO-49102 (IB link flap) and HO-23901 (FRR fabric investigation) — IB fabric issue may be multi-node. Coordinate with FRR before pulling any cables in R07.

**Suggested First Move**: Walk to R04.U19 — DO-48823 is your ticket, the node is down, and the RMA is already open. Pull iDRAC logs and confirm the GPU failure before touching anything else."""


def _run_demo_brief(site: str = "US-CENTRAL-07A") -> None:
    """Demo mode: realistic mock data, no credentials required."""
    import time

    site_label = site or "US-CENTRAL-07A"

    print(f"\n  {DIM}[DEMO MODE] Fetching open queue...{RESET}", end="", flush=True)
    time.sleep(1.2)
    print(f"\r{'':55}\r", end="")

    print(f"\n  {BOLD}Shift Brief{RESET}  {DIM}—  8 open tickets  "
          f"(6 DO · 2 HO)  ·  {site_label}{RESET}")
    print(f"  {YELLOW}{DIM}[DEMO MODE — mock data]{RESET}\n")

    print(f"  {DIM}Asking Claude ({_BRIEF_MODEL})...{RESET}", end="", flush=True)
    time.sleep(2.0)
    print(f"\r{'':55}\r", end="")

    _print_brief(_DEMO_BRIEF)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_shift_brief(email: str, token: str, site: str = "US-CENTRAL-07A",
                    mine_first: bool = False, demo: bool = False) -> None:
    """Fetch queue, call Claude, print shift brief. Called from CLI and menu."""
    if demo:
        _run_demo_brief(site=site)
        return

    print(f"\n  {DIM}Fetching open queue...{RESET}", end="", flush=True)
    issues, radar = _fetch_brief_queue(email, token, site=site, mine_first=mine_first)
    print(f"\r{'':55}\r", end="")

    do_count = sum(1 for i in issues if i.get("key", "").startswith("DO"))
    ho_count = sum(1 for i in issues if i.get("key", "").startswith("HO"))
    total    = do_count + ho_count
    radar_count = len(radar)

    site_label = site or "all sites"
    radar_label = f" · {YELLOW}{radar_count} radar{RESET}{DIM}" if radar_count else ""
    print(f"\n  {BOLD}Shift Brief{RESET}  {DIM}—  {total} open tickets  "
          f"({do_count} DO · {ho_count} HO{radar_label})  ·  {site_label}{RESET}\n")

    if not issues:
        print(f"  {GREEN}Queue is clear — no open tickets at {site_label}.{RESET}\n")
        return

    tickets_text = _format_tickets_for_prompt(issues)
    radar_text = _format_tickets_for_prompt(radar) if radar else ""
    prompt = _build_prompt(tickets_text, site=site, mine_first=mine_first,
                           radar_text=radar_text)

    print(f"  {DIM}Asking Claude ({_BRIEF_MODEL})...{RESET}", end="", flush=True)
    result = _call_anthropic(prompt)
    print(f"\r{'':55}\r", end="")

    _print_brief(result)
