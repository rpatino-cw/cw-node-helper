"""IB trace display — visual connection trace for switch port lookups."""
from __future__ import annotations

import json
import os

from cwhelper.config import BOLD, DIM, RESET, CYAN, GREEN, YELLOW, WHITE, MAGENTA, RED, BLUE

__all__ = ['_display_ibtrace']


def _display_ibtrace(
    results: list[dict],
    query_switch: str,
    query_port: str | None,
) -> None:
    """Render IB trace results — single trace or multi-port table."""
    if not results:
        print(f"\n  {YELLOW}No connections found for {BOLD}{query_switch}"
              f"{' port ' + query_port if query_port else ''}{RESET}\n")
        return

    if query_port and len(results) <= 2:
        for conn in results:
            _display_single_trace(conn)
    else:
        _display_multi_trace(results, query_switch)


def _display_single_trace(conn: dict) -> None:
    """Render a single connection as a visual box trace."""
    src = conn["src_name"]
    dst = conn["dest_name"]
    src_type = conn["src_type"]
    dst_type = conn["dest_type"]
    src_cab = conn["src_cab"]
    dst_cab = conn["dest_cab"]
    src_dh = conn["src_dh"]
    dst_dh = conn["dest_dh"]
    src_port = conn["src_port"]
    dst_port = conn["dest_port"]
    tab = conn["tab_ref"]

    # Compute box widths
    left_lines = [src, f"{src_type} \u00b7 Cab {src_cab}", f"{src_dh} \u00b7 Port {src_port}"]
    right_lines = [dst, f"{dst_type} \u00b7 Cab {dst_cab}", f"{dst_dh} \u00b7 Port {dst_port}"]
    lw = max(len(l) for l in left_lines) + 4
    rw = max(len(l) for l in right_lines) + 4

    def _box_line(text: str, width: int) -> str:
        return f"\u2502  {text}{' ' * (width - len(text) - 4)}  \u2502"

    header = f"  {BOLD}{CYAN}IB Trace \u2014 {src} \u2192 {dst}{RESET}"
    sep = f"  {DIM}\u2550" * 40 + RESET

    print()
    print(header)
    print(sep)
    print()

    # Top border
    h = "\u2500"
    top_l = "  \u250c" + h * (lw - 2) + "\u2510"
    top_r = "\u250c" + h * (rw - 2) + "\u2510"
    print(f"{top_l}         {top_r}")

    # Row 1: switch names (bold)
    l1 = f"  {_box_line(f'{BOLD}{GREEN}{src}{RESET}', lw)}"
    r1 = f"{_box_line(f'{BOLD}{MAGENTA}{dst}{RESET}', rw)}"
    # Compensate for ANSI codes in width calc
    l1_raw = f"  \u2502  {src}{' ' * (lw - len(src) - 4)}  \u2502"
    r1_raw = f"\u2502  {dst}{' ' * (rw - len(dst) - 4)}  \u2502"
    l1_colored = l1_raw.replace(src, f"{BOLD}{GREEN}{src}{RESET}", 1)
    r1_colored = r1_raw.replace(dst, f"{BOLD}{MAGENTA}{dst}{RESET}", 1)
    print(f"{l1_colored}         {r1_colored}")

    # Row 2: type + cab
    l2_text = f"{src_type} \u00b7 Cab {src_cab}"
    r2_text = f"{dst_type} \u00b7 Cab {dst_cab}"
    l2 = f"  \u2502  {l2_text}{' ' * (lw - len(l2_text) - 4)}  \u2502"
    r2 = f"\u2502  {r2_text}{' ' * (rw - len(r2_text) - 4)}  \u2502"
    arrow = f"  {WHITE}\u2500\u2500\u2500\u25b6{RESET}  "
    print(f"{l2}{arrow}{r2}")

    # Row 3: DH + port
    l3_text = f"{src_dh} \u00b7 Port {src_port}"
    r3_text = f"{dst_dh} \u00b7 Port {dst_port}"
    l3 = f"  \u2502  {l3_text}{' ' * (lw - len(l3_text) - 4)}  \u2502"
    r3 = f"\u2502  {r3_text}{' ' * (rw - len(r3_text) - 4)}  \u2502"
    print(f"{l3}         {r3}")

    # Bottom border
    bot_l = "  \u2514" + h * (lw - 2) + "\u2518"
    bot_r = "\u2514" + h * (rw - 2) + "\u2518"
    print(f"{bot_l}         {bot_r}")

    if tab:
        print(f"\n  {DIM}Tab: {tab}{RESET}")

    # --- Port elevation: show where the port sits on each switch ---
    _show_port_elevation(src, src_port, dst, dst_port)

    # --- IB Sketch elevation: show all switches at their RU positions ---
    _show_ib_sketch_elevation(conn)

    # --- Rack elevation: NetBox rack view for both cabs ---
    _show_rack_elevations(conn)

    # --- Rack map: show both endpoints on the DH floor map ---
    _show_trace_map(conn)
    print()


