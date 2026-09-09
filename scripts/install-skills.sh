#!/bin/bash
# Link the deep-search skill into ~/.claude/skills.
#
# A symlink rather than a copy: editing the skill in the repo is the same edit
# Claude Code loads on the next run, so the published copy cannot drift from the
# installed one.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKILLS_DIR="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"

mkdir -p "$SKILLS_DIR"

target="$SKILLS_DIR/deep-search"
if [ -e "$target" ] && [ ! -L "$target" ]; then
    echo "skip: $target exists and is not a symlink" >&2
else
    ln -sfn "$REPO/skills/deep-search" "$target"
    echo "linked $target -> $REPO/skills/deep-search"
fi

echo
echo "Set the stack path for the skill (add to ~/.zshrc or ~/.bashrc):"
echo "    export INTERNET_CLAUDE_DIR=$REPO"
echo
echo "Branch tavily+jina also needs TAVILY_API_KEY_1..3 and JINA_API_KEY in the"
echo "environment. Without them deep-search runs on searxng+crawl4ai alone."
