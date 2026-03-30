"""Connection display — HO/MRB/SDx lookups and network cable views."""
from __future__ import annotations

import os
import re
import webbrowser

from cwhelper.config import (
    BOLD, DIM, RESET, CYAN, GREEN, YELLOW, RED, MAGENTA,
    JIRA_BASE_URL, SDX_PROJECTS, ISSUE_DETAIL_FIELDS,
)
from cwhelper.cache import _escape_jql, _lookup_ib_connections
from cwhelper.clients.jira import _jira_get_issue
from cwhelper.clients.netbox import _netbox_trace_interface
from cwhelper.services.ai import _ai_available, _ai_dispatch
from cwhelper.services.context import _short_device_name, _parse_rack_location
from cwhelper.services.search import _jql_search

__all__ = [
    '_find_linked_ho', '_summarize_ho_for_dct',
    '_show_mrb_for_node', '_show_sdx_for_ticket',
    '_trace_connection', '_print_connections_inline',
]


def _find_linked_ho(ctx: dict, email: str, token: str) -> dict | None:
    """Find the HO ticket linked to a DO. Returns HO issue dict or None.

    1. Check issuelinks for HO- keys (instant, no API call)
    2. Fallback: JQL search by service tag + site
    """
    # Check direct links first
    for link in ctx.get("linked_issues", []):
        key = link.get("key", "")
        if key.startswith("HO-"):
            try:
                return _jira_get_issue(key, email, token)
            except Exception:
                pass

    # Fallback: search by service tag + site
    tag = ctx.get("service_tag") or ""
    site = ctx.get("site") or ""
    if tag:
        try:
            jql = f'project = HO AND cf[10193] ~ "{tag}"'
            if site:
                jql += f' AND cf[10194] ~ "{site}"'
            jql += " ORDER BY created DESC"
            issues = _jql_search(jql, email, token, max_results=1,
                                 fields=ISSUE_DETAIL_FIELDS)
            if issues:
                return issues[0]
        except Exception:
            pass

    return None


def _summarize_ho_for_dct(ho_issue: dict) -> dict:
    """Build a compact HO summary dict for DCT display."""
    f = ho_issue.get("fields", {})
    key = ho_issue.get("key", "?")
    status = (f.get("status") or {}).get("name", "?")
    summary = f.get("summary", "")[:80]

    # Status-based hint
    sl = status.lower()
    if any(s in sl for s in ["rma-initiate", "sent to dct uc", "uncable"]):
        hint = "Uncable/Unrack RMA phase — expect or check for an Uncable DO."
    elif any(s in sl for s in ["sent to dct rc", "recable", "ready for verification"]):
        hint = "Recable/verification phase — expect or check for a Recable DO."
    elif "rma" in sl:
        hint = "RMA flow in progress — vendor/FROps handling parts."
    else:
        hint = "HO tracks full node history and vendor/RMA workflow."

    # Last comment (first line only)
    last_note = ""
    comments_container = f.get("comment", {})
    comments = comments_container.get("comments", []) if isinstance(comments_container, dict) else []
    if comments:
        body = comments[-1].get("body", "")
        if isinstance(body, dict):
            # ADF — extract first text node
            for node in (body.get("content") or []):
                for child in (node.get("content") or []):
                    if child.get("type") == "text" and child.get("text", "").strip():
                        last_note = child["text"].strip()[:80]
                        break
                if last_note:
                    break
        elif isinstance(body, str):
            last_note = body.strip().split("\n")[0][:80]

    return {
        "key": key,
        "status": status,
        "summary": summary,
        "hint": hint,
        "last_note": last_note,
    }