def _display_multi_trace(results: list[dict], query_switch: str) -> None:
    """Render multiple connections as a table."""
    # Determine if query matched as src or dest for the majority
    src_count = sum(
        1 for r in results
        if query_switch.upper() in r["src_name"].upper()
    )
    as_source = src_count >= len(results) // 2

    count = len(results)
    print(f"\n  {BOLD}{CYAN}IB Connections \u2014 {query_switch} ({count} port{'s' if count != 1 else ''}){RESET}")
    print(f"  {DIM}\u2550" * 60 + RESET)
    print()

    # Header
    if as_source:
        hdr = (f"  {BOLD}{'Port':<10}{'Destination':<20}{'Dest Port':<12}"
               f"{'Cab':<8}{'DH':<6}{'Tab'}{RESET}")
    else:
        hdr = (f"  {BOLD}{'Port':<10}{'Source':<20}{'Src Port':<12}"
               f"{'Cab':<8}{'DH':<6}{'Tab'}{RESET}")
    print(hdr)
    print(f"  {DIM}" + "\u2500" * 74 + RESET)

    # Sort by port naturally
    def _port_sort_key(conn: dict) -> tuple:
        port_str = conn["src_port"] if as_source else conn["dest_port"]
        parts = port_str.split("/")
        try:
            return (int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
        except (ValueError, IndexError):
            return (9999, 0)

    for conn in sorted(results, key=_port_sort_key):
        if as_source:
            port = conn["src_port"]
            other = conn["dest_name"]
            other_port = conn["dest_port"]
            cab = conn["dest_cab"]
            dh = conn["dest_dh"]
        else:
            port = conn["dest_port"]
            other = conn["src_name"]
            other_port = conn["src_port"]
            cab = conn["src_cab"]
            dh = conn["src_dh"]

        tab = conn["tab_ref"]
        # Truncate long tab refs
        if len(tab) > 28:
            tab = tab[:25] + "..."

        print(f"  {GREEN}{port:<10}{RESET}{other:<20}{other_port:<12}"
              f"{cab:<8}{dh:<6}{DIM}{tab}{RESET}")

    print()


# ────────────────────────────────────────────────────────────────────
#  Rack map + elevation for single trace
# ────────────────────────────────────────────────────────────────────

def _load_dh_layout(dh_key: str) -> dict | None:
    """Load a DH layout from dh_layouts.json by key (e.g. 'US-SITE01.DH2')."""
    paths = [
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "dh_layouts.json"),
        os.path.join(os.path.expanduser("~/dev/cw-node-helper"), "dh_layouts.json"),
    ]
    for p in paths:
        try:
            with open(p) as f:
                data = json.load(f)
            return data.get(dh_key)
        except (FileNotFoundError, json.JSONDecodeError):
            continue
    return None


def _rack_at(col_start, row, pos, rpr, serpentine):
    if serpentine and row % 2 == 1:
        return col_start + (row + 1) * rpr - 1 - pos
    return col_start + row * rpr + pos


def _find_rack_pos(rack_num, col, rpr, serpentine):
    for row in range(col["num_rows"]):
        for pos in range(rpr):
            if _rack_at(col["start"], row, pos, rpr, serpentine) == rack_num:
                return (row, pos)
    return None


