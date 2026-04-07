"""CLI entry point and subcommand dispatch."""
from __future__ import annotations

import json
import re
import argparse
import os
import sys

from cwhelper import config as _cfg
from cwhelper.config import *  # noqa: F401,F403
__all__ = ['main', '_print_cli_help', '_cli_queue', '_cli_history', '_cli_watch', '_cli_weekend_assign', '_cli_verify', '_cli_ibtrace', '_cli_lookup']
from cwhelper.clients.jira import _get_credentials, _jira_health_check
from cwhelper.state import _load_user_state, _save_user_state
from cwhelper.services.context import _build_context, _format_age, get_node_context
from cwhelper.services.search import _search_queue
from cwhelper.services.queue import _run_queue_interactive, _run_queue_json, _run_history_interactive, _run_history_json
from cwhelper.services.watcher import _run_queue_watcher
from cwhelper.services.weekend import _weekend_auto_assign
from cwhelper.services.brief import run_shift_brief
from cwhelper.tui.menu import _interactive_menu
from cwhelper.tui.display import _print_pretty, _print_json, _print_raw
from cwhelper.services.verify import run_verify



def _preflight_check():
    """Verify Jira connectivity before entering interactive mode."""
    try:
        email, token = _get_credentials()
    except SystemExit:
        return  # credentials missing — _get_credentials already printed the error
    if not _jira_health_check(email, token):
        jira_host = os.environ.get("JIRA_BASE_URL", "your Jira instance")
        print(f"\n  \033[33m⚠  Cannot reach Jira ({jira_host})\033[0m")
        print(f"     Check your network connection and retry.")
        print(f"     Starting in offline mode — some features will fail.\n")


def _require_feature(feature_id: str) -> bool:
    """Check feature flag; print message and return False if disabled."""
    if not _cfg._is_feature_enabled(feature_id):
        label = _cfg._FEATURE_REGISTRY.get(feature_id, {}).get("label", feature_id)
        print(f"\n  {YELLOW}Feature disabled:{RESET} {label}")
        print(f"  Run {BOLD}cwhelper config{RESET} to enable it.\n")
        return False
    return True


def main():
    """Dispatch: no args = interactive menu, with args = one-shot mode."""
    raw_args = sys.argv[1:]

    # Load persisted feature flags before any dispatch
    _state = _load_user_state()
    _cfg._load_features(_state)

    # No arguments at all → launch interactive menu
    if not raw_args:
        # Check if credentials exist before preflight (avoid double-prompting)
        if os.environ.get("JIRA_EMAIL") and os.environ.get("JIRA_API_TOKEN"):
            _preflight_check()
        elif not os.path.exists(os.path.join(_cfg._PROJECT_ROOT, ".env")):
            print(f"\n  {BOLD}Welcome to CW Node Helper!{RESET}")
            print(f"  {DIM}No .env file found. Let's set up your credentials.{RESET}\n")
            _cli_setup()
        _interactive_menu()
        return

    # -h / --help → print help and exit
    if raw_args[0] in ("-h", "--help"):
        _print_cli_help()
        return

    # "setup" subcommand — interactive credential wizard
    if raw_args[0] == "setup":
        _cli_setup()
        return

    # "update" subcommand — pull latest + reinstall
    if raw_args[0] == "update":
        _cli_update()
        return

    # "doctor" subcommand — health check
    if raw_args[0] == "doctor":
        _cli_doctor()
        return

    # "config" subcommand — feature toggle management
    if raw_args[0] == "config":
        _cli_config(raw_args[1:])
        return

    # "queue" subcommand (one-shot, scriptable)
    if raw_args[0] == "queue":
        if not _require_feature("queue"):
            return
        _cli_queue(raw_args[1:])
        return

    # "history" subcommand
    if raw_args[0] == "history":
        if not _require_feature("node_history"):
            return
        _cli_history(raw_args[1:])
        return

    # "watch" subcommand
    if raw_args[0] == "watch":
        if not _require_feature("watcher"):
            return
        _cli_watch(raw_args[1:])
        return

    # "weekend-assign" subcommand
    if raw_args[0] == "weekend-assign":
        if not _require_feature("weekend_assign"):
            return
        _cli_weekend_assign(raw_args[1:])
        return

    # "brief" subcommand — AI shift priority summary
    if raw_args[0] == "brief":
        if not _require_feature("shift_brief"):
            return
        _cli_brief(raw_args[1:])
        return

    # "verify" subcommand — DCT self-service verification
    if raw_args[0] == "verify":
        if not _require_feature("verify"):
            return
        _cli_verify(raw_args[1:])
        return

    # "rack-report" subcommand — tickets grouped by rack
    if raw_args[0] == "rack-report":
        if not _require_feature("rack_report"):
            return
        _cli_rack_report(raw_args[1:])
        return

    # "ibtrace" subcommand — IB connection trace/lookup
    if raw_args[0] == "ibtrace":
        if not _require_feature("ibtrace"):
            return
        _cli_ibtrace(raw_args[1:])
        return

    # Anything else → one-shot lookup (ticket key, service tag, hostname)
    if not _require_feature("ticket_lookup"):
        return
    _cli_lookup(raw_args)


