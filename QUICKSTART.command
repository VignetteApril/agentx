#!/bin/zsh
cat <<'EOF'
agentx quick start

1) Install tmux if needed:
   brew install tmux

2) Install agentx:
   ./install.sh

3) Configure accounts:
   agentx account add codex codex-1
   agentx account add codex codex-2
   agentx account add agy agy

4) Enter any project:
   cd ~/projects/your-project
   agentx

5) Switch:
   Ctrl+G 1   Codex #1
   Ctrl+G 2   Codex #2
   Ctrl+G 3   AGY

   Ctrl+G c   Context Dashboard
   Ctrl+G h   Capture Context Pack
   Ctrl+G ?   Keyboard help

   Context transfer + switch:
   agentx transfer codex-2

6) Detach but keep everything running:
   Ctrl+G d
EOF
