"""NetBox API client — device, rack, and interface queries."""
from __future__ import annotations

import os
import re

import requests
from cwhelper import config as _cfg
from cwhelper.config import *  # noqa: F401,F403
from cwhelper.cache import _cache_put, _classify_port_role
from concurrent.futures import as_completed
__all__ = ['_netbox_available', '_netbox_get', '_netbox_find_device', '_netbox_get_interfaces', '_netbox_trace_interface', '_netbox_get_rack_devices', '_netbox_find_rack_by_name', '_fetch_neighbor_devices', '_parse_iface_speed', '_build_netbox_context']
# NOTE: _parse_rack_location imported late inside _build_netbox_context to avoid circular import




def _netbox_available() -> bool:
    """Check if NetBox env vars are configured."""
    return bool(os.environ.get("NETBOX_API_URL", "").strip()
                and os.environ.get("NETBOX_API_TOKEN", "").strip())



def _netbox_get(path: str, params: dict = None) -> dict | None:
    """Make an authenticated GET to NetBox. Returns JSON or None on error."""
    base = os.environ.get("NETBOX_API_URL", "").strip().rstrip("/")
    token = os.environ.get("NETBOX_API_TOKEN", "").strip()
    if not base or not token:
        return None

    url = f"{base}{path}"
    try:
        response = _cfg._session.get(
            url,
            headers={
                "Authorization": f"Token {token}",
                "Accept": "application/json",
                "User-Agent": "cw-node-helper/v0.5",
            },
            params=params,
            timeout=(5, 10),
        )
        if response.ok:
            return response.json()
    except requests.RequestException:
        pass
    return None



def _netbox_find_device(serial: str | None = None,
                        name: str | None = None) -> dict | None:
    """Find a device in NetBox by serial number or name.

    Fires both lookups in parallel when both are provided.
    Serial result takes priority. Returns the first matching device dict,
    or None if not found / NetBox not configured.
    """
    if not _netbox_available():
        return None

    searches = []
    if serial:
        searches.append(("serial", {"serial": serial}))
    if name:
        searches.append(("name", {"name": name}))

    if not searches:
        return None

    # Single search — no thread overhead
    if len(searches) == 1:
        data = _netbox_get("/dcim/devices/", params=searches[0][1])
        if data and data.get("results"):
            return data["results"][0]
        return None

    # Parallel: fire both, prefer serial result
    results = {}
    future_map = {
        _cfg._executor.submit(_netbox_get, "/dcim/devices/", params=params): label
        for label, params in searches
    }
    for future in as_completed(future_map):
        label = future_map[future]
        try:
            data = future.result()
            if data and data.get("results"):
                results[label] = data["results"][0]
        except Exception:
            pass

    return results.get("serial") or results.get("name")



def _netbox_get_interfaces(device_id: int) -> list:
    """Get interfaces for a device by its NetBox device ID."""
    data = _netbox_get("/dcim/interfaces/", params={
        "device_id": device_id,
        "limit": 100,
    })
    if not data:
        return []
    return data.get("results", [])



def _netbox_trace_interface(iface_id: int) -> list:
    """Trace a cable path from an interface through all hops.

    Calls /api/dcim/interfaces/{id}/trace/ and returns a list of hops.
    Each hop is a list of [near_end, cable, far_end] segments.
    Returns empty list on error.
    """
    data = _netbox_get(f"/dcim/interfaces/{iface_id}/trace/")
    if not data or not isinstance(data, list):
        return []
    return data



def _netbox_get_rack_devices(rack_id: int) -> list:
    """Get all devices in a rack by NetBox rack ID.

    Returns devices sorted by position descending (top of rack first).
    """
    data = _netbox_get("/dcim/devices/", params={
        "rack_id": rack_id,
        "limit": 50,
    })
    if not data:
        return []
    devices = data.get("results", [])
    devices.sort(key=lambda d: (d.get("position") is None, -(d.get("position") or 0)))
    return devices



def _netbox_find_rack_by_name(rack_name: str, site_slug: str = None) -> dict | None:
    """Find a rack in NetBox by name, trying zero-padded variants.

    NetBox rack names are often zero-padded (e.g. '064' not '64').
    Tries: exact → 3-digit → 4-digit padding.
    """
    # Build unique candidate names to try
    seen = set()
    candidates = []
    for c in [rack_name, rack_name.zfill(3), rack_name.zfill(4)]:
        if c not in seen:
            seen.add(c)
            candidates.append(c)

    for name in candidates:
        params = {"name": name, "limit": 1}
        if site_slug:
            params["site"] = site_slug
        data = _netbox_get("/dcim/racks/", params=params)
        if data and data.get("results"):
            return data["results"][0]
    return None



