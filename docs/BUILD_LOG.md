# CW Node Helper — Build Log

## What is this?

A CLI tool for DCTs to look up node/ticket context from the terminal. Built in one session, iteratively, starting from a single Jira API call and growing into a multi-source interactive app.

## Data Sources

- **Jira Cloud** (required) — tickets, custom fields, comments, linked issues
- **NetBox** (optional) — authoritative device info, rack/site, interfaces, cabling

## Features Built (in order)

### v1 — Basic Jira lookup
- `get_node_context(identifier)` function
- Fetch a DO/HO ticket by Jira key (e.g. `DO-12345`)
- Extract: summary, status, issue type, project, assignee
- Pretty-print + raw JSON dump
- Auth via `JIRA_EMAIL` + `JIRA_API_TOKEN` env vars

### v2 — Custom fields + JQL search
- Extracted DCT-specific custom fields from Jira:
  - `cf[10193]` — service tag (e.g. `10NQ724`)
  - `cf[10192]` — hostname (e.g. `d0001142`)
  - `cf[10194]` — site (e.g. `US-EAST-03`)
  - `cf[10207]` — rack location (e.g. `US-BVI01.DC7.R297.RU18`)
  - `cf[10191]` — IP address
  - `cf[10210]` — vendor (e.g. `Dell`)
- JQL search by service tag or hostname across DO + HO projects
- Used new `POST /rest/api/3/search/jql` endpoint (old one was deprecated)
- Fixed "DO" as reserved JQL word (needs quoting)
- Linked issue extraction (DO <-> HO relationships)

### v3 — Clean output + argparse
- Switched to `argparse` for proper `-h`/`--help`/`--json`/`--raw` handling
- `--json` outputs clean JSON only (no banners)
- `--raw` outputs full Jira API response
- Multi-match selection prompt when searching by service tag
- Jira URLs + portal links in output
- Color + icons for terminal output (ANSI colors, unicode icons)
- Status-colored dots (green=closed, yellow=in-progress, blue=verification)

### v4 — Interactive menu + queue browser
- Interactive menu loop (`python3 get_node_context.py` with no args)
- Queue browser: browse open DO/HO tickets by site
- Status filters: open, closed, verification, in progress, waiting, all
- `--mine` flag for tickets assigned to you
- `--project DO/HO` to switch between projects
- Site filter is optional (leave blank for all sites)
- Description parsing: RMA reason, node name, diagnostic links
- Latest comments extraction (most recent 3)
- Grafana dashboard links (built from k8s node name, no API call)
- Node history: all tickets for a service tag/hostname over time
- `[h]` shortcut after viewing a ticket to jump to node history
- `[b]` back, `[m]` main menu, `[q]` quit navigation

### v5 — NetBox integration
- Optional NetBox API enrichment (silently skipped if not configured)
- Device lookup by serial (service tag) or name (k8s node name)
- Extracts: device name, status, site, rack, RU position, mgmt IP, role, platform
- Interface listing with cabled peer connections
- `NETBOX_API_URL` + `NETBOX_API_TOKEN` env vars

### v5.1 — Polish & fixes
- Site picker: numbered list of known sites in interactive menu (no more typing from memory)
- Fixed site JQL: switched from `~` (fuzzy) to `=` (exact match) — fuzzy was tokenizing on hyphens
- Grafana fallback chain: node_name -> NetBox device name -> hostname -> service_tag
- k8s node name shown in ticket header: `DO-12345  ● Closed  (g73d28c)`
- Silent env loader (`load_env.sh`) to avoid printing tokens
- Node history `[h]` shortcut now works inline from queue/history drill-down

