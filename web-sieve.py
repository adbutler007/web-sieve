#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp[cli]"]
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

# CSS selectors for common junk to strip before extraction
REMOVE_SELECTORS = ", ".join([
    "nav",
    "footer",
    "header",
    "[role='banner']",
    "[role='navigation']",
    "[role='contentinfo']",
    "[class*='cookie']",
    "[class*='consent']",
    "[class*='newsletter']",
    "[class*='subscribe']",
    "[class*='signup']",
    "[class*='sign-up']",
    "[class*='popup']",
    "[class*='modal']",
    "[class*='overlay']",
    "[class*='sidebar']",
    "[class*='widget']",
    "[class*='advert']",
    "[class*='sponsor']",
    "[class*='promo']",
    "[class*='related-post']",
    "[class*='share']",
    "[class*='social']",
    "[class*='comment']",
    "[id*='cookie']",
    "[id*='consent']",
    "[id*='newsletter']",
    "[id*='popup']",
    "[id*='modal']",
    "[id*='sidebar']",
    "[id*='ad-']",
    "[id*='ads']",
])


def _headers():
    h = {
        "Accept": "text/markdown",
        "User-Agent": "web-sieve/1.0",
        "X-Remove-Selector": REMOVE_SELECTORS,
        "X-Retain-Images": "none",
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

    # Fetch from Jina Reader
    req = urllib.request.Request(f"https://r.jina.ai/{url}", headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
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
    return json.dumps(_fetch_one(url, cache_dir))


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


if __name__ == "__main__":
    mcp.run(transport="stdio")
