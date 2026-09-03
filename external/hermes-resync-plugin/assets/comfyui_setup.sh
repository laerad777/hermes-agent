#!/usr/bin/env bash
# Bash 3.2-compatible, standalone ComfyUI workspace setup helper.
set -euo pipefail

if [ "$#" -gt 1 ]; then
  printf '%s\n' "usage: $0 [workspace]" >&2
  exit 64
fi

# An omitted or explicitly empty workspace means the current directory.
workspace=${1:-"$PWD"}
case "$workspace" in
  /*) ;;
  *) workspace="$PWD/$workspace" ;;
esac

# Preserve an explicitly supplied port; use ComfyUI's conventional default otherwise.
PORT=${PORT:-8188}
case "$PORT" in
  ''|*[!0-9]*) printf '%s\n' 'PORT must be an integer from 1 through 65535.' >&2; exit 64 ;;
esac
if [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
  printf '%s\n' 'PORT must be an integer from 1 through 65535.' >&2
  exit 64
fi

mkdir -p "$workspace"
printf 'workspace=%s\nPORT=%s\n' "$workspace" "$PORT"
printf '%s\n' "Run ComfyUI from this workspace with: python main.py --port $PORT"