def _show_mrb_for_node(ctx: dict, email: str, token: str):
    """Search MRB project for RMA/parts tickets related to the current node."""
    from cwhelper.tui.display import _status_color
    tag = ctx.get("service_tag") or ""
    host = ctx.get("hostname") or ""
    site = ctx.get("site") or ""
    search_term = tag or host
    if not search_term:
        print(f"\n  {DIM}No service tag or hostname to search MRB.{RESET}")
        return

    # Build JQL: search MRB by service tag (or hostname) + site
    jql = f'project = MRB AND text ~ "{_escape_jql(search_term)}"'
    if site:
        jql += f' AND cf[10194] ~ "{_escape_jql(site)}"'
    jql += " ORDER BY created DESC"

    print(f"\n  {DIM}Searching MRB for '{search_term}'...{RESET}")
    issues = _jql_search(jql, email, token, max_results=10,
                         fields=["summary", "status", "assignee",
                                 "customfield_10193", "customfield_10194"])

    if not issues:
        print(f"\n  {YELLOW}{BOLD}No MRB tickets{RESET} {DIM}found for this node.{RESET}")
        return

    print(f"\n  {BOLD}MRB tickets{RESET}  {DIM}({len(issues)} found for {search_term}){RESET}\n")
    for i, iss in enumerate(issues, 1):
        f = iss.get("fields", {})
        key = iss.get("key", "?")
        st = f.get("status", {}).get("name", "?")
        sc, sd = _status_color(st)
        summary = f.get("summary", "")[:50]
        assignee = (f.get("assignee") or {}).get("displayName") or ""
        asg = f"  {DIM}{assignee}{RESET}" if assignee else ""
        print(f"    {BOLD}{i:>2}.{RESET}  {BOLD}{key:<12}{RESET} {sc}{sd} {st}{RESET}  {DIM}{summary}{RESET}{asg}")

    url_base = f"{JIRA_BASE_URL}/browse/"
    print(f"\n  {DIM}Open in Jira: {url_base}<KEY>{RESET}")


def _show_sdx_for_ticket(ctx: dict, email: str, token: str):
    """Find the originating SDx (customer) ticket for a DO/HO."""
    from cwhelper.tui.display import _status_color
    # 1. Check directly linked issues for SDx projects
    sdx_links = []
    for link in ctx.get("linked_issues", []):
        key = link.get("key", "")
        proj = key.split("-")[0] if "-" in key else ""
        if proj in SDX_PROJECTS:
            sdx_links.append(link)

    if sdx_links:
        print(f"\n  {BOLD}Linked SDx tickets{RESET}\n")
        for link in sdx_links:
            sc, sd = _status_color(link.get("status", ""))
            print(f"    {BOLD}{link['key']:<12}{RESET} {sc}{sd} {link['status']}{RESET}  {DIM}{link.get('summary', '')[:50]}{RESET}")
            print(f"    {DIM}{JIRA_BASE_URL}/browse/{link['key']}{RESET}")
        print()
        return

    # 2. Fallback: search SDx by service tag + site
    tag = ctx.get("service_tag") or ""
    site = ctx.get("site") or ""
    search_term = tag or ctx.get("hostname") or ""
    if not search_term:
        print(f"\n  {DIM}No SDx link found and no service tag to search.{RESET}")
        return

    jql = f'project in (SDA, SDE, SDO, SDP, SDS) AND text ~ "{_escape_jql(search_term)}"'
    if site:
        jql += f' AND cf[10194] ~ "{_escape_jql(site)}"'
    jql += " ORDER BY created DESC"

    print(f"\n  {DIM}No direct SDx link. Searching by '{search_term}'...{RESET}")
    issues = _jql_search(jql, email, token, max_results=5,
                         fields=["summary", "status", "assignee", "reporter",
                                 "customfield_10193", "customfield_10194"])

    if not issues:
        print(f"\n  {YELLOW}{BOLD}No SDx ticket{RESET} {DIM}found (no direct link and no match on service tag + site).{RESET}")
        return

    print(f"\n  {BOLD}SDx tickets{RESET}  {DIM}(matched by search){RESET}\n")
    for i, iss in enumerate(issues, 1):
        f = iss.get("fields", {})
        key = iss.get("key", "?")
        st = f.get("status", {}).get("name", "?")
        sc, sd = _status_color(st)
        summary = f.get("summary", "")[:50]
        reporter = (f.get("reporter") or {}).get("displayName") or ""
        rep = f"  {DIM}Reporter: {reporter}{RESET}" if reporter else ""
        print(f"    {BOLD}{i:>2}.{RESET}  {BOLD}{key:<12}{RESET} {sc}{sd} {st}{RESET}  {DIM}{summary}{RESET}{rep}")
        print(f"         {DIM}{JIRA_BASE_URL}/browse/{key}{RESET}")
    print()


