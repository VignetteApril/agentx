#!/bin/zsh
set -euo pipefail
TARGET="${HOME}/.local/bin/agentx"
LIB_TARGET="${HOME}/.local/bin/agentxlib"
if [[ -f "$TARGET" ]]; then
  rm "$TARGET"
  echo "Removed $TARGET"
else
  echo "agentx executable not found at $TARGET"
fi
if [[ -d "$LIB_TARGET" ]]; then
  rm -rf "$LIB_TARGET"
  echo "Removed $LIB_TARGET"
fi

echo
echo "Profile/login state was NOT removed."
echo "It remains under ~/.agentx"
