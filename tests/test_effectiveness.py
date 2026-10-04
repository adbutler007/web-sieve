"""Fetch quality gate, retry and alternate strategy, freshness, manifests,
search_cache, the Jev deny list, the audit command, and CLI/MCP parity
(docs/effectiveness-spec.md, section 10).

Hermetic: Jina Reader is tests/fake_jina.py (web-sieve's JINA_BASE points at
it), Jev is tests/fake_jev.py (JEV_API_BASE), the retry sleep and the clock
are replaced, and the deny-list config file is a path inside tmp_path.
No test reaches the network or the real keychain.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import socket
import subprocess
import sys
import time
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fake_jina as fj  # noqa: E402
from fake_jev import FakeJev  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
KEY = "test-key-9d2e41"
FAST = {"attempts": 2, "first_s": 0.01, "cap_s": 0.01, "deadline_s": 2.0}
URL = "https://example.com/guide"
URL2 = "https://example.com/other"
DAY = 86400.0
TEST_DENY = {"deny_projects": ["private_proj", "second_proj", "journal*"], "deny_path_prefixes": []}


def load_web_sieve():
    loader = SourceFileLoader("web_sieve_effectiveness", str(ROOT / "web-sieve.py"))
    module = module_from_spec(spec_from_loader("web_sieve_effectiveness", loader))
    loader.exec_module(module)
    return module


ws = load_web_sieve()


# ── Fixtures and helpers ──────────────────────────────────────────


@pytest.fixture(scope="session")
def jev():
    try:
        return ws._load_jev()
    except ws._SourceError as e:
        pytest.fail(f"the jev client could not be loaded, so the Jev tests cannot run: {e.message}")


@pytest.fixture
def jina():
    fake = fj.FakeJina()
    yield fake
    fake.close()


@pytest.fixture
def jevserver():
    fake = FakeJev()
    yield fake
    fake.close()


@pytest.fixture
def fakebin(tmp_path):
    """A PATH directory whose `security` and `secret-tool` find no key."""
    folder = tmp_path / "fakebin"
    folder.mkdir()
    for name in ("security", "secret-tool"):
        stub = folder / name
        stub.write_text("#!/bin/sh\nexit 44\n")
        stub.chmod(0o755)
    return folder


@pytest.fixture
def sleeps(monkeypatch):
    """Every retry wait, recorded instead of slept."""
    waits = []
    monkeypatch.setattr(ws, "_sleep", waits.append)
    return waits


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path, jev, jina, jevserver, fakebin, sleeps):
    monkeypatch.setattr(ws, "JINA_BASE", jina.url)
    monkeypatch.setattr(ws, "API_KEY", "")
    config = tmp_path / "deny_config.json"
    config.write_text(json.dumps({"version": 1, "privacy": TEST_DENY}))
    monkeypatch.setattr(ws, "DENY_CONFIG", str(config))
    monkeypatch.setenv("JEV_API_KEY", KEY)
    monkeypatch.setenv("JEV_API_BASE", jevserver.url)
    monkeypatch.setenv("PATH", f"{fakebin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("WEB_SIEVE_JEV", raising=False)
    monkeypatch.setattr(ws, "JEV_POLICY", FAST)
    monkeypatch.setattr(ws, "JEV_TIMEOUT_S", 0.3)


class Clock:
    t = 0.0


@pytest.fixture
def clock(monkeypatch):
    """web-sieve's clock, moved by the test."""
    c = Clock()
    c.t = time.time()
    monkeypatch.setattr(ws, "_now", lambda: c.t)
    return c


@pytest.fixture
def cache(tmp_path):
    return str(tmp_path / ".web_cache")


def page_path(cache, url=URL):
    return os.path.join(cache, f"{ws._url_hash(url)}.md")


def sidecar_path(cache, url=URL):
    return os.path.join(cache, f"{ws._url_hash(url)}.blocked.json")


def words(tokens, seed=0):
    """Plain ASCII text of about `tokens` estimated tokens, on one line."""
    vocab = ["alpha", "bravo", "delta", "gamma", "omega", "sigma", "kappa", "theta"]
    out, n = [], 0
    while n < tokens * ws.CHARS_PER_TOKEN:
        w = vocab[(seed + len(out)) % len(vocab)]
        out.append(w)
        n += len(w) + 1
    return " ".join(out)


def write_page(folder, name, body, title="Notes", url=None, fetched="2026-10-01T00:00:00+00:00", extra=""):
    """A cached page in web-sieve's format (frontmatter, Jina preamble, body); its path."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    url = url or f"https://example.com/{name}"
    text = (f"---\nurl: {url}\ntitle: {title}\nfetched: {fetched}\nhash: {ws._url_hash(url)}\n{extra}---\n"
            + fj.jina(url, title, body))
    path = folder / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def sections(texts, tokens=100):
    """One section per text: a heading and a paragraph of about `tokens`
    tokens that starts with the text. Each section is one 400-token window."""
    return "".join(f"## Section {k}\n\n{t} {words(tokens, k)}\n\n" for k, t in enumerate(texts, 1))


def call_mcp(name, arguments):
    """Call a tool through FastMCP (argument validation included); the parsed JSON text."""
    result = asyncio.run(ws.mcp.call_tool(name, arguments))
    blocks = result[0] if isinstance(result, tuple) else result
    return json.loads(blocks[0].text)


def cli(monkeypatch, capsys, *args):
    """Run `web-sieve ...` in this process; (exit code, parsed stdout)."""
    monkeypatch.setattr(sys, "argv", ["web-sieve", *args])
    code = 0
    try:
        ws._cli()
    except SystemExit as e:
        code = e.code
    return code, json.loads(capsys.readouterr().out)


def snapshot(folder):
    """{relative path: bytes} of every file under folder."""
    root = Path(folder)
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


# ── Quality gate: _classify_body ──────────────────────────────────


@pytest.mark.parametrize("where", ["title", "body"])
@pytest.mark.parametrize("phrase", ws.CHALLENGE_PHRASES)
def test_classify_each_challenge_phrase_ignoring_case(phrase, where):
    """A challenge page cached as content reads as a clean negative for
    ever; every listed phrase must be caught, in the title or near the top
    of the body, whatever its case."""
    # A body phrase counts on a body under 3,000 characters; "captcha" only on a thin one (under 400).
    filler = fj.FILLER if phrase == "captcha" else fj.FILLER * 5
    for variant in (phrase, phrase.upper(), phrase.lower(), phrase.swapcase()):
        if where == "title":
            text = fj.jina(URL, f"{variant} | Example", fj.FILLER * 5)
        else:
            text = fj.jina(URL, "Example", f"An opening line.\n\n{variant}\n\n" + filler)
        status, reason = ws._classify_body(URL, text)
        assert status == "challenge" and repr(phrase) in reason and where in reason


CONTACT_FORM = ("Contact Example Co\n===============\n\n### **Main Office**\n\nPhone: (555) 010-0000\n\n"
                "100 Example Street\n\n Suite 1\n\n Springfield, ST 00000\n\n### **Second Office**\n\n"
                "200 Sample Avenue\n\n Suite 2\n\n Shelbyville, ST 00000\n\nHow can we be of help?\n"
                "----------------------\n\n*   Email  This field is for validation purposes and should be left "
                "unchanged. \n*   I'd like to learn more about...* \n\n    *   - [x] Financial Planning \n"
                "    *   - [x] Practice Transitions \n    *   - [x] Other \n\n*   Comments / Questions?  \n"
                "*   Name* First Last   \n*   Email*  \n*   Phone  \n\n*   - [x] Yes! Add me to the list. \n\n"
                "*   CAPTCHA    \n\nNotifications\n")


def test_classify_contact_form_with_a_captcha_field_is_ok():
    """Regression, 2026-10-04: a 750-character contact page was quarantined
    because its form has a CAPTCHA field. A form widget is not a bot wall:
    "captcha" in the body counts only on a thin body, and the page is content."""
    assert ws.THIN_CHARS < len(CONTACT_FORM) < ws.CHALLENGE_BODY_MAX_CHARS  # short body, not thin
    assert ws._classify_body(URL, fj.jina(URL, "Contact Example Co", CONTACT_FORM)) == ("ok", "")
    wall = fj.jina(URL, "example.com", "Please complete the CAPTCHA below to continue.\n")
    assert ws._classify_body(URL, wall)[0] == "challenge"


def test_classify_body_phrase_counts_only_on_a_short_body():
    """A long article that mentions "Access denied" in its first paragraph is
    content; the same phrase on a short body is a challenge; in the title it
    is a challenge whatever the body's length."""
    head = "An opening line about Access denied errors in web servers.\n\n"
    short = head + "x" * (ws.CHALLENGE_BODY_MAX_CHARS - len(head) - 1)
    at_limit = head + "x" * (ws.CHALLENGE_BODY_MAX_CHARS - len(head))
    assert ws._classify_body(URL, fj.jina(URL, "Notes", short))[0] == "challenge"
    assert ws._classify_body(URL, fj.jina(URL, "Notes", at_limit)) == ("ok", "")
    assert ws._classify_body(URL, fj.jina(URL, "Notes", head + fj.FILLER * 30)) == ("ok", "")
    status, reason = ws._classify_body(URL, fj.jina(URL, "Access denied | Example", fj.FILLER * 30))
    assert status == "challenge" and "in the title" in reason


def test_classify_empty_body():
    """An empty response holds nothing to judge; cached, it would also read
    as a page that does not answer the question."""
    assert ws._classify_body(URL, fj.EMPTY)[0] == "empty"
    assert ws._classify_body(URL, "")[0] == "empty"
    assert ws._classify_body(URL, fj.jina(URL, "T", "x" * 19))[0] == "empty"
    assert ws._classify_body(URL, fj.jina(URL, "T", "x" * 20))[0] == "thin"


def test_classify_thin_at_399_and_ok_at_400():
    """The thin limit is exact, and surrounding whitespace is not counted,
    so a page is not called thin or ok by accident of its blank lines."""
    s399, r399 = ws._classify_body(URL, fj.jina(URL, "T", "\n\n" + "a" * 399 + "\n\n  \n"))
    s400, r400 = ws._classify_body(URL, fj.jina(URL, "T", "a" * 400))
    assert (s399, s400) == ("thin", "ok") and "399 characters" in r399 and r400 == ""


