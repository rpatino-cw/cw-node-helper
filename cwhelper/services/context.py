"""Data extraction, parsing, and context building."""
from __future__ import annotations

import datetime
import html as html_mod
import json
import os
import re
import sys
import textwrap
import time

from cwhelper import config as _cfg
from cwhelper.config import *  # noqa: F401,F403
from cwhelper.cache import _lookup_ib_connections
__all__ = ['_format_age', '_parse_jira_timestamp', '_unwrap_field', '_extract_custom_fields', '_extract_linked_issues', '_extract_portal_url', '_extract_description_details', '_extract_psu_info', '_extract_comments', '_adf_to_plain_text', '_render_adf_description', '_parse_rack_location', '_get_physical_neighbors', '_short_device_name', '_build_context', '_fetch_and_show', 'get_node_context']
from cwhelper.clients.jira import _jira_get_issue, _jira_get, _get_credentials, _handle_response_errors
from cwhelper.clients.netbox import _build_netbox_context, _netbox_available, _netbox_find_device
from cwhelper.clients.grafana import _build_grafana_urls
from cwhelper.services.search import _fetch_sla, _search_by_text  # noqa: E402

_ACRONYMS = frozenset({"bmc", "tor", "pdu", "dpu", "nic", "gpu", "cpu", "oob", "mgmt"})
_ANSI_ESCAPE = re.compile(r'\x1b\[[0-9;]*m')


def _plain_len(s: str) -> int:
    """Length of string ignoring ANSI escape sequences."""
    return len(_ANSI_ESCAPE.sub('', s))




def _format_age(seconds: float) -> str:
    """Format seconds into a human-readable age string like '3d 4h' or '12m'."""
    total = int(seconds)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)



def _parse_jira_timestamp(ts: str) -> float:
    """Parse a Jira timestamp string and return seconds since that time."""
    if not ts:
        return 0
    # Strip fractional seconds and timezone for simple parsing
    # Format: "2026-02-02T15:32:00.000-0500" or "2026-02-02T15:32:00.000+0000"
    try:
        # Handle both +HHMM and Z formats
        clean = ts.replace("Z", "+0000")
        if "." in clean:
            base, frac_tz = clean.split(".", 1)
            # Extract timezone offset from end
            for i in range(len(frac_tz) - 1, -1, -1):
                if frac_tz[i] in "+-":
                    tz_str = frac_tz[i:]
                    break
            else:
                tz_str = "+0000"
            dt = datetime.datetime.strptime(f"{base}{tz_str}", "%Y-%m-%dT%H:%M:%S%z")
        else:
            dt = datetime.datetime.strptime(clean, "%Y-%m-%dT%H:%M:%S%z")
        now = datetime.datetime.now(datetime.timezone.utc)
        return max(0, (now - dt).total_seconds())
    except Exception:
        return 0



def _unwrap_field(raw_value):
    """Unwrap single-element lists like ["10NQ724"] -> "10NQ724"."""
    if isinstance(raw_value, list):
        return raw_value[0] if len(raw_value) == 1 else raw_value or None
    return raw_value



def _extract_custom_fields(fields: dict) -> dict:
    """Pull known DCT custom fields out of the Jira fields dict."""
    extracted = {}
    for jira_id, friendly_name in CUSTOM_FIELDS.items():
        extracted[friendly_name] = _unwrap_field(fields.get(jira_id))
    return extracted



def _extract_linked_issues(fields: dict) -> list:
    """Pull linked issue keys and their relationship type from issuelinks."""
    links = []
    for link in fields.get("issuelinks", []):
        link_type = link.get("type", {}).get("name", "Related")
        for direction in ("inwardIssue", "outwardIssue"):
            linked = link.get(direction)
            if linked:
                linked_fields = linked.get("fields", {})
                linked_status = linked_fields.get("status", {})
                links.append({
                    "key": linked.get("key"),
                    "relationship": link_type,
                    "summary": linked_fields.get("summary", ""),
                    "status": linked_status.get("name", "Unknown"),
                })
    return links