def _print_cli_help():
    """Print help text for CLI one-shot mode."""
    print(f"""
  CW Node Helper  v{APP_VERSION}

  GETTING STARTED
    cwhelper setup                              # first-time credential wizard
    cwhelper doctor                             # verify environment + connectivity
    cwhelper config --enable-all                # enable all features
    cwhelper update                             # pull latest + reinstall
    cwhelper                                    # launch interactive menu

  USAGE
    cwhelper                                    # interactive menu
    cwhelper <identifier> [options]             # one-shot lookup
    cwhelper queue --site <SITE>                # one-shot queue
    cwhelper brief [--site <SITE>]              # AI shift brief
    cwhelper history <identifier>               # node ticket history
    cwhelper watch --site <SITE>                # live queue watcher
    cwhelper weekend-assign --site <SITE> --group <GROUP>
    cwhelper rack-report --site <SITE>          # tickets per rack
    cwhelper verify <identifier> [--type TYPE]  # verification flows
    cwhelper ibtrace <switch> [port]            # IB connection trace
    cwhelper config                             # feature toggle management

  IDENTIFIER (pick one)
    DO-12345        Jira ticket key (DO or HO)
    10NQ724         Dell service tag
    d0001142        Hostname

  OPTIONS
    --json          Output structured JSON only
    --raw           Output full raw Jira JSON
    -h, --help      Show this help

  QUEUE OPTIONS
    --site, -s      Site to filter (e.g. US-EAST-03). Omit for all sites
    --status        open, closed, verification, "in progress", waiting, all
    --project, -p   DO or HO (default: DO)
    --mine, -m      Only your tickets
    --limit, -l     Max results (default: 20)
    --json          Output queue as JSON

  BRIEF OPTIONS
    --site, -s      Site to pull queue for
    --mine, -m      Prioritize your assigned tickets at the top

  WATCH OPTIONS
    --site, -s      Site to filter (e.g. US-EAST-03)
    --project, -p   DO or HO (default: DO)
    --interval, -i  Seconds between checks (default: 300 = 5 min)
    --weekend-group Jira group for weekend auto-assignment round-robin

  WEEKEND-ASSIGN OPTIONS
    --site, -s      Site to filter (required)
    --group, -g     Jira group name for team roster (required)
    --project, -p   DO or HO (default: DO)
    --dry-run       Show what would be assigned without making changes
    --force         Run even on weekdays (for testing)
    --json          Output results as JSON

  VERIFY OPTIONS
    <identifier>    Jira ticket (DO-96947), hostname, or serial number
    --type, -t      Force flow: ib, bmc, dpu, power, drive, rma (auto-detects from ticket)
    --json          Output structured JSON result

  RACK-REPORT OPTIONS
    --site, -s      Site to filter (e.g. US-EAST-03). Omit for all sites
    --status        open, closed, verification, "in progress", waiting, all
    --project, -p   DO or HO (default: DO)
    --mine, -m      Only your tickets
    --limit, -l     Max tickets to fetch (default: 200)
    --json          Output as JSON

  IBTRACE OPTIONS
    <switch>        Switch name (S8.3.2, L10.1.2-DH2, C1.4) or bare ID (8.3.2)
    [port]          Optional port (22/1, IBP3)
    --dh            Filter by data hall (DH1, DH2)
    --json          Output as JSON

  FEATURE CONFIG
    cwhelper config                             # show all features + status
    cwhelper config --enable queue              # enable a feature
    cwhelper config --disable queue             # disable a feature
    cwhelper config --enable-all                # enable everything
    cwhelper config --disable-all               # disable everything

  EXAMPLES
    cwhelper setup                              # first-time setup
    cwhelper DO-12345                           # lookup a ticket
    cwhelper 10NQ724                            # search by service tag
    cwhelper DO-12345 --json                    # JSON output
    cwhelper queue --site US-EAST-03            # open DO queue
    cwhelper queue --status verification --mine # my verification tickets
    cwhelper history 10NQ724                    # node ticket history
    cwhelper watch --site US-EAST-03            # watch queue for new tickets
    cwhelper rack-report --site US-EAST-03      # tickets per rack
    cwhelper verify DO-96947                    # auto-detect verification flow
    cwhelper ibtrace S8.3.2 22/1               # trace IB connection
""")