### v5.2 — DCT UX overhaul
- Merged Jira + NetBox info into one unified node block (no separate "NetBox" section)
- Missing Jira fields (hostname, IP, vendor) auto-filled from NetBox
- Asset tag, model, manufacturer from NetBox device_type
- Connections display: "switch X port Y" format, color-coded by role (BMC/DPU/NIC)
- Comments hidden by default, viewable via `[c]` hotkey (reduces noise)
- URL hotkeys: `[j]` Jira, `[p]` Portal, `[g]` Grafana, `[i]` IB — open in browser
- Project/Type/Assignee moved to top of detail view
- Numbered project picker for "My tickets" (1=DO, 2=HO)
- Option 2 now shows node history (all tickets) instead of jumping to first match
- Removed emojis from field labels for cleaner output
- Menu simplified to 5 options (removed redundant option 6)

### Next: v6 planned features
- Action summary with pre/post checks (derived from summary + comments)
- Ticket age / staleness indicator (from fields.created/updated)
- Location parsing: split rack_location into locode/DH/rack/RU
- Inline node history snippet in detail view
- Docs/SOP shortcut links (placeholders)

## Environment Setup

### Required
```bash
export JIRA_EMAIL="you@example.com"
export JIRA_API_TOKEN="your-jira-token"
```

### Optional (NetBox)
```bash
export NETBOX_API_URL="https://netbox.example.com/api"
export NETBOX_API_TOKEN="your-netbox-token"
```

### Dependencies
```bash
pip3 install requests
```

## Usage

```bash
# Interactive menu
python3 get_node_context.py

# One-shot lookup
python3 get_node_context.py DO-12345
python3 get_node_context.py 10NQ724
python3 get_node_context.py d0001142

# Queue browser
python3 get_node_context.py queue --site US-EAST-03A
python3 get_node_context.py queue --site US-EAST-03A --status verification
python3 get_node_context.py queue --project HO --status all
python3 get_node_context.py queue --mine

# Node history
python3 get_node_context.py history 10NQ724
python3 get_node_context.py history d0001142 --json

# Output formats
python3 get_node_context.py DO-12345 --json
python3 get_node_context.py DO-12345 --raw

# Help
python3 get_node_context.py --help
```

## Jira Custom Field Reference

| Field ID | Name | Example |
|---|---|---|
| `customfield_10193` / `cf[10193]` | Service Tag | `10NQ724` |
| `customfield_10192` / `cf[10192]` | Hostname | `d0001142` |
| `customfield_10194` / `cf[10194]` | Site | `US-EAST-03` |
| `customfield_10207` / `cf[10207]` | Rack Location | `US-BVI01.DC7.R297.RU18` |
| `customfield_10191` / `cf[10191]` | IP Address | `0.0.0.0` |
| `customfield_10210` / `cf[10210]` | Vendor | `Dell` |
| `customfield_10010` | Service Request Info | Portal URL, request type |
| `fields.description` | ADF document | RMA reason, node name, diag links |
| `fields.comment.comments` | Comments | Author, timestamp, body |
| `fields.issuelinks` | Linked Issues | DO <-> HO relationships |

## API Endpoints Used

### Jira Cloud
- `GET /rest/api/3/issue/{issueKey}` — fetch single issue
- `POST /rest/api/3/search/jql` — JQL search (newer endpoint, replaces deprecated GET /search)

### NetBox
- `GET /dcim/devices/?serial=X` — find device by serial
- `GET /dcim/devices/?name=X` — find device by name
- `GET /dcim/interfaces/?device_id=X` — list interfaces for device

## Files

```
cw-node-helper/
├── get_node_context.py   # main script (~860 lines)
├── .env                  # your actual tokens (git-ignored)
├── .env.example          # template (safe to share)
├── .gitignore            # keeps .env out of git
├── load_env.sh           # silent env loader
└── BUILD_LOG.md          # this file
```

## Security Notes

- Never paste API tokens into chat, terminals where output is shared, or source files
- Use `.env` files + `source load_env.sh` to load credentials silently
- Tokens are read from environment variables only — never hardcoded
- `.gitignore` prevents `.env` from being committed

## Future Ideas

- Box Office / Sheriff API (needs Okta access)
- Teleport integration (check if node is SSH-reachable)
- Grafana API (pull actual metrics, not just links)
- Package as a proper CLI with `pip install` support
- Cache NetBox results to reduce API calls
