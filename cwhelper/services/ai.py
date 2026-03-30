"""AI assistant — chat, summarize, find ticket."""
from __future__ import annotations

import os

import json
import re
import sys
import textwrap

from cwhelper import config as _cfg
from cwhelper.config import *  # noqa: F401,F403
__all__ = ['_ai_available', '_build_ai_context', '_strip_ai_markdown', '_ai_chat', '_ai_summarize', '_suggest_comments', '_pick_or_type_comment', '_ai_find_ticket', '_ai_chat_loop', '_ai_dispatch', '_ai_work_feedback']
from cwhelper.clients.jira import _jira_get, _get_credentials
from cwhelper.services.search import _search_by_text
from cwhelper.services.context import _format_age, _parse_jira_timestamp, _adf_to_plain_text, _extract_comments
from cwhelper.cache import _classify_port_role, _lookup_ib_connections, _brief_pause




def _ai_available() -> bool:
    """Return True if an OpenAI-compatible API is configured and importable."""
    if not _HAS_OPENAI:
        return False
    # Local providers (Ollama) don't need a real API key
    if AI_BASE_URL:
        return True
    return bool(os.environ.get("OPENAI_API_KEY", "").strip())



def _build_ai_context(ctx: dict) -> str:
    """Serialize a ticket context dict into rich plain text for AI prompts.

    Includes full description, all comments, connection details, HO context,
    and diagnostic info for thorough troubleshooting assistance.
    """
    if not ctx:
        return "(no ticket context loaded)"

    lines = []
    lines.append(f"TICKET: {ctx.get('issue_key', '?')}")
    lines.append(f"SUMMARY: {ctx.get('summary', '?')}")
    status = ctx.get("status", "?")
    age = ctx.get("status_age_seconds")
    age_str = f" (for {_format_age(age)})" if age else ""
    lines.append(f"STATUS: {status}{age_str}")
    lines.append(f"PROJECT: {ctx.get('project', '?')}")
    lines.append(f"TYPE: {ctx.get('issue_type', '?')}")
    lines.append(f"PRIORITY: {ctx.get('priority') or '?'}")
    lines.append(f"ASSIGNEE: {ctx.get('assignee') or 'Unassigned'}")
    lines.append(f"REPORTER: {ctx.get('reporter') or '?'}")
    lines.append(f"SITE: {ctx.get('site') or '?'}")
    lines.append(f"RACK: {ctx.get('rack_location') or '?'}")
    lines.append(f"SERVICE TAG: {ctx.get('service_tag') or '?'}")
    lines.append(f"HOSTNAME: {ctx.get('hostname') or '?'}")
    lines.append(f"VENDOR: {ctx.get('vendor') or '?'}")

    # Extra device fields from NetBox
    nb = ctx.get("netbox", {})
    if nb.get("asset_tag"):
        lines.append(f"ASSET TAG: {nb['asset_tag']}")
    if nb.get("device_type"):
        lines.append(f"MODEL: {nb.get('manufacturer', '')} {nb['device_type']}")
    if nb.get("status"):
        lines.append(f"NB STATUS: {nb['status']}")

    ip = ctx.get("ip_address") or nb.get("primary_ip")
    if ip:
        lines.append(f"IP: {ip}")
    if nb.get("oob_ip"):
        lines.append(f"OOB/BMC IP: {nb['oob_ip']}")

    # RMA reason if present
    if ctx.get("rma_reason"):
        lines.append(f"RMA REASON: {ctx['rma_reason']}")

    # Full description (generous limit for troubleshooting)
    desc = ctx.get("description_text", "")
    if desc:
        if len(desc) > 4000:
            desc = desc[:3997] + "..."
        lines.append(f"\nFULL DESCRIPTION:\n{desc}")

    # All comments — full text, not truncated (critical for troubleshooting)
    comments = ctx.get("comments") or []
    if not comments and ctx.get("_raw_comments"):
        comments = _extract_comments(
            {"comment": {"comments": ctx["_raw_comments"]}}, max_comments=20)
    if comments:
        lines.append(f"\nALL COMMENTS ({len(comments)}):")
        for c in comments:
            body = c.get("body", "")
            if len(body) > 1500:
                body = body[:1497] + "..."
            lines.append(f"  [{c.get('created', '?')}] {c.get('author', '?')}:")
            lines.append(f"    {body}")

    # NetBox connections — full detail for troubleshooting
    ifaces = nb.get("interfaces", [])
    if ifaces:
        lines.append(f"\nNETWORK CONNECTIONS ({len(ifaces)}):")
        for iface in ifaces:
            name = iface.get("name", "?")
            role = iface.get("role", "")
            speed = iface.get("speed", "")
            peer = iface.get("peer_device", "")
            peer_port = iface.get("peer_port", "")
            peer_rack = iface.get("peer_rack", "")
            uncabled = iface.get("_uncabled", False)
            status_str = " [UNCABLED]" if uncabled else ""
            peer_str = f" → {peer}:{peer_port}" if peer else ""
            rack_str = f" (rack {peer_rack})" if peer_rack else ""
            lines.append(f"  {name} ({speed} {role}){peer_str}{rack_str}{status_str}")

    # Linked issues with summaries
    linked = ctx.get("linked_issues", [])
    if linked:
        lines.append(f"\nLINKED ISSUES:")
        for l in linked[:8]:
            rel = l.get("relationship", "")
            lines.append(f"  {l.get('key', '?')} [{l.get('status', '?')}] {rel} — {l.get('summary', '')}")

    # HO context — full detail
    ho = ctx.get("ho_context")
    if ho and isinstance(ho, dict):
        lines.append(f"\nLINKED HO TICKET:")
        lines.append(f"  Key: {ho.get('key', '?')}")
        lines.append(f"  Status: {ho.get('status', '?')}")
        lines.append(f"  Summary: {ho.get('summary', '?')}")
        if ho.get("hint"):
            lines.append(f"  Guidance: {ho['hint']}")
        if ho.get("last_note"):
            lines.append(f"  Last note: {ho['last_note']}")

    # Diagnostic links
    diags = ctx.get("diag_links", [])
    if diags or ctx.get("service_tag"):
        lines.append(f"\nDIAGNOSTIC LINKS:")
        if ctx.get("service_tag"):
            lines.append(f"  Fleet Diags: https://fleetops-storage.cwobject.com/diags/{ctx['service_tag']}/index.html")
        for d in diags:
            lines.append(f"  {d.get('label', '?')}: {d.get('url', '?')}")

    # Grafana URLs
    grafana = ctx.get("grafana", {})
    if grafana:
        for label, url in grafana.items():
            if url:
                lines.append(f"  Grafana {label}: {url}")

    # SLA info
    sla = ctx.get("sla")
    if sla and isinstance(sla, list):
        lines.append(f"\nSLA:")
        for s in sla:
            name = s.get("name", "?")
            ongoing = s.get("ongoingCycle", {})
            if ongoing:
                breached = ongoing.get("breached", False)
                remaining = ongoing.get("remainingTime", {}).get("friendly", "?")
                lines.append(f"  {name}: {'BREACHED' if breached else remaining}")

    # Fleet diag logs (if fetched and attached to ctx)
    fleet_logs = ctx.get("_fleet_diag_logs")
    if fleet_logs:
        lines.append(f"\nFLEET DIAGNOSTIC LOGS:")
        lines.append(fleet_logs)

    result = "\n".join(lines)
    # Increased cap — larger when logs are included
    max_cap = 40000 if fleet_logs else 20000
    if len(result) > max_cap:
        result = result[:max_cap - 3] + "..."
    return result