def test_classify_small_raw_file_is_thin_not_challenge():
    """Small legitimate files exist (a package.json fetched raw has no Jina
    preamble); they are cached as thin with a warning, never refused."""
    raw = '{\n  "name": "widget",\n  "version": "1.2.3",\n  "dependencies": {"left-pad": "^1.3.0"}\n}\n'
    assert ws._classify_body("https://raw.githubusercontent.com/x/y/main/package.json", raw)[0] == "thin"


def test_classify_strips_preamble():
    """The size rule must measure the page, not Jina's header lines: a
    response that is only a preamble is empty however long its URL and
    warnings are. Jina's CAPTCHA warning also appears on real pages that hold
    a form widget, so it is not challenge evidence; a phrase past the first
    2,000 body characters is page text."""
    long_url = "https://example.com/" + "a" * 300
    bare = ("Title: A long page\n\nURL Source: " + long_url + "\n\nPublished Time: 2026-03-05T08:00:40-08:00\n\n"
            "Warning: Target URL returned error 404: Not Found\n"
            "Warning: This page maybe requiring CAPTCHA, please make sure you are authorized to access this page.\n\n"
            "Markdown Content:\n")
    assert ws._classify_body(long_url, bare)[0] == "empty"
    title, warnings, body = ws._jina_parts(bare + "Real text.\n")
    assert title == "A long page" and body == "Real text.\n" and len(warnings) == 2
    form_page = fj.jina(URL, "Carry guide", fj.FILLER * 3, ("This page maybe requiring CAPTCHA, please make sure "
                                                            "you are authorized to access this page.",))
    assert ws._classify_body(URL, form_page) == ("ok", "")
    late = fj.jina(URL, "Notes", "x" * 2000 + " captcha " + fj.FILLER)
    assert ws._classify_body(URL, late)[0] == "ok"
    no_preamble = "# Plain notes\n\n" + fj.FILLER * 3
    assert ws._jina_parts(no_preamble)[2] == no_preamble and ws._classify_body(URL, no_preamble)[0] == "ok"


# ── Fetch: retry, alternate strategy, sidecars ────────────────────


def test_challenge_triggers_one_alternate_request_with_the_documented_headers(cache, jina, monkeypatch):
    """A challenge is never cached as content. One alternate request is sent
    with the headers the Jina docs list for blocked sites (X-No-Cache, and
    X-Proxy: auto because a key is set) on top of the normal ones; when it is
    still a challenge, the URL is recorded in a sidecar, no page is written,
    and the caller is sent to Firecrawl."""
    monkeypatch.setattr(ws, "API_KEY", "jina-test-key")
    jina.default = {"body": fj.CHALLENGE}
    r = json.loads(ws.read_url(URL, cache))
    assert r["status"] == "blocked" and r["fallback"] == "firecrawl" and r["attempts"] == 2
    first, second = jina.requests
    assert "x-no-cache" not in first["headers"] and "x-proxy" not in first["headers"]
    assert second["headers"]["x-no-cache"] == "true" and second["headers"]["x-proxy"] == "auto"
    for req in (first, second):
        assert req["url"] == URL and req["headers"]["x-engine"] == "browser"
        assert req["headers"]["authorization"] == "Bearer jina-test-key"
    assert not os.path.exists(page_path(cache))
    side = json.loads(Path(sidecar_path(cache)).read_text())
    assert set(side) == {"url", "status", "reason", "attempts", "at"}
    assert side["url"] == URL and side["status"] == "blocked" and side["attempts"] == 2
    assert "'Just a moment'" in side["reason"] and r["sidecar"] == sidecar_path(cache)


def test_alternate_request_without_a_key_sends_no_proxy_header(cache, jina):
    """Jina's README says its proxy needs an API key, so without one the
    alternate request carries X-No-Cache only."""
    jina.default = {"body": fj.CHALLENGE}
    assert ws._fetch(URL, cache)["status"] == "blocked"
    alternate = jina.requests[1]["headers"]
    assert alternate["x-no-cache"] == "true" and "x-proxy" not in alternate and "authorization" not in alternate


def test_alternate_request_that_succeeds_is_cached_with_a_warning(cache, jina):
    """When the alternate request gets the real page, that page is cached
    and the caller is told it took a second request."""
    jina.script = [{"body": fj.CHALLENGE}]
    r = ws._fetch(URL, cache)
    assert r["status"] == "ok" and r["attempts"] == 2 and not os.path.exists(sidecar_path(cache))
    assert any("alternate request" in w and "X-No-Cache" in w for w in r["warnings"])
    assert "# Widget guide" in Path(r["path"]).read_text()


def test_empty_response_also_gets_the_alternate_request_then_is_blocked(cache, jina):
    """An empty response may be Jina's cached copy of an earlier block; it
    gets the same one alternate request, and is never cached as a page."""
    jina.default = {"body": fj.EMPTY}
    r = ws._fetch(URL, cache)
    assert r["status"] == "blocked" and len(jina.requests) == 2 and r["reason"].startswith("empty:")
    assert jina.requests[1]["headers"]["x-no-cache"] == "true" and not os.path.exists(page_path(cache))


def test_sidecar_is_served_within_24_hours_without_a_request(cache, jina, clock):
    """A site that blocked Jina a moment ago will block it again; repeating
    the two requests on every call wastes time and quota."""
    jina.default = {"body": fj.CHALLENGE}
    ws._fetch(URL, cache)
    clock.t += DAY - 60
    r = ws._fetch(URL, cache)
    assert len(jina.requests) == 2 and r["status"] == "blocked" and r["attempts"] == 0
    assert r["fallback"] == "firecrawl" and "'Just a moment'" in r["reason"]


def test_sidecar_expires_after_24_hours_and_the_url_is_fetched_again(cache, jina, clock):
    """A block is not permanent: after 24 hours the URL is fetched again, a
    success replaces the sidecar with a page, and a new block replaces it
    with a new time."""
    jina.default = {"body": fj.CHALLENGE}
    ws._fetch(URL, cache)
    first_at = json.loads(Path(sidecar_path(cache)).read_text())["at"]
    clock.t += DAY + 1
    assert ws._fetch(URL, cache)["status"] == "blocked" and len(jina.requests) == 4
    assert json.loads(Path(sidecar_path(cache)).read_text())["at"] != first_at
    clock.t += DAY + 1
    jina.default = {}
    r = ws._fetch(URL, cache)
    assert r["status"] == "ok" and len(jina.requests) == 5
    assert not os.path.exists(sidecar_path(cache)) and os.path.exists(page_path(cache))


def test_429_honours_retry_after_and_succeeds_on_the_second_attempt(cache, jina, sleeps, clock):
    """Jina's rate limit says when to come back; the retry waits that long,
    at most 30 s, in seconds or as an HTTP date."""
    jina.script = [{"status": 429, "headers": {"Retry-After": "7"}}]
    r = ws._fetch(URL, cache)
    assert r["status"] == "ok" and r["attempts"] == 2 and sleeps == [7.0]
    jina.script = [{"status": 429, "headers": {"Retry-After": "120"}}]
    assert ws._fetch(URL2, cache)["status"] == "ok" and sleeps[-1] == 30.0
    from email.utils import formatdate
    jina.script = [{"status": 429, "headers": {"Retry-After": formatdate(clock.t + 12, usegmt=True)}}]
    assert ws._fetch("https://example.com/third", cache)["status"] == "ok" and 10.0 <= sleeps[-1] <= 12.0


def test_503_then_200_is_ok_after_the_default_backoff(cache, jina, sleeps):
    """A transient server error costs one 2-second wait, not the result."""
    jina.script = [{"status": 503}]
    r = ws._fetch(URL, cache)
    assert r["status"] == "ok" and r["attempts"] == 2 and sleeps == [2.0] and len(jina.requests) == 2


def test_errors_that_persist_or_are_not_transient_are_reported_with_the_code(cache, jina, sleeps):
    """Two 503s end in an error with both attempts named; a 404 is not
    retried. Neither writes a page or a sidecar."""
    jina.script = [{"status": 503}, {"status": 503}]
    r = ws._fetch(URL, cache)
    assert r["status"] == "error" and r["http_status"] == 503 and r["attempts"] == 2
    assert "attempt 1: HTTP 503" in r["reason"] and "attempt 2: HTTP 503" in r["reason"]
    jina.script = [{"status": 404}]
    r = ws._fetch(URL2, cache)
    assert r["status"] == "error" and r["http_status"] == 404 and r["attempts"] == 1 and sleeps == [2.0]
    assert "fake error" in r["detail"] and os.listdir(cache) == []


def test_incomplete_read_is_retried_once(cache, jina, sleeps):
    """A response cut short is a transport failure: retried once, and when
    the retry is also cut short, an error naming both attempts."""
    jina.script = [{"truncate": True}]
    r = ws._fetch(URL, cache)
    assert r["status"] == "ok" and r["attempts"] == 2 and len(jina.requests) == 2
    jina.script = [{"truncate": True}, {"truncate": True}]
    r = ws._fetch(URL2, cache)
    assert r["status"] == "error" and r["reason"].count("IncompleteRead") == 2 and r["attempts"] == 2
    assert not os.path.exists(page_path(cache, URL2))


def test_invalid_utf8_is_replaced_and_counted(cache, jina):
    """A strict decode would raise and lose the page; a silent replace would
    hide the damage. The page is kept and the count is a warning."""
    jina.default = {"raw": fj.INVALID_UTF8}
    r = ws._fetch(URL, cache)
    assert r["status"] == "ok" and "decode_replacements: 3" in r["warnings"]
    assert Path(r["path"]).read_text(encoding="utf-8").count("�") == 3


FAILURES = {
    "429-twice": [{"status": 429}, {"status": 429}],
    "503-twice": [{"status": 503}, {"status": 503}],
    "404": [{"status": 404}],
    "451": [{"status": 451}],
    "truncated-twice": [{"truncate": True}, {"truncate": True}],
    "closed-twice": [{"close": True}, {"close": True}],
    "slow-twice": [{"delay": 0.6}, {"delay": 0.6}],
    "invalid-utf8": [{"raw": fj.INVALID_UTF8}],
    "challenge": [{"body": fj.CHALLENGE}, {"body": fj.CHALLENGE}],
    "empty": [{"body": fj.EMPTY}, {"body": fj.EMPTY}],
}


