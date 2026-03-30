"""Grafana URL builder."""
from __future__ import annotations

import os
import re

from cwhelper import config as _cfg
from cwhelper.config import *  # noqa: F401,F403
__all__ = ['_build_grafana_urls', '_find_psu_dashboard_url']




def _build_grafana_urls(node_name: str | None, hostname: str | None,
                        service_tag: str | None = None,
                        netbox_device: str | None = None,
                        ctx: dict | None = None) -> dict:
    """Build Grafana dashboard URLs with rich parameters.

    Uses all available context to pre-fill Grafana dashboard variables
    for a richer experience (IB neighbors, metrics, etc.).
    """
    search_key = node_name or netbox_device or hostname or service_tag
    if not search_key:
        return {}
    base = os.environ.get("GRAFANA_BASE_URL", "https://grafana.int.example.com")

    # Build rich params from context when available
    params = [f"var-search={search_key}"]
    if ctx:
        device_slot = hostname or netbox_device or ""
        if device_slot:
            params.append(f"var-device_slot={device_slot}")
        if service_tag:
            params.append(f"var-serial={service_tag}")
            # BMN = 's' + serial lowercase
            params.append(f"var-bmn=s{service_tag.lower()}")
        # k8s node name
        if node_name:
            params.append(f"var-node={node_name}")
        nb = ctx.get("netbox") or {}
        # IP addresses
        if nb.get("oob_ip"):
            params.append(f"var-bmc_ip={nb['oob_ip'].split('/')[0]}")
        if nb.get("primary_ip4"):
            params.append(f"var-node_ip={nb['primary_ip4'].split('/')[0]}")
        # Site / region
        site = ctx.get("site") or ""
        if site:
            params.append(f"var-zone={site}")
            region = site.rsplit("-", 1)[0] if "-" in site else site
            params.append(f"var-region={region}")
            # Cluster name
            params.append(f"var-cluster=fleetops-{site.lower()}")
        # Rack / location
        rack_loc = ctx.get("rack_location") or ""
        from cwhelper.services.context import _parse_rack_location  # lazy — avoids circular import
        parsed = _parse_rack_location(rack_loc) if rack_loc and "." in rack_loc else None
        if parsed:
            params.append(f"var-rack={parsed['rack']}")
            params.append(f"var-location={parsed['dh']}")
        # Model
        model = nb.get("model") or ""
        if model:
            params.append(f"var-model={model}")

    params_str = "&".join(params)
    return {
        "node_details": f"{base}/d/{os.environ.get('GRAFANA_NODE_UID', 'node-details-uid')}/node-details?{params_str}",
        "ib_node_search": f"{base}/d/{os.environ.get('GRAFANA_IB_UID', 'ib-search-uid')}/ib-node-search?var-search={search_key}",
    }



def _find_psu_dashboard_url(ctx: dict):
    """Return PSU dashboard URL from diag_links, or None."""
    for dl in ctx.get("diag_links") or []:
        if "psu" in dl.get("label", "").lower() or "psu" in dl.get("url", "").lower():
            return dl["url"]
    return None


