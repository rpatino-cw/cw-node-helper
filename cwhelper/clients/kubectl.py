"""Kubernetes (kubectl) client — BMN queries via mgmt cluster.

Requires: `tsh` logged in + `kubectl` on PATH.
All functions return None/empty on failure — no exceptions to caller.
"""
from __future__ import annotations

import json
import os
import subprocess
from typing import Optional

__all__ = [
    '_kubectl_available', '_kubectl_get_bmn', '_kubectl_get_bmn_yaml',
    '_kubectl_ensure_mgmt_cluster', '_kubectl_current_context',
]


def _kubectl_available() -> bool:
    """True if kubectl is on PATH."""
    try:
        r = subprocess.run(
            ["kubectl", "version", "--client", "--output=json"],
            capture_output=True, timeout=5, text=True,
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _kubectl_current_context() -> Optional[str]:
    """Return current kubectl context name, or None."""
    try:
        r = subprocess.run(
            ["kubectl", "config", "current-context"],
            capture_output=True, timeout=5, text=True,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return None


def _kubectl_ensure_mgmt_cluster(site: str = "us-site-01a") -> bool:
    """Switch to the mgmt cluster for the given site via tsh. Returns True on success."""
    cluster = f"{site}-mgmt"
    ctx = _kubectl_current_context()
    if ctx and cluster in ctx:
        return True  # already on the right cluster
    try:
        r = subprocess.run(
            ["tsh", "kube", "login", cluster],
            capture_output=True, timeout=15, text=True,
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _serial_to_bmn_name(serial: str) -> str:
    """Convert a physical serial to BMN k8s name.

    Pattern: physical serial S943425X5109244 → BMN name ss943425x5109244
    Rule: strip leading 'S' (physical prefix), lowercase, prepend 'ss'.
    """
    s = serial.strip()
    # Already a BMN name
    if s.lower().startswith("ss"):
        return s.lower()
    # Physical serial starts with single 'S' — strip it, then prepend 'ss'
    if s[0:1].upper() == "S" and not s[0:2].upper() == "SS":
        s = s[1:]
    return f"ss{s.lower()}"


def _kubectl_get_bmn(grep_pattern: str) -> list[str]:
    """Run `kubectl get bmn` and grep for a pattern. Returns matching lines."""
    try:
        r = subprocess.run(
            ["kubectl", "get", "bmn"],
            capture_output=True, timeout=30, text=True,
        )
        if r.returncode != 0:
            return []
        lines = r.stdout.strip().split("\n")
        pattern = grep_pattern.lower()
        return [l for l in lines if pattern in l.lower()]
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []


def _kubectl_get_bmn_yaml(bmn_name: str) -> Optional[dict]:
    """Fetch a BMN resource as parsed YAML (via JSON output). Returns dict or None."""
    name = _serial_to_bmn_name(bmn_name) if not bmn_name.startswith("ss") else bmn_name
    try:
        r = subprocess.run(
            ["kubectl", "get", "bmn", name, "-o", "json"],
            capture_output=True, timeout=15, text=True,
        )
        if r.returncode != 0:
            return None
        return json.loads(r.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
        return None


def _extract_bmn_fields(bmn: dict) -> dict:
    """Extract key verification fields from a BMN JSON object.

    Real BMN structure (discovered via RBAC-limited access):
      spec: action, fieldDiag, firmware, flcc, nodeProfile, ownership, shatMetadata, storage
      status: cluster, conditions, deviceSlot, devices, execute, fieldDiag, firmware,
              flcc, health, hpcVerification, lastSeenDeviceSlot, manufacturer, model,
              nodeProfile, reboot, region, reportedDPUInfo, reportedNodeInfo, serial,
              sku, storage, topology
    """
    spec = bmn.get("spec", {})
    status = bmn.get("status", {})
    metadata = bmn.get("metadata", {})
    labels = metadata.get("labels", {})

    # --- Hostname ---
    # Try: status.reportedNodeInfo, status.cluster, labels, metadata.name
    reported = status.get("reportedNodeInfo", {})
    cluster_info = status.get("cluster", {})
    hostname = (reported.get("hostname", "")
                or reported.get("nodeName", "")
                or cluster_info.get("nodeName", "")
                or cluster_info.get("hostname", "")
                or spec.get("hostname", "")
                or labels.get("kubernetes.io/hostname", "")
                or "")

    # --- BMC IP ---
    # Try: spec.bmc, status.reportedNodeInfo, devices
    bmc_spec = spec.get("bmc", {})
    bmc_ip = (bmc_spec.get("ip", "")
              or bmc_spec.get("address", "")
              or reported.get("bmcIP", "")
              or reported.get("bmc_ip", "")
              or "")

    # --- Health / Ready ---
    health = status.get("health", {})
    ready = (health.get("ready", None)
             if isinstance(health, dict)
             else spec.get("ready", None))
    # health might be a string like "healthy" or a dict
    health_state = ""
    if isinstance(health, str):
        health_state = health
        ready = health.lower() in ("healthy", "ready", "true")
    elif isinstance(health, dict):
        health_state = health.get("state", health.get("status", ""))
        if ready is None:
            ready = health_state.lower() in ("healthy", "ready", "true") if health_state else None

    # --- Lifecycle (FLCC = Fleet Lifecycle) ---
    flcc_status = status.get("flcc", {})
    flcc_spec = spec.get("flcc", {})
    lifecycle_state = ""
    if isinstance(flcc_status, dict):
        lifecycle_state = (flcc_status.get("lifecycleState", "")
                           or flcc_status.get("state", "")
                           or flcc_status.get("lifecycle", ""))
    if not lifecycle_state and isinstance(flcc_spec, dict):
        lifecycle_state = (flcc_spec.get("lifecycleState", "")
                           or flcc_spec.get("state", ""))
    if not lifecycle_state:
        _fleet_prefix = os.environ.get("FLEET_LABEL_PREFIX", "fleet.example.com")
        lifecycle_state = labels.get(f"{_fleet_prefix}/lifecycle-state", "")

    # --- Provision ---
    provision_state = ""
    if isinstance(flcc_status, dict):
        provision_state = (flcc_status.get("provisionState", "")
                           or flcc_status.get("provision", ""))
    if not provision_state:
        provision_state = labels.get(f"{_fleet_prefix}/provision-state", "")

    # --- HPC Verification (directly on BMN!) ---
    hpc = status.get("hpcVerification", {})
    hpc_state = ""
    hpc_last_run = ""
    if isinstance(hpc, dict):
        hpc_state = hpc.get("state", hpc.get("status", ""))
        hpc_last_run = (hpc.get("lastRun", "")
                        or hpc.get("lastSuccess", "")
                        or hpc.get("lastCompletion", ""))
        # If no explicit state but lastCompletion exists, infer completed
        if not hpc_state and hpc_last_run:
            hpc_state = "completed"

    # --- Device slot (rack location) ---
    device_slot = status.get("deviceSlot", {})
    rack_info = ""
    if isinstance(device_slot, dict):
        rack_info = (device_slot.get("rack", "")
                     or device_slot.get("location", ""))

    # --- Region / Site ---
    region = status.get("region", "")
    if isinstance(region, dict):
        region = region.get("name", region.get("slug", ""))

    # --- Model / SKU ---
    model = status.get("model", "")
    if isinstance(model, dict):
        model = model.get("name", "")
    sku = status.get("sku", "")
    if isinstance(sku, dict):
        sku = sku.get("name", "")

    return {
        "name": metadata.get("name", ""),
        "hostname": hostname,
        "ready": ready,
        "health_state": health_state,
        "bmc_ip": bmc_ip,
        "lifecycle_state": lifecycle_state,
        "provision_state": provision_state,
        "conditions": status.get("conditions", []),
        "hpc_state": hpc_state,
        "hpc_last_run": hpc_last_run,
        "rack_info": rack_info,
        "region": region,
        "model": model,
        "sku": sku,
        "_raw_keys": list(bmn.keys()),
        "_spec_keys": list(spec.keys()) if spec else [],
        "_status_keys": list(status.keys()) if status else [],
        "_health_raw": health,
        "_flcc_status_raw": flcc_status,
        "_hpc_raw": hpc,
        "_reported_raw": reported,
        "_cluster_raw": cluster_info,
    }


def _kubectl_get_hpc_verification(node_name: str) -> dict:
    """Check HPC Verification pod status for a node.

    Looks for hpc-verification-* pods scheduled on the given node.
    Returns dict with: found (bool), status (str), pods (list), last_success (str).
    """
    result = {"found": False, "status": "unknown", "pods": [], "last_success": ""}

    # Search for hpc-verification pods on this node across all namespaces
    try:
        r = subprocess.run(
            ["kubectl", "get", "pods", "-A",
             "--field-selector", f"spec.nodeName={node_name}",
             "-o", "json"],
            capture_output=True, timeout=20, text=True,
        )
        if r.returncode != 0:
            # Try alternative: search by label in hpc-verification namespace
            r = subprocess.run(
                ["kubectl", "get", "pods", "-n", "cw-hpc-verification",
                 "-o", "json"],
                capture_output=True, timeout=20, text=True,
            )
            if r.returncode != 0:
                return result

        data = json.loads(r.stdout)
        items = data.get("items", [])

        # Filter for hpc-verification pods on this specific node
        hpc_pods = []
        for pod in items:
            name = pod.get("metadata", {}).get("name", "")
            ns = pod.get("metadata", {}).get("namespace", "")
            pod_node = pod.get("spec", {}).get("nodeName", "")

            is_hpc = ("hpc-verification" in name or
                      "hpc-verification" in ns)
            is_this_node = (pod_node == node_name or not pod_node)

            if is_hpc and is_this_node:
                phase = pod.get("status", {}).get("phase", "Unknown")
                start = pod.get("status", {}).get("startTime", "")
                hpc_pods.append({
                    "name": name,
                    "namespace": ns,
                    "phase": phase,
                    "node": pod_node,
                    "start_time": start,
                })

        if hpc_pods:
            result["found"] = True
            result["pods"] = hpc_pods
            # Check latest pod status
            latest = hpc_pods[-1]
            result["status"] = latest["phase"]
            result["last_success"] = latest.get("start_time", "")

        return result

    except (FileNotFoundError, subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
        return result


def _kubectl_get_jobs_hpc(node_name: str) -> list[dict]:
    """Check for completed HPC Verification Jobs for a node.

    Jobs (not pods) show historical pass/fail. Returns list of job summaries.
    """
    try:
        r = subprocess.run(
            ["kubectl", "get", "jobs", "-n", "cw-hpc-verification",
             "-o", "json"],
            capture_output=True, timeout=20, text=True,
        )
        if r.returncode != 0:
            return []

        data = json.loads(r.stdout)
        jobs = []
        for item in data.get("items", []):
            name = item.get("metadata", {}).get("name", "")
            if node_name.lower() not in name.lower():
                continue
            status = item.get("status", {})
            conditions = status.get("conditions", [])
            succeeded = status.get("succeeded", 0)
            failed = status.get("failed", 0)
            completion = status.get("completionTime", "")
            start = status.get("startTime", "")

            state = "unknown"
            for c in conditions:
                if c.get("type") == "Complete" and c.get("status") == "True":
                    state = "passed"
                elif c.get("type") == "Failed" and c.get("status") == "True":
                    state = "failed"

            if not state or state == "unknown":
                state = "passed" if succeeded > 0 else ("failed" if failed > 0 else "running")

            jobs.append({
                "name": name,
                "state": state,
                "start": start,
                "completion": completion,
                "succeeded": succeeded,
                "failed": failed,
            })
        return jobs

    except (FileNotFoundError, subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
        return []


def _run_on_jump_host(cmd: str,
                      jump_host: str = "your-jump-host",
                      timeout: int = 15) -> Optional[str]:
    """Run a command on the jump host via tsh ssh. Returns stdout or None."""
    try:
        r = subprocess.run(
            ["tsh", "ssh", f"acc@{jump_host}", "--", cmd],
            capture_output=True, timeout=timeout, text=True,
        )
        if r.returncode == 0:
            return r.stdout.strip()
        return r.stderr.strip() or None
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def _ping_bmc(bmc_ip: str,
              jump_host: str = "your-jump-host") -> Optional[bool]:
    """Ping BMC IP from jump host. Returns True (alive), False (dead), None (error)."""
    result = _run_on_jump_host(f"ping -c 2 -W 2 {bmc_ip}", jump_host=jump_host, timeout=15)
    if result is None:
        return None
    return "bytes from" in result.lower() or "0% packet loss" in result


def _ipmitool_cmd(bmc_ip: str, bmc_user: str, bmc_pass: str, args: str,
                  jump_host: str = "your-jump-host") -> Optional[str]:
    """Run an ipmitool command via the jump host. Returns stdout or None."""
    cmd = f'ipmitool -I lanplus -H {bmc_ip} -U {bmc_user} -P "{bmc_pass}" {args}'
    return _run_on_jump_host(cmd, jump_host=jump_host, timeout=20)