def _strip_ai_markdown(text: str) -> str:
    """Strip markdown formatting that LLMs sneak in despite instructions."""
    # Remove bold/italic markers: **text** → text, *text* → text
    text = re.sub(r'\*{1,3}([^*]+)\*{1,3}', r'\1', text)
    # Remove header markers: ## Header → Header
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    # Remove code fences: ```...``` → content
    text = re.sub(r'```\w*\n?', '', text)
    # Remove inline code backticks: `code` → code
    text = re.sub(r'`([^`]+)`', r'\1', text)
    # Remove blockquote markers: > text → text
    text = re.sub(r'^>\s?', '', text, flags=re.MULTILINE)
    return text



def _ai_chat(messages: list, temperature: float = AI_TEMPERATURE,
             max_tokens: int = AI_MAX_TOKENS, stream: bool = True) -> str:
    """Send messages to OpenAI and stream the response to the terminal.

    Returns the complete response text. Catches all errors gracefully.
    """
    if not _HAS_OPENAI:
        return f"{YELLOW}AI not available. Install: pip install openai{RESET}"

    api_key = os.environ.get("OPENAI_API_KEY", "").strip() or "ollama"
    if not AI_BASE_URL and api_key == "ollama":
        return f"{YELLOW}AI not available. Set OPENAI_API_KEY in .env{RESET}"

    try:
        client_kwargs = {"api_key": api_key}
        if AI_BASE_URL:
            client_kwargs["base_url"] = AI_BASE_URL
        client = _openai_mod.OpenAI(**client_kwargs)
        response = client.chat.completions.create(
            model=AI_MODEL,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            stream=stream,
        )

        if not stream:
            text = response.choices[0].message.content or ""
            return _strip_ai_markdown(text)

        # Streaming — print tokens live, strip markdown chars inline
        collected = []
        print(f"\n  {CYAN}{BOLD}AI:{RESET} ", end="", flush=True)
        try:
            for chunk in response:
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta and delta.content:
                    raw = delta.content
                    # Strip markdown for display (lightweight per-token)
                    display = raw.replace("**", "").replace("```", "").replace("`", "")
                    if display.lstrip().startswith("##"):
                        display = display.lstrip().lstrip("#").lstrip()
                    if display.lstrip().startswith(">"):
                        display = display.lstrip("> ")
                    # Indent newlines for clean terminal output
                    display = display.replace("\n", "\n      ")
                    print(display, end="", flush=True)
                    collected.append(raw)
        except KeyboardInterrupt:
            pass  # Graceful stop on Ctrl+C
        print(RESET)
        return _strip_ai_markdown("".join(collected))

    except Exception as e:
        err_type = type(e).__name__
        if "AuthenticationError" in err_type:
            msg = f"\n  {YELLOW}AI Error: Invalid API key. Check OPENAI_API_KEY in .env{RESET}"
        elif "RateLimitError" in err_type:
            msg = f"\n  {YELLOW}AI Error: Rate limited. Wait a moment and try again.{RESET}"
        elif "APIConnectionError" in err_type:
            msg = f"\n  {YELLOW}AI Error: Could not connect to OpenAI. Check internet.{RESET}"
        else:
            msg = f"\n  {YELLOW}AI Error: {e}{RESET}"
        print(msg)
        return ""