def _extract_portal_url(fields: dict) -> str | None:
    """Try to get the Service Desk portal URL from customfield_10010._links.web."""
    req_info = fields.get("customfield_10010")
    if isinstance(req_info, dict):
        return req_info.get("_links", {}).get("web")
    return None



def _extract_description_details(fields: dict) -> dict:
    """Parse the Atlassian Document Format (ADF) description to extract:
      - rma_reason:  text starting with "RMA Reason:"
      - node_name:   text starting with "Node:" or "Node name:"
      - diag_links:  list of {label, url} dicts from any URLs found
    """
    desc = fields.get("description")
    if not desc or not isinstance(desc, dict):
        return {"rma_reason": None, "node_name": None, "diag_links": []}

    rma_reason = None
    node_name = None
    diag_links = []
    desc_rack = None   # rack number parsed from device hostname in description
    desc_dh = None     # data hall (e.g. "DH1") from device hostname
    desc_ru = None     # rack unit from "rack unit N" mention

    def _walk_content(node):
        """Recursively walk ADF nodes and extract text + links."""
        nonlocal rma_reason, node_name, desc_rack, desc_dh, desc_ru

        if not isinstance(node, dict):
            return

        # Text node — check for RMA Reason / Node patterns
        if node.get("type") == "text":
            text = node.get("text", "").strip()

            if text.lower().startswith("rma reason:") and not rma_reason:
                rma_reason = text.split(":", 1)[1].strip()

            if re.match(r"^node\s*(name)?:", text, re.IGNORECASE) and not node_name:
                node_name = re.split(r":\s*", text, maxsplit=1)[1].strip()

            # Extract rack/DH from device hostnames like dh1-r264-node-02-us-central-07a
            if not desc_rack:
                m = re.search(r'\b(dh\w*)-r(\d+)-', text, re.IGNORECASE)
                if m:
                    desc_dh = m.group(1).upper()  # e.g. "DH1"
                    desc_rack = int(m.group(2))    # e.g. 264

            # Extract rack unit from "rack unit N" mentions
            if not desc_ru:
                m = re.search(r'rack\s+unit\s+(\d+)', text, re.IGNORECASE)
                if m:
                    desc_ru = int(m.group(1))

            # Check if this text node has a link mark (URL)
            marks = node.get("marks", [])
            for mark in marks:
                if mark.get("type") == "link":
                    href = mark.get("attrs", {}).get("href", "")
                    if href:
                        href = html_mod.unescape(href)  # Fix &amp; → & etc.
                        # Derive a short label from the URL filename
                        filename = href.rstrip("/").rsplit("/", 1)[-1]
                        diag_links.append({"label": filename, "url": href})

        # Recurse into child content
        for child in node.get("content", []):
            _walk_content(child)

    _walk_content(desc)

    # Deduplicate diag links by URL
    seen = set()
    unique_links = []
    for link in diag_links:
        if link["url"] not in seen:
            seen.add(link["url"])
            unique_links.append(link)

    return {
        "rma_reason": rma_reason,
        "node_name": node_name,
        "diag_links": unique_links,
        "desc_rack": desc_rack,
        "desc_dh": desc_dh,
        "desc_ru": desc_ru,
    }



