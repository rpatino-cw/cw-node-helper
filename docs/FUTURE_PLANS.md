# CW Node Helper — Future Plans

## Current State (v5.2)

A ~900-line single-file Python CLI that serves as a DCT-first companion for daily DO/HO workflow. Built in one session, already functional and opinionated.

**What exists today:**
- Interactive TUI with 6 menu options (lookup, node info, DO/HO queue, my tickets, watch)
- Jira Cloud integration: ticket lookup, JQL search, custom field extraction, comments, linked issues
- NetBox integration: device info, rack/site, interfaces, cabled peer connections (BMC/DPU/NIC)
- Grafana/IB dashboard URL generation (no API call, built from k8s node name)
- Queue watcher with macOS notifications and inline ticket drill-down
- One-shot CLI mode with `--json` output for scripting
- URL hotkeys: `[j]` Jira, `[p]` Portal, `[g]` Grafana, `[i]` IB, `[c]` Comments

**Files:**
```
cw-node-helper/
├── get_node_context.py   # monolith (~900 lines)
├── load_env.sh           # silent env loader
├── watch_queue.sh         # standalone bash watcher (superseded by built-in watch)
├── .env / .env.example   # credentials
├── .gitignore
├── BUILD_LOG.md
└── SESSION_LOG.md
```

**Unique positioning:** No other internal tool combines ticket queues + node context + one-key drill-downs in a personal terminal app. Sheriff is web-based and team-oriented. cw-fleet-tools targets FRO/Fleet. DCT Command Portal is network switch commands via Windmill. This is the only DCT-owned, laptop-first, Jira+NetBox+Grafana queue browser.

---

## Phase 1 — v6: Feature Completion (Next Session)

Finish the features that make the tool genuinely better than Jira UI for daily DCT work.

### 1.1 Ticket Intelligence
- **Ticket age / staleness indicator** — Parse `fields.created` and `fields.updated`, show "3d old, last touched 6h ago" in the detail view. Color-code: green (<24h), yellow (1-3d), red (>3d stale).
- **Action summary** — Derive a one-line "what to do" hint from ticket summary + latest comments. Pattern-match keywords like "reseat", "replace", "RMA", "cable check", "BMC reset" to surface the likely next physical action.
- **Inline node history snippet** — When viewing a ticket detail, show "This node has N other tickets (2 open, 5 closed)" without leaving the view. Quick context on repeat offenders.
- **Similar tickets** — Show tickets related to the one being viewed. The Jira Cloud API has no built-in similarity endpoint (the ML-powered "similar requests" panel in the UI is not exposed via REST). Build our own using multi-field JQL queries:
  1. Match by **same hostname** (`cf[10192]`) or **same service tag** (`cf[10193]`) — strongest signal, same physical node.
  2. Match by **same rack location** (`cf[10207]`) — nearby hardware, potential shared root cause (power, switch, cabling).
  3. Match by **summary text keywords** (`text ~ "keyword"`) — extract key terms from the source ticket's summary (e.g. "BMC unreachable", "disk fault", "GPU ECC").
  4. Deduplicate and rank: exact field matches > same rack > text matches. Exclude the source ticket itself and already-linked issues.
  5. Display as a `[s] Similar (N)` hotkey in the action panel, showing ticket key + summary + status + age.
  - **Note:** JQL fuzzy/proximity/boost operators (`~`, `^`) are silently ignored in Jira Cloud — only basic text contains (`~`) works. Relevance-based sort is also unavailable (tracked as `JRACLOUD-80173`), so ranking must be done client-side.

### 1.2 Menu Additions (Low-Effort, High-Impact)

**7. Settings / Defaults** — Let users set once instead of re-answering each time:
- Default **site** (e.g. `US-SITE-01A`)
- Default **project** (DO/HO)
- Default **poll interval** for watch
- Maybe toggle "remember last menu selection"

Saves as `~/.cwhelper.yaml` or similar. Falls back to built-in defaults if no file exists.

**8. Site summary (status counts)** — Quick high-level view before diving into queues:
```text
8  Site summary      (status counts for a site)

US-SITE-01A – DO tickets
  Open            12
  In Progress      7
  Verification    15
  Waiting Support  3
  Closed         140
```
Uses existing `_jql_search` with grouped status queries. Gives a snapshot of queue health without scrolling through individual tickets. Optionally show "My vs Everyone" counts.

