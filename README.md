# web-sieve

An MCP server for Claude Code that fetches web pages as clean markdown, caches them locally, and enables AI-powered triage so only the relevant content enters your context window.

**70-95% context window compression** — fetch 5 pages (83K chars), use only what matters (~10K chars).

## How it works

```
WebSearch → search_cache → batch_read_urls → .web_cache/ → find_relevant_ranges (Jev) → Read ranges
                                    └─ blocked → Firecrawl
```

1. **WebSearch** (built-in) discovers URLs and returns summaries
2. **search_cache** looks for the answer in pages the project has already cached (BM25, no network)
3. **batch_read_urls** fetches the pages that are missing, in parallel (8 threads), via [Jina Reader](https://r.jina.ai), checks each response with a quality gate, and caches real pages as markdown in `{project}/.web_cache/`. A bot-challenge or empty response is not cached: it comes back as `status: "blocked"` with `fallback: "firecrawl"`
4. **find_relevant_ranges** asks [Jev](https://docs.typesafe.ai) one yes/no question per window of each cached page and returns the relevant line ranges, with every window's probability
5. **Read** pulls only those ranges into the main context

The tool returns **metadata only** — content stays on disk until explicitly requested.

## Install

### Prerequisites

- [Claude Code](https://claude.ai/claude-code) CLI
- [uv](https://docs.astral.sh/uv/) (Python package runner)
- A [Jina API key](https://jina.ai) (free tier available)
- Optional, for `find_relevant_ranges` and `search_cache` with `jev`: a Jev client file and a TypeSafe API key (see [Jev relevance](#jev-relevance))

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
| `batch_read_urls` | Fetch multiple URLs in parallel, cache to disk, return metadata and a `status` per URL |
| `read_url` | Fetch a single URL (same caching behavior) |
| `list_cache` | List all cached pages with metadata; a page it cannot read has `error`, and a directory it cannot list gives an error object |
| `search_cache` | Search the pages already cached in a project (BM25 over windows, optional Jev rerank); returns files, scores and line ranges, never page text |
| `find_relevant_ranges` | Find the line ranges of cached pages that help answer a question, using Jev; returns `status`, `relevant`, `ranges` and every window's probability per page |
| `audit_cache` | Check a cache for challenge, empty and thin pages; with `apply`, quarantine the stubs |

CLI equivalents: `uv run --script web-sieve.py read`, `batch`, `list`, `search`, `ranges` and `audit` print the same JSON as the MCP tools (the examples below write `web-sieve` for `uv run --script web-sieve.py`). With no arguments the file starts the MCP server.

## Fetching

### Quality gate and statuses

Every Jina response is classified before it is cached. The Jina preamble (`Title:`, `URL Source:`, `Published Time:`, `Number of Pages:` (PDFs), `Warning:` lines and `Markdown Content:`) is removed first; the rest is the body.

| Class | Rule | What happens |
|---|---|---|
| `challenge` | The title contains (ignoring case) one of: "Just a moment", "Attention Required", "Access denied", "Verify you are human", "Checking your browser", "Enable JavaScript and cookies", "Please wait while we verify", "Security check", "cf-browser-verification", "captcha", "Request blocked", "403 Forbidden", "Error 1020", "unusual traffic"; or the body is under 3,000 characters and its first 2,000 characters contain one of them ("captcha" only when the body is under 400 characters) | Alternate request, then `blocked` |
| `empty` | The body is under 20 characters, or it is a Jina error object (`{"code": 451, "name": ..., "message": ...}`) served with status 200 | Alternate request, then `blocked` |
| `thin` | The body is under 400 characters and is not a challenge | Cached, with `status: thin` in the frontmatter and a warning, because small legitimate files exist |
| `ok` | Anything else | Cached |

Jina's own `Warning:` lines are not used as evidence of a challenge: its "maybe requiring CAPTCHA" warning also appears on real pages that only contain a form with a CAPTCHA widget. For the same reason a phrase in the body of a page of 3,000 characters or more is page text, and "captcha" in the body counts only on a thin body: on 2026-10-04 every body mention of "captcha" in 1,575 cached pages was a form field or a cookie notice (one, a 750-character contact page, had been quarantined by the first version of the rule and was restored), while all 52 real challenge pages matched in the title and had bodies of 388 characters or less.

Every result from `read_url` and `batch_read_urls` has `status`, `reason`, `warnings`, `attempts`, `cached` and `refreshed`:

- `cached`: served from the cache with no request; `page_status` is the page's frontmatter status (`ok` for pages written before the quality gate).
- `ok` or `thin`: fetched and cached. A thin page carries a `thin page:` warning.
- `blocked`: a challenge or empty response, also after the alternate request. No page is written; a sidecar `<hash>.blocked.json` (`url`, `status`, `reason`, `attempts`, `at`) is, and the result carries `fallback: "firecrawl"`. **Fetch a blocked URL with Firecrawl instead.**
- `error`: the request failed; `error`, `detail` and `http_status` say how.

Warnings also report Jina's own warnings (`jina: Target URL returned error 404: Not Found`) and invalid UTF-8 (`decode_replacements: n`; the bytes are replaced, not fatal).

### Retry

Transport errors (`URLError`, timeouts, `IncompleteRead`, `RemoteDisconnected`) and HTTP 408, 429 and 5xx are retried once, after the `Retry-After` header (seconds or an HTTP date, read as UTC; at most 30 s) or 2 s. Two attempts in total; then `status: "error"` naming each attempt's failure. Other HTTP errors are not retried, and neither is a URL that cannot be sent at all (a host with a space, or a host IDNA cannot encode). No fetch raises: every failure is that URL's result, and the other URLs in a batch are unaffected.

Each URL has 200 s in total for all its requests and waits, including the alternate request below. A request waits at most 180 s for an answer, or less when less of the 200 s is left, and a retry or alternate request is sent only when at least 10 s would be left for it; otherwise the result says it was not sent and why. So a URL whose requests get no answer ends within 200 s. Before this cap the worst case was 362 s for the two attempts (two 180 s timeouts and the 2 s wait), and longer when a challenge led to the alternate request.

### URLs

The URL sent to Jina has a non-ASCII host in IDNA (punycode) form, and spaces, control characters and non-ASCII characters in the rest of the URL percent-encoded as UTF-8. Existing `%XX` escapes and other printable ASCII are sent as they are, and a `%` that does not start an escape becomes `%25`. The cache file name and the frontmatter `url:` keep the URL as given. On 2026-10-04 Jina fetched a Wikipedia article whose title has an accented letter and an IDNA test host this way; before, urllib refused such URLs without sending them.

### Alternate request for blocked pages

A `challenge` or `empty` response is fetched once more with:

- `X-No-Cache: true`, which bypasses Jina's own cache (it may hold an earlier blocked response), and
- `X-Proxy: auto`, which routes the request through Jina's proxy pool, **only when `JINA_API_KEY` is set**, because the jina-ai/reader README says the proxy needs a key.

Both headers are documented on [jina.ai/reader](https://jina.ai/reader) (parameter list) and in the [jina-ai/reader README](https://github.com/jina-ai/reader) ("Having trouble on some websites?"), read 2026-10-04. `X-Engine: browser`, the README's other suggestion, is already on every request. No other header is sent; `X-Proxy-Url` (your own proxy) is documented but needs a proxy URL, so it is not used. On 2026-10-04 Jina answered a request with both headers normally (HTTP 200), and an SSRN abstract page was still a Cloudflare challenge after the alternate request, so it ended as `blocked`.

A repeat request for a blocked URL within 24 hours of the sidecar's time returns the sidecar with no request sent. After 24 hours the URL is fetched again and the sidecar is replaced (by a page on success, or by a new sidecar).

### Freshness

`read_url`, `batch_read_urls` and `find_relevant_ranges` take `max_age_days` (default none) and `refresh` (default false); the CLI flags are `--max-age-days D` and `--refresh`.

- By default a cached page never expires, as before.
- With `max_age_days`, a page whose `fetched:` time is older than that is fetched again.
- `refresh` fetches again regardless, and ignores a blocked sidecar.
- A refetch overwrites the page and reports `refreshed: true`. When a refetch fails or is blocked, the old copy is kept and named in `kept_path`.

## search_cache

`search_cache(query, cache_dir, top_k=10, jev=False)` (CLI: `web-sieve search "QUERY" --cache-dir DIR [--top-k N] [--jev]`) finds material the project has already cached, without a network call.

- BM25 (k1 = 1.2, b = 0.75) over the same windows `find_relevant_ranges` uses (200 tokens by default), tokenised as lower-cased `\w+`, with the page title's tokens counted twice in every window of the page. The `Section:` line that `find_relevant_ranges` sends to Jev is not indexed.
- Output: `status`, `pages` (the top `top_k`), each `{file, url, title, score, windows}`, where `windows` are the page's best three as `[start, end, score]` file line numbers for Read offset/limit; `index` (pages, windows, rebuilt, reused, removed); `warnings`. No page text.
- Pages come from `manifest.json`; when it is missing or does not match the files on disk, the directory is listed instead, with a warning.
- Any cache directory is searched, also one with another name or a `.web_cache` link to one, because the lexical search sends nothing anywhere. With `jev=True` only pages whose real directory is named `.web_cache` are sent; the others are reported as `not_a_cached_page`.
- The index is `search_index.sqlite` in the cache directory. A page is windowed again only when its modification time or size changed and its sha256 changed too, so a repeat query on an unchanged cache only reads the index. The file is stamped with the index version (2 since 2026-10-05), the window size, the link setting and the title weight; when any of them differs from the running code, the whole index is rebuilt on the next search, so an index built with 400-token windows is replaced by one with 200-token windows.
- `jev=True` asks Jev about the top 20 windows (with the same state, Noul, answers cache, pinned model and fail-fast rule as `find_relevant_ranges`) and orders pages and windows by its probability; each window becomes `[start, end, score, p]` and each page gains `p`. The output adds `jev_requests`, `cache_hits`, `input_tokens` and `cost_usd`. A Jev failure gives `status` `partial` or `error` with `error.kind`, and the lexical ranking is still returned. The deny list applies (below).

## audit

`web-sieve audit <cache_dir> [--apply | --dry-run]` (MCP: `audit_cache(cache_dir, apply=False)`) classifies every cached page with the quality gate.

- The default (also `--dry-run`) changes nothing and reports counts per class and the challenge and empty pages.
- `--apply` moves challenge and empty pages to `<cache_dir>/_quarantine/` (never deletes them), appends one JSON line per move to `_quarantine/quarantine.jsonl`, writes a blocked sidecar for each URL dated at the page's `fetched:` time (so the next fetch after 24 hours from that time goes to the network rather than to the stub), adds `status: thin` to the frontmatter of thin pages, and rebuilds `manifest.md` and `manifest.json`. Adding that line moves the thin page's later lines down by one.
- `ok` pages are never written. A second run moves nothing.

## Jev relevance

`find_relevant_ranges` (CLI: `web-sieve ranges`) replaces the Haiku triage step. It splits the body of each cached page into windows of about 200 tokens (400 until 2026-10-05), breaking before headings and at blank lines and never inside a code block or table below the 400-token maximum; a window under the 40-token minimum does not end at a heading. It asks TypeSafe's Jev one yes/no question per window ("does this window help answer the question?") and returns the line ranges of the windows whose probability is at or above the threshold. Jev cannot return line ranges, so the windows and the ranges are computed in code. The design is in [docs/jev-relevance-spec.md](docs/jev-relevance-spec.md).

### Windows and the Section line

A 200-token window often starts in the middle of a section, below the heading that says what the section is about. So the text sent to Jev for each window starts with one line naming its heading path:

```
Section: Configure permissions > Permission rule syntax > Match by input parameter
<the window's lines>
```

- The path is the nearest heading above the window's first line (ATX `#` or setext, underlined with `===` or `---`) and its parent headings up to the top level, joined with ` > `. Lines inside code fences are never headings.
- Each heading is cut to 80 characters, and the whole path is capped at 160 characters by dropping the top-level headings first (shown as `…`), so the nearest heading is always kept.
- A window above the first heading, or on a page without headings, gets `Section: (none)`.
- The line exists only in the request. Window line numbers, `ranges`, `range_detail` and the search index do not include it.
- Before 2026-10-05 the same heading path was sent as a separate `section` field next to the window text. The constant `SECTION_LINE` switches between the two forms; the calibration below compared them.

### Requirements

- **The jev client.** web-sieve loads a standard-library Jev client file named `jev` (it must define `ask`, `resolve_key`, `POLICIES` and the client's error classes) with importlib, from `WEB_SIEVE_JEV` when that is set, else `jev` on `PATH`, else `~/.local/bin/jev`. It is not vendored: other users need their own copy and can point `WEB_SIEVE_JEV` at it. Without it, `find_relevant_ranges` returns `status: "error"` with `kind: "no_client"`; the fetch tools are unaffected.
- **A TypeSafe API key.** The client reads `JEV_API_KEY` from the environment, else the macOS Keychain item with service `JEV_API_KEY` (`security find-generic-password -s JEV_API_KEY -w`), else `secret-tool` on Linux. The key is never printed, logged, returned or stored.

### Usage

```bash
web-sieve ranges "What are the rate limits?" /abs/project/.web_cache/199f6b071e6b.md [MORE PAGES OR URLS] \
    [--cache-dir DIR] [--threshold T] [--window-tokens N] [--max-windows N]
```

The output is a JSON list with one object per source, in input order: `status` (`ok`, `partial` or `error`), `relevant`, `ranges` (`[[start, end], ...]`, 1-based file line numbers for Read offset/limit), `range_detail`, `windows` (`[start, end, p]` for every window), `split_lines`, `long_lines`, `unjudged`, `requests`, `cache_hits`, `input_tokens`, `cost_usd`, `elapsed_ms`, `jev_events` (every retry, hedge and give-up) and `warnings`. A page whose `status` is not `ok` was not fully judged: `relevant` is `null` unless a judged window passed, and `unjudged` lists the lines Jev did not judge. Such a page must not be treated as irrelevant.

CLI exit codes: 0 when every page is `ok`; 4 no key; 2 usage error or client error (401, 422); 5 malformed answer; 3 gave up after retries; 1 any other per-page error. When several apply, the first in the order 4, 2, 5, 3, 1 wins.

### What is sent to TypeSafe

The question, the page title (or its URL when the title is empty), and the text of each window with its `Section:` heading line, with link targets removed. File paths, line numbers, frontmatter and other pages are not sent. Only files inside a `.web_cache/` directory that start with web-sieve frontmatter are accepted, so no other local file can be sent. Do not put credentials or private data in the question. TypeSafe states that Jev is not trained on customer requests; zero data retention is offered only to enterprise customers.

### Deny list

Pages from some projects are never sent to Jev. `find_relevant_ranges` returns `status: "denied"` (with `reason`, `error.kind: "denied"` and the whole body in `unjudged`) for such a page and sends nothing for it; `search_cache(jev=True)` returns `status: "denied"` with the lexical results only.

- A cache directory is denied when a component of its path, as given or with symlinks resolved, equals a listed name or starts with `<name>-wt-` (a git worktree of that project), ignoring case. A name ending in `*` matches any component that starts with the part before the `*`. For a page path, both the given directory and the directory of the file a symlink points to are checked.
- The names are read from `privacy.deny_projects` in `~/.claude/jev_hooks/config.json`, the config file that other Jev hooks on the machine also read, so one list governs every Jev send. Entries of `privacy.deny_path_prefixes`, when present, deny every path under them. web-sieve.py itself names no project except the generic built-in names `clients`, `client_data` and `client-data`, which are always denied.
- When that file is missing, does not parse, or has no `privacy.deny_projects` list, only the built-in names are denied, and every result that sends pages to Jev (`find_relevant_ranges`, and `search_cache` with `jev`) carries a warning that starts `deny_list: not configured`.

A minimal config file:

```json
{"privacy": {"deny_projects": ["private_notes", "clients_*"], "deny_path_prefixes": ["~/Documents/private"]}}
```

### Behaviour

- The model is pinned to `jev-1.13.0`, because the threshold is calibrated against one model version. A different served model is kept and reported in `warnings`.
- 16 windows per request, 4 requests in flight, and the jev client's `default` retry policy (3 attempts within 20 s). web-sieve adds no retries of its own. After the first client error, malformed answer or give-up, no further request is sent in that call.
- Answers are cached in `jev_answers.sqlite` in the page's `.web_cache/` directory, keyed by model, prompt version, question, window (including its `Section:` line) and batch. The file holds hashes, probabilities, the served model, the prompt version and timestamps; it holds no page text, question or key. A repeated call sends no requests, and a rerun after a failure pays only for the batches that failed. Delete the file to clear it.
- Old answers are pruned automatically. A change of prompt version (2 since 2026-10-05, for the Section line) or window size means no old row can be read again. The first time a newer web-sieve opens a cache file, it deletes the rows of older prompt versions and vacuums the file, and records the version in the file (`PRAGMA user_version`) so later opens write nothing. No command is needed. A web-sieve process started before the upgrade can still read the file but can no longer write to it; it reports `answers cache ... could not be written` in `warnings` until it is restarted.
- Pages with more windows than `max_windows` (default 2,000; 1,000 until 2026-10-05) are refused before any request is sent. At 200-token windows the largest of 1,506 cached pages surveyed on 2026-10-05 had 1,482 windows (93 requests), and 3 had more than 1,000; the median page had 10 windows in one request. The largest request in that survey, with a 2,000-character question, was about 7,800 estimated tokens of state plus its longest question and 10,200 with all its questions, against budgets of 24,000 and 56,000.

### Threshold and calibration

The default threshold is **0.80**, for the default windows: 200 tokens, the Section line, 16 windows per request, link reduction on and no bridging. It was calibrated with `jev-1.13.0` on 9 pages and 27 questions (a local, a spread and an absent question per page): on 2026-10-04 at 400-token windows, and on 2026-10-05 for the current window size and Section line.

The 2026-10-05 run compared three configurations, with every question asked afresh (2.27 million input tokens, 360 requests, $0.095, 32 s). Each cell is line precision / line recall:

| Threshold | C2h: 200 tokens, Section line (default) | C2: 200 tokens, heading as a field | C1: 400 tokens, heading as a field |
|---|---|---|---|
| 0.70 | 0.432 / 0.970 | 0.431 / 0.970 | 0.316 / 0.970 |
| 0.75 | 0.478 / 0.970 | 0.455 / 0.970 | 0.342 / 0.970 |
| **0.80** | **0.523 / 0.965** | 0.504 / 0.965 | 0.353 / 0.970 |
| 0.85 | 0.510 / 0.851 | 0.533 / 0.925 | 0.417 / 0.945 |
| 0.90 | 0.650 / 0.711 | 0.664 / 0.816 | 0.537 / 0.876 |
| Median time per page | 0.42 s | 0.50 s | 0.29 s |

Haiku triage, the old recipe (outputs from 2026-10-04, not re-run): precision 0.788, recall 0.960, 9.5 s per page.

- No relevant page was missed at any of these thresholds. The only false alarm (an absent question answered with a range) was C2h at 0.70.
- **Window size.** At 0.80, 200-token windows raised precision from 0.353 to 0.523 at recall 0.965 against 0.970, and Read takes about two thirds of the lines (3.2% of body lines selected against 4.7%). They need more requests (141 against 84 for the 27 questions); the median time per page was still under 0.5 s.
- **Section line.** At 0.80 the Section line kept recall (0.965 with and without) and raised precision from 0.504 to 0.523. The rule set for this change was to keep the line when its recall at 0.80 is at least the recall without it minus 0.01 and its precision is not lower, so `SECTION_LINE` is on. The gain is small. The same requests sent on 2026-10-04 and 2026-10-05 got a different probability for about 44% of windows (95th percentile change 0.04, largest 0.26), and C1's precision at 0.80 moved from 0.362 to 0.353 between the two days. A gain of 0.019 is of the same size as that variation, so the measured result is that the line costs nothing, not that it clearly helps.
- **Threshold.** 0.80 is the highest threshold at which C2h keeps line recall of at least 0.90 (0.85 falls to 0.851, mostly on spread questions), and at 0.80 all three adoption conditions of the spec's decision rule (section 15.5) hold: recall at least the Haiku recall minus 0.02 (0.965 against 0.940), no page misses, and a median time per page under 2 s. So the default stays 0.80. Ranges are still looser than Haiku's (precision 0.52 against 0.79), so expect to read about 1.5 times the lines Haiku would have chosen. Every window's probability is in the output, so a caller can apply another threshold without a new request.
- The script's own decision block, which applies section 15.5 to the three configurations together, prints `adopt: false`. Its step 1 takes each configuration's highest threshold with recall of at least 0.90, which for C2 is 0.85 (recall 0.925). Step 2 then picks C2 at 0.85 over C2h at 0.80, because their precisions (0.533 and 0.523) are within 0.02 of each other, both need 141 requests, and the tie goes to the higher precision. C2 at 0.85 fails the Haiku recall condition (0.925 against 0.940), and the rule does not fall back to a lower threshold. The rule's step order is an open item in the spec (section 19).

The 2026-10-04 calibration, at 400-token windows with the heading as a field, ran twice: the second run replaced two pages that came from projects the deny list now refuses with a GitHub repository page whose answer sits inside a 16,000-character line and a fund page. At 0.80 no relevant page was missed and no absent question was answered with a range, in either run, and the decision rule passed in both, so `find_relevant_ranges` replaced the Haiku step. In run 2, 0.85 lost gold windows that scored 0.81 and 0.82 and met the Haiku recall condition by 0.0001, so the default moved from 0.85 to 0.80.

| Method | Line recall (run 1 / run 2) | Line precision (run 1 / run 2) | Median time per page |
|---|---|---|---|
| Jev at 0.80 | 0.971 / 0.970 | 0.395 / 0.362 | 0.32 / 0.26 s |
| Jev at 0.85 (default after run 1) | 0.967 / 0.940 | 0.426 / 0.420 | the same (the threshold is applied after the answers) |
| Jev at 0.90 | 0.923 / 0.891 | 0.530 / 0.534 | the same |
| Jev, 200-token windows at 0.80 | 0.962 / 0.960 | 0.535 / 0.503 | 0.32 s (run 2) |
| Haiku triage (old recipe) | 0.909 / 0.960 | 0.864 / 0.788 | 9.5 s |

`calibration/calibrate.py` measures line-level precision and recall against labelled pages and applies the decision rule in section 15.5 of the spec. Its inputs and outputs live in `calibration/data/`, which is gitignored because the page list and the labels name private project directories. To run it:

1. Write `calibration/data/pages.json` (`[{"n", "page", "title", "why"}]`, page paths relative to `~/Projects`). `uv run --script calibration/calibrate.py --check` then prints each page's sha256 and body line range for the labellers.
2. Write `calibration/data/labels.jsonl`: one line per page from a Sonnet labeller, with a local, a spread and an absent question and their gold line ranges (format in the script's docstring). `--check` validates it.
3. Write `calibration/data/haiku_baseline.jsonl`: the full output of one Haiku agent per question, given the old step-3 instruction. This is the baseline the tool must match.
4. `uv run --script calibration/calibrate.py --one` sends one question with the default configuration as a smoke test. Then `gtimeout 1200 uv run --script calibration/calibrate.py --configs all` runs the seven configurations, C1 to C6 and C2h (about $0.25); `--configs C1,C2,C2h` runs only the ones named, and `--configs default` the one that matches the constants in `web-sieve.py`. The script writes `calibration/data/results/YYYY-MM-DD.json` with every window's probability, the metrics and the decision, and stops at the first page that is not `ok`.

### Tests

```bash
uv run --frozen pytest -q
```

`--frozen` installs exactly what `uv.lock` records and never rewrites it. Without it, uv rewrites `uv.lock` whenever an exclude-newer cutoff set in the environment (`UV_EXCLUDE_NEWER`) has moved, even when no dependency changed.

The tests use the real jev client and a local fake Jev server (`tests/fake_jev.py`, reached through `JEV_API_BASE`), and a local fake Jina Reader (`tests/fake_jina.py`, reached by setting `JINA_BASE`) that can serve normal, challenge and empty pages, 429 with `Retry-After`, 503, a body cut short, invalid UTF-8 and slow responses. No test uses the network. They fail, rather than skip, when the jev client cannot be loaded.

## Cache format

Pages are cached as `.web_cache/{sha256[:12]}.md` with YAML frontmatter:

```yaml
---
url: https://example.com/article
title: Article Title
fetched: 2026-02-15T10:30:00+00:00
hash: a1b2c3d4e5f6
status: ok
bytes: 18234
---
[clean markdown content]
```

`status` (`ok` or `thin`) and `bytes` (UTF-8 size of the text after the frontmatter) are written on pages fetched since 2026-10-04; a page without `status` is treated as `ok`.

Cache is **project-scoped** and, by default, **permanent** — pages persist across sessions and can be re-queried with different questions (see Freshness to refetch).

Other files in the cache directory:

| File | What it holds |
|---|---|
| `manifest.md` | Table of every page and blocked URL: title, URL, file, fetched date, status, size in KB. Rebuilt on every fetch. |
| `.manifest.lock` | Empty lock file. A rebuild of both manifests holds an exclusive lock on it (fcntl on POSIX), so two processes fetching into one cache cannot leave a stale `manifest.json`. On Windows there is no fcntl and rebuilds are not locked; `search_cache` still lists the directory when `manifest.json` does not match it. |
| `manifest.json` | The same rows as `{file, url, title, fetched, status, bytes, lines}` (blocked rows add `reason`); read by `search_cache`. |
| `<hash>.blocked.json` | Sidecar for a blocked URL (`url`, `status`, `reason`, `attempts`, `at`). |
| `_quarantine/` | Pages moved by `audit --apply`, and `quarantine.jsonl`, one line per move. |
| `jev_answers.sqlite` | Jev answer cache (hashes and probabilities only). Rows of older prompt versions are deleted, and the file vacuumed, the first time a newer web-sieve opens it. |
| `search_index.sqlite` | `search_cache` index: window line ranges and term counts per page. Rebuilt in full when its version or the window size changes. |

Files are written through a temporary `.tmp-*.part` file and renamed into place. A process killed while writing leaves its temporary file behind; the next fetch into that cache removes any older than an hour.

## Performance

| Scenario | Pages | Cached | Used | Compression |
|---|---|---|---|---|
| Narrow query | 2 | 24K chars | 1.2K chars | **95%** |
| Comparison | 3 | 29K chars | 8.5K chars | **70%** |
| Broad research | 5 | 83K chars | 10.5K chars | **87%** |

Relevance with Jev (`jev-1.13.0`, measured 2026-10-03): a 73,000-character docs page in 53 windows went out as 4 parallel requests, 25,030 input tokens ($0.00105), 0.5 s (design probe). An 11,600-character page in 12 windows took 1 request, 5,334 input tokens ($0.00022), 0.5 s (this implementation). A repeated call is answered from the answers cache with no requests. In the 2026-10-04 calibration (400-token windows) Jev took 0.32 s per page and the Haiku triage it replaces 9.5 s; in the 2026-10-05 calibration at the current 200-token windows the median was 0.42 s per page.

## What gets stripped

web-sieve sends no CSS selectors (no `X-Remove-Selector` or `X-Target-Selector`), so what is removed is what Jina Reader's own extraction removes. Every request carries:

- `Accept: text/markdown`
- `X-Engine: browser`: Jina renders the page in headless Chrome, so client-side JavaScript runs before extraction.
- `X-Retain-Images: all`: image markdown (`![alt](url)`) is kept, so figure placement, chart captions and source lines stay next to their images in the cached page.
- `Authorization: Bearer <JINA_API_KEY>` when the key is set.

Jina's documentation (jina.ai/reader, read 2026-10-04) says its default extraction strips boilerplate such as navigation, headers, footers and ads and converts the main content to markdown. Links are kept as `[text](url)`. The cached file keeps links and images as Jina returned them; `find_relevant_ranges` and `search_cache` reduce `![alt](url)` to `[image: alt]` and `[text](url)` to `text` only in the window text they judge or index, not in the file.

## Limitations

- **Cloudflare-protected sites** (SSRN, Medium, some publishers) often return challenge pages even after the alternate request. They come back as `status: "blocked"`; fetch them with Firecrawl.
- **Nav-heavy sites** may still have navigation in the cached page: Jina's extraction does not remove all of it, and web-sieve sends no selectors of its own.
- **Restart required** after editing `web-sieve.py` — restart Claude Code to apply changes.
- **`cache_dir` must be absolute** — the MCP server's working directory may differ from your project.

## Docs

See [docs/web-pipeline-explained.pdf](docs/web-pipeline-explained.pdf) for a walkthrough with diagrams. It describes the first version, which removed page parts with CSS selectors and triaged pages with Haiku agents; the design of the current version is in [docs/jev-relevance-spec.md](docs/jev-relevance-spec.md) and [docs/effectiveness-spec.md](docs/effectiveness-spec.md).

## License

MIT