def _fetch_neighbor_devices(rack_num: int, layout: dict,
                            site_slug: str = None) -> dict:
    """Fetch devices in physically adjacent racks (parallel NetBox calls).

    Returns {"left": {"rack_num": int, "rack_id": int, "devices": list} | None,
             "right": ...}.
    """
    from concurrent.futures import as_completed
    from cwhelper.services.context import _get_physical_neighbors  # late import (avoid circular)
    neighbors = _get_physical_neighbors(rack_num, layout)
    result = {"left": None, "right": None}

    # Phase 1: look up rack IDs in parallel
    rack_futures = {}
    for side in ("left", "right"):
        n = neighbors.get(side)
        if n is not None:
            fut = _cfg._executor.submit(_netbox_find_rack_by_name, str(n), site_slug)
            rack_futures[fut] = (side, n)

    # Phase 2: for each found rack, fetch its devices in parallel
    device_futures = {}
    for fut in as_completed(rack_futures):
        side, n = rack_futures[fut]
        try:
            rack_data = fut.result(timeout=10)
        except Exception:
            rack_data = None
        if rack_data and rack_data.get("id"):
            rid = rack_data["id"]
            result[side] = {"rack_num": n, "rack_id": rid, "devices": []}
            dfut = _cfg._executor.submit(_netbox_get_rack_devices, rid)
            device_futures[dfut] = side
        else:
            result[side] = {"rack_num": n, "rack_id": None, "devices": []}

    for dfut in as_completed(device_futures):
        side = device_futures[dfut]
        try:
            devs = dfut.result(timeout=10)
        except Exception:
            devs = []
        if result[side]:
            result[side]["devices"] = devs

    return result




def _snipe_url_from_tag(asset_tag: str | None) -> str | None:
    """Convert a Snipe-IT asset tag (e.g. 'm001023') to its hardware URL.

    Only handles m-prefixed tags. S-prefixed tags (e.g. 'S029490') are not supported.
    """
    if not asset_tag or not asset_tag.startswith("m"):
        return None
    numeric = asset_tag[1:].lstrip("0") or "0"
    _snipeit_base = os.environ.get("SNIPEIT_BASE_URL", "https://snipe.example.com")
    return f"{_snipeit_base}/hardware/{numeric}"


def _parse_iface_speed(nb_type) -> str:
    """Parse NetBox interface type to a short speed label (e.g. '100G')."""
    if not nb_type or not isinstance(nb_type, dict):
        return ""
    val = (nb_type.get("value") or "").lower()
    for prefix, label in [
        ("400g", "400G"), ("200g", "200G"), ("100g", "100G"),
        ("40g", "40G"), ("25g", "25G"), ("10g", "10G"), ("1000", "1G"),
    ]:
        if val.startswith(prefix):
            return label
    return ""



