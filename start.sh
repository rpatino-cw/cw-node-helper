#!/bin/bash
# ================================================================
# start.sh — CW Node Helper Launcher
# One script to run, update, and manage everything.
# Designed for non-technical users. Just type: bash start.sh
# ================================================================

BRANCH="main"
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

# Graceful Ctrl+C handling — never leaves a broken state
trap 'echo -e "\n\n  ${DIM}Interrupted. Returning to menu...${RESET}"; sleep 0.3' INT

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
    if grep -q "your.name@example.com" "$REPO_DIR/.env" 2>/dev/null; then
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

# -- Env file helpers (read/write individual keys) ----------------

_env_get() {
    # Read a value from .env by key name. Returns empty string if not found.
    local key="$1"
    if [ -f "$REPO_DIR/.env" ]; then
        grep "^${key}=" "$REPO_DIR/.env" 2>/dev/null | head -1 | cut -d'=' -f2-
    fi
}

_env_set() {
    # Set a key=value in .env. Creates the line if missing, updates if present.
    local key="$1"
    local value="$2"
    if grep -q "^${key}=" "$REPO_DIR/.env" 2>/dev/null; then
        if [[ "$OSTYPE" == "darwin"* ]]; then
            sed -i '' "s|^${key}=.*|${key}=${value}|" "$REPO_DIR/.env"
        else
            sed -i "s|^${key}=.*|${key}=${value}|" "$REPO_DIR/.env"
        fi
    else
        echo "${key}=${value}" >> "$REPO_DIR/.env"
    fi
}

# -- API key validation -------------------------------------------

_has_curl() {
    command -v curl &>/dev/null
}

_validate_jira() {
    # Test Jira credentials by hitting the myself endpoint.
    # Returns 0 on success, 1 on failure, 2 on network/curl issue (skip validation).
    local email="$1"
    local token="$2"
    if ! _has_curl; then
        return 2  # Can't validate, assume OK
    fi
    local http_code
    http_code=$(curl -s -o /dev/null -w "%{http_code}" \
        -u "${email}:${token}" \
        -H "Accept: application/json" \
        "https://your-org.atlassian.net/rest/api/3/myself" \
        --connect-timeout 5 --max-time 10 2>/dev/null)
    if [ "$http_code" = "200" ]; then
        return 0
    fi
    if [ "$http_code" = "000" ]; then
        return 2  # Network error — can't reach server
    fi
    return 1
}

_validate_netbox() {
    # Test NetBox token by hitting the status endpoint.
    local url="$1"
    local token="$2"
    if ! _has_curl; then
        return 2
    fi
    local http_code
    http_code=$(curl -s -o /dev/null -w "%{http_code}" \
        -H "Authorization: Token ${token}" \
        -H "Accept: application/json" \
        "${url}/status/" \
        --connect-timeout 5 --max-time 10 2>/dev/null)
    if [ "$http_code" = "200" ]; then
        return 0
    fi
    if [ "$http_code" = "000" ]; then
        return 2
    fi
    return 1
}