def _ai_summarize(ctx: dict) -> str:
    """Generate a one-shot summary of the current ticket."""
    context_text = _build_ai_context(ctx)
    messages = [
        {"role": "system", "content": AI_SYSTEM_PROMPT_TICKET},
        {"role": "user", "content": (
            "Summarize this ticket. What is the issue, what has been done, "
            "and what should happen next?\n\n" + context_text
        )},
    ]
    return _ai_chat(messages, temperature=0.3)



def _suggest_comments(ctx: dict, action: str = "verify") -> list:
    """Generate 2-3 context-aware comment suggestions. No AI — template-based."""
    summary = (ctx.get("summary") or "").lower()
    tag = ctx.get("service_tag") or "?"
    hostname = ctx.get("hostname") or "?"
    rack = ctx.get("rack_location") or "?"

    ticket_type = ""
    for t in ["POWER_CYCLE", "RESEAT", "DEVICE", "RECABLE", "NETWORK",
              "DPU_PORT_CLEAN", "INSPECTION", "SWAP", "UNCABLE", "RMA"]:
        if t.lower().replace("_", " ") in summary.replace("_", " "):
            ticket_type = t
            break

    if action == "verify":
        templates = {
            "POWER_CYCLE": [
                f"Power cycled {hostname} ({tag}). BMC responding, node booting.",
                f"Pulled power on {tag}, reseated after 30s. Node back up.",
            ],
            "RECABLE": [
                f"Recabled {hostname} ({tag}) per onboarding layout. Ready for onboarding.",
                f"Recable complete. All cables verified and labeled.",
            ],
            "RESEAT": [
                f"Reseated component on {hostname} ({tag}). Powered on, verified in BMC.",
                f"Component reseat complete on {tag}. Node booted, device detected.",
            ],
            "DPU_PORT_CLEAN": [
                f"Cleaned DPU ports on {hostname} ({tag}). Optics reseated, links up.",
                f"DPU port cleaning complete. Verified link status.",
            ],
            "SWAP": [
                f"Swapped component on {hostname} ({tag}). Old SN: ___, new SN: ___.",
                f"Swap done. Powered on, device detected in BMC.",
            ],
            "UNCABLE": [
                f"Uncabled {hostname} ({tag}). All cables removed and labeled.",
                f"Uncable complete on {tag}. Power and data disconnected.",
            ],
            "NETWORK": [
                f"Network issue resolved on {hostname} ({tag}). Links verified.",
                f"Reseated cables/optics on {tag}. Connectivity restored.",
            ],
        }
        return templates.get(ticket_type, [
            f"Work complete on {hostname} ({tag}). Ready for verification.",
            f"Task done on {tag}. Node operational.",
        ])
    elif action == "hold":
        return [
            f"Waiting on parts for {hostname} ({tag}).",
            f"Need guidance from Fleet/FROps on {tag}.",
            f"Blocked — node not accessible at {rack}.",
        ]
    elif action == "close":
        return [
            f"Verified and closing. {hostname} ({tag}) is operational.",
            f"Work confirmed complete. Closing.",
        ]
    elif action == "start":
        return [
            f"Starting work on {hostname} ({tag}).",
            f"On-site at {rack}. Beginning {ticket_type or 'work'}.",
        ]
    return [f"Update on {ctx.get('issue_key', '?')}: "]



