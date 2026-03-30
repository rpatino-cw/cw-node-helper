# CW Node Helper — Update Log (2026-03-01)

## Session Summary

Major feature additions, UX improvements, and bug fixes across the tool. All changes in a single file: `get_node_context.py`.

---

## Features Added

### 1. DH Map — Yellow Walking Route
- Added animated yellow walking route from entrance (bottom-right) to target rack
- Route traces: `=======+` turn at target row, `|` corridor from entrance upward
- Entrance line `==================` at bottom of map connecting right wall to corridor
- Map now shows on its own screen (clear → animate → ENTER → clear → ticket info)

### 2. DH Map — Configurable Data Hall Layouts
- New `dh_layouts.json` config file for custom DH layouts
- Interactive setup wizard: `_setup_dh_layout()` asks racks/row, columns, rows per column, serpentine y/n, entrance
- Built-in fallback for DH1 at US-SITE01/US-SITE-01A (no config needed)
- Unknown data halls prompt "Would you like to set one up?"

### 3. Rack Elevation View (`[e]` hotkey)
- Visual rack cabinet drawing showing all devices at their RU positions
- Fetches actual rack height from NetBox `/dcim/racks/{id}/`
- Bulk-fetches device type `u_height` for accurate multi-U blocks
- Device labels at top of block, `┆` continuation markers below
- Current device highlighted in cyan with `>>` marker
- Animates top-to-bottom in terminal, static when piped

### 4. Combined Rack View (merged `[w]` + `[e]` + `[k]`)
- Single `[e]` "Rack View" replaces three separate hotkeys
- Shows elevation → numbered device list → pick to search Jira
- Type `x` to open NetBox rack page in browser
- Removed redundant `[w]` Rack Neighbors and `[k]` Rack View buttons

### 5. MRB Queue (Menu Option 9)
- New menu option to browse MRB (RMA/parts) tickets by site
- Reuses existing `_search_queue(project="MRB")` — no new JQL logic
- CLI already works: `queue --project MRB --site US-SITE-01A`

### 6. MRB Lookup from Tickets (`[f]` hotkey)
- Press `[f]` on a DO/HO ticket to find related MRB parts tickets
- Searches MRB project by service tag + site
- Button only shows when MRB tickets actually exist (pre-checked in parallel)

### 7. SDx Linked Tickets (`[s]` hotkey)
- Press `[s]` to find the originating customer ticket (SDA/SDE/SDO/SDP/SDS)
- First checks `issuelinks` for direct SDx links (no API call)
- Falls back to search by service tag + site
- Button only shows when SDx tickets are found

### 8. SLA Timers
- Fetches SLA data from `/rest/servicedeskapi/request/{key}/sla` in parallel
- Displays in ticket detail: Met/Breached/Paused/Remaining with color coding
- Green (met/plenty of time), Yellow (<50% remaining), Red (breached/<25%), Blue (paused)
- Only shown for Jira tickets, hidden for NetBox-only device views

### 9. NetBox IP Addresses
- Added `primary_ip4`, `primary_ip6`, `oob_ip` extraction from NetBox device response
- BMC IP and IPv6 displayed in node info block when available
- IPs only shown when they have real values (no `0.0.0.0` or `—` placeholders)

### 10. Enhanced Connections Display
- Interface speed shown (parsed from NetBox type field: `1000base-t` → `1G`)
- Short peer device names via `_short_device_name()` (strips DH prefix, rack, site suffix)
- Peer rack shown in parens (e.g. `(R061)`)
- Numbered connections — type a number to open cable in NetBox browser
- Role hints: BMC → "(management)", DPU → "(data fabric)"

### 11. Help / Quick Guide (`?` on main menu)
- Full-screen guide explaining every menu option and hotkey
- "Use when:" plain-English explanations for each feature
- All ticket view hotkeys documented: View, Open, Actions, Navigation

### 12. Default Bookmarks
- Seeded on first run: "All my tickets" and "The Elks Open queue"
- Removable via Bookmarks manager (option 8)
- Rename support added (`r` in bookmark manager)

---

## UX Improvements

### Ticket Header Redesign
- Bold colored `━` borders matching status color (yellow=open, green=closed, blue=waiting)
- Ticket key in white bold, status uppercased
- Assignee in bold magenta

### NetBox Device View (No-Ticket Header)
- Different header for devices with no open tickets
- Shows device short name + full hostname + NetBox status
- No "Project —" / "Type NetBox Device" / "Assignee Unassigned" clutter
- Full action panel available (rack map, connections, elevation, Grafana, etc.)
- Actions section hidden (no Grab/Bookmark for non-tickets)
- `[j]` Jira and `[p]` Portal hidden (no ticket to open)

### Device Name Shortening
- `_short_device_name()` strips DH prefix, rack number, site suffix, parenthetical serials
- Node devices: `dh1-r064-node-01-us-site-01a` → `Node 1`
- Other devices: `dh1-bmc-a2-01-r012-us-site-01a` → `BMC A2 01`
- Known acronyms uppercased: BMC, TOR, PDU, DPU, NIC, GPU, etc.

### Logged-In User Banner
- Shows full name derived from `JIRA_EMAIL` in green bold
- "logged in" confirmation instead of "Hey Romeo"
- Cleaner `━`/`┃` border style

### Assignee Condition
- `[a]` Grab/Reassign button hidden when ticket is already assigned to you
- Compares against JIRA display name, account ID, and email-derived name

### IB Link Warning
- `[i]` IB hotkey now shows a warning that the link isn't working properly
- Provides the URL for manual copy
- Asks "Open anyway? [y/n]" before launching

---

## Bug Fixes

### Clear + Reprint on All Hotkeys
- All 6 browser-open handlers (`[j]`, `[p]`, `[g]`, `[i]`, `[t]`, `[x]`) now clear screen + reprint ticket info after opening
- `[a]` Assign and `[*]` Bookmark also clear + reprint (with 0.5s delay to see confirmation)
- Connections cable-open clears + reprints after interaction

### History Navigation Fix
- Pressing ENTER/back in history list now returns to the ticket you were viewing
- Previously bounced all the way to the main menu
- Fixed in all callers: queue drill-down, main menu, watcher, bookmarks, rack view

### Rack Elevation Float Fix
- NetBox returns `position` as float (e.g. `34.0`) — cast to `int` for `range()`

### Empty Queue Messages
- Queue: `"In Progress — no DO tickets for US-SITE-01A"` with suggestion
- Node history: `"No tickets found for 'S948338X5A04781'"` with context
- Single lookup: `"No results for 'DO-99999'"`

### Map Display Conditional
- DH map removed from auto-display in ticket detail view
- Only shows via explicit `[r]` hotkey or menu option 7

---

## Files Modified

| File | Description |
|------|-------------|
| `get_node_context.py` | All changes (monolith) |
| `dh_layouts.json` | New — DH layout config (created by setup wizard) |
| `FUTURE_PLANS.md` | Updated with rack row neighbors idea |
| `UPDATE_LOG_2026-03-01.md` | This file |

## Memory Updated

| File | Description |
|------|-------------|
| `~/.claude/projects/.../memory/dh1_map_spec.md` | Updated with implementation status |
