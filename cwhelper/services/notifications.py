"""Push notifications — ntfy.sh, macOS, SLA and stale alerts."""
from __future__ import annotations

import datetime
import time

import os
import requests
import subprocess

from cwhelper import config as _cfg
from cwhelper.config import *  # noqa: F401,F403
__all__ = ['_macos_notify', '_ntfy_send', '_check_stale_unassigned', '_check_sla_warnings']
from cwhelper.services.search import _fetch_sla




def _macos_notify(title: str, subtitle: str, message: str):
    """Pop a macOS notification via osascript. Silent no-op on failure."""
    try:
        script = (
            f'display notification "{message}" '
            f'with title "{title}" subtitle "{subtitle}" sound name "Glass"'
        )
        subprocess.run(["osascript", "-e", script],
                       capture_output=True, timeout=5)
    except Exception:
        pass



def _ntfy_send(title: str, message: str, priority: str = "default", tags: str = ""):
    """Send push notification via ntfy.sh. Silent no-op if not configured."""
    if not _cfg._NTFY_ENABLED or not _cfg.NTFY_TOPIC:
        return
    try:
        headers = {"Title": title, "Priority": priority}
        if tags:
            headers["Tags"] = tags
        requests.post(
            f"https://ntfy.sh/{_cfg.NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers=headers,
            timeout=5,
        )
    except Exception:
        pass



def _check_stale_unassigned(issues: list, site: str):
    """Send ntfy alert for tickets sitting unassigned too long."""
    if not _cfg._NTFY_ENABLED or not _cfg.NTFY_TOPIC:
        return
    now = time.time()
    stale = []
    for iss in issues:
        key = iss.get("key", "")
        fields = iss.get("fields", {})
        assignee = fields.get("assignee")
        if assignee:
            continue  # assigned, skip
        created = fields.get("created", "")
        if not created:
            continue
        try:
            # Jira format: 2025-03-04T14:30:00.000+0000
            created_ts = datetime.datetime.strptime(
                created[:19], "%Y-%m-%dT%H:%M:%S").timestamp()
            age_hours = (now - created_ts) / 3600
            if age_hours >= STALE_UNASSIGNED_HOURS:
                alert_key = f"stale:{key}"
                if alert_key not in _cfg._ntfy_alerted:
                    stale.append(key)
                    _cfg._ntfy_alerted.add(alert_key)
        except Exception:
            continue
    if stale:
        msg = f"{len(stale)} unassigned ticket{'s' if len(stale) > 1 else ''}"
        msg += f" sitting {STALE_UNASSIGNED_HOURS}h+"
        if site:
            msg += f" in {site}"
        msg += f": {', '.join(stale[:5])}"
        _ntfy_send("Stale Tickets", msg, priority="high", tags="hourglass")



def _check_sla_warnings(issues: list, email: str, token: str):
    """Check SLA on the user's assigned tickets — alert if close to breach."""
    if not _cfg._NTFY_ENABLED or not _cfg.NTFY_TOPIC:
        return
    my_tickets = []
    for iss in issues:
        fields = iss.get("fields", {})
        assignee = fields.get("assignee")
        if assignee and assignee.get("emailAddress") == email:
            my_tickets.append(iss)
    # Only check up to 5 tickets per cycle to avoid API flood
    for iss in my_tickets[:5]:
        key = iss.get("key", "")
        alert_key = f"sla:{key}"
        if alert_key in _cfg._ntfy_alerted:
            continue
        try:
            sla_data = _fetch_sla(key, email, token)
            for s in sla_data:
                ongoing = s.get("ongoingCycle", {})
                if not ongoing:
                    continue
                breached = ongoing.get("breached", False)
                remaining = ongoing.get("remainingTime", {})
                millis = remaining.get("millis", None)
                friendly = remaining.get("friendly", "")
                if breached:
                    _ntfy_send("SLA BREACHED",
                               f"{key} — {s.get('name', 'SLA')} breached!",
                               priority="urgent", tags="red_circle")
                    _cfg._ntfy_alerted.add(alert_key)
                elif millis is not None and millis < 3600000:  # < 1 hour left
                    _ntfy_send("SLA Warning",
                               f"{key} — {friendly} remaining ({s.get('name', 'SLA')})",
                               priority="high", tags="warning")
                    _cfg._ntfy_alerted.add(alert_key)
        except Exception:
            continue