def _pick_or_type_comment(ctx: dict, action: str = "verify") -> str:
    """Show category-based suggestions, AI option, or type custom."""
    suggestions = _suggest_comments(ctx, action)

    print(f"\n  {DIM}Suggested comments:{RESET}")
    for i, s in enumerate(suggestions, 1):
        print(f"    {BOLD}{i}{RESET}  {s}")
    if _ai_available():
        print(f"    {CYAN}{BOLD}ai{RESET}  {DIM}Ask AI to write a humanized comment{RESET}")
    print(f"    {DIM}Or type your own  |  ENTER to skip{RESET}\n")

    try:
        pick = input("  > ").strip()
    except (EOFError, KeyboardInterrupt):
        return ""

    if not pick:
        return ""
    if pick.isdigit() and 1 <= int(pick) <= len(suggestions):
        comment = suggestions[int(pick) - 1]
        print(f"  {GREEN}{comment}{RESET}")
        return comment
    if pick.lower() == "ai" and _ai_available():
        context_text = _build_ai_context(ctx)
        messages = [
            {"role": "system", "content": (
                "Write a short, professional Jira comment for a DCT technician. "
                "One to two sentences max. Sound like a real person, not a robot. "
                "Be specific about what was done using the ticket context. "
                "NEVER use markdown — no asterisks, no hashes, no backticks. Plain text only."
            )},
            {"role": "user", "content": (
                f"Write a comment for action: {action}\n"
                f"Ticket context:\n{context_text[:3000]}"
            )},
        ]
        result = _ai_chat(messages, stream=False, max_tokens=150)
        if result:
            result = result.strip().strip('"').strip("'")
            print(f"\n  {GREEN}{result}{RESET}")
            print(f"  {DIM}Use this? [Y/n]:{RESET} ", end="")
            try:
                confirm = input().strip().lower()
            except (EOFError, KeyboardInterrupt):
                confirm = "n"
            if confirm != "n":
                return result
            try:
                return input("  Type your own: ").strip()
            except (EOFError, KeyboardInterrupt):
                return ""
        return ""
    return pick



def _ai_find_ticket(user_description: str, email: str, token: str) -> str | None:
    """Help user find a ticket they can't remember.

    Flow: AI extracts keywords -> JQL search -> AI ranks results.
    Returns the selected ticket key, or None.
    """
    # Step 1: Extract search keywords
    print(f"\n  {DIM}Extracting search terms...{RESET}", flush=True)
    keyword_messages = [
        {"role": "system", "content": AI_SYSTEM_PROMPT_FINDER},
        {"role": "user", "content": (
            f"Extract 2-3 Jira search keywords from this description. "
            f"Return ONLY the keywords separated by spaces, nothing else.\n\n"
            f"Description: {user_description}"
        )},
    ]
    keywords = _ai_chat(keyword_messages, stream=False, temperature=0.2).strip()
    if not keywords or keywords.startswith(YELLOW):
        print(f"  {keywords}")
        return None

    print(f"  {DIM}Searching for:{RESET} {WHITE}{keywords}{RESET}", flush=True)

    # Step 2: Search Jira
    results = _search_by_text(keywords, email, token)
    if not results:
        # Try individual words
        for word in keywords.split():
            if len(word) >= 3:
                results = _search_by_text(word, email, token)
                if results:
                    break
    if not results:
        print(f"\n  {YELLOW}No tickets found matching that description.{RESET}")
        print(f"  {DIM}Try different details or use option 3 (Browse queue).{RESET}")
        return None

    # Step 3: Show all results as a numbered list
    print(f"\n  {BOLD}Found {len(results)} results:{RESET}\n")
    for i, issue in enumerate(results[:20], 1):
        f_data = issue.get("fields", {})
        key = issue.get("key", "?")
        summary = f_data.get("summary", "?")
        status_obj = f_data.get("status", {})
        status = status_obj.get("name", "?") if isinstance(status_obj, dict) else str(status_obj)
        assignee_obj = f_data.get("assignee")
        assignee = ""
        if isinstance(assignee_obj, dict) and assignee_obj:
            assignee = f"  {DIM}{assignee_obj.get('displayName', '')}{RESET}"
        from cwhelper.tui.display import _status_color  # lazy — avoids circular import
        sc, sd = _status_color(status)
        # Age from created date (total ticket age)
        age_secs = _parse_jira_timestamp(f_data.get("created"))
        if age_secs > 5 * 86400:
            age_str = f" {RED}{_format_age(age_secs):<7}{RESET}"
        elif age_secs > 86400:
            age_str = f" {YELLOW}{_format_age(age_secs):<7}{RESET}"
        elif age_secs > 0:
            age_str = f" {GREEN}{_format_age(age_secs):<7}{RESET}"
        else:
            age_str = f" {'—':<7}"
        print(f"  {BOLD}{i:>2}.{RESET}  {key}  {sc}{sd} {status:<16}{RESET}{age_str}  {DIM}{summary[:46]}{RESET}{assignee}")

    # Step 4: Let user pick
    print(f"\n  {DIM}Select [1-{min(len(results), 20)}], enter a key, or ENTER to cancel:{RESET}")
    try:
        pick = input("  > ").strip()
    except (EOFError, KeyboardInterrupt):
        return None

    if not pick:
        return None
    if pick.isdigit():
        idx = int(pick) - 1
        if 0 <= idx < len(results):
            return results[idx].get("key")
    if JIRA_KEY_PATTERN.match(pick.upper()):
        return pick.upper()
    return None



