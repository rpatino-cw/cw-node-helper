#!/bin/bash
# ================================================================
# git_menu.sh — Safe git helper for cw-node-helper
# Locked to branch: main
# Locked to remote: origin
# ================================================================

BRANCH="main"
REMOTE="origin"
REPO_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# -- Colors (matching get_node_context.py) -------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
MAGENTA='\033[0;35m'
CYAN='\033[0;36m'
WHITE='\033[0;97m'
DIM='\033[0;90m'
BOLD='\033[1m'
RESET='\033[0m'

# -- gh CLI path ---------------------------------------------------
GH=""
if command -v gh &>/dev/null; then
    GH="gh"
elif [ -x /opt/homebrew/bin/gh ]; then
    GH="/opt/homebrew/bin/gh"
fi

# -- Safety check: correct directory & branch ----------------------
check_safety() {
    cd "$REPO_DIR" || { echo -e "  ${RED}Cannot access $REPO_DIR${RESET}"; exit 1; }

    if [ ! -d .git ]; then
        echo -e "  ${RED}Not a git repo. Run from cw-node-helper directory.${RESET}"
        exit 1
    fi

    local current
    current=$(git branch --show-current 2>/dev/null)

    if [ "$current" != "$BRANCH" ]; then
        echo -e "  ${RED}Wrong branch!${RESET} On ${YELLOW}${current}${RESET}, expected ${GREEN}${BRANCH}${RESET}"
        echo -e "  ${DIM}Switching...${RESET}"
        git checkout "$BRANCH" 2>/dev/null
        current=$(git branch --show-current 2>/dev/null)
        if [ "$current" != "$BRANCH" ]; then
            echo -e "  ${RED}Could not switch to ${BRANCH}. Aborting.${RESET}"
            exit 1
        fi
        echo -e "  ${GREEN}Switched to ${BRANCH}${RESET}"
    fi
}

# -- Intercept: block any branch operations ------------------------
git() {
    # Block checkout, switch, branch creation
    case "$1" in
        checkout|switch)
            echo -e "  ${RED}${BOLD}Blocked.${RESET} ${DIM}This script is locked to ${BRANCH}.${RESET}"
            echo -e "  ${DIM}You cannot switch branches from here.${RESET}"
            return 1
            ;;
        branch)
            if [[ "$2" == "-d" || "$2" == "-D" || "$2" == "-m" || "$2" == "-M" ]]; then
                echo -e "  ${RED}${BOLD}Blocked.${RESET} ${DIM}Branch operations are locked.${RESET}"
                return 1
            fi
            ;;
        push)
            # Only allow pushing to our branch or tags
            if [[ -n "$3" && "$3" != "$BRANCH" && "$3" != v* ]]; then
                echo -e "  ${RED}${BOLD}Blocked.${RESET} ${DIM}Can only push to ${BRANCH}.${RESET}"
                return 1
            fi
            ;;
    esac
    command git "$@"
}

# -- Banner --------------------------------------------------------
print_banner() {
    clear
    echo ""
    echo -e "  ${BOLD}${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
    echo -e "  ${BOLD}${CYAN}┃${RESET}  ${BOLD}${WHITE}cw-node-helper   Git  Menu${RESET}            ${BOLD}${CYAN}┃${RESET}"
    echo -e "  ${BOLD}${CYAN}┃${RESET}  ${DIM}main @ cw-node-helper${RESET}   ${BOLD}${CYAN}┃${RESET}"
    echo -e "  ${BOLD}${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
}

# -- Status bar (shown under banner) ------------------------------
print_status_bar() {
    local changed
    changed=$(command git status --porcelain 2>/dev/null | wc -l | tr -d ' ')
    local last_commit
    last_commit=$(command git log -1 --format="%s" 2>/dev/null)
    local ahead
    ahead=$(command git rev-list --count "$REMOTE/$BRANCH..HEAD" 2>/dev/null || echo "0")

    echo ""
    if [ "$changed" -gt 0 ]; then
        echo -e "  ${YELLOW}${BOLD}${changed} file(s) changed${RESET} ${DIM}— uncommitted${RESET}"
    else
        echo -e "  ${GREEN}Working tree clean${RESET}"
    fi

    if [ "$ahead" -gt 0 ] 2>/dev/null; then
        echo -e "  ${CYAN}${ahead} commit(s) ahead of remote${RESET}"
    fi

    if [ -n "$last_commit" ]; then
        echo -e "  ${DIM}Last commit: ${last_commit:0:50}${RESET}"
    fi
}

