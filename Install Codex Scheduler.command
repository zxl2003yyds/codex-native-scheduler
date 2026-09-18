#!/bin/zsh
set -e
cd "${0:A:h}"
clear
PRIMARY_LANG="$(defaults read -g AppleLanguages 2>/dev/null | sed -n '2s/[", ]//gp' || true)"
if [[ "$PRIMARY_LANG" == zh* ]]; then
  printf 'Codex Scheduler 安装程序\n========================\n\n'
  python3 install.py
  printf '\n按 Return 键关闭… '
else
  printf 'Codex Scheduler installer\n=========================\n\n'
  python3 install.py
  printf '\nPress Return to close… '
fi
read _