@pytest.mark.parametrize("kind", sorted(FAILURES))
def test_no_exception_escapes_batch_read_urls(cache, jina, monkeypatch, kind):
    """One bad URL must not cost the others: every failure kind comes back
    as that URL's result with a status, reason, warnings and attempts, and
    the URLs around it are fetched."""
    monkeypatch.setattr(ws, "FETCH_TIMEOUT_S", 0.3)
    bad, steps = "https://example.com/bad", list(FAILURES[kind])
    jina.rule = lambda url, headers: (steps.pop(0) if steps else {}) if url == bad else None
    out = call_mcp("batch_read_urls", {"urls": [URL, bad, URL2], "cache_dir": cache})
    assert [r["url"] for r in out] == [URL, bad, URL2]
    assert out[0]["status"] == out[2]["status"] == "ok"
    for r in out:
        assert {"status", "reason", "warnings", "attempts", "cached", "refreshed"} <= set(r)
    assert out[1]["status"] in ("ok", "error", "blocked")
    if kind == "slow-twice":
        assert out[1]["status"] == "error" and "timed out" in out[1]["reason"]


def test_unreachable_endpoint_and_internal_failure_are_results_not_exceptions(cache, monkeypatch):
    """A refused connection is retried and reported; an unexpected exception
    inside the fetch is reported with its class, never raised."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    monkeypatch.setattr(ws, "JINA_BASE", f"http://127.0.0.1:{port}")
    r = ws._fetch(URL, cache)
    assert r["status"] == "error" and r["attempts"] == 2 and "attempt 2" in r["reason"]

    def boom(url, body):
        raise RuntimeError("classifier exploded")

    monkeypatch.setattr(ws, "JINA_BASE", "http://127.0.0.1:9")
    monkeypatch.setattr(ws, "_jina_get", lambda url, headers: {"text": "x", "replacements": 0, "attempts": 1})
    monkeypatch.setattr(ws, "_classify_body", boom)
    out = json.loads(ws.batch_read_urls([URL], cache))
    assert out[0]["status"] == "error" and out[0]["reason"] == "RuntimeError: classifier exploded"


def test_new_page_frontmatter_has_status_and_bytes(cache, jina):
    """Readers decide from the frontmatter whether a page is real content;
    bytes is the size of what follows it. A thin page says so, and so does
    its result."""
    r = ws._fetch(URL, cache)
    fields, rest = ws._frontmatter(Path(r["path"]).read_text(encoding="utf-8"))
    assert list(fields) == ["url", "title", "fetched", "hash", "status", "bytes"]
    assert fields["status"] == "ok" and int(fields["bytes"]) == len(rest.encode("utf-8"))
    jina.default = {"body": fj.jina(URL2, "Tiny", "A short note that is real but small.\n")}
    r = ws._fetch(URL2, cache)
    assert r["status"] == "thin" and any(w.startswith("thin page:") for w in r["warnings"])
    assert ws._frontmatter(Path(r["path"]).read_text())[0]["status"] == "thin"
    cached = ws._fetch(URL2, cache)
    assert cached["status"] == "cached" and cached["page_status"] == "thin"
    assert any(w.startswith("thin page:") for w in cached["warnings"])


# ── Freshness ─────────────────────────────────────────────────────


def test_max_age_days_refetches_an_old_page_and_reports_refreshed(cache, jina, clock, jevserver):
    """Pages that change can be refreshed on request: a copy younger than
    max_age_days is served, an older one is fetched again and replaced,
    through read_url and through find_relevant_ranges."""
    first = ws._fetch(URL, cache)
    clock.t += 3 * DAY
    assert ws._fetch(URL, cache, max_age_days=7)["status"] == "cached" and len(jina.requests) == 1
    r = json.loads(ws.read_url(URL, cache, max_age_days=2))
    assert r["status"] == "ok" and r["refreshed"] is True and len(jina.requests) == 2
    assert ws._frontmatter(Path(r["path"]).read_text())[0]["fetched"] == ws._iso(clock.t)
    assert first["path"] == r["path"]
    clock.t += 3 * DAY
    out = json.loads(ws.find_relevant_ranges("How are widgets installed?", [URL], cache_dir=cache, max_age_days=1))
    assert out[0]["status"] == "ok" and out[0]["refreshed"] is True and len(jina.requests) == 3


def test_refresh_forces_a_refetch_and_bypasses_a_sidecar(cache, jina):
    """refresh=True fetches again even when the copy is new, and does not
    wait out a block; a failed refresh keeps the copy it had."""
    ws._fetch(URL, cache)
    r = ws._fetch(URL, cache, refresh=True)
    assert r["status"] == "ok" and r["refreshed"] is True and len(jina.requests) == 2
    before = Path(page_path(cache)).read_bytes()
    jina.default = {"body": fj.CHALLENGE}
    r = ws._fetch(URL, cache, refresh=True)
    assert r["status"] == "blocked" and r["kept_path"] == page_path(cache)
    assert Path(page_path(cache)).read_bytes() == before
    jina.default = {}
    assert ws._fetch(URL, cache, refresh=True)["status"] == "ok" and len(jina.requests) == 5


def test_defaults_never_refetch(cache, jina, clock):
    """Without the new arguments a cached page never expires, as before."""
    write_page(cache, f"{ws._url_hash(URL)}.md", fj.FILLER * 3, url=URL, fetched="2020-01-01T00:00:00+00:00")
    clock.t += 3650 * DAY
    r = json.loads(ws.read_url(URL, cache))
    assert r["status"] == "cached" and r["cached"] is True and r["refreshed"] is False and jina.requests == []


def test_bad_freshness_arguments_are_errors_not_fetches(cache, jina):
    """A negative age would refetch everything; it is refused before any request."""
    r = ws._fetch(URL, cache, max_age_days=-1)
    assert r["status"] == "error" and "max_age_days" in r["reason"] and jina.requests == []
    out = ws._relevance("Q?", [URL], cache, max_age_days=float("nan"))
    assert out[0]["error"]["kind"] == "usage" and jina.requests == []


# ── Manifests ─────────────────────────────────────────────────────


def test_manifest_has_status_and_kb_columns(cache, jina):
    """The human table shows what each cached page is and how big it is."""
    tiny = {"body": fj.jina(URL2, "Tiny", "A short note that is real but small.\n")}
    jina.rule = lambda url, h: tiny if url == URL2 else None
    ws.batch_read_urls([URL, URL2], cache)
    md = Path(cache, "manifest.md").read_text()
    assert "| # | Title | URL | File | Fetched | Status | KB |\n|---|---|---|---|---|---|---|\n" in md
    rows = {line.split(" | ")[2]: line for line in md.splitlines() if line.startswith("| 1 ") or line.startswith("| 2 ")}
    size = len(Path(page_path(cache)).read_text().split("---\n", 2)[2].encode())
    assert rows[URL].endswith(f"| ok | {size / 1024:.1f} |") and "| thin |" in rows[URL2]
    assert "**Total: 2 pages cached, 0 blocked.**" in md


def test_manifest_lists_blocked_rows(cache, jina):
    """A blocked URL leaves no page, so only its manifest row shows that it
    was tried and why."""
    jina.rule = lambda url, h: {"body": fj.CHALLENGE} if url == URL2 else None
    ws.batch_read_urls([URL, URL2], cache)
    md = Path(cache, "manifest.md").read_text()
    blocked = next(line for line in md.splitlines() if f"{ws._url_hash(URL2)}.blocked.json" in line)
    assert "| blocked | 0.0 |" in blocked and "(blocked: challenge:" in blocked and URL2 in blocked
    assert "**Total: 1 pages cached, 1 blocked.**" in md


def test_manifest_json_fields(cache, jina):
    """manifest.json is what search_cache and other readers parse; each row
    has exactly the documented fields (blocked rows add the reason)."""
    jina.rule = lambda url, h: {"body": fj.CHALLENGE} if url == URL2 else None
    ws.batch_read_urls([URL, URL2], cache)
    write_page(cache, "old.md", fj.FILLER * 3, title="Old page")  # written before the quality gate: no status
    ws._update_manifest(cache)
    rows = {r["file"]: r for r in json.loads(Path(cache, "manifest.json").read_text())}
    page = rows[f"{ws._url_hash(URL)}.md"]
    assert set(page) == {"file", "url", "title", "fetched", "status", "bytes", "lines"}
    assert page["url"] == URL and page["status"] == "ok" and page["title"] == "Widget guide"
    assert page["lines"] == len(Path(page_path(cache)).read_text().splitlines())
    assert rows["old.md"]["status"] == "ok" and rows["old.md"]["bytes"] > 0
    side = rows[f"{ws._url_hash(URL2)}.blocked.json"]
    assert set(side) == {"file", "url", "title", "fetched", "status", "bytes", "lines", "reason"}
    assert side["status"] == "blocked" and side["url"] == URL2 and side["bytes"] == side["lines"] == 0


def test_manifests_are_rebuilt_after_audit(tmp_path):
    """After quarantine the manifests must stop listing the moved stubs and
    show their URLs as blocked."""
    cache = tmp_path / ".web_cache"
    write_page(cache, "good.md", fj.FILLER * 3, url="https://example.com/good")
    stub = write_page(cache, "stub.md", "Performing security verification.\n", title="Just a moment...",
                      url="https://example.com/stub")
    ws._update_manifest(str(cache))
    assert "stub.md" in Path(cache, "manifest.md").read_text()
    ws._audit(str(cache), apply=True)
    rows = json.loads(Path(cache, "manifest.json").read_text())
    assert [r["status"] for r in rows] == ["blocked" if r["file"].endswith(".json") else "ok" for r in rows]
    assert {r["url"] for r in rows if r["status"] == "blocked"} == {"https://example.com/stub"}
    assert "stub.md" not in Path(cache, "manifest.md").read_text() and not os.path.exists(stub)


# ── search_cache ──────────────────────────────────────────────────


def search_cache_dir(tmp_path):
    cache = tmp_path / ".web_cache"
    write_page(cache, "a.md", sections(["Position sizing rules for traders."] * 2), title="Kelly criterion sizing")
    write_page(cache, "b.md", sections(["Notes on the kelly bet once.", "Unrelated text."]), title="Notes")
    write_page(cache, "c.md", sections(["Nothing relevant here."] * 2), title="Gardening")
    ws._update_manifest(str(cache))
    return str(cache)


def test_search_ranks_query_terms_in_the_title_above_the_body(tmp_path):
    """Title words count twice, so a page about the subject outranks a page
    that mentions it once; a page without the words is not listed."""
    out = ws._search("kelly", search_cache_dir(tmp_path))
    assert out["status"] == "ok" and [p["file"] for p in out["pages"]] == ["a.md", "b.md"]
    assert out["pages"][0]["score"] > out["pages"][1]["score"] > 0
    assert out["pages"][0]["title"] == "Kelly criterion sizing" and out["pages"][0]["url"] == "https://example.com/a.md"
    single = tmp_path / "single" / ".web_cache"
    write_page(single, "o.md", "A body without the word.\n", title="Okapi")
    (score,) = [p["score"] for p in ws._search("okapi", str(single))["pages"]]
    idf = math.log(1 + (1 - 1 + 0.5) / (1 + 0.5))  # one window, which holds the term
    assert score == round(idf * 2 * (1.2 + 1) / (2 + 1.2), 4)  # tf 2 from the title; length equals the average


def test_search_windows_have_correct_line_numbers(tmp_path):
    """Windows are read with Read(offset, limit); their line numbers must be
    the file's, trimmed of blank edges, and hold the matching text."""
    cache = tmp_path / ".web_cache"
    path = write_page(cache, "p.md", sections(["Plain text.", "The zebra crossing rules.", "More plain text."]))
    ws._update_manifest(str(cache))
    out = ws._search("zebra", str(cache))
    (start, end, score), = out["pages"][0]["windows"]
    lines = Path(path).read_text().split("\n")
    assert lines[start - 1] == "## Section 2" and lines[end - 1].startswith("The zebra crossing rules.")
    assert all("zebra" not in lines[n - 1] for n in range(1, len(lines) + 1) if not start <= n <= end)
    assert score == out["pages"][0]["score"]


