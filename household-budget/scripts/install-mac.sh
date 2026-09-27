#!/bin/bash
# Makes Household Budget start automatically when you log in to this Mac,
# and restart if it ever stops. Run once from the household-budget folder:
#   bash scripts/install-mac.sh
# To undo: launchctl unload ~/Library/LaunchAgents/nz.household-budget.plist && rm that file
set -euo pipefail
cd "$(dirname "$0")/.."
APP_DIR="$(pwd)"
NODE="$(command -v node || true)"
if [ -z "$NODE" ]; then echo "Node isn't installed -- see docs/HOME-SETUP.md step 2"; exit 1; fi
mkdir -p data "$HOME/Library/LaunchAgents"
PLIST="$HOME/Library/LaunchAgents/nz.household-budget.plist"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>nz.household-budget</string>
  <key>ProgramArguments</key>
  <array>
    <string>$NODE</string>
    <string>--disable-warning=ExperimentalWarning</string>
    <string>src/server.js</string>
  </array>
  <key>WorkingDirectory</key><string>$APP_DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$APP_DIR/data/server.log</string>
  <key>StandardErrorPath</key><string>$APP_DIR/data/server.log</string>
</dict>
</plist>
PL
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "Installed. Household Budget is running and will start on every login."
echo "Check it at http://localhost:3100 -- logs are in data/server.log"