def _trace_connection(ctx: dict, iface: dict):
    """Visually trace a cable path from this node to the peer device."""
    iface_id = iface.get("iface_id")
    if not iface_id:
        print(f"  {DIM}No interface ID — cannot trace.{RESET}")
        return

    print(f"\n  {DIM}Tracing cable path...{RESET}", flush=True)
    hops = _netbox_trace_interface(iface_id)
    if not hops:
        print(f"  {DIM}Trace unavailable (API returned no data).{RESET}")
        return

    # Parse hops into a flat list of endpoints
    # NetBox trace returns [[near_ends, cable, far_ends], ...]
    # Each end can be a list of terminations or a single dict
    def _pick_termination(end):
        """Extract a single termination dict from a trace endpoint."""
        if isinstance(end, list):
            return end[0] if end else None
        if isinstance(end, dict):
            return end
        return None

    endpoints = []
    for segment in hops:
        if not isinstance(segment, list) or len(segment) < 3:
            continue
        near = _pick_termination(segment[0])
        cable = segment[1] if isinstance(segment[1], dict) else {}
        far = _pick_termination(segment[2])
        if not endpoints and near:
            # First segment — include the near end (our interface)
            dev = near.get("device", {}) or {}
            endpoints.append({
                "device": dev.get("display") or dev.get("name") or "?",
                "port": near.get("display") or near.get("name") or "?",
                "rack": (dev.get("rack") or {}).get("display"),
                "position": dev.get("position"),
                "cable_id": cable.get("id"),
                "cable_label": cable.get("label") or cable.get("display"),
            })
        # Always include the far end
        if far:
            dev = far.get("device", {}) or {}
            endpoints.append({
                "device": dev.get("display") or dev.get("name") or "?",
                "port": far.get("display") or far.get("name") or "?",
                "rack": (dev.get("rack") or {}).get("display"),
                "position": dev.get("position"),
                "cable_id": cable.get("id"),
                "cable_label": cable.get("label") or cable.get("display"),
            })

    if not endpoints:
        print(f"  {DIM}Could not parse trace data.{RESET}")
        return

    port_name = iface.get("name", "?")
    speed = iface.get("speed", "")
    print(f"\n  {BOLD}Cable Trace: {port_name}{RESET}", end="")
    if speed:
        print(f"  {DIM}({speed}){RESET}")
    else:
        print()
    print(f"  {'═' * 54}")

    # Render each endpoint as a box with connecting arrows
    for i, ep in enumerate(endpoints):
        dev_short = _short_device_name(ep["device"])
        port_display = ep["port"].split(":")[-1] if ":" in ep["port"] else ep["port"]
        rack_str = ""
        if ep.get("rack"):
            # Extract just rack number from display like "US-SITE01.DH1.R244"
            rm = re.search(r"R(\d+)", ep["rack"])
            rack_str = f"R{rm.group(1).lstrip('0') or '0'}" if rm else ep["rack"]
        pos_str = f"U{ep['position']}" if ep.get("position") else ""
        loc = " · ".join(filter(None, [rack_str, pos_str]))

        # Label: "Your Node" for first, short device name for others
        if i == 0:
            label = f"{CYAN}{BOLD}Your Node{RESET}"
        else:
            label = f"{BOLD}{dev_short}{RESET}"

        print(f"\n  {label}")
        print(f"  {DIM}{ep['device']}{RESET}")
        if loc:
            print(f"  {DIM}{loc}{RESET}")
        print(f"  ┌{'─' * 22}┐")
        print(f"  │  {port_display:<20}│")
        print(f"  └{'─' * 22}┘")

        # Draw connecting arrow to next hop (if not last)
        if i < len(endpoints) - 1:
            cable_id = ep.get("cable_id")
            cable_tag = f"  {DIM}cable #{cable_id}{RESET}" if cable_id else ""
            print(f"       ║{cable_tag}")
            if speed:
                print(f"       ║  {DIM}{speed}{RESET}")
            print(f"       ▼")

    print()

    # --- Show connection map if we have rack locations for both ends ---
    _show_connection_on_map(ctx, endpoints)


