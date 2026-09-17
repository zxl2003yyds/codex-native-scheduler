#!/bin/zsh
set -e
LABEL="com.codex.native-scheduler"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl bootout "gui/$UID" "$PLIST" >/dev/null 2>&1 || true
rm -f "$PLIST"
rm -rf "$HOME/.codex/plugins/codex-native-scheduler"
rm -rf "$HOME/Applications/Codex Scheduler.app"
python3 - <<'PY'
import json
from pathlib import Path
p=Path.home()/'.agents/plugins/marketplace.json'
if p.exists():
    try:
        d=json.loads(p.read_text()); d['plugins']=[x for x in d.get('plugins',[]) if x.get('name')!='codex-native-scheduler']; p.write_text(json.dumps(d,indent=2)+'\n')
    except Exception: pass
PY
printf '\nCodex Scheduler plugin, local app and LaunchAgent removed. Task history was kept in Library/Application Support/CodexNativeScheduler.\nPress Return to close… '
read _
