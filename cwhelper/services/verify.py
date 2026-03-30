"""Verify subcommand — DCT self-service node verification.

Given a ticket key, hostname, or serial, runs the appropriate verification
flow (IB port, BMC cable, DPU, power/PSU, drive, RMA) and prints a
red/green verdict.

Usage:
    cwhelper verify DO-96947
    cwhelper verify DO-96947 --type power
    cwhelper verify ss943425x5109244 --type bmc
"""
from __future__ import annotations

import sys
import time

from cwhelper import config as _cfg
from cwhelper.config import *  # noqa: F401,F403
from cwhelper.clients.kubectl import (
    _kubectl_available, _kubectl_ensure_mgmt_cluster, _kubectl_get_bmn,
    _kubectl_get_bmn_yaml, _extract_bmn_fields, _serial_to_bmn_name,
    _ping_bmc, _run_on_jump_host,
    _kubectl_get_hpc_verification, _kubectl_get_jobs_hpc,
)
from cwhelper.clients.teleport import _tsh_available, _tsh_ensure_login

__all__ = ['run_verify', 'run_verify_batch']

# ---------------------------------------------------------------------------
# Flow type detection from ticket summary
# ---------------------------------------------------------------------------

_FLOW_KEYWORDS = {
    "ib":    ["ib ", "infiniband", "ib port", "ib cable", "ibp", "ib reseat"],
    "bmc":   ["bmc", "bmc cable", "bmc reseat", "idrac", "ipmi"],
    "dpu":   ["dpu", "dpu reseat", "bluefield"],
    "power": ["power", "psu", "power_cycle", "power cycle", "power cable",
              "remove and replace"],
    "drive": ["drive", "disk", "storage", "ssd", "nvme", "hdd"],
    "rma":   ["rma", "swap", "replacement"],
}


def _detect_flow(summary: str) -> str:
    """Detect verification flow type from ticket summary. Returns flow key or 'general'."""
    lower = summary.lower()
    for flow, keywords in _FLOW_KEYWORDS.items():
        for kw in keywords:
            if kw in lower:
                return flow
    return "general"


# ---------------------------------------------------------------------------
# Pretty output helpers
# ---------------------------------------------------------------------------

_CHECK = f"{GREEN}✓{RESET}"
_CROSS = f"{RED}✗{RESET}"
_WARN  = f"{YELLOW}?{RESET}"
_ARROW = f"{DIM}→{RESET}"

_FLOW_LABELS = {
    "ib":      "IB Port / Cable",
    "bmc":     "BMC Cable",
    "dpu":     "DPU Reseat",
    "power":   "Power / PSU",
    "drive":   "Drive (Storage)",
    "rma":     "RMA Verification",
    "general": "General Health",
}


def _header(flow: str, identifier: str):
    label = _FLOW_LABELS.get(flow, flow.upper())
    print()
    print(f"  {BOLD}{CYAN}┌─ VERIFY ── {label} ──────────────────────────────┐{RESET}")
    print(f"  {BOLD}{CYAN}│{RESET}  {BOLD}{identifier}{RESET}")
    print(f"  {BOLD}{CYAN}└───────────────────────────────────────────────────┘{RESET}")
    print()


def _step(num: int, desc: str):
    print(f"  {DIM}[{num}]{RESET} {desc} ", end="", flush=True)


def _ok(detail: str = ""):
    msg = f"{_CHECK} {GREEN}{detail}{RESET}" if detail else _CHECK
    print(msg)


def _fail(detail: str = ""):
    msg = f"{_CROSS} {RED}{detail}{RESET}" if detail else _CROSS
    print(msg)


def _warn(detail: str = ""):
    msg = f"{_WARN} {YELLOW}{detail}{RESET}" if detail else _WARN
    print(msg)


def _info(text: str):
    print(f"       {DIM}{text}{RESET}")


def _verdict(status: str, reason: str = "", notes: list[str] | None = None):
    """Print verdict banner.

    status: 'healthy', 'fix_worked', or 'broken'
      - healthy:    everything green
      - fix_worked: your repair succeeded but other issues exist (mgmt plane, etc.)
      - broken:     the specific repair failed
    """
    print()
    if status == "healthy":
        print(f"  {GREEN}{BOLD}╔═══════════════════════════════════════╗{RESET}")
        print(f"  {GREEN}{BOLD}║  VERIFIED HEALTHY                     ║{RESET}")
        if reason:
            print(f"  {GREEN}{BOLD}║{RESET}  {reason}")
        print(f"  {GREEN}{BOLD}╚═══════════════════════════════════════╝{RESET}")
    elif status == "fix_worked":
        print(f"  {YELLOW}{BOLD}╔═══════════════════════════════════════╗{RESET}")
        print(f"  {YELLOW}{BOLD}║  YOUR FIX WORKED                      ║{RESET}")
        if reason:
            print(f"  {YELLOW}{BOLD}║{RESET}  {reason}")
        print(f"  {YELLOW}{BOLD}╚═══════════════════════════════════════╝{RESET}")
    else:
        print(f"  {RED}{BOLD}╔═══════════════════════════════════════╗{RESET}")
        print(f"  {RED}{BOLD}║  STILL BROKEN                         ║{RESET}")
        if reason:
            print(f"  {RED}{BOLD}║{RESET}  {reason}")
        print(f"  {RED}{BOLD}╚═══════════════════════════════════════╝{RESET}")
    if notes:
        for note in notes:
            print(f"  {DIM}{note}{RESET}")
    print()