def _extract_psu_info(description: str) -> dict | None:
    """Parse PSU-specific fields from ticket description text.

    Returns a dict with psu_id, deviceslot, serial, rack_unit, row
    or None if no PSU info found.
    """
    if not description:
        return None
    desc_low = description.lower()
    if "psu" not in desc_low and "power supply" not in desc_low:
        return None

    info = {}

    # PSU ID: "PSU with id 3", "PSU id 3", "PSU #3", "PSU 3"
    # Run findall once — reused below for all_psu_ids
    psu_ids = re.findall(r'psu\s+(?:with\s+)?id\s+(\d+)', description, re.IGNORECASE)
    if not psu_ids:
        psu_ids = re.findall(r'psu\s*#?\s*(\d+)', description, re.IGNORECASE)
    if psu_ids:
        info["psu_id"] = psu_ids[0]

    # Deviceslot: "at deviceslot dh1-r306-node-04-us-central-07a"
    m = re.search(r'(?:deviceslot|at)\s+(dh\d*-r\d+-node-\d+-[\w-]+)', description, re.IGNORECASE)
    if m:
        info["deviceslot"] = m.group(1)

    # Serial: "serial S948338X5830183"
    m = re.search(r'serial\s+(\S+)', description, re.IGNORECASE)
    if m:
        info["serial"] = m.group(1).rstrip(",.")

    # Rack unit: "rack unit 22"
    m = re.search(r'rack\s+unit\s+(\d+)', description, re.IGNORECASE)
    if m:
        info["rack_unit"] = m.group(1)

    # Row: "row 31"
    m = re.search(r'\brow\s+(\d+)', description, re.IGNORECASE)
    if m:
        info["row"] = m.group(1)

    # Count how many PSUs are mentioned (multiple failures) — reuses psu_ids from above
    if psu_ids:
        info["all_psu_ids"] = sorted(set(psu_ids))

    return info if info else None



def _extract_comments(fields: dict, max_comments: int = 3) -> list:
    """Pull the most recent comments from fields.comment.comments.

    Returns a list of dicts: {author, created, body} (most recent first).
    """
    comment_data = fields.get("comment", {})
    comments_raw = comment_data.get("comments", [])

    # Take the last N (most recent)
    recent = comments_raw[-max_comments:]
    recent.reverse()  # newest first

    results = []
    for c in recent:
        author_obj = c.get("author", {})
        author = author_obj.get("displayName", "Unknown")

        # created is like "2024-11-04T14:30:00.000-0500"
        created_raw = c.get("created", "")
        # Trim to just date + time (first 16 chars: "2024-11-04T14:30")
        created = created_raw[:16].replace("T", " ") if created_raw else "?"

        # Extract plain text from ADF body
        body_adf = c.get("body", {})
        body_text = _adf_to_plain_text(body_adf)
        # Trim to first ~120 chars for display
        _MAX = 120
        if len(body_text) > _MAX:
            body_text = body_text[:_MAX - 3] + "..."

        results.append({"author": author, "created": created, "body": body_text})

    return results



def _adf_to_plain_text(node: dict) -> str:
    """Recursively extract plain text from an ADF document node."""
    if not isinstance(node, dict):
        return ""

    if node.get("type") == "text":
        return node.get("text", "")

    parts = []
    for child in node.get("content", []):
        parts.append(_adf_to_plain_text(child))

    return " ".join(parts).strip()