def _cli_update():
    """Pull latest code and reinstall."""
    import subprocess
    print(f"\n  {BOLD}CW Node Helper — Update{RESET}\n")

    # git pull
    print(f"  {DIM}Pulling latest...{RESET}", end="", flush=True)
    try:
        result = subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=_cfg._PROJECT_ROOT,
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            if "Already up to date" in result.stdout:
                print(f"\r  {GREEN}{BOLD}Already up to date{RESET}              ")
            else:
                print(f"\r  {GREEN}{BOLD}Updated{RESET}                         ")
                # Show what changed
                for line in result.stdout.strip().split("\n")[-5:]:
                    if line.strip():
                        print(f"    {DIM}{line.strip()}{RESET}")
        else:
            print(f"\r  {YELLOW}Pull failed{RESET} — {result.stderr.strip()[:60]}")
            print(f"  {DIM}Try manually: cd {_cfg._PROJECT_ROOT} && git pull{RESET}")
            return
    except Exception as e:
        print(f"\r  {YELLOW}Pull failed{RESET} — {e}")
        return

    # pip install
    print(f"  {DIM}Reinstalling...{RESET}", end="", flush=True)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-e", ".", "--quiet"],
            cwd=_cfg._PROJECT_ROOT,
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            print(f"\r  {GREEN}{BOLD}Reinstalled{RESET}                     ")
        else:
            print(f"\r  {YELLOW}pip install failed{RESET}")
    except Exception as e:
        print(f"\r  {YELLOW}Reinstall failed{RESET} — {e}")

    print(f"\n  {DIM}Done! Restart cwhelper to use the latest version.{RESET}\n")


def _cli_doctor():
    """Health check — verify environment, credentials, and API connectivity."""
    import platform

    print(f"\n  {BOLD}CW Node Helper — Doctor{RESET}\n")

    checks = []

    # Python version
    py_ver = platform.python_version()
    py_ok = tuple(int(x) for x in py_ver.split(".")[:2]) >= (3, 10)
    checks.append(("Python", py_ver, py_ok, "3.10+ required" if not py_ok else ""))

    # .env file
    env_path = os.path.join(_cfg._PROJECT_ROOT, ".env")
    env_exists = os.path.exists(env_path)
    checks.append((".env file", env_path, env_exists, "Run: cwhelper setup" if not env_exists else ""))

    # Jira credentials
    jira_email = os.environ.get("JIRA_EMAIL", "").strip()
    jira_token = os.environ.get("JIRA_API_TOKEN", "").strip()
    jira_url = os.environ.get("JIRA_BASE_URL", _cfg.JIRA_BASE_URL)
    has_jira_creds = bool(jira_email and jira_token)
    checks.append(("Jira credentials", jira_email or "(not set)", has_jira_creds,
                    "Set JIRA_EMAIL + JIRA_API_TOKEN" if not has_jira_creds else ""))

    # Jira connectivity
    jira_reachable = False
    if has_jira_creds:
        try:
            jira_reachable = _jira_health_check(jira_email, jira_token)
        except Exception:
            pass
    checks.append(("Jira API", jira_url, jira_reachable,
                    "Check URL/token/network" if has_jira_creds and not jira_reachable else
                    "Fix credentials first" if not has_jira_creds else ""))

    # NetBox
    nb_url = os.environ.get("NETBOX_API_URL", "").strip()
    nb_token = os.environ.get("NETBOX_API_TOKEN", "").strip()
    has_nb = bool(nb_url and nb_token)
    nb_reachable = False
    if has_nb:
        try:
            resp = _cfg._session.get(
                f"{nb_url.rstrip('/')}/api/status/",
                headers={"Authorization": f"Token {nb_token}", "Accept": "application/json"},
                timeout=(3, 5),
            )
            nb_reachable = resp.status_code < 400
        except Exception:
            pass
    checks.append(("NetBox API", nb_url or "(not configured)", nb_reachable if has_nb else None,
                    "" if not has_nb else "Check URL/token" if not nb_reachable else ""))

    # KNOWN_SITES
    sites = _cfg.KNOWN_SITES
    checks.append(("Known sites", f"{len(sites)} configured" if sites else "(none)",
                    bool(sites) or None, "Optional: set KNOWN_SITES in .env"))

    # Features
    n_on = sum(1 for v in _cfg.FEATURES.values() if v)
    n_total = len(_cfg.FEATURES)
    checks.append(("Features", f"{n_on}/{n_total} enabled", n_on > 0, "Run: cwhelper config --enable-all"))

    # Print results
    for label, detail, status, fix in checks:
        if status is True:
            icon = f"{GREEN}{BOLD}✓{RESET}"
        elif status is False:
            icon = f"{RED}{BOLD}✗{RESET}"
        else:
            icon = f"{YELLOW}−{RESET}"  # optional/skipped
        print(f"  {icon}  {BOLD}{label:<20}{RESET} {DIM}{detail}{RESET}")
        if fix and status is False:
            print(f"     {YELLOW}→ {fix}{RESET}")

    print()

    # Summary
    fails = sum(1 for _, _, s, _ in checks if s is False)
    if fails == 0:
        print(f"  {GREEN}{BOLD}All checks passed!{RESET}")
    else:
        print(f"  {YELLOW}{fails} issue{'s' if fails != 1 else ''} found.{RESET}")
    print()