# ---------------------------------------------------------------------------
# Preflight checks
# ---------------------------------------------------------------------------

def _preflight() -> bool:
    """Check that tsh and kubectl are available. Returns True if ready."""
    ok = True

    _step(0, "Checking tsh...")
    if _tsh_available():
        _ok("logged in")
    else:
        # Try interactive login
        print()
        logged_in = _tsh_ensure_login(interactive=True)
        if logged_in:
            _step(0, "Checking tsh...")
            _ok("logged in")
        else:
            _step(0, "Checking tsh...")
            _fail("tsh not available or not logged in")
            ok = False

    _step(0, "Checking kubectl...")
    if _kubectl_available():
        _ok("on PATH")
    else:
        _fail("kubectl not found")
        ok = False

    if ok:
        _step(0, "Switching to mgmt cluster...")
        if _kubectl_ensure_mgmt_cluster():
            _ok("us-site-01a-mgmt")
        else:
            _fail("could not switch to mgmt cluster")
            _info("Run: tsh kube login us-site-01a-mgmt")
            ok = False

    return ok


# ---------------------------------------------------------------------------
# Resolve identifier → BMN fields
# ---------------------------------------------------------------------------

def _resolve_node(identifier: str) -> dict | None:
    """Resolve a ticket key, hostname, or serial to BMN fields.

    Returns dict with bmn fields or None if not found.
    """
    import re
    from cwhelper.clients.jira import _jira_get_issue, _get_credentials
    from cwhelper.services.context import _extract_custom_fields

    hostname = None
    serial = None
    summary = ""

    # If it's a Jira ticket key, pull hostname/serial from ticket
    if re.match(r"^[A-Za-z]+-\d+$", identifier):
        key = identifier.upper()
        try:
            email, token = _get_credentials()
            issue = _jira_get_issue(key, email, token)
            if not issue:
                print(f"  {_CROSS} {RED}Ticket {key} not found{RESET}")
                return None
            fields = issue.get("fields", {})
            summary = (fields.get("summary") or "").strip()
            custom = _extract_custom_fields(fields)
            hostname = custom.get("hostname", "")
            serial = custom.get("service_tag", "")
            if hostname:
                _info(f"Ticket: {key}  Hostname: {hostname}  Serial: {serial}")
            else:
                _info(f"Ticket: {key}  Serial: {serial}")
        except Exception as e:
            print(f"  {_CROSS} {RED}Could not fetch ticket: {e}{RESET}")
            return None
    elif identifier.startswith("ss") or (len(identifier) > 8 and identifier[0].isalpha()):
        # Looks like a serial
        serial = identifier
    else:
        # Treat as hostname
        hostname = identifier

    # Try to find BMN by serial first, then hostname
    bmn = None
    search_term = None

    if serial:
        bmn_name = _serial_to_bmn_name(serial)
        _step(1, f"Looking up BMN by serial ({bmn_name})...")
        bmn = _kubectl_get_bmn_yaml(bmn_name)
        if bmn:
            _ok("found")
        else:
            _warn("not found by serial")
            search_term = serial

    if not bmn and hostname:
        _step(1, f"Looking up BMN by hostname ({hostname})...")
        matches = _kubectl_get_bmn(hostname)
        if matches:
            _ok(f"{len(matches)} match(es)")
            # Extract the BMN name from the first match line
            parts = matches[0].split()
            if parts:
                bmn = _kubectl_get_bmn_yaml(parts[0])
        else:
            _fail("not found")

    if not bmn and search_term:
        _step(1, f"Searching BMN for '{search_term}'...")
        matches = _kubectl_get_bmn(search_term)
        if matches:
            _ok(f"{len(matches)} match(es)")
            parts = matches[0].split()
            if parts:
                bmn = _kubectl_get_bmn_yaml(parts[0])
        else:
            _fail("node not found in BMN")
            return None

    if not bmn:
        return None

    result = _extract_bmn_fields(bmn)
    result["_summary"] = summary

    # Prefer Jira's full hostname over BMN's short hostname
    # BMN often returns short names like "g98943a" while Jira has "dh1-r273-node-07-us-site-01a"
    if hostname and len(hostname) > len(result.get("hostname", "")):
        result["hostname"] = hostname
    if not result.get("bmc_ip"):
        pass  # NetBox fallback happens in _verify_bmc_reachable()

    # Debug: show what BMN returned if fields are sparse
    if not result.get("lifecycle_state") and not result.get("provision_state"):
        _info(f"BMN returned limited data (RBAC may restrict fields)")
        raw_keys = result.get("_raw_keys", [])
        spec_keys = result.get("_spec_keys", [])
        status_keys = result.get("_status_keys", [])
        if raw_keys:
            _info(f"Top-level keys: {', '.join(raw_keys)}")
        if spec_keys:
            _info(f"spec keys: {', '.join(spec_keys)}")
        if status_keys:
            _info(f"status keys: {', '.join(status_keys)}")

    return result