### 1.3 Improved Connections Display

Current format is flat: `BMC  bmc0  -> switch dh1-bmc-a2-01-r012-us-site-01a port swp13`. Hard to scan when there are 6+ interfaces.

**Proposed format** — group by purpose, add human labels, align columns:
```text
Connections (where this node is plugged in)

  Management / BMC
    Node BMC     -> BMC switch      dh1-bmc-a2-01      rack r012   port swp13
    DPU BMC      -> DPU BMC switch  dh1-dpu-a2-01      rack r012   port swp13

  Data / Fabric
    DPU uplink 0 -> TOR (left)      dh1-t0-a2-01       rack r012   port swp13
    DPU uplink 1 -> TOR (right)     dh1-t0-a2-02       rack r012   port swp13
```

**Key changes:**
- Group by purpose: "Management / BMC" vs "Data / Fabric"
- Shorten switch names: strip site suffix (e.g. `-us-site-01a`)
- Add human labels: "BMC switch", "TOR (left/right)" instead of raw hostnames
- Extract rack from switch name for quick reference
- Align columns so eyes can scan: role -> switch label -> switch name -> rack -> port

**Requires:** Sample real NetBox interface data to validate the switch naming convention at US-SITE-01A. The parsing logic (strip site suffix, detect left/right TOR, extract rack) depends on actual switch hostnames.

### 1.4 Network Topology Map
- **Visual topology view** — ASCII/terminal rendering of how a node connects to the network fabric.
- Starting from a node, show the full path: Node → NVLink/IB switch → Spine → other nodes in the same rail/group.
- Pull connection data from NetBox interfaces + cables API (already partially done in `[n] Connections`).
- Display as a tree or graph:
```text
  Node (dh1-r064-node-01)
  ├── IB0 ─── IB Switch 1 (R63) swp5
  │            ├── node-02 (R64)
  │            ├── node-03 (R64)
  │            └── ... (8 nodes in rail)
  ├── IB1 ─── IB Switch 2 (R65) swp5
  ├── DPU ─── TOR A (R64) swp17
  └── BMC ─── BMC Switch (R64) swp1
```
- Could traverse 2 hops: Node → Switch → peer nodes on that switch.
- Useful for diagnosing fabric issues: "which other nodes share this IB switch?"
- Requires: NetBox API calls to walk `cable → connected_endpoint` for each switch port.
- New hotkey: `[y] Topology` or extend `[n] Connections` with a "show peers" option.

### 1.5 Rack Row Neighbors (DH Map Zoomed View)
- Show the target rack's immediate neighbors in the serpentine row
- Display the full row pair with rack numbers labeled, target bracketed
- Shows the rack directly across the aisle (useful for cable tracing)
- Example:
```text
  R61  R62  R63 [R64] R65  R66  R67  R68  R69  R70   ← Row 7 (L→R)
                  ▲▲
  R80  R79  R78  R77  R76  R75  R74  R73  R72  R71   ← Row 8 (R→L)
```
- Could be appended to DH map footer or its own hotkey
- Uses existing serpentine math from `_draw_mini_dh_map`

### 1.5 Location Parsing
- **Split `rack_location`** into structured components: `US-SITE01.DH1.R64.RU34` → `{locode: "US-SITE01", data_hall: "DH1", rack: "R64", ru: "34"}`. Display as a clean table row instead of raw string.
- **Dual-homing hints** — Identify A-side/B-side from NetBox interface connections. Show which TOR switches the node connects to and flag mismatches.

### 1.5 Polish
- **Docs/SOP shortcut links** — Placeholder `[d]` hotkey that opens relevant Confluence SOP based on ticket type or RMA reason (hardcoded mapping initially).
- **Ticket transition preview** — Show valid Jira transitions for the current ticket status so DCTs know what state to move it to next.

### 1.7 Help & Onboarding

Two layers of help: one for the main menu, one inside the ticket detail view. Each has a standard mode and an "ultra noobie" mode for brand-new DCTs.

#### Main Menu — Noobie Guide (expand existing `?` help)

The existing `_print_help()` already explains menu options 1-9 and hotkeys. Add a prominent prompt at the top:

```text
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    Quick Guide — your organization DCT Node Helper
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  🟢 Brand new? Press [n] for the beginner walkthrough.

  MAIN MENU
  ──────────────────────────────────────────────────────
  1  Lookup ticket ...
```

