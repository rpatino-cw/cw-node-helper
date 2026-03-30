"""Learn mode — interactive code quiz TUI."""
from __future__ import annotations

import time

from cwhelper.config import BOLD, DIM, RESET, GREEN, RED, YELLOW, CYAN, MAGENTA, BLUE
from cwhelper.tui.rich_console import console
from cwhelper.tui.display import _clear_screen
from cwhelper.services.learn_engine import (
    _build_code_map,
    _get_static_questions,
    _get_module_names,
    _load_learn_state,
    _save_learn_state,
    _score_answer,
    _rank_from_xp,
)

__all__ = ["_run_learn_mode"]

_CHOICE_LABELS = ["A", "B", "C", "D"]


def _run_learn_mode():
    """Main learn mode entry point — shows game menu and dispatches."""
    state = _load_learn_state()

    while True:
        _clear_screen()
        _print_learn_banner(state)
        _print_learn_menu()

        try:
            choice = input(f"  Select [1-5] or q: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if choice in ("q", "quit", "back", "b"):
            return
        elif choice == "1":
            state = _run_quick_quiz(state)
        elif choice == "2":
            state = _run_deep_dive(state)
        elif choice == "3":
            state = _run_architecture_quiz(state)
        elif choice == "4":
            _show_code_map()
        elif choice == "5":
            _show_scoreboard(state)
        else:
            print(f"\n  {DIM}Invalid choice.{RESET}")
            time.sleep(0.8)


def _print_learn_banner(state: dict):
    """Print the learn mode header with rank and XP."""
    xp = state.get("total_xp", 0)
    rank = _rank_from_xp(xp)
    streak = state.get("streak", 0)
    games = state.get("games_played", 0)

    console.print()
    console.print(f"  [bold white]CWHELPER LEARN[/bold white]  [dim]Code Game[/dim]")
    console.print()

    # Stats bar
    streak_str = f"  {YELLOW}streak {streak}{RESET}" if streak > 0 else ""
    print(f"  {CYAN}{rank}{RESET}  ·  {BOLD}{xp} XP{RESET}  ·  {DIM}{games} games{RESET}{streak_str}")
    print()


def _print_learn_menu():
    """Print the learn mode menu options."""
    print(f"  {BOLD}1{RESET}  Quick Quiz       {DIM}5 random questions, timed{RESET}")
    print(f"  {BOLD}2{RESET}  Deep Dive        {DIM}pick a module, 10 questions{RESET}")
    print(f"  {BOLD}3{RESET}  Architecture     {DIM}how modules connect{RESET}")
    print(f"  {BOLD}4{RESET}  Code Map         {DIM}explore the codebase structure{RESET}")
    print(f"  {BOLD}5{RESET}  Scoreboard       {DIM}progress by module{RESET}")
    print()
    print(f"  {DIM}q = back to main menu{RESET}")
    print()


# ---------------------------------------------------------------------------
# Quick Quiz
# ---------------------------------------------------------------------------

def _run_quick_quiz(state: dict) -> dict:
    """Run a 5-question quick quiz across all modules."""
    questions = _get_static_questions(count=5)
    if not questions:
        print(f"\n  {DIM}No questions available.{RESET}")
        time.sleep(1)
        return state

    _clear_screen()
    print(f"\n  {BOLD}QUICK QUIZ{RESET}  {DIM}5 questions · all modules{RESET}\n")

    correct_count = 0
    start_time = time.time()

    for i, q in enumerate(questions):
        is_correct, state = _ask_question(q, i + 1, len(questions), state)
        if is_correct:
            correct_count += 1

    elapsed = time.time() - start_time
    state["games_played"] = state.get("games_played", 0) + 1
    _save_learn_state(state)

    _print_game_summary(correct_count, len(questions), elapsed, state)

    try:
        input(f"\n  {DIM}Press ENTER to continue...{RESET}")
    except (EOFError, KeyboardInterrupt):
        pass

    return state


# ---------------------------------------------------------------------------
# Deep Dive
# ---------------------------------------------------------------------------

def _run_deep_dive(state: dict) -> dict:
    """Pick a module and get 10 deep questions about it."""
    _clear_screen()
    modules = _get_deep_dive_modules()

    print(f"\n  {BOLD}DEEP DIVE{RESET}  {DIM}pick a module{RESET}\n")
    for i, (mod, q_count) in enumerate(modules, 1):
        # Show mastery indicator
        mod_stats = state.get("modules", {}).get(mod, {})
        total = mod_stats.get("total", 0)
        correct = mod_stats.get("correct", 0)
        if total > 0:
            pct = int(correct / total * 100)
            if pct >= 80:
                indicator = f" {GREEN}■{RESET}"
            elif pct >= 50:
                indicator = f" {YELLOW}■{RESET}"
            else:
                indicator = f" {RED}■{RESET}"
        else:
            indicator = f" {DIM}□{RESET}"

        print(f"    {BOLD}{i}{RESET}. {mod}  {DIM}({q_count} Qs){RESET}{indicator}")

    print(f"\n  {DIM}b = back{RESET}")

    try:
        choice = input(f"\n  Module [1-{len(modules)}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return state

    if choice.lower() in ("b", "back", "q"):
        return state

    try:
        idx = int(choice) - 1
        if 0 <= idx < len(modules):
            mod_name = modules[idx][0]
        else:
            return state
    except ValueError:
        return state

    questions = _get_static_questions(module=mod_name, count=10)
    if not questions:
        print(f"\n  {DIM}No questions for {mod_name}.{RESET}")
        time.sleep(1)
        return state

    _clear_screen()
    print(f"\n  {BOLD}DEEP DIVE{RESET}  {DIM}{mod_name} · {len(questions)} questions{RESET}\n")

    correct_count = 0
    start_time = time.time()

    for i, q in enumerate(questions):
        is_correct, state = _ask_question(q, i + 1, len(questions), state)
        if is_correct:
            correct_count += 1

    elapsed = time.time() - start_time
    state["games_played"] = state.get("games_played", 0) + 1
    _save_learn_state(state)

    _print_game_summary(correct_count, len(questions), elapsed, state)

    try:
        input(f"\n  {DIM}Press ENTER to continue...{RESET}")
    except (EOFError, KeyboardInterrupt):
        pass

    return state


def _get_deep_dive_modules() -> list[tuple[str, int]]:
    """Return list of (module_name, question_count) pairs with available questions."""
    module_counts = {}
    for q in _get_static_questions():
        mod = q["module"]
        module_counts[mod] = module_counts.get(mod, 0) + 1
    return sorted(module_counts.items(), key=lambda x: x[0])


# ---------------------------------------------------------------------------
# Architecture Quiz
# ---------------------------------------------------------------------------

def _run_architecture_quiz(state: dict) -> dict:
    """Run architecture-focused questions."""
    questions = _get_static_questions(module="architecture")
    if not questions:
        print(f"\n  {DIM}No architecture questions available.{RESET}")
        time.sleep(1)
        return state

    _clear_screen()
    print(f"\n  {BOLD}ARCHITECTURE{RESET}  {DIM}{len(questions)} questions · how cwhelper connects{RESET}\n")

    correct_count = 0
    start_time = time.time()

    for i, q in enumerate(questions):
        is_correct, state = _ask_question(q, i + 1, len(questions), state)
        if is_correct:
            correct_count += 1

    elapsed = time.time() - start_time
    state["games_played"] = state.get("games_played", 0) + 1
    _save_learn_state(state)

    _print_game_summary(correct_count, len(questions), elapsed, state)

    try:
        input(f"\n  {DIM}Press ENTER to continue...{RESET}")
    except (EOFError, KeyboardInterrupt):
        pass

    return state


# ---------------------------------------------------------------------------
# Code Map Explorer
# ---------------------------------------------------------------------------

def _show_code_map():
    """Display the codebase structure from AST analysis."""
    _clear_screen()
    print(f"\n  {BOLD}CODE MAP{RESET}  {DIM}cwhelper codebase structure{RESET}\n")

    code_map = _build_code_map()

    print(f"  {DIM}Total:{RESET} {code_map['total_lines']:,} lines · {code_map['total_functions']} functions\n")

    # Sort by line count descending
    sorted_mods = sorted(
        code_map["modules"].items(),
        key=lambda x: x[1]["line_count"],
        reverse=True
    )

    for short, info in sorted_mods:
        if short == "__init__.py":
            continue
        fn_count = len(info["functions"])
        lines = info["line_count"]

        # Bar visualization
        bar_len = min(40, max(1, lines // 50))
        bar = "█" * bar_len

        print(f"  {BOLD}{short:30s}{RESET} {lines:>5} L  {fn_count:>3} fn  {CYAN}{bar}{RESET}")

    print(f"\n  {DIM}Tip: Run Deep Dive on any module to test your knowledge.{RESET}")

    try:
        input(f"\n  {DIM}Press ENTER to continue...{RESET}")
    except (EOFError, KeyboardInterrupt):
        pass


# ---------------------------------------------------------------------------
# Scoreboard
# ---------------------------------------------------------------------------

def _show_scoreboard(state: dict):
    """Display progress by module and overall stats."""
    _clear_screen()
    xp = state.get("total_xp", 0)
    rank = _rank_from_xp(xp)

    print(f"\n  {BOLD}SCOREBOARD{RESET}\n")
    print(f"  Rank:         {CYAN}{rank}{RESET}")
    print(f"  Total XP:     {BOLD}{xp}{RESET}")
    print(f"  Games played: {state.get('games_played', 0)}")
    print(f"  Best streak:  {state.get('best_streak', 0)}")
    print()

    modules = state.get("modules", {})
    if modules:
        print(f"  {BOLD}{'Module':25s} {'Score':>10s}  {'Mastery':>8s}{RESET}")
        print(f"  {DIM}{'─' * 48}{RESET}")

        for mod in sorted(modules.keys()):
            stats = modules[mod]
            total = stats.get("total", 0)
            correct = stats.get("correct", 0)
            if total == 0:
                continue

            pct = int(correct / total * 100)
            score_str = f"{correct}/{total}"

            if pct >= 80:
                color = GREEN
                bar = "████"
            elif pct >= 60:
                color = YELLOW
                bar = "███░"
            elif pct >= 40:
                color = YELLOW
                bar = "██░░"
            else:
                color = RED
                bar = "█░░░"

            print(f"  {mod:25s} {score_str:>10s}  {color}{bar} {pct}%{RESET}")
    else:
        print(f"  {DIM}No games played yet. Try Quick Quiz!{RESET}")

    print()
    try:
        input(f"  {DIM}Press ENTER to continue...{RESET}")
    except (EOFError, KeyboardInterrupt):
        pass


# ---------------------------------------------------------------------------
# Shared question asking
# ---------------------------------------------------------------------------

def _ask_question(question: dict, num: int, total: int, state: dict) -> tuple[bool, dict]:
    """Present a single multiple-choice question and score the answer.

    Returns (correct: bool, updated_state: dict).
    """
    diff_str = {1: f"{DIM}easy{RESET}", 2: f"{YELLOW}medium{RESET}", 3: f"{RED}hard{RESET}"}.get(
        question.get("difficulty", 1), ""
    )
    mod = question.get("module", "")

    print(f"  {DIM}[{num}/{total}]{RESET}  {DIM}{mod}{RESET}  {diff_str}")
    print(f"  {BOLD}{question['question']}{RESET}\n")

    for i, choice in enumerate(question["choices"]):
        print(f"    {BOLD}{_CHOICE_LABELS[i]}{RESET}. {choice}")

    print()
    try:
        raw = input(f"  Answer [A-D]: ").strip().upper()
    except (EOFError, KeyboardInterrupt):
        print()
        raw = ""

    if raw in _CHOICE_LABELS:
        user_answer = _CHOICE_LABELS.index(raw)
    else:
        print(f"  {DIM}Skipped.{RESET}\n")
        return False, state

    correct, state = _score_answer(state, question, user_answer)

    correct_label = _CHOICE_LABELS[question["answer"]]
    if correct:
        xp = question.get("difficulty", 1) * 10
        print(f"  {GREEN}Correct!{RESET} +{xp} XP")
    else:
        print(f"  {RED}Wrong.{RESET} Answer: {BOLD}{correct_label}{RESET}")

    print(f"  {DIM}{question['explanation']}{RESET}\n")

    return correct, state


def _print_game_summary(correct: int, total: int, elapsed: float, state: dict):
    """Print end-of-game summary."""
    pct = int(correct / total * 100) if total > 0 else 0
    rank = _rank_from_xp(state.get("total_xp", 0))

    print(f"\n  {'━' * 40}")
    print(f"  {BOLD}Results:{RESET} {correct}/{total} ({pct}%) in {elapsed:.0f}s")
    print(f"  {CYAN}{rank}{RESET}  ·  {BOLD}{state.get('total_xp', 0)} XP{RESET}  ·  streak {state.get('streak', 0)}")

    if pct == 100:
        print(f"  {GREEN}Perfect score!{RESET}")
    elif pct >= 80:
        print(f"  {GREEN}Great job!{RESET}")
    elif pct >= 60:
        print(f"  {YELLOW}Good effort — review the misses.{RESET}")
    else:
        print(f"  {RED}Room to grow — try Deep Dive on weak areas.{RESET}")