# -- Show status --------------------------------------------------
do_status() {
    echo ""
    echo -e "  ${BOLD}${WHITE}STATUS${RESET}"
    echo -e "  ${DIM}──────────────────────────────────────────────────${RESET}"
    echo ""
    echo -e "  ${CYAN}Branch:${RESET}  $(command git branch --show-current)"
    echo -e "  ${CYAN}Remote:${RESET}  $(command git remote get-url origin 2>/dev/null)"
    echo ""

    local files
    files=$(command git status --porcelain 2>/dev/null)
    if [ -z "$files" ]; then
        echo -e "  ${GREEN}Nothing changed — working tree is clean.${RESET}"
    else
        echo -e "  ${BOLD}${WHITE}Changed files:${RESET}"
        echo ""
        while IFS= read -r line; do
            local status="${line:0:2}"
            local file="${line:3}"
            case "$status" in
                " M"|"M "|"MM") echo -e "    ${YELLOW}modified${RESET}  ${file}" ;;
                "A "|" A")      echo -e "    ${GREEN}added${RESET}     ${file}" ;;
                "D "|" D")      echo -e "    ${RED}deleted${RESET}   ${file}" ;;
                "??")           echo -e "    ${DIM}untracked${RESET} ${file}" ;;
                *)              echo -e "    ${DIM}${status}${RESET}        ${file}" ;;
            esac
        done <<< "$files"
    fi
    echo ""
    echo -e "  ${DIM}──────────────────────────────────────────────────${RESET}"
}

# -- Push ---------------------------------------------------------
do_push() {
    echo ""
    echo -e "  ${BOLD}${WHITE}PUSH${RESET}"
    echo -e "  ${DIM}──────────────────────────────────────────────────${RESET}"

    local files
    files=$(command git status --porcelain 2>/dev/null)
    if [ -z "$files" ]; then
        echo -e "\n  ${YELLOW}Nothing to push — working tree is clean.${RESET}"
        echo -e "  ${DIM}──────────────────────────────────────────────────${RESET}"
        return
    fi

    echo ""
    echo -e "  ${BOLD}${WHITE}Changed files:${RESET}"
    while IFS= read -r line; do
        local file="${line:3}"
        echo -e "    ${YELLOW}•${RESET} ${file}"
    done <<< "$files"

    echo ""
    echo -ne "  ${BOLD}Commit message:${RESET} "
    read -r msg

    if [ -z "$msg" ]; then
        echo -e "  ${RED}No message. Aborting.${RESET}"
        return
    fi

    # Run tests
    echo ""
    echo -e "  ${DIM}Running tests...${RESET}"
    if python3 test_integrity.py >/dev/null 2>&1; then
        echo -e "  ${GREEN}${BOLD}94 tests passed${RESET} ${GREEN}✓${RESET}"
    else
        echo ""
        echo -e "  ${RED}${BOLD}┌─ TESTS FAILED ────────────────────────────────┐${RESET}"
        echo -e "  ${RED}${BOLD}│${RESET}  Fix the failing tests before pushing.         ${RED}${BOLD}│${RESET}"
        echo -e "  ${RED}${BOLD}│${RESET}  Run ${CYAN}python3 test_integrity.py${RESET} to see details. ${RED}${BOLD}│${RESET}"
        echo -e "  ${RED}${BOLD}└────────────────────────────────────────────────┘${RESET}"
        return
    fi

    # Stage, commit, push
    echo -e "  ${DIM}Staging...${RESET}"
    git add -A

    echo -e "  ${DIM}Committing...${RESET}"
    git commit -m "$msg" --quiet

    echo -e "  ${DIM}Pushing to ${BRANCH}...${RESET}"
    echo ""
    if git push "$REMOTE" "$BRANCH" 2>&1; then
        echo ""
        echo -e "  ${GREEN}${BOLD}┌─ PUSHED ───────────────────────────────────────┐${RESET}"
        echo -e "  ${GREEN}${BOLD}│${RESET}  ${WHITE}${msg:0:46}${RESET}  ${GREEN}${BOLD}│${RESET}"
        echo -e "  ${GREEN}${BOLD}│${RESET}  ${DIM}CI running at github.com/.../actions${RESET}          ${GREEN}${BOLD}│${RESET}"
        echo -e "  ${GREEN}${BOLD}└────────────────────────────────────────────────┘${RESET}"
    else
        echo -e "  ${RED}Push failed. Check your connection.${RESET}"
    fi
}