Pressing `[n]` inside the help screen shows a **Noobie Guide** that explains DCT concepts, not just buttons:
- **What is a DO ticket?** — Data Operations tickets track hardware issues. You get assigned one, go to the rack, do the physical work, update the ticket.
- **What is an HO ticket?** — Hardware Operations tickets, similar but different team/workflow.
- **What is a service tag?** — The physical label on the server (e.g. `S948338X5A04781`). It's the hardware serial number — the most reliable way to identify a node.
- **What is a hostname?** — The k8s node name (e.g. `d0001142`). Software identity. Can change if the node is reimaged.
- **What is NetBox?** — your organization's source of truth for physical infrastructure: what's in each rack, how it's cabled, what switches it connects to.
- **What is Grafana?** — Monitoring dashboards. Shows CPU temp, GPU health, network link status. Use it to check if a node is actually down or just flapping.
- **What is IB (InfiniBand)?** — High-speed interconnect for GPU clusters. IB dashboard shows link health between nodes.
- **What is a rack location?** — Format: `US-SITE01.DH1.R64.RU34` = site.data-hall.rack.rack-unit. Tells you exactly where to walk.
- **Typical workflow** — Start shift → browse queue (option 3) → pick a ticket → read the detail → walk to the rack → do the work → update the ticket.

**Implementation:** Add `_print_noobie_guide()` function. Call it from within `_print_help()` when user presses `[n]`. Page through with ENTER, return to help screen when done.

#### Ticket Detail — `[?]` Button Help

Currently `_post_detail_prompt()` has no `[?]` option. Add one to the Navigation group in `_print_action_panel()`:

```text
  Nav
    [b] Back   [m] Menu   [h] History (S948338X5A04781)   [?] Help
```

Pressing `[?]` shows a context-aware help screen explaining **only the buttons currently visible** (since buttons are conditionally shown based on ticket data). Two modes:

**Standard help** (default `[?]`):
```text
  Button Guide
  ──────────────────────────────────────────────────────

  View
    [r] Rack Map      — ASCII map of the data hall with your rack highlighted
    [n] Connections    — Shows what switches this node is cabled to (BMC, DPU, NIC)
    [l] Linked         — Related DO/HO tickets linked to this one
    [c] Comments (3)   — Latest 3 comments on the ticket
    [e] Rack View      — Side-view elevation diagram of every device in the rack

  Actions
    [a] Grab           — Assign this ticket to yourself in Jira
    [*] Bookmark       — Save this ticket to your bookmarks bar (a-e)

  Open (browser)
    [j] Jira           — Open the ticket in Jira web UI
    [p] Portal         — Open the service request portal page
    [g] Grafana        — Open node monitoring dashboard
    [i] IB             — Open InfiniBand link health dashboard
    [t] Remote Console — Open BMC remote console via Teleport

  Press [!] for detailed explanations (new to DCT work?)
```

**Ultra noobie help** (press `[!]` from the `[?]` screen):
```text
  Detailed Guide — What These Buttons Actually Do
  ──────────────────────────────────────────────────────

  [r] Rack Map
      Shows a bird's-eye ASCII map of the data hall floor. Your target
      rack is highlighted in yellow with a walking route from the entrance.
      Use this when you need to physically find the rack on the DC floor.
      The racks follow a serpentine (snake) numbering pattern — odd rows
      go left-to-right, even rows go right-to-left.

  [n] Connections
      Lists every cabled interface on this node from NetBox. Shows you:
      - Which BMC switch port to check for IPMI access
      - Which TOR (Top of Rack) switches carry the data traffic
      - Which DPU ports are active
      This is critical for cable checks — if a link is down, you know
      exactly which port on which switch to inspect.

  [a] Grab (assign to me)
      Claims this ticket in Jira by setting the assignee to your account.
      Use this when you're picking up a ticket from the queue. Other DCTs
      will see it's assigned and won't duplicate your work.

  [g] Grafana
      Opens the node monitoring dashboard in your browser. Check this
      BEFORE walking to the rack — if the node shows healthy metrics,
      the issue might have self-resolved. Look for: CPU temp spikes,
      GPU ECC errors, network link flaps, power anomalies.

  [i] IB (InfiniBand)
      Opens the InfiniBand fabric dashboard. GPU nodes use IB for
      high-speed inter-node communication. If IB links are down or
      flapping, the node can't participate in distributed training jobs.
      Check this when the ticket mentions network or fabric issues.

  ...
```