def _render_adf_description(node: dict, indent: str = "    ") -> tuple[list[str], list[dict]]:
    """Render an ADF document into formatted terminal lines.

    Preserves paragraph breaks, renders headings in bold, handles
    bullet/ordered lists, decodes HTML entities, and word-wraps.
    Returns (lines, links) where lines is a list of ready-to-print
    strings and links is a list of {label, url} dicts.
    """
    if not isinstance(node, dict):
        return [], []

    lines: list[str] = []
    links: list[dict] = []
    seen_urls: set[str] = set()
    width = 66  # wrap width (fits nicely with 4-char indent in ~80-col terminal)

    def _inline_text(n: dict) -> str:
        """Extract inline text from a node, applying bold/link marks."""
        if not isinstance(n, dict):
            return ""
        if n.get("type") == "text":
            raw = n.get("text", "")
            raw = html_mod.unescape(raw)
            marks = n.get("marks", [])
            for m in marks:
                if m.get("type") == "strong":
                    raw = f"{BOLD}{raw}{RESET}"
                elif m.get("type") == "link":
                    href = m.get("attrs", {}).get("href", "")
                    if href and href not in seen_urls:
                        seen_urls.add(href)
                        links.append({"label": raw, "url": href})
                    raw = f"{CYAN}{UNDERLINE}{raw}{RESET}"
            return raw
        if n.get("type") == "hardBreak":
            return "\n"
        parts = []
        for child in n.get("content", []):
            parts.append(_inline_text(child))
        return "".join(parts)

    def _wrap_text(text: str, prefix: str = indent) -> list[str]:
        """Word-wrap text while being aware of ANSI codes."""
        result = []
        for raw_line in text.split("\n"):
            if not raw_line.strip():
                result.append("")
                continue
            # Simple wrap: split on spaces, accumulate
            words = raw_line.split(" ")
            cur = prefix
            cur_plain_len = len(prefix)
            for w in words:
                wlen = _plain_len(w)
                if cur_plain_len + wlen + 1 > width + len(prefix) and cur != prefix:
                    result.append(cur)
                    cur = prefix + w
                    cur_plain_len = len(prefix) + wlen
                else:
                    if cur == prefix:
                        cur += w
                    else:
                        cur += " " + w
                    cur_plain_len += wlen + (0 if cur == prefix + w else 1)
            if cur.strip():
                result.append(cur)
        return result

    def _dim_wrap(text: str, prefix: str = indent) -> list[str]:
        """Wrap text and apply DIM to each line for consistent color."""
        wrapped = _wrap_text(text, prefix)
        return [f"{DIM}{ln}{RESET}" for ln in wrapped]

    def _walk_block(block: dict):
        """Process a top-level ADF block node."""
        btype = block.get("type", "")

        if btype == "heading":
            text = _inline_text(block)
            text = html_mod.unescape(re.sub(r'\x1b\[[0-9;]*m', '', text))
            lines.append("")
            lines.append(f"{indent}{BOLD}{text}{RESET}")
            lines.append("")

        elif btype == "paragraph":
            text = _inline_text(block)
            if text.strip():
                lines.extend(_dim_wrap(text))
            else:
                lines.append("")

        elif btype in ("bulletList", "orderedList"):
            for i, item in enumerate(block.get("content", []), 1):
                bullet = f"\u2022 " if btype == "bulletList" else f"{i}. "
                item_text = ""
                for child in item.get("content", []):
                    item_text += _inline_text(child)
                if item_text.strip():
                    first_prefix = indent + "  " + bullet
                    cont_prefix = indent + "    "
                    wrapped = _dim_wrap(item_text.strip(), first_prefix)
                    if wrapped:
                        lines.append(wrapped[0])
                        for w in wrapped[1:]:
                            lines.append(f"{DIM}{cont_prefix}{w.lstrip()}{RESET}")

        elif btype == "rule":
            lines.append(f"{indent}{DIM}{'─' * (width - 4)}{RESET}")

        else:
            text = _inline_text(block)
            if text.strip():
                lines.extend(_dim_wrap(text))

    for child in node.get("content", []):
        _walk_block(child)

    return lines, links



def _parse_rack_location(rack_loc: str) -> dict | None:
    """Parse 'US-EVI01.DH1.R64.RU34' into structured components.

    Returns {site_code, dh, rack, ru} or None if unparseable.
    """
    if not rack_loc:
        return None
    # Strip parenthetical annotations like "(US-EVI01:dh1:244)" from rack locations
    rack_loc = re.sub(r'\s*\([^)]*\)', '', rack_loc)
    parts = rack_loc.split(".")
    if len(parts) < 3:
        return None
    rack_num = None
    ru_num = None
    for p in parts:
        if p.startswith("RU") and p[2:].replace(".", "").isdigit():
            ru_num = p[2:]
        elif p.startswith("R") and p[1:].isdigit():
            rack_num = int(p[1:])
    if rack_num is None:
        return None
    return {"site_code": parts[0], "dh": parts[1], "rack": rack_num, "ru": ru_num}