_validate_openai() {
    # Test OpenAI key by listing models (lightweight call).
    local key="$1"
    if ! _has_curl; then
        return 2
    fi
    local http_code
    http_code=$(curl -s -o /dev/null -w "%{http_code}" \
        -H "Authorization: Bearer ${key}" \
        "https://api.openai.com/v1/models" \
        --connect-timeout 5 --max-time 10 2>/dev/null)
    if [ "$http_code" = "200" ]; then
        return 0
    fi
    if [ "$http_code" = "000" ]; then
        return 2
    fi
    return 1
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

    # Check for required Jira credentials
    local jira_email jira_token
    jira_email=$(_env_get "JIRA_EMAIL")
    jira_token=$(_env_get "JIRA_API_TOKEN")

    if [ -z "$jira_email" ] || [ "$jira_email" = "your.name@example.com" ] || \
       [ -z "$jira_token" ] || [ "$jira_token" = "paste_your_jira_token_here" ]; then
        echo -e "  ${RED}${BOLD}Jira credentials not configured.${RESET}"
        echo -e "  ${DIM}The app cannot work without Jira. Run option 4 (First-time setup).${RESET}"
        return
    fi

    # Validate Jira credentials
    echo -ne "  ${DIM}Verifying Jira credentials...${RESET}"
    _validate_jira "$jira_email" "$jira_token"
    local jira_rc=$?
    if [ "$jira_rc" -eq 0 ]; then
        echo -e "\r  ${GREEN}Jira ✓${RESET}                          "
    elif [ "$jira_rc" -eq 2 ]; then
        echo -e "\r  ${DIM}Jira — could not verify (no network), continuing anyway${RESET}"
    else
        echo -e "\r  ${RED}${BOLD}Jira credentials are invalid.${RESET}                 "
        echo -e "  ${YELLOW}Either your email or API token is wrong.${RESET}"
        echo -e "  ${DIM}Get a new token: https://id.atlassian.com/manage-profile/security/api-tokens${RESET}"
        echo ""
        echo -ne "  ${BOLD}Run setup to fix? [Y/n]:${RESET} "
        read -r fix_choice
        if [[ "$fix_choice" != "n" && "$fix_choice" != "N" ]]; then
            do_setup
        fi
        return
    fi

    # Validate NetBox (optional — warn if configured but broken)
    local netbox_token netbox_url
    netbox_token=$(_env_get "NETBOX_API_TOKEN")
    netbox_url=$(_env_get "NETBOX_API_URL")
    if [ -n "$netbox_token" ] && [ "$netbox_token" != "paste_your_netbox_token_here" ]; then
        echo -ne "  ${DIM}Verifying NetBox...${RESET}"
        _validate_netbox "$netbox_url" "$netbox_token"
        local nb_rc=$?
        if [ "$nb_rc" -eq 0 ]; then
            echo -e "\r  ${GREEN}NetBox ✓${RESET}                              "
        elif [ "$nb_rc" -eq 2 ]; then
            echo -e "\r  ${DIM}NetBox — could not verify (no network)${RESET}"
        else
            echo -e "\r  ${YELLOW}NetBox ✗ — token may be expired${RESET}       "
            echo -e "  ${DIM}The app will still work, but without rack/device details.${RESET}"
        fi
    fi

    # Validate OpenAI (optional — just inform)
    local openai_key
    openai_key=$(_env_get "OPENAI_API_KEY")
    if [ -n "$openai_key" ] && [ "$openai_key" != "paste_your_openai_api_key_here" ]; then
        echo -ne "  ${DIM}Verifying OpenAI...${RESET}"
        _validate_openai "$openai_key"
        local ai_rc=$?
        if [ "$ai_rc" -eq 0 ]; then
            echo -e "\r  ${GREEN}OpenAI ✓${RESET} ${DIM}(AI features enabled)${RESET}       "
        elif [ "$ai_rc" -eq 2 ]; then
            echo -e "\r  ${DIM}OpenAI — could not verify (no network)${RESET}"
        else
            echo -e "\r  ${YELLOW}OpenAI ✗ — key may be invalid${RESET}       "
            echo -e "  ${DIM}AI features will be disabled. Everything else works fine.${RESET}"
        fi
    fi

    if ! _check_python; then
        return
    fi

    _check_requests || return

    # Load env and run
    echo ""
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
        cat > "$REPO_DIR/.env" << 'ENVEOF'
JIRA_EMAIL=your.name@example.com
JIRA_API_TOKEN=paste_your_jira_token_here
NETBOX_API_URL=https://netbox.example.com/api
NETBOX_API_TOKEN=paste_your_netbox_token_here
OPENAI_API_KEY=paste_your_openai_api_key_here
ENVEOF
    fi

    echo -e "  ${CYAN}Let's set up your credentials.${RESET}"
    echo -e "  ${DIM}These are stored locally on your machine and never shared.${RESET}"
    echo ""

    # ==============================================================
    #  STEP 1: Jira Email (REQUIRED)
    # ==============================================================
    echo -e "  ${BOLD}${WHITE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
    echo -e "  ${BOLD}Step 1 of 4: Your work email${RESET} ${RED}(required)${RESET}"
    echo -e "  ${BOLD}${WHITE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
    echo -e "  ${DIM}(example: john.doe@example.com)${RESET}"
    echo ""
    echo -ne "  Email: "
    read -r user_email

    if [ -z "$user_email" ]; then
        echo -e "  ${RED}No email entered. Setup cancelled.${RESET}"
        echo -e "  ${RED}${BOLD}Without Jira email, the app cannot run at all.${RESET}"
        return
    fi
    _env_set "JIRA_EMAIL" "$user_email"
    echo -e "  ${GREEN}Saved ✓${RESET}"

    # ==============================================================
    #  STEP 2: Jira API Token (REQUIRED)
    # ==============================================================
    echo ""
    echo -e "  ${BOLD}${WHITE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
    echo -e "  ${BOLD}Step 2 of 4: Your Jira API token${RESET} ${RED}(required)${RESET}"
    echo -e "  ${BOLD}${WHITE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
    echo -e "  ${DIM}Get one here:${RESET}"
    echo -e "  ${CYAN}https://id.atlassian.com/manage-profile/security/api-tokens${RESET}"
    echo -e "  ${DIM}Click 'Create API token', name it 'cw-node-helper', copy the token.${RESET}"
    echo ""
    echo -ne "  Jira token: "
    read -r user_jira_token

    if [ -z "$user_jira_token" ]; then
        echo -e "  ${RED}No token entered. Setup cancelled.${RESET}"
        echo -e "  ${RED}${BOLD}Without Jira token, the app cannot run at all.${RESET}"
        return
    fi
    _env_set "JIRA_API_TOKEN" "$user_jira_token"

    # Validate Jira right now
    echo -e "  ${DIM}Checking your Jira credentials...${RESET}"
    _validate_jira "$user_email" "$user_jira_token"
    local jira_setup_rc=$?
    if [ "$jira_setup_rc" -eq 0 ]; then
        echo -e "  ${GREEN}${BOLD}Jira credentials verified ✓${RESET}"
    elif [ "$jira_setup_rc" -eq 2 ]; then
        echo -e "  ${DIM}Could not verify (no network or curl) — saved anyway.${RESET}"
        echo -e "  ${DIM}Your credentials will be checked when you run the app.${RESET}"
    else
        echo -e "  ${RED}${BOLD}Jira credentials are INVALID.${RESET}"
        echo -e "  ${YELLOW}The email or token you entered didn't work.${RESET}"
        echo -e "  ${DIM}Common fixes:${RESET}"
        echo -e "  ${DIM}  • Make sure you used your @example.com email${RESET}"
        echo -e "  ${DIM}  • Generate a fresh token at the link above${RESET}"
        echo -e "  ${DIM}  • Copy the full token — no extra spaces${RESET}"
        echo ""
        echo -ne "  ${BOLD}Try again? [Y/n]:${RESET} "
        read -r retry
        if [[ "$retry" != "n" && "$retry" != "N" ]]; then
            echo -ne "  Jira token: "
            read -r user_jira_token
            if [ -n "$user_jira_token" ]; then
                _env_set "JIRA_API_TOKEN" "$user_jira_token"
                _validate_jira "$user_email" "$user_jira_token"
                local retry_rc=$?
                if [ "$retry_rc" -eq 0 ]; then
                    echo -e "  ${GREEN}${BOLD}Jira credentials verified ✓${RESET}"
                elif [ "$retry_rc" -eq 2 ]; then
                    echo -e "  ${DIM}Saved, but could not verify (no network).${RESET}"
                else
                    echo -e "  ${RED}Still invalid. You can re-run setup later to fix this.${RESET}"
                fi
            fi
        fi
    fi

    # ==============================================================
    #  STEP 3: NetBox Token (OPTIONAL)
    # ==============================================================
    echo ""
    echo -e "  ${BOLD}${WHITE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
    echo -e "  ${BOLD}Step 3 of 4: NetBox token${RESET} ${YELLOW}(optional)${RESET}"
    echo -e "  ${BOLD}${WHITE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
    echo ""
    echo -e "  ${DIM}What NetBox gives you:${RESET}"
    echo -e "    ${GREEN}•${RESET} Device details (model, IPs, interfaces)"
    echo -e "    ${GREEN}•${RESET} Rack position and neighbor info"
    echo -e "    ${GREEN}•${RESET} Cable connections and network topology"
    echo -e "    ${GREEN}•${RESET} Remote console (BMC) links"
    echo ""
    echo -e "  ${YELLOW}${BOLD}Without NetBox:${RESET}"
    echo -e "    ${DIM}The app still works, but you only see Jira data.${RESET}"
    echo -e "    ${DIM}No rack maps, no connections, no device details.${RESET}"
    echo ""
    echo -ne "  NetBox token (or ${BOLD}Enter${RESET} to skip): "
    read -r user_netbox_token

    if [ -n "$user_netbox_token" ]; then
        _env_set "NETBOX_API_TOKEN" "$user_netbox_token"
        echo -e "  ${DIM}Checking your NetBox token...${RESET}"
        local netbox_url
        netbox_url=$(_env_get "NETBOX_API_URL")
        if [ -z "$netbox_url" ]; then
            netbox_url="https://netbox.example.com/api"
            _env_set "NETBOX_API_URL" "$netbox_url"
        fi
        _validate_netbox "$netbox_url" "$user_netbox_token"
        local nb_setup_rc=$?
        if [ "$nb_setup_rc" -eq 0 ]; then
            echo -e "  ${GREEN}${BOLD}NetBox token verified ✓${RESET}"
        elif [ "$nb_setup_rc" -eq 2 ]; then
            echo -e "  ${DIM}Saved, but could not verify (no network).${RESET}"
        else
            echo -e "  ${YELLOW}NetBox token didn't work.${RESET}"
            echo -e "  ${DIM}The token might be expired or wrong.${RESET}"
            echo -e "  ${DIM}The app will still work without it — just no device data.${RESET}"
        fi
    else
        echo -e "  ${DIM}Skipped — no rack maps or device details.${RESET}"
        echo -e "  ${DIM}You can add it later by re-running setup.${RESET}"
    fi

    # ==============================================================
    #  STEP 4: OpenAI API Key (OPTIONAL)
    # ==============================================================
    echo ""
    echo -e "  ${BOLD}${WHITE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
    echo -e "  ${BOLD}Step 4 of 4: OpenAI API key${RESET} ${YELLOW}(optional)${RESET}"
    echo -e "  ${BOLD}${WHITE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
    echo ""
    echo -e "  ${DIM}What AI gives you:${RESET}"
    echo -e "    ${GREEN}•${RESET} Summarize any ticket in plain English"
    echo -e "    ${GREEN}•${RESET} Ask questions about tickets and nodes"
    echo -e "    ${GREEN}•${RESET} Find tickets by describing what you remember"
    echo -e "    ${GREEN}•${RESET} Available from any screen — just type 'ai'"
    echo ""
    echo -e "  ${YELLOW}${BOLD}Without OpenAI:${RESET}"
    echo -e "    ${DIM}Everything else works perfectly — no AI features, that's it.${RESET}"
    echo -e "    ${DIM}You can always add this later.${RESET}"
    echo ""
    echo -e "  ${DIM}Also requires:${RESET} ${WHITE}pip3 install openai${RESET}"
    echo ""
    echo -ne "  OpenAI key (or ${BOLD}Enter${RESET} to skip): "
    read -r user_openai_key

    if [ -n "$user_openai_key" ]; then
        _env_set "OPENAI_API_KEY" "$user_openai_key"
        echo -e "  ${DIM}Checking your OpenAI key...${RESET}"
        _validate_openai "$user_openai_key"
        local ai_setup_rc=$?
        if [ "$ai_setup_rc" -eq 0 ]; then
            echo -e "  ${GREEN}${BOLD}OpenAI key verified ✓${RESET}"
        elif [ "$ai_setup_rc" -eq 2 ]; then
            echo -e "  ${DIM}Saved, but could not verify (no network).${RESET}"
        else
            echo -e "  ${YELLOW}OpenAI key didn't work.${RESET}"
            echo -e "  ${DIM}Check your key at: https://platform.openai.com/api-keys${RESET}"
            echo -e "  ${DIM}AI features will be disabled. Everything else works fine.${RESET}"
        fi
        # Auto-install openai package regardless of validation
        if command -v python3 &>/dev/null && ! python3 -c "import openai" 2>/dev/null; then
            echo -e "  ${DIM}Installing openai package...${RESET}"
            pip3 install openai --quiet 2>/dev/null
            if python3 -c "import openai" 2>/dev/null; then
                echo -e "  ${GREEN}openai package installed ✓${RESET}"
            else
                echo -e "  ${YELLOW}Could not auto-install. Run manually: pip3 install openai${RESET}"
            fi
        fi
    else
        echo -e "  ${DIM}Skipped — no AI features.${RESET}"
        echo -e "  ${DIM}You can add it later by re-running setup.${RESET}"
    fi

    # Install requests if needed
    echo ""
    _check_python && _check_requests

    # ==============================================================
    #  SUMMARY
    # ==============================================================
    local jira_email_final jira_token_final netbox_token_final openai_key_final
    jira_email_final=$(_env_get "JIRA_EMAIL")
    jira_token_final=$(_env_get "JIRA_API_TOKEN")
    netbox_token_final=$(_env_get "NETBOX_API_TOKEN")
    openai_key_final=$(_env_get "OPENAI_API_KEY")

    echo ""
    echo -e "  ${GREEN}${BOLD}┌─ SETUP COMPLETE ──────────────────────────────┐${RESET}"
    echo -e "  ${GREEN}${BOLD}│${RESET}                                                ${GREEN}${BOLD}│${RESET}"

    # Jira
    echo -e "  ${GREEN}${BOLD}│${RESET}  ${WHITE}Email:${RESET}   ${jira_email_final}"
    if [ -n "$jira_token_final" ] && [ "$jira_token_final" != "paste_your_jira_token_here" ]; then
        echo -e "  ${GREEN}${BOLD}│${RESET}  ${WHITE}Jira:${RESET}    ${GREEN}$(_mask_token "$jira_token_final")${RESET}"
    else
        echo -e "  ${GREEN}${BOLD}│${RESET}  ${WHITE}Jira:${RESET}    ${RED}not configured${RESET}"
    fi

    # NetBox
    if [ -n "$netbox_token_final" ] && [ "$netbox_token_final" != "paste_your_netbox_token_here" ]; then
        echo -e "  ${GREEN}${BOLD}│${RESET}  ${WHITE}NetBox:${RESET}  ${GREEN}$(_mask_token "$netbox_token_final")${RESET}"
    else
        echo -e "  ${GREEN}${BOLD}│${RESET}  ${WHITE}NetBox:${RESET}  ${DIM}not configured (no rack/device data)${RESET}"
    fi

    # OpenAI
    if [ -n "$openai_key_final" ] && [ "$openai_key_final" != "paste_your_openai_api_key_here" ]; then
        echo -e "  ${GREEN}${BOLD}│${RESET}  ${WHITE}OpenAI:${RESET}  ${GREEN}$(_mask_token "$openai_key_final")${RESET}"
    else
        echo -e "  ${GREEN}${BOLD}│${RESET}  ${WHITE}OpenAI:${RESET}  ${DIM}not configured (no AI features)${RESET}"
    fi

    echo -e "  ${GREEN}${BOLD}│${RESET}                                                ${GREEN}${BOLD}│${RESET}"
    echo -e "  ${GREEN}${BOLD}│${RESET}  ${DIM}Use option 1 to run the app!${RESET}                  ${GREEN}${BOLD}│${RESET}"
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

    # openai module
    echo -ne "  openai library ..... "
    if python3 -c "import openai" 2>/dev/null; then
        echo -e "${GREEN}✓${RESET} ${DIM}(AI features available)${RESET}"
    else
        echo -e "${DIM}— not installed (optional)${RESET}"
        echo -e "    ${DIM}Install for AI features: ${WHITE}pip3 install openai${RESET}"
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

    # Jira validation
    if _check_env; then
        local j_email j_token
        j_email=$(_env_get "JIRA_EMAIL")
        j_token=$(_env_get "JIRA_API_TOKEN")
        if [ -n "$j_token" ] && [ "$j_token" != "paste_your_jira_token_here" ]; then
            echo -ne "  Jira connection .... "
            _validate_jira "$j_email" "$j_token"
            local hc_jira=$?
            if [ "$hc_jira" -eq 0 ]; then
                echo -e "${GREEN}✓${RESET}"
            elif [ "$hc_jira" -eq 2 ]; then
                echo -e "${DIM}— could not verify (no network)${RESET}"
            else
                echo -e "${RED}✗ INVALID CREDENTIALS${RESET}"
                echo -e "    ${DIM}Your Jira email or token is wrong. Re-run setup (option 4).${RESET}"
                issues=$((issues + 1))
            fi
        fi

        # NetBox validation
        local nb_url nb_token
        nb_url=$(_env_get "NETBOX_API_URL")
        nb_token=$(_env_get "NETBOX_API_TOKEN")
        if [ -n "$nb_token" ] && [ "$nb_token" != "paste_your_netbox_token_here" ]; then
            echo -ne "  NetBox connection .. "
            _validate_netbox "$nb_url" "$nb_token"
            local hc_nb=$?
            if [ "$hc_nb" -eq 0 ]; then
                echo -e "${GREEN}✓${RESET}"
            elif [ "$hc_nb" -eq 2 ]; then
                echo -e "${DIM}— could not verify (no network)${RESET}"
            else
                echo -e "${YELLOW}✗ INVALID TOKEN${RESET}"
                echo -e "    ${DIM}NetBox token may be expired. Re-run setup or edit .env${RESET}"
                echo -e "    ${DIM}Impact: No rack maps, device details, or connections${RESET}"
                issues=$((issues + 1))
            fi
        else
            echo -e "  NetBox connection .. ${DIM}— not configured (optional)${RESET}"
        fi

        # OpenAI validation
        local ai_key
        ai_key=$(_env_get "OPENAI_API_KEY")
        if [ -n "$ai_key" ] && [ "$ai_key" != "paste_your_openai_api_key_here" ]; then
            echo -ne "  OpenAI connection .. "
            _validate_openai "$ai_key"
            local hc_ai=$?
            if [ "$hc_ai" -eq 0 ]; then
                echo -e "${GREEN}✓${RESET}"
            elif [ "$hc_ai" -eq 2 ]; then
                echo -e "${DIM}— could not verify (no network)${RESET}"
            else
                echo -e "${YELLOW}✗ INVALID KEY${RESET}"
                echo -e "    ${DIM}Check your key at: https://platform.openai.com/api-keys${RESET}"
                echo -e "    ${DIM}Impact: AI features disabled (everything else still works)${RESET}"
                issues=$((issues + 1))
            fi
        else
            echo -e "  OpenAI connection .. ${DIM}— not configured (optional)${RESET}"
        fi
    fi

    # Git repo
    echo -ne "  Git repository ..... "
    if [ -d "$REPO_DIR/.git" ]; then
        echo -e "${GREEN}✓${RESET}"
    else
        echo -e "${RED}✗ NOT A GIT REPO${RESET}"
        echo -e "    ${DIM}Something is wrong with the installation.${RESET}"
        echo -e "    ${DIM}Re-clone: git clone https://github.com/your-org/node-helper.git${RESET}"
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