**Implementation:**
- Add `_print_detail_help(ctx)` that reads `ctx` to determine which buttons are visible and only explains those.
- Add `_print_detail_noobie_help(ctx)` for the extended version.
- Handle `[?]` in `_post_detail_prompt()` — clear screen, print help, wait for ENTER or `[!]`, return to ticket detail.
- The help text is static strings (no API calls), so this is purely a display feature.

### 1.8 Visual Guide Site

Build a standalone HTML documentation site that visually explains the app. Ship it as a single `docs/index.html` (or use GitHub Pages) so anyone can open it in a browser.

**Content sections:**

1. **How it works** — Interactive flowchart showing: launch → menu → pick action → API calls → display results. Annotate each step with which functions run and what data flows where.

2. **How to use it** — Walkthrough with terminal screenshots (or ASCII mockups):
   - Starting the app, first-time setup (.env)
   - Looking up a ticket by key
   - Browsing the queue, drilling into a ticket
   - Using hotkeys (r=rack map, n=connections, g=grafana, etc.)
   - Background watcher and notifications
   - Bookmarks

3. **Codebase structure** — Visual map of the single file with colored sections:
   - Top-level architecture diagram (which lines do what)
   - Data flow diagram: user input → search → context build → display
   - Call graph for the main flows (lookup, queue, watcher)
   - Color-coded: blue=HTTP/API, green=parsing, yellow=UI, red=state

4. **File importance guide:**
   - `get_node_context.py` — The entire app (most important)
   - `DEV_GUIDE.md` — Developer reference (second most important)
   - `FUTURE_PLANS.md` — Roadmap
   - `.env` — Credentials (never share)
   - `dh_layouts.json` / `.cwhelper_state.json` — Auto-generated state

5. **How to contribute** — Where to add a new hotkey, how to add a new Jira field, how to add a new NetBox enrichment. Step-by-step with code snippets.

**Tech stack:** Single HTML file with inline CSS/JS (no build step). Use Mermaid.js for diagrams. Keep it self-contained so it works offline.

---

## Phase 2 — Engineering Quality (Ship It)

Turn the working prototype into something you'd put on a resume and other DCTs can install.