def _get_physical_neighbors(rack_num: int, layout: dict) -> dict:
    """Return physically adjacent rack numbers accounting for serpentine layout.

    Returns {"left": int|None, "right": int|None, "row": int, "pos": int,
             "col_label": str}.
    """
    cols = layout.get("columns", [])
    default_per_row = layout.get("racks_per_row", 10)
    serpentine = layout.get("serpentine", True)

    # Find which column this rack belongs to
    target_col = None
    target_row = None
    per_row = default_per_row
    for col in cols:
        col_per_row = col.get("racks_per_row", default_per_row)
        col_start = col["start"]
        col_end = col_start + col["num_rows"] * col_per_row - 1
        if col_start <= rack_num <= col_end:
            target_col = col
            per_row = col_per_row
            offset = rack_num - col_start
            target_row = offset // per_row
            break

    if target_col is None:
        return {"left": None, "right": None, "row": 0, "pos": 0, "col_label": "?"}

    col_start = target_col["start"]
    row_start = col_start + target_row * per_row

    # Determine position within the row (accounting for serpentine reversal)
    if serpentine and target_row % 2 == 1:
        pos = (per_row - 1) - (rack_num - row_start)
    else:
        pos = rack_num - row_start

    # Compute neighbor rack numbers at pos-1 and pos+1
    def rack_at_pos(p):
        if p < 0 or p >= per_row:
            return None
        base = col_start + target_row * per_row
        if serpentine and target_row % 2 == 1:
            return base + (per_row - 1 - p)
        return base + p

    return {
        "left": rack_at_pos(pos - 1),
        "right": rack_at_pos(pos + 1),
        "row": target_row,
        "pos": pos,
        "col_label": target_col.get("label", ""),
    }



def _short_device_name(name: str) -> str:
    """Shorten a NetBox device name for rack view display.

    Node devices:  dh1-r064-node-01-us-central-07a  →  Node 1
    Other devices: dh1-bmc-a2-01-r012-us-central-07a →  BMC A2 01
    """
    if not name:
        return "?"
    # Strip parenthetical suffixes like (m1504860) or (serial)
    clean = re.sub(r"\s*\([^)]*\)", "", name).strip()
    # Detect node pattern: anything-node-NN-anything
    m = re.search(r"node-(\d+)", clean, re.IGNORECASE)
    if m:
        return f"Node {int(m.group(1))}"
    # General cleanup: strip dh prefix, rack number, site suffix
    short = clean.lower()
    short = re.sub(r"^dh\d+-", "", short)           # strip dhN- prefix
    short = re.sub(r"-r\d{2,4}", "", short)          # strip -rNNN rack
    short = re.sub(r"-us-\S+$", "", short)            # strip -us-site-suffix
    # Title-case each part; uppercase known acronyms and short tokens
    parts = [p for p in short.split("-") if p]
    def _fmt(p):
        if p in _ACRONYMS or len(p) <= 2:
            return p.upper()
        return p.capitalize()
    return " ".join(_fmt(p) for p in parts) or name



