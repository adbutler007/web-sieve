# web-sieve effectiveness spec

Written 2026-10-04. Builds on `jev-relevance-spec.md` (the relevance pass). The goal is to make web-sieve more effective on every axis that matters for its job, which is: let Claude read the web without raw page text entering the main context, with per-project caching that is honest about what it holds.

## Axes and the current gaps

| Axis | What matters | Gap found 2026-10-03 |
|---|---|---|
| Fetch honesty | A cached page is real content, or the caller is told it is not | 279 of 1,629 cached pages are under 1.2 KB; many are Cloudflare "Just a moment..." challenge pages, empty-title shells and EDGAR result frames, cached as content. `find_relevant_ranges` then returns "not relevant" for pages that were never fetched. |
| Fetch robustness | Transient failures do not lose results or raise | `_fetch_one` has no retry; `URLError`, timeouts and `IncompleteRead` propagate as exceptions; strict UTF-8 decode can raise. |
| Freshness | Pages that change can be refreshed on request | No expiry, no refresh flag; a blocked page would be blocked forever. |
| Reuse across sessions | Already-cached material is findable without refetching | Only `manifest.md` (titles). No search over cached content. |
| Context economy | Nothing raw reaches the main context | Done by the relevance pass; the manifest table grows unbounded and has no status or size. |
| Privacy | Page text only goes to Jev when the project allows it | `find_relevant_ranges` sends any cached page. Other Jev hooks on the machine share a deny list in a config file; web-sieve ignores it. |
| Calibration | The 0.5 threshold is measured, not guessed | Not run. The smoke call missed a 0.40 window that answered the question. |
| Fail-loud contracts | Every tool returns a per-source status and never raises | Mostly true for the relevance pass; not for fetch. |

Out of scope: a cross-project cache (per-project isolation is deliberate), PDF routing (the liteparse rule covers PDFs), ReaderLM, WebSearch.

## 1. Fetch quality gate

Add `_classify_body(url, body) -> (status, reason)` applied to every Jina response before caching, and to existing pages by the audit command.

- Strip Jina's preamble lines (`Title:`, `URL Source:`, `Published Time:`, `Markdown Content:`) to get the body proper.
- `challenge`: the title or the first 2,000 body characters match, case-insensitively, any of: "Just a moment", "Attention Required", "Access denied", "Verify you are human", "Checking your browser", "Enable JavaScript and cookies", "Please wait while we verify", "Security check", "cf-browser-verification", "captcha", "Request blocked", "403 Forbidden", "Error 1020", "unusual traffic".
- `empty`: the body proper is under 20 characters.
- `thin`: the body proper is under 400 characters and not a challenge. Cached, but with `status: thin` in the frontmatter and a warning in the result, because small raw files (a package.json) are legitimate.
- `ok`: everything else.

Add `status:` and `bytes:` to the frontmatter of every new page. Existing pages without `status:` are treated as `ok` by readers until the audit runs.

## 2. Retry and alternate strategy

`_fetch_one` becomes `_fetch(url, cache_dir, max_age_days, refresh)` and never raises.

- Transport errors (`URLError`, socket timeout, `http.client.IncompleteRead`, `RemoteDisconnected`) and HTTP 408, 429, 5xx are retried once after a backoff of 2 s (honour `Retry-After` up to 30 s). Two attempts total. Then `status: error` with `reason` and the HTTP code.
- Decode with `errors="replace"`; count replacements; over 0 adds a warning `decode_replacements: n`.
- On `challenge`, retry once with an alternate Jina strategy. The implementer must verify the header names against the Jina Reader documentation cached under this repository's `.web_cache/` (fetched 2026-10-04 from jina.ai/reader and the jina-ai/reader README). Candidates in order of preference: a no-cache request (`X-No-Cache: true`) combined with a proxy option if one is documented; failing that, the same request a second time. Do not invent headers; if the docs do not show one, skip it and say so in the README.
- If still a challenge or empty: do not write a page. Write a sidecar `<hash>.blocked.json` with `{url, status, reason, attempts, at}` and return `status: blocked` with `fallback: "firecrawl"`. A later request for the same URL within 24 hours returns the sidecar without a network call; after 24 hours it refetches and replaces the sidecar.
- The MCP result for every source carries `status` (ok, thin, blocked, error, cached), `reason`, `warnings`, `attempts`.

## 3. Freshness

- `read_url`, `batch_read_urls` and `find_relevant_ranges` gain `max_age_days: float | None = None` and `refresh: bool = False`. With `max_age_days` set, a cached page older than that is refetched; `refresh` forces a refetch. A refetch overwrites the page and reports `refreshed: true`. Default behaviour is unchanged (never expires).
- The frontmatter `fetched:` field already exists; keep its format.

## 4. Manifest

- Keep `manifest.md` as the human table, with two new columns: `Status` and `KB`. Blocked sidecars appear as rows with status `blocked` so the manifest shows what was tried.
- Add `manifest.json` next to it: a list of `{file, url, title, fetched, status, bytes, lines}` rebuilt with the table. `search_cache` and `audit` read it; no reader parses `manifest.md`.

## 5. `search_cache(query, cache_dir, top_k=10, jev=False)`

A new MCP tool and CLI subcommand that finds already-cached material.