def _cli_config(args_list: list):
    """Handle: cwhelper config [--enable X] [--disable X] [--enable-all] [--disable-all]"""
    state = _load_user_state()
    _cfg._load_features(state)

    parser = argparse.ArgumentParser(prog="cwhelper config", add_help=False)
    parser.add_argument("--enable", default=None, help="enable a feature by ID")
    parser.add_argument("--disable", default=None, help="disable a feature by ID")
    parser.add_argument("--enable-all", action="store_true", dest="enable_all")
    parser.add_argument("--disable-all", action="store_true", dest="disable_all")
    parser.add_argument("--json", action="store_true", dest="json_mode")
    args = parser.parse_args(args_list)

    changed = False

    if args.enable_all:
        for fid in _cfg.FEATURES:
            _cfg.FEATURES[fid] = True
        changed = True
    elif args.disable_all:
        for fid in _cfg.FEATURES:
            _cfg.FEATURES[fid] = False
        changed = True
    elif args.enable:
        fid = args.enable
        if fid not in _cfg._FEATURE_REGISTRY:
            print(f"\n  {RED}Unknown feature:{RESET} {fid}")
            print(f"  Valid features: {', '.join(sorted(_cfg._FEATURE_REGISTRY))}\n")
            return
        _cfg.FEATURES[fid] = True
        changed = True
    elif args.disable:
        fid = args.disable
        if fid not in _cfg._FEATURE_REGISTRY:
            print(f"\n  {RED}Unknown feature:{RESET} {fid}")
            print(f"  Valid features: {', '.join(sorted(_cfg._FEATURE_REGISTRY))}\n")
            return
        _cfg.FEATURES[fid] = False
        changed = True

    if changed:
        _cfg._save_features(state)
        _save_user_state(state)

    # Display current state
    if args.json_mode:
        print(json.dumps({fid: _cfg.FEATURES[fid] for fid in sorted(_cfg.FEATURES)}, indent=2))
        return

    print(f"\n  {BOLD}Feature Configuration{RESET}\n")
    for fid in sorted(_cfg._FEATURE_REGISTRY):
        meta = _cfg._FEATURE_REGISTRY[fid]
        enabled = _cfg.FEATURES.get(fid, False)
        status = f"{GREEN}{BOLD} ON{RESET}" if enabled else f"{RED}{BOLD}OFF{RESET}"
        deps = ", ".join(meta.get("deps", [])) or "none"
        cmd = meta.get("cli_cmd") or ""
        menu = ",".join(meta.get("menu_keys", [])) or ""
        hint_parts = []
        if cmd:
            hint_parts.append(f"cli:{cmd}")
        if menu:
            hint_parts.append(f"menu:{menu}")
        hint = f"  {DIM}({', '.join(hint_parts)}){RESET}" if hint_parts else ""
        print(f"    [{status}]  {fid:<20} {meta['label']}{hint}")
    print(f"\n  {DIM}Toggle: cwhelper config --enable <feature> / --disable <feature>{RESET}")
    print(f"  {DIM}        cwhelper config --enable-all / --disable-all{RESET}\n")


