#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# Claude Usage Tracker — Upgrade
# ═══════════════════════════════════════════════════════════════════════════
# Migrates from cron to launchd so missed syncs (laptop asleep/off)
# automatically run when the Mac wakes up.
#
# Usage:  npm run upgrade
#         (or: bash upgrade.sh)
#
# Safe to re-run — idempotent.
# ═══════════════════════════════════════════════════════════════════════════

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TRACKER_SCRIPT="$SCRIPT_DIR/tracker.py"
CONFIG_FILE="$SCRIPT_DIR/config.json"

echo "╔══════════════════════════════════════╗"
echo "║   Claude Usage Tracker — Upgrade     ║"
echo "╚══════════════════════════════════════╝"
echo ""

# ─── Verify config exists ─────────────────────────────────────────────────
if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "ERROR: config.json not found. Run 'npm run setup' first."
    exit 1
fi

echo "✓ Found existing config.json"

# ─── Remove old cron job (if any) ─────────────────────────────────────────
CRON_MARKER="# claude-usage-tracker"
if crontab -l 2>/dev/null | grep -q "$CRON_MARKER"; then
    echo "Removing old cron job..."
    (crontab -l 2>/dev/null | grep -v "$CRON_MARKER") | crontab - 2>/dev/null || true
    echo "✓ Old cron job removed."
else
    echo "  (No old cron job found — skipping)"
fi

# ─── Install launchd agent ────────────────────────────────────────────────
PLIST_NAME="com.claude-usage-tracker.sync"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$PLIST_DIR/$PLIST_NAME.plist"

echo ""
echo "Installing launchd agent (runs daily at 11:55 PM)..."
echo "  If your Mac is asleep at that time, it will run on next wake."

mkdir -p "$PLIST_DIR"

cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$PLIST_NAME</string>

    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>$TRACKER_SCRIPT</string>
    </array>

    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>23</integer>
        <key>Minute</key>
        <integer>55</integer>
    </dict>

    <key>StandardOutPath</key>
    <string>$SCRIPT_DIR/tracker.log</string>
    <key>StandardErrorPath</key>
    <string>$SCRIPT_DIR/tracker.log</string>

    <key>WorkingDirectory</key>
    <string>$SCRIPT_DIR</string>
</dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)/$PLIST_NAME" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"

echo "✓ Launchd agent installed."

# ─── Done ──────────────────────────────────────────────────────────────────

echo ""
echo "══════════════════════════════════════════════════"
echo "  Upgrade complete!"
echo ""
echo "  Your config and Gist ID are unchanged."
echo "  Stats sync daily at 11:55 PM (or on wake if missed)."
echo "══════════════════════════════════════════════════"
