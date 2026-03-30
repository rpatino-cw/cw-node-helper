# CW Node Helper — Session Log

## Session Date: 2026-02-27

## What was built (from scratch, in one session)

Started with zero code. Ended with a ~900 line interactive CLI tool that pulls data from Jira Cloud + NetBox and presents it in a DCT-friendly format.

## Version History

### v1 — Basic Jira lookup
- Single `get_node_context(identifier)` function
- Fetch DO/HO ticket by Jira key
- Extract summary, status, issue type, project, assignee
- Auth via JIRA_EMAIL + JIRA_API_TOKEN env vars

### v2 — Custom fields + JQL search
- Extracted DCT custom fields: service tag, hostname, site, rack, IP, vendor
- JQL search by service tag or hostname across DO + HO
- Fixed deprecated Jira search API (GET /search → POST /search/jql)
- Fixed "DO" as reserved JQL word (needs quoting)
- Linked issue extraction (DO ↔ HO)

### v3 — Clean output + argparse
- Switched to argparse for -h/--help/--json/--raw
- Multi-match selection prompt
- Color + icons terminal output
- Jira URLs + portal links

### v4 — Interactive menu + queue browser
- Interactive menu loop (no args = menu)
- Queue browser with site + status filters
- --mine flag, --project DO/HO
- Description parsing: RMA reason, node name, diagnostic links
- Comments extraction
- Grafana dashboard links (from k8s node name)
- Node history (all tickets for a service tag/hostname)
- Navigation: [b] back, [m] menu, [h] history, [q] quit

### v5 — NetBox integration
- Optional NetBox API (silently skipped if not configured)
- Device lookup by serial or name
- Extracts: device name, asset tag, site, rack, position, mgmt IP, role, model, manufacturer
- Interface listing with cabled peer connections
- Classified by role: BMC, DPU, NIC

### v5.1 — Polish
- Site picker (numbered list instead of typing)
- Fixed JQL site filter (= exact match, not ~ fuzzy)
- Grafana fallback: node_name → NetBox device → hostname → service tag
- k8s node name in ticket header
- Silent env loader (load_env.sh)
- History [h] works inline from queue/history drill-down

### v5.2 — DCT UX overhaul
- Merged Jira + NetBox into one unified node info block
- Missing fields auto-filled from NetBox (hostname, IP, vendor)
- Asset tag, model, manufacturer from NetBox device_type
- Connections: "switch X port Y" format, color-coded BMC/DPU/NIC
- Comments hidden by default, [c] hotkey to view
- URL hotkeys: [j] Jira, [p] Portal, [g] Grafana, [i] IB
- Project/Type/Assignee at top of detail view
- Numbered project picker for My Tickets
- Option 2 = Node info (shows history, not single ticket)
- Menu simplified to 5 options

## Data Sources

### Jira Cloud (required)
- Base URL: https://your-org.atlassian.net
- Auth: JIRA_EMAIL + JIRA_API_TOKEN (basic auth)
- Endpoints:
  - GET /rest/api/3/issue/{key}
  - POST /rest/api/3/search/jql

### Jira Custom Fields
| Field ID | Name | Example |
|---|---|---|
| cf[10193] | Service Tag | S948338X5608239 |
| cf[10192] | Hostname | S029489 |
| cf[10194] | Site | US-SITE-01A |
| cf[10207] | Rack Location | US-SITE01.DH1.R248.RU18 |
| cf[10191] | IP Address | 0.0.0.0 |
| cf[10210] | Vendor | Supermicro |
| cf[10010] | Service Request Info | Portal URL |

### NetBox (optional)
- URL: https://netbox.example.com/api (production)
- Auth: Token-based (NETBOX_API_TOKEN)
- Endpoints:
  - GET /dcim/devices/?serial=X or ?name=X
  - GET /dcim/interfaces/?device_id=X

### Known Sites (from real tickets)
- US-SITE-01A (Site A)
- US-EAST-03
- US-EAST-03A
- US-PHX01 (Phoenix)
- US-QNC01 (Quincy)
- US-RIN01
- US-EWS01

## Key Decisions & Fixes
- Jira deprecated GET /rest/api/3/search → switched to POST /rest/api/3/search/jql
- "DO" is a reserved JQL word → must be quoted as "DO"
- Site filter: switched from ~ (fuzzy) to = (exact match) because ~ tokenizes on hyphens
- NetBox URL: production is netbox.example.com, NOT the dev instance
- Python 3.9 compatibility: added `from __future__ import annotations` for str | None syntax
- Grafana dashboards key off k8s node name, not hostname → use var-search parameter

## Files
```
cw-node-helper/
├── get_node_context.py   # main script (~900 lines)
├── .env                  # tokens (git-ignored)
├── .env.example          # template
├── .gitignore
├── load_env.sh           # silent env loader
├── BUILD_LOG.md          # feature history
└── SESSION_LOG.md        # this file
```

## Planned for v6 (next session)
1. Action summary with pre/post checks (derived from summary + comments keywords)
2. Ticket age / staleness indicator (from fields.created/updated)
3. Location parsing: split rack_location (US-SITE01.DH1.R64.RU34) into locode/DH/rack/RU
4. Inline node history snippet in detail view
5. Dual-homing hints in connections (A side / B side)
6. Docs/SOP shortcut links (placeholders)

## Security Notes
- User leaked tokens in chat twice — reminded to revoke and regenerate
- .env is git-ignored
- load_env.sh loads vars silently (no echo)
- Never hardcode tokens in source