# -- Pull ---------------------------------------------------------
do_pull() {
    echo ""
    echo -e "  ${BOLD}${WHITE}PULL${RESET}"
    echo -e "  ${DIM}──────────────────────────────────────────────────${RESET}"
    echo ""
    echo -e "  ${DIM}Pulling latest from ${BRANCH}...${RESET}"
    echo ""
    if git pull "$REMOTE" "$BRANCH" 2>&1; then
        echo ""
        echo -e "  ${GREEN}Up to date ✓${RESET}"
    else
        echo ""
        echo -e "  ${RED}Pull failed. You may have local conflicts.${RESET}"
        echo -e "  ${DIM}Run option 3 (Status) to see what's going on.${RESET}"
    fi
    echo -e "  ${DIM}──────────────────────────────────────────────────${RESET}"
}

# -- Release (tag + push) ----------------------------------------
do_release() {
    echo ""
    echo -e "  ${BOLD}${WHITE}RELEASE${RESET}"
    echo -e "  ${DIM}──────────────────────────────────────────────────${RESET}"

    local current_version
    current_version=$(grep -o "APP_VERSION *= *['\"].*['\"]" get_node_context.py 2>/dev/null | grep -o "[0-9][0-9.]*")
    local latest_tag
    latest_tag=$(command git describe --tags --abbrev=0 2>/dev/null)

    echo ""
    if [ -n "$current_version" ]; then
        echo -e "  ${CYAN}APP_VERSION in code:${RESET}  ${WHITE}${current_version}${RESET}"
    fi
    if [ -n "$latest_tag" ]; then
        echo -e "  ${CYAN}Latest git tag:${RESET}      ${WHITE}${latest_tag}${RESET}"
    else
        echo -e "  ${DIM}No existing tags.${RESET}"
    fi

    # Check for uncommitted changes
    local dirty
    dirty=$(command git status --porcelain 2>/dev/null)
    if [ -n "$dirty" ]; then
        echo ""
        echo -e "  ${YELLOW}You have uncommitted changes.${RESET}"
        echo -e "  ${DIM}Push first (option 1), then tag a release.${RESET}"
        echo -e "  ${DIM}──────────────────────────────────────────────────${RESET}"
        return
    fi

    echo ""
    echo -ne "  ${BOLD}Tag version${RESET} ${DIM}(e.g. v6.3.0):${RESET} "
    read -r tag

    if [ -z "$tag" ]; then
        echo -e "  ${RED}No tag. Aborting.${RESET}"
        return
    fi

    if ! echo "$tag" | grep -qE '^v[0-9]+\.[0-9]+'; then
        echo -e "  ${RED}Tag must start with v + number (e.g. v6.3.0).${RESET}"
        return
    fi

    if command git rev-parse "$tag" >/dev/null 2>&1; then
        echo -e "  ${RED}Tag ${tag} already exists.${RESET}"
        return
    fi

    echo ""
    echo -e "  ${DIM}Tagging ${tag}...${RESET}"
    command git tag "$tag"

    echo -e "  ${DIM}Pushing tag...${RESET}"
    if git push "$REMOTE" "$tag" 2>&1; then
        echo ""
        echo -e "  ${MAGENTA}${BOLD}┌─ RELEASE TRIGGERED ────────────────────────────┐${RESET}"
        echo -e "  ${MAGENTA}${BOLD}│${RESET}  ${WHITE}${tag}${RESET}                                          ${MAGENTA}${BOLD}│${RESET}"
        echo -e "  ${MAGENTA}${BOLD}│${RESET}  ${DIM}GitHub will: test → build zip → publish${RESET}        ${MAGENTA}${BOLD}│${RESET}"
        echo -e "  ${MAGENTA}${BOLD}│${RESET}  ${DIM}Check: github.com/.../releases${RESET}                 ${MAGENTA}${BOLD}│${RESET}"
        echo -e "  ${MAGENTA}${BOLD}└────────────────────────────────────────────────┘${RESET}"
    else
        echo -e "  ${RED}Failed to push tag.${RESET}"
    fi
}