def _build_context(identifier: str, issue: dict,
                   email: str = "", token: str = "") -> dict:
    """Build a structured context dict from a fetched Jira issue."""
    fields = issue.get("fields", {})

    assignee_obj = fields.get("assignee")
    assignee_name = assignee_obj["displayName"] if assignee_obj else None
    assignee_account_id = assignee_obj.get("accountId") if assignee_obj else None
    reporter_obj = fields.get("reporter")
    reporter_name = reporter_obj["displayName"] if reporter_obj else None

    status_obj = fields.get("status") or {}
    issuetype_obj = fields.get("issuetype") or {}
    project_obj = fields.get("project") or {}

    custom = _extract_custom_fields(fields)
    desc_details = _extract_description_details(fields)
    hostname = custom.get("hostname")
    service_tag = custom.get("service_tag")
    node_name = desc_details.get("node_name")

    # Kick off NetBox + SLA in background while we finish parsing Jira fields
    netbox_future = None
    if _netbox_available():
        netbox_future = _cfg._executor.submit(
            _build_netbox_context, service_tag, node_name, hostname,
            rack_location=custom.get("rack_location")
        )
    sla_future = _cfg._executor.submit(_fetch_sla, identifier, email, token) if email else None

    # Continue CPU-bound parsing (overlaps with NetBox I/O)
    linked = _extract_linked_issues(fields)
    portal_url = _extract_portal_url(fields)

    # Check for linked HO (sync check of links, async fetch if found)
    ho_key = None
    for lnk in linked:
        if lnk.get("key", "").startswith("HO-"):
            ho_key = lnk["key"]
            break
    ho_future = None
    if ho_key and email:
        ho_future = _cfg._executor.submit(_jira_get_issue, ho_key, email, token)

    # Lazy comments: store raw data + count; full parsing deferred to [c] handler
    raw_comments = fields.get("comment", {}).get("comments", [])
    comment_count = len(raw_comments)

    # Extract description text (fallback: reporter's first comment)
    desc_adf = fields.get("description")
    description_text = _adf_to_plain_text(desc_adf) if desc_adf else ""
    description_adf = desc_adf  # keep raw ADF for rich rendering
    description_source = "description"
    if not description_text.strip() and raw_comments and reporter_name:
        for cmt in raw_comments:
            cmt_author = (cmt.get("author") or {}).get("displayName", "")
            if cmt_author == reporter_name:
                description_text = _adf_to_plain_text(cmt.get("body", {}))
                description_adf = cmt.get("body", {})
                description_source = "comment"
                break

    # Collect NetBox + SLA results
    netbox = {}
    if netbox_future is not None:
        try:
            netbox = netbox_future.result(timeout=15)
        except Exception:
            netbox = {}

    sla = []
    if sla_future is not None:
        try:
            sla = sla_future.result(timeout=5)
        except Exception:
            pass

    # Collect HO context
    ho_context = None
    if ho_future is not None:
        try:
            ho_issue = ho_future.result(timeout=5)
            if ho_issue:
                from cwhelper.tui.connection_view import _summarize_ho_for_dct  # lazy — avoids circular import
                ho_context = _summarize_ho_for_dct(ho_issue)
        except Exception:
            pass

    # Fill missing Jira fields from NetBox when available
    netbox_device = netbox.get("device_name") if netbox else None
    if netbox:
        if not hostname:
            hostname = netbox.get("device_name")
        if not custom.get("ip_address") or custom.get("ip_address") == "0.0.0.0":
            nb_ip = netbox.get("primary_ip")
            if nb_ip:
                custom["ip_address"] = nb_ip.split("/")[0]
        if not custom.get("vendor"):
            custom["vendor"] = netbox.get("manufacturer")
        # Backfill rack location from NetBox when Jira is empty
        if not custom.get("rack_location"):
            nb_site = netbox.get("site") or ""
            nb_rack = netbox.get("rack") or ""
            nb_pos = netbox.get("position")
            if nb_rack and nb_pos:
                custom["rack_location"] = f"{nb_site}.DH1.R{nb_rack}.RU{int(nb_pos)}"

    # Backfill rack location from description hostnames (e.g. dh1-r264-node-02-...)
    if not custom.get("rack_location") and desc_details.get("desc_rack"):
        site = custom.get("site") or ""
        dh = desc_details.get("desc_dh") or "DH1"
        rack = desc_details["desc_rack"]
        ru = desc_details.get("desc_ru") or 1
        custom["rack_location"] = f"{site}.{dh}.R{rack}.RU{ru}"

    # Build result first, then enrich grafana URLs with full context
    result = {
        "source": "jira",
        "identifier": identifier,
        "issue_key": issue.get("key", identifier),
        "summary": fields.get("summary", ""),
        "status": status_obj.get("name", "Unknown"),
        "priority": (fields.get("priority") or {}).get("name"),
        "issue_type": issuetype_obj.get("name", "Unknown"),
        "project": project_obj.get("key", "Unknown"),
        "assignee": assignee_name,
        "reporter": reporter_name,
        # Ticket age tracking
        "created": fields.get("created"),
        "updated": fields.get("updated"),
        "status_age_seconds": _parse_jira_timestamp(fields.get("statuscategorychangedate")),
        "_assignee_account_id": assignee_account_id,
        "rack_location": custom.get("rack_location"),       # cf[10207]
        "service_tag": service_tag,                          # cf[10193]
        "hostname": hostname,                                # cf[10192] or NetBox device name
        "site": custom.get("site"),                          # cf[10194]
        "ip_address": custom.get("ip_address"),              # cf[10191] or NetBox mgmt IP
        "vendor": custom.get("vendor"),                      # cf[10210] or NetBox manufacturer
        # Parsed from description
        "rma_reason": desc_details.get("rma_reason"),
        "node_name": node_name,
        "diag_links": desc_details.get("diag_links", []),
        # Comments (lazy-loaded: parsed on first [c] press)
        "comments": [],
        "_raw_comments": raw_comments,
        "_comment_count": comment_count,
        # Attachments
        "attachments": [
            {"filename": a.get("filename", "?"),
             "size": a.get("size", 0),
             "author": (a.get("author", {}) or {}).get("displayName", "?"),
             "created": (a.get("created", ""))[:16].replace("T", " "),
             "url": a.get("content", "")}
            for a in (fields.get("attachment") or [])
        ],
        # Related tickets
        "linked_issues": linked,
        # Grafana (placeholder — enriched below with full context)
        "grafana": {},
        # NetBox (optional — empty dict if not configured)
        "netbox": netbox,
        # SLA timers from Jira Service Desk API
        "sla": sla,
        # HO context (linked HO summary, if found)
        "ho_context": ho_context,
        # Description / work order text
        "description_text": description_text.strip(),
        "_description_adf": description_adf,
        "_description_source": description_source,
        # Internal / display-only
        "_portal_url": portal_url,
        "_transitions": None,   # lazy-loaded on first status button press
        "_fetched_at": time.time(),
        "raw_issue": issue,
    }
    result["grafana"] = _build_grafana_urls(node_name, hostname, service_tag, netbox_device, ctx=result)

    # Parse PSU details from description text (for PSU tickets)
    result["psu_info"] = _extract_psu_info(result.get("description_text", ""))

    return result



