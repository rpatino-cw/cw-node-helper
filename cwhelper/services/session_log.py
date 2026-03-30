"""Session activity log — detailed, permanent, rolling storage.

Storage: ~/.cwhelper/activity.jsonl  (JSONL — one event per line)
Limit:   3000 entries max; trimmed to 2000 when exceeded.
Session: identified by the process start timestamp (_SESSION_START).
"""
from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import time

from cwhelper.config import (
    BOLD, DIM, RESET, GREEN, YELLOW, CYAN, BLUE, RED, WHITE, MAGENTA,
)

__all__ = ['_log_event', '_print_session_log', '_SESSION_START', '_build_work_summary', '_copy_session_to_clipboard', '_print_jira_activity']

# Permanent storage — outside the project so it survives git cleans / updates
_LOG_DIR  = os.path.expanduser("~/.cwhelper")
_LOG_FILE = os.path.join(_LOG_DIR, "activity.jsonl")

_MAX_ENTRIES  = 3000   # trim when we hit this
_KEEP_ENTRIES = 2000   # keep this many after trim

# Set once at import — uniquely identifies this run
_SESSION_START: float = time.time()

# Event labels and colors for display
_STYLES: dict[str, tuple[str, str]] = {
    "view":       (DIM,     "Viewed"),
    "grab":       (CYAN,    "Grabbed"),
    "start":      (GREEN,   "Started → In Progress"),
    "verify":     (BLUE,    "→ Verification"),
    "close":      (GREEN,   "Closed"),
    "hold":       (YELLOW,  "→ On Hold"),
    "reopen":     (YELLOW,  "Reopened"),
    "comment":    (DIM,     "Comment posted"),
    "transition": (CYAN,    "Transitioned"),
    "queue":      (DIM,     "Browsed queue"),
    "bulk_start": (GREEN,   "Bulk started"),
    "ai_chat":    (MAGENTA, "AI chat"),
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ensure_dir():
    os.makedirs(_LOG_DIR, mode=0o700, exist_ok=True)


def _read_all() -> list[dict]:
    """Read all JSONL entries. Silently skips malformed lines."""
    if not os.path.exists(_LOG_FILE):
        return []
    entries = []
    try:
        with open(_LOG_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except OSError:
        pass
    return entries


def _write_all(entries: list[dict]):
    _ensure_dir()
    try:
        with open(_LOG_FILE, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e, separators=(",", ":")) + "\n")
        os.chmod(_LOG_FILE, 0o600)
    except OSError:
        pass


def _append(entry: dict):
    """Append one entry, trimming the file if it exceeds _MAX_ENTRIES."""
    _ensure_dir()
    # Fast path: just append
    try:
        with open(_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")
    except OSError:
        return
    # Check if we need to trim (only when near the limit)
    try:
        count = sum(1 for _ in open(_LOG_FILE, encoding="utf-8"))
        if count > _MAX_ENTRIES:
            all_entries = _read_all()
            _write_all(all_entries[-_KEEP_ENTRIES:])
    except OSError:
        pass


def _fmt_ts(ts: float, full: bool = False) -> str:
    dt = datetime.datetime.fromtimestamp(ts)
    return dt.strftime("%Y-%m-%d %H:%M:%S") if full else dt.strftime("%H:%M:%S")


def _fmt_date(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%a %b %d")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _log_event(event_type: str, key: str = "", summary: str = "",
               detail: str = "", ctx: dict = None, chat_log: list = None):
    """Append one event to the permanent log."""
    entry: dict = {
        "ts":      time.time(),
        "session": _SESSION_START,
        "type":    event_type,
        "key":     key,
        "summary": summary[:100],
        "detail":  detail[:120],
    }
    if ctx:
        entry["service_tag"]   = ctx.get("service_tag", "")
        entry["site"]          = ctx.get("site", "")
        entry["rack_location"] = ctx.get("rack_location", "")
        entry["status"]        = ctx.get("status", "")
        entry["assignee"]      = ctx.get("assignee", "")
    if chat_log:
        entry["chat_log"] = chat_log
    _append(entry)


def _print_session_log(show_all: bool = False):
    """Display the session log. show_all=True shows full history."""
    all_entries = _read_all()

    if show_all:
        entries = all_entries
        header_label = f"Full Activity Log  {DIM}({len(all_entries)} entries total){RESET}"
    else:
        entries = [e for e in all_entries if e.get("session") == _SESSION_START]
        header_label = (
            f"Session Log  {DIM}started {_fmt_ts(_SESSION_START, full=True)}"
            f"  —  {len(entries)} events this session"
            f"  ({len(all_entries)} total){RESET}"
        )

    print(f"\n  {BOLD}{header_label}")
    print(f"  {'─' * 60}")

    if not entries:
        print(f"\n  {DIM}No activity yet.{RESET}\n")
        _print_log_footer(all_entries, show_all)
        return

    prev_date  = None
    prev_key   = None

    for ev in entries:
        ts       = ev.get("ts", 0)
        ev_type  = ev.get("type", "?")
        key      = ev.get("key", "")
        summary  = ev.get("summary", "")
        detail   = ev.get("detail", "")
        site     = ev.get("site", "")
        rack     = ev.get("rack_location", "")
        tag      = ev.get("service_tag", "")
        status   = ev.get("status", "")

        color, label = _STYLES.get(ev_type, (DIM, ev_type))

        # Date divider (full history view)
        date_str = _fmt_date(ts)
        if show_all and date_str != prev_date:
            print(f"\n  {BOLD}{date_str}{RESET}  {DIM}{'─' * 40}{RESET}")
            prev_date = date_str
            prev_key  = None

        # Ticket header when key changes
        if key and key != prev_key:
            meta_parts = []
            if tag:
                meta_parts.append(f"{DIM}SN:{RESET} {tag}")
            if site:
                meta_parts.append(f"{DIM}Site:{RESET} {site}")
            if rack:
                # Shorten rack: "US-SITE01.DH1.R257.RU22" → "DH1 R257 RU22"
                # Strip known site prefixes to show just DH/rack/RU
                _rp = re.sub(r"^US-[A-Z0-9-]+\.", "", rack)
                meta_parts.append(f"{DIM}Rack:{RESET} {_rp}")
            meta_str = f"   {DIM}│{RESET}   ".join(meta_parts)
            print(f"\n  {CYAN}{BOLD}{key}{RESET}   {WHITE}{summary}{RESET}")
            if meta_str:
                print(f"  {DIM}{meta_str}{RESET}")
            prev_key = key

        # Event line
        ts_str = _fmt_ts(ts)
        detail_str = f"   {DIM}{detail}{RESET}" if detail else ""
        print(f"    {DIM}{ts_str}{RESET}   {color}{label}{RESET}{detail_str}")

        # AI chat: show the conversation inline
        if ev_type == "ai_chat":
            chat_log = ev.get("chat_log", [])
            for msg in chat_log:
                role = msg.get("role", "")
                content = msg.get("content", "")
                if role == "user":
                    print(f"        {GREEN}You:{RESET} {content[:200]}")
                elif role == "assistant":
                    lines_ai = content[:400]
                    print(f"        {MAGENTA}AI:{RESET}  {lines_ai}")

    _print_log_footer(all_entries, show_all)


def _print_log_footer(all_entries: list, show_all: bool):
    session_entries = [e for e in all_entries if e.get("session") == _SESSION_START]
    ticket_keys     = {e["key"] for e in session_entries if e.get("key") and e["type"] != "queue"}
    oldest          = all_entries[0].get("ts", 0) if all_entries else 0

    print(f"\n  {DIM}{'─' * 60}{RESET}")
    if oldest:
        print(f"  {DIM}Log history from {_fmt_ts(oldest, full=True)}  "
              f"·  {len(all_entries)}/{_MAX_ENTRIES} entries stored{RESET}")
    print(f"  {DIM}This session: {len(session_entries)} events  ·  {len(ticket_keys)} tickets touched{RESET}")
    print()
    print(f"  {BOLD}c{RESET} Copy to clipboard   "
          f"{BOLD}{'h' if not show_all else 's'}{RESET} "
          f"{'Full history' if not show_all else 'Session only'}   "
          f"{BOLD}f{RESET} AI feedback on my work   "
          f"{BOLD}ENTER{RESET} Return to menu")
    print()


def _build_work_summary(show_all: bool = False) -> str:
    """Build a structured plain-text work summary for AI analysis.

    Computes per-ticket state timelines, time-in-state deltas,
    throughput stats, and ticket type breakdown.
    """
    all_entries = _read_all()
    if show_all:
        entries = all_entries
        scope = "full history"
    else:
        entries = [e for e in all_entries if e.get("session") == _SESSION_START]
        scope = f"session started {_fmt_ts(_SESSION_START, full=True)}"

    if not entries:
        return f"No activity recorded ({scope})."

    # Group events by ticket key
    from collections import defaultdict
    tickets: dict[str, list[dict]] = defaultdict(list)
    general_events: list[dict] = []
    for ev in entries:
        key = ev.get("key", "")
        if key and ev.get("type") not in ("queue",):
            tickets[key].append(ev)
        else:
            general_events.append(ev)

    lines = [
        f"WORK SUMMARY — {scope}",
        f"Total events: {len(entries)}",
        f"Tickets touched: {len(tickets)}",
        "",
    ]

    # Per-ticket timelines
    STATE_ORDER = ["grab", "start", "verify", "close"]
    ticket_stats = []
    for key, evs in tickets.items():
        evs_sorted = sorted(evs, key=lambda e: e.get("ts", 0))
        first = evs_sorted[0]
        summary = first.get("summary", "")
        tag = first.get("service_tag", "")
        site = first.get("site", "")
        rack = first.get("rack_location", "")

        # Build state timestamps
        state_ts: dict[str, float] = {}
        for ev in evs_sorted:
            t = ev.get("type", "")
            if t in STATE_ORDER and t not in state_ts:
                state_ts[t] = ev.get("ts", 0)

        # Compute deltas
        deltas = []
        prev_state = None
        prev_ts = None
        final_state = None
        for s in STATE_ORDER:
            if s in state_ts:
                if prev_ts:
                    delta_min = (state_ts[s] - prev_ts) / 60
                    deltas.append(f"{prev_state}→{s}: {delta_min:.0f}m")
                prev_state = s
                prev_ts = state_ts[s]
                final_state = s

        # Infer ticket type from summary
        ticket_type = "unknown"
        summary_lower = summary.lower()
        for t in ["power_cycle", "recable", "reseat", "swap", "uncable",
                  "dpu_port_clean", "network", "inspection", "rma", "device"]:
            if t.replace("_", " ") in summary_lower or t in summary_lower:
                ticket_type = t.replace("_", " ").upper()
                break

        stat = {
            "key": key,
            "summary": summary,
            "type": ticket_type,
            "tag": tag,
            "site": site,
            "rack": rack,
            "states": list(state_ts.keys()),
            "final_state": final_state,
            "deltas": deltas,
            "num_comments": sum(1 for e in evs_sorted if e.get("type") == "comment"),
        }
        ticket_stats.append(stat)

        # Format for output
        rack_short = re.sub(r"^US-[A-Z0-9-]+\.", "", rack)
        lines.append(f"TICKET: {key}  [{ticket_type}]")
        if summary:
            lines.append(f"  Summary: {summary[:80]}")
        if tag or rack_short:
            lines.append(f"  Node: {tag or '?'}  Rack: {rack_short or '?'}  Site: {site or '?'}")
        lines.append(f"  States reached: {' → '.join(stat['states']) if stat['states'] else '(viewed only)'}")
        if deltas:
            lines.append(f"  Time between states: {',  '.join(deltas)}")
        if stat["num_comments"] > 0:
            lines.append(f"  Comments posted: {stat['num_comments']}")
        lines.append("")

    # Aggregate stats
    closed_tickets = [s for s in ticket_stats if "close" in s["states"]]
    verified_tickets = [s for s in ticket_stats if "verify" in s["states"]]
    started_tickets = [s for s in ticket_stats if "start" in s["states"]]
    grabbed_tickets = [s for s in ticket_stats if "grab" in s["states"]]

    # Type breakdown
    type_counts: dict[str, int] = defaultdict(int)
    for s in ticket_stats:
        type_counts[s["type"]] += 1

    lines.append("AGGREGATE STATS:")
    lines.append(f"  Grabbed:  {len(grabbed_tickets)}")
    lines.append(f"  Started:  {len(started_tickets)}")
    lines.append(f"  Verified: {len(verified_tickets)}")
    lines.append(f"  Closed:   {len(closed_tickets)}")
    lines.append("")

    if type_counts:
        lines.append("TICKET TYPE BREAKDOWN:")
        for t, count in sorted(type_counts.items(), key=lambda x: -x[1]):
            lines.append(f"  {t}: {count}")
        lines.append("")

    # Average time to verify (grab → verify or start → verify)
    verify_times = []
    for s in ticket_stats:
        evs_sorted = sorted(tickets[s["key"]], key=lambda e: e.get("ts", 0))
        state_ts = {}
        for ev in evs_sorted:
            t = ev.get("type", "")
            if t in STATE_ORDER and t not in state_ts:
                state_ts[t] = ev.get("ts", 0)
        if "verify" in state_ts:
            anchor = state_ts.get("start") or state_ts.get("grab")
            if anchor:
                verify_times.append((state_ts["verify"] - anchor) / 60)

    close_times = []
    for s in ticket_stats:
        evs_sorted = sorted(tickets[s["key"]], key=lambda e: e.get("ts", 0))
        state_ts = {}
        for ev in evs_sorted:
            t = ev.get("type", "")
            if t in STATE_ORDER and t not in state_ts:
                state_ts[t] = ev.get("ts", 0)
        if "close" in state_ts:
            anchor = state_ts.get("start") or state_ts.get("grab")
            if anchor:
                close_times.append((state_ts["close"] - anchor) / 60)

    if verify_times:
        avg_v = sum(verify_times) / len(verify_times)
        lines.append(f"Avg time to verification: {avg_v:.0f} min")
    if close_times:
        avg_c = sum(close_times) / len(close_times)
        lines.append(f"Avg time to close: {avg_c:.0f} min")

    # Session duration
    session_evs = [e for e in entries if e.get("session") == _SESSION_START]
    if len(session_evs) >= 2:
        dur_min = (session_evs[-1]["ts"] - session_evs[0]["ts"]) / 60
        lines.append(f"Session duration so far: {dur_min:.0f} min")

    return "\n".join(lines)


def _print_jira_activity(email: str, token: str):
    """Fetch and display tickets the current user transitioned today via Jira changelog API."""
    from cwhelper.clients.jira import _jira_get, _jira_post, _get_my_account_id

    print(f"\n  {DIM}Fetching your Jira activity today...{RESET}")

    account_id = _get_my_account_id(email, token)
    if not account_id:
        print(f"\n  {RED}Could not fetch your Jira account ID.{RESET}\n")
        input("  Press ENTER to return...")
        return

    today = datetime.datetime.now().strftime("%Y-%m-%d")
    jql = f'project in ("DO","HO","SDA") AND updated >= "{today}" ORDER BY updated DESC'

    # Step 1: get issue keys — use same endpoint as the rest of the app
    try:
        resp = _jira_post("/rest/api/3/search/jql", email, token, body={
            "jql": jql,
            "maxResults": 50,
            "fields": ["summary", "status", "updated"],
        })
    except Exception as e:
        print(f"\n  {RED}Request error: {e}{RESET}\n")
        input("  Press ENTER to return...")
        return

    if not resp or not resp.ok:
        print(f"\n  {RED}Jira request failed ({getattr(resp, 'status_code', '?')}).{RESET}\n")
        input("  Press ENTER to return...")
        return

    issues = resp.json().get("issues", [])
    if not issues:
        print(f"\n  {DIM}No tickets updated today.{RESET}\n")
        input("  Press ENTER to return...")
        return

    # Step 2: fetch changelog per issue, filter to current user's transitions
    my_tickets = []
    print(f"  {DIM}Scanning {len(issues)} tickets for your transitions...{RESET}")
    for issue in issues:
        key     = issue.get("key", "")
        fields  = issue.get("fields", {})
        summary = fields.get("summary", "")
        status  = fields.get("status", {}).get("name", "")

        try:
            cl_resp = _jira_get(
                f"/rest/api/3/issue/{key}/changelog", email, token,
                params={"maxResults": 50}
            )
        except Exception:
            continue
        if not cl_resp or not cl_resp.ok:
            continue

        my_changes = []
        for history in cl_resp.json().get("values", []):
            if history.get("author", {}).get("accountId") != account_id:
                continue
            created = history.get("created", "")
            if not created.startswith(today):
                continue
            for item in history.get("items", []):
                if item.get("field") == "status":
                    my_changes.append({
                        "ts":   created[:16].replace("T", " "),
                        "from": item.get("fromString", ""),
                        "to":   item.get("toString", ""),
                    })

        if my_changes:
            my_tickets.append({
                "key": key, "summary": summary,
                "status": status, "changes": my_changes,
            })

    print(f"\n  {BOLD}My Jira Activity Today{RESET}  {DIM}(changelog — includes browser activity){RESET}")
    print(f"  {'─' * 60}")

    if not my_tickets:
        print(f"\n  {DIM}No status transitions found for you today.{RESET}\n")
        input("  Press ENTER to return...")
        return

    for t in my_tickets:
        print(f"\n  {CYAN}{BOLD}{t['key']}{RESET}   {WHITE}{t['summary'][:70]}{RESET}")
        print(f"  {DIM}Current: {t['status']}{RESET}")
        for ch in t["changes"]:
            print(f"    {DIM}{ch['ts']}{RESET}   {ch['from']} → {GREEN}{ch['to']}{RESET}")

    print(f"\n  {DIM}{'─' * 60}{RESET}")
    print(f"  {DIM}{len(my_tickets)} ticket(s) with your transitions today{RESET}\n")
    input("  Press ENTER to return...")


def _copy_session_to_clipboard():
    """Format current session as plain text and copy to clipboard."""
    all_entries  = _read_all()
    session_entries = [e for e in all_entries if e.get("session") == _SESSION_START]

    if not session_entries:
        return False

    lines = [
        f"cwhelper Session Log — {_fmt_ts(_SESSION_START, full=True)}",
        "=" * 60,
    ]
    prev_key = None
    for ev in session_entries:
        ts      = ev.get("ts", 0)
        ev_type = ev.get("type", "?")
        key     = ev.get("key", "")
        summary = ev.get("summary", "")
        detail  = ev.get("detail", "")
        tag     = ev.get("service_tag", "")
        rack    = ev.get("rack_location", "")
        _, label = _STYLES.get(ev_type, (DIM, ev_type))

        if key and key != prev_key:
            lines.append(f"\n{key}  {summary}")
            if tag:
                lines.append(f"  Service Tag: {tag}")
            if rack:
                lines.append(f"  Rack: {rack}")
            prev_key = key

        detail_str = f" — {detail}" if detail else ""
        lines.append(f"  {_fmt_ts(ts)}  {label}{detail_str}")

    text = "\n".join(lines) + "\n"
    try:
        subprocess.run(["pbcopy"], input=text.encode(), check=True)
        return True
    except Exception:
        return False
