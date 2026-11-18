#!/bin/sh
# Installiert (oder entfernt mit --uninstall) einen launchd-Dienst, der den
# Obsidian-Export startet, sobald sich LearnHarness-Daten ändern.
#
#   tools/obsidian-export/install-launchd.sh ~/pfad/zum/vault
#   tools/obsidian-export/install-launchd.sh --uninstall
set -eu

LABEL="local.learnharness.obsidian-export"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/learnharness-obsidian-export.log"
DATA="${LH_DATA_DIR:-$HOME/Library/Application Support/LearnHarness/data}"
SCRIPT="$(cd "$(dirname "$0")" && pwd)/export.py"

if [ "${1:-}" = "--uninstall" ]; then
	launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
	rm -f "$PLIST"
	echo "Dienst entfernt."
	exit 0
fi

VAULT="${1:?Pfad zum Vault angeben}"
VAULT="$(cd "$VAULT" && pwd)"
PYTHON="$(command -v python3)"
[ -d "$VAULT/.obsidian" ] || { echo "Kein Obsidian-Vault: $VAULT" >&2; exit 2; }
[ -d "$DATA" ] || { echo "LearnHarness-Daten nicht gefunden: $DATA" >&2; exit 2; }

xml() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }

mkdir -p "$(dirname "$PLIST")" "$(dirname "$LOG")"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key><string>$LABEL</string>
	<key>ProgramArguments</key>
	<array>
		<string>$(xml "$PYTHON")</string>
		<string>-B</string>
		<string>$(xml "$SCRIPT")</string>
		<string>--vault</string>
		<string>$(xml "$VAULT")</string>
		<string>--data-dir</string>
		<string>$(xml "$DATA")</string>
	</array>
	<key>WatchPaths</key>
	<array>
		<string>$(xml "$DATA/graph")</string>
		<string>$(xml "$DATA/chats")</string>
	</array>
	<key>RunAtLoad</key><true/>
	<key>ThrottleInterval</key><integer>60</integer>
	<key>StandardOutPath</key><string>$(xml "$LOG")</string>
	<key>StandardErrorPath</key><string>$(xml "$LOG")</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Dienst $LABEL aktiv. Log: $LOG"