def test_search_index_is_reused_on_an_unchanged_cache(tmp_path):
    """A repeat query on an unchanged cache only reads the index: no page is
    windowed again, even after a touch that changes only the mtime."""
    cache = search_cache_dir(tmp_path)
    first = ws._search("kelly", cache)
    assert first["index"]["rebuilt"] == 3 and first["index"]["reused"] == 0
    second = ws._search("gardening", cache)
    assert second["index"]["rebuilt"] == 0 and second["index"]["reused"] == 3
    os.utime(os.path.join(cache, "a.md"), (1, 1))
    third = ws._search("kelly", cache)
    assert third["index"]["rebuilt"] == 0 and third["index"]["reused"] == 3 and third["pages"] == first["pages"]
    assert os.path.exists(os.path.join(cache, ws.SEARCH_INDEX_DB))


def test_search_index_is_invalidated_on_file_change(tmp_path):
    """An edited page is windowed again and its new words are found; a
    removed page leaves the index."""
    cache = search_cache_dir(tmp_path)
    ws._search("kelly", cache)
    path = Path(cache, "c.md")
    path.write_text(path.read_text().replace("Nothing relevant here.", "Now about okapi tuning."))
    out = ws._search("okapi", cache)
    assert out["index"]["rebuilt"] == 1 and out["index"]["reused"] == 2
    assert [p["file"] for p in out["pages"]] == ["c.md"]
    os.remove(os.path.join(cache, "b.md"))
    ws._update_manifest(cache)
    out = ws._search("kelly", cache)
    assert out["index"]["removed"] == 1 and [p["file"] for p in out["pages"]] == ["a.md"]


def rerank_cache(tmp_path):
    """Page x.md: 15 windows that say widget three times, judged 0.1.
    Page y.md: 15 windows that say it once, the first judged 0.95. BM25
    puts x first; Jev puts y first."""
    cache = tmp_path / ".web_cache"
    write_page(cache, "x.md", sections(["[[p=0.1]] widget widget widget."] * 15))
    write_page(cache, "y.md", sections(["[[p=0.95]] widget."] + ["[[p=0.1]] widget."] * 14))
    ws._update_manifest(str(cache))
    return str(cache)


def test_search_jev_reranks_the_top_20_windows_and_reuses_the_answer_cache(tmp_path, jevserver):
    """Jev reorders by whether a window helps, not by word counts; it is
    asked about exactly the top 20 windows, and a repeat query is answered
    from the answers cache with no request."""
    cache = rerank_cache(tmp_path)
    lexical = ws._search("widget", cache)
    assert [p["file"] for p in lexical["pages"]] == ["x.md", "y.md"]
    out = ws._search("widget", cache, jev=True)
    assert out["status"] == "ok" and [p["file"] for p in out["pages"]] == ["y.md", "x.md"]
    assert out["pages"][0]["p"] == 0.95 and out["pages"][0]["windows"][0][3] == 0.95
    assert sum(len(r["body"]["questions"]) for r in jevserver.requests) == ws.SEARCH_JEV_WINDOWS == 20
    assert out["jev_requests"] == len(jevserver.requests) == 2 and out["input_tokens"] == 200
    assert out["cost_usd"] == round(200 * 0.042 / 1_000_000, 6)
    again = ws._search("widget", cache, jev=True)
    assert again["jev_requests"] == 0 and again["cache_hits"] == 2 and again["pages"] == out["pages"]
    assert len(jevserver.requests) == 2


def test_search_jev_failure_is_loud_and_keeps_the_lexical_results(tmp_path, jevserver, monkeypatch):
    """A Jev failure is reported as in find_relevant_ranges, never as a low
    probability; the lexical ranking is still returned."""
    cache = rerank_cache(tmp_path)
    jevserver.rule = lambda body: {"status": 422, "body": {"detail": "bad"}}
    out = ws._search("widget", cache, jev=True)
    assert out["status"] == "error" and out["error"]["kind"] == "client_error" and ws._search_exit(out) == 2
    assert [p["file"] for p in out["pages"]] == ["x.md", "y.md"] and out["pages"][0]["p"] is None
    monkeypatch.delenv("JEV_API_KEY")
    out = ws._search("widget", cache, jev=True)
    assert out["error"]["kind"] == "no_key" and ws._search_exit(out) == 4


def test_search_denied_project_returns_denied_and_sends_nothing(tmp_path, jevserver):
    """A denied project's pages never go to Jev; the lexical path still
    works so the caller can read what it found."""
    cache = tmp_path / "private_proj" / ".web_cache"
    write_page(cache, "p.md", sections(["[[p=0.9]] ledger entries for march."]))
    out = ws._search("ledger entries", str(cache), jev=True)
    assert out["status"] == "denied" and "'private_proj'" in out["reason"] and jevserver.requests == []
    assert [p["file"] for p in out["pages"]] == ["p.md"] and out["pages"][0]["windows"][0][3] is None


def test_search_output_holds_no_page_text(tmp_path, jevserver):
    """search_cache exists to keep page text out of the caller's context:
    only names, numbers and line ranges come back, with or without Jev."""
    cache = tmp_path / ".web_cache"
    write_page(cache, "z.md", sections(["The zebra SENTINEL-77 grazes quietly by the river."]), title="Zebras")
    for jev_on in (False, True):
        raw = json.dumps(ws._search("zebra river", str(cache), jev=jev_on))
        assert "zebra" in raw and "SENTINEL-77" not in raw and "grazes quietly" not in raw


def test_search_rejects_bad_input_and_a_missing_directory(tmp_path):
    """A query with no words or a bad top_k is a usage error; a missing
    cache is not_found; neither is an empty success."""
    cache = search_cache_dir(tmp_path)
    assert ws._search("  ...  ", cache)["error"]["kind"] == "usage"
    assert ws._search("kelly", cache, top_k=0)["error"]["kind"] == "usage"
    assert ws._search("kelly", str(tmp_path / "nowhere"))["error"]["kind"] == "not_found"


# ── Deny list ─────────────────────────────────────────────────────


def write_deny(privacy):
    """Write the deny-list config file web-sieve reads (a temporary file in these tests)."""
    Path(ws.DENY_CONFIG).write_text(json.dumps({"version": 1, "privacy": privacy}))


def test_deny_list_names_come_from_the_config_file(tmp_path, jevserver):
    """No private project name is written in the public source: the names
    come from the config file and match whole path components, ignoring
    case. The three generic built-in names are denied as well, and a
    readable config adds no warning to a result that was sent."""
    assert ws.DENY_DEFAULTS == ("clients", "client_data", "client-data")
    write_deny({"deny_projects": ["private_proj", "Second_Proj"]})
    for project in ("private_proj", "PRIVATE_PROJ", "second_proj", "clients", "client_data", "Client-Data"):
        reason = ws._jev_denied(str(tmp_path / "Projects" / project / ".web_cache"))
        assert reason and "deny list" in reason, project
    for project in ("private_proj_public", "my_private_proj", "open_proj"):
        assert ws._jev_denied(str(tmp_path / "Projects" / project / ".web_cache")) is None, project
    path = write_page(tmp_path / "open_proj" / ".web_cache", "p.md", sections(["[[p=0.9]] widget facts."]))
    r, = ws._relevance("What are widget facts?", [path])
    assert r["status"] == "ok" and r["warnings"] == []


def test_deny_list_star_entry_is_a_prefix_match(tmp_path):
    """An entry ending in * covers a family of projects (every name that
    starts with its stem), and only that family."""
    write_deny({"deny_projects": ["journal*"]})
    for project in ("journal", "journal_2026", "Journal-Notes", "journal_2026-wt-topic"):
        assert ws._jev_denied(str(tmp_path / project / ".web_cache")), project
    for project in ("my_journal", "jour"):
        assert ws._jev_denied(str(tmp_path / project / ".web_cache")) is None, project


def test_deny_list_covers_worktrees(tmp_path):
    """A git worktree of a denied project (<name>-wt-<topic>) holds the same
    private material; a name that only starts with the project's name is a
    different project."""
    write_deny({"deny_projects": ["private_proj"]})
    assert ws._jev_denied(str(tmp_path / "private_proj-wt-feature" / ".web_cache"))
    assert ws._jev_denied(str(tmp_path / "Private_Proj-WT-q3" / ".web_cache"))
    assert ws._jev_denied(str(tmp_path / "private_projwt" / ".web_cache")) is None
    assert ws._jev_denied(str(tmp_path / "private_proj-wtx" / ".web_cache")) is None


def test_deny_list_path_prefixes(tmp_path, monkeypatch):
    """privacy.deny_path_prefixes denies every directory under a path,
    whatever its project name: given absolute or with ~, with or without a
    trailing separator, and also when the cache is a link into it."""
    vault = tmp_path / "vault"
    (vault / "y").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    write_deny({"deny_projects": [], "deny_path_prefixes": [str(vault) + os.sep, "~/archive"]})
    assert "denied path prefix" in ws._jev_denied(str(vault / "x" / ".web_cache"))
    assert ws._jev_denied(str(vault)) and ws._jev_denied(str(tmp_path / "archive" / "a" / ".web_cache"))
    assert ws._jev_denied(str(tmp_path / "vault2" / ".web_cache")) is None
    link = tmp_path / "open" / ".web_cache"
    link.parent.mkdir()
    link.symlink_to(vault / "y")
    assert ws._jev_denied(str(link))