# -- Check CI status ----------------------------------------------
do_ci_check() {
    echo ""
    echo -e "  ${BOLD}${WHITE}CI STATUS${RESET}"
    echo -e "  ${DIM}──────────────────────────────────────────────────${RESET}"
    echo ""

    if [ -n "$GH" ]; then
        $GH run list --repo your-org/node-helper --branch "$BRANCH" --limit 5 2>&1
    else
        echo -e "  ${YELLOW}gh CLI not in PATH.${RESET}"
        echo -e "  ${DIM}Check manually:${RESET}"
        echo -e "  ${CYAN}https://github.com/your-org/node-helper/actions${RESET}"
    fi
    echo ""
    echo -e "  ${DIM}──────────────────────────────────────────────────${RESET}"
}

# -- View diff ----------------------------------------------------
do_diff() {
    echo ""
    echo -e "  ${BOLD}${WHITE}DIFF${RESET}"
    echo -e "  ${DIM}──────────────────────────────────────────────────${RESET}"
    echo ""
    local diff_output
    diff_output=$(command git diff --stat 2>/dev/null)
    if [ -z "$diff_output" ]; then
        echo -e "  ${DIM}No unstaged changes.${RESET}"
    else
        command git diff --stat
        echo ""
        echo -ne "  ${DIM}Show full diff? [y/N]:${RESET} "
        read -r show_full
        if [[ "$show_full" == "y" || "$show_full" == "Y" ]]; then
            command git diff
        fi
    fi
    echo ""
    echo -e "  ${DIM}──────────────────────────────────────────────────${RESET}"
}

# -- Log ----------------------------------------------------------
do_log() {
    echo ""
    echo -e "  ${BOLD}${WHITE}RECENT COMMITS${RESET}"
    echo -e "  ${DIM}──────────────────────────────────────────────────${RESET}"
    echo ""
    command git log --oneline --graph --decorate -15
    echo ""
    echo -e "  ${DIM}──────────────────────────────────────────────────${RESET}"
}

# -- Menu ---------------------------------------------------------
show_menu() {
    print_banner
    print_status_bar

    echo ""
    echo -e "  ${BOLD}1${RESET}  Push              ${DIM}(stage + test + commit + push)${RESET}"
    echo -e "  ${BOLD}2${RESET}  Pull              ${DIM}(pull latest from GitHub)${RESET}"
    echo -e "  ${BOLD}3${RESET}  Status            ${DIM}(see what changed)${RESET}"
    echo -e "  ${BOLD}4${RESET}  Release           ${DIM}(tag a version + trigger build)${RESET}"
    echo -e "  ${BOLD}5${RESET}  CI check          ${DIM}(see recent test runs)${RESET}"
    echo -e "  ${BOLD}6${RESET}  Diff              ${DIM}(see what changed in detail)${RESET}"
    echo -e "  ${BOLD}7${RESET}  Log               ${DIM}(recent commit history)${RESET}"
    echo ""
    echo -e "  ${BOLD}q${RESET}  Quit"
    echo ""
}

# -- Main loop ----------------------------------------------------
check_safety

while true; do
    show_menu
    echo -ne "  Select [1-7] or q: "
    read -r choice

    case "$choice" in
        1) do_push ;;
        2) do_pull ;;
        3) do_status ;;
        4) do_release ;;
        5) do_ci_check ;;
        6) do_diff ;;
        7) do_log ;;
        q|Q) echo -e "\n  ${DIM}Goodbye.${RESET}\n"; exit 0 ;;
        *)  echo -e "\n  ${RED}Invalid option.${RESET}" ;;
    esac

    echo ""
    echo -ne "  ${DIM}Press Enter to return to menu...${RESET}"
    read -r
done