# ---------------------------------------------------------------------------
# Verification flows
# ---------------------------------------------------------------------------

def _verify_bmn_health(node: dict, step_start: int = 2) -> tuple[bool, int]:
    """Check BMN ready, lifecycle, provision, health. Returns (healthy, next_step)."""
    s = step_start
    healthy = True

    # Health state (from status.health)
    _step(s, "Health state...")
    hs = node.get("health_state", "")
    if hs:
        if hs.lower() in ("healthy", "ready"):
            _ok(hs)
        else:
            _warn(hs)
    else:
        _warn("not reported")
    s += 1

    _step(s, "BMN ready...")
    if node["ready"] is True:
        _ok("true")
    elif node["ready"] is False:
        _fail("false")
        healthy = False
    else:
        _warn(f"unknown ({node['ready']})")
    s += 1

    _step(s, "Lifecycle state...")
    ls = node["lifecycle_state"]
    if ls:
        if ls.lower() == "production":
            _ok(ls)
        elif ls.lower() in ("test", "onboarding", "seatrial", "zap"):
            _warn(ls)
        else:
            _fail(ls)
            healthy = False
    else:
        _warn("unknown")
    s += 1

    _step(s, "Provision state...")
    ps = node["provision_state"]
    if ps:
        if ps.lower() in ("ready", "provisioned"):
            _ok(ps)
        elif ps.lower() == "power-cycle":
            _warn(ps)
        elif ps.lower() in ("fail", "failed"):
            _fail(ps)
            healthy = False
        else:
            _warn(ps)
    else:
        _warn("unknown")
    s += 1

    # Extra metadata if available
    model = node.get("model", "")
    sku = node.get("sku", "")
    region = node.get("region", "")
    rack = node.get("rack_info", "")
    extras = []
    if model:
        extras.append(f"Model: {model}")
    if sku:
        extras.append(f"SKU: {sku}")
    if region:
        extras.append(f"Region: {region}")
    if rack:
        extras.append(f"Rack: {rack}")
    if extras:
        _info("  ".join(extras))

    return healthy, s


def _netbox_bmc_lookup(node: dict) -> str:
    """Try to get BMC IP from NetBox OOB interface. Returns IP or empty string."""
    try:
        from cwhelper.clients.netbox import (
            _netbox_find_device, _netbox_available, _netbox_get_interfaces,
        )
        if not _netbox_available():
            _warn("NetBox not configured")
            return ""
        # Derive physical serial from BMN name: ss943425x5109244 → S943425X5109244
        bmn_name = node.get("name", "")
        serial = None
        if bmn_name.startswith("ss"):
            serial = "S" + bmn_name[2:].upper()
        hostname = node.get("hostname", "")
        nb_device = _netbox_find_device(serial=serial, name=hostname or None)
        if not nb_device:
            _warn("device not found in NetBox")
            return ""

        # 1) Try device-level oob_ip first
        oob_ip_obj = nb_device.get("oob_ip") or {}
        oob_addr = oob_ip_obj.get("address", "")
        if oob_addr:
            if "/" in oob_addr:
                oob_addr = oob_addr.split("/")[0]
            return oob_addr

        # 2) Fall back: check interfaces for bmc/ipmi/oob/ilo/idrac
        device_id = nb_device.get("id")
        if device_id:
            ifaces = _netbox_get_interfaces(device_id)
            _BMC_NAMES = {"bmc", "ipmi", "oob", "ilo", "idrac", "redfish"}
            for iface in ifaces:
                iface_name = (iface.get("name") or "").lower()
                if any(bmc_name in iface_name for bmc_name in _BMC_NAMES):
                    # Check for IP on this interface
                    # NetBox interfaces don't carry IPs directly — IPs are
                    # assigned to interfaces via /ipam/ip-addresses/?interface_id=
                    from cwhelper.clients.netbox import _netbox_get
                    ip_data = _netbox_get("/ipam/ip-addresses/", params={
                        "interface_id": iface.get("id"),
                        "limit": 5,
                    })
                    if ip_data and ip_data.get("results"):
                        addr = ip_data["results"][0].get("address", "")
                        if addr:
                            if "/" in addr:
                                addr = addr.split("/")[0]
                            _info(f"found on interface '{iface.get('name')}'")
                            return addr

        _warn("no BMC/OOB IP in NetBox")
        return ""
    except Exception as e:
        _warn(f"NetBox lookup failed: {e}")
        return ""