### 2.1 Repository Setup
- **Private GitHub repo** in the your organization org (or personal GitHub if org access isn't available yet).
- **README.md** with:
  - One-paragraph description
  - Screenshot/recording of the main flows (menu → queue → detail → Grafana)
  - Install instructions (`python3 -m venv`, `pip install -r requirements.txt`)
  - Configuration (`.env` setup)
  - Example commands
  - Comparison table: "How this differs from Sheriff / DCT Command Portal / cw-fleet-tools"
- **LICENSE** — MIT or Apache 2.0 (check your organization policy for internal tools).
- **requirements.txt** — Pin `requests` version.

### 2.2 Modular Refactor
Split the monolith into focused modules. This is the single biggest "engineering maturity" signal.

```
cw-node-helper/
├── cwhelper/
│   ├── __init__.py
│   ├── cli.py            # argparse, main(), dispatch
│   ├── menu.py           # interactive menu loop, prompts, site picker
│   ├── jira_client.py    # all Jira HTTP calls, JQL builders, field extraction
│   ├── netbox_client.py  # all NetBox HTTP calls, device/interface logic
│   ├── context.py        # _build_context(), merge Jira+NetBox data
│   ├── display.py        # _print_pretty(), colors, formatting
│   ├── urls.py           # Grafana/IB/Portal URL builders
│   ├── watcher.py        # queue watcher, macOS notifications
│   └── config.py         # env var loading, defaults, known sites
├── tests/
│   ├── test_jira_fields.py
│   ├── test_netbox_context.py
│   ├── test_url_builders.py
│   └── test_location_parser.py
├── config.yaml.example   # user defaults (site, project, poll interval)
├── requirements.txt
├── setup.py or pyproject.toml
├── Dockerfile
├── .env.example
├── .gitignore
├── README.md
└── BUILD_LOG.md
```

### 2.3 Config File
Replace hardcoded defaults with a `config.yaml`:
```yaml
defaults:
  site: US-SITE-01A
  project: DO
  poll_interval: 300      # seconds
  queue_limit: 20

grafana:
  base_url: https://grafana.int.example.com
  node_dashboard: your-node-dashboard-uid
  ib_dashboard: your-ib-dashboard-uid

sites:
  - US-SITE-01A
  - US-EAST-03
  - US-EAST-03A
  - US-PHX01
  - US-QNC01
```
Load with `pyyaml`, fall back to built-in defaults if no config file exists.

### 2.4 Tests
Start small — 5-10 tests that cover the pure logic (no API calls):
- `test_extract_custom_fields()` — given a mock Jira fields dict, confirm service tag / hostname / site extraction.
- `test_unwrap_field()` — single-element lists unwrap, empty lists return None, strings pass through.
- `test_build_grafana_urls()` — correct fallback chain (node_name → netbox → hostname → service_tag).
- `test_location_parser()` — "US-SITE01.DH1.R64.RU34" splits correctly.
- `test_status_color()` — "Closed" → green, "In Progress" → yellow, etc.
- `test_queue_jql_builder()` — site + status + mine_only produces correct JQL string.

Run with `pytest`. Add a GitHub Actions workflow later if the repo goes into the org.

### 2.5 Installable Package
```bash
# Instead of:
python3 ~/Documents/Random/cw-node-helper/get_node_context.py

# Users can do:
pip install -e .
cwhelper                   # interactive menu
cwhelper DO-12345          # one-shot lookup
cwhelper queue --site US-SITE-01A --json
```
Use `pyproject.toml` with a `[project.scripts]` entry point.

---

## Phase 3 — Containerization & Dev Environment

This is the "looks polished, shows container fluency" layer.

### 3.1 Dockerfile
```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENTRYPOINT ["python3", "-m", "cwhelper"]
```
- Multi-stage build if dependencies grow.
- Pass credentials via `--env-file .env` at runtime (never bake tokens into the image).

### 3.2 OrbStack / Local Dev
- Use OrbStack to run the Docker image locally on macOS.
- Document the workflow: `orbctl run --env-file .env cwhelper`
- This shows comfort with containers and Linux dev environments without over-engineering.

### 3.3 What to Say About It
> "Built a Python CLI tool for DCT workflow automation. Packaged it as an installable Python package with tests and a Docker image. Developed locally using OrbStack for containerized testing."

---

## Phase 4 — Integrations & API Expansion

These are stretch goals that significantly increase the tool's value.

### 4.1 Grafana API (Metrics + Live IB Status)
- **Requires a Grafana API token** — generate at `https://grafana.int.example.com/org/apikeys` (Viewer role)
  - Add `GRAFANA_API_URL` and `GRAFANA_API_TOKEN` to `.env`
- Pull actual node metrics via Grafana `/api/ds/query`:
  - CPU temp, power draw, link status
  - Show a 1-line health summary: "CPU: 62C | Power: 1.2kW | Links: 8/8 up"
- **Live IB port status** — query the "Expected Backend Neighbors" panel data:
  - Shows ibp0-ibp7 with peer leaf switch + port number + link state (up/down/flapping)
  - Replaces or augments the static cutsheet data with real-time status
  - Critical for debugging: "ibp3 is down → Leaf 1.1 port 8 in R300"
- **Node alerts** — pull active alerts for the node from Grafana alerting API
- Dashboard URL builder already enriched with `var-serial`, `var-rack`, `var-zone`, etc.
  for pre-filled dashboards when opening in browser via `[g]`

### 4.2 Jira Write Actions (Careful)
- **Add comment** — `[a]` hotkey to post a quick comment to the ticket from the terminal.
- **Transition ticket** — Move ticket to next status (e.g. "In Progress" → "Verification") with confirmation prompt.
- **Assign to me** — `[=]` hotkey to self-assign an unassigned ticket.
- Gate all write actions behind a `--read-only` default. Require explicit `--allow-writes` flag or config setting.

### 4.3 Teleport Integration
- Check if a node is SSH-reachable via Teleport (`tsh ls --search=<hostname>`).
- Show "SSH: reachable" / "SSH: unreachable" in the detail view.
- `[s]` hotkey to open a Teleport SSH session to the node.

### 4.4 Box Office / Sheriff API
- If Okta access becomes available, pull ticket metadata from Sheriff/Box Office.
- Enrich the detail view with guided repair steps or active workflow state.

### 4.5 Windmill / DCT Command Portal Bridge
- Your tool is ticket/node-centric. DCT Command Portal is network-switch-centric.
- Possible integration: from a node's detail view, offer `[n]` hotkey to run "show interface" on connected switches via Windmill API.
- This would combine both tools' strengths in one workflow.

---

## Phase 5 — TUI Framework (Long-Term)

If the tool gains traction, upgrade the UI from raw `input()` to a proper TUI.

### 5.1 Rich (Intermediate Step — Pretty Print Upgrade)
Before going full TUI, swap the manual ANSI `print()` calls for [Rich](https://github.com/Textualize/rich). This is a "display layer only" change — no new interactivity, just prettier output.

**What Rich gives you immediately:**
- `Panel` for ticket header (status, assignee, summary)
- `Table` with `box.SIMPLE` for key/value node fields (auto-aligned, overflow-safe)
- `Table` for connections (Type / Interface / Switch / Port)
- Consistent borders, padding, wrapping (long hostnames/URLs won't break layout)
- Clickable hyperlinks in supported terminals (iTerm2, etc.)

**Minimal pattern:**
```python
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box

console = Console()
kv = Table(box=box.SIMPLE, show_header=False, pad_edge=False)
kv.add_column("Key", style="bold", no_wrap=True)
kv.add_column("Value", overflow="fold")
kv.add_row("Site", "US-SITE-01A")
kv.add_row("Service Tag", "S948338X5A04781")
console.print(Panel(kv, title="DO-12345 - On Hold", subtitle="Assignee: Tech A"))
```

**Prerequisite:** Do the modular refactor first (Phase 2.2) so all display logic lives in `display.py`. Then swapping in Rich touches one file instead of hunting through 1800 lines.

**New dependency:** `pip install rich` (add to requirements.txt)

### 5.2 Textual (Full TUI)
- Use [Textual](https://github.com/Textualize/textual) for a full-screen terminal UI with panels, tables, and real-time updates.
- Split-pane: queue list on the left, ticket detail on the right.
- Watch mode becomes a live-updating dashboard instead of polling + printing.
- Mouse support for clicking tickets.

### 5.3 Multi-Site Dashboard
- Show multiple sites simultaneously in a grid view.
- Color-coded queue depth per site (green: <5 open, yellow: 5-15, red: >15).
- Mini NOC view for DCTs managing multiple locations.

---

## Visibility & Career Strategy

### What to Highlight
1. **Problem identification** — You saw a gap in DCT tooling (no personal terminal app for ticket+node context) and built exactly that.
2. **API integration** — Jira Cloud, NetBox, Grafana URL generation, macOS notifications. Multiple data sources unified into one view.
3. **Engineering progression** — v1 through v5.2 shows iterative development: basic API call → custom fields → interactive menu → multi-source enrichment → UX polish.
4. **Practical design** — Every feature maps to an actual DCT workflow step. Not a tech demo — a tool you use every shift.

### Who to Show It To
- **Direct manager** — "I built a tool that saves me X minutes per ticket lookup. Can I share it with the team?"
- **Sheriff / platform engineering teams** — Shows you understand the tooling ecosystem and can contribute.
- **Engineering hiring managers** — Demonstrates self-directed problem solving, API fluency, Python proficiency, and iterative development.

### Milestones That Matter
| Milestone | Signal It Sends |
|---|---|
| Working prototype with README | "I ship things" |
| Modular code + tests | "I write maintainable code" |
| Other DCTs use it | "I build tools people want" |
| Docker + config file | "I understand packaging and deployment" |
| Grafana API or write actions | "I can integrate across systems" |
| Textual TUI | "I care about UX, even in the terminal" |

---

## Priority Order (Recommended)

1. **GitHub repo + README** (1-2 hours) — Instant visibility. Other people can see and try it.
2. **Config file** (30 min) — Removes hardcoded defaults, makes it usable by anyone.
3. **v6 features** (2-3 hours) — Ticket age, action summary, location parsing.
4. **Modular refactor** (3-4 hours) — The big engineering maturity jump.
5. **Tests** (1-2 hours) — Proves the code works, enables confident refactoring.
6. **Docker + pyproject.toml** (1 hour) — Packaging and container experience.
7. **Grafana API** (2-3 hours) — Real metrics in the detail view.
8. **Jira write actions** (2-3 hours) — Comment/transition from terminal.
9. **Textual TUI** (longer project) — Only if the tool gains users.
