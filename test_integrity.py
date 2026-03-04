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
os.environ.setdefault("JIRA_EMAIL", "test.user@coreweave.com")
os.environ.setdefault("JIRA_API_TOKEN", "fake-token")

# Mock requests so the module-level `requests.Session()` doesn't do real HTTP
_mock_requests = MagicMock()
_mock_requests.Session.return_value = MagicMock()
sys.modules.setdefault("requests", _mock_requests)

import get_node_context as gnc  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _capture_panel(ctx: dict, display_name: str = None, account_id: str = None,
                   bookmarks: list = None) -> str:
    """Run _print_action_panel and return everything it printed."""
    old_dn, old_aid = gnc._my_display_name, gnc._my_account_id
    gnc._my_display_name = display_name
    gnc._my_account_id = account_id
    mock_state = {"bookmarks": bookmarks or []}
    try:
        buf = io.StringIO()
        with patch("builtins.print", side_effect=lambda *a, **kw: buf.write(" ".join(str(x) for x in a) + "\n")), \
             patch.object(gnc, "_load_user_state", return_value=mock_state):
            gnc._print_action_panel(ctx)
        return buf.getvalue()
    finally:
        gnc._my_display_name, gnc._my_account_id = old_dn, old_aid


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
            rack_location="US-EVI01.DH1.R64.RU34",
            netbox={"interfaces": [{"name": "eth0"}], "device_id": 1, "device_name": "d001", "site_slug": "us-evi01", "rack_id": 42},
            description_text="Some description",
            linked_issues=[{"key": "HO-111"}],
            diag_links=[{"url": "https://example.com"}],
            comments=[{"author": "Bot", "body": "hi", "created": "2024-01-01"}],
            sla={"ongoing": True},
            ho_context={"key": "HO-222"},
            grafana={"node_details": "https://grafana/d/1", "ib_node_search": "https://grafana/d/2"},
            _portal_url="https://portal.coreweave.com/x",
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
        self._lacks(out_none, "h")
        out_tag = _capture_panel(_base_ticket_ctx(service_tag="10NQ724"))
        self._has(out_tag, "h")
        out_host = _capture_panel(_base_ticket_ctx(hostname="d0001142"))
        self._has(out_host, "h")

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
        with patch("builtins.input", side_effect=inputs), \
             patch("builtins.print"), \
             patch.object(gnc, "_print_action_panel"), \
             patch.object(gnc, "_print_pretty"), \
             patch.object(gnc, "_clear_screen"), \
             patch.object(gnc, "_refresh_ctx"), \
             patch.object(gnc, "_brief_pause"):
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

    def test_h_with_service_tag_returns_history(self):
        ctx = _base_ticket_ctx(service_tag="10NQ724")
        self.assertEqual(self._run_prompt(["h"], ctx=ctx), "history")

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
        ctx = _base_ticket_ctx(rack_location="US-EVI01.DH1.R64.RU34")
        with patch.object(gnc, "_draw_mini_dh_map") as dm:
            # 'r' draws map and waits for input, then loop; 'b' exits
            self._run_prompt(["r", "", "b"], ctx=ctx)
            dm.assert_called_once_with("US-EVI01.DH1.R64.RU34")

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
        with patch.object(gnc, "_print_diagnostics_inline"):
            self._run_prompt(["d", "b"], ctx=ctx)
            self.assertTrue(ctx["_show_diags"])

    def test_bookmark_star_adds(self):
        ctx = _base_ticket_ctx()
        state = {"bookmarks": []}
        with patch.object(gnc, "_add_bookmark", return_value=state) as ab, \
             patch.object(gnc, "_save_user_state"), \
             patch.object(gnc, "_load_user_state", return_value=state):
            self._run_prompt(["*", "b"], ctx=ctx, state=state)
            ab.assert_called_once()

    def test_bookmark_star_removes_when_exists(self):
        ctx = _base_ticket_ctx()
        state = {"bookmarks": [
            {"label": "DO-99999", "type": "ticket", "params": {"key": "DO-99999"}}
        ]}
        with patch.object(gnc, "_remove_bookmark", return_value=state) as rb, \
             patch.object(gnc, "_save_user_state"), \
             patch.object(gnc, "_load_user_state", return_value=state):
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
        gnc._my_display_name = display_name
        gnc._my_account_id = account_id

    def tearDown(self):
        gnc._my_display_name = None
        gnc._my_account_id = None

    def test_matches_display_name_case_insensitive(self):
        self._set_identity(display_name="John Smith")
        self.assertTrue(gnc._is_mine({"assignee": "john smith"}))

    def test_matches_account_id(self):
        self._set_identity(account_id="abc123")
        self.assertTrue(gnc._is_mine({"assignee": "Someone", "_assignee_account_id": "abc123"}))

    def test_matches_email_derived_name(self):
        self._set_identity()
        with patch.dict(os.environ, {"JIRA_EMAIL": "john.smith@coreweave.com"}):
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
        r = gnc._parse_rack_location("US-EVI01.DH1.R64.RU34")
        self.assertEqual(r["site_code"], "US-EVI01")
        self.assertEqual(r["dh"], "DH1")
        self.assertEqual(r["rack"], 64)
        self.assertEqual(r["ru"], "34")

    def test_no_ru(self):
        r = gnc._parse_rack_location("US-EVI01.DH1.R64")
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
    """Verify _get_physical_neighbors for Caledonia SEC1 (3 columns, non-serpentine)."""

    SEC1 = {
        "racks_per_row": 10,
        "columns": [
            {"label": "A", "start": 1, "num_rows": 5},
            {"label": "B", "start": 51, "num_rows": 5},
            {"label": "C", "start": 101, "num_rows": 5},
        ],
        "serpentine": False,
    }

    def test_col_a_middle(self):
        r = gnc._get_physical_neighbors(5, self.SEC1)
        self.assertEqual(r["left"], 4)
        self.assertEqual(r["right"], 6)

    def test_col_a_first(self):
        r = gnc._get_physical_neighbors(1, self.SEC1)
        self.assertIsNone(r["left"])
        self.assertEqual(r["right"], 2)

    def test_col_a_last_in_row(self):
        r = gnc._get_physical_neighbors(10, self.SEC1)
        self.assertEqual(r["left"], 9)
        self.assertIsNone(r["right"])

    def test_col_a_end(self):
        r = gnc._get_physical_neighbors(50, self.SEC1)
        self.assertEqual(r["left"], 49)
        self.assertIsNone(r["right"])

    def test_col_b_first(self):
        r = gnc._get_physical_neighbors(51, self.SEC1)
        self.assertIsNone(r["left"])
        self.assertEqual(r["right"], 52)

    def test_col_b_middle(self):
        r = gnc._get_physical_neighbors(75, self.SEC1)
        self.assertEqual(r["left"], 74)
        self.assertEqual(r["right"], 76)

    def test_col_c_first(self):
        r = gnc._get_physical_neighbors(101, self.SEC1)
        self.assertIsNone(r["left"])
        self.assertEqual(r["right"], 102)

    def test_col_c_last(self):
        r = gnc._get_physical_neighbors(150, self.SEC1)
        self.assertEqual(r["left"], 149)
        self.assertIsNone(r["right"])

    def test_no_cross_column_neighbors(self):
        """R50 (end of A) and R51 (start of B) are NOT neighbors."""
        r50 = gnc._get_physical_neighbors(50, self.SEC1)
        r51 = gnc._get_physical_neighbors(51, self.SEC1)
        self.assertIsNone(r50["right"])
        self.assertIsNone(r51["left"])

    def test_non_serpentine_no_reversal(self):
        """Row 1 (odd) should NOT be reversed when serpentine=False."""
        # R15 is in row 1, pos 4 — neighbors are 14 and 16 (no reversal)
        r = gnc._get_physical_neighbors(15, self.SEC1)
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

    @patch.object(gnc, "NTFY_TOPIC", "test-topic")
    @patch.object(gnc, "_NTFY_ENABLED", True)
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

    @patch.object(gnc, "NTFY_TOPIC", "test-topic")
    @patch.object(gnc, "_NTFY_ENABLED", True)
    def test_omits_tags_header_when_empty(self):
        mock_post = MagicMock()
        with patch("requests.post", mock_post):
            gnc._ntfy_send("Title", "msg")
        headers = mock_post.call_args[1]["headers"]
        self.assertNotIn("Tags", headers)

    @patch.object(gnc, "NTFY_TOPIC", "")
    @patch.object(gnc, "_NTFY_ENABLED", True)
    def test_noop_when_no_topic(self):
        mock_post = MagicMock()
        with patch("requests.post", mock_post):
            gnc._ntfy_send("Title", "msg")
        mock_post.assert_not_called()

    @patch.object(gnc, "NTFY_TOPIC", "test-topic")
    @patch.object(gnc, "_NTFY_ENABLED", False)
    def test_noop_when_disabled(self):
        mock_post = MagicMock()
        with patch("requests.post", mock_post):
            gnc._ntfy_send("Title", "msg")
        mock_post.assert_not_called()

    @patch.object(gnc, "NTFY_TOPIC", "test-topic")
    @patch.object(gnc, "_NTFY_ENABLED", True)
    def test_silent_on_network_error(self):
        mock_post = MagicMock(side_effect=Exception("Connection refused"))
        with patch("requests.post", mock_post):
            gnc._ntfy_send("Title", "msg")  # should not raise