def _verify_bmc_reachable(node: dict, step_start: int = 5) -> tuple[bool, int]:
    """Ping BMC from jump host. Returns (reachable, next_step)."""
    s = step_start
    bmc_ip = node.get("bmc_ip", "")

    if not bmc_ip:
        _step(s, "BMC IP — checking NetBox...")
        bmc_ip = _netbox_bmc_lookup(node)
        if bmc_ip:
            _ok(f"{bmc_ip} (from NetBox OOB)")
            node["bmc_ip"] = bmc_ip  # persist for later steps
        else:
            _fail("no BMC IP from BMN or NetBox")
            return False, s + 1
        s += 1

    _step(s, f"Pinging BMC ({bmc_ip})...")
    result = _ping_bmc(bmc_ip)
    if result is True:
        _ok("alive")
        return True, s + 1
    elif result is False:
        _fail("no response")
        _info("Check: cable seating, switch port, try different cable")
        return False, s + 1
    else:
        _warn("could not reach jump host")
        _info("Jump host may be down. Check #teleport or on-call.")
        return False, s + 1


def _verify_ib_conditions(node: dict, step_start: int = 5) -> tuple[bool, int]:
    """Check IB-related conditions in BMN status. Returns (healthy, next_step)."""
    s = step_start
    conditions = node.get("conditions", [])
    ib_conditions = [c for c in conditions if "ib" in c.get("type", "").lower()
                     or "infiniband" in c.get("type", "").lower()]

    _step(s, "IB conditions in BMN...")
    if not ib_conditions:
        _warn("no IB conditions found in BMN status")
        _info("Check Grafana NCore IB dashboard or UFM for port status")
        return True, s + 1  # not necessarily broken, just no data

    all_ok = True
    for cond in ib_conditions:
        ctype = cond.get("type", "")
        cstatus = cond.get("status", "")
        if cstatus.lower() in ("true", "healthy", "active"):
            _ok(f"{ctype}: {cstatus}")
        else:
            _fail(f"{ctype}: {cstatus}")
            all_ok = False
    return all_ok, s + 1


def _verify_dpu(node: dict, step_start: int = 5) -> tuple[bool, int]:
    """Check if DPU node appears in tsh ls. Returns (healthy, next_step)."""
    s = step_start
    hostname = node.get("hostname", "")

    if not hostname:
        _step(s, "DPU node lookup...")
        _warn("no hostname — can't check DPU")
        return False, s + 1

    # Try to find DPU node in tsh ls (limited grep to avoid hanging)
    dpu_pattern = hostname.replace("node", "dpu")
    _step(s, f"Searching tsh for DPU ({dpu_pattern})...")

    try:
        import subprocess
        r = subprocess.run(
            ["tsh", "ls", f"--format=names", f"--search={dpu_pattern}"],
            capture_output=True, timeout=15, text=True,
        )
        if r.returncode == 0 and r.stdout.strip():
            nodes = r.stdout.strip().split("\n")
            _ok(f"found {len(nodes)} DPU node(s)")
            for n in nodes[:3]:
                _info(n.strip())
            return True, s + 1
        else:
            _fail("DPU not found in Teleport")
            _info("DPU may not be booting — check physical seating and power")
            return False, s + 1
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        _warn("tsh ls timed out (large cluster)")
        _info("Try manually: tsh ls --format=names | grep dpu")
        return False, s + 1


def _verify_chassis_power(node: dict, step_start: int = 5) -> tuple[bool, int]:
    """Check chassis power via ipmitool SEL (without creds, we check BMC ping only)."""
    s = step_start
    bmc_ip = node.get("bmc_ip", "")

    if not bmc_ip:
        _step(s, "Chassis power check...")
        _warn("no BMC IP — skipping ipmitool checks")
        _info("BMC IP not in BMN. Check NetBox or ticket for BMC address.")
        return False, s + 1

    # We can't pass BMC creds automatically (they're not in BMN).
    # Print the commands for manual execution instead.
    _step(s, "Chassis power commands (manual)...")
    _warn("BMC creds required — run these from jump host:")
    print()
    print(f"       {CYAN}# SSH to jump host{RESET}")
    print(f"       {WHITE}tsh ssh acc@dh1-metal-jump01-us-site-01a{RESET}")
    print()
    print(f"       {CYAN}# Check power status{RESET}")
    print(f"       {WHITE}ipmitool -I lanplus -H {bmc_ip} -U <user> -P \"<pass>\" chassis power status{RESET}")
    print()
    print(f"       {CYAN}# Check recent SEL events{RESET}")
    print(f"       {WHITE}ipmitool -I lanplus -H {bmc_ip} -U <user> -P \"<pass>\" sel list | tail -10{RESET}")
    print()
    print(f"       {CYAN}# Check PSU sensors{RESET}")
    print(f"       {WHITE}ipmitool -I lanplus -H {bmc_ip} -U <user> -P \"<pass>\" sdr type \"Power Supply\"{RESET}")
    print()
    s += 1

    return True, s  # can't determine automatically without creds


