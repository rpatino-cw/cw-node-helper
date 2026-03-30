# Developer Guide — cw-node-helper

Quick reference for understanding and working on the codebase.

## File Structure

```
cw-node-helper/
├── get_node_context.py     # Main app (~4,400 lines)
├── load_env.sh             # Silent .env loader
├── test_map.py             # Rack math tests
├── test_integrity.py       # Menu, hotkeys, panels, transitions (74 tests)
├── watch_queue.sh          # Legacy bash watcher (superseded by built-in)
├── pyproject.toml          # Package config (pip install)
├── requirements.txt        # Python deps
├── .env                    # YOUR credentials (never share/commit)
├── .env.example            # Template for new users
├── .gitignore
├── .cwhelper_state.json    # Auto-created: recents, bookmarks
├── dh_layouts.json         # Auto-created: data hall configs
└── docs/                   # Guides & logs (safe to delete)
    ├── DEV_GUIDE.md        # This file
    ├── FUTURE_PLANS.md     # Roadmap
    ├── BUILD_LOG.md        # Feature history
    ├── SESSION_LOG.md      # Dev session notes
    ├── UPDATE_LOG_*.md     # Update notes
    └── .env.security-note  # Token rotation instructions
```

## What's safe to delete vs what breaks the app

### DO NOT DELETE — app will break
| File | Why |
|------|-----|
| `get_node_context.py` | **The entire app.** Delete this = nothing works. |
| `.env` | Your API credentials. Without it the app can't talk to Jira or NetBox. |
| `load_env.sh` | Loads `.env` into your shell. Without it you have to export vars manually. |

### DO NOT DELETE — but app still runs without them
| File | What happens if deleted |
|------|----------------------|
| `pyproject.toml` | Can't `pip install -e .` or run as `cwhelper`. Still works via `python3 get_node_context.py`. |
| `requirements.txt` | Loses dependency documentation. App still runs if `requests` is already installed. |
| `.env.example` | New users won't have a credential template. Your app still works fine. |
| `.gitignore` | Risk of accidentally committing secrets. App unaffected. |

### SAFE TO DELETE — auto-recreated by the app
| File | What happens |
|------|-------------|
| `.cwhelper_state.json` | Loses your bookmarks and recent history. Recreated on next run. |
| `dh_layouts.json` | Loses saved data hall configs. You'll be prompted to re-enter them. |

### SAFE TO DELETE — no impact on app at all
| File/Folder | What it is |
|-------------|-----------|
| `docs/` (entire folder) | Guides, logs, roadmap. Reference material only. |
| `test_map.py` | Rack math tests. Only needed if you're changing rack visualization code. |
| `test_integrity.py` | Menu/hotkey/panel integrity tests. Only needed during development. |
| `watch_queue.sh` | Legacy bash watcher. Fully replaced by built-in `python3 get_node_context.py watch`. |

## Code Map (get_node_context.py, top to bottom)

| Lines | Section | Key Functions |
|-------|---------|---------------|
| 1–140 | **Constants & globals** | Imports, ANSI colors, JIRA_BASE_URL, KNOWN_SITES, cache dicts, watcher state |
| 141–200 | **Utility helpers** | `_escape_jql`, `_classify_port_role`, `_cache_put`, `_request_with_retry`, `_brief_pause` |
| 201–350 | **Auth & HTTP** | `_get_credentials`, `_jira_get/post/put`, `_get_my_account_id`, `_grab_ticket`, `_handle_response_errors`, `_jira_get_issue` |
| 351–590 | **NetBox API** | `_netbox_get`, `_netbox_find_device`, `_netbox_get_interfaces`, `_netbox_get_rack_devices`, `_build_netbox_context` |
| 591–840 | **Field extraction** | `_extract_custom_fields`, `_extract_linked_issues`, `_extract_description_details`, `_extract_comments`, `_adf_to_plain_text`, `_parse_rack_location` |
| 841–980 | **JQL search** | `_jql_search` (with TTL cache), `_search_by_text`, `_search_queue` |
| 981–1160 | **Context building** | `_build_grafana_urls`, `_build_context` (merges Jira + NetBox + SLA + Grafana) |
| 1161–1440 | **Queue & history** | `_run_queue_interactive`, `_search_node_history`, `_run_history_interactive`, `_run_history_json`, `_run_queue_json` |
| 1441–1690 | **Queue watcher** | `_run_queue_watcher` (foreground), `_background_watcher_loop` (daemon thread), `_start/stop_background_watcher` |
| 1691–2000 | **Display handlers** | `_print_connections_inline`, `_print_linked_inline`, `_print_diagnostics_inline`, `_show_mrb_for_node`, `_show_sdx_for_ticket` |
| 2001–2280 | **Rack views** | `_print_rack_neighbors`, `_print_netbox_info_inline`, `_handle_rack_neighbors`, `_handle_rack_view` |
| 2281–2380 | **Action panel** | `_print_action_panel` (renders View / Actions / Open / Nav button groups) |
| 2381–2640 | **Bookmarks** | `_manage_bookmarks`, `_add_bookmark_wizard`, `_remove_bookmark_wizard`, `_rename_bookmark_wizard` |
| 2641–2860 | **Post-detail prompt** | `_post_detail_prompt` (handles all ticket-view hotkeys: a, r, n, l, c, d, e, j, p, g, i, t, x, *, b, m, h) |
| 2861–2980 | **UI basics** | `_clear_screen`, `_print_banner`, `_print_help`, `_ask_site`, `_ask_queue_filters` |
| 2981–3600 | **Interactive menu** | `_interactive_menu` (main loop: 9 options + bookmarks a-e + watcher + direct input) |
| 3601–3720 | **State persistence** | `_load/save_dh_layouts`, `_load/save_user_state`, `_record_ticket_view`, `_record_node_lookup`, `_add/remove_bookmark` |
| 3721–4020 | **DH map viz** | `_get_dh_layout`, `_setup_dh_layout`, `_draw_mini_dh_map` (animated ASCII serpentine map) |
| 4021–4210 | **Rack elevation** | `_fetch_device_type_heights`, `_draw_rack_elevation` (side-view rack diagram) |
| 4211–4360 | **Output formats** | `_print_pretty`, `_print_json`, `_print_raw` |
| 4361–4530 | **CLI entry** | `main()`, `_print_cli_help`, `_cli_queue`, `_cli_history`, `_cli_watch`, `_cli_lookup` |

