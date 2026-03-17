#!/usr/bin/env bash
# =============================================================================
# Broadcast Hub — one-time setup
# Run once as root/sudo after installing Broadcast Hub:
#   sudo ./setup.sh
# =============================================================================
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

ok()   { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}⚠${NC}  $*"; }
err()  { echo -e "${RED}✗${NC} $*"; }
hdr()  { echo -e "\n${BOLD}${CYAN}$*${NC}"; }

# Must be run as root
if [[ $EUID -ne 0 ]]; then
    err "Please run as root:  sudo ./setup.sh"
    exit 1
fi

# Identify the real user who invoked sudo (we write files as them)
REAL_USER="${SUDO_USER:-$USER}"
REAL_HOME=$(getent passwd "$REAL_USER" | cut -d: -f6)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/input_config.json"

echo ""
echo -e "${BOLD}╔══════════════════════════════════════╗${NC}"
echo -e "${BOLD}║     Broadcast Hub  —  Setup          ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════╝${NC}"
echo ""

# ---------------------------------------------------------------------------
# 1. Magewell installer path
# ---------------------------------------------------------------------------
hdr "Step 1 — Magewell driver installer"

DEFAULT_MW=""
# Try to find an existing installer on the system
for d in \
    "$REAL_HOME/src/Magewell"/ProCaptureForLinux_* \
    /opt/magewell/ProCaptureForLinux_* \
    /usr/local/src/Magewell/ProCaptureForLinux_* ; do
    if [[ -f "$d/install.sh" ]]; then
        DEFAULT_MW="$d"
        break
    fi
done

if [[ -n "$DEFAULT_MW" ]]; then
    echo "Found installer at: $DEFAULT_MW"
fi

echo ""
echo "Enter the full path to your Magewell ProCapture driver directory."
echo "(This is the folder that contains install.sh)"
if [[ -n "$DEFAULT_MW" ]]; then
    read -rp "Path [$DEFAULT_MW]: " MW_PATH
    MW_PATH="${MW_PATH:-$DEFAULT_MW}"
else
    read -rp "Path: " MW_PATH
fi

MW_PATH="${MW_PATH%/}"   # strip trailing slash

if [[ -z "$MW_PATH" ]]; then
    warn "No Magewell path provided — skipping driver auto-reinstall setup."
    MW_PATH=""
elif [[ ! -f "$MW_PATH/install.sh" ]]; then
    err "install.sh not found at: $MW_PATH"
    err "Auto-reinstall will not work. You can set the path later in Broadcast Hub."
    MW_PATH=""
else
    ok "Magewell installer found: $MW_PATH/install.sh"
fi

# ---------------------------------------------------------------------------
# 2. Write sudoers rule
# ---------------------------------------------------------------------------
hdr "Step 2 — Passwordless sudo for driver reinstall"

SUDOERS_FILE="/etc/sudoers.d/broadcast-hub-magewell"

if [[ -n "$MW_PATH" ]]; then
    INSTALL_SCRIPT="$MW_PATH/install.sh"

    # Validate the rule before writing
    MODPROBE=$(which modprobe)
    RULE1="$REAL_USER ALL=(root) NOPASSWD: $INSTALL_SCRIPT"
    RULE2="$REAL_USER ALL=(root) NOPASSWD: $MODPROBE ProCapture"
    if echo -e "$RULE1\n$RULE2" | visudo -cf - 2>/dev/null; then
        echo -e "$RULE1\n$RULE2" > "$SUDOERS_FILE"
        chmod 0440 "$SUDOERS_FILE"
        ok "Sudoers rules written: $SUDOERS_FILE"
        ok "  → $REAL_USER can run install.sh and modprobe without a password"
    else
        err "visudo validation failed — sudoers rule NOT written."
        warn "You can add it manually with: sudo visudo -f $SUDOERS_FILE"
        warn "  $RULE"
    fi
else
    warn "Skipping sudoers setup (no installer path configured)."
fi

# ---------------------------------------------------------------------------
# 3. Save installer path to input_config.json
# ---------------------------------------------------------------------------
hdr "Step 3 — Saving config"

if [[ -n "$MW_PATH" ]]; then
    if [[ -f "$CONFIG_FILE" ]]; then
        # Merge into existing config using python (already a dependency)
        python3 - <<PYEOF
import json, sys

cfg_path = "$CONFIG_FILE"
try:
    with open(cfg_path) as f:
        cfg = json.load(f)
except Exception:
    cfg = {}

cfg.setdefault("__user_prefs__", {})["magewell_installer"] = "$MW_PATH"

with open(cfg_path, "w") as f:
    json.dump(cfg, f, indent=2)

print("Config updated.")
PYEOF
    else
        # Create a minimal config
        python3 -c "
import json
cfg = {'__user_prefs__': {'magewell_installer': '$MW_PATH'}}
with open('$CONFIG_FILE', 'w') as f:
    json.dump(cfg, f, indent=2)
print('Config created.')
"
    fi
    # Fix ownership so the app user can read/write it
    chown "$REAL_USER":"$REAL_USER" "$CONFIG_FILE"
    ok "Magewell installer path saved to config"
fi

# ---------------------------------------------------------------------------
# 4. Systemd service (optional)
# ---------------------------------------------------------------------------
hdr "Step 4 — Systemd service (optional)"

read -rp "Install Broadcast Hub as a systemd service? [y/N]: " INSTALL_SERVICE
INSTALL_SERVICE="${INSTALL_SERVICE:-N}"

if [[ "$INSTALL_SERVICE" =~ ^[Yy]$ ]]; then
    SERVICE_FILE="/etc/systemd/system/broadcast-hub.service"
    cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Broadcast Hub
After=network.target

[Service]
Type=simple
User=$REAL_USER
WorkingDirectory=$SCRIPT_DIR
ExecStart=$(which python3) $SCRIPT_DIR/m2tsweb_fastapi.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable broadcast-hub
    ok "Service installed: broadcast-hub.service"
    ok "Start with:  sudo systemctl start broadcast-hub"
else
    echo "Skipping service install."
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
hdr "Setup complete"
echo ""
if [[ -n "$MW_PATH" ]]; then
    ok "Driver auto-reinstall configured for: $MW_PATH"
fi
echo ""
echo -e "Start Broadcast Hub:  ${CYAN}python3 $SCRIPT_DIR/m2tsweb_fastapi.py${NC}"
echo ""
