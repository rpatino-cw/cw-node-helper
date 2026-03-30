# Hidden Features — Temporarily Disabled

Features that are built but disabled due to access issues, bugs, or low priority.

## MRB Queue (Menu Option 9)

**Status:** Hidden — user doesn't have practical use yet
**What it does:** Browses MRB (RMA/parts logistics) tickets by site, same as DO/HO queues.
**Code:** `_search_queue(project="MRB")` — works if you have MRB project access.
**To re-enable:** Uncomment option 9 in the menu display and handler.
**Handler location:** Search for `choice == "9"` and `MRB queue` in get_node_context.py.

## SDx Ticket Lookup ([s] button)

**Status:** Removed — SDx projects (SDA, SDE, SDO, SDP, SDS) do not exist in this Jira instance.
**What it did:** Searched for linked customer-facing tickets in SDx projects.
**Error:** `"The value 'SDA' does not exist for the field 'project'."`
**To re-enable:** If SDx projects become available, re-add `SDX_PROJECTS` constant and `_show_sdx_for_ticket` function. The `[s]` hotkey is available.

## MRB Lookup from DO/HO ([f] button)

**Status:** Hidden — MRB project may not be accessible.
**What it does:** Pre-checks for MRB tickets related to the current node's service tag.
**Code:** `_show_mrb_for_node()` function exists but button hidden when MRB count is 0.
**Note:** The pre-check queries MRB project; if access is denied, it silently returns 0.

## Snipe-IT Link ([si] button)

**Status:** Active — only shown when asset tag starts with `m` (e.g. `m001023`).
**Note:** Devices with `S`-prefixed asset tags (e.g. `S029490`) will not show the button. If the URL pattern for `S`-prefixed tags is identified, update `_snipe_url_from_tag()` in `cwhelper/clients/netbox.py` to handle them.

## IB Grafana Link ([i] button)

**Status:** Shows warning — IB Grafana link not working properly.
**What it does:** Opens InfiniBand dashboard in Grafana.
**Current behavior:** Shows warning + URL + asks "Open anyway? [y/n]"