def _verify_hpc(node: dict, step_start: int = 5) -> tuple[bool, int]:
    """Check HPC Verification status — proves GPU health.

    HPC Verification runs hourly on idle nodes, testing GPU integrity
    (FP8/FP16/BF16), thermal load, and silent data corruption.

    First checks BMN-embedded hpcVerification data (status.hpcVerification),
    then falls back to querying pods/jobs if BMN data is missing.
    """
    s = step_start

    # --- Check BMN-embedded HPC data first (from status.hpcVerification) ---
    hpc_state = node.get("hpc_state", "")
    hpc_last_run = node.get("hpc_last_run", "")

    if hpc_state:
        _step(s, "HPC Verification (BMN)...")
        last_run_display = hpc_last_run[:16].replace("T", " ") if hpc_last_run else ""

        if hpc_state.lower() in ("passed", "succeeded", "healthy", "complete", "completed"):
            _ok(f"{hpc_state} ({last_run_display})" if last_run_display else hpc_state)
            _info("GPU integrity, thermal, and data corruption checks passed")
            return True, s + 1
        elif hpc_state.lower() in ("running", "in_progress", "in-progress"):
            _ok(f"running ({last_run_display})" if last_run_display else "running")
            _info("HPC Verification test in progress — node is idle and being tested")
            return True, s + 1
        elif hpc_state.lower() in ("failed", "fail", "error"):
            _fail(f"{hpc_state} ({last_run_display})" if last_run_display else hpc_state)
            _info("GPU health test failed — node may have hardware issues")
            _info("Check Grafana Node Details dashboard for GPU SM Utilization")
            return False, s + 1
        else:
            _warn(f"{hpc_state} ({last_run_display})" if last_run_display else hpc_state)
            s += 1
            # Fall through to pod/job checks for more detail
    else:
        # Show raw HPC data if available but state was empty
        hpc_raw = node.get("_hpc_raw", {})
        if hpc_raw:
            _step(s, "HPC Verification (BMN)...")
            _warn("data present but no clear state")
            _info(f"Raw keys: {', '.join(hpc_raw.keys()) if isinstance(hpc_raw, dict) else str(type(hpc_raw))}")
            s += 1

    # --- Fall back to pod/job queries ---
    hostname = node.get("hostname", "")

    if not hostname:
        _step(s, "HPC Verification pods...")
        _warn("no hostname — can't check HPC pods")
        return True, s + 1  # not a failure, just no data

    # Check for HPC Verification pods on this node
    _step(s, "HPC Verification pods...")
    hpc = _kubectl_get_hpc_verification(hostname)

    if hpc["found"]:
        pods = hpc["pods"]
        latest = pods[-1]
        phase = latest["phase"]
        start = latest.get("start_time", "")[:16].replace("T", " ")

        if phase == "Succeeded":
            _ok(f"passed ({start})")
            _info("GPU integrity, thermal, and data corruption checks passed")
        elif phase == "Running":
            _ok(f"running now ({start})")
            _info("HPC Verification test in progress — node is idle and being tested")
        elif phase == "Failed":
            _fail(f"failed ({start})")
            _info("GPU health test failed — node may have hardware issues")
            _info("Check Grafana Node Details dashboard for GPU SM Utilization")
            return False, s + 1
        else:
            _warn(f"{phase} ({start})")

        if len(pods) > 1:
            _info(f"{len(pods)} HPC verification pod(s) found on this node")
        s += 1
    else:
        # No pods found — check Jobs for historical results
        _warn("no active pods")
        s += 1

        _step(s, "HPC Verification jobs (history)...")
        jobs = _kubectl_get_jobs_hpc(hostname)
        if jobs:
            latest = jobs[-1]
            state = latest["state"]
            completion = latest.get("completion", "")[:16].replace("T", " ")

            if state == "passed":
                _ok(f"last run passed ({completion})")
                _info("Most recent GPU health test passed")
            elif state == "failed":
                _fail(f"last run failed ({completion})")
                _info("GPU health test failed — check Grafana for GPU errors")
                return False, s + 1
            else:
                _warn(f"last run: {state} ({completion})")

            if len(jobs) > 1:
                passed = sum(1 for j in jobs if j["state"] == "passed")
                failed = sum(1 for j in jobs if j["state"] == "failed")
                _info(f"History: {passed} passed, {failed} failed out of {len(jobs)} runs")
        else:
            _warn("no HPC verification history found")
            _info("Node may not have been idle, or HPC verification namespace not accessible")
            _info("Check Grafana → Node Details → HPC verification status")
        s += 1

    return True, s


# ---------------------------------------------------------------------------
# Flow orchestrators
# ---------------------------------------------------------------------------

def _run_flow_ib(node: dict) -> dict:
    """IB Port / Cable reseat verification."""
    bmn_ok = True
    fix_ok = True
    notes = []

    h, s = _verify_bmn_health(node, step_start=2)
    bmn_ok = h

    h, s = _verify_ib_conditions(node, step_start=s)
    fix_ok = h  # IB conditions = the actual IB fix check

    h, s = _verify_bmc_reachable(node, step_start=s)
    if not h:
        notes.append("BMC unreachable — management plane issue, separate from IB")

    if not fix_ok:
        _info("Also check: physical link LED (solid green = link, off = down)")
        _info("Check Grafana NCore IB dashboard for port status")

    if fix_ok and not bmn_ok:
        notes.append("IB conditions look good but BMN health is degraded")
        notes.append("BMN issues may be unrelated to your IB reseat")

    return {"fix_ok": fix_ok, "bmn_ok": bmn_ok, "notes": notes}


