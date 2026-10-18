#!/bin/zsh
# Opt-in only. Does not alter the installed woplanner-quali or editable installs.
set -eu
script_dir="${0:A:h}"
exec /Users/zhxutong/dr-wo/.venv/bin/python -I -B "$script_dir/q17_quali_isolated.py" "$@"