class TestStaleUnassigned(unittest.TestCase):
    """Tests for _check_stale_unassigned()."""

    def setUp(self):
        gnc._ntfy_alerted.clear()

    @patch.object(gnc, "NTFY_TOPIC", "test-topic")
    @patch.object(gnc, "_NTFY_ENABLED", True)
    def test_alerts_on_stale_unassigned(self):
        import time, datetime as dt
        old_time = (dt.datetime.now() - dt.timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S.000+0000")
        issues = [{"key": "DO-100", "fields": {"assignee": None, "created": old_time}}]
        with patch.object(gnc, "_ntfy_send") as mock_send:
            gnc._check_stale_unassigned(issues, "LAS1")
        mock_send.assert_called_once()
        self.assertIn("DO-100", mock_send.call_args[0][1])

    @patch.object(gnc, "NTFY_TOPIC", "test-topic")
    @patch.object(gnc, "_NTFY_ENABLED", True)
    def test_skips_assigned_tickets(self):
        import datetime as dt
        old_time = (dt.datetime.now() - dt.timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S.000+0000")
        issues = [{"key": "DO-100", "fields": {"assignee": {"displayName": "Alice"}, "created": old_time}}]
        with patch.object(gnc, "_ntfy_send") as mock_send:
            gnc._check_stale_unassigned(issues, "LAS1")
        mock_send.assert_not_called()

    @patch.object(gnc, "NTFY_TOPIC", "test-topic")
    @patch.object(gnc, "_NTFY_ENABLED", True)
    def test_no_duplicate_alerts(self):
        import datetime as dt
        old_time = (dt.datetime.now() - dt.timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S.000+0000")
        issues = [{"key": "DO-100", "fields": {"assignee": None, "created": old_time}}]
        with patch.object(gnc, "_ntfy_send") as mock_send:
            gnc._check_stale_unassigned(issues, "LAS1")
            gnc._check_stale_unassigned(issues, "LAS1")  # second call
        mock_send.assert_called_once()  # only alerted once

    @patch.object(gnc, "NTFY_TOPIC", "test-topic")
    @patch.object(gnc, "_NTFY_ENABLED", True)
    def test_skips_recent_tickets(self):
        import datetime as dt
        recent = dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S.000+0000")
        issues = [{"key": "DO-100", "fields": {"assignee": None, "created": recent}}]
        with patch.object(gnc, "_ntfy_send") as mock_send:
            gnc._check_stale_unassigned(issues, "LAS1")
        mock_send.assert_not_called()


class TestSLAWarnings(unittest.TestCase):
    """Tests for _check_sla_warnings()."""

    def setUp(self):
        gnc._ntfy_alerted.clear()

    @patch.object(gnc, "NTFY_TOPIC", "test-topic")
    @patch.object(gnc, "_NTFY_ENABLED", True)
    def test_alerts_on_sla_breach(self):
        issues = [{"key": "DO-200", "fields": {
            "assignee": {"emailAddress": "me@test.com"}}}]
        sla_data = [{"name": "Time to Resolution", "ongoingCycle": {
            "breached": True, "remainingTime": {"millis": -1000, "friendly": "-1h"}}}]
        with patch.object(gnc, "_fetch_sla", return_value=sla_data), \
             patch.object(gnc, "_ntfy_send") as mock_send:
            gnc._check_sla_warnings(issues, "me@test.com", "tok")
        mock_send.assert_called_once()
        self.assertIn("breached", mock_send.call_args[0][1].lower())

    @patch.object(gnc, "NTFY_TOPIC", "test-topic")
    @patch.object(gnc, "_NTFY_ENABLED", True)
    def test_alerts_on_low_remaining(self):
        issues = [{"key": "DO-200", "fields": {
            "assignee": {"emailAddress": "me@test.com"}}}]
        sla_data = [{"name": "Time to Resolution", "ongoingCycle": {
            "breached": False, "remainingTime": {"millis": 1800000, "friendly": "30m"}}}]
        with patch.object(gnc, "_fetch_sla", return_value=sla_data), \
             patch.object(gnc, "_ntfy_send") as mock_send:
            gnc._check_sla_warnings(issues, "me@test.com", "tok")
        mock_send.assert_called_once()
        self.assertIn("30m", mock_send.call_args[0][1])

    @patch.object(gnc, "NTFY_TOPIC", "test-topic")
    @patch.object(gnc, "_NTFY_ENABLED", True)
    def test_skips_other_peoples_tickets(self):
        issues = [{"key": "DO-200", "fields": {
            "assignee": {"emailAddress": "other@test.com"}}}]
        with patch.object(gnc, "_fetch_sla") as mock_sla, \
             patch.object(gnc, "_ntfy_send") as mock_send:
            gnc._check_sla_warnings(issues, "me@test.com", "tok")
        mock_sla.assert_not_called()
        mock_send.assert_not_called()

    @patch.object(gnc, "NTFY_TOPIC", "")
    @patch.object(gnc, "_NTFY_ENABLED", True)
    def test_noop_when_no_topic(self):
        issues = [{"key": "DO-200", "fields": {
            "assignee": {"emailAddress": "me@test.com"}}}]
        with patch.object(gnc, "_fetch_sla") as mock_sla:
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
        session = {"site_code": "US-CENTRAL-07A", "dh": "DH1", "started_at": "2026-01-01T00:00:00Z"}
        with patch.object(gnc, "_save_user_state"):
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
        "The PSU with id 3 at dh1-r306-node-04-us-central-07a has failed or is unseated.\n"
        "Identify the PSU with id 3 at deviceslot dh1-r306-node-04-us-central-07a, "
        "serial S948338X5830183, rack unit 22, row 31."
    )

    def test_psu_id_extracted(self):
        info = gnc._extract_psu_info(self.SAMPLE_DESC)
        self.assertIsNotNone(info)
        self.assertEqual(info["psu_id"], "3")

    def test_deviceslot_extracted(self):
        info = gnc._extract_psu_info(self.SAMPLE_DESC)
        self.assertEqual(info["deviceslot"], "dh1-r306-node-04-us-central-07a")

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

if __name__ == "__main__":
    unittest.main()
