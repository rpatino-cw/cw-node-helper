"""Rack visualization — elevation, DH maps, neighbors."""
from __future__ import annotations

import os
import re
import shutil
import sys
import textwrap
import time
import webbrowser

from cwhelper import config as _cfg
from cwhelper.config import *  # noqa: F401,F403
__all__ = ['_print_rack_neighbors', '_draw_neighbor_panel', '_print_netbox_info_inline', '_handle_rack_neighbors', '_handle_rack_view', '_draw_mini_dh_map', '_draw_connection_map', '_draw_connection_map_image', '_fetch_device_type_heights', '_draw_rack_elevation']
from cwhelper.clients.netbox import _netbox_available, _netbox_find_rack_by_name, _netbox_get_rack_devices, _netbox_find_device, _netbox_get, _netbox_get_interfaces, _fetch_neighbor_devices, _parse_iface_speed, _build_netbox_context
from cwhelper.clients.jira import _jira_get, _get_credentials, _is_mine
from cwhelper.state import _load_user_state, _get_dh_layout, _setup_dh_layout, _load_dh_layouts, _save_dh_layouts, _record_rack_view
from cwhelper.services.context import _parse_rack_location, _get_physical_neighbors, _short_device_name, _build_context
from cwhelper.cache import _classify_port_role, _lookup_ib_connections, _escape_jql, _brief_pause
from cwhelper.services.search import _search_queue, _jql_search
from cwhelper.services.queue import _search_node_history, _run_history_interactive
from cwhelper.tui.display import _status_color, _prompt_select, _clear_screen, _print_pretty
from cwhelper.clients.grafana import _build_grafana_urls
# NOTE: _post_detail_prompt (tui.actions) is imported lazily inside
# _print_netbox_info_inline to avoid a circular import (actions → rack → actions).




def _dev_lines(devices: list, prefix: str) -> list:
    """Format a list of NetBox devices as numbered display lines."""
    lines = []
    for i, dev in enumerate(devices, 1):
        name = dev.get("name") or dev.get("display") or "?"
        short = _short_device_name(name)
        pos = dev.get("position")
        pos_str = f"U{int(pos):<3}" if pos else "U?  "
        status_label = (dev.get("status") or {}).get("label") or "?"
        sc, _ = _status_color(status_label)
        lines.append(
            f"   {BOLD}{prefix}{i}.{RESET} {DIM}{pos_str}{RESET} "
            f"{short:<16} {sc}{status_label}{RESET}"
        )
    return lines


def _btn(key_char: str, label: str, color: str) -> str:
    """Render a single button-style prompt option."""
    return f"{color}{BOLD}[{key_char}]{RESET} {WHITE}{label}{RESET}"


def _open_device(chosen: dict, email: str, token: str) -> str | None:
    """Search Jira for a chosen NetBox device; show inline view if no tickets.

    Returns "quit" if the user quit from the history view, else None.
    """
    chosen_serial = chosen.get("serial")
    chosen_name = chosen.get("name") or chosen.get("display")
    search_term = chosen_serial or chosen_name
    if not search_term:
        print(f"\n  {DIM}Selected device has no serial or name to search.{RESET}")
        return None
    print(f"\n  {DIM}Searching Jira for '{search_term}'...{RESET}")
    issues = _search_node_history(search_term, email, token, limit=20)
    if issues:
        print(f"  Found {len(issues)} ticket(s).\n")
        result = _run_history_interactive(email, token, search_term)
        return result if result == "quit" else None
    _print_netbox_info_inline(chosen, email, token)
    return None


def _print_rack_neighbors(devices: list, current_device_name: str | None,
                          show_netbox_hint: bool = False):
    """Display a numbered list of rack neighbors and let the user pick one.

    Returns the chosen device dict, "x" for NetBox, or None if cancelled.
    """
    if not devices:
        print(f"\n  {DIM}No devices found in this rack.{RESET}")
        return None

    print(f"\n  {BOLD}Devices in rack{RESET}  {DIM}({len(devices)} devices){RESET}\n")

    def _label(i, dev):
        name = dev.get("name") or dev.get("display") or "?"
        short = _short_device_name(name)
        pos = dev.get("position")
        pos_str = f"U{int(pos):<3}" if pos else "U?  "
        status_label = (dev.get("status") or {}).get("label") or "?"
        sc, sd = _status_color(status_label)

        is_current = current_device_name and name == current_device_name
        marker = f"  {YELLOW}<-- you{RESET}" if is_current else ""
        name_fmt = f"{CYAN}{BOLD}{short}{RESET}" if is_current else short

        return (
            f"    {BOLD}{i:>2}.{RESET}  {DIM}{pos_str}{RESET} "
            f"{name_fmt:<18}"
            f"{sc}{sd} {status_label}{RESET}"
            f"{marker}"
        )

    extra = f", {BOLD}x{RESET} for NetBox" if show_netbox_hint else ""
    return _prompt_select(devices, _label, extra_hint=extra)



