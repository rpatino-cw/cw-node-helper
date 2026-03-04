#!/bin/bash
# ================================================================
# start.sh — CW Node Helper Launcher
# One script to run, update, and manage everything.
# Designed for non-technical users. Just type: bash start.sh
# ================================================================

BRANCH="rpatino/cw-node-helper"
REMOTE="origin"
REPO_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# -- Colors -------------------------------------------------------
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

cd "$REPO_DIR" || { echo -e "  ${RED}Cannot find the app folder.${RESET}"; exit 1; }

# ================================================================
#  HELPERS
# ================================================================

_pause() {
    echo ""
    echo -ne "  ${DIM}Press Enter to go back to menu...${RESET}"
    read -r
}

_banner() {
    clear
    echo ""
    echo -e "  ${BOLD}${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
    echo -e "  ${BOLD}${CYAN}┃${RESET}  ${BOLD}${WHITE}CW Node Helper${RESET}                       ${BOLD}${CYAN}┃${RESET}"
    echo -e "  ${BOLD}${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
}

_check_python() {
    if ! command -v python3 &>/dev/null; then
        echo -e "  ${RED}Python 3 is not installed.${RESET}"
        echo -e "  ${DIM}Install it by running:${RESET}  ${WHITE}brew install python3${RESET}"
        return 1
    fi
    return 0
}

_check_requests() {
    if ! python3 -c "import requests" 2>/dev/null; then
        echo -e "  ${YELLOW}Installing 'requests' library...${RESET}"
        pip3 install requests --quiet 2>/dev/null
        if ! python3 -c "import requests" 2>/dev/null; then
            echo -e "  ${RED}Could not install 'requests'.${RESET}"
            echo -e "  ${DIM}Try manually:${RESET}  ${WHITE}pip3 install requests${RESET}"
            return 1
        fi
        echo -e "  ${GREEN}Installed requests ✓${RESET}"
    fi
    return 0
}

_check_env() {
    if [ ! -f "$REPO_DIR/.env" ]; then
        return 1
    fi
    return 0
}

_check_env_has_real_values() {
    if grep -q "paste_your_jira_token_here" "$REPO_DIR/.env" 2>/dev/null; then
        return 1
    fi
    if grep -q "your.name@coreweave.com" "$REPO_DIR/.env" 2>/dev/null; then
        return 1
    fi
    return 0
}

_backup_personal_files() {
    local backup_dir="/tmp/cwhelper_backup_$$"
    mkdir -p "$backup_dir"
    [ -f "$REPO_DIR/.env" ] && cp "$REPO_DIR/.env" "$backup_dir/.env"
    [ -f "$REPO_DIR/.cwhelper_state.json" ] && cp "$REPO_DIR/.cwhelper_state.json" "$backup_dir/.cwhelper_state.json"
    [ -f "$REPO_DIR/dh_layouts.json" ] && cp "$REPO_DIR/dh_layouts.json" "$backup_dir/dh_layouts.json"
    echo "$backup_dir"
}

_restore_personal_files() {
    local backup_dir="$1"
    [ -f "$backup_dir/.env" ] && cp "$backup_dir/.env" "$REPO_DIR/.env"
    [ -f "$backup_dir/.cwhelper_state.json" ] && cp "$backup_dir/.cwhelper_state.json" "$REPO_DIR/.cwhelper_state.json"
    [ -f "$backup_dir/dh_layouts.json" ] && cp "$backup_dir/dh_layouts.json" "$REPO_DIR/dh_layouts.json"
    rm -rf "$backup_dir"
}

_ensure_branch() {
    local current
    current=$(command git branch --show-current 2>/dev/null)
    if [ "$current" != "$BRANCH" ]; then
        command git checkout "$BRANCH" 2>/dev/null
        current=$(command git branch --show-current 2>/dev/null)
        if [ "$current" != "$BRANCH" ]; then
            echo -e "  ${RED}Could not switch to the right branch.${RESET}"
            echo -e "  ${DIM}Expected: ${BRANCH}${RESET}"
            return 1
        fi
    fi
    return 0
}

