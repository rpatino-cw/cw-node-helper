#!/usr/bin/env bash
# CW Node Helper — one-command installer
# Usage: curl -sL https://raw.githubusercontent.com/rpatino-cw/cw-node-helper/main/install.sh | bash
set -e

REPO="https://github.com/rpatino-cw/cw-node-helper.git"
INSTALL_DIR="$HOME/cw-node-helper"

echo ""
echo "  CW Node Helper — Installer"
echo "  ─────────────────────────────"
echo ""

# Check Python 3.10+
if ! command -v python3 &>/dev/null; then
    echo "  ERROR: python3 not found. Install Python 3.10+ first."
    exit 1
fi

PY_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
    echo "  ERROR: Python 3.10+ required (found $PY_VERSION)"
    exit 1
fi
echo "  ✓ Python $PY_VERSION"

# Check pip
if ! python3 -m pip --version &>/dev/null; then
    echo "  ERROR: pip not found. Install pip first:"
    echo "    python3 -m ensurepip --upgrade"
    exit 1
fi
echo "  ✓ pip"

# Check git
if ! command -v git &>/dev/null; then
    echo "  ERROR: git not found. Install git first."
    exit 1
fi
echo "  ✓ git"

echo ""

# Clone or update
if [ -d "$INSTALL_DIR" ]; then
    echo "  Updating existing install..."
    cd "$INSTALL_DIR"
    git pull --ff-only 2>/dev/null || echo "  (could not auto-update — continuing with existing)"
else
    echo "  Cloning repository..."
    git clone -b rpatino/cw-node-helper "$REPO" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

# Install as editable package
echo "  Installing package..."
python3 -m pip install -e . --quiet 2>/dev/null

# Verify cwhelper is on PATH
if command -v cwhelper &>/dev/null; then
    echo "  ✓ cwhelper installed"
else
    echo "  ⚠ cwhelper not on PATH — you may need to add pip's bin dir to PATH:"
    echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

echo ""
echo "  ─────────────────────────────"
echo "  Install complete! Starting setup wizard..."
echo ""

# Run setup wizard
cd "$INSTALL_DIR"
cwhelper setup 2>/dev/null || python3 -m cwhelper setup 2>/dev/null || {
    echo ""
    echo "  Setup wizard could not start. Run manually:"
    echo "    cd $INSTALL_DIR && cwhelper setup"
}
