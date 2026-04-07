"""Walkthrough mode — free-form rack annotation with issue templates, carryover, and checklist."""
from __future__ import annotations

import csv
import os
import tempfile
import re as _re
import subprocess
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta

from cwhelper.config import BOLD, CYAN, DIM, GREEN, JIRA_BASE_URL, KNOWN_SITES, RED, RESET, YELLOW
__all__ = [
    '_walkthrough_pick_site_dh', '_walkthrough_annotate_device',
    '_walkthrough_save_notes', '_walkthrough_resume_prompt',
    '_walkthrough_export', '_walkthrough_mode',
    '_walkthrough_prewalk_brief', '_walkthrough_show_trend_alert',
]
from cwhelper.clients.netbox import _netbox_get_rack_devices, _netbox_get
from cwhelper.state import _save_user_state
from cwhelper.cache import _brief_pause
from cwhelper.tui.display import _clear_screen
from cwhelper.services.context import _format_age, _parse_jira_timestamp, _parse_rack_location
from cwhelper.services.search import _jql_search
try:
    from cwhelper.services.queue import _search_node_history
    from cwhelper.clients.jira import _post_comment, _upload_attachment
except ImportError:
    _search_node_history = None  # type: ignore[assignment]
    _post_comment = None  # type: ignore[assignment]
    _upload_attachment = None  # type: ignore[assignment]
try:
    from cwhelper.clients.gsheets import _get_rma_data, _rma_available, _rma_file_age, _rma_file_age_secs, _find_latest_file
except ImportError:
    _get_rma_data = None  # type: ignore[assignment]
    _rma_available = lambda: False
    _rma_file_age = lambda: "?"
    _rma_file_age_secs = lambda: -1
    _find_latest_file = lambda: None  # type: ignore[assignment]
try:
    from cwhelper.clients.teleport import _tsh_available
except ImportError:
    _tsh_available = lambda: False  # type: ignore[assignment]


# ── Constants ─────────────────────────────────────────────────────────────────

# One local report at a time — always overwritten on finish
_WALKTHROUGH_REPORT_PATH = os.path.expanduser("~/.cwhelper_walkthrough_report.txt")
_WALKTHROUGH_HTML_PATH   = os.path.expanduser("~/.cwhelper_walkthrough_report.html")
_RMA_SHEET_URL = "https://docs.google.com/spreadsheets/d/1vTcB9-NEIXk1VowTL6NkBHYjioeosgn2WTDnVuABByo/edit?gid=1318234478#gid=1318234478"
_OVERHEAD_MAP_URL = "https://docs.google.com/spreadsheets/d/1dtuaNuDuLPGzqkUb6pBOBM-meeoEioGata3xGkq-zgI/edit?gid=0#gid=0"

_DOWNLOADS = os.path.expanduser("~/Downloads")


def _cleanup_old_tracker_dupes():
    """Remove older duplicate tracker files from Downloads.

    macOS creates 'name (1).csv', 'name (2).csv' on re-download.
    Keeps only the newest file, deletes the rest.
    """
    import glob as _glob
    patterns = [
        "Device-Tracker*", "*Device*Tracker*",
        "*Device-Tracker*", "*Device*Tracker*", "*Active Devices*",
    ]
    candidates = set()
    for pattern in patterns:
        for path in _glob.glob(os.path.join(_DOWNLOADS, pattern)):
            if path.lower().endswith((".csv", ".xlsx")):
                candidates.add(path)

    if len(candidates) <= 1:
        return

    # Sort by mtime, newest first — keep newest, delete the rest
    sorted_files = sorted(candidates, key=lambda p: os.path.getmtime(p), reverse=True)
    kept = sorted_files[0]
    for old in sorted_files[1:]:
        try:
            os.remove(old)
        except OSError:
            pass


_ATLASSIAN_BROWSE_PREFIX = f"{JIRA_BASE_URL}/browse/"


def _normalize_ticket_key(val: str) -> str:
    """Strip Atlassian browse URL to just the ticket key. 'https://...browse/HO-12345' → 'HO-12345'."""
    if not val:
        return val
    val = val.strip()
    if val.startswith(_ATLASSIAN_BROWSE_PREFIX):
        return val[len(_ATLASSIAN_BROWSE_PREFIX):]
    return val


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
    ("RMA / Escalation", [
        ("11", "RMA engaged",       "node flagged for RMA, awaiting replacement"),
        ("12", "RMA pending",       "RMA requested, parts not yet arrived"),
    ]),
    ("Other", [
        ("13", "Custom",            "enter your own note"),
    ]),
]

# Flat list for lookup — keep in sync with groups above
_ISSUE_TEMPLATES = [t for _, items in _ISSUE_TEMPLATE_GROUPS for t in items]
_CUSTOM_KEY = "13"

# Matches the Slack walkthrough form
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


# ── Trend alert ───────────────────────────────────────────────────────────────

def _walkthrough_show_trend_alert(dev_hist: list, dev_name: str) -> None:
    """Print a trend alert if this device has been flagged 2+ times in the last 14 days."""
    if not dev_hist or len(dev_hist) < 2:
        return

    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    recent = []
    for h in dev_hist:
        try:
            d = datetime.fromisoformat(h["date"].replace("Z", "+00:00"))
            if d >= cutoff:
                recent.append(h)
        except Exception:
            recent.append(h)

    display = recent if len(recent) >= 2 else dev_hist[:5]
    count = len(display)
    if count < 2:
        return

    flag_color = RED if count >= 3 else YELLOW
    label = "recurring — consider escalating" if count >= 3 else "seen before — watch closely"

    print(f"\n  {flag_color}{BOLD}⚡ TREND:{RESET}  {BOLD}{count} flag(s) in the last 14 days{RESET}"
          f"  {DIM}— {label}{RESET}")
    for h in display[:4]:
        date_s = str(h.get("date", "?"))[:10]
        itype  = (h.get("issue_type") or h.get("note") or "")[:45]
        dh_s   = h.get("dh", "")
        dh_tag = f"  {DIM}({dh_s}){RESET}" if dh_s else ""
        print(f"  {DIM}  {date_s}  {itype}{RESET}{dh_tag}")
    if count > 4:
        print(f"  {DIM}  + {count - 4} more{RESET}")
    print()


# ── Pre-walk brief ────────────────────────────────────────────────────────────

_BRIEF_OPEN_STATUSES = (
    '"Open","Awaiting Support","Awaiting Triage","To Do","New","In Progress"'
)


def _walkthrough_prewalk_brief(site_code: str, dh: str, email: str, token: str) -> None:
    """Query Jira for open DO tickets in this DH and print a sorted table.

    Runs once after racks are loaded, before the main prompt loop.
    Skipped silently if credentials are missing or Jira is unreachable.
    """
    if not email or not token:
        return

    print(f"  {DIM}Checking Jira for open tickets in {dh}...{RESET}", end="", flush=True)
    jql = (
        f'project = "DO" AND cf[10194] = "{site_code}" '
        f'AND status in ({_BRIEF_OPEN_STATUSES}) '
        f'ORDER BY created ASC'
    )
    try:
        issues = _jql_search(
            jql, email, token, max_results=100,
            fields=["key", "summary", "status", "created",
                    "customfield_10207",   # rack_location
                    "customfield_10193"],  # service_tag
        )
    except Exception:
        print(f"\r{'':60}\r  {DIM}Jira unreachable — skipping pre-walk brief.{RESET}\n")
        return

    print(f"\r{'':60}\r", end="")

    if not issues:
        print(f"  {DIM}No open DO tickets found for {site_code} — floor looks clean.{RESET}\n")
        return

    # Parse rack location and filter to this DH
    current_dh = dh.upper()
    rows = []
    no_rack_count = 0

    for iss in issues:
        fields   = iss.get("fields", {})
        rack_loc = fields.get("customfield_10207") or ""
        if isinstance(rack_loc, dict):
            rack_loc = rack_loc.get("value", "") or ""
        parsed = _parse_rack_location(str(rack_loc))

        key      = iss.get("key", "?")
        summary  = (fields.get("summary") or "")[:48]
        status   = ((fields.get("status") or {}).get("name") or "?")
        created  = (fields.get("created") or "")
        age_secs = _parse_jira_timestamp(created)
        age_str  = _format_age(age_secs) if age_secs else "?"

        if parsed:
            ticket_dh = parsed.get("dh", "").upper()
            if ticket_dh and ticket_dh != current_dh:
                continue
            rows.append({
                "rack":     parsed["rack"],
                "key":      key,
                "summary":  summary,
                "status":   status,
                "age":      age_str,
                "age_secs": age_secs or 0,
            })
        else:
            no_rack_count += 1

    if not rows:
        if no_rack_count:
            print(f"  {DIM}{no_rack_count} open ticket(s) found but none had a rack location in {dh}.{RESET}\n")
        else:
            print(f"  {DIM}No open tickets matched {dh} — floor looks clean.{RESET}\n")
        return

    rows.sort(key=lambda r: r["rack"])
    total = len(rows) + no_rack_count

    print(f"\n  {YELLOW}{BOLD}⚠  {total} open DO ticket(s) in {site_code} / {dh} before you walk:{RESET}\n")
    print(f"  {DIM}{'RACK':<7}  {'TICKET':<12}  {'AGE':<7}  {'STATUS':<22}  SUMMARY{RESET}")
    print(f"  {DIM}{'─' * 72}{RESET}")

    for r in rows[:25]:
        age_color = RED if r["age_secs"] > 86400 * 3 else YELLOW if r["age_secs"] > 86400 else DIM
        rack_s    = f"R{r['rack']:03d}"
        print(f"  {CYAN}{rack_s:<7}{RESET}  {BOLD}{r['key']:<12}{RESET}  "
              f"{age_color}{r['age']:<7}{RESET}  {DIM}{r['status']:<22}  {r['summary']}{RESET}")

    if len(rows) > 25:
        print(f"  {DIM}  ... {len(rows) - 25} more (only top 25 shown){RESET}")
    if no_rack_count:
        print(f"  {DIM}  + {no_rack_count} ticket(s) with no rack location{RESET}")
    print()