def _show_connection_on_map(ctx: dict, endpoints: list):
    """If source and peer are in different racks, show a DH map with both highlighted."""
    from cwhelper.services.rack import _draw_connection_map, _draw_connection_map_image

    rack_loc = ctx.get("rack_location", "")
    if not rack_loc:
        return

    # Collect peer rack numbers from endpoints (skip the first = our node)
    peer_racks = set()
    for ep in endpoints[1:]:
        rack_display = ep.get("rack", "")
        if rack_display:
            rm = re.search(r"R(\d+)", rack_display)
            if rm:
                peer_racks.add(int(rm.group(1).lstrip("0") or "0"))

    if not peer_racks:
        return

    # Build labels
    source_name = _short_device_name(endpoints[0]["device"]) if endpoints else ""
    peer_names = []
    for ep in endpoints[1:]:
        peer_names.append(_short_device_name(ep["device"]))
    peer_label = ", ".join(peer_names) if peer_names else ""

    src_parsed = _parse_rack_location(rack_loc)
    src_rack = src_parsed["rack"] if src_parsed else None
    # Don't show map if peer is in the same rack
    if src_rack and peer_racks == {src_rack}:
        return

    kwargs = dict(
        rack_loc=rack_loc,
        peer_racks=peer_racks,
        source_label=f"{source_name} (R{src_rack})" if src_rack else source_name,
        peer_label=peer_label,
    )

    # Build port_info for elevation panel
    netbox = ctx.get("netbox", {})
    port_info = None
    if endpoints:
        src_ep = endpoints[0]
        peer_ep = endpoints[1] if len(endpoints) > 1 else {}
        peer_desc = _short_device_name(peer_ep.get("device", ""))
        peer_port = peer_ep.get("port", "")
        if peer_desc and peer_port:
            peer_desc += f" {peer_port}"
        port_info = dict(
            port=src_ep.get("port", ""),
            peer=peer_desc or peer_label,
            rack_id=netbox.get("rack_id"),
            device_name=netbox.get("device_name"),
            position=netbox.get("position"),
        )

    _draw_connection_map(**kwargs)
    _draw_connection_map_image(**kwargs, port_info=port_info)