def _build_netbox_context(service_tag: str | None,
                          node_name: str | None,
                          hostname: str | None,
                          rack_location: str | None = None,
                          jira_site: str | None = None) -> dict:
    """Query NetBox for device info and interfaces. Returns a dict.

    This is called during context building. If NetBox is not configured
    or the device isn't found, returns an empty dict (no error).
    Results are cached in-memory by lookup args.

    If *jira_site* is provided and the device found by serial/name lives at
    a different site, discard the match and fall through to rack_location
    lookups so the ticket opens the correct NetBox device.
    """
    from cwhelper.services.context import _parse_rack_location, _short_device_name  # late import (avoid circular)
    if not _netbox_available():
        return {}

    # Check NetBox cache
    cache_key = f"{service_tag}|{node_name}|{hostname}|{rack_location}|{jira_site}"
    if cache_key in _cfg._netbox_cache:
        return _cfg._netbox_cache[cache_key]

    device = _netbox_find_device(serial=service_tag, name=node_name or hostname)

    # Site validation: if Jira says a different site, the serial/name matched
    # the wrong device (asset moved, data mismatch). Drop it and try positional.
    if device and jira_site:
        nb_site_obj = device.get("site") or {}
        nb_slug = (nb_site_obj.get("slug") or "").lower().replace("-", "")
        jira_norm = jira_site.lower().replace("-", "").replace("_", "")
        if nb_slug and jira_norm and nb_slug != jira_norm:
            device = None  # discard — fall through to rack_location

    # Fallback: if rack_location looks like a hostname, try it as a device name
    if not device and rack_location and "." not in rack_location and "-" in rack_location:
        device = _netbox_find_device(name=rack_location)
    # Fallback: look up device by rack + RU position
    if not device and rack_location and "." in rack_location:
        parsed_rl = _parse_rack_location(rack_location)
        if parsed_rl and parsed_rl.get("ru"):
            site_slug = parsed_rl["site_code"].lower()
            rack_obj = _netbox_find_rack_by_name(str(parsed_rl["rack"]), site_slug)
            # Try Jira site field as fallback slug
            if not rack_obj:
                # site_slug might be a LoCode; try common alternatives
                # We don't have ctx here, but we can try without site filter
                rack_obj = _netbox_find_rack_by_name(str(parsed_rl["rack"]))
            if rack_obj and rack_obj.get("id"):
                try:
                    ru = int(float(parsed_rl["ru"]))
                    devices_in_rack = _netbox_get_rack_devices(rack_obj["id"])
                    for d in devices_in_rack:
                        if d.get("position") and int(d["position"]) == ru:
                            device = d
                            break
                except (ValueError, TypeError):
                    pass
    if not device:
        _cache_put(_cfg._netbox_cache, cache_key, {}, _NETBOX_CACHE_MAX)
        return {}

    device_id = device.get("id")

    # Extract key fields from the device
    site_obj = device.get("site") or {}
    rack_obj = device.get("rack") or {}
    position = device.get("position")
    primary_ip_obj = device.get("primary_ip") or {}
    primary_ip4_obj = device.get("primary_ip4") or {}
    primary_ip6_obj = device.get("primary_ip6") or {}
    oob_ip_obj = device.get("oob_ip") or {}

    # Manufacturer + model from device_type
    device_type_obj = device.get("device_type") or {}
    manufacturer_obj = device_type_obj.get("manufacturer") or {}

    result = {
        "device_name": device.get("name"),
        "device_id": device_id,
        "serial": device.get("serial"),
        "asset_tag": device.get("asset_tag"),
        "snipe_url": _snipe_url_from_tag(device.get("asset_tag")),
        "site": site_obj.get("display") or site_obj.get("name"),
        "site_slug": site_obj.get("slug"),  # e.g., "us-site-01a" — for Teleport BMC URL
        "rack": rack_obj.get("display") or rack_obj.get("name"),
        "rack_id": rack_obj.get("id"),
        "position": position,
        "primary_ip": primary_ip_obj.get("address"),
        "primary_ip4": primary_ip4_obj.get("address"),
        "primary_ip6": primary_ip6_obj.get("address"),
        "oob_ip": oob_ip_obj.get("address"),
        "status": (device.get("status") or {}).get("label"),
        "device_role": (device.get("role") or device.get("device_role") or {}).get("display"),
        "platform": (device.get("platform") or {}).get("display"),
        "manufacturer": manufacturer_obj.get("display") or manufacturer_obj.get("name"),
        "model": device_type_obj.get("display") or device_type_obj.get("model"),
        "interfaces": [],
    }

    # Fetch interfaces and classify them for DCT readability
    if device_id:
        ifaces = _netbox_get_interfaces(device_id)
        for iface in ifaces:
            cable = iface.get("cable")
            link_peers = iface.get("link_peers") or []
            if not cable or not link_peers:
                continue  # skip uncabled

            full_name = iface.get("display") or iface.get("name") or "?"
            # Strip device prefix from port name (e.g. "device:bmc" → "bmc")
            port_name = full_name.split(":")[-1] if ":" in full_name else full_name

            peer = link_peers[0]
            peer_device = peer.get("device", {})
            peer_name_full = peer_device.get("display") or peer_device.get("name") or "?"
            peer_port = peer.get("display") or peer.get("name") or "?"
            # Strip device prefix from peer port too
            peer_port_short = peer_port.split(":")[-1] if ":" in peer_port else peer_port

            # Short peer name + extract rack from full name
            peer_short = _short_device_name(peer_name_full)
            rack_match = re.search(r"-r(\d{2,4})", peer_name_full.lower())
            peer_rack = f"R{rack_match.group(1).lstrip('0') or '0'}" if rack_match else ""

            # Cable ID for NetBox link
            cable_id = cable.get("id") if isinstance(cable, dict) else None

            role = _classify_port_role(port_name)

            # Parse interface speed from NetBox type field
            speed = _parse_iface_speed(iface.get("type"))

            result["interfaces"].append({
                "name": port_name,
                "role": role,
                "speed": speed,
                "peer_device": peer_short,
                "peer_device_full": peer_name_full,
                "peer_port": peer_port_short,
                "peer_rack": peer_rack,
                "cable_id": cable_id,
                "iface_id": iface.get("id"),
                "connected_to": f"{peer_name_full}:{peer_port}",
            })

    _cache_put(_cfg._netbox_cache, cache_key, result, _NETBOX_CACHE_MAX)
    return result


