#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp[cli]<2"]
# ///
"""web-sieve: MCP server that fetches web pages as clean markdown via Jina Reader API, with project-level caching."""

import hashlib
import json
import os
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("web-sieve")

API_KEY = os.environ.get("JINA_API_KEY", "")

def _headers() -> dict[str, str]:
    # Use browser rendering with the standard markdown formatter.
    #
    # For the pages we care about, this produced the best fidelity:
    # - fixed truncation on long pages where the plain path cut off early
    # - preserved inline chart captions, source lines, and disclaimer text
    # ReaderLM-v2 was more semantic and tended to drop some exact source /
    # disclaimer language that we want cached verbatim.
    h = {
        "Accept": "text/markdown",
        "User-Agent": "web-sieve/1.0",
        "X-Engine": "browser",
        # Preserve image markdown so figure placement survives in the cached
        # page. This helps when auditing inline chart captions, sources, and
        # surrounding disclaimer language.
        "X-Retain-Images": "all",
    }
    if API_KEY:
        h["Authorization"] = f"Bearer {API_KEY}"
    return h


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:12]


def _extract_title(content: str) -> str:
    for line in content.split("\n"):
        if line.startswith("Title: "):
            return line[7:].strip()
    return "Unknown"


def _update_manifest(cache_dir: str) -> None:
    """Rebuild manifest.md from all cached files."""
    manifest_path = os.path.join(cache_dir, "manifest.md")
    entries = []
    for fname in sorted(os.listdir(cache_dir)):
        if not fname.endswith(".md") or fname == "manifest.md":
            continue
        fpath = os.path.join(cache_dir, fname)
        meta = {"file": fname}
        with open(fpath) as f:
            for line in f:
                if line.strip() == "---" and meta.get("url"):
                    break
                if line.startswith("url: "):
                    meta["url"] = line[5:].strip()
                elif line.startswith("title: "):
                    meta["title"] = line[7:].strip()
                elif line.startswith("fetched: "):
                    meta["fetched"] = line[9:].strip()
        entries.append(meta)
    with open(manifest_path, "w") as f:
        f.write("# Web Cache Manifest\n\n")
        f.write("Cached pages available for re-querying with haiku agents.\n\n")
        f.write(f"| # | Title | URL | File | Fetched |\n")
        f.write(f"|---|---|---|---|---|\n")
        for i, e in enumerate(entries, 1):
            title = e.get("title", "Unknown")
            url = e.get("url", "")
            fname = e.get("file", "")
            fetched = e.get("fetched", "")[:10]
            f.write(f"| {i} | {title} | {url} | {fname} | {fetched} |\n")
        f.write(f"\n**Total: {len(entries)} pages cached.**\n")


def _fetch_one(url: str, cache_dir: str) -> dict:
    """Fetch a single URL, cache it, return metadata dict."""
    os.makedirs(cache_dir, exist_ok=True)
    h = _url_hash(url)
    cache_file = os.path.join(cache_dir, f"{h}.md")

    # Return cached version if exists
    if os.path.exists(cache_file):
        with open(cache_file) as f:
            content = f.read()
        title = "Unknown"
        for line in content.split("\n"):
            if line.startswith("title: "):
                title = line[7:].strip()
                break
        body_start = content.find("\n---\n")
        body = content[body_start + 5:] if body_start != -1 else content
        return {
            "cached": True,
            "path": os.path.abspath(cache_file),
            "url": url,
            "title": title,
            "lines": body.count("\n") + 1,
            "chars": len(body),
        }

    try:
        req = urllib.request.Request(f"https://r.jina.ai/{url}", headers=_headers())
        with urllib.request.urlopen(req, timeout=180) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        return {"url": url, "error": f"{e.code}: {e.reason}", "detail": err_body}

    title = _extract_title(body)
    now = datetime.now(timezone.utc).isoformat()

    with open(cache_file, "w") as f:
        f.write(f"---\nurl: {url}\ntitle: {title}\nfetched: {now}\nhash: {h}\n---\n")
        f.write(body)

    return {
        "cached": False,
        "path": os.path.abspath(cache_file),
        "url": url,
        "title": title,
        "lines": body.count("\n") + 1,
        "chars": len(body),
    }


@mcp.tool()
def read_url(url: str, cache_dir: str = ".web_cache") -> str:
    """Fetch a URL via Jina Reader, cache the markdown to disk, and return metadata.

    Returns JSON with: path, title, lines, chars, cached (bool).
    Content is NOT returned — use the path with Read tool or deploy agents against it.

    Args:
        url: The URL to fetch.
        cache_dir: Directory to cache markdown files. Use an absolute path to the
                   project's .web_cache/ directory.
    """
    result = _fetch_one(url, cache_dir)
    if "error" not in result:
        _update_manifest(cache_dir)
    return json.dumps(result)


