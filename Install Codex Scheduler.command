#!/bin/zsh
set -e
cd "${0:A:h}"
clear
printf 'Codex Scheduler installer\n=========================\n\n'
python3 install.py
printf '\nPress Return to close… '
read _