## Data Flow

```
User input
  │
  ├─ Jira key (DO-12345) ──→ _jira_get_issue() [cached] ──→ _build_context()
  │                                                              │
  ├─ Text (tag/hostname) ──→ _search_by_text() ──→ pick ──→ _jira_get_issue()
  │                                                              │
  └─ Queue browse ──→ _search_queue() [cached] ──→ pick ──→ _jira_get_issue()
                                                                 │
                                                          _build_context()
                                                           ├── _extract_custom_fields()
                                                           ├── _extract_description_details()
                                                           ├── _build_netbox_context() [parallel thread, cached]
                                                           ├── _build_grafana_urls()
                                                           └── _fetch_sla() [parallel thread]
                                                                 │
                                                          _print_pretty(ctx)
                                                                 │
                                                          _post_detail_prompt(ctx)
                                                           └── hotkey dispatch (j/p/g/i/r/n/c/...)
```

## Key Caches

| Cache | Key | Max Size | TTL | Purpose |
|-------|-----|----------|-----|---------|
| `_issue_cache` | Jira issue key | 100 | None (process lifetime) | Avoid re-fetching same ticket |
| `_netbox_cache` | `"serial\|node\|host"` | 50 | None (process lifetime) | Avoid re-querying NetBox for same device |
| `_jql_cache` | `"jql\|max\|fields"` | 200 | 60 seconds | Avoid re-running identical JQL within 1 min |

All caches use `_cache_put()` with LRU eviction (oldest entry removed when full).

## Jira Custom Field IDs

| Field ID | Name | Example | Used For |
|----------|------|---------|----------|
| `cf[10192]` | Hostname | `d0001142` | k8s node name |
| `cf[10193]` | Service Tag | `S948338X5A04781` | Hardware serial |
| `cf[10194]` | Site | `US-SITE-01A` | Data center location |
| `cf[10207]` | Rack Location | `US-SITE01.DH1.R64.RU34` | Physical position |
| `cf[10191]` | IP Address | `10.0.0.1` | Management IP |
| `cf[10210]` | Vendor | `Dell` | Hardware manufacturer |

## Conventions

- All internal functions prefixed with `_` (not part of public API)
- ANSI color constants: `BOLD`, `DIM`, `CYAN`, `GREEN`, `YELLOW`, `RED`, `MAGENTA`, `WHITE`, `RESET`
- User input in JQL always escaped with `_escape_jql()` to prevent injection
- State files written with `chmod 0o600` for security
- HTTP calls use `_request_with_retry()` (2 retries, 1s/2s backoff, only on 5xx/connection errors)
- `_ANIMATE` global (from `CWHELPER_ANIMATE` env var) controls terminal animation

## Running

```bash
# Interactive mode
source load_env.sh && python3 get_node_context.py

# One-shot lookup
python3 get_node_context.py DO-12345

# Queue browse (CLI)
python3 get_node_context.py queue --site US-SITE-01A --status open --json

# Node history
python3 get_node_context.py history S948338X5A04781

# Installable (after pip install -e .)
cwhelper
```

## Testing

```bash
python3 test_map.py              # Rack math tests (no API calls)
python3 test_integrity.py -v     # Menu, hotkeys, panels, transitions (74 tests, no API calls)
python3 -c "import py_compile; py_compile.compile('get_node_context.py', doraise=True)"  # Syntax check
```

### What `test_integrity.py` covers

| Area | What's tested |
|------|---------------|
| Action panel buttons | All view/action/transition/nav buttons show/hide based on context |
| Transition routing | Fuzzy-match resolution for start/verify/hold/resume/close |
| Detail view hotkeys | Navigation (b/m/q/h), inline toggles (c/w/d/r), browser opens (j), bookmarks (*) |
| Helper functions | `_is_mine`, `_text_to_adf`, `_parse_rack_location`, `_escape_jql`, `_classify_port_role` |
| Bookmarks | Add/remove/dedup, max 5 cap, suggestion dedup |
| Config integrity | Custom fields, search projects, TRANSITION_MAP completeness |
