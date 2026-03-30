#!/usr/bin/env python3
"""Self-contained test of the per-dash DH map logic (no module import)."""

BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"
CYAN = "\033[36m"

def _parse_rack_location(rack_loc):
    if not rack_loc:
        return None
    parts = rack_loc.split(".")
    if len(parts) < 3:
        return None
    rack_num = ru_num = None
    for p in parts:
        if p.startswith("RU") and p[2:].replace(".", "").isdigit():
            ru_num = p[2:]
        elif p.startswith("R") and p[1:].isdigit():
            rack_num = int(p[1:])
    if rack_num is None:
        return None
    return {"site_code": parts[0], "dh": parts[1], "rack": rack_num, "ru": ru_num}

def _draw_mini_dh_map(rack_loc):
    parsed = _parse_rack_location(rack_loc)
    if not parsed:
        return
    target = parsed["rack"]
    site_code = parsed["site_code"]
    dh = parsed["dh"]
    LEFT_ROWS = 14
    RIGHT_ROWS = 18
    PER_ROW = 10

    def rack_at(col_start, row, pos):
        base = col_start + row * PER_ROW
        if row % 2 == 0:
            return base + pos
        return base + (PER_ROW - 1 - pos)

    def build_row(col_start, row):
        chars = []
        for pos in range(PER_ROW):
            if rack_at(col_start, row, pos) == target:
                chars.append(f"{CYAN}{BOLD}#{RESET}")
            else:
                chars.append("-")
        return "".join(chars)

    if 1 <= target <= 140:
        side = "LEFT"
    elif 141 <= target <= 320:
        side = "RIGHT"
    else:
        side = "?"

    max_rows = max(LEFT_ROWS, RIGHT_ROWS)
    print(f"\n  {BOLD}{site_code} {dh}{RESET} {DIM}— Rack R{target}{RESET}")
    print(f"  {DIM}Left (R1-R140)               Right (R141-R320){RESET}\n")
    for row in range(max_rows):
        if row < LEFT_ROWS:
            left = f"  {build_row(1, row)}"
        else:
            left = f"  {'          '}"
        if row < RIGHT_ROWS:
            right = f"{build_row(141, row)}"
        else:
            right = ""
        print(f"{left}               {right}")
        if row % 2 == 1 and row < max_rows - 1:
            print()
    print()
    print(f"  {CYAN}{BOLD}#{RESET} = R{target} ({side} column)")
    if parsed.get("ru"):
        print(f"  {DIM}Rack unit: RU{parsed['ru']}{RESET}")
    print(f"  {DIM}Entrance: bottom right (near R311){RESET}")
    print()

# --- Verify serpentine math ---
def verify():
    PER_ROW = 10
    def rack_at(col_start, row, pos):
        base = col_start + row * PER_ROW
        if row % 2 == 0:
            return base + pos
        return base + (PER_ROW - 1 - pos)

    # Left col row 0: R1..R10
    row0 = [rack_at(1, 0, p) for p in range(10)]
    assert row0 == [1,2,3,4,5,6,7,8,9,10], f"row0={row0}"
    # Left col row 1: R20..R11
    row1 = [rack_at(1, 1, p) for p in range(10)]
    assert row1 == [20,19,18,17,16,15,14,13,12,11], f"row1={row1}"
    # Left col row 2: R21..R30
    row2 = [rack_at(1, 2, p) for p in range(10)]
    assert row2 == [21,22,23,24,25,26,27,28,29,30], f"row2={row2}"
    # Right col row 0: R141..R150
    rr0 = [rack_at(141, 0, p) for p in range(10)]
    assert rr0 == [141,142,143,144,145,146,147,148,149,150], f"rr0={rr0}"
    # Right col row 17 (last): R320..R311
    rr17 = [rack_at(141, 17, p) for p in range(10)]
    assert rr17 == [320,319,318,317,316,315,314,313,312,311], f"rr17={rr17}"
    print("All serpentine math checks passed!")

verify()
for rack in ["US-SITE01.DH1.R64.RU34", "US-SITE01.DH1.R18.RU10",
             "US-SITE01.DH1.R311.RU5", "US-SITE01.DH1.R1.RU1",
             "US-SITE01.DH1.R141.RU20"]:
    print(f"{'='*55}")
    _draw_mini_dh_map(rack)
