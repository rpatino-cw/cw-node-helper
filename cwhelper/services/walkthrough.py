"""Walkthrough mode — free-form rack annotation with issue templates, carryover, and checklist."""
from __future__ import annotations

import csv
import os
import re as _re
import subprocess
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta

from cwhelper.config import BOLD, CYAN, DIM, GREEN, KNOWN_SITES, RED, RESET, YELLOW
__all__ = [
    '_walkthrough_pick_site_dh', '_walkthrough_annotate_device',
    '_walkthrough_save_notes', '_walkthrough_resume_prompt',
    '_walkthrough_export', '_walkthrough_mode',
]
from cwhelper.clients.netbox import _netbox_get_rack_devices, _netbox_get
from cwhelper.state import _save_user_state
from cwhelper.cache import _brief_pause
from cwhelper.tui.display import _clear_screen
try:
    from cwhelper.services.queue import _search_node_history
    from cwhelper.clients.jira import _post_comment
except ImportError:
    _search_node_history = None  # type: ignore[assignment]
    _post_comment = None  # type: ignore[assignment]


# ── Constants ─────────────────────────────────────────────────────────────────

# One local report at a time — always overwritten on finish
_WALKTHROUGH_REPORT_PATH = os.path.expanduser("~/.cwhelper_walkthrough_report.txt")

_ISSUE_TEMPLATE_GROUPS = [
    ("Hardware", [
        ("1",  "LED issue",         "amber/red LED, no obvious cause"),
        ("2",  "Power issue",       "PSU fault, no power, power cycling"),
        ("3",  "Drive failure",     "drive missing, failed, or not detected"),
        ("4",  "Dead node",         "unresponsive, POST fail, won't boot"),
    ]),
    ("Management / Connectivity", [
        ("5",  "BMC / iDRAC down",  "BMC unreachable, can't remote manage"),
        ("6",  "IB / Network",      "IBP down, link flap, mgmt network unreachable"),
    ]),
    ("Environment", [
        ("7",  "Cabling issue",     "loose, missing, or misrouted cable"),
        ("8",  "CDU down",          "cooling unit offline or leaking"),
        ("9",  "Thermal",           "high temps, fan failure, airflow issue"),
        ("10", "Garbage/debris",    "trash, packaging, blockage in/around rack"),
    ]),
    ("Other", [
        ("11", "Custom",            "enter your own note"),
    ]),
]

# Flat list for lookup — keep in sync with groups above
_ISSUE_TEMPLATES = [t for _, items in _ISSUE_TEMPLATE_GROUPS for t in items]
_CUSTOM_KEY = "11"

# Matches the CoreWeave Slack walkthrough form
_CHECKLIST = [
    ("jira_queue",        "Checked Jira Queue",                        "yn"),
    ("entry_secured",     "Data Hall Entry Secured",                   "yn"),
    ("temp_airflow",      "Temperature and Air Flow Stable",           "yn"),
    ("trash_out",         "Trash, Cardboard and Debris Out of DH",     "yn"),
    ("dh_clean",          "Data Hall Cleaned and Organized",           "yn"),
    ("carts_tidy",        "Crash Cart, Tools, and Carts Tidy",         "yn"),
    ("power_strip_alarm", "Power Strip Alarm",                         "yn_unit"),
    ("rpp_alarm",         "RPP Alarm",                                 "yn_na_unit"),
    ("pdu_alarm",         "PDU Alarm",                                 "yn_unit"),
    ("crah_alarm",        "CRAH/CRACS/AHU Alarm",                      "yn_na_unit"),
    ("led_warnings",      "Any LEDs with Warning Lights on Gear",      "yn_unit"),
    ("unusual_smell",     "Any Unusual Smells",                        "yn"),
    ("smoking",           "Is Anything Smoking",                       "yn"),
    ("audible_alarms",    "Any Audible Alarms Going Off",              "yn"),
    ("out_of_ordinary",   "Anything Out of the Ordinary",              "yn"),
]

# Carryover status constants
_STATUS_PENDING    = "pending"
_STATUS_RESOLVED   = "resolved"
_STATUS_PERSISTENT = "persistent"
_STATUS_WORSENED   = "worsened"
_STATUS_SKIPPED    = "skipped"

# Timestamp format constants
_ISO_FMT     = "%Y-%m-%dT%H:%M:%SZ"
_DISPLAY_FMT = "%Y-%m-%d %H:%M UTC"

# Carryover disposition labels
_DISPOSITION_LABELS = {
    _STATUS_RESOLVED:   f"{GREEN}✓ Resolved{RESET}",
    _STATUS_PERSISTENT: f"{YELLOW}~ Persistent{RESET}",
    _STATUS_WORSENED:   f"{RED}↑ Worsened{RESET}",
    _STATUS_SKIPPED:    f"{DIM}  Skipped{RESET}",
    _STATUS_PENDING:    f"{YELLOW}⚠ Pending{RESET}",
}


def _count_carryover(carryover: list) -> dict:
    """Return counts of carryover items by status."""
    return {
        s: sum(1 for c in carryover if c.get("status") == s)
        for s in (_STATUS_PENDING, _STATUS_RESOLVED,
                  _STATUS_PERSISTENT, _STATUS_WORSENED, _STATUS_SKIPPED)
    }


# ── Banner ─────────────────────────────────────────────────────────────────────

def _walkthrough_banner(site_code: str, dh: str, notes: list, carryover: list = None):
    """Persistent header printed at the top of every walkthrough screen."""
    count   = len(notes)
    pending = sum(1 for c in (carryover or []) if c.get("status") == "pending")
    noun    = "annotation" if count == 1 else "annotations"

    print(f"\n  {BOLD}{GREEN}◉  WALKTHROUGH MODE{RESET}  {DIM}{'─' * 48}{RESET}")
    line2 = (f"  {BOLD}{site_code}{RESET}  {DIM}/{RESET}  {BOLD}{dh}{RESET}"
             f"  {DIM}│{RESET}  {CYAN}{BOLD}{count}{RESET} {DIM}{noun}{RESET}")
    if pending:
        line2 += f"  {DIM}│{RESET}  {YELLOW}{BOLD}{pending}{RESET} {DIM}unverified from last shift{RESET}"
    print(line2)
    print(f"  {DIM}{'─' * 68}{RESET}\n")


# ── Site / DH picker ──────────────────────────────────────────────────────────

