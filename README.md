# cwhelper

DCT terminal companion -- Jira + NetBox + Grafana queue browser for data center operations.

![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![Version](https://img.shields.io/badge/version-6.5.0-orange)
![License](https://img.shields.io/badge/license-MIT-green)

---

## Quick Start

### One-liner install

```bash
curl -sL https://raw.githubusercontent.com/rpatino-cw/cw-node-helper/main/install.sh | bash
```

This clones the repo, installs the package, and runs the setup wizard.

### Manual install

```bash
git clone https://github.com/rpatino-cw/cw-node-helper.git
cd cw-node-helper
pip install -e .
cwhelper setup    # interactive credential wizard
cwhelper          # launch
```

### What you'll need

- Python 3.10+
- A Jira API token ([generate one here](https://id.atlassian.com/manage-profile/security/api-tokens))
- Your Jira email address
- (Optional) NetBox API URL and token

---

## Usage

```
cwhelper                          # interactive menu
cwhelper DO-12345                 # look up a ticket
cwhelper 10NQ724                  # search by service tag
cwhelper queue --site US-EAST-03  # browse queue
cwhelper setup                    # credential wizard
cwhelper config                   # view/toggle features
cwhelper -h                       # full help
```

At the interactive menu, type any ticket key, service tag, or hostname directly -- no submenu needed.

---

## Features

All features can be toggled on/off individually:

```bash
cwhelper config                   # see what's enabled
cwhelper config --enable queue    # enable one feature
cwhelper config --enable-all      # enable everything
```

| Feature | Command | What it does |
|---------|---------|-------------|
| Ticket lookup | `DO-12345` | Full ticket context: Jira + NetBox + Grafana links |
| Queue browser | `queue` | Filter by site, status, project (DO/HO/SDA) |
| Node history | `history` | All tickets for a device by service tag or hostname |
| Shift brief | `brief` | AI-generated priority summary for your shift |
| Verification | `verify` | Guided verification flows (IB, BMC, power, etc.) |
| Queue watcher | `watch` | Background poller with alerts for new tickets |
| Rack report | `rack-report` | Tickets grouped by rack location |
| IB trace | `ibtrace` | InfiniBand connection tracing from topology data |
| Code quiz | `learn` | Study mode for DCT procedures |

Interactive-only features: rack map, bookmarks, bulk start, walkthrough, activity log, AI chat.

---

## Ticket View Hotkeys

After opening a ticket, these hotkeys are available:

| Key | Action | Key | Action |
|-----|--------|-----|--------|
| `r` | Rack map | `s` | Start work |
| `n` | Connections | `v` | Verification |
| `l` | Linked tickets | `y` | On hold |
| `d` | Diagnostics | `z` | Resume |
| `c` | Comments | `k` | Close |
| `e` | Rack view | `j` | Open in Jira |
| `f` | MRB/Parts | `g` | Open Grafana |
| `b` | Back | `q` | Quit |

---

## Configuration

Credentials are stored in `.env` (gitignored, never committed):

```bash
cwhelper setup   # interactive wizard — recommended
# or manually:
cp .env.example .env
# edit .env with your values
```

Features are stored in `.cwhelper_state.json` (auto-created):

```bash
cwhelper config                    # view all features
cwhelper config --enable queue     # enable a feature
cwhelper config --disable queue    # disable a feature
cwhelper config --enable-all       # enable everything
cwhelper config --json             # machine-readable output
```

---

## Project Structure

```
cwhelper/
  cli.py            CLI entry point and subcommand routing
  config.py         Constants, feature flags, API config
  clients/          API clients (Jira, NetBox, Grafana)
  services/         Business logic (queue, search, AI, verification)
  tui/              Terminal UI (menu, display, settings, actions)
test_integrity.py   Unit tests (176 tests, all API calls mocked)
install.sh          One-command installer
```

---

## Development

```bash
python3 test_integrity.py          # run tests
python3 test_map.py                # rack visualization tests
```

All tests mock API calls -- no real Jira/NetBox requests needed.

---

MIT