@mcp.tool()
def batch_read_urls(urls: list[str], cache_dir: str = ".web_cache") -> str:
    """Fetch multiple URLs in parallel via Jina Reader, cache all to disk.

    Returns JSON array of metadata objects (path, title, lines, chars, cached).
    Content is NOT returned — use the paths with Read tool or deploy agents.
    Fetches run concurrently (up to 8 threads). Cached pages return instantly.

    Args:
        urls: List of URLs to fetch.
        cache_dir: Directory to cache markdown files. Use an absolute path to the
                   project's .web_cache/ directory.
    """
    results = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_fetch_one, url, cache_dir): url for url in urls}
        for future in as_completed(futures):
            results.append(future.result())
    # Return in original URL order
    order = {url: i for i, url in enumerate(urls)}
    results.sort(key=lambda r: order.get(r.get("url", ""), len(urls)))
    _update_manifest(cache_dir)
    return json.dumps(results)


@mcp.tool()
def list_cache(cache_dir: str = ".web_cache") -> str:
    """List all cached web pages with their metadata.

    Args:
        cache_dir: Directory containing cached markdown files.
    """
    if not os.path.isdir(cache_dir):
        return json.dumps([])

    entries = []
    for fname in sorted(os.listdir(cache_dir)):
        if not fname.endswith(".md"):
            continue
        fpath = os.path.join(cache_dir, fname)
        meta = {"path": os.path.abspath(fpath), "file": fname}
        with open(fpath) as f:
            for line in f:
                if line.strip() == "---" and meta.get("url"):
                    break
                if line.startswith("url: "):
                    meta["url"] = line[5:].strip()
                elif line.startswith("title: "):
                    meta["title"] = line[7:].strip()
                elif line.startswith("fetched: "):
                    meta["fetched"] = line[9:].strip()
        entries.append(meta)

    return json.dumps(entries)


def _cli():
    """CLI entrypoint: web-sieve read|batch|list — same caching as the MCP server."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="web-sieve",
        description="Fetch web pages as clean markdown via Jina Reader API, with project-level caching.",
    )
    sub = parser.add_subparsers(dest="command")

    # read <url> [--cache-dir]
    p_read = sub.add_parser("read", help="Fetch a single URL, cache to disk, print metadata JSON")
    p_read.add_argument("url", help="URL to fetch")
    p_read.add_argument("--cache-dir", default=".web_cache", help="Cache directory (default: .web_cache)")
    p_read.add_argument("--print", "-p", action="store_true", dest="print_content",
                        help="Print the cached markdown content instead of metadata")

    # batch <url> [<url> ...] [--cache-dir]
    p_batch = sub.add_parser("batch", help="Fetch multiple URLs in parallel, cache to disk")
    p_batch.add_argument("urls", nargs="+", help="URLs to fetch")
    p_batch.add_argument("--cache-dir", default=".web_cache", help="Cache directory (default: .web_cache)")

    # list [--cache-dir]
    p_list = sub.add_parser("list", help="List all cached pages with metadata")
    p_list.add_argument("--cache-dir", default=".web_cache", help="Cache directory (default: .web_cache)")

    args = parser.parse_args()

    if args.command == "read":
        result = _fetch_one(args.url, args.cache_dir)
        if "error" not in result:
            _update_manifest(args.cache_dir)
        if args.print_content and "path" in result:
            with open(result["path"]) as f:
                content = f.read()
            # Skip the frontmatter
            body_start = content.find("\n---\n")
            print(content[body_start + 5:] if body_start != -1 else content)
        else:
            print(json.dumps(result, indent=2))

    elif args.command == "batch":
        results = []
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(_fetch_one, url, args.cache_dir): url for url in args.urls}
            for future in as_completed(futures):
                results.append(future.result())
        order = {url: i for i, url in enumerate(args.urls)}
        results.sort(key=lambda r: order.get(r.get("url", ""), len(args.urls)))
        _update_manifest(args.cache_dir)
        print(json.dumps(results, indent=2))

    elif args.command == "list":
        print(list_cache(args.cache_dir))

    else:
        parser.print_help()


if __name__ == "__main__":
    import sys
    # If run with CLI arguments, use CLI mode; otherwise start MCP server
    if len(sys.argv) > 1 and sys.argv[1] in ("read", "batch", "list", "--help", "-h"):
        _cli()
    else:
        mcp.run(transport="stdio")
