# web-sieve

An MCP server for Claude Code that fetches web pages as clean markdown, caches them locally, and enables AI-powered triage so only the relevant content enters your context window.

**70-95% context window compression** — fetch 5 pages (83K chars), use only what matters (~10K chars).

## How it works

```
WebSearch → batch_read_urls → .web_cache/ → Haiku agents → Read ranges
```

1. **WebSearch** (built-in) discovers URLs and returns summaries
2. **batch_read_urls** fetches all pages in parallel (8 threads) via [Jina Reader](https://r.jina.ai), strips junk (nav, cookies, ads, sidebars — 30+ CSS selectors), and caches clean markdown to `{project}/.web_cache/`
3. **Parallel Haiku agents** scan each cached page and return structured JSON with relevant line ranges
4. **Read** pulls only those ranges into the main context

The tool returns **metadata only** — content stays on disk until explicitly requested.

## Install

### Prerequisites

- [Claude Code](https://claude.ai/claude-code) CLI
- [uv](https://docs.astral.sh/uv/) (Python package runner)
- A [Jina API key](https://jina.ai) (free tier available)

### Mac / Linux

```bash
git clone https://github.com/adbutler007/web-sieve.git
cd web-sieve
./install.sh
```

The installer will prompt for your Jina API key, copy the server script to `~/.claude/mcp-servers/`, and register it globally.

Then paste the contents of `claude-md-snippet.md` into `~/.claude/CLAUDE.md` and restart Claude Code.

### Windows

```powershell
git clone https://github.com/adbutler007/web-sieve.git
cd web-sieve

# 1. Copy the server script
mkdir -p "$env:USERPROFILE\.claude\mcp-servers"
copy web-sieve.py "$env:USERPROFILE\.claude\mcp-servers\"

# 2. Register with Claude Code (replace YOUR_KEY with your Jina API key)
claude mcp add -s user -e "JINA_API_KEY=YOUR_KEY" -- web-sieve `
    uv run --script "$env:USERPROFILE\.claude\mcp-servers\web-sieve.py"

# 3. Append workflow instructions to CLAUDE.md
Get-Content claude-md-snippet.md | Add-Content "$env:USERPROFILE\.claude\CLAUDE.md"

# 4. Restart Claude Code
```

## Tools

| Tool | Purpose |
|---|---|
| `batch_read_urls` | Fetch multiple URLs in parallel, cache to disk, return metadata |
| `read_url` | Fetch a single URL (same caching behavior) |
| `list_cache` | List all cached pages with metadata |

## Cache format

Pages are cached as `.web_cache/{sha256[:12]}.md` with YAML frontmatter:

```yaml
---
url: https://example.com/article
title: Article Title
fetched: 2026-02-15T10:30:00+00:00
hash: a1b2c3d4e5f6
---
[clean markdown content]
```

Cache is **permanent and project-scoped** — pages persist across sessions and can be re-queried with different questions.

A `manifest.md` file is auto-generated in the cache directory on every fetch, listing all cached pages in a markdown table. Reference it from your `CLAUDE.md` so Claude knows what's cached even after context compaction.

## Performance

| Scenario | Pages | Cached | Used | Compression |
|---|---|---|---|---|
| Narrow query | 2 | 24K chars | 1.2K chars | **95%** |
| Comparison | 3 | 29K chars | 8.5K chars | **70%** |
| Broad research | 5 | 83K chars | 10.5K chars | **87%** |

Haiku triage runs in parallel — latency is ~3-5s regardless of page count.

## What gets stripped

30+ CSS selectors remove common junk before caching:

- Navigation (nav, header, footer)
- Cookie/consent banners
- Newsletter/subscribe popups
- Modals and overlays
- Sidebar widgets
- Ads, sponsors, promos
- Social share buttons
- Comment sections
- All images (`X-Retain-Images: none`)

## Limitations

- **Cloudflare-protected sites** (Medium, HN) may return challenge pages. Skip these.
- **Nav-heavy sites** may still have junk despite CSS stripping.
- **Restart required** after editing `web-sieve.py` — restart Claude Code to apply changes.
- **`cache_dir` must be absolute** — the MCP server's working directory may differ from your project.

## Docs

See [docs/web-pipeline-explained.pdf](docs/web-pipeline-explained.pdf) for a detailed walkthrough with diagrams.

## License

MIT