def _copy_chat_to_clipboard(turns: list):
    """Copy formatted AI chat transcript to clipboard (pbcopy / xclip / print fallback)."""
    import subprocess
    lines = []
    for m in turns:
        role = "You" if m["role"] == "user" else "AI"
        lines.append(f"{role}: {m['content']}")
        lines.append("")
    text = "\n".join(lines).strip()
    copied = False
    for cmd in (["pbcopy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
        try:
            subprocess.run(cmd, input=text.encode(), check=True, capture_output=True)
            print(f"  {GREEN}Copied to clipboard.{RESET}")
            copied = True
            break
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
    if not copied:
        print(f"\n{DIM}{'─' * 40}{RESET}")
        print(text)
        print(f"{DIM}{'─' * 40}{RESET}")


def _ai_team_queue(site: str, email: str, token: str) -> str | None:
    """Show open queue tickets for the team at site (excluding self). User picks one."""
    from cwhelper.services.search import _jql_search
    from cwhelper.tui.display import _status_color
    site_clause = f'AND cf[10194] = "{site}"' if site else ""
    print(f"\n  {DIM}Loading team queue{' for ' + site if site else ''}...{RESET}", flush=True)
    try:
        results = _jql_search(
            f'project in ("DO","HO","SDA") {site_clause} '
            f'AND assignee != currentUser() AND assignee is not EMPTY '
            f'AND statusCategory != Done ORDER BY updated DESC',
            email, token, max_results=30, use_cache=False,
            fields=["key", "summary", "status", "assignee", "created"],
        )
        # Also grab unassigned open tickets
        unassigned = _jql_search(
            f'project = "DO" {site_clause} '
            f'AND assignee is EMPTY AND statusCategory != Done ORDER BY created ASC',
            email, token, max_results=20, use_cache=False,
            fields=["key", "summary", "status", "assignee", "created"],
        )
        results = results + unassigned
    except Exception as _e:
        print(f"  {YELLOW}Query failed: {_e}{RESET}")
        return None

    if not results:
        print(f"  {DIM}No open team tickets found{' at ' + site if site else ''}.{RESET}")
        return None

    print(f"\n  {BOLD}Team queue{' — ' + site if site else ''}  ({len(results)} open):{RESET}\n")
    for i, iss in enumerate(results[:25], 1):
        f_ = iss.get("fields", {})
        key = iss.get("key", "?")
        status = (f_.get("status") or {}).get("name", "?")
        assignee = (f_.get("assignee") or {}).get("displayName", "")
        first = assignee.split()[0] if assignee else f"{DIM}unassigned{RESET}"
        summary = (f_.get("summary") or "")[:46]
        sc, sd = _status_color(status)
        print(f"  {BOLD}{i:>2}.{RESET}  {key}  {sc}{sd} {status:<18}{RESET}  {first:<14}  {DIM}{summary}{RESET}")

    print(f"\n  {DIM}Select [1-{min(len(results), 25)}], enter a key, or ENTER to cancel:{RESET}")
    try:
        pick = input("  > ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not pick:
        return None
    if pick.isdigit():
        idx = int(pick) - 1
        if 0 <= idx < len(results):
            return results[idx].get("key")
    if JIRA_KEY_PATTERN.match(pick.upper()):
        return pick.upper()
    return None


def _ai_chat_loop(ctx: dict = None, queue_info: str = "",
                   email: str = "", token: str = "",
                   initial_msg: str = ""):
    """Run an interactive AI chat session. Goes straight into chat.

    If ctx is provided, ticket context is included.
    If queue_info is provided, queue listing is included as context.
    If initial_msg is provided, sends it immediately as first message.
    Supports in-chat ticket lookup: type a ticket key like DO-12345 to load it.
    Exit: 'back', 'quit', 'q', 'exit', or empty Enter.
    Returns a ticket key string if user wants to load one, else None.
    """
    # Build system message
    if ctx:
        system = AI_SYSTEM_PROMPT_TICKET
        context_text = _build_ai_context(ctx)
        label = ctx.get("issue_key", "ticket")
    elif queue_info:
        system = AI_SYSTEM_PROMPT_CHAT
        context_text = queue_info
        label = "queue"
    else:
        system = AI_SYSTEM_PROMPT_CHAT
        context_text = ""
        label = "general"

    # Build user state context (recent tickets, last viewed, bookmarks)
    state_lines = []
    try:
        state = _load_user_state()
        last = state.get("last_ticket")
        if last:
            state_lines.append(f"LAST VIEWED: {last}")
        recent_nodes = state.get("recent_nodes", [])
        if recent_nodes:
            state_lines.append("RECENT NODES:")
            for n in recent_nodes[:5]:
                state_lines.append(f"  {n.get('term', '?')} — {n.get('hostname', '?')} @ {n.get('site', '?')}")
        bookmarks = state.get("bookmarks", [])
        if bookmarks:
            state_lines.append("BOOKMARKS:")
            for bm in bookmarks:
                state_lines.append(f"  {bm.get('label', '?')}")
    except Exception:
        pass

    # Fetch live active tickets when in general chat (no ctx) and creds are available.
    # This replaces the stale recent_tickets view history which does NOT reflect current status.
    if not ctx and not queue_info and email and token:
        try:
            from cwhelper.services.search import _jql_search
            _live = _jql_search(
                'project in ("DO","HO","SDA") AND assignee = currentUser() '
                'AND status in ("Open","Awaiting Support","Awaiting Triage","To Do","New","In Progress","Verification") '
                'ORDER BY updated DESC',
                email, token, max_results=30, use_cache=False,
                fields=["key", "summary", "status", "customfield_10194"],
            )
            if _live:
                # Extract user's site from first ticket
                _user_site = ""
                for _t in _live:
                    _sf = _t.get("fields", {}).get("customfield_10194") or ""
                    if isinstance(_sf, dict):
                        _sf = _sf.get("value", "")
                    if _sf:
                        _user_site = str(_sf)
                        break
                if not _user_site:
                    for _n in state.get("recent_nodes", []) if 'state' in dir() else []:
                        if _n.get("site"):
                            _user_site = _n["site"]
                            break
                if _user_site:
                    state_lines.append(f"USER SITE: {_user_site}")
                state_lines.append("MY ACTIVE TICKETS (live — open/in-progress only):")
                for _t in _live:
                    _tf = _t.get("fields", {})
                    _st = (_tf.get("status") or {}).get("name", "?")
                    _sm = (_tf.get("summary") or "")[:70]
                    state_lines.append(f"  {_t['key']}  [{_st}]  {_sm}")
            else:
                # Still try to get site from recent_nodes
                try:
                    _user_site = next(
                        (n["site"] for n in state.get("recent_nodes", []) if n.get("site")), "")
                    if _user_site:
                        state_lines.append(f"USER SITE: {_user_site}")
                except Exception:
                    pass
                state_lines.append("MY ACTIVE TICKETS: none — no open or in-progress tickets right now.")
        except Exception:
            pass

    state_context = "\n".join(state_lines) if state_lines else ""
    if state_context and not context_text:
        context_text = state_context
    elif state_context and context_text:
        context_text = context_text + "\n\n" + state_context

    # Add ticket lookup awareness to system prompt
    enhanced_system = system + (
        "\n\nYou have access to the user's live active tickets (open/in-progress only), site, bookmarks, and last viewed ticket. "
        "CRITICAL: MY ACTIVE TICKETS is real-time Jira data. NEVER suggest a Closed or Verification ticket as 'pending' or 'open'.\n\n"
        "COMMANDS you can embed in responses:\n"
        "- [LOAD:DO-12345] — opens a specific ticket the user wants to view.\n"
        "- [SEARCH:First Last] — finds a specific teammate's open tickets by name.\n"
        "- [TEAM_QUEUE:US-SITE01] — shows the full open team queue at a site (teammates + unassigned). "
        "  Use the USER SITE from context, or empty string if unknown.\n\n"
        "CRITICAL BEHAVIOR:\n"
        "- MY ACTIVE TICKETS is real-time — if empty, you have zero open tickets. Don't guess.\n"
        "- 'my team / team queue / team tickets / open queue / team open tickets' → [TEAM_QUEUE:USER_SITE]\n"
        "- 'find Raphael's tickets' or 'show John's queue' → [SEARCH:Raphael Rodea]\n"
        "- 'find power cycle tickets' → [SEARCH:power cycle]\n"
        "- 'open my last ticket' → [LOAD:key from LAST VIEWED]\n"
        "- When user says 'yes' after you suggest something, DO IT immediately.\n"
        "- Be direct. Don't ask for info you already have in context."
    )

    messages = [{"role": "system", "content": enhanced_system}]
    if context_text:
        messages.append({"role": "user", "content": f"Here is my current context:\n\n{context_text}"})
        messages.append({"role": "assistant", "content": "Got it. I can see the context. What would you like to know?"})

    print(f"\n  {CYAN}{BOLD}{'─' * 40}{RESET}")
    print(f"  {CYAN}{BOLD}AI Chat{RESET} {DIM}— {label}{RESET}")
    print(f"  {DIM}Type 'back' to exit/open selected  |  'find <desc>' to search tickets{RESET}")
    print(f"  {CYAN}{BOLD}{'─' * 40}{RESET}")

    found_key = None

    # Handle initial message (from unrecognized menu input)
    if initial_msg:
        print(f"\n  {GREEN}You:{RESET} {initial_msg}")
        messages.append({"role": "user", "content": initial_msg})
        response = _ai_chat(messages)
        if response:
            messages.append({"role": "assistant", "content": response})
            load_match = re.search(r'\[LOAD:([A-Z]+-\d+)\]', response)
            if load_match:
                found_key = load_match.group(1)
                print(f"\n  {GREEN}Opening {found_key}...{RESET}")
                return found_key
            tq_match = re.search(r'\[TEAM_QUEUE:([^\]]*)\]', response)
            if tq_match and email and token:
                fk = _ai_team_queue(tq_match.group(1).strip(), email, token)
                if fk:
                    found_key = fk
            search_match = re.search(r'\[SEARCH:([^\]]+)\]', response)
            if search_match and email and token:
                search_terms = search_match.group(1).strip()
                fk = _ai_find_ticket(search_terms, email, token)
                if fk:
                    found_key = fk
                    print(f"\n  {GREEN}{fk} ready to open — type 'back' to view it, or keep chatting.{RESET}")

    while True:
        try:
            user_input = input(f"\n  {GREEN}You:{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not user_input or user_input.lower() in ("back", "quit", "q", "exit", "b"):
            break

        # Direct ticket key — return it for the caller to load
        if JIRA_KEY_PATTERN.match(user_input.upper()):
            found_key = user_input.upper()
            print(f"\n  {GREEN}Loading {found_key}...{RESET}")
            break

        # "find <description>" — search for a ticket
        if user_input.lower().startswith("find ") and email and token:
            desc = user_input[5:].strip()
            if desc:
                fk = _ai_find_ticket(desc, email, token)
                if fk:
                    found_key = fk
                    print(f"\n  {GREEN}{fk} ready to open — type 'back' to view it, or keep chatting.{RESET}")
            continue

        if user_input.lower() == "clear":
            messages = [{"role": "system", "content": enhanced_system}]
            if context_text:
                messages.append({"role": "user", "content": f"Here is my current context:\n\n{context_text}"})
                messages.append({"role": "assistant", "content": "Context reloaded. What would you like to know?"})
            print(f"  {DIM}Chat history cleared.{RESET}")
            continue

        if user_input.lower() == "summary" and ctx:
            _ai_summarize(ctx)
            continue

        messages.append({"role": "user", "content": user_input})
        response = _ai_chat(messages)
        if response:
            messages.append({"role": "assistant", "content": response})

            # Check if AI wants to load a ticket via [LOAD:XX-NNNNN]
            load_match = re.search(r'\[LOAD:([A-Z]+-\d+)\]', response)
            if load_match:
                found_key = load_match.group(1)
                print(f"\n  {GREEN}Opening {found_key}...{RESET}")
                break

            # Check if AI wants to show team queue via [TEAM_QUEUE:site]
            tq_match = re.search(r'\[TEAM_QUEUE:([^\]]*)\]', response)
            if tq_match and email and token:
                _tq_site = tq_match.group(1).strip()
                fk = _ai_team_queue(_tq_site, email, token)
                if fk:
                    found_key = fk
                    print(f"\n  {GREEN}{fk} ready to open — type 'back' to view it, or keep chatting.{RESET}")
                continue

            # Check if AI wants to search via [SEARCH:keywords]
            search_match = re.search(r'\[SEARCH:([^\]]+)\]', response)
            if search_match and email and token:
                search_terms = search_match.group(1).strip()
                fk = _ai_find_ticket(search_terms, email, token)
                if fk:
                    found_key = fk
                    print(f"\n  {GREEN}{fk} ready to open — type 'back' to view it, or keep chatting.{RESET}")

        # Cap history at 20 messages (keep system + context)
        while len(messages) > 22:
            start_idx = 3 if context_text else 1
            if len(messages) > start_idx + 2:
                del messages[start_idx]
                del messages[start_idx]

    # Log the conversation to the session log
    real_turns = [m for m in messages if m["role"] in ("user", "assistant")]
    skip = 2 if context_text else 0  # skip the boilerplate context exchange
    real_turns = real_turns[skip:]
    if real_turns:
        from cwhelper.services.session_log import _log_event
        ticket_key     = ctx.get("issue_key", "") if ctx else ""
        ticket_summary = ctx.get("summary", "") if ctx else ""
        n_exchanges = len(real_turns) // 2
        _log_event("ai_chat", key=ticket_key, summary=ticket_summary,
                   detail=f"{n_exchanges} exchange(s)", ctx=ctx,
                   chat_log=real_turns)

    # Offer copy before closing (only if there were real turns)
    if real_turns:
        try:
            copy_choice = input(f"\n  {DIM}Copy chat to clipboard? [y/N]: {RESET}").strip().lower()
        except (EOFError, KeyboardInterrupt):
            copy_choice = ""
        if copy_choice == "y":
            _copy_chat_to_clipboard(real_turns)

    print(f"\n  {DIM}Chat ended. AI conversation saved to session log (press 'l' from menu).{RESET}")
    return found_key



def _ai_work_feedback(show_all: bool = False):
    """Send the session work summary to AI and stream performance feedback."""
    from cwhelper.services.session_log import _build_work_summary
    if not _ai_available():
        print(f"\n  {YELLOW}AI not available — set OPENAI_API_KEY in .env{RESET}")
        return

    print(f"\n  {DIM}Building work summary...{RESET}", flush=True)
    summary = _build_work_summary(show_all=show_all)

    system_prompt = (
        "You are a data center operations coach reviewing a DCT technician's work log. "
        "The log contains ticket activity with timestamps, state transitions (grab → start → verify → close), "
        "ticket types (power cycle, reseat, swap, recable, etc.), and time deltas between states. "
        "\n\nYour job: give honest, specific, actionable feedback. Be direct — not generic. "
        "Cover: efficiency (time per state), throughput, patterns you notice, what's going well, "
        "and specific things to improve. Think of it like an SLA report + coaching session. "
        "If data is sparse, say so and give advice based on what you can see. "
        "Keep it concise — no bullet walls. Plain text only, no markdown."
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Here is my work log:\n\n{summary}\n\nGive me feedback on my performance."},
    ]
    _ai_chat(messages, temperature=0.4, max_tokens=600)


def _ai_dispatch(ctx: dict = None, email: str = "", token: str = "",
                 queue_info: str = "", initial_msg: str = ""):
    """Universal AI entry point — goes straight into chat.

    If initial_msg is provided, sends it as the first message (for
    unrecognized main menu input routed to AI).
    Returns a ticket key if the user finds one via AI, else None.
    """
    if not _ai_available():
        print(f"\n  {YELLOW}AI not available.{RESET}", end="")
        if not _HAS_OPENAI:
            print(f" Install: {WHITE}pip install openai{RESET}")
        else:
            print(f" Set {WHITE}OPENAI_API_KEY{RESET} in your .env file")
        _brief_pause(1.5)
        return None

    found_key = _ai_chat_loop(ctx=ctx, queue_info=queue_info,
                               email=email, token=token,
                               initial_msg=initial_msg)
    return found_key