def _render_single_dh_map(dh_label: str, layout: dict, hl_a: int | None,
                          hl_b: int | None, name_a: str, name_b: str) -> None:
    """Render one DH floor map with optional highlights."""
    cols = layout["columns"]
    rpr_default = layout.get("racks_per_row", 10)
    serpentine = layout.get("serpentine", True)
    COL_GAP = "       "

    def _vis_width(rpr):
        return 4 + (rpr * 2 - 1) + 4

    def _cell(rn):
        if rn == hl_a:
            return f"{GREEN}{BOLD}@{RESET}"
        if rn == hl_b:
            return f"{MAGENTA}{BOLD}#{RESET}"
        return f"{DIM}-{RESET}"

    max_rows = max(c["num_rows"] for c in cols)

    print(f"\n  {BOLD}{dh_label} Rack Map{RESET}")
    legend = []
    if hl_a is not None:
        legend.append(f"{GREEN}{BOLD}@{RESET}=R{hl_a} ({name_a})")
    if hl_b is not None:
        legend.append(f"{MAGENTA}{BOLD}#{RESET}=R{hl_b} ({name_b})")
    if legend:
        print(f"  {DIM}{'  '.join(legend)}{RESET}")

    hdr_parts = []
    for col in cols:
        rpr = col.get("racks_per_row", rpr_default)
        end_rack = col["start"] + col["num_rows"] * rpr - 1
        part = f"{col['label']} (R{col['start']}-R{end_rack})"
        cw = _vis_width(rpr)
        hdr_parts.append(f"{part:<{cw}}")
    print(f"  {DIM}{COL_GAP.join(hdr_parts)}{RESET}")

    for row in range(max_rows):
        col_strs = []
        for col in cols:
            rpr = col.get("racks_per_row", rpr_default)
            cw = _vis_width(rpr)
            if row < col["num_rows"]:
                first = _rack_at(col["start"], row, 0, rpr, serpentine)
                last = _rack_at(col["start"], row, rpr - 1, rpr, serpentine)
                label_l = f"{DIM}{first:>3}{RESET} "
                label_r = f" {DIM}{last:<3}{RESET}"
                cells = " ".join(_cell(_rack_at(col["start"], row, pos, rpr, serpentine)) for pos in range(rpr))
                col_strs.append(label_l + cells + label_r)
            else:
                col_strs.append(" " * cw)
        print(f"  {COL_GAP.join(col_strs)}")
        if row % 2 == 1 and row < max_rows - 1:
            print()
    print()


def _show_trace_map(conn: dict) -> None:
    """Show DH floor map(s) highlighting both endpoints. Renders both maps for cross-DH traces."""
    src_cab = conn.get("src_cab", "")
    dst_cab = conn.get("dest_cab", "")
    src_dh = conn.get("src_dh", "")
    dst_dh = conn.get("dest_dh", "")

    cab_a = int(src_cab) if src_cab.isdigit() else None
    cab_b = int(dst_cab) if dst_cab.isdigit() else None
    if cab_a is None and cab_b is None:
        return

    # Collect unique DHs involved
    dh_set = set()
    if src_dh:
        dh_set.add(src_dh)
    if dst_dh:
        dh_set.add(dst_dh)

    for dh in sorted(dh_set):
        key = f"{os.environ.get('DEFAULT_SITE', 'US-SITE01')}.{dh}"
        layout = _load_dh_layout(key)
        if not layout:
            continue

        # Highlight only endpoints that belong to this DH
        hl_a = cab_a if src_dh == dh else None
        hl_b = cab_b if dst_dh == dh else None
        if hl_a is None and hl_b is None:
            continue

        name_a = conn.get("src_name", "") if hl_a else ""
        name_b = conn.get("dest_name", "") if hl_b else ""
        _render_single_dh_map(dh, layout, hl_a, hl_b, name_a, name_b)


def _show_ib_sketch_elevation(conn: dict) -> None:
    """Show IB sketch elevation — all switches at their RU positions in the rack."""
    try:
        from cwhelper.services.ib_sketch import _get_rack_switches
    except ImportError:
        return

    src_cab = conn.get("src_cab", "")
    dst_cab = conn.get("dest_cab", "")
    src_name = conn.get("src_name", "").upper()
    dst_name = conn.get("dest_name", "").upper()

    shown = set()
    for cab, highlight_name in [(src_cab, src_name), (dst_cab, dst_name)]:
        if not cab or cab in shown:
            continue

        rack_data = _get_rack_switches(cab)
        if not rack_data:
            continue
        shown.add(cab)

        ru_map = rack_data.get("ru", {})
        if not ru_map:
            continue

        row_label = rack_data.get("row_label", "")
        config = rack_data.get("config", "")

        # Determine RU range (only show populated area + 1U margin)
        rus = sorted(int(r) for r in ru_map)
        ru_min = max(rus[0] - 1, 1)
        ru_max = min(rus[-1] + 1, 50)

        # Header
        hdr = f"  {BOLD}Rack {cab}{RESET}"
        if row_label:
            hdr += f"  {DIM}·  {row_label}{RESET}"
        if config:
            hdr += f"  {DIM}·  {config}{RESET}"
        print(f"\n{hdr}")

        # Box
        inner_w = 44
        print(f"  {'┌' + '─' * inner_w + '┐'}")

        for ru in range(ru_max, ru_min - 1, -1):
            ru_str = str(ru)
            entry = ru_map.get(ru_str)
            if entry:
                sw = entry["name"]
                model = entry["model"]
                is_highlight = sw.upper() == highlight_name
                if is_highlight:
                    line = f"  U{ru:<3}  {CYAN}{BOLD}{sw:<12}{RESET}  {model}"
                    marker = f"  {CYAN}◄── traced{RESET}"
                else:
                    line = f"  U{ru:<3}  {sw:<12}  {DIM}{model}{RESET}"
                    marker = ""
                # Pad to inner width (account for ANSI codes)
                visible_len = 2 + 1 + 3 + 2 + 12 + 2 + len(model)
                pad = max(inner_w - visible_len, 0)
                print(f"  │{line}{' ' * pad}│{marker}")
            else:
                print(f"  │  U{ru:<3}{' ' * (inner_w - 6)}│")

        print(f"  {'└' + '─' * inner_w + '┘'}")

        sw_count = len(ru_map)
        print(f"  {DIM}{sw_count} switches  │  IB Sketch{RESET}")