- Lexical: BM25 (stdlib, k1=1.2, b=0.75) over the same windows `find_relevant_ranges` uses, tokenised on `\w+` lower-cased, title tokens counted twice. The index is built per call from `manifest.json` and the page files, cached in `.web_cache/search_index.sqlite` keyed by file hash and mtime so a repeat query on an unchanged cache is a read.
- Output per page: `{file, url, title, score, windows: [[start, end, score], ...]}` for the top_k pages, best three windows each. No page text in the output.
- `jev=True`: take the top 20 windows across pages and run the existing Noul machinery (same state design, answer cache, model pin, deny list) to rerank, returning `p` next to the BM25 score and `jev_requests`, `cost_usd`. Fails loud exactly as `find_relevant_ranges` does.
- Privacy: `jev=True` honours the deny list in section 6.

## 6. Privacy deny list for Jev sends

Revised 2026-10-04: the project names are read from the config file only, because this repository is public and a built-in list would publish them.

- `_jev_denied(cache_dir) -> reason | None`. Resolve the real path of `cache_dir`. Deny if any path component, as given or resolved, equals an entry, starts with `<entry>-wt-` (a git worktree of that project), or, for an entry ending in `*`, starts with the part before the `*`, ignoring case; also deny a resolved path under an entry of `privacy.deny_path_prefixes`. The entries are the `privacy.deny_projects` list in `~/.claude/jev_hooks/config.json` (the file other Jev hooks on the machine read) plus the generic built-in names `clients, client_data, client-data`. No other name is written in the source.
- When that file is missing, does not parse, or has no `privacy.deny_projects` list, only the built-in names apply, and every result that sends pages to Jev carries a warning that starts `deny_list: not configured`, so the gap is visible.
- `find_relevant_ranges` and `search_cache(jev=True)` return `status: denied` with the reason and send nothing. The lexical path still works.

## 7. `audit` command

`web-sieve audit <cache_dir> [--apply]` and an MCP tool `audit_cache(cache_dir, apply=False)`.

- Classifies every page with `_classify_body`, reports counts per status and lists challenge and empty pages.
- With `--apply`: moves challenge and empty pages to `<cache_dir>/_quarantine/` (never deletes), appends one JSON line per move to `<cache_dir>/_quarantine/quarantine.jsonl` with the reason, writes a blocked sidecar for each so the next fetch of that URL is retried rather than served from the stub, marks thin pages `status: thin` in their frontmatter, and rebuilds both manifests.
- Idempotent: a second run moves nothing.

## 8. Calibration

Run separately (see the calibration agent brief): Sonnet labels, Haiku baseline, `calibrate.py` over the six configurations. The implementer leaves `DEFAULT_THRESHOLD` as a single constant so it can be set from the result.

## 9. Documentation

- README: the quality gate, statuses, retry, freshness flags, `search_cache`, `audit`, the deny list, and the Firecrawl fallback rule.
- `claude-md-snippet.md`: the recipe becomes: WebSearch; `search_cache` on the project cache first; `batch_read_urls` for what is missing; `find_relevant_ranges`; Read only the ranges; a `blocked` result goes to Firecrawl. Do not edit `~/.claude/CLAUDE.md`.

## 10. Tests (pytest, fake servers, no network)

A fake Jina server in `tests/fake_jina.py` that can return: a normal page, a challenge page, an empty body, a 429 with Retry-After, a 503 then 200, an IncompleteRead (close mid-body), invalid UTF-8, and a slow response.

Cases, each a separate test:
- classify: each challenge phrase, case-insensitive; empty; thin at 399 and ok at 400; a legitimate small raw file is thin not challenge; preamble stripping.
- fetch: challenge triggers one alternate attempt with the documented headers (assert the headers the fake server received), then a sidecar and `status: blocked`; sidecar served within 24 h without a request; refetched after 24 h (mock the clock); 429 honours Retry-After and succeeds on the second attempt; 503 then 200; IncompleteRead retried once; invalid UTF-8 counted; no exception escapes `batch_read_urls` for any failure kind; frontmatter has `status` and `bytes`.
- freshness: `max_age_days` refetches an old page and reports `refreshed`; `refresh=True` forces; defaults never refetch.
- manifest: the two columns; blocked rows; `manifest.json` fields; rebuilt after audit.
- search_cache: ranking on a synthetic cache (query terms in title outrank body); windows reported with correct line numbers; index reused on unchanged cache (count rebuilds); invalidated on file change; `jev=True` reranks top 20 through the fake Jev server and reuses the answer cache; denied project returns `status: denied` with no request; no page text in output.
- deny list (a temporary config file, never the machine's own): names from the config, star entries, `-wt-` worktrees, path prefixes, the built-in names; a missing or unparsable config warns on every Jev-sending result; a symlinked cache_dir resolves to its real path.
- audit: dry run moves nothing; apply moves challenge and empty pages, writes the quarantine log and sidecars, marks thin, rebuilds manifests; second run is a no-op; `ok` pages untouched byte-for-byte.
- CLI and MCP return identical JSON for the same inputs for every new tool.

The existing 103 tests must keep passing. No skipped tests.

## 11. Deployment

Copy `web-sieve.py` to `~/.claude/mcp-servers/web-sieve.py` after a dated backup, and smoke the launcher with `search_cache` on one project's `.web_cache` (no network) and `audit --dry-run` on the same directory. Then run `audit --apply` across every `~/Projects/*/.web_cache` and report the counts moved per project.

## Decisions and their reasons

- Quarantine, never delete: the stubs are evidence of which sites block Jina, and the move is reversible.
- Challenge pages are never cached as content: a cached challenge is a permanent blind spot that reads as a clean negative.
- Thin pages are cached: small legitimate files exist, and the warning is enough.
- A sidecar rather than a manifest flag: the sidecar expires on its own and survives manifest rebuilds.
- BM25 per project rather than a global index: per-project isolation is a privacy boundary.
- The deny list is read from the shared config file only, so one list governs every Jev send from the machine and no private project name is published with this public repository.