def _walkthrough_pick_site_dh():
    """Prompt user to select a site and data hall. Returns (site_code, dh) or None."""
    print(f"\n  {BOLD}Select site:{RESET}")
    for i, s in enumerate(KNOWN_SITES, start=1):
        print(f"    {BOLD}{i}{RESET}. {s}")
    try:
        raw = input(f"\n  Site [1-{len(KNOWN_SITES)}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if not raw:
        return None
    try:
        idx = int(raw)
        if idx < 1 or idx > len(KNOWN_SITES):
            print(f"  {DIM}Invalid selection.{RESET}")
            return None
        site_code = KNOWN_SITES[idx - 1]
    except ValueError:
        print(f"  {DIM}Invalid selection.{RESET}")
        return None

    try:
        dh = input(f"  Data hall (e.g. DH1): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if not dh:
        return None
    return (site_code, dh)


# ── Session persistence ───────────────────────────────────────────────────────

def _walkthrough_save_notes(state: dict, notes: list, session: dict):
    """Persist walkthrough annotations and session to user state."""
    state["walkthrough_notes"]   = notes
    state["walkthrough_session"] = session
    _save_user_state(state)


def _walkthrough_resume_prompt(state: dict):
    """Check for existing session and offer to resume. Returns (notes, session)."""
    session = state.get("walkthrough_session")
    if not session:
        return ([], None)

    notes    = state.get("walkthrough_notes", [])
    site     = session.get("site_code", "?")
    dh       = session.get("dh", "?")
    started  = session.get("started_at", "?")
    last_rack = notes[-1].get("rack", "?") if notes else "none"
    print(f"\n  {BOLD}Previous walkthrough found:{RESET}")
    print(f"    {site} {dh} — started {started}")
    print(f"    {len(notes)} annotation(s) — last: {CYAN}{BOLD}{last_rack}{RESET}")
    try:
        raw = input(f"\n  Resume? [y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return ([], None)
    if raw == "y":
        return (notes, session)
    return ([], None)


# ── Legacy annotator (test compatibility) ─────────────────────────────────────

def _walkthrough_annotate_device(devices: list, rack: str):
    """Legacy annotator — kept for test compatibility."""
    racked = [d for d in devices if d.get("position") is not None]
    if not racked:
        return None

    print(f"\n  {BOLD}Devices in {rack}:{RESET}")
    for i, d in enumerate(racked, start=1):
        name   = d.get("name") or d.get("display") or "?"
        status = (d.get("status") or {}).get("label", "?")
        role   = (d.get("device_role") or {}).get("name", "")
        pos    = d.get("position", "?")
        print(f"    {BOLD}{i}{RESET}. RU{pos}  {name}  [{status}]  {DIM}{role}{RESET}")

    try:
        pick = input(f"\n  Device # to annotate (ENTER to skip): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if not pick:
        return None

    try:
        idx = int(pick)
        if idx < 1 or idx > len(racked):
            return None
        dev = racked[idx - 1]
    except ValueError:
        return None

    try:
        note = input(f"  Note: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if not note:
        return None

    return {
        "rack":        rack,
        "ru":          dev.get("position"),
        "device_name": dev.get("name") or dev.get("display") or "?",
        "status":      (dev.get("status") or {}).get("label", "?"),
        "note":        note,
        "timestamp":   datetime.now(timezone.utc).strftime(_ISO_FMT),
    }


# ── Legacy export (test compatibility) ───────────────────────────────────────

def _walkthrough_export(notes: list, site_code: str, dh: str) -> str:
    """Export to XLSX (preferred) or CSV fallback. Returns absolute file path."""
    ts   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    base = f"walkthrough_{site_code}_{dh}_{ts}"
    headers = ["Rack", "RU", "Device", "Status", "Note", "Timestamp"]
    rows = [
        [n.get("rack", ""), n.get("ru", ""), n.get("device_name", ""),
         n.get("status", ""), n.get("note", ""), n.get("timestamp", "")]
        for n in notes
    ]

    try:
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Walkthrough"
        ws.append(headers)
        for row in rows:
            ws.append(row)
        path = os.path.abspath(f"{base}.xlsx")
        wb.save(path)
        return path
    except ImportError:
        pass
    except Exception as e:
        print(f"  {DIM}XLSX export failed ({e}), falling back to CSV...{RESET}")

    path = os.path.abspath(f"{base}.csv")
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)
    return path


# ── Checklist ─────────────────────────────────────────────────────────────────

def _walkthrough_run_checklist(site_code: str, dh: str) -> dict:
    """Run the pre-walkthrough health checklist. Returns dict of answers."""
    _clear_screen()
    print(f"\n  {BOLD}{GREEN}◉  WALKTHROUGH MODE{RESET}  {DIM}{'─' * 48}{RESET}")
    print(f"  {BOLD}{site_code}{RESET}  {DIM}/{RESET}  {BOLD}{dh}{RESET}"
          f"  {DIM}│  Pre-Shift Checklist{RESET}")
    print(f"  {DIM}{'─' * 68}{RESET}\n")
    print(f"  {DIM}Answer y/n. ENTER skips (N/A). Press Ctrl+C to skip entire checklist.{RESET}\n")

    answers: dict = {}
    try:
        for key, label, mode in _CHECKLIST:
            if "na" in mode:
                prompt = f"  {label}? [y/n/na]: "
            else:
                prompt = f"  {label}? [y/n]: "
            try:
                raw = input(prompt).strip().lower()
            except (EOFError, KeyboardInterrupt):
                raise KeyboardInterrupt

            if raw in ("y", "yes"):
                ans = "YES"
            elif raw in ("n", "no"):
                ans = "NO"
            elif raw in ("na", "n/a", ""):
                ans = "N/A"
            else:
                ans = raw.upper() or "N/A"

            answers[key] = {"label": label, "answer": ans, "unit": ""}

            # Follow-up: ask for unit ID if alarm is YES
            if "unit" in mode and ans == "YES":
                try:
                    unit = input(f"    Unit ID: ").strip()
                    answers[key]["unit"] = unit
                except (EOFError, KeyboardInterrupt):
                    pass
    except KeyboardInterrupt:
        print(f"\n  {DIM}Checklist skipped.{RESET}")
        return {}

    return answers


# ── Carryover: parse ──────────────────────────────────────────────────────────

def _walkthrough_normalize_rack(raw: str) -> list[str]:
    """Normalize a rack string like 'R021/022' → ['R021', 'R022'], 'R12' → ['R012']."""
    # Handle multi-rack like R021/022 or R21/22
    parts = raw.upper().lstrip("R").split("/")
    result = []
    for p in parts:
        p = p.strip()
        if p.isdigit():
            result.append(f"R{int(p):03d}")
    return result if result else []


def _walkthrough_parse_slack_notes(text: str) -> list[dict]:
    """Parse 'Additional Walkthrough Notes' section from a Slack walkthrough post.

    Returns list of carryover dicts with keys: rack, original_note.
    Items without a rack prefix are stored under rack='GENERAL'.
    """
    # Extract the notes section — everything after the label line
    marker_pat = _re.compile(r'additional walkthrough notes\s*\n', _re.IGNORECASE)
    m = marker_pat.search(text)
    notes_text = text[m.end():] if m else text

    items = []
    # Match rack-prefixed lines: R069 CDU...; or R021/022 Nokias...
    rack_pat = _re.compile(
        r'\b(R\d+(?:/\d+)?)\s+(.+?)(?:;|\n|$)',
        _re.IGNORECASE | _re.MULTILINE
    )
    for match in rack_pat.finditer(notes_text):
        rack_raw = match.group(1).strip()
        note_raw = match.group(2).strip().rstrip(";").strip()
        if not note_raw:
            continue
        racks = _walkthrough_normalize_rack(rack_raw)
        for rack in racks:
            items.append({"rack": rack, "original_note": note_raw, "status": "pending",
                          "followup_note": "", "checked_at": None})

    # Catch lines that start with "Row N" or have no rack prefix
    general_pat = _re.compile(r'^(Row\s+\d+\s+.+?)(?:;|$)', _re.IGNORECASE | _re.MULTILINE)
    for match in general_pat.finditer(notes_text):
        note_raw = match.group(1).strip()
        items.append({"rack": "GENERAL", "original_note": note_raw, "status": "pending",
                      "followup_note": "", "checked_at": None})

    return items


def _walkthrough_parse_cwhelper_report(text: str) -> list[dict]:
    """Parse a cwhelper plain-text report into carryover items."""
    items = []
    rack_pat = _re.compile(r'^RACK\s+(R\d+)', _re.MULTILINE)
    note_pat = _re.compile(r'Issue:\s+(.+)')
    dev_pat  = _re.compile(r'├\s+RU\d+\s+(.+?)\s+\[')

    current_rack = None
    current_dev  = None
    for line in text.splitlines():
        rm = rack_pat.match(line)
        if rm:
            current_rack = rm.group(1)
            current_dev  = None
            continue
        dm = dev_pat.search(line)
        if dm and current_rack:
            current_dev = dm.group(1).strip()
            continue
        nm = note_pat.search(line)
        if nm and current_rack:
            issue = nm.group(1).strip()
            note  = f"{current_dev} — {issue}" if current_dev else issue
            items.append({
                "rack":          current_rack,
                "original_note": note,
                "status":        "pending",
                "followup_note": "",
                "checked_at":    None,
            })
            current_dev = None

    return items


def _walkthrough_import_carryover_ui(site_code: str, dh: str) -> list[dict]:
    """UI to load carryover from saved local report or pasted Slack text.

    Returns list of carryover item dicts.
    """
    _clear_screen()
    print(f"\n  {BOLD}{GREEN}◉  WALKTHROUGH MODE{RESET}  {DIM}{'─' * 48}{RESET}")
    print(f"  {BOLD}{site_code}{RESET}  {DIM}/{RESET}  {BOLD}{dh}{RESET}"
          f"  {DIM}│  Load Yesterday's Findings{RESET}")
    print(f"  {DIM}{'─' * 68}{RESET}\n")

    options = []
    # Option A: local saved report
    if os.path.exists(_WALKTHROUGH_REPORT_PATH):
        try:
            mtime = os.path.getmtime(_WALKTHROUGH_REPORT_PATH)
            age = datetime.now() - datetime.fromtimestamp(mtime)
            age_str = f"{int(age.total_seconds() // 3600)}h ago"
        except Exception:
            age_str = "unknown age"
        options.append(("f", f"Load from saved report  {DIM}({age_str}){RESET}", "file"))

    options.append(("p", "Paste Slack walkthrough text", "paste"))
    options.append(("s", "Skip carryover", "skip"))

    for key, label, _ in options:
        print(f"  {BOLD}{key}{RESET}  {label}")
    print()

    try:
        pick = input(f"  Choice: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return []

    # Load from file
    if pick == "f":
        try:
            with open(_WALKTHROUGH_REPORT_PATH) as fp:
                text = fp.read()
            items = _walkthrough_parse_cwhelper_report(text)
            if items:
                print(f"\n  {GREEN}✓ Loaded {len(items)} item(s) from saved report.{RESET}")
                time.sleep(1)
                return items
            else:
                print(f"  {DIM}No rack items found in saved report.{RESET}")
                time.sleep(1)
        except OSError as e:
            print(f"  {DIM}Could not read file: {e}{RESET}")
            time.sleep(1)
        return []

    # Paste Slack text
    if pick == "p":
        print(f"\n  {DIM}Paste the Slack walkthrough text below.")
        print(f"  Type END on its own line when done.{RESET}\n")
        lines = []
        try:
            while True:
                line = input()
                if line.strip().upper() == "END":
                    break
                lines.append(line)
        except (EOFError, KeyboardInterrupt):
            pass
        text = "\n".join(lines)
        items = _walkthrough_parse_slack_notes(text)
        if items:
            print(f"\n  {GREEN}✓ Parsed {len(items)} item(s) from Slack text.{RESET}")
            time.sleep(1)
            return items
        else:
            print(f"  {DIM}No rack items found. Check the text includes 'Additional Walkthrough Notes'.{RESET}")
            time.sleep(1.5)
        return []

    return []


def _walkthrough_carryover_for_rack(rack_label: str, carryover: list) -> list[dict]:
    """Return pending carryover items for a specific rack."""
    return [c for c in carryover if c.get("rack") == rack_label and c.get("status") == "pending"]


def _walkthrough_followup_item(item: dict):
    """Disposition a single carryover item. Modifies item in place."""
    print(f"\n  {YELLOW}{BOLD}⚠  Carryover:{RESET}  {item['original_note']}")
    print()
    print(f"  {BOLD}r{RESET}  Resolved    — issue is fixed")
    print(f"  {BOLD}p{RESET}  Persistent  — still present, no change")
    print(f"  {BOLD}w{RESET}  Worsened    — escalate / ticket opened")
    print(f"  {BOLD}s{RESET}  Skip        — not checked this shift")
    print()
    try:
        pick = input(f"  Status: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        pick = "s"

    status_map = {"r": "resolved", "p": "persistent", "w": "worsened", "s": "skipped"}
    item["status"] = status_map.get(pick, "skipped")

    if item["status"] in ("worsened", "persistent"):
        try:
            note = input(f"  Follow-up note (ENTER to skip): ").strip()
            item["followup_note"] = note
        except (EOFError, KeyboardInterrupt):
            pass

    item["checked_at"] = datetime.now(timezone.utc).strftime(_ISO_FMT)
    label = _DISPOSITION_LABELS.get(item["status"], item["status"])
    print(f"  {label}")


def _walkthrough_followup_rack(rack_label: str, carryover: list, state: dict,
                                notes: list, session: dict):
    """Offer to disposition all pending carryover items for a rack."""
    items = _walkthrough_carryover_for_rack(rack_label, carryover)
    if not items:
        return
    print(f"\n  {YELLOW}{BOLD}⚠  {len(items)} carryover item(s) for {rack_label}{RESET}")
    try:
        raw = input(f"  Follow up on them now? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return
    if raw != "y":
        return
    for item in items:
        _walkthrough_followup_item(item)
    # Persist updated carryover
    state["walkthrough_carryover"] = carryover
    _walkthrough_save_notes(state, notes, session)


# ── Issue template picker ─────────────────────────────────────────────────────

def _walkthrough_pick_template() -> tuple[str, str] | None:
    """Show grouped issue type menu. Returns (issue_label, note_text) or None."""
    print(f"\n  {BOLD}Issue type:{RESET}")
    for group_name, items in _ISSUE_TEMPLATE_GROUPS:
        print(f"\n  {DIM}── {group_name} {'─' * (30 - len(group_name))}{RESET}")
        for key, label, hint in items:
            print(f"    {BOLD}{key:>2}{RESET}  {label:<22} {DIM}{hint}{RESET}")
    print(f"\n     {BOLD}0{RESET}  {DIM}Cancel{RESET}")

    try:
        pick = input(f"\n  Issue: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None

    if not pick or pick == "0":
        return None

    for key, label, hint in _ISSUE_TEMPLATES:
        if pick == key:
            if key == _CUSTOM_KEY:
                try:
                    custom = input(f"  Note: ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    return None
                return ("Custom", custom) if custom else None
            else:
                try:
                    extra = input(f"  Detail (ENTER to skip): ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    return (label, label)
                note = label + (f" — {extra}" if extra else "")
                return (label, note)

    print(f"  {DIM}Invalid selection.{RESET}")
    return None


# ── Jira comment helper ───────────────────────────────────────────────────────

def _walkthrough_post_jira_comment(dev_name: str, note_text: str,
                                    email: str, token: str) -> str | None:
    """Search for a Jira ticket by device name and post a walkthrough comment."""
    if not _search_node_history or not _post_comment:
        print(f"  {DIM}Jira integration unavailable.{RESET}")
        return None

    print(f"  {DIM}Searching Jira for {dev_name}...{RESET}", end="", flush=True)
    issues = _search_node_history(dev_name, email, token, limit=5)
    print(f"\r{'':60}\r", end="")

    if not issues:
        print(f"  {DIM}No tickets found for {dev_name}.{RESET}")
        return None

    print(f"\n  {BOLD}Tickets for {dev_name}:{RESET}\n")
    for i, iss in enumerate(issues[:5], start=1):
        key     = iss.get("key", "?")
        summary = (iss.get("fields", {}).get("summary") or "")[:55]
        status  = ((iss.get("fields", {}).get("status") or {}).get("name") or "?")
        print(f"    {BOLD}{i}{RESET}. {CYAN}{key}{RESET}  {DIM}[{status}]{RESET}  {summary}")

    try:
        pick = input(f"\n  Comment on # (ENTER to skip): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if not pick:
        return None

    try:
        idx = int(pick)
        if idx < 1 or idx > len(issues[:5]):
            return None
        key = issues[idx - 1].get("key", "")
    except ValueError:
        return None

    ts   = datetime.now(timezone.utc).strftime(_DISPLAY_FMT)
    body = f"[Walkthrough] {note_text}\n\nDevice: {dev_name}\nTime: {ts}"
    ok = _post_comment(key, body, email, token)
    if ok:
        return key
    print(f"  {DIM}Comment post failed.{RESET}")
    return None


# ── Full annotation flow ──────────────────────────────────────────────────────

def _walkthrough_annotate_full(dev: dict, rack_label: str,
                                email: str, token: str,
                                rack_carryover: list = None) -> dict | None:
    """Template picker → auto-stamp ongoing → Jira search → return annotation dict."""
    result = _walkthrough_pick_template()
    if not result:
        return None
    issue_type, note_text = result

    # Auto-stamp ONGOING if this rack had a carryover issue
    ongoing = bool(rack_carryover)
    if ongoing:
        prior = rack_carryover[0].get("original_note", "")
        note_text = f"[ONGOING] {note_text}  (prev: {prior})"

    # Jira: search first, then show results, then offer to comment
    jira_key = None
    dev_name = dev.get("name") or dev.get("display") or ""
    if dev_name and email and token:
        if _post_comment:
            print(f"\n  {DIM}Searching Jira for {dev_name}...{RESET}", end="", flush=True)
            issues = _search_node_history(dev_name, email, token, limit=5)
            print(f"\r{'':60}\r", end="")

            if not issues:
                print(f"  {DIM}No Jira ticket found for this device.{RESET}")
            else:
                # Show top result inline, full list on demand
                top = issues[0]
                top_key    = top.get("key", "?")
                top_sum    = (top.get("fields", {}).get("summary") or "")[:55]
                top_status = ((top.get("fields", {}).get("status") or {}).get("name") or "?")
                print(f"\n  {DIM}Jira:{RESET}  {CYAN}{BOLD}{top_key}{RESET}  "
                      f"{DIM}[{top_status}]{RESET}  {top_sum}")
                if len(issues) > 1:
                    print(f"  {DIM}  + {len(issues) - 1} more ticket(s){RESET}")

                try:
                    raw = input(f"\n  Comment on {top_key}? [y/n/?]: ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    raw = "n"

                if raw == "y":
                    ts   = datetime.now(timezone.utc).strftime(_DISPLAY_FMT)
                    body = f"[Walkthrough] {note_text}\n\nDevice: {dev_name}\nTime: {ts}"
                    if _post_comment(top_key, body, email, token):
                        jira_key = top_key
                        print(f"  {GREEN}✓ Commented on {top_key}{RESET}")
                    else:
                        print(f"  {DIM}Comment failed.{RESET}")
                elif raw == "?":
                    # Show full list and let them pick
                    jira_key = _walkthrough_post_jira_comment(dev_name, note_text, email, token)

    annotation = {
        "rack":        rack_label,
        "ru":          dev.get("position"),
        "device_name": dev_name or "?",
        "status":      (dev.get("status") or {}).get("label", "?"),
        "issue_type":  issue_type,
        "note":        note_text,
        "ongoing":     ongoing,
        "jira_key":    jira_key,
        "timestamp":   datetime.now(timezone.utc).strftime(_ISO_FMT),
    }
    ongoing_tag = f"  {YELLOW}[ONGOING]{RESET}" if ongoing else ""
    print(f"\n  {GREEN}✓ Annotated:{RESET}  {annotation['device_name']}  "
          f"{DIM}— {issue_type}{RESET}{ongoing_tag}")
    if jira_key:
        print(f"  {GREEN}✓ Commented:{RESET}  {jira_key}")
    return annotation


# ── Report builder ────────────────────────────────────────────────────────────

def _walkthrough_build_report(notes: list, session: dict,
                               carryover: list = None,
                               checklist: dict = None,
                               history: list = None) -> str:
    """Build a plain-text walkthrough report with optional checklist and carryover."""
    site     = session.get("site_code", "?")
    dh       = session.get("dh", "?")
    started  = session.get("started_at", "?")
    finished = datetime.now(timezone.utc).strftime(_ISO_FMT)
    tech     = session.get("tech", "")

    lines = [
        "WALKTHROUGH REPORT",
        "=" * 60,
        f"Site:     {site}",
        f"DH:       {dh}",
    ]
    if tech:
        lines.append(f"Tech:     {tech}")
    lines += [
        f"Started:  {started}",
        f"Finished: {finished}",
        f"Notes:    {len(notes)}",
        "=" * 60,
        "",
    ]

    # Checklist section
    if checklist:
        lines.append("PRE-SHIFT CHECKLIST")
        lines.append("─" * 40)
        for key, data in checklist.items():
            unit = f"  Unit: {data['unit']}" if data.get("unit") else ""
            lines.append(f"  {data['label']:<46}  {data['answer']}{unit}")
        lines.append("")

    # Carryover section
    if carryover:
        lines.append("CARRYOVER FROM PREVIOUS WALKTHROUGH")
        lines.append("─" * 40)
        by_status: dict[str, list] = {}
        for c in carryover:
            by_status.setdefault(c.get("status", "pending"), []).append(c)

        for status in ("resolved", "persistent", "worsened", "pending", "skipped"):
            items = by_status.get(status, [])
            if not items:
                continue
            status_label = status.upper()
            for c in items:
                fn = f"  → {c['followup_note']}" if c.get("followup_note") else ""
                lines.append(f"  [{status_label}]  {c.get('rack', '?')}  {c['original_note']}{fn}")
        lines.append("")

    # New annotations grouped by rack
    if notes:
        lines.append("TODAY'S ANNOTATIONS")
        lines.append("─" * 40)
        by_rack: dict[str, list] = {}
        for n in notes:
            by_rack.setdefault(n.get("rack", "?"), []).append(n)

        for rack, rack_notes in sorted(by_rack.items()):
            lines.append(f"\nRACK {rack}")
            for n in rack_notes:
                issue  = n.get("issue_type") or n.get("note", "?")
                detail = n.get("note", "")
                lines.append(f"  ├ RU{n.get('ru', '?'):>3}  {n.get('device_name', '?')}  [{n.get('status', '?')}]")
                lines.append(f"  │   Issue:  {issue}")
                if detail and detail != issue:
                    lines.append(f"  │   Detail: {detail}")
                if n.get("jira_key"):
                    lines.append(f"  │   Jira:   {n['jira_key']} (commented)")
                lines.append(f"  │   Time:   {n.get('timestamp', '?')}")
                lines.append(f"  │")
        lines.append("")

    # Trending / repeat issues — history already includes today (saved before report build)
    if history:
        trending = _walkthrough_detect_trends(history, min_count=2)
        if trending:
            lines.append("TRENDING / REPEAT ISSUES")
            lines.append("─" * 40)
            lines.append(f"  {'Device':<36}  {'Rack':<8}  {'Times'}  Recent issues")
            lines.append(f"  {'─' * 36}  {'─' * 8}  {'─' * 5}  {'─' * 20}")
            for t in trending:
                recent = t["events"][-3:]  # last 3 occurrences
                issue_summary = " → ".join(
                    e.get("issue_type") or e.get("note", "?")
                    for e in recent
                )
                dates = ", ".join(e.get("date", "?") for e in recent)
                lines.append(f"  {t['device_name']:<36}  {t['rack']:<8}  {t['count']:>5}x  {issue_summary}")
                lines.append(f"  {'':36}  {'':8}         dates: {dates}")
            lines.append("")

    # Summary
    lines += [
        "─" * 60,
        f"Racks visited:     {len({n.get('rack') for n in notes})}",
        f"New annotations:   {len(notes)}",
    ]
    if carryover:
        counts = _count_carryover(carryover)
        lines += [
            f"Carryover total:   {len(carryover)}",
            f"  No issue found:  {counts[_STATUS_RESOLVED]}",
            f"  Still present:   {counts[_STATUS_PERSISTENT]}",
            f"  Worsened:        {counts[_STATUS_WORSENED]}",
        ]
    lines.append("─" * 60)
    return "\n".join(lines)


# ── Finish screen ─────────────────────────────────────────────────────────────

def _walkthrough_finish(notes: list, session: dict, state: dict,
                         carryover: list = None, checklist: dict = None):
    """Generate report, save to fixed path, offer copy / view."""
    _clear_screen()
    site = session.get("site_code", "?")
    dh   = session.get("dh", "?")
    print(f"\n  {BOLD}{GREEN}◉  WALKTHROUGH MODE{RESET}  {DIM}{'─' * 48}{RESET}")
    print(f"  {BOLD}{site}{RESET}  {DIM}/{RESET}  {BOLD}{dh}{RESET}"
          f"  {DIM}│  Complete{RESET}\n")

    if not notes and not carryover:
        print(f"  {DIM}No annotations or carryover — nothing to save.{RESET}")
        input(f"\n  {DIM}Press ENTER to return to menu...{RESET}")
        _walkthrough_clear_session(state)
        return

    # Save to rolling history before building report (so trends include today)
    _walkthrough_save_to_history(state, notes, session)
    history = state.get("walkthrough_history", [])

    report = _walkthrough_build_report(notes, session, carryover, checklist, history)

    try:
        with open(_WALKTHROUGH_REPORT_PATH, "w") as f:
            f.write(report)
        print(f"  {GREEN}✓ Saved:{RESET}  {_WALKTHROUGH_REPORT_PATH}")
        print(f"  {DIM}  (overwrites any previous walkthrough report){RESET}\n")
    except OSError as e:
        print(f"  {YELLOW}Could not save report: {e}{RESET}\n")

    rack_count = len({n.get("rack") for n in notes})
    print(f"  {BOLD}New annotations:{RESET}  {len(notes)}  across  {rack_count} rack(s)")
    if carryover:
        counts = _count_carryover(carryover)
        print(f"  {BOLD}Carryover:{RESET}  {counts[_STATUS_RESOLVED]}/{len(carryover)} resolved"
              + (f"  {YELLOW}({counts[_STATUS_PENDING]} not checked){RESET}"
                 if counts[_STATUS_PENDING] else ""))
    trending = _walkthrough_detect_trends(history, min_count=2)
    if trending:
        print(f"  {BOLD}{YELLOW}Trending:{RESET}  {len(trending)} node(s) flagged 2+ times"
              f"  {DIM}— see report for details{RESET}")
    print()

    while True:
        print(f"  {BOLD}c{RESET}  copy to clipboard")
        print(f"  {BOLD}v{RESET}  view report")
        print(f"  {BOLD}ENTER{RESET}  return to menu\n")
        try:
            act = input(f"  Action: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            break

        if act == "c":
            try:
                subprocess.run(["pbcopy"], input=report.encode(), check=True)
                print(f"  {GREEN}Copied to clipboard.{RESET}\n")
            except Exception as e:
                print(f"  {DIM}Clipboard failed: {e}{RESET}\n")
        elif act == "v":
            _clear_screen()
            print()
            for line in report.splitlines():
                print(f"  {line}")
            print()
            input(f"  ENTER to go back: ")
            _clear_screen()
            print(f"\n  {BOLD}{GREEN}◉  WALKTHROUGH MODE{RESET}  "
                  f"{DIM}{'─' * 48}{RESET}")
            print(f"  {BOLD}{site}{RESET}  {DIM}/{RESET}  {BOLD}{dh}{RESET}"
                  f"  {DIM}│  Complete{RESET}\n")
            print(f"  {GREEN}✓ Saved:{RESET}  {_WALKTHROUGH_REPORT_PATH}\n")
        elif act == "":
            break

    _walkthrough_clear_session(state)


def _walkthrough_clear_session(state: dict):
    state["walkthrough_session"]   = None
    state["walkthrough_notes"]     = []
    state["walkthrough_carryover"] = []
    state["walkthrough_checklist"] = {}
    # NOTE: walkthrough_history is intentionally NOT cleared — it accumulates across sessions
    _save_user_state(state)


# ── History: accumulate & trend detection ─────────────────────────────────────

def _walkthrough_carryover_to_history(carryover: list, session: dict, state: dict):
    """Write Slack-imported carryover items into history so they count toward trends.

    Uses yesterday's date since carryover represents the previous shift's findings.
    Only runs once — skips items already in history for that date+rack combination.
    """
    if not carryover:
        return
    _now      = datetime.now(timezone.utc)
    yesterday = (_now - timedelta(days=1)).strftime("%Y-%m-%d")
    cutoff    = (_now - timedelta(days=60)).strftime("%Y-%m-%d")

    # Avoid double-writing: check if a carryover entry for yesterday already exists
    history = state.get("walkthrough_history", [])
    existing_dates = {h.get("date") for h in history}
    if yesterday in existing_dates:
        return  # already imported for this date

    annotations = []
    for c in carryover:
        if c.get("rack") == "GENERAL":
            continue
        annotations.append({
            "rack":        c.get("rack"),
            "ru":          None,
            "device_name": c.get("rack"),   # rack as proxy when no device name
            "issue_type":  c.get("original_note", ""),
            "note":        c.get("original_note", ""),
        })

    if not annotations:
        return

    entry = {
        "date":        yesterday,
        "site_code":   session.get("site_code", "?"),
        "dh":          session.get("dh", "?"),
        "tech":        "imported",
        "annotations": annotations,
    }
    history.append(entry)
    history = [h for h in history if h.get("date", "") >= cutoff]
    state["walkthrough_history"] = history
    _save_user_state(state)


def _walkthrough_save_to_history(state: dict, notes: list, session: dict):
    """Append this session's annotations to rolling walkthrough_history.

    Keeps entries from the last 60 days. Each entry is keyed by device_name
    so trend detection can match the same node across multiple shifts.
    """
    if not notes:
        return
    _now   = datetime.now(timezone.utc)
    today  = _now.strftime("%Y-%m-%d")
    cutoff = (_now - timedelta(days=60)).strftime("%Y-%m-%d")

    entry = {
        "date":      today,
        "site_code": session.get("site_code", "?"),
        "dh":        session.get("dh", "?"),
        "tech":      session.get("tech", ""),
        "annotations": [
            {
                "rack":        n.get("rack"),
                "ru":          n.get("ru"),
                "device_name": n.get("device_name"),
                "issue_type":  n.get("issue_type") or n.get("note", ""),
                "note":        n.get("note", ""),
            }
            for n in notes
        ],
    }

    history = state.get("walkthrough_history", [])
    history.append(entry)
    # Trim entries older than 60 days
    history = [h for h in history if h.get("date", "") >= cutoff]
    state["walkthrough_history"] = history
    _save_user_state(state)


def _walkthrough_get_device_history(device_name: str, history: list) -> list:
    """Return past annotations for a device, newest first.

    Each result: {"date", "issue_type", "note", "dh"}
    """
    if not device_name or not history:
        return []
    matches = []
    for session in reversed(history):
        for ann in session.get("annotations", []):
            if ann.get("device_name") == device_name:
                matches.append({
                    "date":       session.get("date", "?"),
                    "dh":         session.get("dh", "?"),
                    "issue_type": ann.get("issue_type", ""),
                    "note":       ann.get("note", ""),
                })
    return matches


def _walkthrough_detect_trends(history: list, min_count: int = 2) -> list:
    """Find devices/racks flagged min_count or more times across all history.

    Returns list of dicts sorted by occurrence count descending.
    """
    device_events: dict = defaultdict(list)

    for session in history:
        for ann in session.get("annotations", []):
            key = (ann.get("device_name") or ann.get("rack", "?"),
                   ann.get("rack", "?"))
            device_events[key].append({
                "date":       session.get("date", "?"),
                "issue_type": ann.get("issue_type", ""),
                "note":       ann.get("note", ""),
            })

    trending = []
    for (device_name, rack), events in device_events.items():
        if len(events) >= min_count:
            trending.append({
                "device_name": device_name,
                "rack":        rack,
                "count":       len(events),
                "events":      events,
            })

    return sorted(trending, key=lambda x: -x["count"])


def _rack_sort_key(c: dict) -> int:
    """Sort key for carryover items by rack number (e.g. R012 → 12)."""
    m = _re.search(r'\d+', c.get("rack", ""))
    return int(m.group()) if m else 9999


# ── Main mode ─────────────────────────────────────────────────────────────────

def _walkthrough_mode(state: dict, email: str, token: str) -> dict:
    """Free-form rack walkthrough with checklist, carryover, and issue templates."""
    _clear_screen()
    print(f"\n  {BOLD}=== Walkthrough Mode ==={RESET}")
    print(f"  {DIM}Type a rack number to open it.  'done' to finish,  "
          f"'list' to review,  'carryover' to see pending items,  'q' to quit.{RESET}\n")

    notes, session = _walkthrough_resume_prompt(state)

    if session:
        site_code = session["site_code"]
        dh        = session["dh"]
        carryover = state.get("walkthrough_carryover", [])
        checklist = state.get("walkthrough_checklist", {})
    else:
        result = _walkthrough_pick_site_dh()
        if not result:
            return state
        site_code, dh = result
        session = {
            "site_code":  site_code,
            "dh":         dh,
            "started_at": datetime.now(timezone.utc).strftime(_ISO_FMT),
        }
        notes     = []
        carryover = []
        checklist = {}

        # ── Pre-shift checklist ───────────────────────────────────────────────
        print()
        try:
            do_checklist = input(
                f"  Run pre-shift checklist? [y/N]: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            do_checklist = "n"
        if do_checklist == "y":
            checklist = _walkthrough_run_checklist(site_code, dh)
            state["walkthrough_checklist"] = checklist

        # ── Carryover import ──────────────────────────────────────────────────
        print()
        try:
            do_carryover = input(
                f"  Load yesterday's findings as carryover? [y/N]: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            do_carryover = "n"
        if do_carryover == "y":
            carryover = _walkthrough_import_carryover_ui(site_code, dh)
            state["walkthrough_carryover"] = carryover
            # Write imported items into history so they count toward trend detection
            _walkthrough_carryover_to_history(carryover, session, state)

    site_slug = site_code.lower()

    # ── Pre-fetch rack index from NetBox once ─────────────────────────────────
    _clear_screen()
    print(f"\n  {BOLD}{GREEN}◉  WALKTHROUGH MODE{RESET}  {DIM}Loading racks from NetBox...{RESET}")
    rack_lookup: dict[str, dict] = {}
    try:
        dh_racks = _netbox_get("/dcim/racks/",
                               params={"site": site_slug, "location": dh.lower(), "limit": 1000})
        results = (dh_racks or {}).get("results", [])
        if not results:
            all_racks = _netbox_get("/dcim/racks/",
                                    params={"site": site_slug, "limit": 1000})
            results = (all_racks or {}).get("results", [])
        for r in results:
            rack_lookup[r.get("name", "")] = r
    except Exception:
        pass

    if not rack_lookup:
        print(f"  {DIM}No racks found for {site_code}. Check site name.{RESET}")
        _brief_pause()
        return state

    print(f"  {DIM}{len(rack_lookup)} rack(s) loaded.{RESET}")

    # Pre-build device→history lookup once (history doesn't change during session)
    _wt_history = state.get("walkthrough_history", [])
    _dev_hist_map: dict = {}
    for _s in reversed(_wt_history):
        for _a in _s.get("annotations", []):
            _dn = _a.get("device_name", "")
            if _dn:
                _dev_hist_map.setdefault(_dn, []).append({
                    "date":       _s.get("date", "?"),
                    "dh":         _s.get("dh", "?"),
                    "issue_type": _a.get("issue_type", ""),
                    "note":       _a.get("note", ""),
                })

    # Announce carryover
    if carryover:
        pending = sum(1 for c in carryover if c.get("status") == "pending")
        print(f"  {YELLOW}{BOLD}{pending}{RESET} {DIM}carryover item(s) from previous walkthrough.{RESET}")
    time.sleep(0.7)

    # ── Main prompt loop ──────────────────────────────────────────────────────
    while True:
        _clear_screen()
        _walkthrough_banner(site_code, dh, notes, carryover)

        # Build sorted pending list — used for numbered shortcuts
        pending_items = [c for c in carryover if c.get("status") == "pending"]
        pending_sorted = sorted(pending_items, key=_rack_sort_key)

        if pending_sorted:
            print(f"  {YELLOW}⚠  From last walkthrough — type # to verify:{RESET}\n")
            for idx, c in enumerate(pending_sorted, start=1):
                rack  = c.get("rack", "?")
                note  = c.get("original_note", "")[:55]
                print(f"    {BOLD}{idx:>2}{RESET}.  {CYAN}{rack}{RESET}  {DIM}{note}{RESET}")
            print()

        try:
            raw = input(f"  # or rack # (or 'done' / 'list' / 'q'): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            raw = "q"

        # ── Quit ──────────────────────────────────────────────────────────────
        if raw in ("q", "quit"):
            try:
                confirm = input(
                    f"\n  Quit without finishing? Unsaved annotations will be lost. [y/N]: "
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                confirm = "n"
            if confirm == "y":
                _walkthrough_clear_session(state)
                _clear_screen()
                return state
            continue

        # ── Done ──────────────────────────────────────────────────────────────
        if raw in ("done", "finish", "end"):
            # Warn about carryover racks never opened this shift
            never_visited = [c for c in carryover if c.get("status") == "pending"]
            if never_visited:
                unvisited_racks = sorted({c.get("rack") for c in never_visited
                                          if c.get("rack") != "GENERAL"})
                print(f"\n  {YELLOW}{BOLD}⚠  Warning:{RESET}  You haven't checked these rack(s) from last shift:")
                for r in unvisited_racks:
                    items = [c for c in never_visited if c.get("rack") == r]
                    for c in items:
                        print(f"     {CYAN}{r}{RESET}  {DIM}{c.get('original_note', '')}{RESET}")
                print()
                try:
                    confirm_done = input(
                        f"  Are you sure you're done? They'll be marked as no issue found. [y/N]: "
                    ).strip().lower()
                except (EOFError, KeyboardInterrupt):
                    confirm_done = "n"
                if confirm_done != "y":
                    continue  # back to main prompt
                # Mark all unvisited as resolved with note
                _ts = datetime.now(timezone.utc).strftime(_ISO_FMT)
                for item in never_visited:
                    item["status"]       = "resolved"
                    item["checked_at"]   = _ts
                    item["followup_note"] = "not visited this shift — assumed no issue"
                state["walkthrough_carryover"] = carryover
                _walkthrough_save_notes(state, notes, session)

            _walkthrough_finish(notes, session, state, carryover, checklist)
            _clear_screen()
            return state

        # ── List annotations ──────────────────────────────────────────────────
        if raw == "list":
            _clear_screen()
            _walkthrough_banner(site_code, dh, notes, carryover)
            if notes:
                print(f"  {BOLD}Today's annotations:{RESET}\n")
                for n in notes:
                    issue = n.get("issue_type") or n.get("note", "?")
                    jira  = f"  {CYAN}{n['jira_key']}{RESET}" if n.get("jira_key") else ""
                    print(f"  {BOLD}{n.get('rack')}{RESET}  "
                          f"RU{n.get('ru', '?'):>3}  "
                          f"{n.get('device_name', '?'):<32}  "
                          f"{DIM}{issue}{RESET}{jira}")
            else:
                print(f"  {DIM}No annotations yet.{RESET}")
            print()
            try:
                input(f"  ENTER to continue: ")
            except (EOFError, KeyboardInterrupt):
                pass
            continue

        # ── Carryover review ──────────────────────────────────────────────────
        if raw in ("carryover", "co", "carry"):
            _clear_screen()
            _walkthrough_banner(site_code, dh, notes, carryover)
            if not carryover:
                print(f"  {DIM}No carryover items loaded.{RESET}\n")
            else:
                print(f"  {BOLD}Carryover items:{RESET}\n")
                for c in carryover:
                    status = c.get("status", "pending")
                    label  = _DISPOSITION_LABELS.get(status, status)
                    fn     = f"  → {c['followup_note']}" if c.get("followup_note") else ""
                    print(f"  {BOLD}{c.get('rack', '?'):<8}{RESET}  "
                          f"{c.get('original_note', ''):<50}  "
                          f"{label}{fn}")
                print()
                try:
                    do_item = input(f"  Disposition an item? Enter rack # or ENTER to go back: ").strip().upper()
                except (EOFError, KeyboardInterrupt):
                    do_item = ""
                if do_item:
                    rack_items = [c for c in carryover
                                  if c.get("rack", "").upper() == do_item
                                  and c.get("status") == "pending"]
                    if rack_items:
                        for item in rack_items:
                            _walkthrough_followup_item(item)
                        state["walkthrough_carryover"] = carryover
                        _walkthrough_save_notes(state, notes, session)
                    else:
                        print(f"  {DIM}No pending items for {do_item}.{RESET}")
                        time.sleep(1)
            continue

        # ── Parse input: list index or rack number ────────────────────────────
        try:
            input_num = int(raw)
        except ValueError:
            input_num = None

        # If input matches a carryover list index (1–N), resolve to that rack
        if input_num is not None and pending_sorted and 1 <= input_num <= len(pending_sorted):
            selected_rack = pending_sorted[input_num - 1].get("rack", "")
            rack_m = _re.search(r'\d+', selected_rack)
            rack_num = int(rack_m.group()) if rack_m else input_num
        elif input_num is not None:
            rack_num = input_num
        else:
            m = _re.search(r'(\d+)$', raw)
            if m:
                rack_num = int(m.group(1))
            else:
                print(f"  {DIM}Enter a rack number (e.g. 12 or R012), 'done', or 'q'.{RESET}")
                time.sleep(1.2)
                continue

        rack_label = f"R{rack_num:03d}"

        rack_obj = (rack_lookup.get(rack_label) or
                    rack_lookup.get(str(rack_num)) or
                    rack_lookup.get(str(rack_num).zfill(3)) or
                    rack_lookup.get(str(rack_num).zfill(4)))

        if not rack_obj:
            print(f"  {DIM}{rack_label} not found in NetBox for {site_code}.{RESET}")
            time.sleep(1.2)
            continue

        devices = _netbox_get_rack_devices(rack_obj["id"])
        racked  = [d for d in devices if d.get("position") is not None]

        # Opening this rack = mark any pending carryover here as checked (no issue found yet).
        # Annotating a device will override to "persistent". This is intentional.
        _now_ts = datetime.now(timezone.utc).strftime(_ISO_FMT)
        for _c in carryover:
            if _c.get("rack") == rack_label and _c.get("status") == "pending":
                _c["status"]     = "resolved"
                _c["checked_at"] = _now_ts
                _c["followup_note"] = "visited — no new issue noted"
        state["walkthrough_carryover"] = carryover

        # ── Rack screen ───────────────────────────────────────────────────────
        while True:
            _clear_screen()
            _walkthrough_banner(site_code, dh, notes, carryover)
            print(f"  {BOLD}{rack_label}{RESET}  {DIM}— {len(racked)} device(s){RESET}\n")

            # Show carryover alert for this rack (any status, not just pending)
            rack_carryover = [c for c in carryover if c.get("rack") == rack_label]
            if rack_carryover:
                print(f"  {YELLOW}{'━' * 60}{RESET}")
                print(f"  {YELLOW}{BOLD}⚠  FLAGGED IN LAST WALKTHROUGH — verify if still present:{RESET}")
                for c in rack_carryover:
                    print(f"     {BOLD}→{RESET}  {c['original_note']}")
                print(f"  {YELLOW}{'━' * 60}{RESET}")
                print()

            if not racked:
                print(f"  {DIM}No racked devices in NetBox.{RESET}\n")
                try:
                    input(f"  ENTER to continue: ")
                except (EOFError, KeyboardInterrupt):
                    pass
                break

            for i, d in enumerate(racked, start=1):
                name   = d.get("name") or d.get("display") or "?"
                status = (d.get("status") or {}).get("label", "?")
                role   = (d.get("device_role") or {}).get("name", "")
                pos    = d.get("position")
                pos_s  = str(int(pos)) if isinstance(pos, (int, float)) else str(pos or "?")
                # Detect if device name references a different DH than current
                dh_mismatch = ""
                dh_m = _re.search(r'dh(\d+)', name, _re.IGNORECASE)
                if dh_m:
                    dh_num_in_name = dh_m.group(1)
                    dh_num_current = _re.search(r'\d+', dh)
                    if dh_num_current and dh_num_in_name != dh_num_current.group():
                        dh_mismatch = f"  {YELLOW}⚠ named for DH{dh_num_in_name} — physically in this rack{RESET}"
                print(f"    {BOLD}{i:2}{RESET}.  RU{pos_s:>3}  {name:<32}  "
                      f"{DIM}[{status}]  {role}{RESET}{dh_mismatch}")
                # Show prior flag history for this device (O(1) via pre-built map)
                dev_hist = _dev_hist_map.get(name, [])
                if dev_hist:
                    count = len(dev_hist)
                    recent = dev_hist[0]  # newest first
                    flag = f"{RED}" if count >= 3 else f"{YELLOW}"
                    print(f"         {flag}{BOLD}↺ Flagged {count}x{RESET}  "
                          f"{DIM}last: {recent['issue_type']} ({recent['date']}){RESET}")

            print(f"\n  {DIM}ENTER to go back{RESET}")
            try:
                dev_raw = input(f"\n  Device #: ").strip()
            except (EOFError, KeyboardInterrupt):
                dev_raw = ""

            if not dev_raw:
                # Carryover already auto-resolved when rack was opened — just leave
                break

            try:
                dev_idx = int(dev_raw)
                if dev_idx < 1 or dev_idx > len(racked):
                    print(f"  {DIM}Invalid — enter 1–{len(racked)} or ENTER to go back.{RESET}")
                    time.sleep(0.8)
                    continue
                dev = racked[dev_idx - 1]
            except ValueError:
                print(f"  {DIM}Enter a device number or ENTER to go back.{RESET}")
                time.sleep(0.8)
                continue

            annotation = _walkthrough_annotate_full(dev, rack_label, email, token,
                                                     rack_carryover=rack_carryover)
            if annotation:
                notes.append(annotation)
                # Auto-mark carryover as persistent — issue confirmed still present
                for item in rack_carryover:
                    if item.get("status") == "pending":
                        item["status"] = "persistent"
                        item["checked_at"] = datetime.now(timezone.utc).strftime(_ISO_FMT)
                        item["followup_note"] = annotation.get("note", "")
                state["walkthrough_carryover"] = carryover
                _walkthrough_save_notes(state, notes, session)

            try:
                more = input(f"\n  Annotate another device in {rack_label}? [y/N]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                more = "n"
            if more != "y":
                break

    return state
