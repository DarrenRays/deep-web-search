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
echo "The skill resolves the symlink to find $REPO, so no path variable is"
echo "needed. Set DEEP_SEARCH_DIR only if you copied the skill instead."
