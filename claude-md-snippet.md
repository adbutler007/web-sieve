## Web

Always use this workflow for any web search or fetch task:

1. **WebSearch** to discover relevant URLs (returns concise summaries + source links)
2. **batch_read_urls** (web-sieve MCP) to fetch all URLs in one call — always pass `cache_dir` as the absolute path to `{project}/.web_cache/`. Fetches run in parallel server-side (8 threads). Returns JSON array of metadata (path, title, lines, chars) — NOT the content. Pages are cached permanently for reuse. Use `read_url` only for single-page fetches.
3. **Deploy parallel haiku Task agents** against each cached file with the query. Agents must return structured JSON only: `{"relevant": true, "ranges": [[start_line, end_line], ...]}` or `{"relevant": false}`. No prose.
4. **Read** only the relevant line ranges into main context using offset/limit
5. **Before any web task**, read `{project}/.web_cache/manifest.md` if it exists — it lists all cached pages. Reuse cached pages instead of re-fetching.

Never use WebFetch. Never put raw page content directly into the main context.
