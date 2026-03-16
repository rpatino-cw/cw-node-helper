# CW Node Helper

CoreWeave DCT terminal companion — Jira + NetBox + Grafana queue browser.
Single-file Python CLI for data center operations ticket management.

## Quick Reference

```bash
# Run
source load_env.sh && python3 get_node_context.py

# Run (if installed as package)
source load_env.sh && cwhelper

# Test
python3 test_integrity.py
python3 test_map.py

# Install as editable package
pip install -e .
```

## Tech Stack

- **Language:** Python 3.10+
- **Dependencies:** `requests>=2.28.0` (only runtime dep)
- **Optional:** `openpyxl` (Excel cutsheet processing only)
- **APIs:** Jira Cloud, NetBox, Grafana (URL generation only)

## Project Structure

```
get_node_context.py      # Main app (monolith, ~7k lines)
test_integrity.py        # Unit tests (74 tests, all API calls mocked)
test_map.py              # Rack visualization math tests
pyproject.toml           # Package config, entry point: cwhelper
requirements.txt         # requests>=2.28.0
load_env.sh              # Loads .env into shell
update.sh                # Self-updater script
.env                     # Credentials (gitignored, never commit)
.env.example             # Credential template
.cwhelper_state.json     # User state: bookmarks, recents (auto-created)
dh_layouts.json          # Data hall rack configs (auto-created)
ib_topology.json         # InfiniBand port mappings (pre-generated, read-only)
docs/                    # Documentation
site/                    # Visual docs website
source/                  # Reference Excel cutsheets (gitignored)
.github/workflows/       # CI (test on push) + release (tag → zip)
```

## Package Layout (cwhelper/)

`get_node_context.py` is now a backward-compat shim. All code lives in the `cwhelper/` package.

```
cwhelper/
  config.py         (~475)  constants, globals, ANSI colors, feature flags, radar statuses
  cache.py          (~147)  TTL cache, JQL escape, IB topology lookup
  state.py          (~245)  .cwhelper_state.json read/write
  cli.py            (~267)  argparse + main() entry point
  clients/
    jira.py         (~503)  Jira Cloud REST API
    netbox.py       (~377)  NetBox API
    grafana.py       (~82)  Grafana URL generation only
  services/
    ai.py           (~874)  Claude/OpenAI AI features
    bookmarks.py    (~278)  bookmark CRUD
    brief.py        (~392)  AI shift brief with radar HO integration
    context.py      (~890)  ticket context building, field extraction, prep brief
    notifications.py(~128)  ntfy push notifications
    queue.py        (~678)  queue browser, stale verification
    rack.py        (~1088)  rack ASCII visualization, DH maps
    radar.py        (~225)  HO radar dashboard — pre-DO awareness
    search.py       (~209)  JQL search, history search
    session_log.py  (~549)  session event logging
    walkthrough.py (~1415)  data hall walkthrough mode (candidate for sub-package)
    watcher.py      (~660)  background ticket + HO radar watcher threads
    weekend.py      (~151)  weekend auto-assign logic
  tui/
    actions.py      (~600)  action panel + ticket detail hotkey loop
    cab_view.py     (~115)  cabinet rack view (_run_cab_view)
    connection_view.py(~270) HO/MRB/SDx lookups, network cable display
    display.py      (~650)  core screen utils, ticket pretty-print
    menu.py         (~966)  main interactive menu loop
    rack_helpers.py (~270)  rack conflict checks, bulk grab/hold/link
    rich_console.py (~521)  shared Rich console instance
```

## Layer Architecture

```
cli.py → tui/menu.py → tui/actions.py
                     → services/queue.py
                     → services/walkthrough.py
tui/actions.py       → tui/rack_helpers.py  (rack conflict logic)
                     → tui/cab_view.py       (cabinet view)
                     → tui/connection_view.py (HO/MRB/SDx/cable display)
                     → services/*            (business logic)
                     → clients/*             (API calls)
tui/display.py       ← imported by most tui + service modules
clients/*            ← leaf layer, no imports from tui or services
```

## Display Layer

All TUI rendering uses **Python Rich** (`cwhelper/tui/rich_console.py`). Do not use raw ANSI print statements for new display code.

- Use `console` (the shared `Console` instance from `rich_console.py`) for all output
- Follow **POSIX CLI conventions** and **keyboard-driven navigation** throughout
- Ticket detail header answers the 3 DCT questions in order: **Where → What to do → Which device**
- New menus: use `_rich_print_menu()` with options as `list[tuple[key, label, hint]]`
- New queue displays: use `_rich_print_queue_table()` + `_rich_queue_prompt()`
- Status styles: use `_rich_status(status_name)` → returns `(style, dot_char)`

## Conventions

- **All functions are private** (`_function_name` prefix)
- **Constants:** `UPPER_CASE` (e.g., `JIRA_BASE_URL`, `CUSTOM_FIELDS`)
- **Context dict:** `ctx` — passed through functions with ticket/node data
- **Caching:** In-memory dicts with 60s TTL (`_issue_cache`, `_jql_cache`)
- **Error handling:** Graceful degradation — NetBox down = Jira-only mode
- **No exceptions to user** — all caught and printed as warnings
- **Env vars for config** — credentials via `JIRA_EMAIL`, `JIRA_API_TOKEN`, `NETBOX_API_URL`, `NETBOX_API_TOKEN`

## CLI Modes

1. **Interactive menu** — no args, TUI with hotkeys
2. **One-shot subcommands** — `queue`, `history`, `watch`, `weekend-assign`, or pass a ticket ID directly
3. **Common flags:** `--site`, `--status`, `--project`, `--json`, `--limit`

## Testing

- All tests mock API calls — no real Jira/NetBox requests
- CI runs on Python 3.9, 3.11, 3.13
- Run `python3 test_integrity.py` before any release

## Important Files to Never Commit

- `.env` / `.env.local` — credentials
- `.cwhelper_state.json` — personal state
- `source/` — large Excel binaries

## Good Practice Feedback

Claude will flag good or bad practices as they come up during development. Feedback will appear inline as a short note before or after the relevant change.

### Format

> **Practice:** [Good / Caution / Bad]
> **What:** Brief description of what was done
> **Why:** Why it matters in this codebase or generally
> **Advice:** What to do instead (if applicable)

### Standing Rules for This Project

**Good practices to reinforce:**
- Mocking all API calls in tests — keeps tests fast and offline-safe
- Using `_private_prefix` for internal functions — consistent with project convention
- Caching API responses with TTL — reduces rate limit risk on Jira/NetBox
- Graceful degradation (Jira-only mode when NetBox is down) — resilient for ops use
- Keeping credentials in `.env` and gitignored — never hardcoded

**Practices to flag:**
- Hardcoding ticket IDs, URLs, or credentials anywhere in the code
- Adding real API calls inside test files
- Raising unhandled exceptions to the user (tracebacks)
- Growing `get_node_context.py` with unrelated logic — consider if a new module is warranted
- Committing state files like `.cwhelper_state.json` or `dh_layouts.json`
- Skipping `.env.example` updates when new env vars are added
