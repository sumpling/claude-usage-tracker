#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# Claude Usage Tracker — One-Time Setup
# ═══════════════════════════════════════════════════════════════════════════
# This script:
#   1. Prompts for your display name and GitHub token
#   2. Auto-creates a secret Gist to store your stats
#   3. Saves config locally (config.json — never uploaded)
#   4. Does an initial stats push
#   5. Installs a daily cron job so it runs automatically
#
# Prerequisites:
#   - Python 3.8+
#   - A GitHub Personal Access Token with "gist" scope
#     Create one at: https://github.com/settings/tokens/new?scopes=gist
#
# After setup, share your Gist ID with friends so they can add it to
# their dashboard. That's it — everything else is automatic.
# ═══════════════════════════════════════════════════════════════════════════

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/config.json"
TRACKER_SCRIPT="$SCRIPT_DIR/tracker.py"

echo "╔══════════════════════════════════════╗"
echo "║   Claude Usage Tracker — Setup       ║"
echo "╚══════════════════════════════════════╝"
echo ""

# ─── Gather info ────────────────────────────────────────────────────────────

read -rp "Your display name: " USERNAME
echo ""
echo "You need a GitHub Personal Access Token with 'gist' scope."
echo "Create one here: https://github.com/settings/tokens/new?scopes=gist"
echo ""
read -rp "GitHub Personal Access Token: " GITHUB_TOKEN

if [[ -z "$USERNAME" || -z "$GITHUB_TOKEN" ]]; then
    echo "ERROR: Both fields are required."
    exit 1
fi

# ─── Create a secret Gist automatically ────────────────────────────────────
# The Gist holds one file: <username>.json with your stats.
# Secret = unlisted (only people with the ID/link can see it).

echo ""
echo "Creating your secret Gist..."

GIST_RESPONSE=$(curl -s -X POST \
    -H "Authorization: Bearer $GITHUB_TOKEN" \
    -H "Accept: application/vnd.github+json" \
    -d "{
        \"description\": \"Claude Usage Tracker — $USERNAME\",
        \"public\": false,
        \"files\": {
            \"$USERNAME.json\": {
                \"content\": \"{}\"
            }
        }
    }" \
    "https://api.github.com/gists")

# Extract Gist ID from response
GIST_ID=$(echo "$GIST_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))" 2>/dev/null)

if [[ -z "$GIST_ID" ]]; then
    echo "ERROR: Failed to create Gist. Check your token has 'gist' scope."
    echo "Response: $GIST_RESPONSE"
    exit 1
fi

echo "✓ Gist created: https://gist.github.com/$GIST_ID"

# ─── Write config ───────────────────────────────────────────────────────────

cat > "$CONFIG_FILE" <<EOF
{
    "username": "$USERNAME",
    "github_token": "$GITHUB_TOKEN",
    "gist_id": "$GIST_ID"
}
EOF

echo "✓ Config saved to $CONFIG_FILE"

# ─── Initial run ───────────────────────────────────────────────────────────

echo ""
echo "Running tracker for the first time..."
python3 "$TRACKER_SCRIPT"

# ─── Install cron job ──────────────────────────────────────────────────────
# Runs daily at 11:55 PM local time.
# The cron entry is idempotent — re-running setup won't duplicate it.

CRON_CMD="55 23 * * * /usr/bin/python3 $TRACKER_SCRIPT >> $SCRIPT_DIR/tracker.log 2>&1"
CRON_MARKER="# claude-usage-tracker"

echo ""
echo "Installing daily cron job (runs at 11:55 PM)..."

(crontab -l 2>/dev/null | grep -v "$CRON_MARKER" || true; echo "$CRON_CMD $CRON_MARKER") | crontab -

echo "✓ Cron job installed."

# ─── Done ──────────────────────────────────────────────────────────────────

echo ""
echo "══════════════════════════════════════════════════"
echo "  Setup complete!"
echo ""
echo "  Your Gist ID:  $GIST_ID"
echo ""
echo "  ➤ Send this Gist ID to your friends"
echo "  ➤ Collect their Gist IDs"
echo "  ➤ Open dashboard.html and paste all IDs"
echo ""
echo "  Stats auto-sync daily at 11:55 PM."
echo "  Run manually anytime: python3 $TRACKER_SCRIPT"
echo "══════════════════════════════════════════════════"