@pytest.mark.parametrize("content", [None, "{not json", '["a list"]', '{"privacy": {}}',
                                     '{"privacy": {"deny_projects": "private_proj"}}'],
                         ids=["missing", "not-json", "not-an-object", "no-deny-projects", "not-a-list"])
def test_missing_or_unparsable_deny_config_denies_only_the_defaults_and_warns(tmp_path, jevserver, content):
    """Without a readable list web-sieve cannot know which projects are
    private. It must not invent names and must not stay quiet: only the
    generic built-in names are denied, and every result that sent pages to
    Jev says the deny list is not configured. A lexical search sends
    nothing and carries no such warning."""
    config = Path(ws.DENY_CONFIG)
    if content is None:
        config.unlink()
    else:
        config.write_text(content)
    warnings = []
    assert ws._jev_denied(str(tmp_path / "private_proj" / ".web_cache"), warnings) is None
    assert ws._jev_denied(str(tmp_path / "clients" / ".web_cache"), warnings)
    assert len(warnings) == 1 and warnings[0].startswith("deny_list: not configured")
    assert ("does not exist" if content is None else "could not be read") in warnings[0]
    cache = tmp_path / ".web_cache"
    path = write_page(cache, "p.md", sections(["[[p=0.9]] widget facts."]))
    r, = ws._relevance("What are widget facts?", [path])
    assert r["status"] == "ok" and r["warnings"] == warnings
    out = ws._search("widget facts", str(cache), jev=True)
    assert out["status"] == "ok" and out["jev_requests"] == 1 and out["pages"][0]["p"] == 0.9
    assert sum(w.startswith("deny_list: not configured") for w in out["warnings"]) == 1
    assert not any(w.startswith("deny_list") for w in ws._search("widget facts", str(cache))["warnings"])


def test_symlinked_cache_dir_resolves_to_its_real_path(tmp_path, jevserver):
    """A link from an innocent name to a denied project's cache, or a page
    file linked from one, must not get past the deny list."""
    real = tmp_path / "private_proj" / ".web_cache"
    page = write_page(real, "p.md", sections(["[[p=0.9]] private notes."]))
    public = tmp_path / "public"
    public.mkdir()
    (public / ".web_cache").symlink_to(real)
    assert ws._jev_denied(str(public / ".web_cache"))
    other = tmp_path / "open" / ".web_cache"
    other.mkdir(parents=True)
    (other / "link.md").symlink_to(page)
    results = ws._relevance("What are the notes?", [str(public / ".web_cache" / "p.md"), str(other / "link.md")])
    assert [r["status"] for r in results] == ["denied", "denied"] and jevserver.requests == []


def test_find_relevant_ranges_on_a_denied_page_sends_nothing(tmp_path, jevserver, monkeypatch, capsys):
    """A denied page gets status denied with the reason and its whole body
    unjudged; other pages in the same call are judged; the CLI exits 1."""
    denied = write_page(tmp_path / "private_proj" / ".web_cache", "p.md", sections(["[[p=0.9]] a name."]))
    allowed = write_page(tmp_path / "open" / ".web_cache", "q.md", sections(["[[p=0.9]] widget facts."]))
    rd, ra = json.loads(ws.find_relevant_ranges("Which widget facts?", [denied, allowed]))
    assert rd["status"] == "denied" and rd["error"]["kind"] == "denied" and "'private_proj'" in rd["reason"]
    assert rd["relevant"] is None and rd["unjudged"] == [{"lines": [12, rd["file_lines"]], "reason": "denied"}]
    assert ra["status"] == "ok" and ra["relevant"] is True
    sent = json.dumps([r["body"] for r in jevserver.requests])
    assert len(jevserver.requests) == 1 and "a name" not in sent
    code, _ = cli(monkeypatch, capsys, "ranges", "Which widget facts?", denied)
    assert code == 1


# ── audit ─────────────────────────────────────────────────────────


def audit_cache_dir(tmp_path, name=".web_cache"):
    """One page of each status, a non-page note, and the manifests."""
    cache = tmp_path / name
    write_page(cache, "ok.md", fj.FILLER * 3, title="Widget guide", url="https://example.com/ok")
    write_page(cache, "thin.md", "A short note that is real but small.\n", title="Tiny", url="https://example.com/thin")
    write_page(cache, "challenge.md", "## Performing security verification\n", title="Just a moment...",
               url="https://example.com/challenge", fetched="2026-09-01T00:00:00+00:00")
    write_page(cache, "empty.md", "\n", title="", url="https://example.com/empty")
    (cache / "notes.md").write_text("my own notes, not a cached page\n")
    ws._update_manifest(str(cache))
    return cache


def test_audit_dry_run_moves_nothing(tmp_path):
    """The default only reports: every file is left exactly as it was."""
    cache = audit_cache_dir(tmp_path)
    before = snapshot(cache)
    out = ws._audit(str(cache))
    assert snapshot(cache) == before and out["apply"] is False and out["status"] == "ok"
    assert out["counts"] == {"ok": 1, "thin": 1, "challenge": 1, "empty": 1} and out["pages"] == 4
    assert [e["file"] for e in out["challenge"]] == ["challenge.md"] and [e["file"] for e in out["empty"]] == ["empty.md"]
    assert "'Just a moment'" in out["challenge"][0]["reason"] and out["moved"] == [] and out["marked_thin"] == []
    assert out["skipped"] == [{"file": "notes.md", "reason": "no web-sieve frontmatter with a url: line"}]


def test_audit_apply_quarantines_logs_writes_sidecars_marks_thin_and_rebuilds(tmp_path, jina):
    """Stubs move to _quarantine (evidence, reversible), each move is logged,
    each URL gets a sidecar dated when the stub was fetched (so the next
    fetch goes to the network), thin pages are marked, manifests rebuilt."""
    cache = audit_cache_dir(tmp_path)
    thin_before = Path(cache, "thin.md").read_text()
    out = ws._audit(str(cache), apply=True)
    assert out["status"] == "ok" and out["sidecars_written"] == 2 and out["marked_thin"] == ["thin.md"]
    assert {m["file"]: m["moved_to"] for m in out["moved"]} == {"challenge.md": "_quarantine/challenge.md",
                                                                "empty.md": "_quarantine/empty.md"}
    assert not Path(cache, "challenge.md").exists() and Path(cache, "_quarantine", "challenge.md").exists()
    log = [json.loads(line) for line in Path(cache, "_quarantine", "quarantine.jsonl").read_text().splitlines()]
    assert [(e["file"], e["status"]) for e in log] == [("challenge.md", "challenge"), ("empty.md", "empty")]
    assert all(e["reason"] and e["url"] for e in log)
    side = json.loads(Path(sidecar_path(str(cache), "https://example.com/challenge")).read_text())
    assert side["status"] == "blocked" and side["at"] == "2026-09-01T00:00:00+00:00"
    assert side["quarantined"] == "_quarantine/challenge.md"
    thin_after = Path(cache, "thin.md").read_text()
    assert ws._frontmatter(thin_after)[0]["status"] == "thin"
    assert thin_after.replace("status: thin\n", "", 1) == thin_before
    statuses = {r["file"]: r["status"] for r in json.loads(Path(cache, "manifest.json").read_text())}
    assert statuses["thin.md"] == "thin" and "challenge.md" not in statuses
    assert sorted(s for f, s in statuses.items() if f.endswith(".blocked.json")) == ["blocked", "blocked"]
    r = ws._fetch("https://example.com/challenge", str(cache))
    assert r["status"] == "ok" and jina.urls() == ["https://example.com/challenge"]


def test_audit_second_run_is_a_no_op(tmp_path):
    """Running the audit again must not move, mark or re-date anything."""
    cache = audit_cache_dir(tmp_path)
    ws._audit(str(cache), apply=True)
    before = {k: v for k, v in snapshot(cache).items() if not k.startswith("manifest.")}
    out = ws._audit(str(cache), apply=True)
    assert out["moved"] == [] and out["marked_thin"] == [] and out["sidecars_written"] == 0
    assert out["counts"] == {"ok": 1, "thin": 1, "challenge": 0, "empty": 0}
    assert {k: v for k, v in snapshot(cache).items() if not k.startswith("manifest.")} == before


def test_audit_leaves_ok_pages_untouched_byte_for_byte(tmp_path):
    """Ranges already handed out for good pages must stay valid: their bytes
    and modification times do not change."""
    cache = audit_cache_dir(tmp_path)
    ok = Path(cache, "ok.md")
    before, mtime = ok.read_bytes(), ok.stat().st_mtime_ns
    ws._audit(str(cache), apply=True)
    assert ok.read_bytes() == before and ok.stat().st_mtime_ns == mtime


# ── CLI and MCP parity ────────────────────────────────────────────


def without(result, *keys):
    if isinstance(result, list):
        return [without(r, *keys) for r in result]
    return {k: v for k, v in result.items() if k not in keys}


def test_search_cache_cli_and_mcp_return_the_same_json(tmp_path, jevserver, monkeypatch, capsys):
    """The MCP tool and `web-sieve search` share one code path; given the
    same inputs and the same starting state they print the same result."""
    cache = rerank_cache(tmp_path)
    for jev_on in (False, True):
        for f in Path(cache).glob("*.sqlite*"):
            f.unlink()
        via_mcp = call_mcp("search_cache", {"query": "widget", "cache_dir": cache, "top_k": 5, "jev": jev_on})
        for f in Path(cache).glob("*.sqlite*"):
            f.unlink()
        code, via_cli = cli(monkeypatch, capsys, "search", "widget", "--cache-dir", cache, "--top-k", "5",
                            *(["--jev"] if jev_on else []))
        assert without(via_mcp, "elapsed_ms") == without(via_cli, "elapsed_ms") and code == 0
        assert via_cli["jev"] is jev_on and via_cli["index"]["rebuilt"] == 2


