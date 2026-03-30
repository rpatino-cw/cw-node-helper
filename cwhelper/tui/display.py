"""Display formatting — pretty print, JSON output, detail panels."""
from __future__ import annotations

import json
import os
import re
import textwrap
import webbrowser

from cwhelper import config as _cfg
from cwhelper.config import (
    APP_VERSION, BOLD, DIM, RESET, RED, GREEN, YELLOW, CYAN, WHITE, MAGENTA, BLUE,
    JIRA_BASE_URL, SDX_PROJECTS, ISSUE_DETAIL_FIELDS,
)
__all__ = [
    '_status_color', '_prompt_select',
    '_print_linked_inline', '_print_diagnostics_inline', '_print_sla_detail',
    '_clear_screen', '_print_banner', '_print_help',
    '_print_pretty', '_print_json', '_print_raw',
    '_print_prep_brief',
]
from cwhelper.services.ai import _ai_available, _ai_dispatch, _ai_chat_loop, _ai_find_ticket, _ai_summarize, _suggest_comments, _pick_or_type_comment
from cwhelper.clients.jira import _is_mine, _jira_get, _jira_get_issue, _get_credentials, _get_my_account_id
from cwhelper.clients.netbox import _netbox_available, _netbox_find_device, _netbox_get_interfaces, _netbox_trace_interface, _build_netbox_context
from cwhelper.clients.grafana import _build_grafana_urls, _find_psu_dashboard_url
from cwhelper.state import _load_user_state
from cwhelper.cache import _escape_jql, _lookup_ib_connections, _classify_port_role
from cwhelper.services.context import _parse_rack_location, _format_age, _parse_jira_timestamp, _short_device_name, _extract_comments, _adf_to_plain_text, _build_context




def _status_color(status_name: str) -> tuple:
    """Return (ansi_color, dot_char) for a status name."""
    s = status_name.lower()
    if s in ("closed", "done", "resolved", "canceled"):
        return GREEN, "\u25cf"
    if s in ("in progress", "open", "reopened"):
        return YELLOW, "\u25cf"
    if s in ("on hold", "blocked", "paused"):
        return MAGENTA, "\u25cb"
    if s in ("waiting for support", "verification"):
        return BLUE, "\u25cf"
    if s in ("to do", "new", "waiting for triage"):
        return DIM, "\u25cb"
    return DIM, "\u25cf"



def _prompt_select(items: list, label_fn, extra_hint: str = "") -> dict | str | None:
    """Generic numbered selection prompt.

    items       — list of dicts
    label_fn    — function(index, item) -> str to display for each item
    extra_hint  — optional extra text for the prompt (e.g. ", 'x' for NetBox")
    Returns the chosen item dict, a special string command (e.g. "x"), or None.
    """
    if not items:
        return None

    for i, item in enumerate(items, start=1):
        print(label_fn(i, item))
    print()

    prompt_text = f"  Type a number (1-{len(items)}){extra_hint}, {BOLD}b{RESET} back, {BOLD}m{RESET} menu, or ENTER to refresh: "
    for _ in range(3):
        try:
            raw = input(prompt_text).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None

        if raw == "":
            return "refresh"
        if raw.lower() in ("q", "quit", "exit"):
            return "quit"
        if raw.lower() in ("m", "menu"):
            return "menu"
        if raw.lower() in ("b", "back"):
            return None
        if raw.lower() == "ai":
            return "ai"
        # Pass through single-letter/special commands to the caller
        if raw.lower() in ("x", "n", "a", "e", "f", "s", "r", "l", "t") or raw == "*":
            return raw
        try:
            choice = int(raw)
            if 1 <= choice <= len(items):
                return items[choice - 1]
            print(f"  That number is out of range. Pick between 1 and {len(items)}.")
        except ValueError:
            print(f"  Not a number. Type 1-{len(items)} to pick, or q to go back.")

    return None



def _print_linked_inline(ctx: dict):
    """Print linked tickets."""
    if not ctx.get("linked_issues"):
        return
    print()
    print(f"  {BOLD}Linked tickets{RESET}")
    for link in ctx["linked_issues"]:
        lc, ld = _status_color(link["status"])
        print(f"    {BOLD}{link['key']:<12}{RESET} {lc}{ld} {link['status']:<14}{RESET} {DIM}\u2192{RESET} {link['relationship']}")
    print()