# ── Banner ─────────────────────────────────────────────────────────────────────

def _walkthrough_banner(site_code: str, dh: str, notes: list, carryover: list = None,
                         visited: int = 0, total_racks: int = 0,
                         started_at: str = None, zone: str = None):
    """Persistent header printed at the top of every walkthrough screen."""
    count   = len(notes)
    pending = sum(1 for c in (carryover or []) if c.get("status") == "pending")
    noun    = "annotation" if count == 1 else "annotations"

    print(f"\n  {BOLD}{GREEN}◉  WALKTHROUGH MODE{RESET}  {DIM}{'─' * 48}{RESET}")
    zone_tag = f"  {DIM}({zone}){RESET}" if zone else ""
    line2 = (f"  {BOLD}{site_code}{RESET}  {DIM}/{RESET}  {BOLD}{dh}{RESET}{zone_tag}"
             f"  {DIM}│{RESET}  {CYAN}{BOLD}{count}{RESET} {DIM}{noun}{RESET}")
    if pending:
        line2 += f"  {DIM}│{RESET}  {YELLOW}{BOLD}{pending}{RESET} {DIM}unverified from last shift{RESET}"
    print(line2)

    # Progress + ETA line
    if total_racks > 0:
        remaining = total_racks - visited
        # ~30 sec per clean rack, ~2 min per rack with issues
        elapsed_str = ""
        if started_at:
            try:
                t0 = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
                mins = int(elapsed // 60)
                elapsed_str = f"  {DIM}│  {mins}m elapsed{RESET}"
            except Exception:
                pass
        # ETA: use actual pace if we have data, otherwise default 30s/rack
        eta_str = ""
        if visited > 0 and started_at:
            try:
                t0 = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
                pace = elapsed / visited  # seconds per rack
                eta_mins = int((remaining * pace) / 60)
                eta_str = f"  {DIM}│  ~{eta_mins}m remaining{RESET}"
            except Exception:
                pass
        elif remaining > 0:
            eta_mins = int(remaining * 0.5)  # ~30s default
            eta_str = f"  {DIM}│  ~{eta_mins}m est.{RESET}"
        print(f"  {DIM}Visited{RESET}  {BOLD}{visited}{RESET}{DIM}/{total_racks}{RESET}"
              f"{elapsed_str}{eta_str}")
    print(f"  {DIM}{'─' * 68}{RESET}\n")


# ── Zone picker ───────────────────────────────────────────────────────────────

def _walkthrough_pick_zone(rack_lookup: dict) -> tuple:
    """Prompt user to pick a walk zone. Returns (filtered_rack_lookup, zone_label) or (rack_lookup, None)."""
    # Extract and sort rack numbers
    rack_nums = []
    for name in rack_lookup:
        m = _re.search(r'\d+', name)
        if m:
            rack_nums.append(int(m.group()))
    if not rack_nums:
        return rack_lookup, None

    rack_nums.sort()
    mid = rack_nums[len(rack_nums) // 2]
    lo, hi = rack_nums[0], rack_nums[-1]

    print(f"\n  {BOLD}Walk zone:{RESET}")
    print(f"    {BOLD}1{RESET}. Full hall  {DIM}(all {len(rack_lookup)} racks){RESET}")
    print(f"    {BOLD}2{RESET}. First half  {DIM}(R{lo:03d}–R{mid - 1:03d}){RESET}")
    print(f"    {BOLD}3{RESET}. Second half  {DIM}(R{mid:03d}–R{hi:03d}){RESET}")
    print(f"    {BOLD}4{RESET}. Custom range")

    try:
        raw = input(f"\n  Zone [1]: ").strip()
    except (EOFError, KeyboardInterrupt):
        return rack_lookup, None
    if not raw or raw == "1":
        return rack_lookup, None

    if raw == "2":
        start, end = lo, mid - 1
        label = f"R{lo:03d}–R{mid - 1:03d} (first half)"
    elif raw == "3":
        start, end = mid, hi
        label = f"R{mid:03d}–R{hi:03d} (second half)"
    elif raw == "4":
        try:
            range_raw = input(f"  Range (e.g. 200-300): ").strip()
        except (EOFError, KeyboardInterrupt):
            return rack_lookup, None
        parts = _re.split(r'[\s\-–]+', range_raw.upper().lstrip("R"))
        if len(parts) != 2:
            print(f"  {DIM}Invalid range. Using full hall.{RESET}")
            return rack_lookup, None
        try:
            start, end = int(parts[0]), int(parts[1])
        except ValueError:
            print(f"  {DIM}Invalid range. Using full hall.{RESET}")
            return rack_lookup, None
        label = f"R{start:03d}–R{end:03d} (custom)"
    else:
        return rack_lookup, None

    filtered = {}
    for name, obj in rack_lookup.items():
        m = _re.search(r'\d+', name)
        if m and start <= int(m.group()) <= end:
            filtered[name] = obj

    if not filtered:
        print(f"  {DIM}No racks in that range. Using full hall.{RESET}")
        return rack_lookup, None

    print(f"  {GREEN}✓ Zone:{RESET} {label}  {DIM}({len(filtered)} racks){RESET}")
    return filtered, label


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

    # Fetch data halls from NetBox for this site
    site_slug = site_code.lower()
    dh_names = []
    try:
        loc_data = _netbox_get("/dcim/locations/",
                               params={"site": site_slug, "limit": 200})
        if loc_data and loc_data.get("results"):
            for loc in loc_data["results"]:
                name = (loc.get("name") or "").strip()
                if name:
                    dh_names.append(name)
            dh_names.sort()
    except Exception:
        pass

    if dh_names:
        print(f"\n  {BOLD}Select data hall:{RESET}")
        for i, name in enumerate(dh_names, start=1):
            print(f"    {BOLD}{i}{RESET}. {name}")
        try:
            dh_raw = input(f"\n  DH [1-{len(dh_names)}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if not dh_raw:
            return None
        try:
            dh_idx = int(dh_raw)
            if 1 <= dh_idx <= len(dh_names):
                dh = dh_names[dh_idx - 1]
            else:
                dh = dh_raw  # fallback to raw input
        except ValueError:
            dh = dh_raw  # typed a name like "DH1" directly
    else:
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


# ── Full annotation flow ──────────────────────────────────────────────────────

_TERMINAL_STATUSES = {"closed", "done", "resolved", "rma", "rma engaged",
                      "won't do", "wont do", "cancelled", "canceled"}



def _walkthrough_annotate_full(dev: dict, rack_label: str,
                                email: str, token: str,
                                rack_carryover: list = None,
                                dev_hist: list = None,
                                rack_rma: list = None) -> dict | None:
    """Jira context → template picker → auto-stamp ongoing → return annotation dict."""
    dev_name = dev.get("name") or dev.get("display") or ""

    # ── Pre-fetch Jira tickets so user sees context before choosing issue type ──
    prefetched_issues = []
    if dev_name and email and token and _post_comment:
        print(f"\n  {DIM}Checking Jira for {dev_name}...{RESET}", end="", flush=True)
        prefetched_issues = _search_node_history(dev_name, email, token, limit=5)
        print(f"\r{'':60}\r", end="")

        if prefetched_issues:
            print(f"  {DIM}Current tickets:{RESET}")
            for i, iss in enumerate(prefetched_issues[:3], 1):
                k  = iss.get("key", "?")
                st = ((iss.get("fields", {}).get("status") or {}).get("name") or "?")
                sm = (iss.get("fields", {}).get("summary") or "")[:50]
                created_ts = (iss.get("fields", {}).get("created") or "")
                age_secs = _parse_jira_timestamp(created_ts)
                date_str = created_ts[:10] if created_ts else ""
                st_color = YELLOW if st.lower() not in _TERMINAL_STATUSES else DIM
                print(f"  {DIM}{i}.{RESET}  {CYAN}{k}{RESET}  {st_color}[{st}]{RESET}  "
                      f"{DIM}{date_str}  {sm}{RESET}")
            if len(prefetched_issues) > 3:
                print(f"  {DIM}  + {len(prefetched_issues) - 3} more{RESET}")
            print()

    # ── Trend alert ──────────────────────────────────────────────────────────
    _walkthrough_show_trend_alert(dev_hist or [], dev_name)

    # ── RMA tracker status for this device ────────────────────────────────
    if rack_rma:
        dev_rma = [r for r in rack_rma
                   if dev_name and (dev_name.lower() in r.get("node_name", "").lower()
                                    or r.get("node_name", "").lower() in dev_name.lower())]
        if dev_rma:
            rma = dev_rma[0]
            status_val = rma.get("status", "?")
            status_color = YELLOW if "awaiting" in status_val.lower() else RED
            print(f"\n  {RED}{BOLD}⚙  THIS NODE IS IN RMA{RESET}  {status_color}[{status_val}]{RESET}")
            if rma.get("issue"):
                print(f"     Issue:     {DIM}{rma['issue']}{RESET}")
            if rma.get("ho_ticket"):
                print(f"     Ticket:    {CYAN}{rma['ho_ticket']}{RESET}")
            reported = rma.get("date_reported", "")
            age = rma.get("age_days", "")
            if reported or age:
                age_str = f" ({age}d)" if age else ""
                print(f"     Reported:  {DIM}{reported}{age_str}{RESET}")
            if rma.get("last_updated"):
                print(f"     Last walk: {DIM}{rma['last_updated']}{RESET}")
            if rma.get("assigned_to"):
                print(f"     Assigned:  {DIM}{rma['assigned_to']}{RESET}")
            if rma.get("notes"):
                print(f"     Notes:     {DIM}{rma['notes']}{RESET}")
            print()

            # Offer to skip — node is already tracked on the sheet
            try:
                skip = input(f"  {DIM}Already on sheet. Skip annotation? [y/N]:{RESET} ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                skip = "n"
            if skip == "y":
                # Return a minimal annotation so it still appears in the report
                return {
                    "rack":        rack_label,
                    "ru":          dev.get("position"),
                    "device_name": dev_name or "?",
                    "status":      (dev.get("status") or {}).get("label", "?"),
                    "issue_type":  rma.get("status", "On sheet"),
                    "note":        f"[ON SHEET] {rma.get('issue', 'tracked')}",
                    "ongoing":     False,
                    "jira_key":    _normalize_ticket_key(rma.get("ho_ticket", "")),
                    "rma_ticket":  _normalize_ticket_key(rma.get("ho_ticket", "")),
                    "on_sheet":    True,
                    "timestamp":   datetime.now(timezone.utc).strftime(_ISO_FMT),
                }

    result = _walkthrough_pick_template()
    if not result:
        return None
    issue_type, note_text = result

    # Auto-stamp ONGOING if this rack had a carryover issue
    ongoing = bool(rack_carryover)
    if ongoing:
        prior = rack_carryover[0].get("original_note", "")
        note_text = f"[ONGOING] {note_text}  (prev: {prior})"

    # Jira: offer to comment using already-fetched results
    jira_key = None
    if prefetched_issues:
        all_closed = all(
            ((i.get("fields", {}).get("status") or {}).get("name") or "").lower()
            in _TERMINAL_STATUSES
            for i in prefetched_issues
        )

        top = prefetched_issues[0]
        top_key    = top.get("key", "?")
        top_sum    = (top.get("fields", {}).get("summary") or "")[:55]
        top_status = ((top.get("fields", {}).get("status") or {}).get("name") or "?")

        if all_closed:
            print(f"\n  {DIM}Jira: {top_key} [{top_status}] — all tickets closed{RESET}")
        else:
            print(f"\n  {DIM}Jira:{RESET}  {CYAN}{BOLD}{top_key}{RESET}  "
                  f"{DIM}[{top_status}]{RESET}  {top_sum}")
            if len(prefetched_issues) > 1:
                print(f"  {DIM}  + {len(prefetched_issues) - 1} more ticket(s){RESET}")
            jira_key = top_key

    # Capture RMA ticket number if this device has one
    rma_ticket = None
    if rack_rma:
        dev_rma = [r for r in rack_rma
                   if dev_name and (dev_name.lower() in r.get("node_name", "").lower()
                                    or r.get("node_name", "").lower() in dev_name.lower())]
        if dev_rma and dev_rma[0].get("ho_ticket"):
            rma_ticket = dev_rma[0]["ho_ticket"]

    annotation = {
        "rack":        rack_label,
        "ru":          dev.get("position"),
        "device_name": dev_name or "?",
        "status":      (dev.get("status") or {}).get("label", "?"),
        "issue_type":  issue_type,
        "note":        note_text,
        "ongoing":     ongoing,
        "jira_key":    jira_key,
        "rma_ticket":  rma_ticket,
        "timestamp":   datetime.now(timezone.utc).strftime(_ISO_FMT),
    }
    ongoing_tag = f"  {YELLOW}[ONGOING]{RESET}" if ongoing else ""
    print(f"\n  {GREEN}✓ Annotated:{RESET}  {annotation['device_name']}  "
          f"{DIM}— {issue_type}{RESET}{ongoing_tag}")
    if jira_key:
        print(f"  {GREEN}✓ Commented:{RESET}  {jira_key}")
    if rma_ticket:
        print(f"  {GREEN}✓ On sheet:{RESET}   {CYAN}{rma_ticket}{RESET}  {DIM}— already tracked on Device tracker/RMA{RESET}")
    elif rack_rma:
        dev_rma_check = [r for r in rack_rma
                         if dev_name and (dev_name.lower() in r.get("node_name", "").lower()
                                          or r.get("node_name", "").lower() in dev_name.lower())]
        if dev_rma_check:
            sheet_status = dev_rma_check[0].get("status", "?")
            print(f"  {GREEN}✓ On sheet:{RESET}   {DIM}[{sheet_status}] — already tracked on Device tracker/RMA{RESET}")
    return annotation


# ── Report builder ────────────────────────────────────────────────────────────

def _walkthrough_build_report(notes: list, session: dict,
                               carryover: list = None,
                               checklist: dict = None,
                               history: list = None,
                               rma_by_rack: dict = None) -> str:
    """Build a plain-text walkthrough report with optional checklist and carryover."""
    site     = session.get("site_code", "?")
    dh       = session.get("dh", "?")
    tech     = session.get("tech", "")

    header = f"Site: {site}  DH: {dh}"
    zone = session.get("zone")
    if zone:
        header += f"  Zone: {zone}"

    lines = [
        "WALKTHROUGH REPORT",
        "=" * 60,
        header,
        "=" * 60,
        "",
    ]

    # Add to sheet — new issues not yet tracked
    new_for_sheet = [n for n in notes if not n.get("on_sheet")]
    if new_for_sheet:
        lines.append("ADD TO SHEET")
        lines.append("─" * 40)
        lines.append(f"  {'RACK':<8}  {'RU':<4}  {'DEVICE':<30}  {'ISSUE':<20}  TICKET")
        for n in new_for_sheet:
            rack = n.get("rack", "?")
            ru = str(n.get("ru", "?"))
            device = n.get("device_name", "?")
            issue = n.get("issue_type") or n.get("note", "?")
            tickets = []
            if n.get("jira_key"):
                tickets.append(_normalize_ticket_key(n["jira_key"]))
            if n.get("rma_ticket"):
                tickets.append(_normalize_ticket_key(n["rma_ticket"]))
            seen = set()
            tickets = [t for t in tickets if t and t not in seen and not seen.add(t)]
            tkt_str = ", ".join(tickets) if tickets else ""
            lines.append(f"  {rack:<8}  {ru:<4}  {device:<30}  {issue:<20}  {tkt_str}")
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

    # Summary — compact single line
    rack_count = len({n.get("rack") for n in notes})
    new_count = len([n for n in notes if not n.get("on_sheet")])
    sheet_count = len([n for n in notes if n.get("on_sheet")])
    summary = f"Racks: {rack_count}  |  New: {new_count}  |  On sheet: {sheet_count}  |  Total: {len(notes)}"
    if carryover:
        counts = _count_carryover(carryover)
        summary += f"  |  Carryover: {counts[_STATUS_RESOLVED]}/{len(carryover)} resolved"
    lines += ["─" * 60, summary, "─" * 60]
    return "\n".join(lines)


# ── HTML report builder (Concept A — Operations Brief) ────────────────────────

def _he(s: str) -> str:
    """Minimal HTML escape."""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _walkthrough_build_html(notes: list, session: dict,
                             carryover: list = None,
                             history: list = None) -> str:
    """Generate a self-contained Concept A HTML report from walkthrough data."""
    site     = session.get("site_code", "?")
    dh       = session.get("dh", "?")
    started  = session.get("started_at", "?")
    tech     = session.get("tech", "")
    finished = datetime.now(timezone.utc).strftime(_ISO_FMT)

    duration_str = ""
    try:
        t0 = datetime.fromisoformat(started.replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(finished.replace("Z", "+00:00"))
        duration_str = f"{int((t1 - t0).total_seconds() // 60)} min"
    except Exception:
        pass

    date_str = started[:10] if len(started) >= 10 else "?"
    time_range = f"{started[11:16]} – {finished[11:16]} UTC" if len(started) >= 16 else ""

    # Issue type counts
    type_counts: dict[str, int] = {}
    for n in notes:
        t = n.get("issue_type") or "Other"
        type_counts[t] = type_counts.get(t, 0) + 1

    # Ongoing notes
    ongoing_notes = [n for n in notes if "[ONGOING]" in n.get("note", "")]

    # Carryover counts
    c_counts = _count_carryover(carryover) if carryover else {}

    # Trending
    trending = []
    if history:
        trending = _walkthrough_detect_trends(history, min_count=2)

    # ── HTML ──────────────────────────────────────────────────────────────────
    def _issue_class(issue_type: str) -> str:
        t = (issue_type or "").lower()
        if "bmc" in t or "idrac" in t:    return "critical"
        if "power" in t:                   return "warn"
        if "cdu" in t or "cool" in t:     return "cdu"
        return "info"

    def _badge(is_ongoing: bool) -> str:
        cls = "ongoing badge-pulse" if is_ongoing else "new"
        label = "Ongoing" if is_ongoing else "New"
        return f'<span class="a-badge {cls}">{label}</span>'

    def _status_badge(status: str) -> str:
        s = status.lower() if status else ""
        label = {"active": "Active", "staged": "Staged"}.get(s, _he(status))
        return f'<span class="a-badge staged">{label}</span>'

    def _carry_status_html(c: dict) -> str:
        st = c.get("status", "")
        fn = c.get("followup_note", "")
        if st == _STATUS_RESOLVED:
            return '<span class="a-carry-status">Visited · Clear</span>'
        if st == _STATUS_SKIPPED:
            return '<span class="a-carry-status skipped">Not visited</span>'
        if st == _STATUS_PERSISTENT:
            return '<span class="a-carry-status skipped">Still present</span>'
        if st == _STATUS_WORSENED:
            return '<span class="a-carry-status" style="color:#D93025">Worsened</span>'
        return '<span class="a-carry-status skipped">Pending</span>'

    # Build issue type stat cells
    stat_cells = ""
    for itype, count in sorted(type_counts.items(), key=lambda x: -x[1]):
        css = _issue_class(itype)
        stat_cells += (
            f'<div class="a-type-cell {css}">'
            f'<div class="a-type-n" data-counter="{count}">{count}</div>'
            f'<div class="a-type-label">{_he(itype)}</div>'
            f'</div>'
        )

    # Build annotation rows
    annot_rows = ""
    for n in notes:
        rack    = _he(n.get("rack", "?"))
        device  = _he(n.get("device_name", "?"))
        issue   = _he(n.get("issue_type") or n.get("note", "?"))
        detail  = _he(n.get("note", ""))
        ongoing = "[ONGOING]" in n.get("note", "")
        status  = n.get("status", "")
        jira    = n.get("jira_key", "")
        detail_line = f'<div class="a-detail">{detail}</div>' if detail and detail != issue else ""
        jira_line   = f'<div class="a-detail" style="margin-top:3px;font-style:normal;font-size:0.75rem;">Jira: {_he(jira)}</div>' if jira else ""
        annot_rows += (
            f'<div class="a-annot reveal">'
            f'<div class="a-rack">{rack}</div>'
            f'<div class="a-annot-body">'
            f'<div class="a-device">{device}&nbsp;<span style="font-size:0.72rem;font-weight:400;color:var(--cw-sub);">[{_he(status)}]</span></div>'
            f'<div class="a-detail">{issue}</div>'
            f'{detail_line}{jira_line}'
            f'</div>'
            f'<div>{_badge(ongoing)}</div>'
            f'</div>'
        )

    # Build carryover rows
    carry_rows = ""
    if carryover:
        for c in carryover:
            carry_rows += (
                f'<div class="a-carry-row reveal">'
                f'<div class="a-carry-rack">{_he(c.get("rack","?"))}</div>'
                f'<div class="a-carry-note">{_he(c.get("original_note",""))}</div>'
                f'{_carry_status_html(c)}'
                f'</div>'
            )

    # Build trending rows
    trend_rows = ""
    for t in trending:
        recent = t["events"][-3:]
        issues = " → ".join(_he(e.get("issue_type") or e.get("note", "?")) for e in recent)
        dates  = ", ".join(_he(e.get("date", "?")) for e in recent)
        trend_rows += (
            f'<div class="a-annot reveal" style="grid-template-columns:68px 1fr auto;">'
            f'<div class="a-rack">{_he(t["rack"])}</div>'
            f'<div class="a-annot-body">'
            f'<div class="a-device">{_he(t["device_name"])}</div>'
            f'<div class="a-detail">{issues} · {dates}</div>'
            f'</div>'
            f'<div><span class="a-badge staged">{t["count"]}×</span></div>'
            f'</div>'
        )

    # Footer stat counters
    rack_count = len({n.get("rack") for n in notes})
    footer_html = (
        f'<div class="a-footer-stat"><span class="a-footer-n" data-counter="{rack_count}">{rack_count}</span>racks visited</div>'
        f'<div class="a-footer-stat"><span class="a-footer-n" data-counter="{len(notes)}">{len(notes)}</span>new annotations</div>'
    )
    if carryover:
        total_c = len(carryover)
        footer_html += f'<div class="a-footer-stat"><span class="a-footer-n" data-counter="{total_c}">{total_c}</span>carryover</div>'
    if duration_str:
        footer_html += f'<div class="a-footer-stat"><span class="a-footer-n">{_he(duration_str)}</span>duration</div>'

    trending_section = ""
    if trend_rows:
        trending_section = f"""
  <div class="a-section-head reveal">Trending Issues</div>
  {trend_rows}"""

    carry_section = ""
    if carry_rows:
        total_c  = len(carryover)
        resolved = c_counts.get(_STATUS_RESOLVED, 0)
        skipped  = c_counts.get(_STATUS_SKIPPED, 0)
        carry_section = f"""
  <div class="a-section-head reveal">Carryover
    <span class="a-section-count">{total_c} items · {resolved} cleared · {skipped} not visited</span>
  </div>
  {carry_rows}"""

    tech_line = f"Tech: <span class='a-meta-val'>{_he(tech)}</span>&emsp;" if tech else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Walkthrough Report — {_he(site)} / {_he(dh)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Source+Serif+4:ital,wght@0,300;0,400;0,600;1,400&display=swap" rel="stylesheet">
<style>
:root {{
  --cw-bg:#FFFFFF; --cw-text:#0A0F1E; --cw-sub:#4A5568;
  --cw-blue:#2040E8; --cw-blue-mid:#4060F0; --cw-blue-light:#8099F8;
  --cw-blue-mist:#D0D8FA; --cw-border:#E8ECF8; --cw-navy:#0A0F1E;
  --sev-critical:#D93025; --sev-warn:#E8750A; --sev-cdu:#7C3AED;
  --ease-out-expo:cubic-bezier(0.16,1,0.3,1);
  --ease-out-quart:cubic-bezier(0.25,1,0.5,1);
}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--cw-bg);color:var(--cw-text);font-family:system-ui,"Helvetica Neue",sans-serif}}
.wrap{{max-width:860px;margin:0 auto;padding:48px 40px 80px}}
.reveal{{opacity:0;transform:translateY(20px);transition:opacity .55s var(--ease-out-expo),transform .55s var(--ease-out-expo)}}
.reveal.visible{{opacity:1;transform:translateY(0)}}
@keyframes prism-in{{from{{opacity:0;transform:translateX(40px)}}to{{opacity:1;transform:translateX(0)}}}}
.pi{{animation:prism-in .7s var(--ease-out-expo) both}}
.pd1{{animation-delay:.1s}}.pd2{{animation-delay:.2s}}.pd3{{animation-delay:.3s}}.pd4{{animation-delay:.4s}}
@keyframes pulse-badge{{0%,100%{{box-shadow:0 0 0 0 rgba(217,48,37,.4)}}50%{{box-shadow:0 0 0 5px rgba(217,48,37,0)}}}}
.badge-pulse{{animation:pulse-badge 2.4s ease-in-out infinite}}
.a-banner{{background:var(--cw-navy);color:#fff;padding:28px 32px;position:relative;overflow:hidden}}
.a-prisms{{position:absolute;top:-20px;right:-20px;width:220px;height:130px;pointer-events:none}}
.a-prism{{position:absolute;clip-path:polygon(20% 0%,100% 0%,80% 100%,0% 100%)}}
.ap1{{width:160px;height:100px;top:0;right:0;background:var(--cw-blue);opacity:.85}}
.ap2{{width:120px;height:80px;top:10px;right:40px;background:var(--cw-blue-mid);opacity:.65}}
.ap3{{width:90px;height:65px;top:18px;right:68px;background:var(--cw-blue-light);opacity:.45}}
.ap4{{width:62px;height:52px;top:24px;right:92px;background:var(--cw-blue-mist);opacity:.3}}
.a-banner-inner{{display:flex;justify-content:space-between;align-items:flex-end}}
.a-site-label{{font-family:'Bebas Neue',sans-serif;font-size:2.2rem;letter-spacing:.06em;line-height:1;position:relative;z-index:1}}
.a-banner-sub{{font-family:system-ui,sans-serif;font-size:.67rem;letter-spacing:.14em;text-transform:uppercase;opacity:.5;margin-top:5px;position:relative;z-index:1}}
.a-doc-id{{font-family:system-ui,sans-serif;font-size:.7rem;opacity:.5;text-align:right;display:flex;flex-direction:column;align-items:flex-end;gap:2px;position:relative;z-index:1}}
.a-meta-bar{{background:var(--cw-blue-mist);border:1px solid var(--cw-border);border-top:none;padding:9px 28px;font-size:.7rem;letter-spacing:.07em;text-transform:uppercase;color:var(--cw-sub);margin-bottom:36px;font-family:system-ui,sans-serif}}
.a-meta-val{{font-weight:700;color:var(--cw-blue)}}
.a-section-head{{font-family:'Bebas Neue',sans-serif;font-size:1.3rem;letter-spacing:.06em;border-bottom:2px solid var(--cw-blue);padding-bottom:4px;margin-bottom:16px;margin-top:36px;display:flex;justify-content:space-between;align-items:baseline}}
.a-section-count{{font-family:system-ui,sans-serif;font-size:.72rem;font-weight:500;color:var(--cw-sub)}}
.a-type-row{{display:flex;margin-bottom:28px;border:1px solid var(--cw-border)}}
.a-type-cell{{flex:1;padding:14px 16px;border-right:1px solid var(--cw-border)}}
.a-type-cell:last-child{{border-right:none}}
.a-type-n{{font-family:'Bebas Neue',sans-serif;font-size:2.4rem;line-height:1}}
.a-type-label{{font-family:system-ui,sans-serif;font-size:.64rem;text-transform:uppercase;letter-spacing:.09em;color:var(--cw-sub);margin-top:3px}}
.critical .a-type-n{{color:var(--sev-critical)}}.warn .a-type-n{{color:var(--sev-warn)}}
.info .a-type-n{{color:var(--cw-blue)}}.cdu .a-type-n{{color:var(--sev-cdu)}}
.a-annot{{display:grid;grid-template-columns:68px 1fr auto;border-bottom:1px solid var(--cw-border);padding:12px 0;align-items:start;transition:background .15s}}
.a-annot:hover{{background:#f8f9ff;margin:0 -4px;padding:12px 4px}}
.a-rack{{font-family:'Bebas Neue',sans-serif;font-size:1.05rem;letter-spacing:.04em;color:var(--cw-blue)}}
.a-annot-body{{padding-right:14px}}
.a-device{{font-size:.8rem;font-weight:600;margin-bottom:2px;font-family:system-ui,sans-serif}}
.a-detail{{font-size:.82rem;font-style:italic;color:var(--cw-sub);line-height:1.5}}
.a-badge{{font-size:.58rem;font-family:system-ui,sans-serif;text-transform:uppercase;letter-spacing:.1em;padding:2px 7px;border:1px solid;white-space:nowrap;margin-top:3px;display:inline-block}}
.a-badge.ongoing{{border-color:var(--sev-critical);color:var(--sev-critical)}}
.a-badge.new{{border-color:var(--cw-blue);color:var(--cw-blue)}}
.a-badge.staged{{border-color:var(--cw-border);color:var(--cw-sub)}}
.a-carry-row{{display:flex;gap:16px;padding:8px 0;border-bottom:1px solid var(--cw-border);font-size:.8rem;align-items:baseline;font-family:system-ui,sans-serif}}
.a-carry-rack{{width:56px;font-weight:700;flex-shrink:0;color:var(--cw-blue)}}
.a-carry-note{{flex:1;color:var(--cw-sub);font-style:italic}}
.a-carry-status{{font-size:.63rem;text-transform:uppercase;letter-spacing:.08em;color:#16a34a;flex-shrink:0;font-weight:600}}
.a-carry-status.skipped{{color:var(--cw-sub);font-weight:400}}
.a-footer{{margin-top:40px;padding-top:16px;border-top:2px solid var(--cw-blue);display:flex;justify-content:space-between;font-size:.7rem;text-transform:uppercase;letter-spacing:.08em;font-family:system-ui,sans-serif;color:var(--cw-sub)}}
.a-footer-stat{{text-align:center}}
.a-footer-n{{font-family:'Bebas Neue',sans-serif;font-size:2rem;display:block;line-height:1;color:var(--cw-text)}}
@media(prefers-reduced-motion:reduce){{
  *,.reveal{{animation:none!important;transition:none!important;opacity:1!important;transform:none!important}}
}}
</style>
</head>
<body>
<div class="wrap">

  <div class="a-banner">
    <div class="a-prisms">
      <div class="a-prism ap1 pi pd1"></div>
      <div class="a-prism ap2 pi pd2"></div>
      <div class="a-prism ap3 pi pd3"></div>
      <div class="a-prism ap4 pi pd4"></div>
    </div>
    <div class="a-banner-inner">
      <div>
        <div class="a-site-label">{_he(site)} / {_he(dh)}</div>
        <div class="a-banner-sub">Data Hall Walkthrough Report</div>
      </div>
      <div class="a-doc-id">
        <span>{_he(date_str)}</span>
        <span>{_he(time_range)}</span>
        {f"<span>{_he(duration_str)}</span>" if duration_str else ""}
      </div>
    </div>
  </div>

  <div class="a-meta-bar reveal">
    {tech_line}
    Racks visited: <span class="a-meta-val">{rack_count}</span>&emsp;
    Annotations: <span class="a-meta-val">{len(notes)}</span>&emsp;
    {f"Carryover: <span class='a-meta-val'>{len(carryover)} items</span>" if carryover else ""}
  </div>

  <div class="a-section-head reveal">Issue Breakdown
    <span class="a-section-count">{len(notes)} total</span>
  </div>
  <div class="a-type-row reveal">{stat_cells}</div>

  <div class="a-section-head reveal">Today's Annotations</div>
  {annot_rows}

  {carry_section}
  {trending_section}

  <div class="a-footer reveal">{footer_html}</div>

</div>
<script>
const obs = new IntersectionObserver(entries => {{
  entries.forEach(e => {{
    if (!e.isIntersecting) return;
    e.target.classList.add('visible');
    e.target.querySelectorAll('[data-counter]').forEach(animateCounter);
    if (e.target.hasAttribute('data-counter')) animateCounter(e.target);
    obs.unobserve(e.target);
  }});
}}, {{threshold: 0.1}});
document.querySelectorAll('.reveal').forEach(el => obs.observe(el));
// Also trigger immediately for elements already in view
document.querySelectorAll('.reveal').forEach(el => {{
  const r = el.getBoundingClientRect();
  if (r.top < window.innerHeight) el.classList.add('visible');
}});
function animateCounter(el) {{
  const target = parseInt(el.dataset.counter);
  if (isNaN(target)) return;
  const start = performance.now();
  const update = now => {{
    const p = Math.min((now - start) / 700, 1);
    const e = 1 - Math.pow(1 - p, 4);
    el.textContent = Math.round(e * target);
    if (p < 1) requestAnimationFrame(update);
    else el.textContent = target;
  }};
  requestAnimationFrame(update);
}}
</script>
</body>
</html>"""


def _walkthrough_open_html(notes: list, session: dict,
                            carryover: list = None,
                            history: list = None) -> bool:
    """Build HTML report, write to fixed path, open in browser. Returns True on success."""
    try:
        html = _walkthrough_build_html(notes, session, carryover, history)
        with open(_WALKTHROUGH_HTML_PATH, "w") as f:
            f.write(html)
        subprocess.run(["open", _WALKTHROUGH_HTML_PATH], check=True)
        return True
    except Exception as e:
        return False


# ── Finish screen ─────────────────────────────────────────────────────────────

def _walkthrough_finish(notes: list, session: dict, state: dict,
                         carryover: list = None, checklist: dict = None,
                         rma_by_rack: dict = None):
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

    report = _walkthrough_build_report(notes, session, carryover, checklist, history,
                                       rma_by_rack=rma_by_rack)

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

    # Build pasteable RMA update lines (tab-separated for Google Sheets)
    rma_paste_lines = []
    if rma_by_rack:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        for rack, items in sorted((rma_by_rack or {}).items()):
            for rma in items:
                ws = rma.get("walkthrough_status")
                if ws:
                    node = rma.get("node_name", "?")
                    rma_paste_lines.append(f"{rack}\t{node}\t{ws}\t{today}")
    has_rma_updates = bool(rma_paste_lines)

    while True:
        print(f"  {BOLD}c{RESET}  copy to clipboard")
        print(f"  {BOLD}v{RESET}  view report")
        print(f"  {BOLD}o{RESET}  open in browser")
        if has_rma_updates:
            print(f"  {BOLD}r{RESET}  copy RMA updates (tab-separated, {len(rma_paste_lines)} row(s))")
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
        elif act == "r" and has_rma_updates:
            rma_text = "\n".join(rma_paste_lines)
            try:
                subprocess.run(["pbcopy"], input=rma_text.encode(), check=True)
                print(f"  {GREEN}Copied {len(rma_paste_lines)} RMA update(s) to clipboard.{RESET}")
                print(f"  {DIM}Paste into the Device tracker/RMA sheet.{RESET}\n")
            except Exception as e:
                print(f"  {DIM}Clipboard failed: {e}{RESET}")
                print(f"\n{rma_text}\n")
        elif act == "o":
            ok = _walkthrough_open_html(notes, session, carryover, history)
            if ok:
                print(f"  {GREEN}Opened in browser.{RESET}  {DIM}{_WALKTHROUGH_HTML_PATH}{RESET}\n")
            else:
                print(f"  {DIM}Could not open browser — check that 'open' is available.{RESET}\n")
        elif act == "v":
            _clear_screen()
            print()
            for line in report.splitlines():
                print(f"  {line}")
            print()
            while True:
                print(f"  {BOLD}c{RESET}  copy to clipboard")
                print(f"  {BOLD}ENTER{RESET}  go back\n")
                try:
                    sub = input(f"  Action: ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    break
                if sub == "c":
                    try:
                        subprocess.run(["pbcopy"], input=report.encode(), check=True)
                        print(f"  {GREEN}Copied to clipboard.{RESET}\n")
                    except Exception as e:
                        print(f"  {DIM}Clipboard failed: {e}{RESET}\n")
                elif sub == "":
                    break
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

    site_slug = site_code.lower()

    # ── Pre-fetch rack index from NetBox once ─────────────────────────────────
    _clear_screen()
    print(f"\n  {BOLD}{GREEN}◉  WALKTHROUGH MODE{RESET}  {DIM}Loading racks from NetBox...{RESET}")
    rack_lookup: dict[str, dict] = {}
    try:
        # Resolve the actual NetBox location slug/ID for this DH.
        # User may type "DH1" but NetBox slug could be "dh1", "dh-1", etc.
        dh_lower = dh.lower()
        location_id = None
        loc_data = _netbox_get("/dcim/locations/",
                               params={"site": site_slug, "limit": 200})
        if loc_data and loc_data.get("results"):
            # Try exact slug match first, then name-contains match
            for loc in loc_data["results"]:
                slug = (loc.get("slug") or "").lower()
                name = (loc.get("name") or "").lower()
                if slug == dh_lower or name == dh_lower:
                    location_id = loc["id"]
                    break
            if location_id is None:
                # Fuzzy: strip separators and compare (dh-1 vs dh1)
                dh_stripped = dh_lower.replace("-", "").replace("_", "").replace(" ", "")
                for loc in loc_data["results"]:
                    slug = (loc.get("slug") or "").lower().replace("-", "").replace("_", "")
                    name = (loc.get("name") or "").lower().replace("-", "").replace("_", "").replace(" ", "")
                    if slug == dh_stripped or name == dh_stripped:
                        location_id = loc["id"]
                        break
        results = []
        if location_id is not None:
            dh_racks = _netbox_get("/dcim/racks/",
                                   params={"site": site_slug, "location_id": location_id, "limit": 1000})
            results = (dh_racks or {}).get("results", [])
        else:
            # Try raw slug as fallback (original behavior)
            dh_racks = _netbox_get("/dcim/racks/",
                                   params={"site": site_slug, "location": dh_lower, "limit": 1000})
            results = (dh_racks or {}).get("results", [])

        if not results:
            all_racks = _netbox_get("/dcim/racks/",
                                    params={"site": site_slug, "limit": 1000})
            results = (all_racks or {}).get("results", [])
        for r in results:
            rack_lookup[r.get("name", "")] = r

        # ── Post-filter: keep only racks whose NetBox location matches the selected DH ──
        if rack_lookup:
            dh_stripped = dh.lower().replace("-", "").replace("_", "").replace(" ", "")
            filtered = {}
            for rname, robj in rack_lookup.items():
                loc = robj.get("location") or {}
                loc_name = (loc.get("name") or "").lower().replace("-", "").replace("_", "").replace(" ", "")
                loc_slug = (loc.get("slug") or "").lower().replace("-", "").replace("_", "")
                if loc_name == dh_stripped or loc_slug == dh_stripped:
                    filtered[rname] = robj
            if filtered:
                rack_lookup = filtered
            # If no racks matched the DH filter, keep all (avoid empty walkthrough)
    except Exception:
        pass

    if not rack_lookup:
        print(f"  {DIM}No racks found for {site_code}. Check site name.{RESET}")
        _brief_pause()
        return state

    print(f"  {DIM}{len(rack_lookup)} rack(s) loaded.{RESET}")

    # ── Zone selection (split hall between techs) ────────────────────────────
    rack_lookup, zone_label = _walkthrough_pick_zone(rack_lookup)
    if zone_label:
        session["zone"] = zone_label

    # ── Pre-walk brief: open Jira tickets in this DH ──────────────────────────
    _walkthrough_prewalk_brief(site_code, dh, email, token)

    # ── Pre-fetch RMA tracker data for this DH ──────────────────────────────
    _STALE_THRESHOLD_SECS = 8 * 3600  # 8 hours
    rma_by_rack: dict = {}
    if _get_rma_data and _rma_available():
        print(f"  {DIM}Loading RMA tracker...{RESET}", end="", flush=True)
        try:
            rma_by_rack = _get_rma_data(dh) or {}
        except Exception:
            rma_by_rack = {}
        rma_count = sum(len(v) for v in rma_by_rack.values())
        print(f"\r{'':60}\r", end="")
        age = _rma_file_age()
        age_secs = _rma_file_age_secs()
        stale = age_secs > _STALE_THRESHOLD_SECS
        if rma_count:
            if stale:
                print(f"  {YELLOW}{BOLD}{rma_count}{RESET} {YELLOW}node(s) in RMA tracker across "
                      f"{len(rma_by_rack)} rack(s)  (CSV: {age} — STALE){RESET}")
            else:
                print(f"  {RED}{BOLD}{rma_count}{RESET} {DIM}node(s) in RMA tracker across "
                      f"{len(rma_by_rack)} rack(s)  (CSV: {age}){RESET}")
        if stale:
            try:
                dl = input(f"  {YELLOW}CSV is stale ({age}). Refresh from Downloads? [Y/n]:{RESET} ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                dl = "n"
            if dl != "n":
                # Clean up old duplicates — macOS creates "name (1).csv", "name (2).csv"
                _cleanup_old_tracker_dupes()
                # Re-scan Downloads for the newest file
                fresh = _find_latest_file()
                if fresh:
                    try:
                        rma_by_rack = _get_rma_data(dh) or {}
                        rma_count = sum(len(v) for v in rma_by_rack.values())
                        new_age = _rma_file_age()
                        print(f"  {GREEN}✓ Reloaded:{RESET}  {rma_count} node(s) across "
                              f"{len(rma_by_rack)} rack(s)  {DIM}(CSV: {new_age}){RESET}")
                    except Exception:
                        print(f"  {YELLOW}Reload failed — continuing with old data{RESET}")
                else:
                    print(f"  {YELLOW}No tracker file found in Downloads.{RESET}")
                    try:
                        dl2 = input(f"  Open the sheet to download? [Y/n]: ").strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        dl2 = "n"
                    if dl2 != "n":
                        try:
                            subprocess.Popen(["open", _RMA_SHEET_URL],
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            print(f"  {DIM}Opening sheet... download the CSV, then restart walkthrough.{RESET}")
                            return
                        except Exception:
                            print(f"  {DIM}{_RMA_SHEET_URL}{RESET}")
    else:
        if _get_rma_data and not _rma_available():
            print(f"  {YELLOW}RMA tracker: no CSV in Downloads.{RESET}")
            try:
                dl = input(f"  Open the Device tracker/RMA to download? [Y/n]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                dl = "n"
            if dl != "n":
                try:
                    subprocess.Popen(["open", _RMA_SHEET_URL],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    print(f"  {DIM}Opening sheet... download the CSV, then restart walkthrough.{RESET}")
                except Exception:
                    print(f"  {DIM}{_RMA_SHEET_URL}{RESET}")

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
    _visited_set: set = set()
    _started_ts = session.get("started_at", "")
    while True:
        _clear_screen()
        _walkthrough_banner(site_code, dh, notes, carryover,
                             visited=len(_visited_set), total_racks=len(rack_lookup),
                             started_at=_started_ts, zone=session.get("zone"))

        # Build sorted pending list — used for numbered shortcuts
        pending_items = [c for c in carryover if c.get("status") == "pending"]
        pending_sorted = sorted(pending_items, key=_rack_sort_key)

        # ── Skip guide: show which racks need attention vs clean ─────────
        flagged_racks: set = set()
        for c in carryover:
            if c.get("status") == "pending" and c.get("rack"):
                flagged_racks.add(c["rack"])
        for rk in rma_by_rack:
            flagged_racks.add(rk)
        # Racks already annotated this session
        visited_racks = {n.get("rack") for n in notes if n.get("rack")}
        clean_count = len(rack_lookup) - len(flagged_racks - visited_racks)
        needs_attn = sorted(flagged_racks - visited_racks, key=lambda r: r)
        if needs_attn:
            # Show issue count per rack
            rack_labels = []
            for rk in needs_attn:
                count = len(rma_by_rack.get(rk, []))
                co_count = sum(1 for c in carryover
                               if c.get("rack") == rk and c.get("status") == "pending")
                total = count + co_count
                rack_labels.append(f"{rk} ({total})" if total else rk)
            print(f"  {DIM}Already tracked ({RESET}{YELLOW}{BOLD}{len(needs_attn)}{RESET}{DIM}):{RESET}  "
                  f"{YELLOW}{', '.join(rack_labels)}{RESET}")
            print(f"  {GREEN}{BOLD}{clean_count}{RESET}{DIM} racks are clean.{RESET}")
            print()

        if pending_sorted:
            print(f"  {YELLOW}⚠  From last walkthrough — type # to verify:{RESET}\n")
            for idx, c in enumerate(pending_sorted, start=1):
                rack  = c.get("rack", "?")
                note  = c.get("original_note", "")[:55]
                print(f"    {BOLD}{idx:>2}{RESET}.  {CYAN}{rack}{RESET}  {DIM}{note}{RESET}")
            print()

        print(f"  {DIM}[{RESET}{CYAN}sheet{RESET}{DIM}]  Device tracker/RMA{RESET}")
        print(f"  {DIM}[{RESET}{CYAN}map{RESET}{DIM}]    Overhead map{RESET}")
        if needs_attn:
            print(f"  {DIM}[{RESET}{CYAN}verify{RESET}{DIM}] Cycle through {len(needs_attn)} flagged rack(s){RESET}")
        print()

        try:
            raw = input(f"  # or rack # (or 'done' / 'list' / 'sheet' / 'q'): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            raw = "q"

        # ── Open Google Sheets ────────────────────────────────────────────────
        if raw == "sheet":
            try:
                subprocess.Popen(["open", _RMA_SHEET_URL],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                print(f"  {DIM}Opening Device tracker/RMA...{RESET}")
            except Exception:
                print(f"  {DIM}{_RMA_SHEET_URL}{RESET}")
            time.sleep(1)
            continue

        if raw == "map":
            try:
                subprocess.Popen(["open", _OVERHEAD_MAP_URL],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                print(f"  {DIM}Opening Overhead map...{RESET}")
            except Exception:
                print(f"  {DIM}{_OVERHEAD_MAP_URL}{RESET}")
            time.sleep(1)
            continue

        # ── Verify mode: auto-cycle through flagged racks ─────────────────
        if raw == "verify" and needs_attn:
            verify_queue = []
            for rk in needs_attn:
                m = _re.search(r'\d+', rk)
                if m:
                    verify_queue.append(int(m.group()))
            if verify_queue:
                print(f"  {CYAN}Verify mode: cycling through {len(verify_queue)} flagged rack(s)...{RESET}")
                time.sleep(0.8)
                for vr_num in verify_queue:
                    vr_label = f"R{vr_num:03d}"
                    vr_obj = (rack_lookup.get(vr_label) or
                              rack_lookup.get(str(vr_num)) or
                              rack_lookup.get(str(vr_num).zfill(3)))
                    if not vr_obj:
                        continue
                    vr_devices = _netbox_get_rack_devices(vr_obj["id"])
                    vr_racked = [d for d in vr_devices if d.get("position") is not None]
                    _visited_set.add(vr_label)
                    _now_ts = datetime.now(timezone.utc).strftime(_ISO_FMT)
                    for _c in carryover:
                        if _c.get("rack") == vr_label and _c.get("status") == "pending":
                            _c["status"]     = "resolved"
                            _c["checked_at"] = _now_ts
                            _c["followup_note"] = "visited — no new issue noted"
                    state["walkthrough_carryover"] = carryover

                    # Show rack screen
                    _verify_done = False
                    while not _verify_done:
                        _clear_screen()
                        _walkthrough_banner(site_code, dh, notes, carryover,
                             visited=len(_visited_set), total_racks=len(rack_lookup),
                             started_at=_started_ts, zone=session.get("zone"))
                        remaining = verify_queue[verify_queue.index(vr_num):]
                        print(f"  {CYAN}{BOLD}VERIFY MODE{RESET}  {DIM}{len(remaining)} rack(s) remaining{RESET}\n")
                        print(f"  {BOLD}{vr_label}{RESET}  {DIM}— {len(vr_racked)} device(s){RESET}\n")

                        vr_carryover = [c for c in carryover if c.get("rack") == vr_label]
                        if vr_carryover:
                            print(f"  {YELLOW}{'━' * 60}{RESET}")
                            print(f"  {YELLOW}{BOLD}⚠  FLAGGED IN LAST WALKTHROUGH:{RESET}")
                            for c in vr_carryover:
                                print(f"     {BOLD}→{RESET}  {c['original_note']}")
                            print(f"  {YELLOW}{'━' * 60}{RESET}")
                            print()

                        vr_rack_rma = rma_by_rack.get(vr_label, [])
                        if vr_rack_rma:
                            print(f"  {RED}{BOLD}⚙  RMA TRACKER — {len(vr_rack_rma)} node(s):{RESET}")
                            for rma in vr_rack_rma:
                                print(f"  {BOLD}{rma.get('node_name', '?')}{RESET}"
                                      f"  {DIM}[{rma.get('status', '?')}]{RESET}")
                                if rma.get("issue"):
                                    print(f"    {DIM}{rma['issue']}{RESET}")
                            print()

                        for i, d in enumerate(vr_racked, start=1):
                            name = d.get("name") or d.get("display") or "?"
                            status = (d.get("status") or {}).get("label", "?")
                            pos = d.get("position")
                            pos_s = str(int(pos)) if isinstance(pos, (int, float)) else str(pos or "?")
                            print(f"    {BOLD}{i:2}{RESET}.  RU{pos_s:>3}  {name:<32}  {DIM}[{status}]{RESET}")

                        print(f"\n  {DIM}ENTER = looks good, next rack  ·  # = annotate device  ·  r = rack note  ·  x = exit verify{RESET}")
                        try:
                            vr_input = input(f"\n  Device #: ").strip()
                        except (EOFError, KeyboardInterrupt):
                            vr_input = "x"

                        if not vr_input:
                            _verify_done = True
                        elif vr_input.lower() == "x":
                            _verify_done = True
                            verify_queue.clear()
                        elif vr_input.lower() == "r":
                            rack_dev = {"name": vr_label, "display": vr_label,
                                        "position": None, "status": {"label": "Rack"}}
                            ann = _walkthrough_annotate_full(rack_dev, vr_label, email, token,
                                                             rack_carryover=vr_carryover,
                                                             rack_rma=vr_rack_rma)
                            if ann:
                                notes.append(ann)
                                for item in vr_carryover:
                                    if item.get("status") == "pending":
                                        item["status"] = "persistent"
                                        item["checked_at"] = datetime.now(timezone.utc).strftime(_ISO_FMT)
                                        item["followup_note"] = ann.get("note", "")
                                state["walkthrough_carryover"] = carryover
                                _walkthrough_save_notes(state, notes, session)
                        else:
                            try:
                                dev_idx = int(vr_input)
                                if 1 <= dev_idx <= len(vr_racked):
                                    dev = vr_racked[dev_idx - 1]
                                    ann = _walkthrough_annotate_full(dev, vr_label, email, token,
                                                                      rack_carryover=vr_carryover,
                                                                      dev_hist=_dev_hist_map.get(
                                                                          dev.get("name") or dev.get("display") or "", []),
                                                                      rack_rma=vr_rack_rma)
                                    if ann:
                                        notes.append(ann)
                                        for item in vr_carryover:
                                            if item.get("status") == "pending":
                                                item["status"] = "persistent"
                                                item["checked_at"] = datetime.now(timezone.utc).strftime(_ISO_FMT)
                                                item["followup_note"] = ann.get("note", "")
                                        state["walkthrough_carryover"] = carryover
                                        _walkthrough_save_notes(state, notes, session)
                            except ValueError:
                                pass
            continue

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
                    item["status"]       = _STATUS_SKIPPED
                    item["checked_at"]   = _ts
                    item["followup_note"] = "not visited this shift"
                state["walkthrough_carryover"] = carryover
                _walkthrough_save_notes(state, notes, session)

            _walkthrough_finish(notes, session, state, carryover, checklist,
                                rma_by_rack=rma_by_rack)
            _clear_screen()
            return state

        # ── List annotations ──────────────────────────────────────────────────
        if raw == "list":
            _clear_screen()
            _walkthrough_banner(site_code, dh, notes, carryover,
                             visited=len(_visited_set), total_racks=len(rack_lookup),
                             started_at=_started_ts, zone=session.get("zone"))
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
            _walkthrough_banner(site_code, dh, notes, carryover,
                             visited=len(_visited_set), total_racks=len(rack_lookup),
                             started_at=_started_ts, zone=session.get("zone"))
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
        _visited_set.add(rack_label)

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
            _walkthrough_banner(site_code, dh, notes, carryover,
                             visited=len(_visited_set), total_racks=len(rack_lookup),
                             started_at=_started_ts, zone=session.get("zone"))
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

            # Show RMA tracker alert for this rack
            rack_rma = rma_by_rack.get(rack_label, [])
            if rack_rma:
                print(f"  {RED}{BOLD}⚙  RMA TRACKER — {len(rack_rma)} node(s) in this rack:{RESET}")
                print(f"  {RED}{'━' * 55}{RESET}")
                for i, rma in enumerate(rack_rma):
                    status_val = rma.get("status", "?")
                    status_color = YELLOW if "awaiting" in status_val.lower() else RED
                    print(f"  {BOLD}{rma.get('node_name', '?')}{RESET}"
                          f"  {status_color}[{status_val}]{RESET}")
                    if rma.get("issue"):
                        print(f"    Issue:     {DIM}{rma['issue']}{RESET}")
                    if rma.get("ho_ticket"):
                        print(f"    Ticket:    {CYAN}{rma['ho_ticket']}{RESET}")
                    reported = rma.get("date_reported", "")
                    age = rma.get("age_days", "")
                    if reported or age:
                        age_str = f" ({age}d)" if age else ""
                        print(f"    Reported:  {DIM}{reported}{age_str}{RESET}")
                    if rma.get("last_updated"):
                        print(f"    Last walk: {DIM}{rma['last_updated']}{RESET}")
                    if rma.get("assigned_to"):
                        print(f"    Assigned:  {DIM}{rma['assigned_to']}{RESET}")
                    if rma.get("notes"):
                        print(f"    Notes:     {DIM}{rma['notes']}{RESET}")
                    if i < len(rack_rma) - 1:
                        print(f"  {DIM}{'─' * 55}{RESET}")
                print(f"  {RED}{'━' * 55}{RESET}")
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

            hints = f"ENTER to go back  ·  r = note on whole rack"
            if rack_rma:
                hints += f"  ·  u = update RMA status"
            print(f"\n  {DIM}{hints}{RESET}")
            try:
                dev_raw = input(f"\n  Device #: ").strip()
            except (EOFError, KeyboardInterrupt):
                dev_raw = ""

            if not dev_raw:
                # Carryover already auto-resolved when rack was opened — just leave
                break

            # Update RMA status for nodes in this rack
            if dev_raw.lower() == "u" and rack_rma:
                for i, rma in enumerate(rack_rma):
                    node = rma.get("node_name", "?")
                    cur  = rma.get("status", "?")
                    print(f"\n  {BOLD}{node}{RESET}  {DIM}[{cur}]{RESET}")
                    print(f"    {BOLD}1{RESET}. Still present")
                    print(f"    {BOLD}2{RESET}. Resolved / fixed")
                    print(f"    {BOLD}3{RESET}. Worse / escalate")
                    print(f"    {DIM}ENTER to skip{RESET}")
                    try:
                        choice = input(f"    Status: ").strip()
                    except (EOFError, KeyboardInterrupt):
                        choice = ""
                    status_map = {"1": "still present", "2": "resolved", "3": "worsened"}
                    if choice in status_map:
                        rma["walkthrough_status"] = status_map[choice]
                        rma["walkthrough_ts"] = datetime.now(timezone.utc).strftime(_ISO_FMT)
                        print(f"    {GREEN}Marked: {status_map[choice]}{RESET}")
                print(f"\n  {DIM}RMA updates saved to walkthrough report.{RESET}")
                time.sleep(1)
                continue

            # Rack-level annotation (no specific device)
            if dev_raw.lower() == "r":
                rack_dev = {
                    "name":     rack_label,
                    "display":  rack_label,
                    "position": None,
                    "status":   {"label": "Rack"},
                }
                annotation = _walkthrough_annotate_full(rack_dev, rack_label, email, token,
                                                         rack_carryover=rack_carryover,
                                                         rack_rma=rack_rma)
            else:
                try:
                    dev_idx = int(dev_raw)
                    if dev_idx < 1 or dev_idx > len(racked):
                        print(f"  {DIM}Invalid — enter 1–{len(racked)} or ENTER to go back.{RESET}")
                        time.sleep(0.8)
                        continue
                    dev = racked[dev_idx - 1]
                except ValueError:
                    print(f"  {DIM}Enter a device number, r for rack note, or ENTER to go back.{RESET}")
                    time.sleep(0.8)
                    continue

                # ── Show device info before annotation ──
                _dev_name = dev.get("name") or dev.get("display") or "?"
                _dev_serial = dev.get("serial") or ""
                _dev_status = (dev.get("status") or {}).get("label", "?")
                _dev_role = (dev.get("device_role") or {}).get("name", "")
                _dev_type = (dev.get("device_type") or {}).get("display", "")
                _dev_ip4 = (dev.get("primary_ip4") or {}).get("address", "")
                _dev_ip6 = (dev.get("primary_ip6") or {}).get("address", "")
                _dev_ip = _dev_ip4 or _dev_ip6 or (dev.get("primary_ip") or {}).get("address", "")
                _dev_tenant = (dev.get("tenant") or {}).get("name", "")
                _dev_pos = dev.get("position")
                _dev_pos_s = str(int(_dev_pos)) if isinstance(_dev_pos, (int, float)) else str(_dev_pos or "?")
                print(f"\n  {BOLD}{_dev_name}{RESET}  {DIM}[{_dev_status}]{RESET}")
                info_parts = []
                if _dev_type:
                    info_parts.append(f"Type: {_dev_type}")
                if _dev_serial:
                    info_parts.append(f"S/N: {_dev_serial}")
                if _dev_ip:
                    info_parts.append(f"IP: {_dev_ip}")
                if _dev_role:
                    info_parts.append(f"Role: {_dev_role}")
                if _dev_tenant:
                    info_parts.append(f"Tenant: {_dev_tenant}")
                info_parts.append(f"RU: {_dev_pos_s}")
                print(f"  {DIM}{'  ·  '.join(info_parts)}{RESET}")

                annotation = _walkthrough_annotate_full(dev, rack_label, email, token,
                                                         rack_carryover=rack_carryover,
                                                         dev_hist=_dev_hist_map.get(
                                                             dev.get("name") or dev.get("display") or "", []),
                                                         rack_rma=rack_rma)
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