def _run_flow_bmc(node: dict) -> dict:
    """BMC Cable reseat verification."""
    bmn_ok = True
    notes = []

    h, s = _verify_bmn_health(node, step_start=2)
    bmn_ok = h

    h, s = _verify_bmc_reachable(node, step_start=s)
    fix_ok = h  # BMC reachable = BMC cable fix worked

    if fix_ok:
        _verify_chassis_power(node, step_start=s)

    return {"fix_ok": fix_ok, "bmn_ok": bmn_ok, "notes": notes}


def _run_flow_dpu(node: dict) -> dict:
    """DPU reseat verification."""
    bmn_ok = True
    notes = []

    h, s = _verify_bmn_health(node, step_start=2)
    bmn_ok = h

    h, s = _verify_dpu(node, step_start=s)
    fix_ok = h  # DPU found in tsh = DPU reseat worked

    h, s = _verify_bmc_reachable(node, step_start=s)
    if not h:
        notes.append("BMC unreachable — separate from DPU health")

    return {"fix_ok": fix_ok, "bmn_ok": bmn_ok, "notes": notes}


def _run_flow_power(node: dict) -> dict:
    """Power / PSU verification.

    Returns dict with: fix_ok (bool), bmn_ok (bool), notes (list[str]).
    For PSU: HPC passing = PSU fix worked (GPUs have power, compute runs).
    BMN health/lifecycle issues are management-plane, not your repair.
    """
    bmn_ok = True
    fix_ok = False
    notes = []

    h, s = _verify_bmn_health(node, step_start=2)
    bmn_ok = h

    h, s = _verify_bmc_reachable(node, step_start=s)
    # BMC unreachable is mgmt-plane — doesn't mean PSU fix failed
    if not h:
        notes.append("BMC unreachable — management plane issue, not your PSU fix")

    _, s = _verify_chassis_power(node, step_start=s)

    h, s = _verify_hpc(node, step_start=s)
    fix_ok = h  # HPC passing = GPUs powered = PSU fix worked
    if h and not bmn_ok:
        notes.append("BMN shows failed/triage (likely RedfishDown or mgmt-plane alert)")
        notes.append("HPC passed → compute is healthy → your PSU fix worked")
        notes.append("Remaining issues are for engineers, not DCT scope")

    return {"fix_ok": fix_ok, "bmn_ok": bmn_ok, "notes": notes}


def _run_flow_drive(node: dict) -> dict:
    """Drive / Storage verification."""
    bmn_ok = True
    notes = []

    h, s = _verify_bmn_health(node, step_start=2)
    bmn_ok = h

    h, s = _verify_bmc_reachable(node, step_start=s)
    if not h:
        notes.append("BMC unreachable — can't run ipmitool SEL checks remotely")

    # Drive checks require SSH to the actual node — print manual commands
    hostname = node.get("hostname", "")
    bmc_ip = node.get("bmc_ip", "")
    _step(s, "Drive health commands (manual)...")
    _warn("requires SSH to node or ipmitool SEL:")
    print()
    if hostname:
        print(f"       {CYAN}# If you can SSH to the node:{RESET}")
        print(f"       {WHITE}tsh ssh acc@{hostname}{RESET}")
        print(f"       {WHITE}lsblk{RESET}")
        print(f"       {WHITE}smartctl -a /dev/sd<x>{RESET}")
        print()
    if bmc_ip:
        print(f"       {CYAN}# Check SEL for drive events:{RESET}")
        print(f"       {WHITE}ipmitool -I lanplus -H {bmc_ip} -U <user> -P \"<pass>\" sel list | grep -i \"drive\\|disk\\|storage\"{RESET}")
        print()

    # Can't auto-verify drives — need manual check
    return {"fix_ok": None, "bmn_ok": bmn_ok, "notes": notes}


def _run_flow_rma(node: dict) -> dict:
    """RMA verification — full node or component."""
    bmn_ok = True
    notes = []

    h, s = _verify_bmn_health(node, step_start=2)
    bmn_ok = h

    h, s = _verify_bmc_reachable(node, step_start=s)
    if not h:
        notes.append("BMC unreachable — may need engineer intervention")

    # RMA-specific: check if provision state indicates re-provisioning needed
    ps = node.get("provision_state", "")
    ls = node.get("lifecycle_state", "")
    if ps.lower() in ("fail", "failed"):
        _info("Provision state is fail — engineers need to re-provision")
        notes.append("Provision failed — engineer needs to re-provision")
    elif ls.lower() == "test":
        _info("Lifecycle is 'test' — may need manual promotion to production")

    h, s = _verify_hpc(node, step_start=s)
    fix_ok = h  # HPC passing = RMA'd component works

    if fix_ok and not bmn_ok:
        notes.append("HPC passed → hardware works, BMN state needs engineer attention")

    return {"fix_ok": fix_ok, "bmn_ok": bmn_ok, "notes": notes}