def test_audit_cache_cli_and_mcp_return_the_same_json(tmp_path, monkeypatch, capsys):
    """Dry runs on one cache, and applies on two identical caches, give the
    same report through the MCP tool and `web-sieve audit`."""
    cache = audit_cache_dir(tmp_path)
    via_mcp = call_mcp("audit_cache", {"cache_dir": str(cache)})
    code, via_cli = cli(monkeypatch, capsys, "audit", str(cache), "--dry-run")
    assert via_mcp == via_cli and code == 0
    one = audit_cache_dir(tmp_path / "one")
    two = audit_cache_dir(tmp_path / "two")
    via_mcp = call_mcp("audit_cache", {"cache_dir": str(one), "apply": True})
    code, via_cli = cli(monkeypatch, capsys, "audit", str(two), "--apply")
    assert without(via_mcp, "cache_dir") == without(via_cli, "cache_dir") and code == 0 and via_cli["moved"]


def test_read_and_batch_cli_and_mcp_return_the_same_json(tmp_path, jina, monkeypatch, capsys):
    """The fetch tools gained statuses; the CLI must report them exactly as
    the MCP tools do, for a cached page, a blocked URL and a failed one."""
    cache = str(tmp_path / ".web_cache")
    bad = "https://example.com/missing"
    jina.rule = lambda url, h: ({"body": fj.CHALLENGE} if url == URL2 else {"status": 404} if url == bad else None)
    ws.batch_read_urls([URL, URL2], cache)
    via_mcp = call_mcp("batch_read_urls", {"urls": [URL, URL2, bad], "cache_dir": cache})
    code, via_cli = cli(monkeypatch, capsys, "batch", URL, URL2, bad, "--cache-dir", cache)
    assert via_mcp == via_cli and code == 0
    assert [r["status"] for r in via_cli] == ["cached", "blocked", "error"]
    assert call_mcp("read_url", {"url": URL, "cache_dir": cache}) == cli(monkeypatch, capsys, "read", URL,
                                                                          "--cache-dir", cache)[1]


def test_new_subcommands_dispatch_from_the_command_line(tmp_path):
    """`web-sieve search` and `web-sieve audit` must reach the CLI rather
    than start the MCP server."""
    cache = search_cache_dir(tmp_path)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1"}
    for args in (["search", "kelly", "--cache-dir", cache], ["audit", cache, "--dry-run"]):
        out = subprocess.run([sys.executable, str(ROOT / "web-sieve.py"), *args],
                             capture_output=True, text=True, env=env, timeout=60)
        assert out.returncode == 0, out.stderr
        assert json.loads(out.stdout)["status"] == "ok"


# ── Review probes (adversarial review, 2026-10-04) ────────────────


def test_preamble_warning_and_pdf_page_count_lines_are_header_not_body():
    """Jina puts Warning: lines, and for a PDF a Number of Pages: line,
    between URL Source: and Markdown Content:. They are header: a response
    that is only header is empty (an empty PDF must go to the fallback, not
    be cached as a thin page), and a Warning: line inside the body is body."""
    head = "Title: Report\n\nURL Source: https://example.com/r.pdf\n\nPublished Time: Thu, 22 May 2025 14:49:05 GMT\n\n"
    warnings_only = head + "Warning: Target URL returned error 404: Not Found\nWarning: second\n\nMarkdown Content:\n"
    assert ws._classify_body(URL, warnings_only)[0] == "empty"
    assert ws._classify_body(URL, head + "Number of Pages: 12\n\nMarkdown Content:\n\n")[0] == "empty"
    pdf = head + "Number of Pages: 4\n\nMarkdown Content:\n" + fj.FILLER * 3
    assert ws._jina_parts(pdf)[2] == fj.FILLER * 3 and ws._classify_body(URL, pdf) == ("ok", "")
    title, warnings, body = ws._jina_parts(head + "Markdown Content:\nWarning: this sentence is page text.\n")
    assert title == "Report" and warnings == [] and body.startswith("Warning: this sentence")


def test_crlf_response_is_read_like_lf():
    """Line endings must not change a classification: the preamble, the
    title, the warnings and the size rules read CRLF as they read LF."""
    challenge = fj.jina(URL, "Just a moment...", "## Performing security verification\n")
    assert ws._classify_body(URL, challenge.replace("\n", "\r\n"))[0] == "challenge"
    assert ws._classify_body(URL, fj.EMPTY.replace("\n", "\r\n"))[0] == "empty"
    title, warnings, body = ws._jina_parts(fj.jina(URL, "Guide", "Body.\n", ("w1",)).replace("\n", "\r\n"))
    assert title == "Guide" and warnings == ["w1"] and body.strip() == "Body."


def test_crlf_page_marked_thin_keeps_crlf_and_search_lines_match_read(tmp_path):
    """The audit's frontmatter edit keeps a CRLF file CRLF, and search_cache
    numbers its lines as Read does."""
    cache = tmp_path / ".web_cache"
    path = Path(write_page(cache, "c.md", "A short note about a zebra crossing.\n", title="Tiny"))
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    assert ws._audit(str(cache), apply=True)["marked_thin"] == ["c.md"]
    raw = path.read_bytes()
    assert b"status: thin\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
    (start, end, _), = ws._search("zebra", str(cache))["pages"][0]["windows"]
    lines = raw.decode().split("\r\n")
    assert start == end and "zebra" in lines[start - 1]


def test_jina_error_json_in_a_200_is_not_cached_as_content(cache, jina):
    """Jina reports its own failures as a JSON object (code, name, message).
    Served with status 200, that text is not the page: cached, it would read
    as a clean negative, so it is treated like an empty response."""
    err = json.dumps({"data": None, "code": 451, "name": "SecurityCompromiseError", "status": 45102,
                      "message": "Anonymous access to domain example.com blocked until Sat Oct 10 2026 due to "
                                 "previous abuse found on " + "https://example.com/a/very/long/path " * 8,
                      "readableMessage": "SecurityCompromiseError: Anonymous access to domain example.com blocked"})
    assert len(err) > ws.THIN_CHARS
    jina.default = {"body": err}
    r = ws._fetch(URL, cache)
    assert r["status"] == "blocked" and "451" in r["reason"] and not os.path.exists(page_path(cache))
    assert ws._classify_body(URL, '{"name": "widget", "code": 7, "message": "a package file"}')[0] == "thin"


def test_corrupt_sidecar_is_refetched_not_raised(cache, jina):
    """A sidecar cut short by a crash or edited by hand must not block the
    URL or break the fetch or the manifest: the URL is fetched again with a
    warning, and a success replaces the sidecar with a page."""
    os.makedirs(cache)
    Path(sidecar_path(cache)).write_text('{"url": "https://exa')
    Path(sidecar_path(cache, URL2)).write_text('["not", "an", "object"]')
    ws._update_manifest(cache)
    rows = {r["file"]: r for r in json.loads(Path(cache, "manifest.json").read_text())}
    assert rows[f"{ws._url_hash(URL)}.blocked.json"]["reason"] == "sidecar is not readable JSON"
    out = json.loads(ws.batch_read_urls([URL, URL2], cache))
    assert [r["status"] for r in out] == ["ok", "ok"] and len(jina.requests) == 2
    assert all(any("not readable JSON" in w for w in r["warnings"]) for r in out)
    assert not os.path.exists(sidecar_path(cache)) and not os.path.exists(sidecar_path(cache, URL2))


def test_sidecar_boundary_is_exactly_24_hours(cache, jina, clock):
    """Just under 24 hours the sidecar answers with no request; at 24 hours
    the URL is fetched again."""
    clock.t = 1_800_000_000.0  # a whole second, so the sidecar's ISO time reads back exactly
    jina.default = {"body": fj.CHALLENGE}
    ws._fetch(URL, cache)
    at = ws._epoch(json.loads(Path(sidecar_path(cache)).read_text())["at"])
    assert at == clock.t and len(jina.requests) == 2
    clock.t = at + ws.BLOCKED_TTL_S - 0.001
    assert ws._fetch(URL, cache)["attempts"] == 0 and len(jina.requests) == 2
    clock.t = at + ws.BLOCKED_TTL_S
    jina.default = {}
    r = ws._fetch(URL, cache)
    assert r["status"] == "ok" and r["attempts"] == 1 and len(jina.requests) == 3


@pytest.mark.parametrize("fetched", [None, "", "yesterday", "2026-13-45T00:00:00"])
def test_max_age_days_refetches_a_page_of_unknown_age(cache, jina, fetched):
    """A page whose fetched: time is missing or does not parse has an
    unknown age: with max_age_days it is fetched again (stale is the safe
    reading); without max_age_days it is served from the cache as before."""
    path = Path(write_page(cache, f"{ws._url_hash(URL)}.md", fj.FILLER * 3, url=URL, fetched=fetched or ""))
    if fetched is None:
        path.write_text(path.read_text().replace("fetched: \n", ""))
        assert "fetched" not in ws._frontmatter(path.read_text())[0]
    assert ws._fetch(URL, cache)["status"] == "cached" and jina.requests == []
    r = ws._fetch(URL, cache, max_age_days=365)
    assert r["status"] == "ok" and r["refreshed"] is True and len(jina.requests) == 1


def test_refresh_on_a_blocked_url_goes_to_the_network(cache, jina):
    """refresh=True is how a caller retries a block within 24 hours: a
    request is sent, and a success replaces the sidecar with a page."""
    jina.default = {"body": fj.CHALLENGE}
    assert ws._fetch(URL, cache)["status"] == "blocked"
    assert ws._fetch(URL, cache)["attempts"] == 0
    jina.default = {}
    r = ws._fetch(URL, cache, refresh=True)
    assert r["status"] == "ok" and r["attempts"] == 1 and len(jina.requests) == 3
    assert os.path.exists(page_path(cache)) and not os.path.exists(sidecar_path(cache))