def _fetch_and_show(identifier: str, email: str, token: str,
                    quiet: bool = False) -> dict | None:
    """Fetch a single issue by key or search term, return context dict.

    In interactive mode (quiet=False), shows the pretty output and returns
    the context so callers can offer follow-up actions.
    Returns None if nothing was found or user cancelled.
    """
    # Direct fetch if it looks like a Jira key
    if JIRA_KEY_PATTERN.match(identifier):
        issue = _jira_get_issue(identifier, email, token)
        return _build_context(identifier, issue, email, token)

    # Otherwise, search by text (serial, hostname, etc.)
    if not quiet:
        print(f"  Searching DO/HO for '{identifier}'...\n")

    issues = _search_by_text(identifier, email, token)

    if not issues:
        if not quiet:
            print(f"  {YELLOW}{BOLD}No results{RESET} {DIM}for '{identifier}'.{RESET}")
        return None

    if quiet or len(issues) == 1:
        chosen = issues[0]
    else:
        from cwhelper.tui.display import _status_color, _prompt_select  # lazy — avoids circular import

        def _label(i, iss):
            f = iss.get("fields", {})
            st = f.get("status", {}).get("name", "?")
            sc, sd = _status_color(st)
            return f"  {BOLD}{i}.{RESET}  {iss['key']}  {sc}{sd} {st:<18}{RESET} {f.get('summary', '')}"

        print(f"  Found {len(issues)} matches:\n")
        chosen = _prompt_select(issues, _label)
        if chosen in ("refresh", "menu", "quit"):
            return None
        if not chosen:
            return None

    if not quiet:
        print(f"\n  Fetching {chosen['key']}...\n")

    issue = _jira_get_issue(chosen['key'], email, token)
    return _build_context(identifier, issue, email, token)



def get_node_context(identifier: str, quiet: bool = False) -> dict:
    """Public API: given an identifier, return a context dict or exit."""
    email, token = _get_credentials()
    ctx = _fetch_and_show(identifier, email, token, quiet=quiet)
    if ctx is None:
        sys.exit(1)
    return ctx