def _run_flow_general(node: dict) -> dict:
    """General health check — runs all basic checks."""
    bmn_ok = True
    notes = []

    h, s = _verify_bmn_health(node, step_start=2)
    bmn_ok = h

    h, s = _verify_bmc_reachable(node, step_start=s)
    if not h:
        notes.append("BMC unreachable — management plane issue")

    h, s = _verify_hpc(node, step_start=s)
    fix_ok = h and bmn_ok  # general: all must pass

    return {"fix_ok": fix_ok, "bmn_ok": bmn_ok, "notes": notes}


_FLOW_RUNNERS = {
    "ib":      _run_flow_ib,
    "bmc":     _run_flow_bmc,
    "dpu":     _run_flow_dpu,
    "power":   _run_flow_power,
    "drive":   _run_flow_drive,
    "rma":     _run_flow_rma,
    "general": _run_flow_general,
}


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_verify(identifier: str, flow_type: str | None = None,
               json_mode: bool = False, hints: dict | None = None):
    """Run verification for a ticket/node/serial.

    Args:
        identifier: Jira key (DO-96947), hostname, or serial number
        flow_type: Force a specific flow ('ib','bmc','dpu','power','drive','rma')
                   or None to auto-detect from ticket summary
        json_mode: Output structured JSON instead of pretty output
        hints: Optional dict with pre-resolved data from TUI context
               (hostname, bmc_ip, etc.) to supplement BMN fields
    """
    print()
    print(f"  {BOLD}cwhelper verify{RESET} {DIM}v{APP_VERSION}{RESET}")
    print()

    # Preflight
    if not _preflight():
        print()
        print(f"  {RED}Preflight failed — fix the issues above and retry.{RESET}")
        print()
        return

    print()

    # Resolve node
    node = _resolve_node(identifier)
    if not node:
        print()
        print(f"  {RED}Could not resolve node. Check the identifier and try again.{RESET}")
        print()
        return

    # Apply hints from TUI context (enriched data not available in raw Jira fields)
    if hints:
        if hints.get("hostname") and len(hints["hostname"]) > len(node.get("hostname", "")):
            node["hostname"] = hints["hostname"]
        if hints.get("bmc_ip") and not node.get("bmc_ip"):
            node["bmc_ip"] = hints["bmc_ip"]

    # Detect or use forced flow type
    summary = node.get("_summary", "")
    if flow_type:
        flow = flow_type.lower()
        if flow not in _FLOW_RUNNERS:
            print(f"  {RED}Unknown flow type: {flow}{RESET}")
            print(f"  {DIM}Valid types: {', '.join(_FLOW_RUNNERS.keys())}{RESET}")
            return
    else:
        flow = _detect_flow(summary) if summary else "general"

    _header(flow, f"{node['hostname'] or node['name']}  {DIM}({node['name']}){RESET}")

    # Summary line
    if node.get("hostname"):
        _info(f"Hostname: {node['hostname']}")
    bmc_display = node.get('bmc_ip') or 'none'
    _info(f"BMN: {node['name']}  BMC: {bmc_display}")
    hs = node.get("health_state", "")
    if hs:
        _info(f"Health: {hs}")
    if summary:
        _info(f"Ticket: {summary}")
    print()

    # Run the flow
    runner = _FLOW_RUNNERS[flow]
    result = runner(node)

    # result: {"fix_ok": bool|None, "bmn_ok": bool, "notes": list[str]}
    fix_ok = result.get("fix_ok")
    bmn_ok = result.get("bmn_ok", True)
    notes = result.get("notes", [])

    # Three-state verdict:
    #   fix_ok=True  + bmn_ok=True  → VERIFIED HEALTHY
    #   fix_ok=True  + bmn_ok=False → YOUR FIX WORKED (other issues exist)
    #   fix_ok=False                → STILL BROKEN
    #   fix_ok=None                 → manual check needed
    if fix_ok is None:
        _verdict("fix_worked",
                 "Manual verification needed — check commands above.",
                 notes)
    elif fix_ok and bmn_ok:
        _verdict("healthy",
                 "All checks passed. Safe to move ticket to Verification.",
                 notes)
    elif fix_ok and not bmn_ok:
        _verdict("fix_worked",
                 "Your repair worked. BMN issues are outside DCT scope.",
                 notes)
    else:
        _verdict("broken",
                 "The specific repair may not have taken. Investigate.",
                 notes)

    # JSON output
    if json_mode:
        import json as json_mod
        json_result = {
            "identifier": identifier,
            "flow": flow,
            "hostname": node.get("hostname", ""),
            "bmn_name": node.get("name", ""),
            "bmc_ip": node.get("bmc_ip", ""),
            "ready": node.get("ready"),
            "lifecycle": node.get("lifecycle_state", ""),
            "provision": node.get("provision_state", ""),
            "fix_ok": fix_ok,
            "bmn_ok": bmn_ok,
        }
        print(json_mod.dumps(json_result, indent=2))


# ---------------------------------------------------------------------------
# Batch verify — rack-level summary
# ---------------------------------------------------------------------------

