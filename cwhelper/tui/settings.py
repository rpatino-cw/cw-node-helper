"""Feature flag settings page — Rich TUI for toggling features on/off."""
from __future__ import annotations

from cwhelper import config as _cfg
from cwhelper.config import BOLD, DIM, RESET, GREEN, YELLOW, RED
from cwhelper.state import _save_user_state
from cwhelper.tui.rich_console import console

from rich.table import Table
from rich.text import Text
from rich import box as rich_box

__all__ = ['_settings_page']


def _render_settings_table() -> None:
    """Display the feature toggle table with Rich."""
    table = Table(
        title="Feature Settings",
        title_style="bold",
        box=rich_box.ROUNDED,
        show_header=True,
        header_style="dim",
        padding=(0, 1),
    )
    table.add_column("#", style="bold", width=3, no_wrap=True)
    table.add_column("Feature", min_width=28)
    table.add_column("Status", width=6, no_wrap=True)
    table.add_column("Deps", style="dim", width=18)

    _ids = sorted(_cfg._FEATURE_REGISTRY.keys())
    for i, fid in enumerate(_ids, 1):
        meta = _cfg._FEATURE_REGISTRY[fid]
        enabled = _cfg.FEATURES.get(fid, meta["default"])
        status = Text(" ON", style="bold green") if enabled else Text("OFF", style="bold red")
        deps = ", ".join(meta.get("deps", [])) or "—"
        table.add_row(str(i), meta["label"], status, deps)

    console.print()
    console.print(table)
    console.print()


def _render_watcher_status() -> None:
    """Show live watcher status below the feature table."""
    import os
    from cwhelper.services.watcher import _is_watcher_running
    from cwhelper import config as _c

    running = _is_watcher_running()
    site = _c._watcher_site or os.environ.get("DEFAULT_SITE", "")

    if running:
        console.print(f"  [bold green]● Watcher running[/]  [dim]{site or 'all sites'} — every {_c._watcher_interval}s[/]")
        console.print(f"  [dim]  [bold]w[/bold] = stop watcher[/]")
    else:
        console.print(f"  [bold red]○ Watcher stopped[/]")
        if site:
            console.print(f"  [dim]  [bold]w[/bold] = start watcher for {site}[/]")
        else:
            console.print(f"  [dim]  Set DEFAULT_SITE in .env to enable watcher[/]")
    console.print()


def _settings_page(state: dict) -> dict:
    """Interactive settings loop. Returns updated state dict."""
    _ids = sorted(_cfg._FEATURE_REGISTRY.keys())

    while True:
        _render_settings_table()
        _render_watcher_status()
        console.print(f"  [dim]Toggle by number, [bold]a[/bold] = all on, "
                      f"[bold]n[/bold] = all off, [bold]w[/bold] = watcher, [bold]b[/bold] = back[/dim]")
        console.print()

        try:
            raw = input(f"  Toggle [1-{len(_ids)}], w, or b: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if raw in ("b", "back", "q", ""):
            break

        if raw == "w":
            import os
            from cwhelper.services.watcher import _is_watcher_running, _start_background_watcher, _stop_background_watcher
            from cwhelper.clients.jira import _get_credentials
            if _is_watcher_running():
                _stop_background_watcher()
                print(f"  Watcher stopped.")
            else:
                site = os.environ.get("DEFAULT_SITE", "")
                if site:
                    try:
                        email, token = _get_credentials()
                        _start_background_watcher(email, token, site, project="DO", interval=60)
                        print(f"  Watcher started for {site}.")
                    except Exception:
                        print(f"  Could not start watcher — check credentials.")
                else:
                    print(f"  Set DEFAULT_SITE in .env first (run: cwhelper setup)")
            continue

        if raw == "a":
            for fid in _ids:
                _cfg.FEATURES[fid] = True
            _cfg._save_features(state)
            _save_user_state(state)
            continue

        if raw == "n":
            for fid in _ids:
                _cfg.FEATURES[fid] = False
            _cfg._save_features(state)
            _save_user_state(state)
            continue

        try:
            idx = int(raw)
            if 1 <= idx <= len(_ids):
                fid = _ids[idx - 1]
                _cfg.FEATURES[fid] = not _cfg.FEATURES[fid]
                label = _cfg._FEATURE_REGISTRY[fid]["label"]
                new_state = "ON" if _cfg.FEATURES[fid] else "OFF"
                print(f"  {label} → {new_state}")
                _cfg._save_features(state)
                _save_user_state(state)
        except ValueError:
            pass

    return state