def _print_diagnostics_inline(ctx: dict):
    """Print diagnostic links with numbered buttons to open in browser."""
    links = ctx.get("diag_links", [])
    if not links:
        return

    line = "\u2500" * 50
    print(f"\n  {DIM}{line}{RESET}")
    print(f"  {BOLD}{BLUE}Diagnostics{RESET}  {DIM}({len(links)} link{'s' if len(links) != 1 else ''}){RESET}\n")

    for i, dl in enumerate(links, 1):
        label = dl.get("label", "link")
        url = dl.get("url", "")
        # Color-code by link type
        if "grafana" in url.lower():
            color = GREEN
        elif "sherlock" in url.lower() or "sheriff" in url.lower():
            color = YELLOW
        elif "netbox" in url.lower():
            color = CYAN
        else:
            color = MAGENTA
        print(f"    {color}{BOLD}[{i}]{RESET} {WHITE}{label}{RESET}")
        print(f"        {DIM}{url[:80]}{RESET}")
        print()

    print(f"  {DIM}Press [1-{len(links)}] to open in browser, or any key to go back{RESET}\n")

    try:
        pick = input("  > ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return

    if pick.isdigit():
        idx = int(pick) - 1
        if 0 <= idx < len(links):
            url = links[idx]["url"]
            print(f"  {DIM}Opening {links[idx]['label']}...{RESET}")
            webbrowser.open(url)



def _print_sla_detail(ctx: dict):
    """Show detailed SLA breakdown with personalized, ticket-specific DCT guidance."""
    sla_values = ctx.get("sla", [])

    status = ctx.get("status", "")
    age_secs = ctx.get("status_age_seconds", 0)
    age_str = _format_age(age_secs) if age_secs else "unknown"
    issue_key = ctx.get("issue_key", "?")

    # Ticket created age
    created_age = _parse_jira_timestamp(ctx.get("created", ""))
    created_str = _format_age(created_age) if created_age else "unknown"

    # ── Personalization signals (all from ctx, no new API calls) ──────────
    assignee = ctx.get("assignee")
    ho = ctx.get("ho_context")  # {key, status, summary, last_note, hint} or None

    # Last comment info
    last_cmt_age = None
    last_cmt_author = None
    last_cmt_str = None
    raw_comments = ctx.get("_raw_comments", [])
    comment_count = ctx.get("_comment_count", 0) or len(raw_comments)
    if raw_comments:
        _lc = raw_comments[-1]
        last_cmt_author = (_lc.get("author") or {}).get("displayName")
        _lc_ts = _lc.get("created")
        if _lc_ts:
            last_cmt_age = _parse_jira_timestamp(_lc_ts)
            last_cmt_str = _format_age(last_cmt_age)

    # Last modified
    updated_age = _parse_jira_timestamp(ctx.get("updated", ""))
    updated_str = _format_age(updated_age) if updated_age else None

    # Device identifiers for the header
    stag = ctx.get("service_tag") or ctx.get("hostname") or ""

    W = 58  # panel width
    bar = "\u2501" * W
    thin = "\u2500" * W

    # ── Header ────────────────────────────────────────────────────────────
    print()
    title = f"{issue_key} \u2014 {stag}" if stag else issue_key
    print(f"  {BOLD}{WHITE}\u2503 SLA & Ticket Health{RESET}  {DIM}{title}{RESET}")
    print(f"  {bar}")

    # Status + age row
    sc, _ = _status_color(status)
    print(f"  {sc}{BOLD}{status}{RESET}  {DIM}for {age_str}{RESET}  {DIM}\u2502{RESET}  Ticket age: {DIM}{created_str}{RESET}")

    # ── Ticket snapshot ───────────────────────────────────────────────────
    print(f"  {DIM}{thin}{RESET}")
    snap_rows = []
    if assignee:
        snap_rows.append(("Assignee", assignee))
    else:
        snap_rows.append(("Assignee", f"{YELLOW}Unassigned{RESET}"))
    if last_cmt_str:
        who = f"  by {last_cmt_author}" if last_cmt_author else ""
        snap_rows.append(("Last comment", f"{last_cmt_str} ago{who}"))
    else:
        snap_rows.append(("Last comment", f"{YELLOW}None{RESET}"))
    if updated_str:
        snap_rows.append(("Last modified", f"{updated_str} ago"))
    if ho:
        ho_sc, _ = _status_color(ho["status"])
        snap_rows.append(("Linked HO", f"{ho['key']} ({ho_sc}{ho['status']}{RESET})"))
    elif ctx.get("linked_issues"):
        # Has linked issues but no HO specifically
        other_keys = [l["key"] for l in ctx["linked_issues"][:3]]
        snap_rows.append(("Linked", ", ".join(other_keys)))

    for label, value in snap_rows:
        print(f"  {DIM}{label:<14}{RESET} {value}")
    print(f"  {DIM}{thin}{RESET}")
    print()

    # ── Detect ticket category from summary ────────────────────────────────
    summary = ctx.get("summary", "")
    _cat_tag = ""
    import re as _re_sla
    _cat_m = _re_sla.match(r"^DO Ticket:\s*(\S+)\s*-", summary)
    if _cat_m:
        _cat_tag = _cat_m.group(1).upper()
    _s_low = summary.lower()
    diag_links = ctx.get("diag_links") or []
    _diag_text = " ".join(d.get("label", "") for d in diag_links)

    # Check HO context for recable/verification phase
    _ho_hint = (ho.get("hint", "") if ho else "").lower()
    _ho_status = (ho.get("status", "") if ho else "").lower()
    _is_recable = (
        "recable" in _ho_hint
        or "sent to dct rc" in _ho_status
        or "ready for verification" in _ho_status
    )

    if _is_recable:
        ticket_category = "RECABLE_VERIFY"
    elif _cat_tag == "POWER_CYCLE" or "power cycle" in _s_low or "power drain" in _s_low:
        ticket_category = "POWER_CYCLE"
    elif _cat_tag == "PSU_RESEAT" or "psu" in _s_low or "power supply" in _s_low or "psu-health" in _diag_text:
        ticket_category = "PSU_RESEAT"
    elif _re_sla.search(r"low.?light|light.?level|optic", _s_low):
        ticket_category = "LOW_LIGHT"
    elif "flap" in _s_low:
        ticket_category = "PORT_FLAPPING"
    elif _cat_tag == "NETWORK" or _re_sla.search(r"network|nic|link.?down|cable|transceiver|sfp", _s_low):
        ticket_category = "NETWORK"
    elif _cat_tag == "DEVICE" or "device" in _s_low or "hardware" in _s_low:
        ticket_category = "DEVICE"
    elif "failed state" in _s_low:
        ticket_category = "FAILED_STATE"
    elif "coolant" in _s_low or "cdu" in _s_low:
        ticket_category = "COOLING"
    elif "leak" in _s_low:
        ticket_category = "LEAK"
    elif "nvme" in _s_low or "drive" in _s_low or "disk" in _s_low or "ssd" in _s_low:
        ticket_category = "DRIVE"
    elif "gpu" in _s_low or "cold plate" in _s_low:
        ticket_category = "GPU_HARDWARE"
    else:
        ticket_category = _cat_tag if _cat_tag else "OTHER"

    # Category display labels and descriptions
    _CATEGORY_INFO = {
        "RECABLE_VERIFY": {
            "label": "RECABLE / VERIFICATION",
            "color": CYAN,
            "verify_tips": [
                "Verify serial number matches NetBox \u2014 `ipmitool fru` or BMC web UI vs NB asset tag",
                "Ping BMC + OS IP \u2014 both must be reachable before closing",
                "Check Grafana node_details for GPU count, NVMe count, memory \u2014 all expected?",
                "Run HPC verification tests if B200 (Gamble engine) \u2014 press [vr] or `cwhelper verify`",
                "Confirm IB links are up and healthy (check [n] connections view)",
                "Compare firmware versions (BMC, BIOS, PSU FW) against fleet baseline \u2014 flag mismatches",
                "If node was RMA'd: confirm replacement part serial in NetBox matches physical label",
            ],
            "work_tips": [
                "Check for a linked Recable DO \u2014 it has the cable list and port assignments",
                "Verify serial number on the physical chassis matches NetBox asset tag",
                "Plug all cables per the Recable DO or cutsheet \u2014 power, network, IB, BMC/OOB",
                "Seat cables firmly \u2014 check SFP click, power cord latch, IB connector lock",
                "Power on and confirm BMC comes up \u2014 check iDRAC/IPMI via BMC IP",
                "Verify OS boots and node is reachable on management network",
                "If cabling doesn't match DO \u2014 comment discrepancy, don't guess",
                "Check LEDs: amber/red on NIC, PSU, or drive = stop and investigate before closing",
            ],
        },
        "POWER_CYCLE": {
            "label": "POWER CYCLE",
            "color": YELLOW,
            "verify_tips": [
                "Confirm node came back online \u2014 ping BMC + OS IP",
                "Check Grafana for pre-cycle metrics (thermal, GPU errors, kernel panics)",
                "If repeat offender (>2 cycles) \u2192 escalate to hardware (suspect PSU, mobo, or BMC FW)",
                "If no POST after cycle \u2192 AC power drain (pull both PSU cords 30s)",
            ],
            "work_tips": [
                "Power drain node for duration specified in description",
                "Reseat PSU cords firmly after drain period",
                "Verify BMC comes back, then OS boots",
                "Add comment with result: did it POST? BMC reachable? OS up?",
            ],
        },
        "PSU_RESEAT": {
            "label": "PSU RESEAT",
            "color": YELLOW,
            "verify_tips": [
                "Verify both PSUs show green/healthy in iDRAC after reseat",
                "Check PDU outlet is live if PSU still shows fault",
                "If PSU fault persists after reseat \u2192 RMA the PSU",
                "Confirm node has redundant power (both PSUs active)",
            ],
            "work_tips": [
                "Fully remove PSU, inspect bay for dust/debris",
                "Reseat firmly until latch clicks",
                "Check the other PSU too \u2014 single-PSU = degraded redundancy",
                "Verify both PSUs healthy in iDRAC, then move to verification",
            ],
        },
        "NETWORK": {
            "label": "NETWORK",
            "color": CYAN,
            "verify_tips": [
                "Confirm link is up and stable (no flapping in last 24h)",
                "Check TOR switch port for CRC errors, drops",
                "Verify no new NETWORK tickets for same rack (shared TOR issue?)",
                "Check IB connectivity if applicable",
            ],
            "work_tips": [
                "Check TOR switch port status \u2014 link up/down?",
                "Reseat cable at both NIC and TOR side",
                "If single-port: reseat cable \u2192 test \u2192 swap cable \u2192 swap SFP",
                "If multi-port same switch: escalate as TOR issue",
            ],
        },
        "LOW_LIGHT": {
            "label": "LOW LIGHT",
            "color": MAGENTA,
            "verify_tips": [
                "Confirm light levels are within spec after cleaning/swap",
                "Check both TX and RX \u2014 one side low = dirty, both = bad optic",
                "If multiple ports on same switch had low light, verify all are clean",
            ],
            "work_tips": [
                "Clean fiber ends with IPA wipes (both ends)",
                "Reseat SFP modules on both ends",
                "If still low after clean \u2192 replace the optic",
                "Use optical power meter if available to verify dB levels",
            ],
        },
        "PORT_FLAPPING": {
            "label": "PORT FLAPPING",
            "color": MAGENTA,
            "verify_tips": [
                "Confirm port has been stable for 24h+ (no flap events in logs)",
                "Check switch logs: show log | grep swpN",
                "Verify remote end (node NIC) is also stable",
            ],
            "work_tips": [
                "Check cable integrity \u2014 swap with known-good",
                "Look for bent pins on SFP/port",
                "Check if remote end (node NIC) is also flapping",
                "If persists after cable swap \u2192 try different switch port",
            ],
        },
        "DEVICE": {
            "label": "DEVICE",
            "color": WHITE,
            "verify_tips": [
                "Ping BMC + OS IP \u2014 both reachable?",
                "Check Grafana node_details dashboard for health overview",
                "Verify no new alerts since your fix",
                "If multiple DEVICE tickets in same rack \u2192 check shared infra",
            ],
            "work_tips": [
                "Ping BMC/oob_ip \u2014 if unreachable, physical visit needed",
                "Pull Grafana node_details for health overview",
                "Check NetBox status (Active? Decommissioning?)",
                "Look at linked HO tickets for upstream context",
            ],
        },
        "OTHER": {
            "label": "OTHER",
            "color": DIM,
            "verify_tips": [
                "Read description + comments \u2014 'OTHER' = automation couldn't classify",
                "Could be firmware, config change, or multi-symptom",
                "Check linked tickets for additional context",
            ],
            "work_tips": [
                "Read the description carefully for the actual ask",
                "Check linked tickets and comments for context",
                "May need manual triage \u2014 look at node in Grafana",
            ],
        },
        "FAILED_STATE": {
            "label": "FAILED STATE",
            "color": RED,
            "verify_tips": [
                "Node should be back online and healthy in Grafana",
                "Check BMC reachability + OS IP",
                "If HO is still open, the fix may not be final yet",
            ],
            "work_tips": [
                "Check BMC reachability first \u2014 if down, likely POWER_CYCLE or PSU",
                "If BMC up \u2192 check Grafana for root cause (GPU, NVMe, memory, network)",
                "If node up but degraded \u2192 likely DEVICE issue",
                "This ticket will likely convert to DEVICE, POWER_CYCLE, or NETWORK",
            ],
        },
        "COOLING": {
            "label": "COOLING / CDU",
            "color": CYAN,
            "verify_tips": [
                "CDU issues stay in HO \u2014 DCT assists with physical access only",
                "Verify coolant levels are back to normal",
                "Do NOT power cycle nodes on the affected cooling loop",
            ],
            "work_tips": [
                "CDU / cooling \u2014 typically HO-only, not a DO task",
                "DCT may need to provide physical access or visual inspection",
                "Do NOT power cycle nodes on the affected cooling loop",
            ],
        },
        "LEAK": {
            "label": "LEAK",
            "color": RED,
            "verify_tips": [
                "Confirm no active liquid present after cleanup",
                "Verify affected components are dry and undamaged",
                "Check neighboring nodes for splash damage",
            ],
            "work_tips": [
                "Pull node from rack ASAP \u2014 safety first",
                "Check for liquid damage on motherboard, GPU trays, NVMe slots",
                "Likely full node RMA \u2014 document with photos",
                "Check if leak source is CDU, cold plate, or fitting",
            ],
        },
        "DRIVE": {
            "label": "DRIVE / NVMe",
            "color": MAGENTA,
            "verify_tips": [
                "Confirm drive is detected and reporting in OS",
                "Check Grafana for disk health metrics post-swap",
                "Verify no SMART errors on the replacement",
            ],
            "work_tips": [
                "NVMe reseat first \u2014 reseat in slot, verify in OS",
                "If still not reporting \u2192 swap with known-good from parts stock",
                "RMA the dead drive via HO workflow",
                "Check for multi-drive failures (could indicate backplane issue)",
            ],
        },
        "GPU_HARDWARE": {
            "label": "GPU HARDWARE",
            "color": YELLOW,
            "verify_tips": [
                "Confirm GPU is detected and healthy in Grafana post-fix",
                "Check cold plate sensor readings are normal",
                "Verify no thermal throttling events",
            ],
            "work_tips": [
                "Cold plate sensor fail = cooling loop issue on GPU tray",
                "Reseat GPU tray \u2014 check thermal paste / cold plate contact",
                "If sensor still fails after reseat \u2192 RMA GPU + cold plate assembly",
                "Check cooling loop for leaks or low flow",
            ],
        },
    }

    cat_info = _CATEGORY_INFO.get(ticket_category, _CATEGORY_INFO["OTHER"])

    def _print_category_badge():
        """Print the detected category as a colored badge."""
        c = cat_info["color"]
        print()
        print(f"  {c}{BOLD}[{cat_info['label']}]{RESET}")
        print()

    def _print_category_tips(tip_key: str):
        """Print category-specific tips (verify_tips or work_tips)."""
        tips = cat_info.get(tip_key, [])
        if tips:
            print(f"  {DIM}{thin}{RESET}")
            print(f"  {BOLD}{WHITE}Category Tips \u2014 {cat_info['label']}{RESET}")
            print()
            for tip in tips:
                print(f"    {DIM}\u25b8 {tip}{RESET}")
            print()
            print(f"  {DIM}{thin}{RESET}")
            print()

    # ── Status-based personalized guidance ────────────────────────────────
    status_lower = status.lower()

    # Helper: build a list of personalized observations as bullet points
    def _observations() -> list:
        """Return ticket-specific observation strings for guidance."""
        obs = []
        # Comment activity
        if not raw_comments:
            obs.append(f"{YELLOW}\u25b8{RESET} No comments on this ticket at all")
        elif last_cmt_age is not None and last_cmt_age > 7 * 86400:
            obs.append(
                f"{YELLOW}\u25b8{RESET} Last comment was {last_cmt_str} ago"
                f" \u2014 nobody has asked to keep it open")
        elif last_cmt_age is not None and last_cmt_age > 48 * 3600:
            obs.append(
                f"{DIM}\u25b8{RESET} Last comment {last_cmt_str} ago by {last_cmt_author or '?'}")
        else:
            obs.append(
                f"{GREEN}\u25b8{RESET} Recent comment {last_cmt_str} ago"
                f" by {last_cmt_author or '?'}")

        # HO status
        if ho:
            ho_st = ho["status"].lower()
            if ho_st in ("closed", "done", "resolved"):
                obs.append(
                    f"{GREEN}\u25b8{RESET} Linked {ho['key']} is {GREEN}Closed{RESET}"
                    f" \u2014 nothing blocking")
            elif ho_st in ("on hold", "waiting for support"):
                obs.append(
                    f"{BLUE}\u25b8{RESET} Linked {ho['key']} is {BLUE}On Hold{RESET}"
                    f" \u2014 check if they need this DO open")
            else:
                obs.append(
                    f"{DIM}\u25b8{RESET} Linked {ho['key']} is {ho['status']}")
        else:
            obs.append(f"{DIM}\u25b8{RESET} No linked HO \u2014 this DO stands on its own")

        # Assignee
        if not assignee:
            obs.append(
                f"{YELLOW}\u25b8{RESET} Unassigned \u2014 claim with [a] before taking action")

        return obs

    # ·· Verification — stale (>7 days) ····································
    if status_lower == "verification" and age_secs > 7 * 86400:
        days = age_secs / 86400
        print(f"  {RED}{BOLD}\u26a0  STALE VERIFICATION{RESET}  {DIM}({days:.0f} days){RESET}")
        _print_category_badge()
        print()

        # Personalized assessment
        # Determine recommendation based on signals
        ho_blocking = ho and ho["status"].lower() not in ("closed", "done", "resolved")
        recent_comments = last_cmt_age is not None and last_cmt_age < 48 * 3600
        should_investigate = ho_blocking or recent_comments

        if should_investigate:
            print(f"  {YELLOW}{BOLD}Recommendation: INVESTIGATE BEFORE CLOSING{RESET}")
        else:
            print(f"  {GREEN}{BOLD}Recommendation: CLOSE THIS TICKET{RESET}")
        print()

        # Show personalized observations
        print(f"  {BOLD}What I see on {issue_key}:{RESET}")
        for obs in _observations():
            print(f"    {obs}")
        print()

        if not should_investigate:
            print(f"  {WHITE}This DO has been in Verification for {days:.0f} days with no{RESET}")
            if not raw_comments:
                print(f"  {WHITE}comments at all. Your work appears done.{RESET}")
            elif last_cmt_age and last_cmt_age > 7 * 86400:
                print(f"  {WHITE}activity in {last_cmt_str}. Your work appears done.{RESET}")
            else:
                print(f"  {WHITE}recent blockers. Your work appears done.{RESET}")
            print()
            print(f"  {BOLD}Close it:{RESET}")
            print(f"    {GREEN}\u25b8{RESET} Press {GREEN}{BOLD}[k]{RESET} to close. Suggested comment:")
            print(f"    {DIM}\u250c{'─' * 52}{RESET}")
            print(f"    {DIM}\u2502 Work performed: [short summary of what you did].{RESET}")
            print(f"    {DIM}\u2502 Current state: LEDs normal; Grafana/IB healthy;{RESET}")
            print(f"    {DIM}\u2502 no recurring alerts during >{days:.0f}d in Verification.{RESET}")
            print(f"    {DIM}\u2502 Closing DO per DCT workflow. If issue returns,{RESET}")
            print(f"    {DIM}\u2502 please reopen or submit a new DO.{RESET}")
            print(f"    {DIM}\u2514{'─' * 52}{RESET}")
        else:
            print(f"  {WHITE}This DO has been in Verification for {days:.0f} days but{RESET}")
            if ho_blocking:
                print(f"  {WHITE}linked {ho['key']} is still {ho['status']} \u2014 check{RESET}")
                print(f"  {WHITE}if they need this DO to stay open.{RESET}")
            if recent_comments:
                print(f"  {WHITE}there was a recent comment ({last_cmt_str} ago) \u2014{RESET}")
                print(f"  {WHITE}read it before closing. Press [c] to view.{RESET}")
            print()
            print(f"  {BOLD}If everything is actually fine:{RESET}")
            print(f"    {GREEN}\u25b8{RESET} Press {GREEN}{BOLD}[k]{RESET} to close with a summary comment")
            print()
            print(f"  {BOLD}If something is still active:{RESET}")
            print(f"    {YELLOW}\u25b8{RESET} Press {YELLOW}{BOLD}[z]{RESET} to resume In Progress")
            print(f"    {YELLOW}\u25b8{RESET} Or {YELLOW}{BOLD}[y]{RESET} On Hold + comment + @ the right engineer")

        # Escalation guidance for stale tickets
        print()
        print(f"  {BOLD}Escalate stale tickets:{RESET}")
        print(f"    {CYAN}\u25b8{RESET} Post in your site ops channel with a list of stale DOs")
        print(f"    {CYAN}\u25b8{RESET} For each: confirm what you verified (power, cabling, diags)")
        print(f"    {CYAN}\u25b8{RESET} Ping the owning engineer/SOE by name if blocked on them")
        print(f"    {DIM}  Don\u2019t just wait \u2014 Slack escalation is expected for old tickets.{RESET}")
        print(f"    {DIM}  Use the stale list (press v from main menu) to export all at once.{RESET}")
        print()
        _print_category_tips("verify_tips")

    # ·· Verification — past 48h window ····································
    elif status_lower == "verification" and age_secs > 48 * 3600:
        days = age_secs / 86400
        print(f"  {YELLOW}{BOLD}\u25b2  VERIFICATION > 48h{RESET}  {DIM}({days:.1f} days){RESET}")
        _print_category_badge()
        print(f"  {WHITE}{issue_key} is past the standard 24\u201348h observation window.{RESET}")
        print(f"  {WHITE}If your runbook work is done and the node is healthy,{RESET}")
        print(f"  {WHITE}it\u2019s normal and expected for you to close this DO yourself.{RESET}")
        print()

        # Show personalized observations
        print(f"  {BOLD}What I see:{RESET}")
        for obs in _observations():
            print(f"    {obs}")
        print()

        # Quick recommendation
        ho_blocking = ho and ho["status"].lower() not in ("closed", "done", "resolved")
        if ho_blocking:
            print(f"  {YELLOW}\u25b8{RESET} {ho['key']} is still {ho['status']} \u2014 verify it doesn\u2019t need this DO open")
        if not raw_comments:
            print(f"  {DIM}\u25b8 No comments \u2014 nobody has asked to keep it open.{RESET}")
        print()
        print(f"    {GREEN}\u25b8{RESET} All good \u2192 Press {GREEN}{BOLD}[k]{RESET} to close")
        print(f"    {YELLOW}\u25b8{RESET} Unsure  \u2192 Press {YELLOW}{BOLD}[z]{RESET} to resume and investigate")
        print()
        _print_category_tips("verify_tips")

    # ·· Verification — within normal window ·······························
    elif status_lower == "verification":
        print(f"  {GREEN}{BOLD}\u25cf  PENDING VERIFICATION{RESET}")
        _print_category_badge()
        print(f"  {WHITE}{issue_key} is within the 24\u201348h observation window.{RESET}")
        print(f"  {WHITE}You\u2019ve done the runbook. Now confirm the fix is holding:{RESET}")
        print(f"    {DIM}\u25b8 LEDs look good{RESET}")
        print(f"    {DIM}\u25b8 Grafana / IB dashboards are clean{RESET}")
        print(f"    {DIM}\u25b8 No new alerts{RESET}")
        print()
        _print_category_tips("verify_tips")
        if assignee:
            print(f"  {DIM}Assigned to {assignee}.{RESET}")
        if created_age and created_age < 4 * 3600:
            print(f"  {GREEN}\u2605{RESET}  {GREEN}Moving fast \u2014 ticket is under 4 hours old. Keep it up.{RESET}")
        elif created_age and created_age < 24 * 3600:
            print(f"  {DIM}\u25b8 Good pace \u2014 same-day verification.{RESET}")
        print(f"  {DIM}Once confirmed healthy \u2192 close the ticket with [k].{RESET}")
        print(f"  {DIM}Don\u2019t let it sit here for weeks \u2014 that\u2019s a smell.{RESET}")

    # ·· In Progress — long-running (>24h) ·································
    elif status_lower == "in progress" and age_secs > 24 * 3600:
        print(f"  {YELLOW}{BOLD}\u25b2  LONG RUNNING{RESET}  {DIM}In Progress for {age_str}{RESET}")
        _print_category_badge()
        print(f"  {WHITE}{issue_key} has been In Progress for over 24 hours.{RESET}")
        print()

        # Personalized context
        if not raw_comments:
            print(f"  {YELLOW}\u25b8{RESET} No comments yet \u2014 add one noting your progress or blocker.")
        elif last_cmt_age and last_cmt_age > 24 * 3600:
            print(f"  {DIM}\u25b8 Last comment {last_cmt_str} ago by {last_cmt_author or '?'}.{RESET}")
        if assignee:
            print(f"  {DIM}\u25b8 Assigned to {assignee}.{RESET}")
        print()

        print(f"  {BOLD}Are you blocked?{RESET}")
        print(f"    {YELLOW}\u25b8{RESET} Waiting on parts/vendor \u2192 {YELLOW}{BOLD}[y]{RESET} On Hold {DIM}(pauses SLA clock){RESET}")
        print(f"    {DIM}  Add a comment: what you\u2019re waiting on and from whom.{RESET}")
        print()
        print(f"  {BOLD}Is the work done?{RESET}")
        print(f"    {GREEN}\u25b8{RESET} Runbook complete \u2192 {GREEN}{BOLD}[v]{RESET} Verify {DIM}(start 24\u201348h watch window){RESET}")
        print()
        print(f"  {BOLD}Still actively working?{RESET}")
        print(f"    {DIM}\u25b8 No action needed \u2014 just be aware the SLA clock is running.{RESET}")
        print()
        _print_category_tips("work_tips")

    # ·· In Progress — normal ··············································
    elif status_lower == "in progress":
        print(f"  {CYAN}{BOLD}\u25cf  IN PROGRESS{RESET}")
        _print_category_badge()
        if assignee:
            print(f"  {DIM}{issue_key} assigned to {assignee}. SLA clock is running.{RESET}")
        else:
            print(f"  {YELLOW}\u25b8{RESET} {issue_key} is unassigned \u2014 claim with {BOLD}[a]{RESET} first.")
        print()
        _print_category_tips("work_tips")
        if created_age and age_secs:
            if age_secs < 1800:
                print(f"  {GREEN}\u2605{RESET}  {GREEN}Jumped on it fast \u2014 started within 30 min. SLA happy.{RESET}")
            elif age_secs < 2 * 3600:
                print(f"  {DIM}\u25b8 In progress for {_format_age(age_secs)} \u2014 you\u2019re on it.{RESET}")
        print(f"  {DIM}Complete the runbook work, then:{RESET}")
        print(f"    {GREEN}\u25b8{RESET} {GREEN}{BOLD}[v]{RESET} Verify  {DIM}\u2014 move to Verification for 24\u201348h watch{RESET}")
        print(f"    {YELLOW}\u25b8{RESET} {YELLOW}{BOLD}[y]{RESET} Hold    {DIM}\u2014 if waiting on parts/vendor (pauses clock){RESET}")

    # ·· On Hold / Waiting ·················································
    elif status_lower in ("on hold", "waiting for support"):
        print(f"  {BLUE}{BOLD}\u25a0  PAUSED{RESET}  {DIM}On Hold for {age_str}{RESET}")
        _print_category_badge()
        if assignee:
            print(f"  {DIM}{issue_key} assigned to {assignee}. SLA clock is paused.{RESET}")
        else:
            print(f"  {DIM}{issue_key} \u2014 SLA clock is paused.{RESET}")
        print()

        # Personalized nudge if on hold for a long time
        if age_secs > 7 * 86400:
            days = age_secs / 86400
            print(f"  {YELLOW}\u25b8{RESET} On Hold for {days:.0f} days \u2014 has the blocker been resolved?")
            if last_cmt_str:
                print(f"  {DIM}\u25b8 Last comment {last_cmt_str} ago \u2014 follow up if needed.{RESET}")
            print()

        print(f"  {BOLD}When ready to continue:{RESET}")
        print(f"    {GREEN}\u25b8{RESET} Press {GREEN}{BOLD}[z]{RESET} to resume In Progress")
        print(f"    {DIM}\u25b8 Add a comment noting what unblocked you.{RESET}")
        print()
        print(f"  {BOLD}If the blocker resolves the issue entirely:{RESET}")
        print(f"    {DIM}\u25b8 Resume \u2192 Verify \u2192 Close (normal flow).{RESET}")
        print(f"    {DIM}\u25b8 Don\u2019t skip Verification \u2014 watch for 24\u201348h to confirm.{RESET}")

    # ·· New / To Do ·······················································
    elif status_lower in ("to do", "new", "waiting for triage"):
        print(f"  {WHITE}{BOLD}\u25cb  NOT STARTED{RESET}")
        _print_category_badge()
        if assignee:
            print(f"  {DIM}{issue_key} is assigned to {assignee} but hasn\u2019t been started.{RESET}")
        else:
            print(f"  {DIM}{issue_key} is waiting for someone to pick it up.{RESET}")
        if created_age and created_age > 4 * 3600:
            print(f"  {RED}\u25b8{RESET} {RED}Created {created_str} ago \u2014 this is way past the 30 min SLA target.{RESET}")
            print(f"    {DIM}Grab it now or flag it in your site channel.{RESET}")
        elif created_age and created_age > 1800:
            print(f"  {YELLOW}\u25b8{RESET} Created {created_str} ago \u2014 SLA target is < 30 min to start.")
        else:
            print(f"  {GREEN}\u25b8{RESET} {GREEN}Fresh ticket \u2014 just came in. Grab it before someone else does.{RESET}")
        print(f"    {GREEN}\u25b8{RESET} Press {GREEN}{BOLD}[s]{RESET} to start work {DIM}(auto-assigns to you){RESET}")
        print()
        _print_category_tips("work_tips")

    # ·· Closed ····························································
    elif status_lower in ("closed", "done", "resolved"):
        print(f"  {GREEN}{BOLD}\u2713  CLOSED{RESET}  {DIM}No action needed{RESET}")
        _print_category_badge()
        print(f"  {DIM}{issue_key}")
        if assignee:
            print(f"  Closed by / last assigned to: {assignee}{RESET}")
        if last_cmt_str:
            print(f"  {DIM}Last comment {last_cmt_str} ago by {last_cmt_author or '?'}{RESET}")
        print(f"  {DIM}If the issue returns, reopen or create a new DO.{RESET}")
        print()
        # Timing feedback based on total ticket lifetime
        if created_age:
            if created_age < 4 * 3600:
                print(f"  {GREEN}{BOLD}\u2605{RESET}  {GREEN}Crushed it \u2014 closed in under 4 hours. Fast hands.{RESET}")
            elif created_age < 24 * 3600:
                print(f"  {GREEN}\u2605{RESET}  {WHITE}Solid turnaround \u2014 opened and closed same day.{RESET}")
            elif created_age < 3 * 86400:
                print(f"  {DIM}\u25b8 Closed within 3 days \u2014 right on track.{RESET}")
            elif created_age < 7 * 86400:
                print(f"  {YELLOW}\u25b8{RESET} {DIM}Took about a week. Not bad, but could be tighter.{RESET}")
            elif created_age < 14 * 86400:
                print(f"  {YELLOW}\u25b8{RESET} {YELLOW}This one dragged \u2014 {created_age / 86400:.0f} days from open to close.{RESET}")
                print(f"    {DIM}Next time: if blocked, put it On Hold sooner so it\u2019s visible.{RESET}")
            else:
                print(f"  {RED}\u25b8{RESET} {RED}{created_age / 86400:.0f} days to close \u2014 that\u2019s a long time for a DO.{RESET}")
                print(f"    {DIM}If this was waiting on HO/vendor, On Hold keeps the queue clean.{RESET}")
                print(f"    {DIM}If it was forgotten, the stale list (v) catches these early.{RESET}")

    else:
        print(f"  {DIM}Status: {status}{RESET}")

    print()
    print(f"  {DIM}{thin}{RESET}")

    # ── Jira SLA timers ───────────────────────────────────────────────────
    if sla_values:
        print(f"  {BOLD}Jira SLA Timers{RESET}")
        print()

        for sla in sla_values:
            sla_name = sla.get("name", "?")
            ongoing = sla.get("ongoingCycle")
            completed = sla.get("completedCycles", [])

            if not ongoing and completed:
                last = completed[-1]
                breached = last.get("breached", False)
                elapsed = (last.get("elapsedTime") or {}).get("friendly", "?")
                goal = (last.get("goalDuration") or {}).get("friendly", "?")
                if breached:
                    print(f"  {RED}\u2716 BREACHED{RESET}  {WHITE}{sla_name}{RESET}")
                    print(f"             {DIM}Goal: {goal}  \u2502  Took: {elapsed}{RESET}")
                else:
                    print(f"  {GREEN}\u2714 MET{RESET}       {WHITE}{sla_name}{RESET}")
                    print(f"             {DIM}Goal: {goal}  \u2502  Completed in: {elapsed}{RESET}")

            elif ongoing:
                elapsed = (ongoing.get("elapsedTime") or {}).get("friendly", "?")
                remaining = (ongoing.get("remainingTime") or {}).get("friendly", "?")
                goal = (ongoing.get("goalDuration") or {}).get("friendly", "?")

                if ongoing.get("breached"):
                    print(f"  {RED}\u2716 BREACHED{RESET}  {WHITE}{sla_name}{RESET}")
                    print(f"             {DIM}Goal: {goal}  \u2502  Elapsed: {elapsed}  \u2502  Over by: {remaining}{RESET}")
                elif ongoing.get("paused"):
                    print(f"  {BLUE}\u25a0 PAUSED{RESET}    {WHITE}{sla_name}{RESET}")
                    print(f"             {DIM}Goal: {goal}  \u2502  Remaining: {remaining}  \u2502  Clock stopped{RESET}")
                else:
                    remaining_ms = (ongoing.get("remainingTime") or {}).get("millis", 0)
                    goal_ms = (ongoing.get("goalDuration") or {}).get("millis", 1)
                    pct = remaining_ms / goal_ms if goal_ms else 0
                    if pct < 0.25:
                        color, icon, label = RED, "\u25bc", "CRITICAL"
                    elif pct < 0.50:
                        color, icon, label = YELLOW, "\u25b2", "WARNING"
                    else:
                        color, icon, label = GREEN, "\u25cf", "OK"
                    print(f"  {color}{icon} {label}{RESET}     {WHITE}{sla_name}{RESET}")
                    print(f"             {DIM}Goal: {goal}  \u2502  Remaining: {remaining}  \u2502  Elapsed: {elapsed}{RESET}")

            print()

        print(f"  {DIM}{thin}{RESET}")
    else:
        print(f"  {DIM}No Jira SLA timer attached to this ticket.{RESET}")
        print(f"  {DIM}Guidance above is based on status and ticket age.{RESET}")
        print()

    # ── DCT Reference Card ────────────────────────────────────────────────
    print(f"  {BOLD}DCT Quick Reference{RESET}")
    print()
    print(f"  {BOLD}SLA Targets{RESET}")
    print(f"    {DIM}New \u2192 In Progress     {RESET}{CYAN}< 30 min{RESET}")
    print(f"    {DIM}In Progress \u2192 Verify  {RESET}{CYAN}< 4 hours{RESET}  {DIM}(routine){RESET}")
    print(f"    {DIM}Verify \u2192 Close        {RESET}{CYAN}< 48 hours{RESET}")
    print()
    print(f"  {BOLD}Clock Rules{RESET}")
    print(f"    {GREEN}\u25b6{RESET} {DIM}Running{RESET}   In Progress, Verification")
    print(f"    {BLUE}\u23f8{RESET} {DIM}Paused{RESET}    On Hold, Waiting for Support/Vendor")
    print(f"    {WHITE}\u23f9{RESET} {DIM}Stopped{RESET}   Closed, Done, Resolved")
    print()
    print(f"  {BOLD}Verification \u2014 What It Means{RESET}")
    print(f"    {DIM}You\u2019ve done the runbook (reseat/swap/clean/recable/etc.).{RESET}")
    print(f"    {DIM}Now you\u2019re watching 24\u201348h to confirm the fix holds:{RESET}")
    print(f"    {DIM}LEDs good, Grafana clean, IB healthy, no new alerts.{RESET}")
    print()
    print(f"  {BOLD}Rule of Thumb{RESET}")
    print(f"    {DIM}DO = your work. If your part is done and stable, close it.{RESET}")
    print(f"    {DIM}Don\u2019t park DOs in Verification for weeks \u2014 it clutters{RESET}")
    print(f"    {DIM}the queue and keeps the SLA clock running for nothing.{RESET}")
    print(f"    {DIM}After 24\u201348h clean: close. After days/weeks: close or escalate.{RESET}")
    print()
    print(f"  {BOLD}Stale Ticket Escalation{RESET}")
    print(f"    {DIM}1. Post in your site ops channel (e.g. #ops-us-site-01a-...){RESET}")
    print(f"    {DIM}   with a list of DOs \u226530 days in Verification.{RESET}")
    print(f"    {DIM}2. For each: note what you verified (power, cabling, diags).{RESET}")
    print(f"    {DIM}3. Ping the owning engineer/SOE by name \u2014 don\u2019t just wait.{RESET}")
    print(f"    {DIM}4. In Jira: add a comment, then close or move to Failed Verification.{RESET}")
    print(f"    {DIM}Use the stale list (v from main menu, then e to export) to share.{RESET}")
    print()



# Connection display functions moved to cwhelper/tui/connection_view.py



def _print_prep_brief(prep: dict) -> None:
    """Print a prep brief panel for an HO ticket approaching DO stage."""
    key = prep.get("key", "?")
    proc = prep.get("procedure", "?")
    hint = prep.get("hint", "")
    location = prep.get("location", "—")
    node = prep.get("node", "—")
    tools = prep.get("tools", [])
    history = prep.get("history_count", 0)
    repeat = prep.get("repeat_offender", False)
    neighbors = prep.get("rack_neighbors", [])

    w = 52
    border = f"{YELLOW}{BOLD}"
    b = f"{border}│{RESET}"

    print()
    print(f"  {border}┌─ PREP BRIEF {'─' * (w - 14)}┐{RESET}")
    print(f"  {b}  {BOLD}{key}{RESET}  —  {proc}")
    print(f"  {b}  {DIM}{hint}{RESET}")
    print(f"  {b}")
    print(f"  {b}  {CYAN}Location:{RESET}  {location}")
    print(f"  {b}  {CYAN}Node:{RESET}      {node}")
    print(f"  {b}  {CYAN}Bring:{RESET}     {', '.join(tools)}")

    if history > 0:
        color = RED if repeat else DIM
        flag = " — REPEAT OFFENDER" if repeat else ""
        print(f"  {b}  {CYAN}History:{RESET}   {color}{history} tickets in 90d{flag}{RESET}")

    if neighbors:
        print(f"  {b}  {CYAN}Also open:{RESET} {', '.join(neighbors[:3])}")

    print(f"  {border}└{'─' * w}┘{RESET}")
    print()


def _clear_screen():
    """Clear terminal (works on macOS/Linux)."""
    os.system("clear" if os.name != "nt" else "cls")



def _print_banner(greeting: str = ""):
    """Print the app header with optional personalized greeting."""
    from cwhelper.tui.rich_console import _rich_print_banner
    _rich_print_banner(greeting)



def _print_help():
    """Display a full help guide explaining every menu option and hotkey."""
    print(f"""
  {BOLD}{CYAN}{'━' * 54}{RESET}
  {BOLD}{WHITE}  Quick Guide — DCT Node Helper{RESET}
  {BOLD}{CYAN}{'━' * 54}{RESET}

  {BOLD}{WHITE}WHAT'S NEW  {RESET}{DIM}v{APP_VERSION}{RESET}
  {DIM}{'─' * 54}{RESET}

  {BOLD}{YELLOW}v6.3.0{RESET}
    {GREEN}+{RESET} {WHITE}Ticket categorizer{RESET}
      {DIM}Scans last 50 queue tickets and groups them by
      type (DEVICE, NETWORK, POWER_CYCLE, PSU_RESEAT,
      LOW_LIGHT, PORT_FLAPPING, OTHER) with boolean
      matching on summary keywords and diag links.
      Includes troubleshooting tips per category.{RESET}

  {BOLD}{YELLOW}v6.2.0{RESET}
    {GREEN}+{RESET} {WHITE}SDA queue support{RESET}
      {DIM}Browse SDA project tickets with Triage and
      Customer Verification status filters.{RESET}
    {GREEN}+{RESET} {WHITE}Beginner setup guide{RESET}
      {DIM}Step-by-step install guide at site/setup-guide.html
      and live on CodePen.{RESET}
    {GREEN}+{RESET} {WHITE}start.sh launcher{RESET}
      {DIM}One-click launcher script for non-tech users.{RESET}

  {BOLD}{YELLOW}v6.1.0{RESET}
    {GREEN}+{RESET} {WHITE}Rack map visualization{RESET}
      {DIM}ASCII data hall maps with animated walking routes.{RESET}
    {GREEN}+{RESET} {WHITE}MRB / Parts search [f]{RESET}
      {DIM}Find RMA and parts tickets from ticket detail view.{RESET}
    {GREEN}+{RESET} {WHITE}Bookmark shortcuts [a-e]{RESET}
      {DIM}Save tickets and queue searches to main menu.{RESET}

  {BOLD}{WHITE}MAIN MENU{RESET}
  {DIM}{'─' * 54}{RESET}

  {BOLD}1{RESET}  {WHITE}Lookup ticket{RESET}
     {DIM}Enter a Jira key like DO-12345 or HO-67890.
     Pulls up full ticket detail with node info, NetBox
     data, and all available actions.
     Use when: you have a ticket number and need context.{RESET}

  {BOLD}2{RESET}  {WHITE}Node info{RESET}
     {DIM}Enter a service tag (e.g. S948338X5A04781) or
     hostname. Finds ALL tickets for that node across
     DO and HO projects.
     Use when: you're at a rack and need the node's
     full ticket history.{RESET}

  {BOLD}3{RESET}  {WHITE}Browse queue{RESET}
     {DIM}Pick DO (Data Operations) or HO (Hardware Operations),
     then filter by site and status.
     HO includes a "Radar" filter showing tickets likely
     to spawn DOs (RMA-initiate, Sent to DCT, etc.).
     Use when: starting your shift and need to see
     what's in the queue.{RESET}

  {BOLD}4{RESET}  {WHITE}My tickets{RESET}
     {DIM}Shows only tickets assigned to you.
     Use when: checking your personal workload.{RESET}

  {BOLD}5{RESET}  {WHITE}Watch queue{RESET}
     {DIM}Polls for new tickets every N seconds and sends
     macOS notifications when new ones appear.
     Use when: you want to be alerted about new tickets
     without refreshing manually.{RESET}

  {BOLD}6{RESET}  {WHITE}Rack map{RESET}
     {DIM}Shows a visual data hall map with your rack
     highlighted and a yellow walking route from the
     entrance. Animated in terminal.
     Use when: you need to physically find a rack on
     the floor.{RESET}

  {BOLD}7{RESET}  {WHITE}Bookmarks{RESET}
     {DIM}Save frequently used tickets or searches as
     shortcuts (a-e) on the main menu.
     Use when: you have tickets you check repeatedly.{RESET}

  {BOLD}{WHITE}TICKET VIEW HOTKEYS{RESET}
  {DIM}{'─' * 54}{RESET}
  {DIM}These appear after viewing a ticket detail:{RESET}

  {BOLD}{WHITE}View{RESET}
    {CYAN}[r]{RESET} {WHITE}Rack Map{RESET}       {DIM}Data hall map with walking route{RESET}
    {MAGENTA}[n]{RESET} {WHITE}Connections{RESET}   {DIM}Network cables: your ports → switch ports{RESET}
    {YELLOW}[l]{RESET} {WHITE}Linked{RESET}        {DIM}Related DO/HO tickets{RESET}
    {BLUE}[d]{RESET} {WHITE}Diags{RESET}         {DIM}Diagnostic links from description{RESET}
    {GREEN}[c]{RESET} {WHITE}Comments{RESET}      {DIM}Latest Jira comments{RESET}
    {YELLOW}[e]{RESET} {WHITE}Rack View{RESET}     {DIM}Visual rack elevation + pick a neighbor
                      to search Jira. Type 'x' for NetBox.{RESET}
    {YELLOW}[f]{RESET} {WHITE}MRB / Parts{RESET}   {DIM}Find RMA/parts tickets for this node
                      in the MRB project (optics, PSUs, etc.){RESET}

  {BOLD}{WHITE}Status transitions{RESET}  {DIM}(shown based on current status){RESET}
    {GREEN}[s]{RESET} {WHITE}Start Work{RESET}    {DIM}Grab & begin (New/To Do → In Progress){RESET}
    {BLUE}[v]{RESET} {WHITE}Verification{RESET}  {DIM}Move to verification (optional comment){RESET}
    {YELLOW}[y]{RESET} {WHITE}On Hold{RESET}       {DIM}Pause work (reason required){RESET}
    {CYAN}[z]{RESET} {WHITE}Resume{RESET}        {DIM}Back to In Progress{RESET}
    {RED}[k]{RESET} {WHITE}Close{RESET}         {DIM}Close ticket (comment + confirm required){RESET}

  {BOLD}{WHITE}Open in browser{RESET}
    {CYAN}[j]{RESET} {WHITE}Jira{RESET}          {DIM}Open ticket in Jira{RESET}
    {CYAN}[p]{RESET} {WHITE}Portal{RESET}        {DIM}Open Service Desk portal{RESET}
    {YELLOW}[x]{RESET} {WHITE}NetBox{RESET}        {DIM}Open device in NetBox{RESET}
    {GREEN}[g]{RESET} {WHITE}Grafana{RESET}       {DIM}Open node dashboard{RESET}
    {GREEN}[i]{RESET} {WHITE}IB{RESET}            {DIM}InfiniBand search{RESET}

  {BOLD}{WHITE}Navigation{RESET}
    {DIM}[b]{RESET} {WHITE}Back{RESET}          {DIM}Return to queue list{RESET}
    {DIM}[m]{RESET} {WHITE}Menu{RESET}          {DIM}Return to main menu{RESET}
    {DIM}[h]{RESET} {WHITE}History{RESET}       {DIM}All tickets for this node{RESET}
    {DIM}[q]{RESET} {WHITE}Quit{RESET}          {DIM}Exit the tool{RESET}

  {BOLD}{CYAN}{'━' * 54}{RESET}

  {DIM}Scroll up to see the full guide.{RESET}
  {DIM}Press {RESET}{CYAN}{BOLD}w{RESET}{DIM} to open the visual docs in your browser.{RESET}
""")
    try:
        pick = input(f"\n  Press {BOLD}w{RESET} for web docs, or ENTER to go back: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return
    if pick == "w":
        import os as _os
        site_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "site", "index.html")
        if _os.path.exists(site_path):
            import webbrowser
            webbrowser.open(f"file://{site_path}")
            print(f"  {DIM}Opening docs in browser...{RESET}")
        else:
            print(f"  {DIM}No site/index.html found.{RESET}")


def _print_pretty(ctx: dict):
    """Print a clean, readable summary — delegates to Rich display."""
    from cwhelper.tui.rich_console import _rich_print_ticket
    _rich_print_ticket(ctx)

def _print_pretty_legacy(ctx: dict):
    """Legacy ANSI fallback — kept for reference."""
    status = ctx["status"]
    sc, sd = _status_color(status)

    key = ctx["issue_key"]
    line = "\u2500" * 50

    status_upper = status.upper()

    if ctx.get("source") == "netbox":
        # Device view — no Jira ticket
        print()
        print(f"  {sc}{'━' * 50}{RESET}")
        print(f"  {sc}{BOLD}{sd}{RESET}  {WHITE}{BOLD}{ctx['summary']}{RESET}    {sc}{BOLD}{status_upper}{RESET}")
        print(f"     {DIM}{ctx.get('hostname', '')}{RESET}")
        print(f"  {sc}{'━' * 50}{RESET}")
        print(f"  {GREEN}No open Jira tickets for this device{RESET}")
        print(f"  {DIM}{line}{RESET}")
    else:
        # Ticket view — bold key + status badge + assignee
        node_tag = f"  {DIM}({ctx['node_name']}){RESET}" if ctx.get("node_name") else ""
        assignee = ctx["assignee"] or "Unassigned"
        ac = f"{MAGENTA}{BOLD}{assignee}{RESET}" if ctx["assignee"] else f"{DIM}{assignee}{RESET}"

        print()
        print(f"  {sc}{'━' * 50}{RESET}")
        print(f"  {sc}{BOLD}{sd}{RESET}  {WHITE}{BOLD}{key}{RESET}    {sc}{BOLD}{status_upper}{RESET}{node_tag}")
        print(f"     {DIM}{ctx['summary']}{RESET}")
        print(f"  {sc}{'━' * 50}{RESET}")
        # Location line — right under the ticket header
        _parsed_hdr = _parse_rack_location(ctx.get("rack_location", ""))
        if _parsed_hdr:
            _hdr_loc_parts = []
            if _parsed_hdr.get("dh"):
                _hdr_loc_parts.append(f"DH  {CYAN}{_parsed_hdr['dh']}{RESET}")
            if _parsed_hdr.get("rack") is not None:
                _hdr_loc_parts.append(f"Rack  {CYAN}{_parsed_hdr['rack']}{RESET}")
            if _parsed_hdr.get("ru"):
                _hdr_loc_parts.append(f"RU  {CYAN}{_parsed_hdr['ru']}{RESET}")
            _hn_hdr = ctx.get("hostname") or ""
            _node_hdr = re.search(r'-node-(\d+)-', _hn_hdr)
            if _node_hdr:
                _hdr_loc_parts.append(f"Node  {CYAN}{_node_hdr.group(1)}{RESET}")
            if _hdr_loc_parts:
                print(f"  {DIM}{'  │  '.join(_hdr_loc_parts)}{RESET}")
        # Ticket age in current status
        age_secs = ctx.get("status_age_seconds", 0)
        if age_secs > 0:
            age_str = _format_age(age_secs)
            if age_secs > 48 * 3600:
                age_color = RED
            elif age_secs > 24 * 3600:
                age_color = YELLOW
            else:
                age_color = GREEN
            print(f"  {age_color}In {status} for {age_str}{RESET}")

        reporter = ctx.get("reporter") or ""
        rep_part = f"  {DIM}\u2502{RESET}  Reporter  {DIM}{reporter}{RESET}" if reporter else ""
        print(f"  Project  {BOLD}{ctx['project']}{RESET}  {DIM}\u2502{RESET}  Type  {ctx['issue_type']}  {DIM}\u2502{RESET}  Assignee  {ac}{rep_part}")
        print(f"  {DIM}{line}{RESET}")

    # Unified node info — Jira fields + NetBox enrichment in one block
    netbox = ctx.get("netbox", {})

    # Build combined rows: Jira data with NetBox extras mixed in
    node_rows = [
        ("Site",        ctx["site"]),
        ("Rack",        ctx["rack_location"]),
        ("Service Tag", ctx["service_tag"]),
        ("Hostname",    ctx["hostname"]),
        ("Vendor",      ctx["vendor"]),
    ]
    # Add IPs only when they have real values
    ip = ctx.get("ip_address")
    if ip and ip != "0.0.0.0":
        node_rows.append(("IP", ip))
    if netbox:
        oob_raw = netbox.get("oob_ip")
        if oob_raw:
            node_rows.append(("BMC IP", oob_raw.split("/")[0]))
        ip6_raw = netbox.get("primary_ip6")
        if ip6_raw:
            node_rows.append(("IPv6", ip6_raw.split("/")[0]))
    # Add NetBox-only fields inline (no separate section)
    if netbox:
        if netbox.get("asset_tag"):
            tag_val = netbox["asset_tag"]
            node_rows.append(("Asset Tag", tag_val))
        if netbox.get("position"):
            node_rows.append(("RU", f"U{netbox['position']}"))
        if netbox.get("model"):
            node_rows.append(("Model", netbox["model"]))
        if netbox.get("device_role"):
            node_rows.append(("Role", netbox["device_role"]))
        if netbox.get("status"):
            node_rows.append(("NB Status", netbox["status"]))

    for label, value in node_rows:
        if not value:
            continue  # hide rows with no data
        print(f"  {label:<14} {CYAN}{value}{RESET}")


    # RMA reason + node name
    if ctx.get("rma_reason") or ctx.get("node_name"):
        print(f"  {DIM}{line}{RESET}")
        if ctx.get("rma_reason"):
            print(f"  {'RMA Reason':<14} {YELLOW}{ctx['rma_reason']}{RESET}")
        if ctx.get("node_name"):
            print(f"  {'Node':<14} {CYAN}{ctx['node_name']}{RESET}")

    # PSU quick-reference (for PSU tickets)
    psu = ctx.get("psu_info")
    if psu:
        print(f"  {DIM}{line}{RESET}")
        psu_id = psu.get("psu_id", "?")
        if psu.get("all_psu_ids") and len(psu["all_psu_ids"]) > 1:
            all_ids = ", ".join(f"PSU {p}" for p in psu["all_psu_ids"])
            print(f"  {YELLOW}{BOLD}⚡ {all_ids}{RESET}  {RED}{BOLD}← FAILED{RESET}")
        else:
            print(f"  {YELLOW}{BOLD}⚡ PSU {psu_id}{RESET}  {RED}{BOLD}← REMOVE THIS ONE{RESET}")
        # Show PSU health Grafana link if available in diag_links
        psu_url = _find_psu_dashboard_url(ctx)
        if psu_url:
            print(f"    {GREEN}Dashboard:{RESET} {DIM}{psu_url[:90]}{RESET}")

    # HO context (linked HO for this DO)
    ho = ctx.get("ho_context")
    if ho and ctx.get("source") != "netbox":
        print(f"  {DIM}{line}{RESET}")
        hsc, hsd = _status_color(ho["status"])
        print(f"  {BOLD}HO Context{RESET}  {hsc}{hsd} {ho['key']}{RESET}  {hsc}{ho['status']}{RESET}")
        print(f"  {DIM}{ho['summary']}{RESET}")
        print(f"  {MAGENTA}{ho['hint']}{RESET}")
        if ho.get("last_note"):
            print(f"  {DIM}Last note: {ho['last_note']}{RESET}")

    # SLA timers (Jira tickets only)
    sla_values = ctx.get("sla", [])
    if sla_values and ctx.get("source") != "netbox":
        print(f"  {DIM}{line}{RESET}")
        print(f"  {BOLD}SLA{RESET}")
        for sla in sla_values:
            sla_name = sla.get("name", "?")
            ongoing = sla.get("ongoingCycle")
            completed = sla.get("completedCycles", [])

            if not ongoing and completed:
                last = completed[-1]
                breached = last.get("breached", False)
                elapsed = (last.get("elapsedTime") or {}).get("friendly", "?")
                if breached:
                    print(f"    {RED}\u25cf Breached{RESET}  {DIM}{sla_name}  (took {elapsed}){RESET}")
                else:
                    print(f"    {GREEN}\u25cf Met{RESET}       {DIM}{sla_name}  (in {elapsed}){RESET}")
            elif ongoing:
                if ongoing.get("breached"):
                    elapsed = (ongoing.get("elapsedTime") or {}).get("friendly", "?")
                    print(f"    {RED}\u25cf Breached{RESET}  {sla_name}  {DIM}({elapsed} elapsed){RESET}")
                elif ongoing.get("paused"):
                    remaining = (ongoing.get("remainingTime") or {}).get("friendly", "?")
                    print(f"    {BLUE}\u25cf Paused{RESET}    {sla_name}  {DIM}({remaining} remaining){RESET}")
                else:
                    remaining_ms = (ongoing.get("remainingTime") or {}).get("millis", 0)
                    goal_ms = (ongoing.get("goalDuration") or {}).get("millis", 1)
                    remaining_str = (ongoing.get("remainingTime") or {}).get("friendly", "?")
                    pct = remaining_ms / goal_ms if goal_ms else 0
                    if pct < 0.25:
                        color = RED
                    elif pct < 0.50:
                        color = YELLOW
                    else:
                        color = GREEN
                    print(f"    {color}\u25cf {remaining_str}{RESET}  {sla_name}  {DIM}remaining{RESET}")

    print()



def _print_json(ctx: dict):
    """Print only a clean JSON object to stdout.
    Includes all fields except raw_issue and internal _-prefixed keys."""
    out = {k: v for k, v in ctx.items() if not k.startswith("_") and k != "raw_issue"}
    print(json.dumps(out, indent=2))



def _print_raw(ctx: dict):
    """Print the full raw Jira issue JSON to stdout."""
    print(json.dumps(ctx["raw_issue"], indent=2))


