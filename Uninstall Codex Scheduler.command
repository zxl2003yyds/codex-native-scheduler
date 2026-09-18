#!/bin/zsh
set -e
LABEL="com.codex.native-scheduler"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl bootout "gui/$UID" "$PLIST" >/dev/null 2>&1 || true
rm -f "$PLIST"
rm -rf "$HOME/.codex/plugins/codex-native-scheduler"
rm -rf "$HOME/Applications/Codex Scheduler.app"
python3 - <<'PY2'
import json
from pathlib import Path
p=Path.home()/'.agents/plugins/marketplace.json'
if p.exists():
    try:
        d=json.loads(p.read_text()); d['plugins']=[x for x in d.get('plugins',[]) if x.get('name')!='codex-native-scheduler']; p.write_text(json.dumps(d,indent=2)+'\n')
    except Exception: pass
PY2
PRIMARY_LANG="$(defaults read -g AppleLanguages 2>/dev/null | sed -n '2s/[", ]//gp' || true)"
if [[ "$PRIMARY_LANG" == zh* ]]; then
  printf '\nCodex Scheduler 插件、本地应用和 LaunchAgent 已移除。任务历史仍保留在 Library/Application Support/CodexNativeScheduler。\n按 Return 键关闭… '
else
  printf '\nCodex Scheduler plugin, local app and LaunchAgent removed. Task history was kept in Library/Application Support/CodexNativeScheduler.\nPress Return to close… '
fi
read _