def _verify_node_compact(serial: str) -> dict:
    """Quick BMN check for batch mode. Returns summary dict, no output."""
    bmn_name = _serial_to_bmn_name(serial)
    bmn = _kubectl_get_bmn_yaml(bmn_name)
    if not bmn:
        return {"serial": serial, "bmn_name": bmn_name, "found": False}

    node = _extract_bmn_fields(bmn)
    hostname = node.get("hostname", "")
    # Extract short node name (e.g. "node-07" from "dh1-r273-node-07-us-site-01a")
    short = hostname
    import re as _re
    m = _re.search(r"(node-\d+)", hostname)
    if m:
        short = m.group(1)

    hpc_state = node.get("hpc_state", "")
    hpc_ok = hpc_state.lower() in ("passed", "succeeded", "healthy", "complete", "completed")
    health = node.get("health_state", "")
    lifecycle = node.get("lifecycle_state", "")
    ready = node.get("ready")
    bmc_ip = node.get("bmc_ip", "")

    return {
        "serial": serial,
        "bmn_name": bmn_name,
        "found": True,
        "short": short,
        "hostname": hostname,
        "hpc_ok": hpc_ok,
        "hpc_state": hpc_state,
        "hpc_last_run": node.get("hpc_last_run", ""),
        "health": health,
        "lifecycle": lifecycle,
        "ready": ready,
        "bmc_ip": bmc_ip,
    }


def run_verify_batch(serials: list[str], rack_label: str = "",
                     flow_label: str = ""):
    """Batch verify a list of serials. Prints one-line-per-node summary.

    Args:
        serials: list of physical serial numbers (e.g. ["S948338X5405782", ...])
        rack_label: display label for the rack (e.g. "R273")
        flow_label: what was done (e.g. "PSU Reseat")
    """
    print()
    print(f"  {BOLD}cwhelper verify{RESET} {DIM}v{APP_VERSION} — batch{RESET}")
    print()

    # Preflight
    if not _preflight():
        print()
        print(f"  {RED}Preflight failed.{RESET}")
        return
    print()

    title = f"{rack_label} {flow_label}".strip() or "Batch Verify"
    print(f"  {BOLD}{CYAN}┌─ {title} ── {len(serials)} node(s) ─────────────────────────┐{RESET}")
    print(f"  {BOLD}{CYAN}└───────────────────────────────────────────────────┘{RESET}")
    print()

    # Header
    print(f"  {DIM}{'Node':<12} {'HPC':<14} {'Health':<10} {'Lifecycle':<12} {'BMC'}{RESET}")
    print(f"  {DIM}{'─'*12} {'─'*14} {'─'*10} {'─'*12} {'─'*10}{RESET}")

    results = []
    for serial in serials:
        r = _verify_node_compact(serial)
        results.append(r)

        if not r["found"]:
            print(f"  {r['serial'][:12]:<12} {RED}not found in BMN{RESET}")
            continue

        # HPC column
        if r["hpc_ok"]:
            hpc_display = f"{GREEN}✓ {r['hpc_state'][:10]}{RESET}"
        elif r["hpc_state"]:
            hpc_display = f"{RED}✗ {r['hpc_state'][:10]}{RESET}"
        else:
            hpc_display = f"{YELLOW}? none{RESET}"

        # Health column
        hs = r["health"]
        if hs.lower() in ("healthy", "ready"):
            health_display = f"{GREEN}{hs[:10]}{RESET}"
        elif hs:
            health_display = f"{YELLOW}{hs[:10]}{RESET}"
        else:
            health_display = f"{DIM}—{RESET}"

        # Lifecycle column
        ls = r["lifecycle"]
        if ls.lower() == "production":
            life_display = f"{GREEN}{ls[:12]}{RESET}"
        elif ls:
            life_display = f"{YELLOW}{ls[:12]}{RESET}"
        else:
            life_display = f"{DIM}—{RESET}"

        # BMC column
        bmc = r["bmc_ip"]
        bmc_display = f"{GREEN}✓{RESET}" if bmc else f"{RED}✗{RESET}"

        # Use short name with ANSI-aware padding
        name = r["short"] or r["bmn_name"][:12]
        print(f"  {name:<12} {hpc_display:<24} {health_display:<20} {life_display:<22} {bmc_display}")

    # Summary
    print()
    found = [r for r in results if r["found"]]
    hpc_pass = sum(1 for r in found if r.get("hpc_ok"))
    hpc_fail = len(found) - hpc_pass
    not_found = len(results) - len(found)

    if hpc_fail == 0 and not_found == 0:
        print(f"  {GREEN}{BOLD}{hpc_pass}/{len(serials)} fixes worked.{RESET}")
    elif hpc_fail > 0:
        # List the failures
        failed_nodes = [r["short"] or r["serial"][:12] for r in found if not r.get("hpc_ok")]
        print(f"  {GREEN}{BOLD}{hpc_pass}/{len(found)} fixes worked.{RESET}"
              f"  {RED}{BOLD}{hpc_fail} need attention: {', '.join(failed_nodes)}{RESET}")
    if not_found > 0:
        print(f"  {YELLOW}{not_found} serial(s) not found in BMN.{RESET}")
    print()