def _show_rack_elevations(conn: dict) -> None:
    """Show NetBox rack elevation for both endpoint cabinets."""
    try:
        from cwhelper.clients.netbox import _netbox_find_rack_by_name
        from cwhelper.services.rack import _draw_rack_elevation
    except ImportError:
        return

    src_cab = conn.get("src_cab", "")
    dst_cab = conn.get("dest_cab", "")
    src_dh = conn.get("src_dh", "")
    dst_dh = conn.get("dest_dh", "")

    shown = set()
    for cab, dh, name in [(src_cab, src_dh, conn.get("src_name", "")),
                           (dst_cab, dst_dh, conn.get("dest_name", ""))]:
        if not cab or cab in shown:
            continue
        shown.add(cab)

        rack = _netbox_find_rack_by_name(cab, site_slug=os.environ.get("DEFAULT_SITE_SLUG", "us-site-01a"))
        if not rack:
            continue

        rack_id = rack.get("id")
        if not rack_id:
            continue

        # Build a minimal ctx for _draw_rack_elevation
        ctx = {
            "netbox": {
                "rack_id": rack_id,
                "rack": rack.get("display") or rack.get("name") or f"R{cab}",
                "device_name": name,
                "position": None,
            },
            "site": os.environ.get("DEFAULT_SITE", ""),
        }
        _draw_rack_elevation(ctx)

    print()


def _show_port_elevation(src: str, src_port: str, dst: str, dst_port: str) -> None:
    """Show a visual port elevation for both switches.

    QM9790 switches have 64 ports arranged as 32 port pairs (X/1, X/2).
    Renders a 2-row grid showing port positions with the active port highlighted.
    """
    def _parse_port(port_str: str) -> tuple[int, int] | None:
        parts = port_str.split("/")
        if len(parts) == 2:
            try:
                return (int(parts[0]), int(parts[1]))
            except ValueError:
                pass
        return None

    def _draw_switch_ports(name: str, active_port: str, color: str) -> None:
        parsed = _parse_port(active_port)
        if not parsed:
            return
        slot, lane = parsed
        total_slots = 32  # QM9790: 32 slots, 2 lanes each

        print(f"  {BOLD}{name}{RESET} {DIM}port {active_port}{RESET}")

        # Row 1: lane /1 (top)
        row1 = "  /1 "
        for s in range(1, total_slots + 1):
            if s == slot and lane == 1:
                row1 += f"{color}{BOLD}█{RESET}"
            elif s == slot and lane == 2:
                row1 += f"{DIM}░{RESET}"
            else:
                row1 += f"{DIM}·{RESET}"
        print(row1)

        # Row 2: lane /2 (bottom)
        row2 = "  /2 "
        for s in range(1, total_slots + 1):
            if s == slot and lane == 2:
                row2 += f"{color}{BOLD}█{RESET}"
            elif s == slot and lane == 1:
                row2 += f"{DIM}░{RESET}"
            else:
                row2 += f"{DIM}·{RESET}"
        print(row2)

        # Scale
        scale = "     "
        for s in range(1, total_slots + 1):
            if s % 8 == 1:
                scale += f"{DIM}{s}{RESET}"
                scale += " " * (1 - len(str(s)) + 1) if len(str(s)) == 1 else ""
            elif s % 8 != 2 or s == 2:
                scale += " "
        print(f"  {DIM}     1       8       16      24      32{RESET}")
        print()

    print()
    _draw_switch_ports(src, src_port, GREEN)
    _draw_switch_ports(dst, dst_port, MAGENTA)

    print()