def _print_connections_inline(ctx: dict):
    """Print network connections with speed, short names, and NetBox links."""
    from cwhelper.tui.display import _clear_screen, _print_pretty
    netbox = ctx.get("netbox", {})
    if not (netbox and netbox.get("interfaces")):
        return

    ifaces = netbox["interfaces"]

    # Group by role, preserving order
    groups = {}
    for iface in ifaces:
        role = iface.get("role", "—")
        groups.setdefault(role, []).append(iface)

    role_order = ["BMC", "DPU", "IB", "NIC", "—"]
    role_colors = {"BMC": YELLOW, "DPU": MAGENTA, "IB": GREEN, "NIC": CYAN}
    role_hints = {"BMC": "management", "DPU": "data fabric", "IB": "InfiniBand", "NIC": "network"}

    # Split cabled vs uncabled
    cabled_groups = {}
    uncabled_ib = []
    for iface in ifaces:
        if iface.get("_uncabled"):
            uncabled_ib.append(iface)
        else:
            role = iface.get("role", "—")
            cabled_groups.setdefault(role, []).append(iface)

    print(f"\n  {BOLD}Connections{RESET}")
    print(f"  {'━' * 54}")

    num = 0
    all_ifaces = []  # flat list for numbered selection
    for role in role_order:
        if role not in cabled_groups:
            continue
        color = role_colors.get(role, DIM)
        hint = role_hints.get(role, "")
        hint_str = f"  {DIM}({hint}){RESET}" if hint else ""
        print(f"\n  {color}{BOLD}{role}{RESET}{hint_str}")

        for iface in cabled_groups[role]:
            num += 1
            all_ifaces.append(iface)
            port = iface.get("name", "?")
            speed = iface.get("speed", "")
            peer = iface.get("peer_device", "?")
            peer_port = iface.get("peer_port", "?")
            peer_rack = iface.get("peer_rack", "")

            spd = f"{speed:<5}" if speed else "     "
            rack_tag = f" ({peer_rack})" if peer_rack else ""

            print(f"    {BOLD}{num}.{RESET}  {port:<10} {DIM}{spd}{RESET} {DIM}→{RESET}  {BOLD}{peer}{RESET}{DIM}{rack_tag}{RESET}  {DIM}{peer_port}{RESET}")

    # Show IB connections from topology cutsheet (if no cabled IB from NetBox)
    has_netbox_ib = "IB" in cabled_groups
    ib_topo = []
    ib_indexed = []  # parallel list: ib_topo entries indexed by num
    ib_start_num = num + 1  # first IB port number (continues from cabled)
    if not has_netbox_ib:
        hostname = ctx.get("hostname") or netbox.get("device_name") or ""
        ib_topo = _lookup_ib_connections(hostname, ctx.get("rack_location")) or []
        if ib_topo:
            print(f"\n  {GREEN}{BOLD}IB{RESET}  {DIM}(from cutsheet — {len(ib_topo)} ports){RESET}")
            for conn in ib_topo:
                num += 1
                ib_indexed.append(conn)
                port = conn.get("port", "?")
                leaf_rack = conn.get("leaf_rack", "?")
                leaf_id = conn.get("leaf_id", "?")
                leaf_port = conn.get("leaf_port", "?")
                print(f"    {BOLD}{num}.{RESET}  {port:<6} {DIM}400G{RESET}  {DIM}→{RESET}  {BOLD}Leaf {leaf_id}{RESET} {DIM}(R{leaf_rack}){RESET}  {DIM}port {leaf_port}{RESET}")
        elif uncabled_ib:
            print(f"\n  {DIM}IB  (not cabled in NetBox — {len(uncabled_ib)} ports){RESET}")
            uncabled_names = sorted([i.get("name", "?") for i in uncabled_ib])
            print(f"    {DIM}{', '.join(uncabled_names)}{RESET}")
        else:
            # No IB data from any source — hint about cutsheet
            role = (netbox.get("device_role") or "").lower()
            if "node" in role or "gpu" in role or not role:
                print(f"\n  {DIM}IB  No IB port data available for this site.{RESET}")
                print(f"    {DIM}Ask admin to upload the IB cutsheet for this data hall.{RESET}")

    # Footer with NetBox cable link hint
    has_cables = any(i.get("cable_id") for i in all_ifaces)
    has_map = bool(ctx.get("rack_location"))
    has_ib = bool(ib_indexed)
    total_conns = len(all_ifaces) + len(ib_indexed)
    ai_hint = f"  {DIM}│  'ai' to chat about connections{RESET}" if _ai_available() else ""
    map_hint = f"  {DIM}│  'm' show on map{RESET}" if has_map else ""
    print(f"\n  {DIM}{total_conns} connections{RESET}", end="")
    if has_cables or has_ib:
        cable_hint = "  # to open cable  t# to trace" if has_cables else ""
        ib_hint = "  # to show on map" if has_ib else ""
        print(f"  {DIM}│{cable_hint}{ib_hint}{RESET}{map_hint}{ai_hint}")
    else:
        print(f"{map_hint}{ai_hint}" if (map_hint or ai_hint) else "")

    # Interactive: let user open a cable in NetBox, show IB on map, or chat with AI
    try:
        raw = input(f"\n  > ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if raw.lower() == "ai" or raw.lower().startswith("ai "):
        initial = raw[3:].strip() if raw.lower().startswith("ai ") else ""
        email = os.environ.get("JIRA_EMAIL", "")
        token = os.environ.get("JIRA_API_TOKEN", "")
        _ai_dispatch(ctx=ctx, email=email, token=token, initial_msg=initial)
    elif raw.lower() == "m" and has_map:
        # Show all connections on the DH map
        _show_all_connections_on_map(ctx, all_ifaces, ib_topo)
        try:
            input(f"  {DIM}Press Enter to continue...{RESET}")
        except (EOFError, KeyboardInterrupt):
            pass
    elif raw.lower().startswith("t") and raw[1:].isdigit():
        picked = int(raw[1:])
        if has_cables and 1 <= picked <= len(all_ifaces):
            _trace_connection(ctx, all_ifaces[picked - 1])
            try:
                input(f"  {DIM}Press Enter to continue...{RESET}")
            except (EOFError, KeyboardInterrupt):
                pass
        elif has_ib and ib_start_num <= picked <= ib_start_num + len(ib_indexed) - 1:
            ib_conn = ib_indexed[picked - ib_start_num]
            _show_ib_connection_on_map(ctx, ib_conn)
            try:
                input(f"  {DIM}Press Enter to continue...{RESET}")
            except (EOFError, KeyboardInterrupt):
                pass
    elif raw.isdigit():
        picked = int(raw)
        if has_cables and 1 <= picked <= len(all_ifaces):
            # Cabled connection — open cable in NetBox
            cable_id = all_ifaces[picked - 1].get("cable_id")
            if cable_id:
                api_base = os.environ.get("NETBOX_API_URL", "").strip().rstrip("/")
                nb_base = api_base.rsplit("/api", 1)[0] if "/api" in api_base else api_base
                url = f"{nb_base}/dcim/cables/{cable_id}/"
                print(f"  {DIM}Opening {url}{RESET}")
                webbrowser.open(url)
        elif has_ib and ib_start_num <= picked <= ib_start_num + len(ib_indexed) - 1:
            ib_conn = ib_indexed[picked - ib_start_num]
            _show_ib_connection_on_map(ctx, ib_conn)
            try:
                input(f"  {DIM}Press Enter to continue...{RESET}")
            except (EOFError, KeyboardInterrupt):
                pass
    # After interaction, clear and reprint ticket info
    _clear_screen()
    _print_pretty(ctx)
    print()


def _show_ib_connection_on_map(ctx: dict, ib_conn: dict):
    """Show DH map for a single IB cutsheet connection (source → leaf rack)."""
    from cwhelper.services.rack import _draw_connection_map, _draw_connection_map_image

    rack_loc = ctx.get("rack_location", "")
    if not rack_loc:
        print(f"  {DIM}No rack location — cannot draw map.{RESET}")
        return

    leaf_rack = ib_conn.get("leaf_rack", "")
    if not leaf_rack:
        print(f"  {DIM}No peer rack data for this IB port.{RESET}")
        return

    try:
        peer_rack_num = int(leaf_rack)
    except ValueError:
        print(f"  {DIM}Invalid peer rack: {leaf_rack}{RESET}")
        return

    src_parsed = _parse_rack_location(rack_loc)
    src_rack = src_parsed["rack"] if src_parsed else None
    if src_rack == peer_rack_num:
        print(f"  {DIM}IB peer is in the same rack.{RESET}")
        return

    netbox = ctx.get("netbox", {})
    device_name = netbox.get("device_name") or ctx.get("hostname") or "?"
    short_name = _short_device_name(device_name)

    # Build peer label from IB connection info
    leaf_switch = ib_conn.get("leaf_switch", "")
    port = ib_conn.get("port", "")
    peer_label = leaf_switch if leaf_switch else f"R{peer_rack_num}"
    if port:
        peer_label += f" ({port})"

    # Build peer description for elevation label
    leaf_id = ib_conn.get("leaf_id", "")
    leaf_port = ib_conn.get("leaf_port", "")
    peer_desc = f"Leaf {leaf_id}" if leaf_id else f"R{peer_rack_num}"
    if leaf_port:
        peer_desc += f" (R{peer_rack_num}) port {leaf_port}"

    kwargs = dict(
        rack_loc=rack_loc,
        peer_racks={peer_rack_num},
        source_label=f"{short_name} (R{src_rack})" if src_rack else short_name,
        peer_label=peer_label,
    )
    port_info = dict(
        port=ib_conn.get("port", ""),
        peer=peer_desc,
        rack_id=netbox.get("rack_id"),
        device_name=device_name,
        position=netbox.get("position"),
    )
    _draw_connection_map(**kwargs)
    _draw_connection_map_image(**kwargs, port_info=port_info)


def _show_all_connections_on_map(ctx: dict, cabled_ifaces: list,
                                 ib_topo: list):
    """Show a DH connection map with all peer racks for this device."""
    from cwhelper.services.rack import _draw_connection_map, _draw_connection_map_image

    rack_loc = ctx.get("rack_location", "")
    if not rack_loc:
        print(f"  {DIM}No rack location — cannot draw map.{RESET}")
        return

    peer_racks = set()

    # Collect peer racks from cabled (Ethernet/BMC) interfaces
    for iface in cabled_ifaces:
        pr = iface.get("peer_rack", "")
        if pr:
            rm = re.search(r"(\d+)", pr)
            if rm:
                peer_racks.add(int(rm.group(1)))

    # Collect peer racks from IB topology
    for conn in ib_topo:
        lr = conn.get("leaf_rack", "")
        if lr:
            try:
                peer_racks.add(int(lr))
            except ValueError:
                pass

    if not peer_racks:
        print(f"  {DIM}No peer rack data to map.{RESET}")
        return

    netbox = ctx.get("netbox", {})
    device_name = netbox.get("device_name") or ctx.get("hostname") or "?"
    short_name = _short_device_name(device_name)

    src_parsed = _parse_rack_location(rack_loc)
    src_rack = src_parsed["rack"] if src_parsed else None
    # Remove self from peer set
    if src_rack in peer_racks:
        peer_racks.discard(src_rack)

    if not peer_racks:
        print(f"  {DIM}All connections are within the same rack.{RESET}")
        return

    kwargs = dict(
        rack_loc=rack_loc,
        peer_racks=peer_racks,
        source_label=f"{short_name} (R{src_rack})" if src_rack else short_name,
        peer_label=f"{len(peer_racks)} peer rack{'s' if len(peer_racks) != 1 else ''}",
    )
    _draw_connection_map(**kwargs)
    _draw_connection_map_image(**kwargs)