_mask_token() {
    local val="$1"
    local len=${#val}
    if [ "$len" -le 8 ]; then
        echo "****"
    else
        echo "${val:0:4}...${val: -4}"
    fi
}

# ================================================================
#  1. RUN THE APP
# ================================================================
do_run() {
    echo ""
    echo -e "  ${BOLD}${WHITE}LAUNCHING APP${RESET}"
    echo -e "  ${DIM}──────────────────────────────────────────────────${RESET}"
    echo ""

    # Pre-flight checks
    if ! _check_env; then
        echo -e "  ${YELLOW}No credentials file found (.env).${RESET}"
        echo -e "  ${DIM}Let's set that up first.${RESET}"
        echo ""
        _pause
        do_setup
        if ! _check_env; then
            echo -e "  ${RED}Setup was not completed. Can't run without credentials.${RESET}"
            return
        fi
    fi

    if ! _check_env_has_real_values; then
        echo -e "  ${YELLOW}Your .env file still has placeholder values.${RESET}"
        echo -e "  ${DIM}Run option 4 (First-time setup) to enter your real credentials.${RESET}"
        return
    fi

    if ! _check_python; then
        return
    fi

    _check_requests || return

    # Load env and run
    echo -e "  ${DIM}Loading credentials...${RESET}"
    eval "$(grep -v '^\s*#' "$REPO_DIR/.env" | grep -v '^\s*$' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' | sed 's/^/export /')"

    echo -e "  ${GREEN}Starting cw-node-helper...${RESET}"
    echo -e "  ${DIM}──────────────────────────────────────────────────${RESET}"
    echo ""

    python3 "$REPO_DIR/get_node_context.py"

    echo ""
    echo -e "  ${DIM}App exited. Back to launcher.${RESET}"
}

# ================================================================
#  2. UPDATE + RUN
# ================================================================
do_update_and_run() {
    do_update
    echo ""
    do_run
}

# ================================================================
#  3. UPDATE ONLY
# ================================================================
do_update() {
    echo ""
    echo -e "  ${BOLD}${WHITE}UPDATING${RESET}"
    echo -e "  ${DIM}──────────────────────────────────────────────────${RESET}"
    echo ""

    _ensure_branch || return

    # Back up personal files
    echo -e "  ${DIM}Backing up your settings...${RESET}"
    local backup_dir
    backup_dir=$(_backup_personal_files)

    # Stash any local changes to prevent conflicts
    local stashed=false
    local dirty
    dirty=$(command git status --porcelain 2>/dev/null)
    if [ -n "$dirty" ]; then
        echo -e "  ${DIM}Saving local changes...${RESET}"
        command git stash --quiet 2>/dev/null && stashed=true
    fi

    # Pull
    echo -e "  ${DIM}Downloading latest version...${RESET}"
    echo ""
    if command git pull "$REMOTE" "$BRANCH" 2>&1; then
        echo ""
        echo -e "  ${GREEN}${BOLD}Updated ✓${RESET}"
    else
        echo ""
        echo -e "  ${RED}Update failed.${RESET}"
        echo -e "  ${DIM}Check your internet connection and try again.${RESET}"
    fi

    # Restore personal files (always, even if pull failed)
    echo -e "  ${DIM}Restoring your settings...${RESET}"
    _restore_personal_files "$backup_dir"

    # Pop stash if we stashed
    if [ "$stashed" = true ]; then
        command git stash pop --quiet 2>/dev/null
    fi

    echo -e "  ${DIM}──────────────────────────────────────────────────${RESET}"
}

# ================================================================
#  4. FIRST-TIME SETUP
# ================================================================
do_setup() {
    echo ""
    echo -e "  ${BOLD}${WHITE}FIRST-TIME SETUP${RESET}"
    echo -e "  ${DIM}──────────────────────────────────────────────────${RESET}"
    echo ""

    # Check if .env already exists with real values
    if _check_env && _check_env_has_real_values; then
        echo -e "  ${GREEN}You already have credentials set up.${RESET}"
        echo ""
        echo -ne "  ${BOLD}Overwrite with new credentials? [y/N]:${RESET} "
        read -r overwrite
        if [[ "$overwrite" != "y" && "$overwrite" != "Y" ]]; then
            echo -e "  ${DIM}Keeping existing credentials.${RESET}"
            echo -e "  ${DIM}──────────────────────────────────────────────────${RESET}"
            return
        fi
    fi

    # Create .env from template
    if [ -f "$REPO_DIR/.env.example" ]; then
        cp "$REPO_DIR/.env.example" "$REPO_DIR/.env"
    else
        # Create manually if template is missing
        cat > "$REPO_DIR/.env" << 'ENVEOF'
JIRA_EMAIL=your.name@coreweave.com
JIRA_API_TOKEN=paste_your_jira_token_here
NETBOX_API_URL=https://coreweave.cloud.netboxapp.com/api
NETBOX_API_TOKEN=paste_your_netbox_token_here
ENVEOF
    fi

    echo -e "  ${CYAN}Let's set up your credentials.${RESET}"
    echo -e "  ${DIM}These are stored locally on your machine and never shared.${RESET}"
    echo ""

    # Email
    echo -e "  ${BOLD}Step 1 of 3: Your CoreWeave email${RESET}"
    echo -e "  ${DIM}(example: john.doe@coreweave.com)${RESET}"
    echo ""
    echo -ne "  Email: "
    read -r user_email

    if [ -z "$user_email" ]; then
        echo -e "  ${RED}No email entered. Setup cancelled.${RESET}"
        return
    fi

    # Jira token
    echo ""
    echo -e "  ${BOLD}Step 2 of 3: Your Jira API token${RESET}"
    echo -e "  ${DIM}Get one here: https://id.atlassian.com/manage-profile/security/api-tokens${RESET}"
    echo -e "  ${DIM}Click 'Create API token', name it 'cw-node-helper', copy the token.${RESET}"
    echo ""
    echo -ne "  Jira token: "
    read -r user_jira_token

    if [ -z "$user_jira_token" ]; then
        echo -e "  ${RED}No token entered. Setup cancelled.${RESET}"
        return
    fi

    # Write email and token
    if [[ "$OSTYPE" == "darwin"* ]]; then
        sed -i '' "s|JIRA_EMAIL=.*|JIRA_EMAIL=${user_email}|" "$REPO_DIR/.env"
        sed -i '' "s|JIRA_API_TOKEN=.*|JIRA_API_TOKEN=${user_jira_token}|" "$REPO_DIR/.env"
    else
        sed -i "s|JIRA_EMAIL=.*|JIRA_EMAIL=${user_email}|" "$REPO_DIR/.env"
        sed -i "s|JIRA_API_TOKEN=.*|JIRA_API_TOKEN=${user_jira_token}|" "$REPO_DIR/.env"
    fi

    # NetBox (optional)
    echo ""
    echo -e "  ${BOLD}Step 3 of 3: NetBox token (optional)${RESET}"
    echo -e "  ${DIM}This enables rack views and device lookups.${RESET}"
    echo -e "  ${DIM}If you don't have one, just press Enter to skip.${RESET}"
    echo ""
    echo -ne "  NetBox token (or Enter to skip): "
    read -r user_netbox_token

    if [ -n "$user_netbox_token" ]; then
        if [[ "$OSTYPE" == "darwin"* ]]; then
            sed -i '' "s|NETBOX_API_TOKEN=.*|NETBOX_API_TOKEN=${user_netbox_token}|" "$REPO_DIR/.env"
        else
            sed -i "s|NETBOX_API_TOKEN=.*|NETBOX_API_TOKEN=${user_netbox_token}|" "$REPO_DIR/.env"
        fi
        echo -e "  ${GREEN}NetBox token saved ✓${RESET}"
    else
        echo -e "  ${DIM}Skipped — you can add it later by editing .env${RESET}"
    fi

    # Install requests if needed
    echo ""
    _check_python && _check_requests

    # Show summary
    echo ""
    echo -e "  ${GREEN}${BOLD}┌─ SETUP COMPLETE ──────────────────────────────┐${RESET}"
    echo -e "  ${GREEN}${BOLD}│${RESET}  ${WHITE}Email:${RESET}   ${user_email}"
    echo -e "  ${GREEN}${BOLD}│${RESET}  ${WHITE}Jira:${RESET}    $(_mask_token "$user_jira_token")"
    if [ -n "$user_netbox_token" ]; then
        echo -e "  ${GREEN}${BOLD}│${RESET}  ${WHITE}NetBox:${RESET}  $(_mask_token "$user_netbox_token")"
    else
        echo -e "  ${GREEN}${BOLD}│${RESET}  ${WHITE}NetBox:${RESET}  ${DIM}not configured${RESET}"
    fi
    echo -e "  ${GREEN}${BOLD}│${RESET}"
    echo -e "  ${GREEN}${BOLD}│${RESET}  ${DIM}Use option 1 to run the app!${RESET}"
    echo -e "  ${GREEN}${BOLD}└────────────────────────────────────────────────┘${RESET}"
    echo -e "  ${DIM}──────────────────────────────────────────────────${RESET}"
}

# ================================================================
#  5. FIX PROBLEMS (health check)
# ================================================================
do_health_check() {
    echo ""
    echo -e "  ${BOLD}${WHITE}HEALTH CHECK${RESET}"
    echo -e "  ${DIM}──────────────────────────────────────────────────${RESET}"
    echo ""

    local issues=0

    # Python
    echo -ne "  Python 3 ........... "
    if command -v python3 &>/dev/null; then
        local pyver
        pyver=$(python3 --version 2>&1 | awk '{print $2}')
        echo -e "${GREEN}✓${RESET} ${DIM}(${pyver})${RESET}"
    else
        echo -e "${RED}✗ NOT INSTALLED${RESET}"
        echo -e "    ${DIM}Fix: ${WHITE}brew install python3${RESET}"
        issues=$((issues + 1))
    fi

    # requests module
    echo -ne "  requests library ... "
    if python3 -c "import requests" 2>/dev/null; then
        echo -e "${GREEN}✓${RESET}"
    else
        echo -e "${YELLOW}✗ MISSING${RESET}"
        echo -e "    ${DIM}Auto-fixing...${RESET}"
        pip3 install requests --quiet 2>/dev/null
        if python3 -c "import requests" 2>/dev/null; then
            echo -e "    ${GREEN}Installed ✓${RESET}"
        else
            echo -e "    ${RED}Could not install. Try: pip3 install requests${RESET}"
            issues=$((issues + 1))
        fi
    fi

    # .env file
    echo -ne "  Credentials (.env).. "
    if _check_env; then
        if _check_env_has_real_values; then
            echo -e "${GREEN}✓${RESET}"
        else
            echo -e "${YELLOW}✗ PLACEHOLDER VALUES${RESET}"
            echo -e "    ${DIM}Fix: Run option 4 (First-time setup) to enter your real credentials${RESET}"
            issues=$((issues + 1))
        fi
    else
        echo -e "${RED}✗ MISSING${RESET}"
        echo -e "    ${DIM}Fix: Run option 4 (First-time setup)${RESET}"
        issues=$((issues + 1))
    fi

    # Git repo
    echo -ne "  Git repository ..... "
    if [ -d "$REPO_DIR/.git" ]; then
        echo -e "${GREEN}✓${RESET}"
    else
        echo -e "${RED}✗ NOT A GIT REPO${RESET}"
        echo -e "    ${DIM}Something is wrong with the installation.${RESET}"
        echo -e "    ${DIM}Re-clone: git clone https://github.com/coreweave/TopoWeave.git${RESET}"
        issues=$((issues + 1))
    fi

    # Correct branch
    echo -ne "  Branch ............. "
    local current_branch
    current_branch=$(command git branch --show-current 2>/dev/null)
    if [ "$current_branch" = "$BRANCH" ]; then
        echo -e "${GREEN}✓${RESET} ${DIM}(${BRANCH})${RESET}"
    else
        echo -e "${YELLOW}✗ WRONG BRANCH${RESET} ${DIM}(on: ${current_branch})${RESET}"
        echo -e "    ${DIM}Auto-fixing...${RESET}"
        command git checkout "$BRANCH" 2>/dev/null
        if [ "$(command git branch --show-current 2>/dev/null)" = "$BRANCH" ]; then
            echo -e "    ${GREEN}Switched to ${BRANCH} ✓${RESET}"
        else
            echo -e "    ${RED}Could not switch. Ask Ricky for help.${RESET}"
            issues=$((issues + 1))
        fi
    fi

    # Main app file
    echo -ne "  App file ........... "
    if [ -f "$REPO_DIR/get_node_context.py" ]; then
        echo -e "${GREEN}✓${RESET}"
    else
        echo -e "${RED}✗ MISSING${RESET}"
        echo -e "    ${DIM}The main app file is missing. Try: option 3 (Update)${RESET}"
        issues=$((issues + 1))
    fi

    # Summary
    echo ""
    if [ "$issues" -eq 0 ]; then
        echo -e "  ${GREEN}${BOLD}Everything looks good!${RESET} ${DIM}Use option 1 to run the app.${RESET}"
    else
        echo -e "  ${YELLOW}${BOLD}${issues} issue(s) found.${RESET} ${DIM}Fix them above, then try again.${RESET}"
    fi
    echo -e "  ${DIM}──────────────────────────────────────────────────${RESET}"
}

# ================================================================
#  6. OPEN SETUP GUIDE
# ================================================================
do_open_guide() {
    echo ""
    if [ -f "$REPO_DIR/site/setup-guide.html" ]; then
        echo -e "  ${DIM}Opening setup guide in your browser...${RESET}"
        open "$REPO_DIR/site/setup-guide.html" 2>/dev/null || xdg-open "$REPO_DIR/site/setup-guide.html" 2>/dev/null
        echo -e "  ${GREEN}Opened ✓${RESET}"
    else
        echo -e "  ${YELLOW}Setup guide not found.${RESET}"
        echo -e "  ${DIM}Try updating first (option 3).${RESET}"
    fi
}

# ================================================================
#  MENU
# ================================================================
show_menu() {
    _banner

    echo ""
    echo -e "  ${BOLD}1${RESET}  Run the app"
    echo -e "  ${BOLD}2${RESET}  Update + Run        ${DIM}(get latest code, then launch)${RESET}"
    echo -e "  ${BOLD}3${RESET}  Update only         ${DIM}(download latest, don't run)${RESET}"
    echo -e "  ${BOLD}4${RESET}  First-time setup    ${DIM}(enter your credentials)${RESET}"
    echo -e "  ${BOLD}5${RESET}  Fix problems        ${DIM}(check everything is working)${RESET}"
    echo -e "  ${BOLD}6${RESET}  Open setup guide    ${DIM}(step-by-step instructions)${RESET}"
    echo ""
    echo -e "  ${BOLD}d${RESET}  Developer tools     ${DIM}(push, release, CI — advanced)${RESET}"
    echo -e "  ${BOLD}q${RESET}  Quit"
    echo ""
}

# ================================================================
#  MAIN LOOP
# ================================================================
_ensure_branch

while true; do
    show_menu
    echo -ne "  Select [1-6, d, q]: "
    read -r choice

    case "$choice" in
        1) do_run ; _pause ;;
        2) do_update_and_run ; _pause ;;
        3) do_update ; _pause ;;
        4) do_setup ; _pause ;;
        5) do_health_check ; _pause ;;
        6) do_open_guide ; _pause ;;
        d|D)
            if [ -f "$REPO_DIR/git_menu.sh" ]; then
                bash "$REPO_DIR/git_menu.sh"
            else
                echo -e "  ${RED}git_menu.sh not found.${RESET}"
                _pause
            fi
            ;;
        q|Q) echo -e "\n  ${DIM}Goodbye.${RESET}\n"; exit 0 ;;
        *)  echo -e "\n  ${RED}Invalid option.${RESET}" ; _pause ;;
    esac
done