def test_manifest_write_interrupted_mid_file_leaves_the_old_manifest(tmp_path, monkeypatch):
    """An exception while manifest.json is half written must leave the
    previous complete file and no temporary file."""
    cache = search_cache_dir(tmp_path)
    before = Path(cache, "manifest.json").read_bytes()
    write_page(cache, "d.md", fj.FILLER * 3, title="New page")
    real_fdopen = os.fdopen

    class Torn:
        def __init__(self, f):
            self.f = f

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.f.close()

        def write(self, text):
            if text.startswith("["):
                self.f.write(text[: len(text) // 2])
                self.f.flush()
                raise KeyboardInterrupt("interrupted mid-write")
            return self.f.write(text)

    monkeypatch.setattr(ws.os, "fdopen", lambda *a, **k: Torn(real_fdopen(*a, **k)))
    with pytest.raises(KeyboardInterrupt):
        ws._update_manifest(cache)
    monkeypatch.setattr(ws.os, "fdopen", real_fdopen)
    assert Path(cache, "manifest.json").read_bytes() == before and not list(Path(cache).glob(".tmp-*"))


def test_manifest_survives_a_process_killed_mid_write(tmp_path):
    """A process killed (SIGKILL) while writing manifest.json leaves the
    previous complete file; its temporary file is left behind, and no
    reader is confused by it."""
    cache = search_cache_dir(tmp_path)
    before = Path(cache, "manifest.json").read_bytes()
    write_page(cache, "d.md", fj.FILLER * 3, title="New page")
    script = (
        "import os, signal\n"
        "from importlib.machinery import SourceFileLoader\n"
        "from importlib.util import module_from_spec, spec_from_loader\n"
        f"loader = SourceFileLoader('ws', {str(ROOT / 'web-sieve.py')!r})\n"
        "ws = module_from_spec(spec_from_loader('ws', loader)); loader.exec_module(ws)\n"
        "real = os.fdopen\n"
        "class Torn:\n"
        "    def __init__(self, f): self.f = f\n"
        "    def __enter__(self): return self\n"
        "    def __exit__(self, *a): self.f.close()\n"
        "    def write(self, text):\n"
        "        if text.startswith('['):\n"
        "            self.f.write(text[: len(text) // 2]); self.f.flush(); os.kill(os.getpid(), signal.SIGKILL)\n"
        "        return self.f.write(text)\n"
        "os.fdopen = lambda *a, **k: Torn(real(*a, **k))\n"
        f"ws._update_manifest({cache!r})\n")
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=60)
    assert proc.returncode == -9, proc.stderr
    assert Path(cache, "manifest.json").read_bytes() == before
    assert len(list(Path(cache).glob(".tmp-*.part"))) == 1
    out = ws._search("kelly", cache)
    assert out["status"] == "ok" and out["index"]["pages"] == 4
    ws._update_manifest(cache)
    assert len(json.loads(Path(cache, "manifest.json").read_text())) == 4


def test_search_on_an_empty_cache_and_a_quarantine_only_cache(tmp_path):
    """Nothing cached, or only quarantined stubs: an empty ok result, never an
    error and never a quarantined stub."""
    empty = tmp_path / "e" / ".web_cache"
    empty.mkdir(parents=True)
    out = ws._search("kelly", str(empty))
    assert out["status"] == "ok" and out["pages"] == [] and out["index"]["pages"] == 0
    stubs = tmp_path / "q" / ".web_cache"
    write_page(stubs, "stub.md", "Performing security verification.\n", title="Just a moment...")
    ws._audit(str(stubs), apply=True)
    out = ws._search("security verification moment", str(stubs))
    assert out["status"] == "ok" and out["pages"] == [] and out["warnings"] == []


def test_search_stopword_query_is_ranked_not_refused(tmp_path):
    """The spec has no stopword list: a query of common words is a valid
    query and ranks pages by those words, with positive scores."""
    cache = search_cache_dir(tmp_path)
    out = ws._search("for the and", cache)
    assert out["status"] == "ok" and out["pages"] and all(p["score"] > 0 for p in out["pages"])


def test_search_two_pages_with_identical_content_are_both_listed(tmp_path):
    """Two URLs that serve the same text are two cached pages; both are
    found, with equal scores, in file order."""
    cache = tmp_path / ".web_cache"
    body = sections(["The okapi lives in the forest."])
    write_page(cache, "b.md", body, title="Okapi", url="https://example.com/b")
    write_page(cache, "a.md", body, title="Okapi", url="https://example.com/a")
    out = ws._search("okapi", str(cache))
    assert [p["file"] for p in out["pages"]] == ["a.md", "b.md"]
    assert out["pages"][0]["score"] == out["pages"][1]["score"] and out["pages"][0]["windows"] == out["pages"][1]["windows"]


def test_split_line_scans_at_most_one_segment_of_text(monkeypatch):
    """A one-line page of non-ASCII text (no newlines) took 17 s to window at
    1 MB because each segment's search measured slices up to the rest of the
    line. No segment can be longer than limit * CHARS_PER_TOKEN characters,
    so no measured slice may be either; the segments are unchanged."""
    line = ("漢字かな交じり文 " * 3_000) + ("ascii words " * 2_000)

    def unbounded(line, limit):  # the search before the fix: up to the end of the line
        spans, a, n = [], 0, len(line)
        while a < n:
            lo, hi = a + 1, n
            while lo < hi:
                mid = (lo + hi + 1) // 2
                lo, hi = (mid, hi) if ws._est_tokens(line[a:mid]) <= limit else (lo, mid - 1)
            b = lo
            if b < n:
                floor = a + (b - a) * 4 // 5
                cut = next((j for j in range(b - 1, floor - 1, -1) if line[j].isspace()), None)
                if cut is not None and cut + 1 > a:
                    b = cut + 1
            spans.append([a, b])
            a = b
        return spans

    reference = unbounded(line, 800)  # same segments, so the same windows and answer-cache keys
    measured = []
    real = ws._est_tokens
    monkeypatch.setattr(ws, "_est_tokens", lambda text: measured.append(len(text)) or real(text))
    assert ws._split_line(line, 800) == reference
    assert max(measured) <= 800 * ws.CHARS_PER_TOKEN + 1
    for a, b in reference:
        assert real(line[a:b]) <= 800


def test_search_indexes_a_1_mb_page_in_under_2_seconds(tmp_path):
    """search_cache windows every page of the cache on a cold index; one
    large page must not stall the server."""
    for name, body in (("multi", ("Widgets are installed with the widgetctl tool. zebra\n\n" * 20_000)),
                       ("oneline", "漢字かな交じり文 zebra " * 70_000)):
        cache = tmp_path / name / ".web_cache"
        write_page(cache, "big.md", body)
        assert len(body) >= 1_000_000
        started = time.monotonic()
        out = ws._search("zebra", str(cache))
        assert out["status"] == "ok" and out["pages"] and time.monotonic() - started < 2.0, name


def test_search_index_drops_a_page_the_audit_quarantines(tmp_path):
    """After the audit moves a stub, search_cache stops returning it, with
    no warning (the audit rebuilt manifest.json)."""
    cache = search_cache_dir(tmp_path)
    write_page(cache, "stub.md", "Kelly. Performing security verification.\n", title="Just a moment...")
    ws._update_manifest(cache)
    assert "stub.md" in [p["file"] for p in ws._search("kelly", cache)["pages"]]
    ws._audit(cache, apply=True)
    out = ws._search("kelly", cache)
    assert out["index"]["removed"] == 1 and out["warnings"] == []
    assert "stub.md" not in [p["file"] for p in out["pages"]]


def test_corrupt_search_index_is_reported_and_never_raised(tmp_path):
    """A search_index.sqlite that is not a database is replaced by an
    in-memory index for the call, with a warning; damaged rows inside a
    valid database give an index_error result, not an exception."""
    import sqlite3
    cache = search_cache_dir(tmp_path)
    expected = ws._search("kelly", cache)["pages"]
    db = Path(cache, ws.SEARCH_INDEX_DB)
    db.write_bytes(b"this is not a sqlite database " * 200)
    out = ws._search("kelly", cache)
    assert out["status"] == "ok" and out["pages"] == expected and any("unusable" in w for w in out["warnings"])
    db.unlink()
    ws._search("kelly", cache)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE pages SET windows = 'not json'")
    conn.commit()
    conn.close()
    out = ws._search("kelly", cache)
    assert out["status"] == "error" and out["error"]["kind"] == "index_error"
    assert ws.SEARCH_INDEX_DB in out["error"]["message"]


def test_unreadable_page_is_a_warning_in_search_and_an_error_in_ranges(tmp_path, jevserver):
    """One page the server cannot read (mode 000) must not take down
    search_cache for the rest of the cache, and is that source's error in
    find_relevant_ranges."""
    cache = search_cache_dir(tmp_path)
    bad = Path(cache, "c.md")
    bad.chmod(0)
    try:
        out = ws._search("kelly", cache)
        assert out["status"] == "ok" and [p["file"] for p in out["pages"]] == ["a.md", "b.md"]
        assert any("c.md" in w and "PermissionError" in w for w in out["warnings"])
        r, = json.loads(ws.find_relevant_ranges("Kelly sizing?", [str(bad)]))
        assert r["status"] == "error" and r["error"]["kind"] == "unreadable" and jevserver.requests == []
    finally:
        bad.chmod(0o644)


def test_ranges_fetch_with_an_unreadable_page_in_the_cache_does_not_raise(cache, jina, jevserver):
    """find_relevant_ranges rebuilds the manifests after a fetch; a page it
    cannot read in the same cache stops the rebuild, which is a warning on
    the results, as in read_url, not an exception out of the tool."""
    bad = Path(write_page(cache, "locked.md", fj.FILLER * 3))
    bad.chmod(0)
    try:
        r, = json.loads(ws.find_relevant_ranges("How are widgets installed?", [URL], cache_dir=cache))
        assert r["status"] == "ok" and any("manifest not rebuilt" in w for w in r["warnings"])
        assert json.loads(ws.read_url(URL2, cache))["status"] == "ok"
    finally:
        bad.chmod(0o644)


def test_search_jev_denies_star_worktree_and_plain_config_names(tmp_path, jevserver):
    """Every form of a config entry (a star entry, a worktree of a plain
    name, a plain name) stops search_cache(jev=True) too."""
    for project in ("journal_2026", "second_proj-wt-tax", "private_proj"):
        cache = tmp_path / project / ".web_cache"
        write_page(cache, "p.md", sections(["[[p=0.9]] private notes."]))
        out = ws._search("private notes", str(cache), jev=True)
        assert out["status"] == "denied" and out["pages"], project
    assert jevserver.requests == []


def test_search_jev_does_not_send_a_page_linked_from_a_denied_project(tmp_path, jevserver):
    """find_relevant_ranges refuses a page file that is a link into a denied
    project's cache; search_cache(jev=True) must refuse it too, and send
    nothing, while the lexical result still lists it."""
    real = write_page(tmp_path / "private_proj" / ".web_cache", "p.md", sections(["[[p=0.9]] ledger entries for march."]))
    cache = tmp_path / "open" / ".web_cache"
    write_page(cache, "q.md", sections(["[[p=0.9]] ledger entries for sale."]))
    (cache / "link.md").symlink_to(real)
    out = ws._search("ledger entries", str(cache), jev=True)
    assert "link.md" in [p["file"] for p in out["pages"]]
    assert out["status"] == "denied" and "'private_proj'" in out["reason"] and jevserver.requests == []


def test_search_jev_ignores_answers_it_did_not_ask_for_and_fails_on_missing_ones(tmp_path, jevserver):
    """An answer for a window id that was not asked is ignored, never
    attached to another window; an asked window with no answer makes the
    batch malformed, loudly."""
    cache = rerank_cache(tmp_path)
    jevserver.default = {"mutate": lambda answers: {**answers, "w999": {"type": "noul", "noul": 1.0}}}
    out = ws._search("widget", cache, jev=True)
    assert out["status"] == "ok" and [p["file"] for p in out["pages"]] == ["y.md", "x.md"]
    assert all(w[3] != 1.0 for p in out["pages"] for w in p["windows"])
    for f in Path(cache).glob("jev_answers.sqlite*"):
        f.unlink()
    jevserver.default = {"mutate": lambda answers: {("w999" if k == min(answers) else k): v
                                                     for k, v in answers.items()}}
    out = ws._search("widget", cache, jev=True)
    assert out["status"] in ("error", "partial") and out["error"]["kind"] == "malformed"


def test_audit_through_a_symlinked_cache_dir(tmp_path):
    """A project whose .web_cache is a link is audited in the real directory,
    the link stays a link, and a second run is a no-op."""
    real = audit_cache_dir(tmp_path, "real_cache")
    link = tmp_path / "proj" / ".web_cache"
    link.parent.mkdir()
    link.symlink_to(real)
    out = ws._audit(str(link), apply=True)
    assert out["status"] == "ok" and len(out["moved"]) == 2 and out["marked_thin"] == ["thin.md"]
    assert Path(real, "_quarantine", "challenge.md").exists() and link.is_symlink()
    assert ws._audit(str(link), apply=True)["moved"] == []


def test_audit_reports_permission_errors_instead_of_raising(tmp_path):
    """An unreadable page, a read-only directory and a directory that cannot
    be listed are errors in the report (status partial or error), never an
    exception out of audit_cache; the other pages are still handled."""
    cache = audit_cache_dir(tmp_path / "one")
    locked = Path(cache, "ok.md")
    locked.chmod(0)
    try:
        out = ws._audit(str(cache), apply=True)
        assert out["status"] == "partial" and len(out["moved"]) == 2
        assert any(e.startswith("ok.md: PermissionError") for e in out["errors"])
    finally:
        locked.chmod(0o644)
    readonly = audit_cache_dir(tmp_path / "two")
    readonly.chmod(0o555)
    try:
        out = ws._audit(str(readonly), apply=True)
        assert out["status"] == "partial" and out["moved"] == [] and out["marked_thin"] == []
        assert len(out["errors"]) == 4  # two moves, one thin mark, the manifest
    finally:
        readonly.chmod(0o755)
    unlisted = audit_cache_dir(tmp_path / "three")
    unlisted.chmod(0o333)
    try:
        out = json.loads(ws.audit_cache(str(unlisted)))
        assert out["status"] == "error" and "PermissionError" in out["error"]["message"]
        assert ws._search("kelly", str(unlisted))["status"] == "error"
    finally:
        unlisted.chmod(0o755)


def test_audit_dates_the_sidecar_of_a_stub_without_fetched_now(tmp_path, clock):
    """A stub with no fetched: time is still quarantined; its sidecar is
    dated at the audit, so the URL is retried 24 hours later."""
    cache = tmp_path / ".web_cache"
    path = Path(write_page(cache, "s.md", "Performing security verification.\n", title="Just a moment...",
                           url="https://example.com/s"))
    path.write_text(path.read_text().replace("fetched: 2026-10-01T00:00:00+00:00\n", ""))
    out = ws._audit(str(cache), apply=True)
    assert [m["file"] for m in out["moved"]] == ["s.md"]
    assert json.loads(Path(sidecar_path(str(cache), "https://example.com/s")).read_text())["at"] == ws._iso(clock.t)


def test_concurrent_batch_reads_never_corrupt_the_manifest(cache, jina):
    """Eight callers fetching into one cache at once (two sessions on one
    project) each rebuild the manifests. Every write is whole: both files
    parse, no temporary file is left, and search_cache sees every page even
    when the last rebuild listed the directory before another caller's page
    landed."""
    import threading
    batches = [[f"https://example.com/t{t}/p{k}" for k in range(6)] for t in range(8)]
    errors = []

    def run(batch):
        try:
            assert all(r["status"] == "ok" for r in json.loads(ws.batch_read_urls(batch, cache)))
        except Exception as e:  # collected and asserted below
            errors.append(e)

    threads = [threading.Thread(target=run, args=(b,)) for b in batches]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    json.loads(Path(cache, "manifest.json").read_text())
    assert Path(cache, "manifest.md").read_text().rstrip().endswith("blocked.**")
    assert not list(Path(cache).glob(".tmp-*"))
    assert ws._search("widgetctl", cache, top_k=100)["index"]["pages"] == 48


def test_retry_after_100_is_capped_at_30_and_two_429s_are_an_error(cache, jina, sleeps):
    """A long Retry-After costs at most 30 s; a second 429 ends the fetch as
    an error after two attempts, with no wait after the last one, and
    leaves no page or sidecar (a rate limit is not a block)."""
    jina.script = [{"status": 429, "headers": {"Retry-After": "100"}}] * 2
    r = ws._fetch(URL, cache)
    assert r["status"] == "error" and r["attempts"] == 2 and r["http_status"] == 429 and sleeps == [30.0]
    assert "attempt 1: HTTP 429" in r["reason"] and "attempt 2: HTTP 429" in r["reason"]
    assert os.listdir(cache) == []


def test_retry_after_http_date_without_a_zone_is_read_as_utc(clock):
    """An HTTP date in Retry-After with the zone -0000 is UTC; reading it as
    local time made the wait 0 s or 30 s depending on the machine's zone."""
    from email.utils import formatdate
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Tokyo"
    time.tzset()
    try:
        value = formatdate(clock.t + 12)
        assert value.endswith("-0000") and 10.0 <= ws._retry_after({"Retry-After": value}) <= 12.0
    finally:
        if old is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = old
        time.tzset()


def test_line_numbers_after_thin_marking_match_read(tmp_path, jevserver):
    """Marking a page thin adds a frontmatter line and moves its body down
    by one. search_cache and find_relevant_ranges read the file as it is
    now, so their line numbers point at the right text as Read numbers it;
    ranges handed out before the audit are one line off."""
    cache = tmp_path / ".web_cache"
    path = write_page(cache, "t.md", "## Zebra\n\n[[p=0.9]] zebra crossing facts.\n", title="Tiny")
    before = ws._relevance("Where are zebra facts?", [path])[0]["ranges"]
    ws._search("zebra", str(cache))
    assert ws._audit(str(cache), apply=True)["marked_thin"] == ["t.md"]
    lines = Path(path).read_text().split("\n")
    (start, end, _), = ws._search("zebra", str(cache))["pages"][0]["windows"]
    assert lines[start - 1] == "## Zebra" and "zebra crossing" in lines[end - 1]
    after = ws._relevance("Where are zebra facts?", [path])[0]
    (a, b), = after["ranges"]
    assert lines[a - 1] == "## Zebra" and "zebra crossing" in lines[b - 1]
    assert after["ranges"] == [[x + 1 for x in r] for r in before]


def test_manifest_tolerates_a_non_ascii_digit_in_bytes(tmp_path):
    """str.isdigit accepts '²', which int() rejects; a hand-edited bytes:
    field must not stop the manifest rebuild."""
    cache = tmp_path / ".web_cache"
    write_page(cache, "p.md", fj.FILLER * 3, extra="bytes: ²\n")
    ws._update_manifest(str(cache))
    assert json.loads(Path(cache, "manifest.json").read_text())[0]["bytes"] > 0


def test_search_and_audit_errors_are_the_same_through_cli_and_mcp(tmp_path, monkeypatch, capsys):
    """The error paths share one code path too: a usage error and a missing
    directory print what the MCP tools return, with the documented exit codes."""
    cache = search_cache_dir(tmp_path)
    via_mcp = call_mcp("search_cache", {"query": "kelly", "cache_dir": cache, "top_k": 0})
    code, via_cli = cli(monkeypatch, capsys, "search", "kelly", "--cache-dir", cache, "--top-k", "0")
    assert without(via_mcp, "elapsed_ms") == without(via_cli, "elapsed_ms") and code == 2
    missing = str(tmp_path / "nowhere")
    via_mcp = call_mcp("audit_cache", {"cache_dir": missing})
    code, via_cli = cli(monkeypatch, capsys, "audit", missing)
    assert via_mcp == via_cli and code == 1 and via_cli["error"]["kind"] == "not_found"


def test_deeply_nested_json_body_is_classified_not_raised(tmp_path):
    """The Jina-error check parses a body that starts with {; a body nested
    too deep for the parser must still be classified, and the audit must
    not raise on such a page."""
    deep = "{" + '"a":{' * 100_000 + "}" * 100_001
    assert ws._classify_body(URL, deep)[0] == "ok"
    write_page(tmp_path / ".web_cache", "deep.md", deep)
    assert ws._audit(str(tmp_path / ".web_cache"))["counts"]["ok"] == 1


def test_url_with_a_space_fails_once_without_a_retry(cache, jina, sleeps):
    """urllib refuses a URL with a space before sending anything; that is
    not transient, so there is no 2-second wait and no second attempt."""
    r = ws._fetch("https://example.com/a b", cache)
    assert r["status"] == "error" and r["attempts"] == 1 and "InvalidURL" in r["reason"]
    assert sleeps == [] and jina.requests == []


def test_sidecar_removed_by_a_concurrent_fetch_does_not_fail_this_fetch(cache, jina, clock, monkeypatch):
    """Two fetches of one expired blocked URL can both succeed; the second
    to remove the sidecar finds it gone. That is not this fetch's error:
    its page was written."""
    jina.default = {"body": fj.CHALLENGE}
    ws._fetch(URL, cache)
    clock.t += ws.BLOCKED_TTL_S + 1
    jina.default = {}
    real_remove = os.remove

    def raced(path, *a, **k):
        if path.endswith(".blocked.json"):
            real_remove(path)  # the other fetch removes it first
        return real_remove(path, *a, **k)

    monkeypatch.setattr(ws.os, "remove", raced)
    r = ws._fetch(URL, cache)
    monkeypatch.setattr(ws.os, "remove", real_remove)
    assert r["status"] == "ok" and os.path.exists(page_path(cache)) and not os.path.exists(sidecar_path(cache))

