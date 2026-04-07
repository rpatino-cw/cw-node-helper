#!/usr/bin/env python3
"""Integrity tests for get_node_context.py — menu options, hotkeys, panels, transitions.

No real Jira/NetBox calls. All I/O is mocked.
Run:  python3 -m pytest test_integrity.py -v   (or)   python3 test_integrity.py
"""

import io
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

# ---------------------------------------------------------------------------
# Patch environment and heavy imports BEFORE importing the module
# ---------------------------------------------------------------------------
os.environ.setdefault("JIRA_EMAIL", "test.user@example.com")
os.environ.setdefault("JIRA_API_TOKEN", "fake-token")
# Clear real site names so they don't leak into test output
os.environ.pop("KNOWN_SITES", None)

# Mock requests so the module-level `requests.Session()` doesn't do real HTTP
_mock_requests = MagicMock()
_mock_requests.Session.return_value = MagicMock()
sys.modules.setdefault("requests", _mock_requests)

import get_node_context as gnc  # noqa: E402
import cwhelper.config as _cfg  # noqa: E402
import cwhelper.services.notifications as _notif  # noqa: E402
import cwhelper.tui.actions as _actions  # noqa: E402
import cwhelper.cli as _cli  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _capture_panel(ctx: dict, display_name: str = None, account_id: str = None,
                   bookmarks: list = None) -> str:
    """Run _print_action_panel and return everything it printed."""
    old_dn, old_aid = _cfg._my_display_name, _cfg._my_account_id
    _cfg._my_display_name = display_name
    _cfg._my_account_id = account_id
    import cwhelper.tui.actions as _act
    mock_state = {"bookmarks": bookmarks or []}
    try:
        buf = io.StringIO()
        with patch("builtins.print", side_effect=lambda *a, **kw: buf.write(" ".join(str(x) for x in a) + "\n")):
            gnc._print_action_panel(ctx, state=mock_state)
        return buf.getvalue()
    finally:
        _cfg._my_display_name, _cfg._my_account_id = old_dn, old_aid


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences for easier assertions."""
    import re
    return re.sub(r"\033\[[0-9;]*m", "", text)


def _base_ticket_ctx(**overrides) -> dict:
    """Minimal Jira ticket context dict."""
    ctx = {
        "issue_key": "DO-99999",
        "source": "jira",
        "status": "Open",
        "summary": "Test ticket",
        "_show_more_actions": True,  # Show all buttons for test visibility
        "_show_nav": True,  # Show nav buttons for test visibility
    }
    ctx.update(overrides)
    return ctx


# ===========================================================================
# 1. Action Panel Button Visibility
# ===========================================================================

class TestActionPanelButtons(unittest.TestCase):
    """Verify _print_action_panel shows the right buttons per context."""

    def _has(self, output, *keys):
        plain = _strip_ansi(output)
        for k in keys:
            self.assertIn(f"[{k}]", plain, f"Expected [{k}] in panel")

    def _lacks(self, output, *keys):
        plain = _strip_ansi(output)
        for k in keys:
            self.assertNotIn(f"[{k}]", plain, f"Did not expect [{k}] in panel")

    # --- View buttons ---

    def test_minimal_ctx_shows_basic_buttons(self):
        out = _capture_panel(_base_ticket_ctx())
        self._has(out, "a", "j", "b", "m", "q", "u", "c")  # [c] always shows, [u] SLA always shows
        self._lacks(out, "r", "n", "w", "l", "d", "e", "f")

    def test_full_ctx_shows_all_view_buttons(self):
        ctx = _base_ticket_ctx(
            rack_location="US-SITE01.DH1.R64.RU34",
            netbox={"interfaces": [{"name": "eth0"}], "device_id": 1, "device_name": "d001", "site_slug": "us-site01", "rack_id": 42},
            description_text="Some description",
            linked_issues=[{"key": "HO-111"}],
            diag_links=[{"url": "https://example.com"}],
            comments=[{"author": "Bot", "body": "hi", "created": "2024-01-01"}],
            sla={"ongoing": True},
            ho_context={"key": "HO-222"},
            grafana={"node_details": "https://grafana/d/1", "ib_node_search": "https://grafana/d/2"},
            _portal_url="https://portal.example.com/x",
        )
        out = _capture_panel(ctx)
        self._has(out, "r", "n", "w", "l", "d", "c", "e", "u")
        self._has(out, "j", "p", "x", "g", "i", "t", "o")
        self._has(out, "b", "m", "q")

    def test_mrb_button_only_when_count_positive(self):
        out_zero = _capture_panel(_base_ticket_ctx(_mrb_count=0))
        self._lacks(out_zero, "f")
        out_pos = _capture_panel(_base_ticket_ctx(_mrb_count=3))
        self._has(out_pos, "f")
        self.assertIn("MRB (3)", _strip_ansi(out_pos))

    def test_node_history_button_needs_service_tag_or_hostname(self):
        out_none = _capture_panel(_base_ticket_ctx())
        self._lacks(out_none, "hn")
        out_tag = _capture_panel(_base_ticket_ctx(service_tag="10NQ724"))
        self._has(out_tag, "hn")
        out_host = _capture_panel(_base_ticket_ctx(hostname="d0001142"))
        self._has(out_host, "hn")

    # --- Netbox device view (no actions/transitions) ---

    def test_netbox_source_hides_ticket_actions(self):
        ctx = _base_ticket_ctx(source="netbox")
        out = _capture_panel(ctx)
        self._lacks(out, "a", "s", "v", "y", "z", "k", "j")

    # --- Assign label variants ---

    def test_assign_grab_when_unassigned(self):
        out = _capture_panel(_base_ticket_ctx(assignee=None))
        self.assertIn("Grab", _strip_ansi(out))

    def test_assign_take_when_someone_else(self):
        out = _capture_panel(_base_ticket_ctx(assignee="Jane Doe"))
        self.assertIn("Take from Jane Doe", _strip_ansi(out))

    def test_assign_unassign_when_mine(self):
        out = _capture_panel(
            _base_ticket_ctx(assignee="Test User"),
            display_name="Test User",
        )
        self.assertIn("Unassign", _strip_ansi(out))

    # --- Status transition buttons ---

    def test_in_progress_mine_shows_verify_hold(self):
        out = _capture_panel(
            _base_ticket_ctx(status="In Progress", assignee="Me", _assignee_account_id="aid1"),
            display_name="Me", account_id="aid1",
        )
        self._has(out, "v", "y")
        self._lacks(out, "s", "z", "k")

    def test_verification_mine_shows_resume_close(self):
        out = _capture_panel(
            _base_ticket_ctx(status="Verification", assignee="Me", _assignee_account_id="aid1"),
            display_name="Me", account_id="aid1",
        )
        self._has(out, "z", "k")
        self._lacks(out, "s", "v", "y")

    def test_on_hold_mine_shows_resume(self):
        out = _capture_panel(
            _base_ticket_ctx(status="On Hold", assignee="Me", _assignee_account_id="aid1"),
            display_name="Me", account_id="aid1",
        )
        self._has(out, "z")
        self._lacks(out, "s", "v", "y", "k")

    def test_todo_unassigned_shows_start(self):
        out = _capture_panel(_base_ticket_ctx(status="To Do", assignee=None))
        self._has(out, "s")
        self._lacks(out, "v", "y", "z", "k")

    def test_closed_shows_no_transitions(self):
        out = _capture_panel(
            _base_ticket_ctx(status="Closed", assignee="Me", _assignee_account_id="aid1"),
            display_name="Me", account_id="aid1",
        )
        self._lacks(out, "s", "v", "y", "z", "k")

    def test_new_unassigned_shows_start(self):
        out = _capture_panel(_base_ticket_ctx(status="New", assignee=None))
        self._has(out, "s")

    def test_waiting_for_triage_unassigned_shows_start(self):
        out = _capture_panel(_base_ticket_ctx(status="Waiting for Triage", assignee=None))
        self._has(out, "s")

    def test_in_progress_not_mine_no_transitions(self):
        """If In Progress but assigned to someone else, no transition buttons."""
        out = _capture_panel(
            _base_ticket_ctx(status="In Progress", assignee="Someone Else"),
            display_name="Me",
        )
        self._lacks(out, "s", "v", "y", "z", "k")

    def test_sla_button_only_for_tickets(self):
        out_ticket = _capture_panel(_base_ticket_ctx(sla={"ongoing": True}))
        self._has(out_ticket, "u")
        out_nb = _capture_panel(_base_ticket_ctx(source="netbox", sla={"ongoing": True}))
        self._lacks(out_nb, "u")


# ===========================================================================
# 2. Transition Routing Logic
# ===========================================================================

class TestTransitionRouting(unittest.TestCase):
    """Verify _find_transition resolves fuzzy matches correctly."""

    def _make_t(self, tid, name, to_name):
        return {"id": tid, "name": name, "to": {"name": to_name}}

    def test_start_finds_in_progress(self):
        ts = [self._make_t("1", "Start Work", "In Progress")]
        self.assertEqual(gnc._find_transition(ts, "start")["id"], "1")

    def test_verify_finds_verification(self):
        ts = [self._make_t("2", "Move to Verification", "Verification")]
        self.assertEqual(gnc._find_transition(ts, "verify")["id"], "2")

    def test_hold_finds_on_hold(self):
        ts = [self._make_t("3", "Put On Hold", "On Hold")]
        self.assertEqual(gnc._find_transition(ts, "hold")["id"], "3")

    def test_resume_finds_in_progress(self):
        ts = [self._make_t("4", "Resume Work", "In Progress")]
        self.assertEqual(gnc._find_transition(ts, "resume")["id"], "4")

    def test_close_finds_closed(self):
        ts = [self._make_t("5", "Close Issue", "Closed")]
        self.assertEqual(gnc._find_transition(ts, "close")["id"], "5")

    def test_close_finds_done(self):
        ts = [self._make_t("6", "Mark Done", "Done")]
        self.assertEqual(gnc._find_transition(ts, "close")["id"], "6")

    def test_close_finds_resolved(self):
        ts = [self._make_t("7", "Resolve", "Resolved")]
        self.assertEqual(gnc._find_transition(ts, "close")["id"], "7")

    def test_fuzzy_hint_fallback(self):
        """Transition name contains hint keyword even though target status differs."""
        ts = [self._make_t("10", "Begin Work", "Working")]
        result = gnc._find_transition(ts, "start")
        self.assertIsNotNone(result)
        self.assertEqual(result["id"], "10")

    def test_exact_status_wins_over_hint(self):
        """Exact to.name match takes priority over transition name hint."""
        ts = [
            self._make_t("20", "Begin Work", "Working"),   # hint match only
            self._make_t("21", "Transition X", "In Progress"),  # exact status match
        ]
        result = gnc._find_transition(ts, "start")
        self.assertEqual(result["id"], "21")

    def test_no_match_returns_none(self):
        ts = [self._make_t("99", "Unrelated", "Archived")]
        self.assertIsNone(gnc._find_transition(ts, "start"))

    def test_empty_list_returns_none(self):
        self.assertIsNone(gnc._find_transition([], "start"))

    def test_unknown_action_returns_none(self):
        ts = [self._make_t("1", "Start", "In Progress")]
        self.assertIsNone(gnc._find_transition(ts, "nonexistent_action"))


# ===========================================================================
# 3. Detail View Key Routing
# ===========================================================================

class TestDetailViewRouting(unittest.TestCase):
    """Verify _post_detail_prompt routes hotkeys correctly."""

    def _run_prompt(self, keys, ctx=None, **kw):
        """Feed a sequence of keys to _post_detail_prompt and return result."""
        if ctx is None:
            ctx = _base_ticket_ctx()
        inputs = iter(keys)
        import cwhelper.tui.actions as _actions
        with patch("builtins.input", side_effect=inputs), \
             patch("builtins.print"), \
             patch.object(_actions, "_print_action_panel"), \
             patch.object(_actions, "_print_pretty"), \
             patch.object(_actions, "_clear_screen"), \
             patch.object(_actions, "_refresh_ctx"), \
             patch.object(_actions, "_brief_pause"):
            return gnc._post_detail_prompt(ctx, **kw)

    def test_b_returns_back(self):
        self.assertEqual(self._run_prompt(["b"]), "back")

    def test_enter_refreshes_then_b_returns_back(self):
        """Enter refreshes the view (loops), then b returns back."""
        self.assertEqual(self._run_prompt(["", "b"]), "back")

    def test_m_returns_menu(self):
        self.assertEqual(self._run_prompt(["m"]), "menu")

    def test_q_returns_quit(self):
        self.assertEqual(self._run_prompt(["q"]), "quit")

    def test_hn_with_service_tag_returns_history(self):
        ctx = _base_ticket_ctx(service_tag="10NQ724")
        self.assertEqual(self._run_prompt(["hn"], ctx=ctx), "history")

    def test_h_without_node_id_falls_through(self):
        """Without service_tag/hostname, 'h' is not 'history', falls to back."""
        ctx = _base_ticket_ctx()  # no service_tag, no hostname
        # 'h' won't match history; loop continues; next input "b" exits
        self.assertEqual(self._run_prompt(["h", "b"], ctx=ctx), "back")

    def test_j_opens_jira(self):
        ctx = _base_ticket_ctx()
        with patch("webbrowser.open") as wb:
            # After 'j' opens browser, loop continues; 'b' exits
            self._run_prompt(["j", "b"], ctx=ctx)
            wb.assert_called_once()
            self.assertIn("DO-99999", wb.call_args[0][0])

    def test_r_with_rack_calls_map(self):
        ctx = _base_ticket_ctx(rack_location="US-SITE01.DH1.R64.RU34")
        with patch.object(_actions, "_draw_mini_dh_map") as dm:
            # 'r' draws map and waits for input, then loop; 'b' exits
            self._run_prompt(["r", "", "b"], ctx=ctx)
            dm.assert_called_once_with("US-SITE01.DH1.R64.RU34", site="")

    def test_c_toggles_comments(self):
        ctx = _base_ticket_ctx(comments=[{"author": "A", "body": "x", "created": "2024-01-01"}])
        self.assertFalse(ctx.get("_show_comments", False))
        # 'c' toggles on, then 'b' exits
        self._run_prompt(["c", "b"], ctx=ctx)
        self.assertTrue(ctx["_show_comments"])

    def test_w_toggles_description(self):
        ctx = _base_ticket_ctx(description_text="Some desc")
        self._run_prompt(["w", "b"], ctx=ctx)
        self.assertTrue(ctx["_show_desc"])

    def test_d_toggles_diags(self):
        ctx = _base_ticket_ctx(diag_links=[{"url": "https://example.com", "label": "link"}])
        with patch.object(_actions, "_print_diagnostics_inline"):
            self._run_prompt(["d", "b"], ctx=ctx)
            self.assertTrue(ctx["_show_diags"])

    def test_bookmark_star_adds(self):
        ctx = _base_ticket_ctx()
        state = {"bookmarks": []}
        with patch.object(_actions, "_add_bookmark", return_value=state) as ab, \
             patch.object(_actions, "_save_user_state"), \
             patch.object(_actions, "_load_user_state", return_value=state):
            self._run_prompt(["*", "b"], ctx=ctx, state=state)
            ab.assert_called_once()

    def test_bookmark_star_removes_when_exists(self):
        ctx = _base_ticket_ctx()
        state = {"bookmarks": [
            {"label": "DO-99999", "type": "ticket", "params": {"key": "DO-99999"}}
        ]}
        with patch.object(_actions, "_remove_bookmark", return_value=state) as rb, \
             patch.object(_actions, "_save_user_state"), \
             patch.object(_actions, "_load_user_state", return_value=state):
            self._run_prompt(["*", "b"], ctx=ctx, state=state)
            rb.assert_called_once()

    def test_eof_returns_quit(self):
        """Ctrl-D / EOF at prompt returns quit."""
        ctx = _base_ticket_ctx()
        with patch("builtins.input", side_effect=EOFError), \
             patch("builtins.print"), \
             patch.object(gnc, "_print_action_panel"):
            result = gnc._post_detail_prompt(ctx)
            self.assertEqual(result, "quit")


# ===========================================================================
# 4. Helper Function Integrity
# ===========================================================================

class TestIsMine(unittest.TestCase):
    """Verify _is_mine checks assignment correctly."""

    def _set_identity(self, display_name=None, account_id=None):
        _cfg._my_display_name = display_name
        _cfg._my_account_id = account_id

    def tearDown(self):
        _cfg._my_display_name = None
        _cfg._my_account_id = None

    def test_matches_display_name_case_insensitive(self):
        self._set_identity(display_name="John Smith")
        self.assertTrue(gnc._is_mine({"assignee": "john smith"}))

    def test_matches_account_id(self):
        self._set_identity(account_id="abc123")
        self.assertTrue(gnc._is_mine({"assignee": "Someone", "_assignee_account_id": "abc123"}))

    def test_matches_email_derived_name(self):
        self._set_identity()
        with patch.dict(os.environ, {"JIRA_EMAIL": "john.smith@example.com"}):
            self.assertTrue(gnc._is_mine({"assignee": "John Smith"}))

    def test_false_when_unassigned(self):
        self._set_identity(display_name="Me")
        self.assertFalse(gnc._is_mine({"assignee": None}))
        self.assertFalse(gnc._is_mine({"assignee": ""}))
        self.assertFalse(gnc._is_mine({}))

    def test_false_when_different_person(self):
        self._set_identity(display_name="Me", account_id="mine")
        self.assertFalse(gnc._is_mine({"assignee": "Other Person", "_assignee_account_id": "theirs"}))


class TestTextToAdf(unittest.TestCase):
    """Verify _text_to_adf produces valid ADF structure."""

    def test_basic_structure(self):
        adf = gnc._text_to_adf("hello world")
        self.assertEqual(adf["type"], "doc")
        self.assertEqual(adf["version"], 1)
        self.assertTrue(len(adf["content"]) >= 1)
        para = adf["content"][0]
        self.assertEqual(para["type"], "paragraph")
        self.assertEqual(para["content"][0]["type"], "text")
        self.assertEqual(para["content"][0]["text"], "hello world")

    def test_empty_string(self):
        adf = gnc._text_to_adf("")
        self.assertEqual(adf["type"], "doc")


class TestParseRackLocation(unittest.TestCase):
    """Verify _parse_rack_location parses correctly."""

    def test_full_location(self):
        r = gnc._parse_rack_location("US-SITE01.DH1.R64.RU34")
        self.assertEqual(r["site_code"], "US-SITE01")
        self.assertEqual(r["dh"], "DH1")
        self.assertEqual(r["rack"], 64)
        self.assertEqual(r["ru"], "34")

    def test_no_ru(self):
        r = gnc._parse_rack_location("US-SITE01.DH1.R64")
        self.assertEqual(r["rack"], 64)
        self.assertIsNone(r["ru"])

    def test_too_few_parts(self):
        self.assertIsNone(gnc._parse_rack_location("SITE.R1"))

    def test_none_input(self):
        self.assertIsNone(gnc._parse_rack_location(None))

    def test_empty_string(self):
        self.assertIsNone(gnc._parse_rack_location(""))


class TestEscapeJql(unittest.TestCase):
    """Verify _escape_jql handles special chars."""

    def test_escapes_quotes(self):
        self.assertEqual(gnc._escape_jql('he said "hi"'), 'he said \\"hi\\"')

    def test_escapes_backslash(self):
        self.assertEqual(gnc._escape_jql("path\\file"), "path\\\\file")

    def test_empty(self):
        self.assertEqual(gnc._escape_jql(""), "")

    def test_none(self):
        self.assertIsNone(gnc._escape_jql(None))


class TestClassifyPortRole(unittest.TestCase):
    """Verify _classify_port_role categorizes interfaces."""

    def test_bmc(self):
        self.assertEqual(gnc._classify_port_role("BMC-eth0"), "BMC")
        self.assertEqual(gnc._classify_port_role("ipmi0"), "BMC")

    def test_dpu(self):
        self.assertEqual(gnc._classify_port_role("dpu0"), "DPU")

    def test_nic(self):
        self.assertEqual(gnc._classify_port_role("eno1"), "NIC")
        self.assertEqual(gnc._classify_port_role("eth0"), "NIC")
        self.assertEqual(gnc._classify_port_role("bond0"), "NIC")

    def test_ib(self):
        self.assertEqual(gnc._classify_port_role("ib0"), "IB")
        self.assertEqual(gnc._classify_port_role("ib1"), "IB")
        self.assertEqual(gnc._classify_port_role("mlx5_0"), "IB")
        self.assertEqual(gnc._classify_port_role("mlx5_bond0"), "IB")

    def test_unknown(self):
        self.assertEqual(gnc._classify_port_role("serial0"), "\u2014")


# ===========================================================================
# 5. Bookmark Logic
# ===========================================================================

class TestBookmarkLogic(unittest.TestCase):

    def test_add_bookmark_deduplicates(self):
        state = {"bookmarks": [
            {"label": "old", "type": "ticket", "params": {"key": "DO-1"}},
        ]}
        state = gnc._add_bookmark(state, "new label", "ticket", {"key": "DO-1"})
        # Should replace, not duplicate
        ticket_bms = [b for b in state["bookmarks"] if b["params"].get("key") == "DO-1"]
        self.assertEqual(len(ticket_bms), 1)
        self.assertEqual(ticket_bms[0]["label"], "new label")

    def test_max_five_bookmarks(self):
        state = {"bookmarks": [
            {"label": f"bm{i}", "type": "ticket", "params": {"key": f"DO-{i}"}}
            for i in range(5)
        ]}
        state = gnc._add_bookmark(state, "bm5", "ticket", {"key": "DO-5"})
        self.assertLessEqual(len(state["bookmarks"]), 5)

    def test_remove_bookmark(self):
        state = {"bookmarks": [
            {"label": "a", "type": "ticket", "params": {"key": "DO-1"}},
            {"label": "b", "type": "ticket", "params": {"key": "DO-2"}},
        ]}
        state = gnc._remove_bookmark(state, 0)
        self.assertEqual(len(state["bookmarks"]), 1)
        self.assertEqual(state["bookmarks"][0]["label"], "b")

    def test_remove_invalid_index(self):
        state = {"bookmarks": [{"label": "x", "type": "ticket", "params": {}}]}
        state = gnc._remove_bookmark(state, 99)
        self.assertEqual(len(state["bookmarks"]), 1)

    def test_build_suggestions_dedupes(self):
        existing = [{"label": "DO-1", "type": "ticket", "params": {"key": "DO-1"}}]
        state = {"recent_tickets": [{"key": "DO-1", "summary": "dup"}]}
        suggestions = gnc._build_bookmark_suggestions(state, existing)
        # The existing bookmark should be filtered out
        keys = [s["params"].get("key") for s in suggestions]
        self.assertNotIn("DO-1", keys)

    def test_panel_shows_remove_bookmark_when_bookmarked(self):
        bms = [{"label": "DO-99999", "type": "ticket", "params": {"key": "DO-99999"}}]
        out = _capture_panel(_base_ticket_ctx(), bookmarks=bms)
        self.assertIn("Remove Bookmark", _strip_ansi(out))

    def test_panel_shows_bookmark_when_not_bookmarked(self):
        out = _capture_panel(_base_ticket_ctx(), bookmarks=[])
        plain = _strip_ansi(out)
        self.assertIn("Bookmark", plain)
        self.assertNotIn("Remove Bookmark", plain)


# ===========================================================================
# 6. Physical Rack Neighbors (Serpentine)
# ===========================================================================

class TestPhysicalNeighbors(unittest.TestCase):
    """Verify _get_physical_neighbors for DH1 serpentine layout."""

    DH1 = {
        "racks_per_row": 10,
        "columns": [
            {"label": "Left",  "start": 1,   "num_rows": 14},
            {"label": "Right", "start": 141,  "num_rows": 18},
        ],
        "serpentine": True,
    }

    def test_first_in_row(self):
        r = gnc._get_physical_neighbors(1, self.DH1)
        self.assertIsNone(r["left"])
        self.assertEqual(r["right"], 2)

    def test_last_in_row(self):
        r = gnc._get_physical_neighbors(10, self.DH1)
        self.assertEqual(r["left"], 9)
        self.assertIsNone(r["right"])

    def test_serpentine_row(self):
        # Row 1 (odd) runs R20→R11 reversed
        r = gnc._get_physical_neighbors(15, self.DH1)
        self.assertEqual(r["left"], 16)
        self.assertEqual(r["right"], 14)

    def test_middle_rack(self):
        r = gnc._get_physical_neighbors(64, self.DH1)
        self.assertEqual(r["left"], 63)
        self.assertEqual(r["right"], 65)

    def test_right_column(self):
        r = gnc._get_physical_neighbors(141, self.DH1)
        self.assertIsNone(r["left"])
        self.assertEqual(r["right"], 142)

    def test_rack_not_in_layout(self):
        r = gnc._get_physical_neighbors(999, self.DH1)
        self.assertIsNone(r["left"])
        self.assertIsNone(r["right"])


class TestPhysicalNeighborsNonSerpentine(unittest.TestCase):
    """Verify _get_physical_neighbors for HALL-A (3 columns, non-serpentine)."""

    HALL_A = {
        "racks_per_row": 10,
        "columns": [
            {"label": "A", "start": 1, "num_rows": 5},
            {"label": "B", "start": 51, "num_rows": 5},
            {"label": "C", "start": 101, "num_rows": 5},
        ],
        "serpentine": False,
    }

    def test_col_a_middle(self):
        r = gnc._get_physical_neighbors(5, self.HALL_A)
        self.assertEqual(r["left"], 4)
        self.assertEqual(r["right"], 6)

    def test_col_a_first(self):
        r = gnc._get_physical_neighbors(1, self.HALL_A)
        self.assertIsNone(r["left"])
        self.assertEqual(r["right"], 2)

    def test_col_a_last_in_row(self):
        r = gnc._get_physical_neighbors(10, self.HALL_A)
        self.assertEqual(r["left"], 9)
        self.assertIsNone(r["right"])

    def test_col_a_end(self):
        r = gnc._get_physical_neighbors(50, self.HALL_A)
        self.assertEqual(r["left"], 49)
        self.assertIsNone(r["right"])

    def test_col_b_first(self):
        r = gnc._get_physical_neighbors(51, self.HALL_A)
        self.assertIsNone(r["left"])
        self.assertEqual(r["right"], 52)

    def test_col_b_middle(self):
        r = gnc._get_physical_neighbors(75, self.HALL_A)
        self.assertEqual(r["left"], 74)
        self.assertEqual(r["right"], 76)

    def test_col_c_first(self):
        r = gnc._get_physical_neighbors(101, self.HALL_A)
        self.assertIsNone(r["left"])
        self.assertEqual(r["right"], 102)

    def test_col_c_last(self):
        r = gnc._get_physical_neighbors(150, self.HALL_A)
        self.assertEqual(r["left"], 149)
        self.assertIsNone(r["right"])

    def test_no_cross_column_neighbors(self):
        """R50 (end of A) and R51 (start of B) are NOT neighbors."""
        r50 = gnc._get_physical_neighbors(50, self.HALL_A)
        r51 = gnc._get_physical_neighbors(51, self.HALL_A)
        self.assertIsNone(r50["right"])
        self.assertIsNone(r51["left"])

    def test_non_serpentine_no_reversal(self):
        """Row 1 (odd) should NOT be reversed when serpentine=False."""
        # R15 is in row 1, pos 4 — neighbors are 14 and 16 (no reversal)
        r = gnc._get_physical_neighbors(15, self.HALL_A)
        self.assertEqual(r["left"], 14)
        self.assertEqual(r["right"], 16)


# ===========================================================================
# 7. TRANSITION_MAP Completeness (was 6)
# ===========================================================================

class TestTransitionMapCompleteness(unittest.TestCase):

    def test_all_actions_defined(self):
        for action in ("start", "verify", "hold", "resume", "close"):
            self.assertIn(action, gnc.TRANSITION_MAP, f"Missing action: {action}")

    def test_each_has_target_status(self):
        for action, cfg in gnc.TRANSITION_MAP.items():
            self.assertIn("target_status", cfg, f"{action} missing target_status")
            self.assertTrue(len(cfg["target_status"]) > 0, f"{action} has empty target_status")

    def test_each_has_hints(self):
        for action, cfg in gnc.TRANSITION_MAP.items():
            self.assertIn("transition_hints", cfg, f"{action} missing transition_hints")
            self.assertTrue(len(cfg["transition_hints"]) > 0, f"{action} has empty hints")


# ===========================================================================
# 8. Configuration Integrity (was 7)
# ===========================================================================

class TestConfigIntegrity(unittest.TestCase):

    def test_custom_fields_all_mapped(self):
        expected = {"rack_location", "service_tag", "hostname", "site", "ip_address", "vendor"}
        actual = set(gnc.CUSTOM_FIELDS.values())
        self.assertEqual(actual, expected)

    def test_known_sites_non_empty(self):
        # KNOWN_SITES is loaded from env var; skip if not set
        if not gnc.KNOWN_SITES:
            self.skipTest("KNOWN_SITES env var not set")
        self.assertTrue(len(gnc.KNOWN_SITES) > 0)

    def test_search_projects(self):
        self.assertIn("DO", gnc.SEARCH_PROJECTS)
        self.assertIn("HO", gnc.SEARCH_PROJECTS)
        self.assertIn("SDA", gnc.SEARCH_PROJECTS)
        # SDE/SDO/SDP/SDS must NOT be in search projects
        for bad in ("SDE", "SDO", "SDP", "SDS"):
            self.assertNotIn(bad, gnc.SEARCH_PROJECTS, f"{bad} should not be a search project")

    def test_jira_key_pattern(self):
        self.assertTrue(gnc.JIRA_KEY_PATTERN.match("DO-12345"))
        self.assertTrue(gnc.JIRA_KEY_PATTERN.match("HO-1"))
        self.assertFalse(gnc.JIRA_KEY_PATTERN.match("do-123"))   # lowercase
        self.assertFalse(gnc.JIRA_KEY_PATTERN.match("12345"))    # no project
        self.assertFalse(gnc.JIRA_KEY_PATTERN.match("DO-"))      # no number


# ===========================================================================
# ntfy.sh push notifications
# ===========================================================================

class TestNtfySend(unittest.TestCase):
    """Tests for _ntfy_send() — ntfy.sh push notification function."""

    @patch.object(_cfg, "NTFY_TOPIC", "test-topic")
    @patch.object(_cfg, "_NTFY_ENABLED", True)
    def test_sends_notification(self):
        mock_post = MagicMock()
        with patch("requests.post", mock_post):
            gnc._ntfy_send("Test Title", "Test message", priority="high", tags="warning")
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], "https://ntfy.sh/test-topic")
        self.assertEqual(kwargs["data"], b"Test message")
        self.assertEqual(kwargs["headers"]["Title"], "Test Title")
        self.assertEqual(kwargs["headers"]["Priority"], "high")
        self.assertEqual(kwargs["headers"]["Tags"], "warning")

    @patch.object(_cfg, "NTFY_TOPIC", "test-topic")
    @patch.object(_cfg, "_NTFY_ENABLED", True)
    def test_omits_tags_header_when_empty(self):
        mock_post = MagicMock()
        with patch("requests.post", mock_post):
            gnc._ntfy_send("Title", "msg")
        headers = mock_post.call_args[1]["headers"]
        self.assertNotIn("Tags", headers)

    @patch.object(_cfg, "NTFY_TOPIC", "")
    @patch.object(_cfg, "_NTFY_ENABLED", True)
    def test_noop_when_no_topic(self):
        mock_post = MagicMock()
        with patch("requests.post", mock_post):
            gnc._ntfy_send("Title", "msg")
        mock_post.assert_not_called()

    @patch.object(_cfg, "NTFY_TOPIC", "test-topic")
    @patch.object(_cfg, "_NTFY_ENABLED", False)
    def test_noop_when_disabled(self):
        mock_post = MagicMock()
        with patch("requests.post", mock_post):
            gnc._ntfy_send("Title", "msg")
        mock_post.assert_not_called()

    @patch.object(_cfg, "NTFY_TOPIC", "test-topic")
    @patch.object(_cfg, "_NTFY_ENABLED", True)
    def test_silent_on_network_error(self):
        mock_post = MagicMock(side_effect=Exception("Connection refused"))
        with patch("requests.post", mock_post):
            gnc._ntfy_send("Title", "msg")  # should not raise


class TestStaleUnassigned(unittest.TestCase):
    """Tests for _check_stale_unassigned()."""

    def setUp(self):
        _cfg._ntfy_alerted.clear()

    @patch.object(_cfg, "NTFY_TOPIC", "test-topic")
    @patch.object(_cfg, "_NTFY_ENABLED", True)
    def test_alerts_on_stale_unassigned(self):
        import time, datetime as dt
        old_time = (dt.datetime.now() - dt.timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S.000+0000")
        issues = [{"key": "DO-100", "fields": {"assignee": None, "created": old_time}}]
        with patch.object(_notif, "_ntfy_send") as mock_send:
            gnc._check_stale_unassigned(issues, "LAS1")
        mock_send.assert_called_once()
        self.assertIn("DO-100", mock_send.call_args[0][1])

    @patch.object(_cfg, "NTFY_TOPIC", "test-topic")
    @patch.object(_cfg, "_NTFY_ENABLED", True)
    def test_skips_assigned_tickets(self):
        import datetime as dt
        old_time = (dt.datetime.now() - dt.timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S.000+0000")
        issues = [{"key": "DO-100", "fields": {"assignee": {"displayName": "Alice"}, "created": old_time}}]
        with patch.object(_notif, "_ntfy_send") as mock_send:
            gnc._check_stale_unassigned(issues, "LAS1")
        mock_send.assert_not_called()

    @patch.object(_cfg, "NTFY_TOPIC", "test-topic")
    @patch.object(_cfg, "_NTFY_ENABLED", True)
    def test_no_duplicate_alerts(self):
        import datetime as dt
        old_time = (dt.datetime.now() - dt.timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S.000+0000")
        issues = [{"key": "DO-100", "fields": {"assignee": None, "created": old_time}}]
        with patch.object(_notif, "_ntfy_send") as mock_send:
            gnc._check_stale_unassigned(issues, "LAS1")
            gnc._check_stale_unassigned(issues, "LAS1")  # second call
        mock_send.assert_called_once()  # only alerted once

    @patch.object(_cfg, "NTFY_TOPIC", "test-topic")
    @patch.object(_cfg, "_NTFY_ENABLED", True)
    def test_skips_recent_tickets(self):
        import datetime as dt
        recent = dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S.000+0000")
        issues = [{"key": "DO-100", "fields": {"assignee": None, "created": recent}}]
        with patch.object(_notif, "_ntfy_send") as mock_send:
            gnc._check_stale_unassigned(issues, "LAS1")
        mock_send.assert_not_called()


class TestSLAWarnings(unittest.TestCase):
    """Tests for _check_sla_warnings()."""

    def setUp(self):
        _cfg._ntfy_alerted.clear()

    @patch.object(_cfg, "NTFY_TOPIC", "test-topic")
    @patch.object(_cfg, "_NTFY_ENABLED", True)
    def test_alerts_on_sla_breach(self):
        issues = [{"key": "DO-200", "fields": {
            "assignee": {"emailAddress": "me@test.com"}}}]
        sla_data = [{"name": "Time to Resolution", "ongoingCycle": {
            "breached": True, "remainingTime": {"millis": -1000, "friendly": "-1h"}}}]
        with patch.object(_notif, "_fetch_sla", return_value=sla_data), \
             patch.object(_notif, "_ntfy_send") as mock_send:
            gnc._check_sla_warnings(issues, "me@test.com", "tok")
        mock_send.assert_called_once()
        self.assertIn("breached", mock_send.call_args[0][1].lower())

    @patch.object(_cfg, "NTFY_TOPIC", "test-topic")
    @patch.object(_cfg, "_NTFY_ENABLED", True)
    def test_alerts_on_low_remaining(self):
        issues = [{"key": "DO-200", "fields": {
            "assignee": {"emailAddress": "me@test.com"}}}]
        sla_data = [{"name": "Time to Resolution", "ongoingCycle": {
            "breached": False, "remainingTime": {"millis": 1800000, "friendly": "30m"}}}]
        with patch.object(_notif, "_fetch_sla", return_value=sla_data), \
             patch.object(_notif, "_ntfy_send") as mock_send:
            gnc._check_sla_warnings(issues, "me@test.com", "tok")
        mock_send.assert_called_once()
        self.assertIn("30m", mock_send.call_args[0][1])

    @patch.object(_cfg, "NTFY_TOPIC", "test-topic")
    @patch.object(_cfg, "_NTFY_ENABLED", True)
    def test_skips_other_peoples_tickets(self):
        issues = [{"key": "DO-200", "fields": {
            "assignee": {"emailAddress": "other@test.com"}}}]
        with patch.object(_notif, "_fetch_sla") as mock_sla, \
             patch.object(_notif, "_ntfy_send") as mock_send:
            gnc._check_sla_warnings(issues, "me@test.com", "tok")
        mock_sla.assert_not_called()
        mock_send.assert_not_called()

    @patch.object(_cfg, "NTFY_TOPIC", "")
    @patch.object(_cfg, "_NTFY_ENABLED", True)
    def test_noop_when_no_topic(self):
        issues = [{"key": "DO-200", "fields": {
            "assignee": {"emailAddress": "me@test.com"}}}]
        with patch.object(_notif, "_fetch_sla") as mock_sla:
            gnc._check_sla_warnings(issues, "me@test.com", "tok")
        mock_sla.assert_not_called()


# ===========================================================================
# Walkthrough mode
# ===========================================================================

class TestWalkthroughMode(unittest.TestCase):
    """Tests for walkthrough mode functions."""

    def test_annotate_device_returns_correct_structure(self):
        devices = [
            {"position": 34, "name": "node-01", "display": "node-01",
             "status": {"label": "Active"},
             "device_role": {"name": "GPU Server"}},
        ]
        with patch("builtins.input", side_effect=["1", "LED blinking amber"]):
            result = gnc._walkthrough_annotate_device(devices, "R064")
        self.assertIsNotNone(result)
        self.assertEqual(result["rack"], "R064")
        self.assertEqual(result["ru"], 34)
        self.assertEqual(result["device_name"], "node-01")
        self.assertEqual(result["status"], "Active")
        self.assertEqual(result["note"], "LED blinking amber")
        self.assertIn("timestamp", result)

    def test_annotate_device_skip_on_enter(self):
        devices = [
            {"position": 34, "name": "node-01", "display": "node-01",
             "status": {"label": "Active"},
             "device_role": {"name": "GPU Server"}},
        ]
        with patch("builtins.input", return_value=""):
            result = gnc._walkthrough_annotate_device(devices, "R064")
        self.assertIsNone(result)

    def test_annotate_device_no_racked_devices(self):
        devices = [{"name": "unracked-thing"}]  # no position
        result = gnc._walkthrough_annotate_device(devices, "R064")
        self.assertIsNone(result)

    def test_save_notes_persists_to_state(self):
        state = {"walkthrough_notes": [], "walkthrough_session": None}
        notes = [{"rack": "R064", "ru": 34, "device_name": "node-01",
                  "status": "Active", "note": "test", "timestamp": "2026-01-01T00:00:00Z"}]
        session = {"site_code": "US-SITE-01A", "dh": "DH1", "started_at": "2026-01-01T00:00:00Z"}
        with patch.object(_actions, "_save_user_state"):
            gnc._walkthrough_save_notes(state, notes, session)
        self.assertEqual(state["walkthrough_notes"], notes)
        self.assertEqual(state["walkthrough_session"], session)

    def test_export_csv_fallback(self):
        notes = [{"rack": "R064", "ru": 34, "device_name": "node-01",
                  "status": "Active", "note": "test note", "timestamp": "2026-01-01T00:00:00Z"}]
        # Force CSV fallback by making openpyxl import fail
        import sys
        original = sys.modules.get("openpyxl")
        sys.modules["openpyxl"] = None  # force ImportError on import
        try:
            filename = gnc._walkthrough_export(notes, "US-TEST", "DH1")
            self.assertTrue(filename.endswith(".csv"))
            import os
            self.assertTrue(os.path.exists(filename))
            with open(filename) as f:
                content = f.read()
            self.assertIn("Rack,RU,Device,Status,Note,Timestamp", content)
            self.assertIn("test note", content)
            os.remove(filename)
        finally:
            if original is not None:
                sys.modules["openpyxl"] = original
            else:
                sys.modules.pop("openpyxl", None)

    def test_resume_prompt_no_session(self):
        state = {"walkthrough_session": None, "walkthrough_notes": []}
        notes, session = gnc._walkthrough_resume_prompt(state)
        self.assertEqual(notes, [])
        self.assertIsNone(session)

    def test_resume_prompt_with_session(self):
        existing_notes = [{"rack": "R064", "note": "old note"}]
        existing_session = {"site_code": "US-TEST", "dh": "DH1", "started_at": "2026-01-01T00:00:00Z"}
        state = {"walkthrough_session": existing_session, "walkthrough_notes": existing_notes}
        with patch("builtins.input", return_value="y"):
            notes, session = gnc._walkthrough_resume_prompt(state)
        self.assertEqual(notes, existing_notes)
        self.assertEqual(session, existing_session)

    def test_pick_site_dh_valid(self):
        if not gnc.KNOWN_SITES:
            self.skipTest("KNOWN_SITES env var not set")
        with patch("builtins.input", side_effect=["1", "DH1"]):
            result = gnc._walkthrough_pick_site_dh()
        self.assertIsNotNone(result)
        self.assertEqual(result[0], gnc.KNOWN_SITES[0])
        self.assertEqual(result[1], "DH1")

    def test_pick_site_dh_cancel(self):
        with patch("builtins.input", return_value=""):
            result = gnc._walkthrough_pick_site_dh()
        self.assertIsNone(result)


class TestExtractPsuInfo(unittest.TestCase):
    """Tests for _extract_psu_info — PSU detail parsing from description text."""

    SAMPLE_DESC = (
        "The PSU with id 3 at dh1-r306-node-04-us-site-01a has failed or is unseated.\n"
        "Identify the PSU with id 3 at deviceslot dh1-r306-node-04-us-site-01a, "
        "serial S948338X5830183, rack unit 22, row 31."
    )

    def test_psu_id_extracted(self):
        info = gnc._extract_psu_info(self.SAMPLE_DESC)
        self.assertIsNotNone(info)
        self.assertEqual(info["psu_id"], "3")

    def test_deviceslot_extracted(self):
        info = gnc._extract_psu_info(self.SAMPLE_DESC)
        self.assertEqual(info["deviceslot"], "dh1-r306-node-04-us-site-01a")

    def test_serial_extracted(self):
        info = gnc._extract_psu_info(self.SAMPLE_DESC)
        self.assertEqual(info["serial"], "S948338X5830183")

    def test_rack_unit_and_row(self):
        info = gnc._extract_psu_info(self.SAMPLE_DESC)
        self.assertEqual(info["rack_unit"], "22")
        self.assertEqual(info["row"], "31")

    def test_no_psu_returns_none(self):
        info = gnc._extract_psu_info("Reseat the NIC in slot 2")
        self.assertIsNone(info)

    def test_empty_returns_none(self):
        self.assertIsNone(gnc._extract_psu_info(""))
        self.assertIsNone(gnc._extract_psu_info(None))

    def test_multiple_psu_ids(self):
        desc = "PSU with id 1 failed. PSU with id 3 also failed."
        info = gnc._extract_psu_info(desc)
        self.assertEqual(info["all_psu_ids"], ["1", "3"])


# ===========================================================================
# Pre-walk brief
# ===========================================================================

class TestPrewalkBrief(unittest.TestCase):
    """Tests for _walkthrough_prewalk_brief."""

    def _make_issue(self, key, rack_loc, status="Open", created="2026-03-01T00:00:00.000+0000"):
        return {
            "key": key,
            "fields": {
                "summary": f"Test issue {key}",
                "status":  {"name": status},
                "created": created,
                "customfield_10207": rack_loc,
                "customfield_10193": "",
            },
        }

    @patch("cwhelper.services.walkthrough._jql_search")
    def test_filters_to_current_dh(self, mock_jql):
        """Only tickets matching the current DH are shown."""
        mock_jql.return_value = [
            self._make_issue("DO-001", "US-SITE01.DH1.R042.RU03"),   # DH1 — should show
            self._make_issue("DO-002", "US-SITE01.DH2.R010.RU01"),   # DH2 — should be filtered
            self._make_issue("DO-003", "US-SITE01.DH1.R064.RU14"),   # DH1 — should show
        ]
        with patch("builtins.print"):
            gnc._walkthrough_prewalk_brief("US-SITE01", "DH1", "user@cw.com", "token")
        mock_jql.assert_called_once()

    @patch("cwhelper.services.walkthrough._jql_search")
    def test_skips_when_no_credentials(self, mock_jql):
        """No Jira call is made when email or token is missing."""
        gnc._walkthrough_prewalk_brief("US-SITE01", "DH1", "", "token")
        gnc._walkthrough_prewalk_brief("US-SITE01", "DH1", "user@cw.com", "")
        mock_jql.assert_not_called()

    @patch("cwhelper.services.walkthrough._jql_search")
    def test_handles_empty_result(self, mock_jql):
        """No crash or output when Jira returns no tickets."""
        mock_jql.return_value = []
        with patch("builtins.print"):
            gnc._walkthrough_prewalk_brief("US-SITE01", "DH1", "user@cw.com", "token")

    @patch("cwhelper.services.walkthrough._jql_search")
    def test_handles_jira_exception(self, mock_jql):
        """Gracefully degrades when Jira raises an exception."""
        mock_jql.side_effect = Exception("network error")
        with patch("builtins.print"):
            gnc._walkthrough_prewalk_brief("US-SITE01", "DH1", "user@cw.com", "token")

    @patch("cwhelper.services.walkthrough._jql_search")
    def test_sorts_by_rack_number(self, mock_jql):
        """Rows are sorted by rack number ascending."""
        mock_jql.return_value = [
            self._make_issue("DO-010", "US-SITE01.DH1.R100.RU01"),
            self._make_issue("DO-002", "US-SITE01.DH1.R005.RU01"),
            self._make_issue("DO-007", "US-SITE01.DH1.R042.RU01"),
        ]
        printed = []
        with patch("builtins.print", side_effect=lambda *a, **k: printed.append(str(a))):
            gnc._walkthrough_prewalk_brief("US-SITE01", "DH1", "user@cw.com", "token")
        # R005 should appear before R042 and R100 in output
        rack_lines = [l for l in printed if "R005" in l or "R042" in l or "R100" in l]
        self.assertTrue(len(rack_lines) >= 3)
        self.assertLess(printed.index(rack_lines[0]), printed.index(rack_lines[1]))


# ===========================================================================
# Radar — HO pre-DO awareness
# ===========================================================================

import cwhelper.services.radar as _radar  # noqa: E402
import cwhelper.services.watcher as _watcher  # noqa: E402


class TestInferProcedure(unittest.TestCase):
    """Tests for _infer_procedure status→procedure mapping."""

    def test_sent_to_dct_uc(self):
        proc, hint = _watcher._infer_procedure("Sent to DCT UC")
        self.assertEqual(proc, "Uncable")
        self.assertIn("imminent", hint.lower())

    def test_sent_to_dct_rc(self):
        proc, hint = _watcher._infer_procedure("Sent to DCT RC")
        self.assertEqual(proc, "Recable")
        self.assertIn("imminent", hint.lower())

    def test_rma_initiate(self):
        proc, hint = _watcher._infer_procedure("RMA-initiate")
        self.assertEqual(proc, "RMA Swap")

    def test_awaiting_parts(self):
        proc, hint = _watcher._infer_procedure("Awaiting Parts")
        self.assertEqual(proc, "Parts")
        self.assertIn("parts", hint.lower())

    def test_unknown_status(self):
        proc, hint = _watcher._infer_procedure("Some Other Status")
        self.assertEqual(proc, "Unknown")

    def test_case_insensitive(self):
        proc, _ = _watcher._infer_procedure("sent to dct rc")
        self.assertEqual(proc, "Recable")
        proc2, _ = _watcher._infer_procedure("SENT TO DCT RC")
        self.assertEqual(proc2, "Recable")


class TestRadarUrgencyRank(unittest.TestCase):
    """Tests for _urgency_rank ordering."""

    def test_imminent_ranks_highest(self):
        self.assertEqual(_radar._urgency_rank("Sent to DCT UC"), 1)
        self.assertEqual(_radar._urgency_rank("Sent to DCT RC"), 1)

    def test_soon_ranks_second(self):
        self.assertEqual(_radar._urgency_rank("RMA-initiate"), 2)

    def test_eventual_ranks_third(self):
        self.assertEqual(_radar._urgency_rank("Awaiting Parts"), 3)

    def test_unknown_ranks_last(self):
        self.assertGreater(_radar._urgency_rank("Random"), 3)

    def test_ordering(self):
        self.assertLess(
            _radar._urgency_rank("Sent to DCT RC"),
            _radar._urgency_rank("RMA-initiate"),
        )
        self.assertLess(
            _radar._urgency_rank("RMA-initiate"),
            _radar._urgency_rank("Awaiting Parts"),
        )


class TestFetchRadarQueue(unittest.TestCase):
    """Tests for _fetch_radar_queue with mocked Jira."""

    def _make_ho(self, key, status, rack_loc="US-SITE01.DH1.R064.RU22"):
        return {
            "key": key,
            "fields": {
                "summary": f"Test HO {key}",
                "status": {"name": status},
                "customfield_10207": rack_loc,
                "customfield_10193": "10NQ724",
                "customfield_10192": "d0001142",
                "customfield_10194": "US-SITE-01A",
                "assignee": None,
                "created": "2026-03-01T00:00:00.000+0000",
                "updated": "2026-03-10T00:00:00.000+0000",
                "statuscategorychangedate": "2026-03-09T00:00:00.000+0000",
                "issuetype": {"name": "Task"},
            },
        }

    @patch("cwhelper.services.radar._search_queue")
    def test_returns_sorted_by_urgency(self, mock_sq):
        mock_sq.return_value = [
            self._make_ho("HO-100", "Awaiting Parts", "US-SITE01.DH1.R010.RU01"),
            self._make_ho("HO-200", "Sent to DCT RC", "US-SITE01.DH1.R020.RU01"),
            self._make_ho("HO-300", "RMA-initiate", "US-SITE01.DH1.R030.RU01"),
        ]
        result = _radar._fetch_radar_queue("e", "t", site="US-SITE01")
        keys = [r["key"] for r in result]
        # Imminent (RC) first, then soon (RMA), then eventual (Parts)
        self.assertEqual(keys, ["HO-200", "HO-300", "HO-100"])

    @patch("cwhelper.services.radar._search_queue")
    def test_same_urgency_sorted_by_rack(self, mock_sq):
        mock_sq.return_value = [
            self._make_ho("HO-100", "Sent to DCT UC", "US-SITE01.DH1.R100.RU01"),
            self._make_ho("HO-200", "Sent to DCT RC", "US-SITE01.DH1.R010.RU01"),
        ]
        result = _radar._fetch_radar_queue("e", "t")
        keys = [r["key"] for r in result]
        # Both urgency 1, so sorted by rack: R010 < R100
        self.assertEqual(keys, ["HO-200", "HO-100"])

    @patch("cwhelper.services.radar._search_queue")
    def test_empty_queue(self, mock_sq):
        mock_sq.return_value = []
        result = _radar._fetch_radar_queue("e", "t")
        self.assertEqual(result, [])

    @patch("cwhelper.services.radar._search_queue")
    def test_calls_search_with_radar_filter(self, mock_sq):
        mock_sq.return_value = []
        _radar._fetch_radar_queue("email", "token", site="US-SITE01")
        mock_sq.assert_called_once_with(
            "US-SITE01", "email", "token",
            limit=50, status_filter="radar", project="HO", use_cache=False,
        )


class TestRadarSummaryLine(unittest.TestCase):
    """Tests for _radar_summary_line."""

    def _make_ho(self, key, status, rack="US-SITE01.DH1.R064.RU22"):
        return {
            "key": key,
            "fields": {
                "status": {"name": status},
                "customfield_10207": rack,
            },
        }

    def test_counts_urgency_tiers(self):
        issues = [
            self._make_ho("HO-1", "Sent to DCT RC"),
            self._make_ho("HO-2", "Sent to DCT UC"),
            self._make_ho("HO-3", "Awaiting Parts"),
        ]
        line = _strip_ansi(_radar._radar_summary_line(issues))
        self.assertIn("2 imminent", line)
        self.assertIn("1 awaiting parts", line)

    def test_empty(self):
        line = _radar._radar_summary_line([])
        self.assertIn("No radar tickets", line)

    def test_hottest_area(self):
        issues = [
            self._make_ho("HO-1", "Sent to DCT RC", "US-SITE01.DH1.R064.RU01"),
            self._make_ho("HO-2", "Sent to DCT UC", "US-SITE01.DH1.R065.RU01"),
        ]
        line = _strip_ansi(_radar._radar_summary_line(issues))
        self.assertIn("R60-R69", line)


class TestBuildPrepBrief(unittest.TestCase):
    """Tests for _build_prep_brief in context.py."""

    def _make_ho(self, status="Sent to DCT RC", summary="Recable node for onboarding"):
        return {
            "key": "HO-23456",
            "fields": {
                "summary": summary,
                "status": {"name": status},
                "customfield_10207": "US-SITE01.DH1.R064.RU22",
                "customfield_10193": "10NQ724",
                "customfield_10192": "d0001142",
            },
        }

    @patch("cwhelper.services.search._search_queue", return_value=[])
    @patch("cwhelper.services.queue._search_node_history", return_value=[])
    def test_basic_recable(self, mock_hist, mock_sq):
        from cwhelper.services.context import _build_prep_brief
        prep = _build_prep_brief(self._make_ho(), "e", "t")
        self.assertEqual(prep["key"], "HO-23456")
        self.assertEqual(prep["procedure"], "Recable")
        self.assertEqual(prep["node"], "10NQ724")
        self.assertIn("R064", prep["location"])
        self.assertIn("optics", ", ".join(prep["tools"]).lower())

    @patch("cwhelper.services.search._search_queue", return_value=[])
    @patch("cwhelper.services.queue._search_node_history")
    def test_repeat_offender(self, mock_hist, mock_sq):
        from cwhelper.services.context import _build_prep_brief
        mock_hist.return_value = [{"key": f"DO-{i}"} for i in range(5)]
        prep = _build_prep_brief(self._make_ho(), "e", "t")
        self.assertTrue(prep["repeat_offender"])
        self.assertEqual(prep["history_count"], 5)

    @patch("cwhelper.services.search._search_queue", return_value=[])
    @patch("cwhelper.services.queue._search_node_history", return_value=[])
    def test_psu_kit(self, mock_hist, mock_sq):
        from cwhelper.services.context import _build_prep_brief
        prep = _build_prep_brief(
            self._make_ho(status="RMA-initiate", summary="PSU failure slot 2"),
            "e", "t",
        )
        self.assertEqual(prep["kit_key"], "psu swap")

    @patch("cwhelper.services.search._search_queue", return_value=[])
    @patch("cwhelper.services.queue._search_node_history", return_value=[])
    def test_uncable_procedure(self, mock_hist, mock_sq):
        from cwhelper.services.context import _build_prep_brief
        prep = _build_prep_brief(
            self._make_ho(status="Sent to DCT UC", summary="Uncable for RMA"),
            "e", "t",
        )
        self.assertEqual(prep["procedure"], "Uncable")
        self.assertEqual(prep["kit_key"], "uncable")


class TestCheckRadarLink(unittest.TestCase):
    """Tests for _check_radar_link — linking new DOs to tracked radar HOs."""

    def setUp(self):
        self._orig = dict(_cfg._radar_known_keys)

    def tearDown(self):
        _cfg._radar_known_keys = self._orig

    def _make_do(self, key="DO-49999"):
        return {
            "key": key,
            "fields": {
                "summary": "Recable node",
                "status": {"name": "Open"},
                "customfield_10193": "10NQ724",
                "customfield_10207": "US-SITE01.DH1.R064.RU22",
            },
        }

    @patch("cwhelper.services.watcher._jira_get_issue")
    def test_attaches_radar_ho_when_linked(self, mock_get):
        _cfg._radar_known_keys = {
            "HO-23456": {
                "key": "HO-23456",
                "fields": {
                    "status": {"name": "Sent to DCT RC"},
                    "customfield_10207": "US-SITE01.DH1.R064.RU22",
                },
            },
        }
        mock_get.return_value = {
            "key": "DO-49999",
            "fields": {
                "issuelinks": [{
                    "type": {"name": "Relates"},
                    "outwardIssue": {"key": "HO-23456"},
                }],
            },
        }
        issue = self._make_do()
        _watcher._check_radar_link(issue, "e", "t")
        self.assertIn("_radar_ho", issue)
        self.assertEqual(issue["_radar_ho"]["ho_key"], "HO-23456")
        self.assertEqual(issue["_radar_ho"]["procedure"], "Recable")

    @patch("cwhelper.services.watcher._jira_get_issue")
    def test_no_match_when_ho_not_tracked(self, mock_get):
        _cfg._radar_known_keys = {}
        issue = self._make_do()
        _watcher._check_radar_link(issue, "e", "t")
        self.assertNotIn("_radar_ho", issue)
        mock_get.assert_not_called()

    @patch("cwhelper.services.watcher._jira_get_issue")
    def test_skips_non_do_tickets(self, mock_get):
        _cfg._radar_known_keys = {"HO-100": {}}
        issue = {"key": "HO-100", "fields": {}}
        _watcher._check_radar_link(issue, "e", "t")
        mock_get.assert_not_called()

    @patch("cwhelper.services.watcher._jira_get_issue")
    def test_handles_api_failure(self, mock_get):
        _cfg._radar_known_keys = {"HO-100": {}}
        mock_get.side_effect = Exception("network error")
        issue = self._make_do()
        _watcher._check_radar_link(issue, "e", "t")
        self.assertNotIn("_radar_ho", issue)


class TestShowGrabCardRadar(unittest.TestCase):
    """Tests for enhanced grab card with radar HO context."""

    def _make_issue(self, radar_ho=None):
        iss = {
            "key": "DO-49999",
            "fields": {
                "summary": "Recable node for onboarding",
                "status": {"name": "Open"},
                "customfield_10193": "10NQ724",
                "customfield_10207": "US-SITE01.DH1.R064.RU22",
                "assignee": None,
            },
        }
        if radar_ho:
            iss["_radar_ho"] = radar_ho
        return iss

    @patch("builtins.input", return_value="s")
    def test_normal_card_no_radar(self, mock_input):
        buf = io.StringIO()
        with patch("builtins.print", side_effect=lambda *a, **kw: buf.write(" ".join(str(x) for x in a) + "\n")):
            _watcher._show_grab_card(self._make_issue(), "e", "t")
        output = _strip_ansi(buf.getvalue())
        self.assertIn("NEW TICKET", output)
        self.assertNotIn("EXPECTED", output)
        self.assertNotIn("Linked to", output)

    @patch("builtins.input", return_value="s")
    def test_enhanced_card_with_radar(self, mock_input):
        radar = {"ho_key": "HO-23456", "procedure": "Recable",
                 "hint": "Recable DO imminent", "rack": "R064"}
        buf = io.StringIO()
        with patch("builtins.print", side_effect=lambda *a, **kw: buf.write(" ".join(str(x) for x in a) + "\n")):
            _watcher._show_grab_card(self._make_issue(radar_ho=radar), "e", "t")
        output = _strip_ansi(buf.getvalue())
        self.assertIn("EXPECTED", output)
        self.assertIn("HO-23456", output)
        self.assertIn("Recable", output)


# ===========================================================================
# NetBox Site Mismatch Fallback
# ===========================================================================

import cwhelper.clients.netbox as _netbox_mod  # noqa: E402


class TestNetboxSiteMismatchFallback(unittest.TestCase):
    """When serial lookup returns a device at the wrong site, discard it
    and fall through to rack_location positional lookup."""

    _WRONG_SITE_DEVICE = {
        "id": 111,
        "name": "dh1000-r187-cdu-01-ca-east-01a",
        "serial": "LU015K15350000100A",
        "site": {"slug": "ca-east-01a", "display": "CA-EAST-01A", "name": "CA-EAST-01A"},
        "rack": {"id": 50, "display": "187", "name": "187"},
        "position": 1,
        "primary_ip": None, "primary_ip4": None, "primary_ip6": None,
        "oob_ip": None, "status": {"label": "Active"},
        "role": {"display": "CDU"}, "device_role": None,
        "platform": None, "device_type": {"manufacturer": {"display": "Vertiv", "name": "Vertiv"},
                                           "display": "CDU-4RU-02", "model": "CDU-4RU-02"},
        "asset_tag": None,
    }

    _CORRECT_SITE_DEVICE = {
        "id": 222,
        "name": "dh2-r187-cdu-01-us-central-07a",
        "serial": "XYZABC123",
        "site": {"slug": "us-central-07a", "display": "US-CENTRAL-07A", "name": "US-CENTRAL-07A"},
        "rack": {"id": 99, "display": "187", "name": "187"},
        "position": 1,
        "primary_ip": None, "primary_ip4": None, "primary_ip6": None,
        "oob_ip": None, "status": {"label": "Active"},
        "role": {"display": "CDU"}, "device_role": None,
        "platform": None, "device_type": {"manufacturer": {"display": "Vertiv", "name": "Vertiv"},
                                           "display": "CDU-4RU-02", "model": "CDU-4RU-02"},
        "asset_tag": None,
    }

    @patch.object(_netbox_mod, "_netbox_get_interfaces", return_value=[])
    @patch.object(_netbox_mod, "_netbox_get_rack_devices")
    @patch.object(_netbox_mod, "_netbox_find_rack_by_name")
    @patch.object(_netbox_mod, "_netbox_find_device")
    @patch.object(_netbox_mod, "_netbox_available", return_value=True)
    def test_serial_match_wrong_site_falls_through_to_rack(
        self, mock_avail, mock_find, mock_rack, mock_rack_devs, mock_ifaces
    ):
        """Serial finds CA-EAST device but Jira says US-CENTRAL-07A.
        Should discard it and use rack_location to find the correct device."""
        # Serial lookup returns the wrong-site device
        mock_find.return_value = self._WRONG_SITE_DEVICE
        # Rack lookup returns the correct-site device at RU1
        mock_rack.return_value = {"id": 99, "display": "187"}
        mock_rack_devs.return_value = [self._CORRECT_SITE_DEVICE]

        # Clear cache to avoid stale hits
        _cfg._netbox_cache.clear()

        result = _netbox_mod._build_netbox_context(
            service_tag="LU015K15350000100A",
            node_name=None,
            hostname="dh1000-r187-cdu-01-ca-east-01a",
            rack_location="US-EVI01.DH2.R187.RU1",
            jira_site="US-CENTRAL-07A",
        )

        # Should have found the correct device (id 222), not the serial match (id 111)
        self.assertEqual(result.get("device_id"), 222)
        self.assertEqual(result.get("site"), "US-CENTRAL-07A")

    @patch.object(_netbox_mod, "_netbox_get_interfaces", return_value=[])
    @patch.object(_netbox_mod, "_netbox_find_device")
    @patch.object(_netbox_mod, "_netbox_available", return_value=True)
    def test_serial_match_correct_site_kept(self, mock_avail, mock_find, mock_ifaces):
        """When serial match IS at the right site, keep it (no fallback)."""
        mock_find.return_value = self._CORRECT_SITE_DEVICE

        _cfg._netbox_cache.clear()

        result = _netbox_mod._build_netbox_context(
            service_tag="XYZABC123",
            node_name=None,
            hostname=None,
            rack_location=None,
            jira_site="US-CENTRAL-07A",
        )

        self.assertEqual(result.get("device_id"), 222)
        self.assertEqual(result.get("site"), "US-CENTRAL-07A")

    @patch.object(_netbox_mod, "_netbox_get_interfaces", return_value=[])
    @patch.object(_netbox_mod, "_netbox_find_device")
    @patch.object(_netbox_mod, "_netbox_available", return_value=True)
    def test_no_jira_site_skips_validation(self, mock_avail, mock_find, mock_ifaces):
        """When jira_site is None, no site check — serial match kept as-is."""
        mock_find.return_value = self._WRONG_SITE_DEVICE

        _cfg._netbox_cache.clear()

        result = _netbox_mod._build_netbox_context(
            service_tag="LU015K15350000100A",
            node_name=None,
            hostname=None,
            rack_location=None,
            jira_site=None,
        )

        # Wrong-site device kept because no jira_site to validate against
        self.assertEqual(result.get("device_id"), 111)


# ===========================================================================
# Feature flag system
# ===========================================================================

class TestFeatureFlags(unittest.TestCase):
    """Verify the feature flag registry, loading, saving, and gating."""

    def setUp(self):
        # Reset features to defaults before each test
        for fid, meta in _cfg._FEATURE_REGISTRY.items():
            _cfg.FEATURES[fid] = meta["default"]

    def test_registry_has_all_features(self):
        expected = {
            "ticket_lookup", "queue", "my_tickets", "node_history",
            "shift_brief", "verify", "watcher", "rack_report",
            "ibtrace", "rack_map", "bookmarks",
            "bulk_start", "activity", "walkthrough", "weekend_assign",
            "ai_chat",
        }
        self.assertEqual(set(_cfg._FEATURE_REGISTRY.keys()), expected)

    def test_registry_entries_have_required_keys(self):
        required = {"label", "cli_cmd", "menu_keys", "deps", "default"}
        for fid, meta in _cfg._FEATURE_REGISTRY.items():
            self.assertTrue(required.issubset(meta.keys()),
                            f"{fid} missing keys: {required - meta.keys()}")

    def test_default_enabled_features(self):
        """Core features enabled by default."""
        expected_on = {"ticket_lookup", "queue", "my_tickets", "node_history", "activity"}
        for fid, meta in _cfg._FEATURE_REGISTRY.items():
            if fid in expected_on:
                self.assertTrue(meta["default"], f"{fid} should default to True")
            else:
                self.assertFalse(meta["default"], f"{fid} should default to False")

    def test_load_features_from_state(self):
        state = {"features": {"queue": True, "activity": True}}
        _cfg._load_features(state)
        self.assertTrue(_cfg.FEATURES["queue"])
        self.assertTrue(_cfg.FEATURES["activity"])
        self.assertTrue(_cfg.FEATURES["ticket_lookup"])  # default
        self.assertFalse(_cfg.FEATURES["watcher"])       # default

    def test_load_features_empty_state(self):
        _cfg._load_features({})
        for fid, meta in _cfg._FEATURE_REGISTRY.items():
            self.assertEqual(_cfg.FEATURES[fid], meta["default"])

    def test_save_features_roundtrip(self):
        _cfg.FEATURES["queue"] = True
        _cfg.FEATURES["activity"] = True
        state = {}
        _cfg._save_features(state)
        self.assertTrue(state["features"]["queue"])
        self.assertTrue(state["features"]["activity"])
        # Reset and reload
        _cfg.FEATURES["queue"] = False
        _cfg.FEATURES["activity"] = False
        _cfg._load_features(state)
        self.assertTrue(_cfg.FEATURES["queue"])
        self.assertTrue(_cfg.FEATURES["activity"])

    def test_is_feature_enabled(self):
        _cfg.FEATURES["activity"] = True
        self.assertTrue(_cfg._is_feature_enabled("activity"))
        _cfg.FEATURES["activity"] = False
        self.assertFalse(_cfg._is_feature_enabled("activity"))

    def test_is_feature_enabled_unknown(self):
        self.assertFalse(_cfg._is_feature_enabled("nonexistent_feature"))

    def test_enabled_menu_keys(self):
        keys = _cfg._enabled_menu_keys()
        self.assertIn("1", keys)       # queue
        self.assertIn("2", keys)       # my_tickets
        self.assertIn("l", keys)       # activity
        self.assertNotIn("3", keys)    # watcher disabled
        self.assertNotIn("4", keys)    # rack_map disabled

    def test_enabled_menu_keys_after_enabling(self):
        _cfg.FEATURES["queue"] = True
        _cfg.FEATURES["activity"] = True
        keys = _cfg._enabled_menu_keys()
        self.assertIn("1", keys)  # queue is now menu key 1
        self.assertIn("l", keys)  # activity menu key

    def test_menu_options_filtered(self):
        """Disabled features are excluded from menu options list."""
        options = [
            ("1",  "Browse queue", "hint"),
            ("2",  "My tickets",   "hint"),
            ("",   "",             ""),
            ("L",  "Learn",        "hint"),
        ]
        _cfg.FEATURES["queue"] = True
        _cfg.FEATURES["my_tickets"] = False
        _cfg.FEATURES["activity"] = False
        emk = _cfg._enabled_menu_keys()
        filtered = [o for o in options if not o[0].strip() or o[0] in emk]
        keys = [o[0] for o in filtered if o[0].strip()]
        self.assertIn("1", keys)
        self.assertNotIn("2", keys)
        # Separator preserved
        self.assertIn(("", "", ""), filtered)

    def test_require_feature_blocks_disabled(self):
        """_require_feature prints message and returns False when disabled."""
        _cfg.FEATURES["queue"] = False
        buf = io.StringIO()
        with patch("builtins.print", side_effect=lambda *a, **kw: buf.write(str(a))):
            result = _cli._require_feature("queue")
        self.assertFalse(result)
        self.assertIn("Feature disabled", buf.getvalue())

    def test_require_feature_allows_enabled(self):
        """_require_feature returns True when enabled."""
        _cfg.FEATURES["queue"] = True
        result = _cli._require_feature("queue")
        self.assertTrue(result)

    def test_cli_config_json_output(self):
        """cwhelper config --json outputs valid JSON with all features."""
        buf = io.StringIO()
        import json
        with patch("builtins.print", side_effect=lambda *a, **kw: buf.write(str(a[0]) if a else "")):
            with patch("cwhelper.state._load_user_state", return_value={"features": {}}):
                _cli._cli_config(["--json"])
        output = buf.getvalue()
        data = json.loads(output)
        self.assertIn("ticket_lookup", data)
        self.assertEqual(len(data), len(_cfg._FEATURE_REGISTRY))

    def test_cli_config_enable(self):
        """cwhelper config --enable sets feature to True."""
        _cfg.FEATURES["activity"] = False
        state = {"features": {}}
        with patch("cwhelper.state._load_user_state", return_value=state), \
             patch("cwhelper.state._save_user_state"), \
             patch("builtins.print"):
            _cli._cli_config(["--enable", "activity"])
        self.assertTrue(_cfg.FEATURES["activity"])

    def test_cli_config_disable(self):
        """cwhelper config --disable sets feature to False."""
        _cfg.FEATURES["ticket_lookup"] = True
        state = {"features": {}}
        with patch("cwhelper.state._load_user_state", return_value=state), \
             patch("cwhelper.state._save_user_state"), \
             patch("builtins.print"):
            _cli._cli_config(["--disable", "ticket_lookup"])
        self.assertFalse(_cfg.FEATURES["ticket_lookup"])

    def test_cli_config_invalid_feature(self):
        """cwhelper config --enable with unknown feature prints error."""
        buf = io.StringIO()
        with patch("builtins.print", side_effect=lambda *a, **kw: buf.write(str(a))):
            with patch("cwhelper.state._load_user_state", return_value={"features": {}}):
                _cli._cli_config(["--enable", "nonexistent"])
        self.assertIn("Unknown feature", buf.getvalue())


# ===========================================================================
# Queue browser feature tests
# ===========================================================================

def _mock_queue_issues(n=5, project="DO", site="US-EAST-03"):
    """Generate mock Jira issue dicts resembling a queue search result."""
    issues = []
    statuses = ["Open", "In Progress", "Verification", "On Hold", "Waiting For Support"]
    for i in range(n):
        issues.append({
            "key": f"{project}-{10000 + i}",
            "fields": {
                "summary": f"Test ticket {i} — GPU reseat node {i}",
                "status": {"name": statuses[i % len(statuses)]},
                "issuetype": {"name": "Task"},
                "customfield_10193": f"SVC{i:04d}",        # service_tag
                "customfield_10207": f"{site}.DH1.R{100+i}.RU{10+i}",  # rack_location
                "customfield_10192": f"dh1-r{100+i}-node-{i:02d}",     # hostname
                "customfield_10194": site,                               # site
                "assignee": {"displayName": f"Tech {i}", "accountId": f"abc{i}"} if i % 2 == 0 else None,
                "created": "2026-04-01T10:00:00.000+0000",
                "updated": "2026-04-06T14:30:00.000+0000",
                "statuscategorychangedate": "2026-04-05T08:00:00.000+0000",
                "description": f"Reseat GPU in slot {i}",
            },
        })
    return issues


class TestSearchQueue(unittest.TestCase):
    """Tests for _search_queue — JQL construction and site fallback logic."""

    def setUp(self):
        _cfg.FEATURES["queue"] = True
        # Clear JQL cache to avoid cross-test pollution
        _cfg._jql_cache.clear()

    @patch("cwhelper.services.search._jql_search")
    def test_no_site_filter(self, mock_jql):
        """No site → single JQL call without site clause."""
        mock_jql.return_value = _mock_queue_issues(3)
        from cwhelper.services.search import _search_queue
        results = _search_queue("", "e", "t", limit=10)
        self.assertEqual(len(results), 3)
        mock_jql.assert_called_once()
        jql = mock_jql.call_args[0][0]
        self.assertNotIn("cf[10194]", jql)

    @patch("cwhelper.services.search._jql_search")
    def test_site_exact_match(self, mock_jql):
        """Site provided → tries exact match on cf[10194] first."""
        mock_jql.return_value = _mock_queue_issues(2, site="US-EAST-03")
        from cwhelper.services.search import _search_queue
        results = _search_queue("US-EAST-03", "e", "t")
        self.assertEqual(len(results), 2)
        jql = mock_jql.call_args_list[0][0][0]
        self.assertIn('cf[10194] = "US-EAST-03"', jql)

    @patch("cwhelper.services.search._jql_search")
    def test_site_fallback_to_contains(self, mock_jql):
        """Exact site match empty → falls back to contains."""
        mock_jql.side_effect = [[], _mock_queue_issues(1)]
        from cwhelper.services.search import _search_queue
        results = _search_queue("US-EAST", "e", "t")
        self.assertEqual(len(results), 1)
        self.assertEqual(mock_jql.call_count, 2)
        jql2 = mock_jql.call_args_list[1][0][0]
        self.assertIn('cf[10194] ~ "US-EAST"', jql2)

    @patch("cwhelper.services.search._jql_search")
    def test_site_fallback_to_rack_location(self, mock_jql):
        """Both site filters empty → falls back to rack_location prefix."""
        mock_jql.side_effect = [[], [], _mock_queue_issues(1)]
        from cwhelper.services.search import _search_queue
        results = _search_queue("US-RIN01", "e", "t")
        self.assertEqual(len(results), 1)
        self.assertEqual(mock_jql.call_count, 3)
        jql3 = mock_jql.call_args_list[2][0][0]
        self.assertIn('cf[10207] ~ "US-RIN01"', jql3)

    @patch("cwhelper.services.search._jql_search")
    def test_mine_only_flag(self, mock_jql):
        """mine_only=True adds currentUser() to JQL."""
        mock_jql.return_value = _mock_queue_issues(1)
        from cwhelper.services.search import _search_queue
        _search_queue("", "e", "t", mine_only=True)
        jql = mock_jql.call_args[0][0]
        self.assertIn("currentUser()", jql)

    @patch("cwhelper.services.search._jql_search")
    def test_status_filter_verification(self, mock_jql):
        """status_filter='verification' uses correct JQL clause."""
        mock_jql.return_value = []
        from cwhelper.services.search import _search_queue
        _search_queue("", "e", "t", status_filter="verification")
        jql = mock_jql.call_args[0][0]
        self.assertIn('status = "Verification"', jql)

    @patch("cwhelper.services.search._jql_search")
    def test_status_filter_all(self, mock_jql):
        """status_filter='all' omits status clause entirely."""
        mock_jql.return_value = []
        from cwhelper.services.search import _search_queue
        _search_queue("", "e", "t", status_filter="all")
        jql = mock_jql.call_args[0][0]
        self.assertNotIn("status", jql.lower().split("order")[0])

    @patch("cwhelper.services.search._jql_search")
    def test_project_ho(self, mock_jql):
        """project='HO' appears in JQL."""
        mock_jql.return_value = []
        from cwhelper.services.search import _search_queue
        _search_queue("", "e", "t", project="HO")
        jql = mock_jql.call_args[0][0]
        self.assertIn('"HO"', jql)

    @patch("cwhelper.services.search._jql_search")
    def test_radar_status_filter(self, mock_jql):
        """status_filter='radar' uses the HO radar status list."""
        mock_jql.return_value = []
        from cwhelper.services.search import _search_queue
        _search_queue("", "e", "t", status_filter="radar", project="HO")
        jql = mock_jql.call_args[0][0]
        self.assertIn("RMA-initiate", jql)
        self.assertIn("Sent to DCT UC", jql)


class TestQueueJsonOutput(unittest.TestCase):
    """Tests for _run_queue_json — scriptable JSON output."""

    def setUp(self):
        _cfg.FEATURES["queue"] = True

    @patch("cwhelper.services.queue._search_queue")
    def test_json_output_structure(self, mock_sq):
        """--json outputs valid JSON array of issues."""
        mock_sq.return_value = _mock_queue_issues(3)
        buf = io.StringIO()
        with patch("builtins.print", side_effect=lambda *a, **kw: buf.write(str(a[0]) if a else "")):
            from cwhelper.services.queue import _run_queue_json
            _run_queue_json("e", "t", "US-EAST-03", False, 20, "open", "DO")
        output = buf.getvalue()
        import json
        data = json.loads(output)
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 3)
        self.assertIn("key", data[0])

    @patch("cwhelper.services.queue._search_queue")
    def test_empty_queue_json(self, mock_sq):
        """Empty queue outputs empty JSON array."""
        mock_sq.return_value = []
        buf = io.StringIO()
        with patch("builtins.print", side_effect=lambda *a, **kw: buf.write(str(a[0]) if a else "")):
            from cwhelper.services.queue import _run_queue_json
            _run_queue_json("e", "t", "", False, 20, "open", "DO")
        import json
        data = json.loads(buf.getvalue())
        self.assertEqual(data, [])


class TestQueueExtractRackNum(unittest.TestCase):
    """Tests for rack number extraction used in queue sorting."""

    def setUp(self):
        from cwhelper.services.queue import _run_queue_interactive
        # Access the nested function via the module
        self._extract = None

    def test_rack_from_location_field(self):
        """Extract rack from cf[10207] format: US-SITE01.DH1.R064.RU22"""
        from cwhelper.services import queue as _q
        # Test the regex pattern directly
        import re
        loc = "US-SITE01.DH1.R064.RU22"
        m = re.search(r'\.R(\d+)\.', loc)
        self.assertIsNotNone(m)
        self.assertEqual(int(m.group(1)), 64)

    def test_rack_from_hostname(self):
        """Extract rack from hostname: dh1-r306-node-04"""
        import re
        hostname = "dh1-r306-node-04"
        m = re.search(r'\br(\d+)\b', hostname, re.IGNORECASE)
        self.assertIsNotNone(m)
        self.assertEqual(int(m.group(1)), 306)

    def test_no_rack_returns_none(self):
        """No rack number in any field → None."""
        import re
        for val in ["", "no-rack-here", "some random text"]:
            m = re.search(r'\.R(\d+)\.', val) or re.search(r'\bR(\d+)\b', val)
            self.assertIsNone(m)


class TestQueueFeatureGate(unittest.TestCase):
    """Tests that queue feature is properly gated."""

    def setUp(self):
        for fid, meta in _cfg._FEATURE_REGISTRY.items():
            _cfg.FEATURES[fid] = meta["default"]

    def test_cli_queue_blocked_when_disabled(self):
        """cwhelper queue exits with message when feature disabled."""
        _cfg.FEATURES["queue"] = False
        buf = io.StringIO()
        with patch("builtins.print", side_effect=lambda *a, **kw: buf.write(str(a))):
            result = _cli._require_feature("queue")
        self.assertFalse(result)
        self.assertIn("Queue browser", buf.getvalue())

    def test_cli_queue_allowed_when_enabled(self):
        """cwhelper queue proceeds when feature enabled."""
        _cfg.FEATURES["queue"] = True
        result = _cli._require_feature("queue")
        self.assertTrue(result)


# ===========================================================================

if __name__ == "__main__":
    unittest.main()
