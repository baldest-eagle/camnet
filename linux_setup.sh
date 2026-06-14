#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# CamNet Linux Setup — installs dependencies and configures v4l2loopback
#
# Usage:
#   chmod +x linux_setup.sh
#   sudo ./linux_setup.sh
#
# What this does:
#   1. Installs system packages (ffmpeg, v4l2loopback-dkms, python3-pip)
#   2. Loads the v4l2loopback kernel module with CamNet label
#   3. Installs Python dependencies
#   4. Verifies everything is working
# ---------------------------------------------------------------------------

set -euo pipefail

# ----- Configuration -----
V4L2_VIDEO_NR=2                # /dev/video2 by default
CARD_LABEL="CamNet Virtual Camera"
EXCLUSIVE_CAPS=1               # Required for output-mode v4l2loopback
MAX_DEVICES=1                  # Number of virtual video devices

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ----- Preflight checks -----

if [[ $EUID -ne 0 ]]; then
    error "This script must be run as root (use sudo)."
fi

info "CamNet Linux Setup starting..."

# ----- Detect distribution -----

if [[ -f /etc/debian_version ]]; then
    PKG_MANAGER="apt"
    PKG_UPDATE="apt update"
    PKG_INSTALL="apt install -y"
    PKGS=(ffmpeg v4l2loopback-dkms python3-pip python3-venv)
elif [[ -f /etc/fedora-release ]]; then
    PKG_MANAGER="dnf"
    PKG_UPDATE="dnf check-update || true"
    PKG_INSTALL="dnf install -y"
    PKGS=(ffmpeg v4l2loopback python3-pip)
elif [[ -f /etc/arch-release ]]; then
    PKG_MANAGER="pacman"
    PKG_UPDATE="pacman -Sy --noconfirm"
    PKG_INSTALL="pacman -S --noconfirm"
    PKGS=(ffmpeg v4l2loopback-dkms python-pip)
else
    warn "Unknown distribution. Attempting apt-based install..."
    PKG_MANAGER="apt"
    PKG_UPDATE="apt update"
    PKG_INSTALL="apt install -y"
    PKGS=(ffmpeg v4l2loopback-dkms python3-pip python3-venv)
fi

# ----- Install system packages -----

info "Updating package lists..."
eval "$PKG_UPDATE" || true

info "Installing system packages: ${PKGS[*]}"
eval "$PKG_INSTALL" "${PKGS[@]}" || warn "Some packages may have failed to install."

# Verify ffmpeg
if command -v ffmpeg &>/dev/null; then
    info "FFmpeg found: $(ffmpeg -version 2>&1 | head -1)"
else
    warn "FFmpeg not found on PATH. Install it manually."
fi

# ----- v4l2loopback setup -----

info "Configuring v4l2loopback kernel module..."

# Unload if already loaded (ignore errors)
modprobe -r v4l2loopback 2>/dev/null || true

# Load with CamNet parameters
if modprobe v4l2loopback \
    video_nr="$V4L2_VIDEO_NR" \
    card_label="$CARD_LABEL" \
    exclusive_caps="$EXCLUSIVE_CAPS" \
    max_devices="$MAX_DEVICES"; then
    info "v4l2loopback loaded: /dev/video${V4L2_VIDEO_NR} (${CARD_LABEL})"
else
    error "Failed to load v4l2loopback. Is the kernel module installed?"
fi

# Verify the device exists
DEVICE="/dev/video${V4L2_VIDEO_NR}"
if [[ -c "$DEVICE" ]]; then
    info "V4L2 device confirmed: $DEVICE"
else
    error "V4L2 device $DEVICE not found. Check dmesg for errors."
fi

# Make it persistent across reboots
CONF_FILE="/etc/modules-load.d/camnet.conf"
PARAMS_FILE="/etc/modprobe.d/camnet.conf"

echo "v4l2loopback" > "$CONF_FILE"
cat > "$PARAMS_FILE" <<EOF
# CamNet Virtual Camera — v4l2loopback configuration
options v4l2loopback video_nr=${V4L2_VIDEO_NR} card_label="${CARD_LABEL}" exclusive_caps=${EXCLUSIVE_CAPS} max_devices=${MAX_DEVICES}
EOF

info "Persistent configuration written to $CONF_FILE and $PARAMS_FILE"

# ----- Python dependencies -----

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REQUIREMENTS="$SCRIPT_DIR/receiver/requirements_linux.txt"

if [[ -f "$REQUIREMENTS" ]]; then
    info "Installing Python dependencies from $REQUIREMENTS ..."
    pip3 install -r "$REQUIREMENTS" || warn "pip install had issues. You may need a venv."
else
    warn "requirements_linux.txt not found at $REQUIREMENTS"
fi

# ----- Verification -----

info "Running verification..."

# Check v4l2loopback
if lsmod | grep -q v4l2loopback; then
    info "v4l2loopback kernel module: LOADED"
else
    warn "v4l2loopback kernel module: NOT LOADED"
fi

# Check device
if [[ -c "$DEVICE" ]]; then
    info "V4L2 device $DEVICE: EXISTS"
else
    warn "V4L2 device $DEVICE: NOT FOUND"
fi

# Check Python packages
python3 -c "import zeroconf; import flask; import click; import loguru" 2>/dev/null && \
    info "Python core dependencies: OK" || \
    warn "Some Python dependencies are missing. Run: pip3 install -r receiver/requirements_linux.txt"

# Check ffmpeg
command -v ffmpeg &>/dev/null && info "FFmpeg: OK" || warn "FFmpeg: NOT FOUND"

echo ""
info "========================================="
info "  CamNet Linux Setup Complete!"
info "========================================="
echo ""
info "Next steps:"
info "  1. Start the receiver:"
info "     cd receiver && python3 main.py"
info ""
info "  2. Start the sender on another device:"
info "     cd sender && python3 main.py --device 0"
info ""
info "  3. In OBS Studio, add a Video Capture Device source"
info "     and select '${CARD_LABEL}' (/dev/video${V4L2_VIDEO_NR})"
echo ""
