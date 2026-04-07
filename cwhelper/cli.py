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
from cwhelper.state import _load_user_state
from cwhelper.services.context import _build_context, _format_age, get_node_context
from cwhelper.services.search import _search_queue
from cwhelper.services.queue import _run_queue_interactive, _run_queue_json, _run_history_interactive, _run_history_json
from cwhelper.services.watcher import _run_queue_watcher
from cwhelper.services.weekend import _weekend_auto_assign
from cwhelper.services.brief import run_shift_brief
from cwhelper.tui.menu import _interactive_menu
from cwhelper.tui.display import _print_pretty, _print_json, _print_raw
from cwhelper.services.verify import run_verify
from cwhelper.services.learn import _run_learn_mode



def _preflight_check():
    """Verify Jira connectivity before entering interactive mode."""
    try:
        email, token = _get_credentials()
    except SystemExit:
        return  # credentials missing — _get_credentials already printed the error
    if not _jira_health_check(email, token):
        print(f"\n  \033[33m⚠  Cannot reach Jira (coreweave.atlassian.net)\033[0m")
        print(f"     Check VPN/Teleport connection and retry.")
        print(f"     Starting in offline mode — some features will fail.\n")


def main():
    """Dispatch: no args = interactive menu, with args = one-shot mode."""
    raw_args = sys.argv[1:]

    # No arguments at all → launch interactive menu
    if not raw_args:
        _preflight_check()
        _interactive_menu()
        return

    # -h / --help → print help and exit
    if raw_args[0] in ("-h", "--help"):
        _print_cli_help()
        return

    # "queue" subcommand (one-shot, scriptable)
    if raw_args[0] == "queue":
        _cli_queue(raw_args[1:])
        return

    # "history" subcommand
    if raw_args[0] == "history":
        _cli_history(raw_args[1:])
        return

    # "watch" subcommand
    if raw_args[0] == "watch":
        _cli_watch(raw_args[1:])
        return

    # "weekend-assign" subcommand
    if raw_args[0] == "weekend-assign":
        _cli_weekend_assign(raw_args[1:])
        return

    # "brief" subcommand — AI shift priority summary
    if raw_args[0] == "brief":
        _cli_brief(raw_args[1:])
        return

    # "verify" subcommand — DCT self-service verification
    if raw_args[0] == "verify":
        _cli_verify(raw_args[1:])
        return

    # "rack-report" subcommand — tickets grouped by rack
    if raw_args[0] == "rack-report":
        _cli_rack_report(raw_args[1:])
        return

    # "learn" subcommand — code quiz game
    if raw_args[0] == "learn":
        _run_learn_mode()
        return

    # "ibtrace" subcommand — IB connection trace/lookup
    if raw_args[0] == "ibtrace":
        _cli_ibtrace(raw_args[1:])
        return

    # Anything else → one-shot lookup
    _cli_lookup(raw_args)


def _print_cli_help():
    """Print help text for CLI one-shot mode."""
    print(f"""
  DCT Node Helper  v{APP_VERSION}

  USAGE
    python3 get_node_context.py                           # interactive menu
    python3 get_node_context.py <identifier> [options]    # one-shot lookup
    python3 get_node_context.py queue --site <SITE>       # one-shot queue
    python3 get_node_context.py brief [--site <SITE>]     # AI shift brief
    python3 get_node_context.py history <identifier>      # node ticket history
    python3 get_node_context.py watch --site <SITE>       # live queue watcher
    python3 get_node_context.py weekend-assign --site <SITE> --group <GROUP>
    python3 get_node_context.py learn                            # code quiz game
    cwhelper rack-report --site <SITE>                   # tickets per rack
    python3 get_node_context.py verify <identifier> [--type TYPE]
    cwhelper ibtrace <switch> [port]                   # IB connection trace

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

  EXAMPLES
    python3 get_node_context.py                                          # interactive
    python3 get_node_context.py DO-12345                                 # lookup
    python3 get_node_context.py 10NQ724                                  # search
    python3 get_node_context.py DO-12345 --json                          # JSON output
    python3 get_node_context.py queue --site US-EAST-03                  # open DO
    python3 get_node_context.py queue --site US-EAST-03 --status closed   # closed DO
    python3 get_node_context.py queue --site US-EAST-03 --project HO     # HO queue
    python3 get_node_context.py queue --status verification --mine       # my verification
    python3 get_node_context.py history 10NQ724                          # node history
    python3 get_node_context.py history d0001142 --json                  # history as JSON
    python3 get_node_context.py watch --site US-EAST-03                  # watch queue
    python3 get_node_context.py watch --site US-EAST-03 -i 180          # every 3 min
    python3 get_node_context.py weekend-assign -s US-EAST-03 -g dct-ops              # auto-assign
    python3 get_node_context.py weekend-assign -s US-EAST-03 -g dct-ops --dry-run --force
    cwhelper rack-report --site US-EAST-03                               # which racks have most tickets
    cwhelper rack-report --site US-EAST-03 --status all --json           # full JSON breakdown
    python3 get_node_context.py verify DO-96947                          # auto-detect flow
    python3 get_node_context.py verify DO-96947 --type power             # force power flow
    python3 get_node_context.py verify ss943425x5109244 --type bmc       # by serial

  SETUP (first time only)
    1. Generate a Jira API token:
       https://id.atlassian.com/manage-profile/security/api-tokens
    2. Set env vars (add to ~/.zshrc to keep them):
       export JIRA_EMAIL="you@example.com"
       export JIRA_API_TOKEN="your-token"
    3. pip3 install requests
""")


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
