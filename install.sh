#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
TARGET="${BIN_DIR}/agentx"
LIB_TARGET="${BIN_DIR}/agentxlib"

mkdir -p "$BIN_DIR"
cp "$ROOT/agentx.py" "$TARGET"
rm -rf "$LIB_TARGET"
cp -R "$ROOT/agentxlib" "$LIB_TARGET"
chmod +x "$TARGET"

echo "Installed agentx -> $TARGET"

if ! command -v tmux >/dev/null 2>&1; then
  echo
  echo "tmux is required and was not found."
  if command -v brew >/dev/null 2>&1; then
    echo "Install it with:"
    echo "  brew install tmux"
  else
    echo "Install Homebrew first, then:"
    echo "  brew install tmux"
  fi
fi

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    echo
    echo "$BIN_DIR is not currently in PATH."
    echo "Add this to ~/.zshrc:"
    echo '  export PATH="$HOME/.local/bin:$PATH"'
    ;;
esac

echo
echo "Next:"
echo "  agentx doctor"
echo "  agentx account add codex codex-1"
echo "  agentx account add codex codex-2"
echo "  agentx account add agy agy"