def _cli_setup():
    """Interactive setup wizard — creates .env with credentials and tests connectivity."""
    import getpass

    env_path = os.path.join(_cfg._PROJECT_ROOT, ".env")
    existing = {}

    # Read existing .env if present
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                existing[k.strip()] = v.strip().strip('"').strip("'")

    print(f"\n  {BOLD}CW Node Helper — Setup{RESET}\n")
    print(f"  {DIM}This will create a .env file with your credentials.{RESET}")
    print(f"  {DIM}Press ENTER to keep the current value (shown in brackets).{RESET}\n")

    fields = [
        ("JIRA_BASE_URL",    "Jira URL",        "https://your-org.atlassian.net",
         "Your Jira Cloud instance URL"),
        ("JIRA_EMAIL",       "Jira email",       "",
         "The email you log into Jira with"),
        ("JIRA_API_TOKEN",   "Jira API token",   "",
         "Generate at: https://id.atlassian.com/manage-profile/security/api-tokens"),
        ("NETBOX_API_URL",   "NetBox URL",        "",
         "Your NetBox instance URL (optional — skip if you don't use NetBox)"),
        ("NETBOX_API_TOKEN", "NetBox API token",  "",
         "NetBox API token (optional)"),
        ("KNOWN_SITES",      "Known sites",       "",
         "Comma-separated site codes, e.g. US-EAST-03,US-WEST-01 (optional)"),
    ]

    values = {}
    try:
        for key, label, default, hint in fields:
            current = existing.get(key) or os.environ.get(key, "") or default
            display = current if key != "JIRA_API_TOKEN" else ("***" + current[-4:] if len(current) > 4 else current)

            print(f"  {DIM}{hint}{RESET}")
            if key == "JIRA_API_TOKEN" and current:
                raw = getpass.getpass(f"  {BOLD}{label}{RESET} [{display}]: ")
            else:
                raw = input(f"  {BOLD}{label}{RESET} [{display}]: ").strip()

            values[key] = raw if raw else current
            print()
    except (EOFError, KeyboardInterrupt):
        print(f"\n\n  {DIM}Setup cancelled.{RESET}\n")
        return

    # Write .env
    lines = [
        "# CW Node Helper credentials",
        "# Generated by: cwhelper setup",
        "#",
    ]
    for key, _, _, _ in fields:
        val = values.get(key, "")
        if val:
            lines.append(f'{key}="{val}"')
    lines.append("")

    with open(env_path, "w") as f:
        f.write("\n".join(lines))
    try:
        os.chmod(env_path, 0o600)
    except OSError:
        pass

    print(f"  {GREEN}{BOLD}Saved{RESET} → .env")
    print(f"  {DIM}Permissions set to 600 (owner-only read/write){RESET}\n")

    # Test Jira connectivity
    jira_url = values.get("JIRA_BASE_URL", "")
    jira_email = values.get("JIRA_EMAIL", "")
    jira_token = values.get("JIRA_API_TOKEN", "")

    if jira_email and jira_token and jira_url:
        print(f"  {DIM}Testing Jira connection...{RESET}", end="", flush=True)
        os.environ["JIRA_BASE_URL"] = jira_url
        os.environ["JIRA_EMAIL"] = jira_email
        os.environ["JIRA_API_TOKEN"] = jira_token
        _cfg.JIRA_BASE_URL = jira_url
        if _jira_health_check(jira_email, jira_token):
            print(f"\r  {GREEN}{BOLD}Jira OK{RESET}                         ")

            # Auto-discover KNOWN_SITES if not already set
            if not values.get("KNOWN_SITES"):
                print(f"  {DIM}Discovering sites from recent tickets...{RESET}", end="", flush=True)
                try:
                    from cwhelper.clients.jira import _jira_post
                    resp = _jira_post("/rest/api/3/search/jql", jira_email, jira_token, body={
                        "jql": 'project in ("DO","HO") AND cf[10194] is not EMPTY ORDER BY updated DESC',
                        "maxResults": 50,
                        "fields": ["customfield_10194"],
                    })
                    if resp and resp.ok:
                        sites = set()
                        for iss in resp.json().get("issues", []):
                            site_val = iss.get("fields", {}).get("customfield_10194")
                            if isinstance(site_val, dict):
                                site_val = site_val.get("value", "")
                            if isinstance(site_val, str) and site_val.strip():
                                sites.add(site_val.strip())
                        if sites:
                            sorted_sites = sorted(sites)
                            values["KNOWN_SITES"] = ",".join(sorted_sites)
                            # Re-write .env with discovered sites
                            lines = ["# CW Node Helper credentials", "# Generated by: cwhelper setup", "#"]
                            for key, _, _, _ in fields:
                                val = values.get(key, "")
                                if val:
                                    lines.append(f'{key}="{val}"')
                            lines.append("")
                            with open(env_path, "w") as f:
                                f.write("\n".join(lines))
                            os.environ["KNOWN_SITES"] = values["KNOWN_SITES"]
                            print(f"\r  {GREEN}{BOLD}{len(sorted_sites)} sites found{RESET} — saved to .env       ")
                            for s in sorted_sites[:8]:
                                print(f"    {DIM}{s}{RESET}")
                            if len(sorted_sites) > 8:
                                print(f"    {DIM}...and {len(sorted_sites) - 8} more{RESET}")
                        else:
                            print(f"\r  {DIM}No sites found in recent tickets{RESET}              ")
                except Exception:
                    print(f"\r  {DIM}Could not auto-discover sites{RESET}              ")
        else:
            print(f"\r  {YELLOW}Jira unreachable{RESET} — check URL/token/network")
    else:
        print(f"  {DIM}Skipping Jira test — credentials incomplete{RESET}")

    # Test NetBox connectivity
    nb_url = values.get("NETBOX_API_URL", "")
    nb_token = values.get("NETBOX_API_TOKEN", "")
    if nb_url and nb_token:
        print(f"  {DIM}Testing NetBox connection...{RESET}", end="", flush=True)
        os.environ["NETBOX_API_URL"] = nb_url
        os.environ["NETBOX_API_TOKEN"] = nb_token
        try:
            resp = _cfg._session.get(
                f"{nb_url.rstrip('/')}/api/status/",
                headers={"Authorization": f"Token {nb_token}", "Accept": "application/json"},
                timeout=(3, 5),
            )
            if resp.status_code < 400:
                print(f"\r  {GREEN}{BOLD}NetBox OK{RESET}                       ")
            else:
                print(f"\r  {YELLOW}NetBox returned {resp.status_code}{RESET} — check URL/token")
        except Exception:
            print(f"\r  {YELLOW}NetBox unreachable{RESET} — check URL/network")

    # Offer to enable all features
    n_on = sum(1 for v in _cfg.FEATURES.values() if v)
    n_total = len(_cfg.FEATURES)
    if n_on < n_total:
        try:
            enable = input(f"\n  Enable all {n_total} features? [Y/n]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            enable = "n"
        if enable in ("", "y", "yes"):
            for fid in _cfg.FEATURES:
                _cfg.FEATURES[fid] = True
            state = _load_user_state()
            _cfg._save_features(state)
            _save_user_state(state)
            print(f"  {GREEN}{BOLD}All features enabled!{RESET}")
        else:
            print(f"  {DIM}Kept {n_on}/{n_total} features. Change later: cwhelper config{RESET}")

    print(f"\n  {DIM}You're all set! Run:{RESET}")
    print(f"    {BOLD}cwhelper{RESET}                        Launch interactive menu")
    print(f"    {BOLD}cwhelper DO-12345{RESET}               Look up a ticket")
    print(f"    {BOLD}cwhelper doctor{RESET}                 Verify everything works\n")


def _cli_queue(args_list: list):
    """Handle: python3 get_node_context.py queue --site X [--status Y] [--project Z]"""
    parser = argparse.ArgumentParser(prog="get_node_context.py queue")
    parser.add_argument("--site", "-s", default="",
                        help="site filter (e.g. US-EAST-03). Omit for all sites")
    parser.add_argument("--mine", "-m", action="store_true")
    parser.add_argument("--limit", "-l", type=int, default=20)
    parser.add_argument("--status", default="open",
                        help="open, closed, verification, 'in progress', waiting, radar (HO only), all (default: open)")
    parser.add_argument("--project", "-p", default="DO",
                        help="DO or HO (default: DO)")
    parser.add_argument("--json", action="store_true", dest="json_mode")
    args = parser.parse_args(args_list)

    email, token = _get_credentials()

    if args.json_mode:
        _run_queue_json(email, token, args.site, args.mine, args.limit,
                        args.status, args.project.upper())
    else:
        _run_queue_interactive(email, token, args.site, args.mine, args.limit,
                               args.status, args.project.upper())


def _cli_history(args_list: list):
    """Handle: python3 get_node_context.py history <identifier> [--json] [--limit N]"""
    parser = argparse.ArgumentParser(prog="get_node_context.py history")
    parser.add_argument("identifier", help="service tag (10NQ724) or hostname (d0001142)")
    parser.add_argument("--limit", "-l", type=int, default=20)
    parser.add_argument("--json", action="store_true", dest="json_mode")
    args = parser.parse_args(args_list)

    email, token = _get_credentials()

    if args.json_mode:
        _run_history_json(email, token, args.identifier, args.limit)
    else:
        _run_history_interactive(email, token, args.identifier, args.limit)


def _cli_watch(args_list: list):
    """Handle: python3 get_node_context.py watch --site X [--project Y] [--interval N]"""
    parser = argparse.ArgumentParser(prog="get_node_context.py watch")
    parser.add_argument("--site", "-s", default="",
                        help="site filter (e.g. US-EAST-03). Omit for all sites")
    parser.add_argument("--project", "-p", default="DO",
                        help="DO or HO (default: DO)")
    parser.add_argument("--interval", "-i", type=int, default=300,
                        help="seconds between checks (default: 300 = 5 min)")
    parser.add_argument("--weekend-group", default="",
                        help="Jira group for weekend auto-assignment round-robin")
    args = parser.parse_args(args_list)

    email, token = _get_credentials()
    _run_queue_watcher(email, token, args.site, project=args.project.upper(),
                       interval=args.interval,
                       auto_assign_group=args.weekend_group)


def _cli_weekend_assign(args_list: list):
    """Handle: python3 get_node_context.py weekend-assign --site X --group Y"""
    parser = argparse.ArgumentParser(prog="get_node_context.py weekend-assign")
    parser.add_argument("--site", "-s", required=True,
                        help="site filter (e.g. US-EAST-03)")
    parser.add_argument("--group", "-g", required=True,
                        help="Jira group name for team roster")
    parser.add_argument("--project", "-p", default="DO",
                        help="DO or HO (default: DO)")
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would be assigned without making changes")
    parser.add_argument("--force", action="store_true",
                        help="run even on weekdays (for testing)")
    parser.add_argument("--json", action="store_true", dest="json_mode",
                        help="output results as JSON")
    args = parser.parse_args(args_list)

    email, token = _get_credentials()

    results = _weekend_auto_assign(
        site=args.site,
        group_name=args.group,
        email=email,
        token=token,
        project=args.project.upper(),
        dry_run=args.dry_run,
        force_weekend=args.force,
    )

    if args.json_mode:
        print(json.dumps(results, indent=2))
    else:
        if not results:
            print(f"\n  {DIM}No assignments made.{RESET}\n")
        else:
            prefix = "[DRY RUN] " if args.dry_run else ""
            print(f"\n  {BOLD}{prefix}{len(results)} ticket(s) assigned:{RESET}")
            for r in results:
                print(f"    {r['key']}  ->  {r['assigned_to']}  ({r['ts'][:16]})")
            print()


def _cli_brief(args_list: list):
    """Handle: cwhelper brief [--site SITE] [--mine]"""
    parser = argparse.ArgumentParser(prog="cwhelper brief")
    parser.add_argument(
        "--site", "-s", default=os.environ.get("DEFAULT_SITE", ""),
        help="site to pull queue for (uses DEFAULT_SITE env var)",
    )
    parser.add_argument(
        "--mine", "-m", action="store_true",
        help="prioritize your assigned tickets at the top",
    )
    parser.add_argument(
        "--demo", action="store_true",
        help="run with mock data — no credentials needed (for presentations)",
    )
    args = parser.parse_args(args_list)

    if args.demo:
        run_shift_brief(None, None, site=args.site, demo=True)
        return

    email, token = _get_credentials()
    run_shift_brief(email, token, site=args.site, mine_first=args.mine)


def _cli_verify(args_list: list):
    """Handle: cwhelper verify <identifier> [--type TYPE] [--json]"""
    parser = argparse.ArgumentParser(prog="cwhelper verify")
    parser.add_argument("identifier",
                        help="Jira ticket (DO-96947), hostname, or serial number")
    parser.add_argument("--type", "-t", default=None, dest="flow_type",
                        help="Force verification type: ib, bmc, dpu, power, drive, rma")
    parser.add_argument("--json", action="store_true", dest="json_mode",
                        help="Output structured JSON result")
    args = parser.parse_args(args_list)

    run_verify(args.identifier, flow_type=args.flow_type, json_mode=args.json_mode)


def _cli_rack_report(args_list: list):
    """Handle: cwhelper rack-report [--site SITE] [--status STATUS] [--project P] [--json]"""
    parser = argparse.ArgumentParser(prog="cwhelper rack-report")
    parser.add_argument("--site", "-s", default="",
                        help="site filter (e.g. US-EAST-03). Omit for all sites")
    parser.add_argument("--mine", "-m", action="store_true")
    parser.add_argument("--limit", "-l", type=int, default=200,
                        help="max tickets to fetch (default: 200)")
    parser.add_argument("--status", default="open",
                        help="open, closed, verification, 'in progress', waiting, all")
    parser.add_argument("--project", "-p", default="DO",
                        help="DO or HO (default: DO)")
    parser.add_argument("--json", action="store_true", dest="json_mode")
    args = parser.parse_args(args_list)

    email, token = _get_credentials()

    from cwhelper.services.rack_report import _run_rack_report
    _run_rack_report(email, token, args.site, mine_only=args.mine,
                     limit=args.limit, status_filter=args.status,
                     project=args.project.upper(), json_mode=args.json_mode)


def _cli_ibtrace(args_list: list):
    """Handle: cwhelper ibtrace <switch> [port] [--json] [--dh DH]"""
    parser = argparse.ArgumentParser(prog="cwhelper ibtrace",
                                     description="Trace IB connections from the cutsheet")
    parser.add_argument("switch",
                        help="Switch name (S8.3.2, L10.1.2-DH2, C1.4) or bare ID (8.3.2)")
    parser.add_argument("port", nargs="?", default=None,
                        help="Port (22/1, IBP3)")
    parser.add_argument("--json", action="store_true", dest="json_mode",
                        help="Output as JSON")
    parser.add_argument("--dh", default=None,
                        help="Filter by data hall (DH1, DH2)")
    args = parser.parse_args(args_list)

    from cwhelper.services.ib_trace import _load_connections, _search_connections
    from cwhelper.tui.ib_trace_view import _display_ibtrace

    connections = _load_connections()
    if not connections:
        return

    results = _search_connections(connections, args.switch, args.port)

    if args.dh:
        dh = args.dh.upper()
        results = [r for r in results
                   if dh in (r["data_hall"], r["src_dh"], r["dest_dh"])]

    if args.json_mode:
        print(json.dumps(results, indent=2))
    else:
        _display_ibtrace(results, args.switch, args.port)


def _cli_lookup(raw_args: list):
    """Handle: python3 get_node_context.py <identifier> [--json] [--raw]"""
    identifier = None
    flags = []
    for arg in raw_args:
        if arg.startswith("-"):
            flags.append(arg)
        elif identifier is None:
            identifier = arg

    if not identifier:
        _print_cli_help()
        sys.exit(1)

    parser = argparse.ArgumentParser(prog="get_node_context.py")
    parser.add_argument("--json", action="store_true", dest="json_mode")
    parser.add_argument("--raw", action="store_true", dest="raw_mode")
    args = parser.parse_args(flags)

    identifier = identifier.strip()
    if re.match(r"^[A-Za-z]+-\d+$", identifier):
        identifier = identifier.upper()

    quiet = args.json_mode or args.raw_mode
    ctx = get_node_context(identifier, quiet=quiet)

    # --raw wins over --json
    if args.raw_mode:
        _print_raw(ctx)
    elif args.json_mode:
        _print_json(ctx)
    else:
        _print_pretty(ctx)
