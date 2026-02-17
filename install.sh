#!/usr/bin/env bash
set -euo pipefail

# web-sieve installer for Claude Code
# Drops the MCP server script into ~/.claude/mcp-servers/ and registers it globally.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.claude/mcp-servers/web-sieve.py"

# Check for uv
if ! command -v uv &>/dev/null; then
    echo "Error: uv is required but not installed."
    echo "Install it: curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

# Check for claude
if ! command -v claude &>/dev/null; then
    echo "Error: claude (Claude Code CLI) is required but not installed."
    echo "Install it: npm install -g @anthropic-ai/claude-code"
    exit 1
fi

# Prompt for API key if not set
if [ -z "${JINA_API_KEY:-}" ]; then
    echo -n "Enter your Jina API key (from https://jina.ai): "
    read -r JINA_API_KEY
    if [ -z "$JINA_API_KEY" ]; then
        echo "Error: Jina API key is required."
        exit 1
    fi
fi

# Copy server script
mkdir -p "$(dirname "$DEST")"
cp "$SCRIPT_DIR/web-sieve.py" "$DEST"
echo "Installed web-sieve.py → $DEST"

# Register with Claude Code
claude mcp add -s user -e "JINA_API_KEY=$JINA_API_KEY" -- web-sieve \
    uv run --script "$DEST"
echo "Registered web-sieve MCP server globally."

echo ""
echo "Done! Restart Claude Code to activate."
echo ""
echo "Recommended: add these instructions to ~/.claude/CLAUDE.md:"
echo ""
cat "$SCRIPT_DIR/claude-md-snippet.md"
