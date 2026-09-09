#!/bin/bash
# Link the two skills into ~/.claude/skills and point them at this checkout.
#
# Symlinks rather than copies: editing the skill in the repo is the same edit
# Claude Code loads on the next run, so the published copy cannot drift from the
# installed one.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKILLS_DIR="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"

mkdir -p "$SKILLS_DIR"

for skill in deep-search custom-search; do
    target="$SKILLS_DIR/$skill"
    if [ -e "$target" ] && [ ! -L "$target" ]; then
        echo "skip $skill: $target exists and is not a symlink" >&2
        continue
    fi
    ln -sfn "$REPO/skills/$skill" "$target"
    echo "linked $target -> $REPO/skills/$skill"
done

echo
echo "Set the stack path for the skill (add to ~/.zshrc or ~/.bashrc):"
echo "    export INTERNET_CLAUDE_DIR=$REPO"
echo
echo "Branch tavily+jina also needs TAVILY_API_KEY_1..3 and JINA_API_KEY in the"
echo "environment. Without them deep-search runs on searxng+crawl4ai alone."