def _draw_neighbor_panel(neighbor_data: dict) -> dict:
    """Draw the adjacent-racks panel below the current rack's device list.

    neighbor_data has keys "left" and "right", each either None or
    {"rack_num": int, "rack_id": int|None, "devices": list}.

    Returns {"L": [devices], "R": [devices]} for prompt selection mapping.
    """
    term_w = shutil.get_terminal_size((80, 24)).columns
    side_by_side = term_w >= 100

    left = neighbor_data.get("left")
    right = neighbor_data.get("right")
    if not left and not right:
        return {"L": [], "R": []}

    print(f"\n  {DIM}{'─' * 2} Adjacent Racks {'─' * (min(term_w, 70) - 20)}{RESET}")

    # Build header
    left_hdr = ""
    right_hdr = ""
    if left:
        n_devs = len(left.get("devices", []))
        lbl = f"R{left['rack_num']}" if left["rack_num"] else "?"
        left_hdr = f"  {BOLD}<<{RESET} {BOLD}{lbl}{RESET}  {DIM}({n_devs} devices){RESET}"
    if right:
        n_devs = len(right.get("devices", []))
        lbl = f"R{right['rack_num']}" if right["rack_num"] else "?"
        right_hdr = f"{BOLD}{lbl}{RESET}  {DIM}({n_devs} devices){RESET} {BOLD}>>{RESET}"

    if side_by_side:
        # Pad left header to ~half width
        pad = max(2, (term_w // 2) - 20)
        print(f"\n  {left_hdr}{'':>{pad}}{right_hdr}" if left_hdr and right_hdr
              else f"\n  {left_hdr}{right_hdr}")
    else:
        if left_hdr:
            print(f"\n  {left_hdr}")
        if right_hdr:
            print(f"  {right_hdr}")

    # Build device lines for each side
    left_lines = _dev_lines(left["devices"], "L") if left and left.get("devices") else []
    right_lines = _dev_lines(right["devices"], "R") if right and right.get("devices") else []

    if side_by_side:
        # Print left and right side by side
        max_rows = max(len(left_lines), len(right_lines))
        # Calculate raw width of a left line (approx 40 visible chars)
        col_w = max(38, (term_w // 2) - 2)
        print()
        for row_i in range(max_rows):
            l_str = left_lines[row_i] if row_i < len(left_lines) else ""
            r_str = right_lines[row_i] if row_i < len(right_lines) else ""
            if l_str and r_str:
                # Pad left column using visible length
                visible_len = len(l_str.encode("ascii", "ignore").decode())
                # ANSI escapes make raw len > visible len; estimate padding
                print(f"{l_str}{'':>{max(2, col_w - 35)}}{r_str}")
            else:
                if l_str:
                    print(l_str)
                elif r_str:
                    print(f"{'':>{col_w}}{r_str}")
    else:
        # Stacked: left first, then right
        if left_lines:
            print()
            for ln in left_lines:
                print(ln)
        if right_lines:
            print()
            for ln in right_lines:
                print(ln)

    # Show empty-rack messages
    if left and not left.get("devices"):
        if left.get("rack_id") is None:
            print(f"\n   {DIM}R{left['rack_num']} — not found in NetBox{RESET}")
        else:
            print(f"\n   {DIM}R{left['rack_num']} — empty{RESET}")
    if right and not right.get("devices"):
        if right.get("rack_id") is None:
            print(f"   {DIM}R{right['rack_num']} — not found in NetBox{RESET}")
        else:
            print(f"   {DIM}R{right['rack_num']} — empty{RESET}")

    print()
    return {
        "L": left["devices"] if left else [],
        "R": right["devices"] if right else [],
    }



def _print_netbox_info_inline(device: dict, email: str = "", token: str = ""):
    """Rich view for a device with no open Jira tickets.

    Builds a lightweight ctx from the raw NetBox device dict, then uses
    _print_pretty and the full action panel so the user gets rack map,
    connections, elevation, etc. — everything except Jira ticket data.
    """
    dev_name = device.get("name") or device.get("display") or "?"
    serial = device.get("serial") or ""
    site_obj = device.get("site") or {}
    rack_obj = device.get("rack") or {}
    rack_id = rack_obj.get("id")
    position = device.get("position")
    primary_ip = (device.get("primary_ip") or {}).get("address", "").split("/")[0]
    oob_ip = (device.get("oob_ip") or {}).get("address", "").split("/")[0]
    status_label = (device.get("status") or {}).get("label") or "?"
    device_id = device.get("id")
    device_type_obj = device.get("device_type") or {}
    manufacturer_obj = device_type_obj.get("manufacturer") or {}

    # Build a ctx that _print_pretty and _post_detail_prompt can use
    netbox_ctx = {
        "device_name": dev_name,
        "device_id": device_id,
        "serial": serial,
        "asset_tag": device.get("asset_tag"),
        "site": site_obj.get("display") or site_obj.get("name"),
        "rack": rack_obj.get("display") or rack_obj.get("name"),
        "rack_id": rack_id,
        "position": position,
        "primary_ip": (device.get("primary_ip") or {}).get("address"),
        "primary_ip4": (device.get("primary_ip4") or {}).get("address"),
        "primary_ip6": (device.get("primary_ip6") or {}).get("address"),
        "oob_ip": (device.get("oob_ip") or {}).get("address"),
        "status": status_label,
        "device_role": (device.get("role") or device.get("device_role") or {}).get("display"),
        "platform": (device.get("platform") or {}).get("display"),
        "manufacturer": manufacturer_obj.get("display") or manufacturer_obj.get("name"),
        "model": device_type_obj.get("display") or device_type_obj.get("model"),
        "interfaces": [],
        "site_slug": site_obj.get("slug") or "",
    }

    # Fetch interfaces for connections
    if device_id and _netbox_available():
        try:
            ifaces = _netbox_get_interfaces(device_id)
            cabled_names = set()
            for iface in ifaces:
                cable = iface.get("cable")
                link_peers = iface.get("link_peers") or []
                full_name = iface.get("display") or iface.get("name") or "?"
                port_name = full_name.split(":")[-1] if ":" in full_name else full_name
                if not cable or not link_peers:
                    continue
                cabled_names.add(port_name)
                peer = link_peers[0]
                peer_dev = peer.get("device", {})
                peer_name_full = peer_dev.get("display") or peer_dev.get("name") or "?"
                peer_port = peer.get("display") or peer.get("name") or "?"
                peer_port_short = peer_port.split(":")[-1] if ":" in peer_port else peer_port
                peer_short = _short_device_name(peer_name_full)
                rack_match = re.search(r"-r(\d{2,4})", peer_name_full.lower())
                peer_rack = f"R{rack_match.group(1).lstrip('0') or '0'}" if rack_match else ""
                cable_id = cable.get("id") if isinstance(cable, dict) else None
                role = _classify_port_role(port_name)
                speed = _parse_iface_speed(iface.get("type"))
                netbox_ctx["interfaces"].append({
                    "name": port_name, "role": role, "speed": speed,
                    "peer_device": peer_short, "peer_device_full": peer_name_full,
                    "peer_port": peer_port_short, "peer_rack": peer_rack,
                    "cable_id": cable_id, "iface_id": iface.get("id"),
                    "connected_to": f"{peer_name_full}:{peer_port}",
                })
            # Include uncabled IB ports so DCTs can see they exist
            for iface in ifaces:
                full_name = iface.get("display") or iface.get("name") or "?"
                port_name = full_name.split(":")[-1] if ":" in full_name else full_name
                if port_name in cabled_names:
                    continue
                role = _classify_port_role(port_name)
                if role != "IB":
                    continue
                speed = _parse_iface_speed(iface.get("type"))
                netbox_ctx["interfaces"].append({
                    "name": port_name, "role": role, "speed": speed,
                    "peer_device": None, "peer_device_full": None,
                    "peer_port": None, "peer_rack": "",
                    "cable_id": None,
                    "connected_to": None,
                    "_uncabled": True,
                })
        except Exception:
            pass

    # Build a pseudo-ctx for _print_pretty and the action panel
    grafana = _build_grafana_urls(None, dev_name, serial or None, dev_name)

    # Try to find rack_location from NetBox rack name + position
    rack_location = ""
    site_code = site_obj.get("name") or ""
    rack_name = rack_obj.get("name") or ""
    if rack_name and position:
        rack_location = f"{site_code}.DH1.R{rack_name}.RU{int(position)}"

    ctx = {
        "source": "netbox",
        "identifier": serial or dev_name,
        "issue_key": f"{GREEN}{BOLD}No open tickets{RESET}",
        "summary": _short_device_name(dev_name),
        "status": status_label,
        "issue_type": "NetBox Device",
        "project": "\u2014",
        "assignee": None,
        "reporter": None,
        "rack_location": rack_location,
        "service_tag": serial,
        "hostname": dev_name,
        "site": site_obj.get("display") or site_obj.get("name"),
        "ip_address": primary_ip,
        "vendor": netbox_ctx.get("manufacturer"),
        "rma_reason": None,
        "node_name": None,
        "diag_links": [],
        "comments": [],
        "linked_issues": [],
        "grafana": grafana,
        "netbox": netbox_ctx,
        "_portal_url": None,
        "raw_issue": {},
    }

    _clear_screen()
    _print_pretty(ctx)

    # Recent history
    if email and token and serial:
        try:
            issues = _search_node_history(serial, email, token, limit=3)
            if issues:
                print(f"  {BOLD}Recent history{RESET}\n")
                for iss in issues[:3]:
                    f = iss.get("fields", {})
                    key = iss.get("key", "?")
                    st = f.get("status", {}).get("name", "?")
                    created = f.get("created", "")[:10]
                    summary = f.get("summary", "")[:45]
                    isc, isd = _status_color(st)
                    print(f"    {BOLD}{key:<12}{RESET} {isc}{isd} {st:<14}{RESET} {DIM}{created}  {summary}{RESET}")
                print()
        except Exception:
            pass

    # Give user the full action panel so they can use rack map, connections, etc.
    if email and token:
        from cwhelper.tui.actions import _post_detail_prompt  # lazy: avoids circular import
        action = _post_detail_prompt(ctx, email, token)
        # Only propagate "quit"; everything else returns to the caller
        if action == "quit":
            return "quit"
    else:
        input(f"  {DIM}Press ENTER to return...{RESET}")

    return None



def _handle_rack_neighbors(ctx: dict, email: str, token: str) -> str | None:
    """Handle the [w] Rack Neighbors flow.

    Returns a navigation action string, or None to stay in the action panel.
    """
    netbox = ctx.get("netbox", {})
    rack_id = netbox.get("rack_id")
    if not rack_id:
        print(f"\n  {DIM}No rack info available.{RESET}")
        return None

    rack_name = netbox.get("rack") or f"rack {rack_id}"
    current_device = netbox.get("device_name")

    print(f"\n  {DIM}Loading devices in {rack_name}...{RESET}")
    devices = _netbox_get_rack_devices(rack_id)

    if not devices:
        print(f"\n  {DIM}No devices found in {rack_name}.{RESET}")
        return None

    chosen = _print_rack_neighbors(devices, current_device)
    if not chosen:
        return None

    # Build search identifier: prefer serial, fall back to device name
    return _open_device(chosen, email, token)



def _handle_rack_view(ctx: dict, email: str, token: str) -> str | None:
    """Combined rack elevation + neighbor selection + NetBox link.

    Shows the visual rack elevation, then a numbered device list below,
    followed by adjacent-rack panels with L/R navigation.
    Returns a navigation action string, or None to stay in the action panel.
    """
    # Allow re-centering on neighbor racks via < / >
    current_ctx = ctx
    while True:
        netbox = current_ctx.get("netbox", {})
        rack_id = netbox.get("rack_id")
        if not rack_id:
            print(f"\n  {DIM}No rack info available.{RESET}")
            return None

        current_device = netbox.get("device_name")

        # Draw the visual elevation (also fetches and returns devices)
        devices = _draw_rack_elevation(current_ctx)
        if devices:
            print(f"\n  {BOLD}{len(devices)} devices{RESET} {DIM}loaded{RESET}")
        else:
            # CDU / empty rack — show minimal header, continue to neighbors
            rack_label = netbox.get("rack") or "?"
            print(f"\n  {DIM}R{rack_label} — no devices in NetBox (CDU / sidecar){RESET}")

        # --- Fetch and show adjacent racks ---
        neighbor_map = {"L": [], "R": []}
        neighbor_data = None
        rack_loc = current_ctx.get("rack_location", "")
        parsed = _parse_rack_location(rack_loc)
        # Fallback: derive rack number from NetBox rack name (e.g. "64")
        if not parsed:
            rname = netbox.get("rack", "")
            if rname and rname.isdigit():
                site = current_ctx.get("site") or ""
                parsed = {"site_code": site.split(".")[0] if "." in site else site,
                          "dh": "DH1", "rack": int(rname), "ru": None}

        if parsed:
            site_code = parsed["site_code"]
            dh = parsed["dh"]
            layout = _get_dh_layout(site_code, dh)
            _supported = [s.strip().upper() for s in os.environ.get("SUPPORTED_SITES", "").split(",") if s.strip()]
            if layout is None and dh.upper() == "DH1" and _supported and any(
                s in site_code.upper() for s in _supported
            ):
                layout = {
                    "racks_per_row": 10,
                    "columns": [
                        {"label": "Left",  "start": 1,   "num_rows": 14},
                        {"label": "Right", "start": 141,  "num_rows": 17},
                    ],
                    "serpentine": True,
                    "entrance": "bottom-right",
                }
            if layout:
                site_slug = netbox.get("site_slug")
                if not site_slug and parsed.get("site_code"):
                    site_slug = parsed["site_code"].lower()
                try:
                    neighbor_data = _fetch_neighbor_devices(
                        parsed["rack"], layout, site_slug)
                    neighbor_map = _draw_neighbor_panel(neighbor_data)
                except Exception:
                    pass  # graceful degradation — skip neighbors

        # --- Enhanced prompt ---
        has_left = bool(neighbor_map.get("L"))
        has_right = bool(neighbor_map.get("R"))

        hint_parts = []
        if devices:
            hint_parts.append(_btn("d", "Pick Device", CYAN))
        if has_left:
            hint_parts.append(_btn(f"L1\u2011L{len(neighbor_map['L'])}", "Left", YELLOW))
        if has_right:
            hint_parts.append(_btn(f"R1\u2011R{len(neighbor_map['R'])}", "Right", YELLOW))
        if neighbor_data and neighbor_data.get("left"):
            hint_parts.append(_btn("<", "Move Left", MAGENTA))
        if neighbor_data and neighbor_data.get("right"):
            hint_parts.append(_btn(">", "Move Right", MAGENTA))
        hint_parts.append(_btn("#", "Go to Cab", DIM))
        hint_parts.append(_btn("x", "NetBox", GREEN))
        hint_parts.append(_btn("\u21b5", "Back", DIM))

        line = "\u2500" * 50
        prompt_text = f"\n  {DIM}{line}{RESET}\n\n  {'   '.join(hint_parts)}\n\n  > "

        for _ in range(3):
            try:
                raw = input(prompt_text).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return None

            if raw.lower() in ("q", "quit", "exit", "b", "back", ""):
                return None

            # Device picker — show full list on clean screen
            if raw.lower() == "d" and devices:
                _clear_screen()
                rack_label = netbox.get("rack") or "?"
                print(f"\n  {BOLD}R{rack_label} — Pick a device{RESET}  {DIM}({len(devices)} devices){RESET}\n")
                for i, dev in enumerate(devices, 1):
                    dname = dev.get("name") or dev.get("display") or "?"
                    short = _short_device_name(dname)
                    pos = dev.get("position")
                    pos_str = f"U{int(pos):<3}" if pos else "U?  "
                    status_label = (dev.get("status") or {}).get("label") or "?"
                    sc, sd = _status_color(status_label)
                    is_current = current_device and dname == current_device
                    marker = f"  {YELLOW}<-- you{RESET}" if is_current else ""
                    name_fmt = f"{CYAN}{BOLD}{short}{RESET}" if is_current else short
                    print(
                        f"    {BOLD}{i:>2}.{RESET}  {DIM}{pos_str}{RESET} "
                        f"{name_fmt:<18}{sc}{sd} {status_label}{RESET}{marker}"
                    )
                print()
                try:
                    pick = input(f"  {DIM}Pick 1-{len(devices)}, or ENTER to go back:{RESET} ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    return None
                if not pick:
                    _clear_screen()
                    break  # re-render the rack view
                try:
                    idx = int(pick)
                    if 1 <= idx <= len(devices):
                        return _open_device(devices[idx - 1], email, token)
                except ValueError:
                    pass
                _clear_screen()
                break  # re-render the rack view

            # NetBox shortcut
            if raw.lower() == "x":
                api_base = os.environ.get("NETBOX_API_URL", "").strip().rstrip("/")
                nb_base = api_base.rsplit("/api", 1)[0] if "/api" in api_base else api_base
                url = f"{nb_base}/dcim/racks/{rack_id}/"
                print(f"  {DIM}Opening {url}{RESET}")
                webbrowser.open(url)
                return None

            # Navigation: re-center on neighbor rack
            if raw in ("<", ">"):
                side = "left" if raw == "<" else "right"
                nd = neighbor_data.get(side) if neighbor_data else None
                if not nd or not nd.get("rack_id"):
                    print(f"  {DIM}No {side} neighbor to navigate to.{RESET}")
                    continue
                # Build a lightweight ctx for the neighbor rack
                current_ctx = dict(current_ctx)  # shallow copy
                nb_copy = dict(netbox)
                nb_copy["rack_id"] = nd["rack_id"]
                nb_copy["rack"] = str(nd["rack_num"])
                nb_copy["device_name"] = None  # no highlighted device
                nb_copy["position"] = None
                current_ctx["netbox"] = nb_copy
                # Update rack_location to match new rack
                if parsed:
                    rl = f"{parsed['site_code']}.{parsed['dh']}.R{nd['rack_num']}"
                    current_ctx["rack_location"] = rl
                _clear_screen()
                break  # break inner prompt loop → re-enter outer while loop
            else:
                # Parse selection: L3, R1, or plain number
                chosen = None
                raw_upper = raw.upper()
                if raw_upper.startswith("L") and raw_upper[1:].isdigit():
                    idx = int(raw_upper[1:])
                    devs = neighbor_map.get("L", [])
                    if 1 <= idx <= len(devs):
                        chosen = devs[idx - 1]
                    else:
                        print(f"  Out of range. Left rack has {len(devs)} devices.")
                        continue
                elif raw_upper.startswith("R") and raw_upper[1:].isdigit():
                    idx = int(raw_upper[1:])
                    devs = neighbor_map.get("R", [])
                    if 1 <= idx <= len(devs):
                        chosen = devs[idx - 1]
                    else:
                        print(f"  Out of range. Right rack has {len(devs)} devices.")
                        continue
                elif raw.isdigit():
                    # Jump to any cab by number
                    target_rack = int(raw)
                    site_slug = netbox.get("site_slug")
                    if not site_slug and parsed and parsed.get("site_code"):
                        site_slug = parsed["site_code"].lower()
                    rack_obj = _netbox_find_rack_by_name(str(target_rack), site_slug)
                    if rack_obj and rack_obj.get("id"):
                        current_ctx = dict(current_ctx)
                        nb_copy = dict(netbox)
                        nb_copy["rack_id"] = rack_obj["id"]
                        nb_copy["rack"] = rack_obj.get("name") or str(target_rack)
                        nb_copy["device_name"] = None
                        nb_copy["position"] = None
                        current_ctx["netbox"] = nb_copy
                        if parsed:
                            current_ctx["rack_location"] = f"{parsed['site_code']}.{parsed['dh']}.R{target_rack}"
                        _clear_screen()
                        break  # re-enter outer while loop with new rack
                    else:
                        print(f"  {DIM}Rack {target_rack} not found in NetBox.{RESET}")
                        continue
                else:
                    print(f"  {DIM}Try d, L#, R#, <, >, cab#, or ENTER.{RESET}")
                    continue

                if chosen:
                    return _open_device(chosen, email, token)
                return None
        else:
            # Exhausted prompt retries
            return None



def _draw_mini_dh_map(rack_loc: str):
    """Draw a miniature data hall map with per-dash rack display and walking route.

    Uses saved DH layout config when available, falls back to built-in DH1
    layout for sites listed in SUPPORTED_SITES env var.  For unknown data
    halls, offers to run the setup wizard.

    Each dash = 1 cab.  Target rack highlighted in cyan.
    Yellow walking route from entrance (bottom) to target rack.
    Blank line between every 2 rows = walking aisle.
    """
    parsed = _parse_rack_location(rack_loc)
    if not parsed:
        return

    target = parsed["rack"]
    site_code = parsed["site_code"]
    dh = parsed["dh"]
    _supported = [s.strip().upper() for s in os.environ.get("SUPPORTED_SITES", "").split(",") if s.strip()]
    _is_supported_dh1 = dh.upper() == "DH1" and _supported and any(
        s in site_code.upper() for s in _supported
    )

    # --- Resolve layout: saved config > built-in DH1 > offer setup ---
    layout = _get_dh_layout(site_code, dh)

    if layout is None:
        # Built-in fallback for DH1 at supported sites (set via SUPPORTED_SITES env var)
        if _is_supported_dh1:
            layout = {
                "racks_per_row": 10,
                "columns": [
                    {"label": "Left",  "start": 1,   "num_rows": 14},
                    {"label": "Right", "start": 141,  "num_rows": 17},
                ],
                "serpentine": True,
                "entrance": "bottom-right",
            }
        else:
            print(f"\n  {DIM}No data hall map configured for {site_code} {dh}.{RESET}")
            raw = input(f"  Would you like to set one up? [y/{CYAN}n{RESET}]: ").strip().lower()
            if raw == "y":
                layout = _setup_dh_layout(site_code, dh)
            if layout is None:
                return

    cols = layout["columns"]
    default_per_row = layout["racks_per_row"]
    serpentine = layout.get("serpentine", True)
    entrance = layout.get("entrance", "bottom-right")

    # --- Rack-at helper (serpentine or sequential, per-column width) ---
    def rack_at(col_start, row, pos, col_per_row=None):
        pr = col_per_row or default_per_row
        base = col_start + row * pr
        if serpentine and row % 2 == 1:
            return base + (pr - 1 - pos)
        return base + pos

    # Use default per_row for visual rendering width
    per_row = default_per_row
    # Visible width of one column segment: "NNN " + "X " * pr + " NNN"
    vis_width = 4 + (per_row * 2 - 1) + 4  # 27 for 10 racks

    def build_row(col_start, row, col_per_row=None):
        pr = col_per_row or default_per_row
        first_rack = rack_at(col_start, row, 0, pr)
        last_rack = rack_at(col_start, row, pr - 1, pr)
        chars = []
        for pos in range(pr):
            if rack_at(col_start, row, pos, pr) == target:
                chars.append(f"{CYAN}{BOLD}#{RESET}")
            else:
                chars.append(f"{DIM}-{RESET}")
        label_l = f"{DIM}{first_rack:>3}{RESET} "
        label_r = f" {DIM}{last_rack:<3}{RESET}"
        return label_l + " ".join(chars) + label_r

    # --- Find which column and row the target is in ---
    target_col_idx = -1
    target_row = -1
    side = "?"
    for ci, col in enumerate(cols):
        col_pr = col.get("racks_per_row", default_per_row)
        col_end = col["start"] + col["num_rows"] * col_pr - 1
        if col["start"] <= target <= col_end:
            target_col_idx = ci
            target_row = (target - col["start"]) // col_pr
            side = col["label"].upper()
            break

    max_rows = max(c["num_rows"] for c in cols)
    COL_GAP = "       "  # 7 spaces between column pairs (consistent for any # of columns)

    # Walking route only enabled for supported-site DH1 (well-tested 2-column serpentine).
    # Other sites show the map with # marker and row-end labels only.
    has_route = _is_supported_dh1 and target_row >= 0 and len(cols) >= 2

    # --- Compute route gap position for animated rendering ---
    if has_route:
        if target_col_idx < len(cols) - 1:
            route_gap_idx = target_col_idx
        else:
            route_gap_idx = len(cols) - 2
    else:
        route_gap_idx = 0

    # --- Build all display lines ---
    header1 = f"\n  {BOLD}{site_code} {dh}{RESET} {DIM}— Rack R{target}{RESET}"
    hdr_parts = []
    for c in cols:
        end = c["start"] + c["num_rows"] * per_row - 1
        part = f"{c['label']} (R{c['start']}-R{end})"
        hdr_parts.append(f"{part:<{vis_width}}")
    header2 = f"  {DIM}{COL_GAP.join(hdr_parts)}{RESET}"

    body = []  # list of dicts: {plain, on_path, is_turn}
    for row in range(max_rows):
        col_strs = []
        for ci, col in enumerate(cols):
            if row < col["num_rows"]:
                col_strs.append(build_row(col["start"], row, col.get("racks_per_row")))
            else:
                col_strs.append(" " * vis_width)

        on_path = target_row >= 0 and row >= target_row
        is_turn = target_row >= 0 and row == target_row

        # Labels are now embedded in build_row — just join columns
        plain = f"  {col_strs[0]}"
        for ci in range(1, len(cols)):
            plain += COL_GAP + col_strs[ci]

        body.append({"plain": plain, "on_path": on_path, "is_turn": is_turn})

        # Aisle line
        if row % 2 == 1 and row < max_rows - 1:
            body.append({"plain": "", "on_path": on_path, "is_turn": False})

    # Entrance line — spans from the route corridor gap all the way to the right edge
    entrance_line = ""
    if has_route:
        # The corridor is in the gap after column route_gap_idx
        # Entrance spans from that gap to the right edge of the last column
        gap_char_start = (route_gap_idx + 1) * vis_width + route_gap_idx * len(COL_GAP)
        total_width = len(cols) * vis_width + (len(cols) - 1) * len(COL_GAP)
        entrance_width = total_width - gap_char_start
        entrance_line = f"  {' ' * gap_char_start}{YELLOW}{BOLD}{'=' * entrance_width}{RESET}"

    if has_route:
        footer = [
            "",
            "",
            f"  {CYAN}{BOLD}#{RESET} = R{target} ({side} column)  {YELLOW}{BOLD}==={RESET} walking route",
            "",
        ]
    else:
        footer = [
            "",
            "",
            f"  {CYAN}{BOLD}#{RESET} = R{target} ({side} column)",
            "",
        ]

    # --- Render (animated if terminal, static if piped) ---
    animate = sys.stdout.isatty() and has_route

    if not animate:
        # Static: use pre-built body lines (includes row-end labels, no route for non-DH1)
        print(header1)
        print(header2)
        print()
        for bl in body:
            print(bl["plain"])
        if entrance_line:
            print(entrance_line)
        for fl in footer:
            print(fl)
        return

    # ── Animated render ──────────────────────────────────────────────
    ROW_DELAY = 0.015 if _ANIMATE else 0    # map rows appear top→bottom
    ROUTE_DELAY = 0.02 if _ANIMATE else 0  # route traces bottom→top

    # ANSI column positions (1-indexed) — computed dynamically for any # of columns
    # The route corridor runs through the gap at route_gap_idx.
    # Gap i starts at: indent(2) + (i+1)*vis_width + i*len(COL_GAP) + 1
    gap_start = 2 + (route_gap_idx + 1) * vis_width + route_gap_idx * len(COL_GAP) + 1
    gap_w = len(COL_GAP)
    corridor_col = gap_start + gap_w // 2  # middle of the gap

    # Phase 1 — map loads (no route)
    print(header1)
    time.sleep(0.08)
    print(header2)
    print()
    time.sleep(0.05)

    for bl in body:
        print(bl["plain"])
        sys.stdout.flush()
        time.sleep(ROW_DELAY)

    # Phase 2 — route traces from entrance upward
    if _ANIMATE:
        time.sleep(0.15)
    print(entrance_line)
    sys.stdout.flush()
    time.sleep(0.15)

    # Cursor is now 1 line below entrance.
    # body[i] is (len(body) - i + 1) lines up from cursor.
    n = len(body)
    half_gap = gap_w // 2
    for i in range(n - 1, -1, -1):
        bl = body[i]
        if not bl["on_path"]:
            continue
        lines_up = n - i + 1  # +1 for entrance line

        if bl["is_turn"]:
            # Paint the turn marker in the route gap
            sys.stdout.write(f"\033[{lines_up}A\033[{gap_start}G")
            if target_col_idx <= route_gap_idx:
                # Target is left of the gap — turn left
                sys.stdout.write(f"{YELLOW}{BOLD}{'=' * half_gap}+{RESET}{' ' * half_gap}")
            else:
                # Target is right of the gap — turn right
                sys.stdout.write(f"{' ' * half_gap}{YELLOW}{BOLD}+{'=' * half_gap}{RESET}")
            sys.stdout.write(f"\033[{lines_up}B\r")
        else:
            # Paint corridor |
            sys.stdout.write(f"\033[{lines_up}A\033[{corridor_col}G")
            sys.stdout.write(f"{YELLOW}|{RESET}")
            sys.stdout.write(f"\033[{lines_up}B\r")

        sys.stdout.flush()
        time.sleep(ROUTE_DELAY)

    # Footer
    for fl in footer:
        print(fl)



def _draw_connection_map(rack_loc: str, peer_racks: set | list = (),
                        source_label: str = "", peer_label: str = ""):
    """Draw a DH map highlighting source rack AND peer rack(s).

    Like ``_draw_mini_dh_map`` but designed for connection tracing:
    source rack shown in cyan, peer racks in green with pulsing dots.
    No walking route — just the two-color highlight so the DCT can see
    exactly where both ends of a connection are on the floor.

    *peer_racks* is a set/list of rack numbers (ints) to highlight.
    """
    parsed = _parse_rack_location(rack_loc)
    if not parsed:
        return

    target = parsed["rack"]
    site_code = parsed["site_code"]
    dh = parsed["dh"]
    _supported = [s.strip().upper() for s in os.environ.get("SUPPORTED_SITES", "").split(",") if s.strip()]
    _is_supported_dh1 = dh.upper() == "DH1" and _supported and any(
        s in site_code.upper() for s in _supported
    )

    layout = _get_dh_layout(site_code, dh)
    if layout is None and _is_supported_dh1:
        layout = {
            "racks_per_row": 10,
            "columns": [
                {"label": "Left",  "start": 1,   "num_rows": 14},
                {"label": "Right", "start": 141,  "num_rows": 17},
            ],
            "serpentine": True,
            "entrance": "bottom-right",
        }
    if layout is None:
        print(f"  {DIM}No DH map for {site_code} {dh}.{RESET}")
        return

    peer_set = set(int(r) for r in peer_racks) if peer_racks else set()
    cols = layout["columns"]
    default_per_row = layout["racks_per_row"]
    serpentine = layout.get("serpentine", True)

    def rack_at(col_start, row, pos, col_per_row=None):
        pr = col_per_row or default_per_row
        base = col_start + row * pr
        if serpentine and row % 2 == 1:
            return base + (pr - 1 - pos)
        return base + pos

    per_row = default_per_row

    def build_row(col_start, row, col_per_row=None):
        pr = col_per_row or default_per_row
        chars = []
        for pos in range(pr):
            rn = rack_at(col_start, row, pos, pr)
            if rn == target:
                chars.append(f"{CYAN}{BOLD}@{RESET}")
            elif rn in peer_set:
                chars.append(f"{GREEN}{BOLD}#{RESET}")
            else:
                chars.append(f"{DIM}-{RESET}")
        return "".join(chars)

    # Find source column/side
    side = "?"
    for col in cols:
        col_pr = col.get("racks_per_row", default_per_row)
        col_end = col["start"] + col["num_rows"] * col_pr - 1
        if col["start"] <= target <= col_end:
            side = col["label"]
            break

    # Find peer sides
    peer_sides = set()
    for pr in peer_set:
        for col in cols:
            col_pr = col.get("racks_per_row", default_per_row)
            col_end = col["start"] + col["num_rows"] * col_pr - 1
            if col["start"] <= pr <= col_end:
                peer_sides.add(col["label"])

    max_rows = max(c["num_rows"] for c in cols)
    COL_GAP = "       "

    # Header
    src_lbl = source_label or f"R{target}"
    peer_nums = ", ".join(f"R{r}" for r in sorted(peer_set))
    peer_lbl = peer_label or peer_nums

    print(f"\n  {BOLD}Connection Map{RESET}  {DIM}{site_code} {dh}{RESET}")
    print(f"  {CYAN}{BOLD}@{RESET} = {src_lbl} ({side})")
    if peer_set:
        print(f"  {GREEN}{BOLD}#{RESET} = {peer_lbl} ({', '.join(sorted(peer_sides))})")
    print()

    # Column headers
    hdr_parts = []
    for c in cols:
        end = c["start"] + c["num_rows"] * (c.get("racks_per_row") or per_row) - 1
        hdr_parts.append(f"{c['label']} (R{c['start']}-R{end})")
    print(f"  {DIM}{COL_GAP.join(hdr_parts)}{RESET}")
    print()

    # Body
    for row in range(max_rows):
        col_strs = []
        for ci, col in enumerate(cols):
            if row < col["num_rows"]:
                col_strs.append(build_row(col["start"], row, col.get("racks_per_row")))
            else:
                col_strs.append(" " * per_row)

        # Row label (first rack of first visible column)
        first_col = None
        for ci in range(len(cols)):
            if row < cols[ci]["num_rows"]:
                first_col = cols[ci]
                break
        if first_col:
            fc_pr = first_col.get("racks_per_row") or default_per_row
            start_rack = rack_at(first_col["start"], row, 0, fc_pr)
            line = f"  {DIM}R{start_rack:<4}{RESET} {col_strs[0]}"
        else:
            line = f"        {col_strs[0]}"
        for ci in range(1, len(cols)):
            line += COL_GAP + col_strs[ci]

        # Right label
        last_col = None
        for ci in range(len(cols) - 1, -1, -1):
            if row < cols[ci]["num_rows"]:
                last_col = cols[ci]
                break
        if last_col:
            lc_pr = last_col.get("racks_per_row", default_per_row)
            end_rack = rack_at(last_col["start"], row, lc_pr - 1, lc_pr)
            line += f"  {DIM}R{end_rack}{RESET}"

        print(line)

        # Aisle
        if row % 2 == 1 and row < max_rows - 1:
            print()

    print()


def _draw_connection_map_image(rack_loc: str, peer_racks: set | list = (),
                               source_label: str = "", peer_label: str = "",
                               port_info: dict | None = None):
    """Generate and display a PNG floor plan with connection lines.

    If *port_info* is provided, an elevation panel for the source rack is
    drawn on the right showing devices at their RU positions, with the
    selected port highlighted.

    Requires Pillow.  Falls back silently if not installed.
    Displays inline in iTerm2; saves to temp file otherwise.
    """
    if not _HAS_PILLOW or not _VISUAL_MAPS:
        return

    from PIL import Image, ImageDraw, ImageFont
    import base64
    import io
    import tempfile

    parsed = _parse_rack_location(rack_loc)
    if not parsed:
        return

    target = parsed["rack"]
    site_code = parsed["site_code"]
    dh = parsed["dh"]

    layout = _get_dh_layout(site_code, dh)
    if layout is None:
        _supported = [s.strip().upper() for s in os.environ.get("SUPPORTED_SITES", "").split(",") if s.strip()]
        _is_supported = dh.upper() == "DH1" and _supported and any(
            s in site_code.upper() for s in _supported)
        if _is_supported:
            layout = {
                "racks_per_row": 10,
                "columns": [
                    {"label": "Left",  "start": 1,   "num_rows": 14},
                    {"label": "Right", "start": 141,  "num_rows": 17},
                ],
                "serpentine": True,
            }
        else:
            return

    peer_set = set(int(r) for r in peer_racks) if peer_racks else set()
    cols = layout["columns"]
    default_pr = layout["racks_per_row"]
    serpentine = layout.get("serpentine", True)

    # --- Sizing constants ---
    CELL_W, CELL_H = 54, 42
    CELL_GAP = 6
    ROW_PITCH = CELL_H + CELL_GAP
    AISLE_EXTRA = 30
    COL_GAP_PX = 200
    MARGIN_L, MARGIN_T = 110, 120
    MARGIN_B = 110

    # --- Compute column pixel offsets ---
    col_offsets = []
    x_cursor = 0
    for ci, col in enumerate(cols):
        col_offsets.append(x_cursor)
        pr = col.get("racks_per_row", default_pr)
        x_cursor += pr * (CELL_W + CELL_GAP) + COL_GAP_PX

    max_rows = max(c["num_rows"] for c in cols)
    img_w = MARGIN_L + x_cursor - COL_GAP_PX + 50
    img_h = MARGIN_T + max_rows * ROW_PITCH + (max_rows // 2) * AISLE_EXTRA + MARGIN_B

    # --- Helper: rack_at ---
    def rack_at(col_start, row, pos, col_per_row=None):
        pr = col_per_row or default_pr
        base = col_start + row * pr
        if serpentine and row % 2 == 1:
            return base + (pr - 1 - pos)
        return base + pos

    # --- Helper: rack number → pixel center ---
    def rack_to_px(rack_num):
        for ci, col in enumerate(cols):
            pr = col.get("racks_per_row", default_pr)
            col_end = col["start"] + col["num_rows"] * pr - 1
            if col["start"] <= rack_num <= col_end:
                offset = rack_num - col["start"]
                row = offset // pr
                pos = offset % pr
                if serpentine and row % 2 == 1:
                    pos = pr - 1 - pos
                px = MARGIN_L + col_offsets[ci] + pos * (CELL_W + CELL_GAP)
                py = MARGIN_T + row * ROW_PITCH + (row // 2) * AISLE_EXTRA
                return (px + CELL_W // 2, py + CELL_H // 2)
        return None

    # --- Colors (CRT theme) ---
    BG       = (12, 13, 18)
    RACK_DIM = (40, 42, 54)
    SRC_CLR  = (86, 182, 194)    # cyan
    PEER_CLR = (152, 195, 121)   # green
    LINE_CLR = (212, 148, 58)    # amber
    LABEL_CLR = (107, 109, 128)  # dim
    TEXT_CLR  = (200, 201, 212)

    # --- Create image ---
    img = Image.new("RGB", (img_w, img_h), BG)
    draw = ImageDraw.Draw(img)

    try:
        font = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 24)
        font_sm = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 18)
    except (OSError, IOError):
        font = ImageFont.load_default()
        font_sm = font

    # --- Header ---
    header = f"{site_code} {dh} — Connection Map"
    draw.text((MARGIN_L, 30), header, fill=TEXT_CLR, font=font)

    # --- Draw column labels ---
    for ci, col in enumerate(cols):
        pr = col.get("racks_per_row", default_pr)
        end = col["start"] + col["num_rows"] * pr - 1
        lbl = f"{col['label']} (R{col['start']}–R{end})"
        lx = MARGIN_L + col_offsets[ci]
        draw.text((lx, MARGIN_T - 32), lbl, fill=LABEL_CLR, font=font_sm)

    # --- Draw all racks ---
    for ci, col in enumerate(cols):
        pr = col.get("racks_per_row", default_pr)
        for row in range(col["num_rows"]):
            for pos in range(pr):
                rn = rack_at(col["start"], row, pos, pr)
                px = MARGIN_L + col_offsets[ci] + pos * (CELL_W + CELL_GAP)
                py = MARGIN_T + row * ROW_PITCH + (row // 2) * AISLE_EXTRA

                if rn == target:
                    color = SRC_CLR
                elif rn in peer_set:
                    color = PEER_CLR
                else:
                    color = RACK_DIM

                draw.rectangle([px, py, px + CELL_W, py + CELL_H], fill=color)

            # Row label on first column
            if ci == 0:
                start_rack = rack_at(col["start"], row, 0, pr)
                ly = MARGIN_T + row * ROW_PITCH + (row // 2) * AISLE_EXTRA + 1
                draw.text((6, ly), f"R{start_rack}", fill=LABEL_CLR, font=font_sm)

    # --- Draw connection lines (amber, with glow) ---
    src_px = rack_to_px(target)
    if src_px:
        for pr_num in peer_set:
            dst_px = rack_to_px(pr_num)
            if dst_px:
                # Glow layer
                draw.line([src_px, dst_px], fill=(LINE_CLR[0], LINE_CLR[1], LINE_CLR[2]),
                          width=12)
                # Core line
                draw.line([src_px, dst_px], fill=(255, 200, 100), width=4)

    # --- Legend ---
    ly = img_h - 75
    # Source
    draw.rectangle([MARGIN_L, ly, MARGIN_L + 28, ly + 22], fill=SRC_CLR)
    src_lbl = source_label or f"R{target}"
    draw.text((MARGIN_L + 38, ly), src_lbl, fill=TEXT_CLR, font=font_sm)
    # Peer
    off2 = MARGIN_L + 420
    draw.rectangle([off2, ly, off2 + 28, ly + 22], fill=PEER_CLR)
    plbl = peer_label or ", ".join(f"R{r}" for r in sorted(peer_set))
    draw.text((off2 + 38, ly), plbl, fill=TEXT_CLR, font=font_sm)
    # Line
    off3 = off2 + 460
    draw.line([(off3, ly + 11), (off3 + 50, ly + 11)], fill=LINE_CLR, width=4)
    draw.text((off3 + 60, ly), "connection", fill=LABEL_CLR, font=font_sm)

    # --- Elevation panel (right side) ---
    if port_info and port_info.get("rack_id"):
        try:
            elev_devices = _netbox_get_rack_devices(port_info["rack_id"])
        except Exception:
            elev_devices = []
        if elev_devices:
            dt_heights = _fetch_device_type_heights(elev_devices)

            # Fetch rack height
            try:
                rack_data = _netbox_get(f"/dcim/racks/{port_info['rack_id']}/")
                rack_height = int(rack_data.get("u_height", 42)) if rack_data else 42
            except Exception:
                rack_height = 42

            # Build slot map
            elev_slots = {}
            elev_top_ru = {}
            elev_dev_height = {}
            for dev in elev_devices:
                pos = dev.get("position")
                if not pos:
                    continue
                pos = int(pos)
                dt_id = (dev.get("device_type") or {}).get("id")
                h = int(dt_heights.get(dt_id, 1))
                name = dev.get("name") or dev.get("display") or "?"
                elev_dev_height[name] = h
                elev_top_ru[name] = pos + h - 1
                for u in range(pos, pos + h):
                    elev_slots[u] = dev
            if elev_slots:
                rack_height = max(rack_height, max(elev_slots.keys()))

            # Elevation panel sizing
            ELEV_W = 320
            ELEV_MARGIN = 60
            ELEV_X = img_w + ELEV_MARGIN  # start of elevation panel
            RU_H = max(8, (img_h - MARGIN_T - MARGIN_B - 60) // rack_height)
            ELEV_RACK_W = 180
            ELEV_RACK_X = ELEV_X + 70  # leave room for U labels
            ELEV_TOP = MARGIN_T + 30

            # Widen canvas
            new_w = ELEV_X + ELEV_W + 40
            new_img = Image.new("RGB", (new_w, img_h), BG)
            new_img.paste(img, (0, 0))
            img = new_img
            draw = ImageDraw.Draw(img)

            # Divider line
            div_x = img_w + ELEV_MARGIN // 2
            draw.line([(div_x, MARGIN_T - 10), (div_x, img_h - MARGIN_B + 10)],
                      fill=LABEL_CLR, width=1)

            # Elevation header
            rack_name = port_info.get("rack_name", f"R{target}")
            draw.text((ELEV_X, 30), f"Rack Elevation", fill=TEXT_CLR, font=font)

            # Draw rack frame
            rack_bottom = ELEV_TOP + rack_height * RU_H
            draw.rectangle(
                [ELEV_RACK_X - 2, ELEV_TOP - 2, ELEV_RACK_X + ELEV_RACK_W + 2, rack_bottom + 2],
                outline=LABEL_CLR, width=1)

            # Current device info
            cur_name = port_info.get("device_name", "")
            cur_pos = port_info.get("position")
            cur_pos = int(cur_pos) if cur_pos else None

            # Draw each RU (bottom-up: U1 at bottom)
            for u in range(1, rack_height + 1):
                # Y position: U1 at bottom, U{rack_height} at top
                uy = rack_bottom - u * RU_H
                dev = elev_slots.get(u)

                if dev:
                    name = dev.get("name") or dev.get("display") or "?"
                    is_current = cur_name and name == cur_name
                    is_top = (elev_top_ru.get(name) == u)

                    if is_current:
                        color = SRC_CLR
                    else:
                        color = (55, 58, 72)

                    draw.rectangle(
                        [ELEV_RACK_X, uy, ELEV_RACK_X + ELEV_RACK_W, uy + RU_H - 1],
                        fill=color)

                    # Label on top RU of each device
                    if is_top and RU_H >= 10:
                        short = _short_device_name(name)
                        try:
                            tiny = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", min(11, RU_H - 2))
                        except (OSError, IOError):
                            tiny = font_sm
                        lbl_clr = BG if is_current else LABEL_CLR
                        draw.text((ELEV_RACK_X + 4, uy + 1), short[:18], fill=lbl_clr, font=tiny)
                else:
                    # Empty slot
                    draw.rectangle(
                        [ELEV_RACK_X, uy, ELEV_RACK_X + ELEV_RACK_W, uy + RU_H - 1],
                        fill=(20, 21, 28))

                # U labels every 5U or at device boundaries
                if u % 5 == 0 or u == 1 or u == rack_height:
                    try:
                        tiny = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 10)
                    except (OSError, IOError):
                        tiny = font_sm
                    draw.text((ELEV_RACK_X - 40, uy + 1), f"U{u}", fill=LABEL_CLR, font=tiny)

            # Highlight selected port with arrow
            if cur_pos and port_info.get("port"):
                cur_h = elev_dev_height.get(cur_name, 1)
                # Arrow at midpoint of current device
                mid_u = cur_pos + cur_h // 2
                arrow_y = rack_bottom - mid_u * RU_H + RU_H // 2
                arrow_x = ELEV_RACK_X + ELEV_RACK_W + 8

                # Arrow line
                draw.line([(arrow_x, arrow_y), (arrow_x + 40, arrow_y)],
                          fill=LINE_CLR, width=3)
                # Arrowhead
                draw.polygon([
                    (arrow_x, arrow_y),
                    (arrow_x + 8, arrow_y - 5),
                    (arrow_x + 8, arrow_y + 5),
                ], fill=LINE_CLR)

                # Port label
                port_text = port_info["port"]
                draw.text((arrow_x + 46, arrow_y - 10), port_text,
                          fill=LINE_CLR, font=font_sm)

            # Peer info below elevation
            if port_info.get("peer"):
                info_y = rack_bottom + 16
                port_name = port_info.get("port", "?")
                draw.text((ELEV_X, info_y),
                          f"{port_name} → {port_info['peer']}",
                          fill=TEXT_CLR, font=font_sm)

    # --- Output ---
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    png_bytes = buf.getvalue()

    is_iterm2 = os.environ.get("TERM_PROGRAM", "") == "iTerm.app"
    if is_iterm2:
        b64 = base64.b64encode(png_bytes).decode("ascii")
        osc = f"\033]1337;File=inline=1;size={len(png_bytes)};width=auto;height=auto;preserveAspectRatio=1:{b64}\a"
        sys.stdout.write("\n")
        sys.stdout.write(osc)
        sys.stdout.write("\n")
        sys.stdout.flush()
    else:
        tmp = tempfile.NamedTemporaryFile(
            suffix=".png", prefix="cwhelper_connmap_", delete=False,
            dir=os.environ.get("TMPDIR", "/tmp"))
        tmp.write(png_bytes)
        tmp.close()
        print(f"\n  {DIM}Connection map saved to {tmp.name}{RESET}")


def _fetch_device_type_heights(devices: list) -> dict:
    """Bulk-fetch u_height for all device types in a list of devices.

    Returns {device_type_id: u_height}.  One API call instead of N.
    """
    dt_ids = set()
    for dev in devices:
        dt = dev.get("device_type") or {}
        if dt.get("id"):
            dt_ids.add(dt["id"])
    if not dt_ids:
        return {}
    data = _netbox_get("/dcim/device-types/", params={"id": list(dt_ids), "limit": 50})
    if not data:
        return {}
    return {dt["id"]: dt.get("u_height", 1) for dt in data.get("results", [])}



def _draw_rack_elevation(ctx: dict) -> list:
    """Draw a visual rack elevation showing devices at their RU positions.

    Fetches all devices in the rack from NetBox, resolves u_height per
    device type, and renders a cabinet view with the current device
    highlighted.  Animates top-to-bottom when running in a terminal.

    Returns the list of devices in the rack (for use by the combined
    rack view handler), or an empty list on failure.
    """
    netbox = ctx.get("netbox", {})
    rack_id = netbox.get("rack_id")
    if not rack_id:
        print(f"\n  {DIM}No rack info available from NetBox.{RESET}")
        return []

    rack_name = netbox.get("rack") or f"Rack {rack_id}"
    current_device = netbox.get("device_name")
    current_pos = int(netbox["position"]) if netbox.get("position") else None
    site = ctx.get("site") or ""

    print(f"\n  {DIM}Loading rack elevation...{RESET}")
    devices = _netbox_get_rack_devices(rack_id)
    if not devices:
        print(f"\n  {DIM}No devices found in {rack_name}.{RESET}")
        return []

    # Bulk-fetch device type u_heights
    dt_heights = _fetch_device_type_heights(devices)

    # Build slot map: {ru_number: device}
    slots = {}          # ru -> device dict
    top_ru = {}         # device_name -> highest RU (top of device, where label goes)
    device_height = {}  # device_name -> u_height

    for dev in devices:
        pos = dev.get("position")
        if not pos:
            continue
        pos = int(pos)  # NetBox may return float (e.g. 34.0)
        dt_id = (dev.get("device_type") or {}).get("id")
        height = int(dt_heights.get(dt_id, 1))
        name = dev.get("name") or dev.get("display") or "?"
        device_height[name] = height
        top_ru[name] = pos + height - 1  # label at top of device block
        for u in range(pos, pos + height):
            slots[u] = dev

    # Fetch actual rack height from NetBox
    rack_data = _netbox_get(f"/dcim/racks/{rack_id}/")
    rack_height = int(rack_data.get("u_height", 42)) if rack_data else 42
    # Safety: ensure we at least cover all occupied slots
    if slots:
        rack_height = max(rack_height, max(slots.keys()))

    # Count stats
    unique_devices = {(d.get("name") or d.get("display") or id(d)) for d in devices if d.get("position")}
    occupied_u = len(slots)

    # --- Rendering ---
    COL_WIDTH = 50  # inner width of the rack frame
    animate = sys.stdout.isatty() and _ANIMATE
    ROW_DELAY = 0.01

    def _device_label(dev, is_first):
        """Build the label for a rack slot."""
        name = dev.get("name") or dev.get("display") or "?"
        short = _short_device_name(name)
        is_current = current_device and name == current_device

        if not is_first:
            # Continuation RU of a multi-U device
            marker = f"{CYAN}┆┆{RESET}" if is_current else f"{DIM}┆{RESET}"
            return marker, is_current

        # Top RU — show short name + role + status
        role = (dev.get("role") or dev.get("device_role") or {}).get("display") or ""
        status_label = (dev.get("status") or {}).get("label") or ""
        role_short = role[:12] if role else ""
        status_short = status_label[:10] if status_label else ""

        if is_current:
            label = f"{CYAN}{BOLD}{short}{RESET}"
            suffix = f"  {DIM}{role_short}  {status_short}{RESET}"
            marker_text = f"{CYAN}{BOLD}>>{RESET}  {label}{suffix}"
        else:
            suffix = f"  {DIM}{role_short}  {status_short}{RESET}"
            marker_text = f"    {short}{suffix}"

        return marker_text, is_current

    # Clear the "loading" message
    sys.stdout.write("\033[A\033[K")
    sys.stdout.flush()

    # Header
    header = f"\n  {BOLD}{rack_name}{RESET}  {DIM}{site}{RESET}  {DIM}{rack_height}U{RESET}"
    top_border = f"  ┌{'─' * COL_WIDTH}┐"
    bottom_border = f"  └{'─' * COL_WIDTH}┘"

    print(header)
    if animate:
        time.sleep(0.08)
    print(top_border)
    if animate:
        time.sleep(0.03)

    # Track lines for animation (highlighting current device after draw)
    body_lines = []

    # Draw rows top-to-bottom
    u = rack_height
    while u >= 1:
        dev = slots.get(u)
        u_label = f"U{u:<3}"

        if dev:
            name = dev.get("name") or dev.get("display") or "?"
            is_top = (top_ru.get(name) == u) if name != "?" else True
            label_text, is_current = _device_label(dev, is_top)

            if is_current:
                line = f"  │ {CYAN}{u_label}{RESET} {label_text}"
            else:
                line = f"  │ {DIM}{u_label}{RESET} {label_text}"
        else:
            # Empty slot — check for runs of empties to compress
            empty_start = u
            while u - 1 >= 1 and u - 1 not in slots:
                u -= 1
            empty_end = u

            if empty_start - empty_end >= 3:
                # Compress large empty runs
                line = f"  │ {DIM}{u_label}  ...  (empty U{empty_start}-U{empty_end}){RESET}"
            elif empty_start == empty_end:
                line = f"  │ {DIM}{u_label}{RESET}"
            else:
                # Small gap: print individually
                for uu in range(empty_start, empty_end, -1):
                    body_lines.append(f"  │ {DIM}U{uu:<3}{RESET}")
                line = f"  │ {DIM}U{empty_end:<3}{RESET}"

        body_lines.append(line)
        u -= 1

    # Print body lines
    for bl in body_lines:
        print(bl)
        if animate:
            sys.stdout.flush()
            time.sleep(ROW_DELAY)

    print(bottom_border)

    # Footer
    print(f"\n  {len(unique_devices)} devices  {DIM}│{RESET}  {occupied_u}/{rack_height}U occupied")
    if current_device and current_pos:
        h = device_height.get(current_device, 1)
        top_u = current_pos + h - 1
        pos_range = f"U{current_pos}-U{top_u}" if h > 1 else f"U{current_pos}"
        print(f"  {CYAN}{BOLD}>>{RESET} = {CYAN}{current_device}{RESET} at {pos_range}")
    print()

    return devices


