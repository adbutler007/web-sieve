## Web

Always use this workflow for any web search or fetch task:

1. **WebSearch** to discover relevant URLs (returns concise summaries + source links)
2. **search_cache** (web-sieve MCP) on the project cache first, with the question or its key words and `cache_dir` as the absolute path to `{project}/.web_cache/`. It searches pages already cached (no network) and returns files, scores and line ranges, never page text. Pages it finds need no new fetch.
3. **batch_read_urls** (web-sieve MCP) for the URLs that are still missing, all in one call, with the same absolute `cache_dir`. Fetches run in parallel server-side (8 threads). Returns one JSON object per URL (status, path, title, lines, chars) — NOT the content. Pages are cached for reuse; pass `max_age_days` or `refresh` only when a page may have changed. Use `read_url` only for single-page fetches.
4. **find_relevant_ranges** (web-sieve MCP) with the question and the `path` values from steps 2 and 3, all pages in one call (URLs also work; uncached ones are fetched first). Pass the same absolute `cache_dir`. Each page returns `status`, `relevant`, `ranges` (`[[start_line, end_line], ...]`, file line numbers for Read offset/limit) and every window's probability; the default threshold is 0.85 (calibrated 2026-10-04: line recall 0.967 against 0.909 for Haiku triage, 0.32 s per page against 9.5 s). If a page's `status` is not `ok`, it was not fully judged: Read its `unjudged` lines or triage that page with a haiku agent. Never treat it as not relevant. `status: denied` means the project is on the Jev deny list; triage that page with a haiku agent.
5. **Read** only the relevant line ranges into main context using offset/limit
6. A result with `status: blocked` (a bot challenge or an empty page; `fallback: "firecrawl"`) was not cached. Fetch that URL with Firecrawl instead. A `thin` page is cached but may be a stub; check its warning.

Never use WebFetch. Never put raw page content directly into the main context.
