"""find_relevant_ranges: windowing, batching, ranges, the Jev client contract,
the answer cache and the MCP/CLI surface (docs/jev-relevance-spec.md, section 16).

Hermetic: web-sieve.py is loaded with SourceFileLoader and uses the real jev
client through _load_jev(); only the HTTP endpoint is fake (JEV_API_BASE
points at tests/fake_jev.py). Keys are fake, and `security` and
`secret-tool` are stubs first on PATH, so the real keychain is never read.
If the jev client cannot be loaded, every test fails rather than skips.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import shutil
import sqlite3
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_jev import FakeJev  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
KEY = "test-key-9d2e41"
FAST = {"attempts": 2, "first_s": 0.01, "cap_s": 0.01, "deadline_s": 2.0}
QUESTION = "How do I install a widget?"


def load_web_sieve():
    loader = SourceFileLoader("web_sieve", str(ROOT / "web-sieve.py"))
    module = module_from_spec(spec_from_loader("web_sieve", loader))
    loader.exec_module(module)
    return module


ws = load_web_sieve()


# ── Fixtures and helpers ──────────────────────────────────────────


@pytest.fixture(scope="session")
def jev():
    try:
        return ws._load_jev()
    except ws._SourceError as e:
        pytest.fail(f"the jev client could not be loaded, so the contract tests cannot run: {e.message}")


@pytest.fixture
def server():
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


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path, jev, server, fakebin):
    config = tmp_path / "deny_config.json"  # an empty deny list, so no test reads the machine's own config
    config.write_text(json.dumps({"version": 1, "privacy": {"deny_projects": [], "deny_path_prefixes": []}}))
    monkeypatch.setattr(ws, "DENY_CONFIG", str(config))
    monkeypatch.setenv("JEV_API_KEY", KEY)
    monkeypatch.setenv("JEV_API_BASE", server.url)
    monkeypatch.setenv("PATH", f"{fakebin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("WEB_SIEVE_JEV", raising=False)
    monkeypatch.setattr(ws, "JEV_POLICY", FAST)
    monkeypatch.setattr(ws, "JEV_TIMEOUT_S", 0.3)


HEAD = "---\nurl: {url}\ntitle: {title}\nfetched: 2026-10-03T00:00:00+00:00\nhash: 0123456789ab\n---\n"
PREAMBLE = "Title: {title}\n\nURL Source: {url}\n\nMarkdown Content:\n"  # body starts on line 12


def make_page(tmp_path, body, name="page.md", title="Test Page", url="https://example.com/page", preamble=True):
    folder = tmp_path / ".web_cache"
    folder.mkdir(exist_ok=True)
    text = HEAD.format(url=url, title=title) + (PREAMBLE.format(url=url, title=title) if preamble else "") + body
    path = folder / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def fixture_page(tmp_path, name):
    folder = tmp_path / ".web_cache"
    folder.mkdir(exist_ok=True)
    shutil.copy(FIXTURES / name, folder / name)
    return str(folder / name)


def words(tokens, seed=0):
    """Plain ASCII text of about `tokens` estimated tokens, on one line."""
    vocab = ["alpha", "bravo", "delta", "gamma", "omega", "sigma", "kappa", "theta"]
    out, n = [], 0
    while n < tokens * ws.CHARS_PER_TOKEN:
        w = vocab[(seed + len(out)) % len(vocab)]
        out.append(w)
        n += len(w) + 1
    return " ".join(out)


def sections(probs, tokens=100, level="##"):
    """One section per probability: a heading and a paragraph of about
    `tokens` tokens carrying the marker. At the default 200-token windows
    each section is exactly one window (above the 40-token minimum, below
    the target)."""
    out = []
    for k, p in enumerate(probs, 1):
        marker = f"[[p={p}]] " if p is not None else ""
        out.append(f"{level} Section {k}\n\n{marker}{words(tokens, k)}\n\n")
    return "".join(out)


def windows_of(path, window_tokens=None, strip_links=True):
    window_tokens = ws.WINDOW_TOKENS if window_tokens is None else window_tokens
    lines, body_start, _ = ws._read_cached_page(path)
    return lines, body_start, ws._windows(lines, body_start, window_tokens, strip_links=strip_links)


def run(question, sources, **kw):
    return ws._relevance(question, sources, **kw)


def one(question, source, **kw):
    return run(question, [source], **kw)[0]


def trim(lines, start, end):
    while not lines[start - 1].strip():
        start += 1
    while not lines[end - 1].strip():
        end -= 1
    return [start, end]


def assert_partition(lines, body_start, wins, window_tokens):
    """Windows cover every body line exactly once, in order; segments of a
    split line cover every character of it; no window exceeds the maximum."""
    if not any(x.strip() for x in lines[body_start - 1:]):
        assert wins == []
        return
    expected, i = body_start, 0
    while i < len(wins):
        w = wins[i]
        assert w["start"] == expected, f"{w['id']} starts at {w['start']}, expected {expected}"
        assert w["tokens"] <= 2 * window_tokens
        if "span" in w:
            line, segs = w["line"], []
            while i < len(wins) and wins[i].get("line") == line:
                segs.append(wins[i])
                i += 1
            pos = 0
            for s in segs:
                assert s["span"][0] == pos
                pos = s["span"][1]
            assert pos == len(lines[line - 1])
            expected = segs[-1]["end"] + 1
        else:
            assert any(lines[j - 1].strip() for j in range(w["start"], w["end"] + 1)), f"{w['id']} is blank only"
            expected = w["end"] + 1
            i += 1
    assert expected == len(lines) + 1


def cli(monkeypatch, capsys, *args):
    """Run `web-sieve ranges ...` in this process; (exit code, parsed stdout)."""
    monkeypatch.setattr(sys, "argv", ["web-sieve", "ranges", *args])
    with pytest.raises(SystemExit) as info:
        ws._cli()
    return info.value.code, json.loads(capsys.readouterr().out)


# ── Windowing (pure functions) ────────────────────────────────────


def test_frontmatter_and_preamble_are_never_in_a_window(tmp_path):
    """Frontmatter and the Jina preamble are metadata, not page content:
    sending them costs tokens, and a range over them would point Read at
    lines that cannot answer anything."""
    path = fixture_page(tmp_path, "standard.md")
    lines, body_start, wins = windows_of(path)
    assert lines[10].startswith("Markdown Content:") and body_start == 12
    assert wins[0]["start"] == 12 and all(w["start"] >= 12 for w in wins)
    sent = "\n".join(w["text"] for w in wins)
    assert "URL Source:" not in sent and "fetched:" not in sent and "hash:" not in sent


def test_body_starts_after_frontmatter_without_preamble(tmp_path):
    """16 cached pages have no `Markdown Content:` line; their body starts
    right after the frontmatter. A `Markdown Content:` line far into the body
    is page text, not preamble, and must not move the body start."""
    path = fixture_page(tmp_path, "no_preamble.md")
    lines, body_start, wins = windows_of(path)
    assert body_start == 7 and lines[6] == "# Plain notes" and wins[0]["start"] == 7
    late = make_page(tmp_path, "\n".join(f"line {k}" for k in range(20)) + "\nMarkdown Content: quoted\nmore\n",
                     name="late.md", preamble=False)
    assert ws._read_cached_page(late)[1] == 7


@pytest.mark.parametrize("window_tokens", [50, 400, 4000])
def test_windows_cover_every_body_line_exactly_once(tmp_path, window_tokens):
    """A line in no window is never judged, so a gap would silently drop
    content; a line in two windows would get two probabilities."""
    names = sorted(p.name for p in FIXTURES.glob("*.md"))
    assert len(names) >= 5
    for name in names:
        lines, body_start, wins = windows_of(fixture_page(tmp_path, name), window_tokens)
        assert_partition(lines, body_start, wins, window_tokens)


@pytest.mark.parametrize("heading", ["## Second part", "Second part\n-----------"], ids=["atx", "setext"])
def test_heading_starts_a_window_once_minimum_reached(tmp_path, heading):
    """A window that starts at a heading carries its own topic; one that
    straddles two sections mixes them and blurs the probability."""
    path = make_page(tmp_path, f"{words(120)}\n\n{heading}\n\n{words(120, 3)}\n")
    lines, _, wins = windows_of(path)
    heading_line = next(n for n in range(12, len(lines) + 1) if lines[n - 1].startswith(("## Second", "Second")))
    assert [w["start"] for w in wins] == [12, heading_line]
    assert wins[1]["section"] == "Second part"


def test_short_section_does_not_close_below_minimum(tmp_path):
    """A window under the minimum would be a near-empty request entry that
    Jev cannot judge on its own; it stays with the following heading."""
    path = make_page(tmp_path, f"{words(30)}\n\n## Next\n\n{words(100, 2)}\n")
    _, _, wins = windows_of(path)
    assert len(wins) == 1 and wins[0]["start"] == 12


def test_heading_path_pops_by_level(tmp_path):
    """The section label is the context Jev gets for a window; a stale
    sibling heading (A > C) would describe the wrong section."""
    body = f"## A\n\n{words(100)}\n\n### B\n\n{words(100, 1)}\n\n## C\n\n{words(100, 2)}\n"
    _, _, wins = windows_of(make_page(tmp_path, body))
    assert [w["section"] for w in wins] == ["A", "A > B", "C"]


def test_no_split_inside_code_fence_or_table_below_maximum(tmp_path):
    """Half a code block or half a table cannot be judged or read on its
    own, so a blank line inside one must not end a window below the maximum."""
    code = "\n\n".join(f"run_step_{k} --flag {words(10, k)}" for k in range(8))    # about 120 tokens
    table = "\n\n".join(f"| row {k} | {words(10, k)} |" for k in range(10))       # about 140 tokens, blank-separated
    body = f"{words(30)}\n\n```bash\n{code}\n```\n\n{table}\n\nAfter the table.\n"
    path = make_page(tmp_path, body)
    lines, body_start, wins = windows_of(path, window_tokens=100)
    assert_partition(lines, body_start, wins, 100)
    open_fence = lines.index("```bash") + 1
    close_fence = len(lines) - lines[::-1].index("```")
    rows = [n for n in range(1, len(lines) + 1) if lines[n - 1].startswith("| row")]

    def window_of(n):
        return next(w["id"] for w in wins if w["start"] <= n <= w["end"])

    assert window_of(open_fence) == window_of(close_fence)
    assert window_of(rows[0]) == window_of(rows[-1])


def test_fence_or_table_over_maximum_is_split(tmp_path):
    """The maximum protects the request budget; a block larger than it must
    still be split rather than sent as one oversized window."""
    code = "\n".join(f"step_{k} {words(12, k)}" for k in range(60))          # about 900 tokens
    table = "\n".join(f"| row {k} | {words(12, k)} |" for k in range(60))
    lines, body_start, wins = windows_of(make_page(tmp_path, f"```\n{code}\n```\n\n{table}\n"), window_tokens=100)
    assert_partition(lines, body_start, wins, 100)
    assert len(wins) >= 8 and all(w["tokens"] <= 200 for w in wins)


def test_oversize_line_is_split_into_segments_covering_every_character(tmp_path, server):
    """Some cached lines exceed Jev's whole state budget; dropping or
    truncating them would hide content, so every character is sent once."""
    path = fixture_page(tmp_path, "long_line.md")
    lines, body_start, wins = windows_of(path, strip_links=False)
    n = next(k for k in range(1, len(lines) + 1) if len(lines[k - 1]) > 9000)
    segs = [w for w in wins if w.get("line") == n]
    assert len(segs) >= 3
    assert "".join(lines[n - 1][a:b] for a, b in (s["span"] for s in segs)) == lines[n - 1]
    assert all(s["start"] == s["end"] == n and s["tokens"] <= 2 * ws.WINDOW_TOKENS for s in segs)
    assert all(lines[n - 1][s["span"][1] - 1] == " " for s in segs[:-1])  # cut at whitespace
    assert_partition(lines, body_start, wins, ws.WINDOW_TOKENS)
    r = one(QUESTION, path)
    assert r["split_lines"] == {str(n): [s["span"] for s in segs]}
    assert [w[:2] for w in r["windows"] if w[0] == w[1] == n] == [[n, n]] * len(segs)


def test_trailing_small_window_merges_into_previous(tmp_path):
    """A tiny last window costs a Noul and gets judged without context; it
    joins the window before it when the sum fits."""
    body = f"{words(110)}\n\n{words(110, 1)}\n\n{words(10, 2)}\n"
    lines, _, wins = windows_of(make_page(tmp_path, body), window_tokens=100)
    assert len(wins) == 2 and wins[-1]["end"] == len(lines)
    assert wins[-1]["text"].endswith(words(10, 2))


def test_blank_only_window_does_not_exist(tmp_path):
    """A window of blank lines would be sent and judged for nothing, and
    could form a range Read finds empty."""
    long1, long2 = words(1000), words(1000, 1)
    body = f"\n\n{long1}\n\n\n{long2}\n\n\n"
    lines, body_start, wins = windows_of(make_page(tmp_path, body))
    assert_partition(lines, body_start, wins, ws.WINDOW_TOKENS)
    for w in wins:
        assert any(lines[j - 1].strip() for j in range(w["start"], w["end"] + 1))


def test_window_ids_are_zero_padded_and_stable(tmp_path):
    """Ids are the named keys that keep answers from leaking between
    neighbours; they must be unique, sortable and the same on every run,
    or the cache and the answers would point at the wrong windows."""
    small = make_page(tmp_path, sections([None] * 3), name="small.md")
    assert [w["id"] for w in windows_of(small)[2]] == ["w001", "w002", "w003"]
    big = make_page(tmp_path, "".join(f"{words(60, k)}\n\n" for k in range(1000)), name="big.md")
    first = windows_of(big, window_tokens=50)[2]
    assert len(first) == 1000 and first[0]["id"] == "w0001" and first[-1]["id"] == "w1000"
    second = windows_of(big, window_tokens=50)[2]
    assert [(w["id"], w["start"], w["text"]) for w in first] == [(w["id"], w["start"], w["text"]) for w in second]


def test_link_reduction_changes_text_not_line_numbers(tmp_path):
    """Link targets cost tokens and add unrelated detail Jev is weaker on;
    reducing them must not move any line, because ranges refer to the file."""
    body = ("## Links\n\nSee the [install guide](https://example.com/install) for details.\n"
            "![Diagram of the widget](https://example.com/d.png)\n"
            "![](https://example.com/blank.png)\n"
            "[![Logo](https://example.com/logo.png)](https://example.com/home)\n"
            "Read [Foo (bar)](https://en.wikipedia.org/wiki/Foo_(bar)) next.\n"
            "Bare https://example.com/bare stays.\n")
    path = make_page(tmp_path, body)
    _, _, reduced = windows_of(path, strip_links=True)
    _, _, kept = windows_of(path, strip_links=False)
    assert [(w["start"], w["end"]) for w in reduced] == [(w["start"], w["end"]) for w in kept]
    text = reduced[0]["text"]
    assert "See the install guide for details." in text
    assert "[image: Diagram of the widget]" in text and "\n[image]\n" in text
    assert "\n[image: Logo]\n" in text and "Read Foo (bar) next." in text
    assert "https://example.com/bare" in text and "](" not in text
    assert "](https://example.com/install)" in kept[0]["text"]


def test_non_ascii_text_is_sized_conservatively(tmp_path):
    """CJK text uses far fewer characters per token; counting it as ASCII
    would build requests over Jev's limit, which fail as client errors."""
    cjk = fixture_page(tmp_path, "cjk.md")
    _, _, cjk_wins = windows_of(cjk)
    ascii_page = make_page(tmp_path, sections([None] * 30, tokens=300), name="ascii.md")
    _, _, ascii_wins = windows_of(ascii_page)
    limit = 2 * ws.WINDOW_TOKENS  # the maximum window in tokens: at most that many CJK characters
    assert max(len(w["text"]) for w in cjk_wins) <= limit < max(len(w["text"]) for w in ascii_wins)
    lines, body_start, big = windows_of(cjk, window_tokens=4000)
    batches = ws._batches(big, QUESTION, {"title": "小部件手册", "url": "u"})
    assert len(batches) > math.ceil(len(big) / ws.WINDOWS_PER_REQUEST)
    for b in batches:
        state = ws._est_tokens(json.dumps(b["request"]["state"], ensure_ascii=False))
        qs = [ws._est_tokens(json.dumps({k: q}, ensure_ascii=False)) for k, q in b["request"]["questions"].items()]
        assert state + max(qs) <= ws.STATE_TOKEN_BUDGET and state + sum(qs) <= ws.REQUEST_TOKEN_BUDGET
    assert [i for b in batches for i in b["ids"]] == [w["id"] for w in big]


def test_empty_body_is_ok_and_not_relevant_with_warning(tmp_path, server):
    """A blocked or challenge page has nothing to judge: that is a complete
    answer (ok, not relevant), but the caller is told the body was empty."""
    r = one(QUESTION, fixture_page(tmp_path, "empty_body.md"))
    assert r["status"] == "ok" and r["relevant"] is False and r["windows"] == []
    assert "page body is empty" in r["warnings"] and not server.requests


# ── Batching ──────────────────────────────────────────────────────


def test_requests_hold_16_windows_with_matching_question_ids(tmp_path, server):
    """One Noul per window under the same named key: a question id that
    does not match a state key would ask Jev about a window it cannot see."""
    r = one(QUESTION, make_page(tmp_path, sections([None] * 40)))
    assert r["status"] == "ok" and r["requests"] == 3 == len(server.requests)
    sizes = sorted(len(req["body"]["questions"]) for req in server.requests)
    assert sizes == [8, 16, 16]
    for req in server.requests:
        assert set(req["body"]["questions"]) == set(req["body"]["state"]["windows"])
    assert sorted(i for ids in server.asked_ids() for i in ids) == [f"w{k:03d}" for k in range(1, 41)]


def test_every_request_is_within_budget_and_no_window_is_dropped(tmp_path, server):
    """Large windows must shrink batches, not drop windows: a dropped window
    would be reported as judged when it was never asked."""
    path = make_page(tmp_path, sections([None] * 20, tokens=5000))
    r = one(QUESTION, path, window_tokens=4000)
    assert r["status"] == "ok" and len(r["windows"]) == 20 and len(server.requests) > 2
    for req in server.requests:
        body = req["body"]
        state = ws._est_tokens(json.dumps(body["state"], ensure_ascii=False))
        qs = [ws._est_tokens(json.dumps({k: q}, ensure_ascii=False)) for k, q in body["questions"].items()]
        assert state + max(qs) <= ws.STATE_TOKEN_BUDGET and state + sum(qs) <= ws.REQUEST_TOKEN_BUDGET
    assert sorted(i for ids in server.asked_ids() for i in ids) == [f"w{k:03d}" for k in range(1, 21)]


def test_too_many_windows_is_an_error_and_sends_nothing(tmp_path, server):
    """A page past max_windows would cost many requests; it is refused
    before any is sent, and the caller is told what it would have needed."""
    r = one(QUESTION, make_page(tmp_path, sections([None] * 5)), max_windows=2)
    assert r["status"] == "error" and r["error"]["kind"] == "too_many_windows" and r["relevant"] is None
    assert "5 windows" in r["error"]["message"] and "1 requests" in r["error"]["message"]
    assert r["unjudged"] == [{"lines": [12, r["file_lines"]], "reason": "too_many_windows"}]
    assert not server.requests and ws._exit_code([r]) == 1


# ── Ranges and thresholds ─────────────────────────────────────────


def test_consecutive_selected_windows_merge_and_gaps_stay_separate(tmp_path):
    """Adjacent relevant windows are one passage and read best as one range;
    an irrelevant window between them must stay out so it is not read."""
    path = make_page(tmp_path, sections([0.9, 0.9, 0.1, 0.9, 0.1, 0.9]))
    lines, _, wins = windows_of(path)
    r = one(QUESTION, path)
    assert r["ranges"] == [trim(lines, wins[0]["start"], wins[1]["end"]),
                           trim(lines, wins[3]["start"], wins[3]["end"]),
                           trim(lines, wins[5]["start"], wins[5]["end"])]
    assert [d["lines"] for d in r["range_detail"]] == r["ranges"]
    assert [d["section"] for d in r["range_detail"]] == ["Section 1", "Section 4", "Section 6"]


def test_ranges_trim_blank_lines_and_match_file_lines(tmp_path):
    """Callers pass a range straight to Read(offset=start, limit=end-start+1);
    it must read exactly the selected windows' lines, without blank edges."""
    path = make_page(tmp_path, sections([0.1, 0.9, 0.9, 0.1]))
    lines, _, wins = windows_of(path, strip_links=False)
    (start, end), = one(QUESTION, path)["ranges"]
    read = lines[start - 1:start - 1 + (end - start + 1)]
    assert read[0].strip() and read[-1].strip()
    expected = [x for w in wins[1:3] for x in w["text"].split("\n") if x]
    assert [x for x in read if x.strip()] == expected


def test_threshold_equality_selects(tmp_path):
    """The rule is p >= threshold; an off-by-epsilon rule would drop windows
    exactly at the calibrated value."""
    path = make_page(tmp_path, sections([0.5, 0.4999]))
    lines, _, wins = windows_of(path)
    r = one(QUESTION, path, threshold=0.5)
    assert r["ranges"] == [trim(lines, wins[0]["start"], wins[0]["end"])] and r["threshold"] == 0.5
    assert len(one(QUESTION, path, threshold=0.4999)["ranges"]) == 1  # both selected, merged into one range
    assert one(QUESTION, path, threshold=0.4999)["lines_selected"] > r["lines_selected"]


def test_relevant_false_only_when_status_ok(tmp_path, server):
    """`relevant: false` tells the caller to skip a page; saying it about a
    page Jev did not fully judge would hide content the caller needs."""
    low = one(QUESTION, make_page(tmp_path, sections([0.1] * 3), name="low.md"))
    assert low["status"] == "ok" and low["relevant"] is False
    server.rule = lambda body: {"status": 422, "body": {"detail": "no"}} if "w017" in body["questions"] else None
    part = one(QUESTION, make_page(tmp_path, sections([0.1] * 20), name="part.md"))
    assert part["status"] == "partial" and part["relevant"] is None
    server.rule = lambda body: {"status": 422, "body": {"detail": "no"}}
    err = one(QUESTION, make_page(tmp_path, sections([0.2] * 3), name="err.md"))  # not cached by the first call
    assert err["status"] == "error" and err["relevant"] is None
    for r in (low, part, err):
        assert r["relevant"] is not False or r["status"] == "ok"


def test_selected_segment_selects_its_whole_line_once(tmp_path):
    """A split line is read as one line; two selected segments must not
    produce the same line twice, and one selected segment selects it."""
    line = "[[p=0.9]] " + words(int(4.9 * ws.WINDOW_TOKENS)) + " [[p=0.9]]"  # three segments of at most 2x
    path = make_page(tmp_path, f"## Long\n\n{words(100)}\n\n{line}\n\n## Other\n\n{words(100, 4)}\n")
    lines, _, wins = windows_of(path)
    n = lines.index(line) + 1
    r = one(QUESTION, path)
    assert [w[2] for w in r["windows"] if w[0] == w[1] == n] == [0.9, 0.05, 0.9]
    assert r["ranges"] == [[n, n]] and r["lines_selected"] == 1 and r["range_detail"][0]["max_p"] == 0.9


@pytest.mark.parametrize("window_tokens", [None, 400], ids=["default-split-line", "whole-line-in-a-window"])
def test_long_lines_reported_inside_selected_ranges(tmp_path, window_tokens):
    """A caller about to Read a range should know when one line in it is
    large; lines outside the selected ranges are not its concern. At the
    default size a line over LONG_LINE_CHARS is longer than the maximum
    window, so it is split and a selected segment selects the whole line;
    at 400 tokens the same line sits whole inside a selected window."""
    selected, other = "[[p=0.9]] " + words(640), words(770, 3)
    body = f"## Keep\n\n[[p=0.9]] intro\n{selected}\n\n## Skip\n\n{other}\n"
    path = make_page(tmp_path, body)
    lines = ws._read_cached_page(path)[0]
    r = one(QUESTION, path, **({"window_tokens": window_tokens} if window_tokens else {}))
    assert r["long_lines"] == {str(lines.index(selected) + 1): len(selected)}
    assert len(selected) > ws.LONG_LINE_CHARS and len(other) > ws.LONG_LINE_CHARS


@pytest.mark.parametrize("kw", [
    {"threshold": -0.1}, {"threshold": 1.5}, {"threshold": float("nan")},
    {"window_tokens": 49}, {"window_tokens": 4001}, {"max_windows": 0},
    {"question": ""}, {"question": "   "}, {"question": "x" * 2001},
], ids=["threshold-low", "threshold-high", "threshold-nan", "window-small", "window-large", "max-windows-0",
        "question-empty", "question-blank", "question-long"])
def test_bad_threshold_or_window_size_or_question_is_a_usage_error_and_sends_nothing(tmp_path, server, kw):
    """A bad argument must fail before any request, with exit 2, and must
    not look like a judged page."""
    path = make_page(tmp_path, sections([0.9] * 2))
    args = {"question": QUESTION, **kw}
    r = run(args.pop("question"), [path], **args)[0]
    assert r["status"] == "error" and r["error"]["kind"] == "usage" and r["relevant"] is None
    assert r["unjudged"] == [{"lines": [12, r["file_lines"]], "reason": "usage"}]
    assert not server.requests and ws._exit_code([r]) == 2


# ── Client integration through the fake server ────────────────────


def test_request_shape(tmp_path, server):
    """The request is the privacy boundary: Jev gets the question, the page
    title and window text under named keys, and nothing that identifies the
    local file. The key must travel only as the bearer header."""
    path = make_page(tmp_path, sections([None] * 3), url="https://example.com/secret-url")
    assert one(QUESTION, path)["status"] == "ok"
    req, = server.requests
    body = req["body"]
    assert req["path"] == "/v1/systemone" and req["headers"]["Authorization"] == f"Bearer {KEY}"
    assert body["model"] == "jev-1.13.0" and set(body) == {"state", "model", "questions"}
    state = body["state"]
    assert set(state) == {"question", "page", "windows"} and state["question"] == QUESTION
    assert state["page"] == {"title": "Test Page"}
    assert list(state["windows"]) == ["w001", "w002", "w003"]
    for k, entry in enumerate(state["windows"].values(), 1):  # the heading travels as the first line of the text
        assert set(entry) == {"text"} and entry["text"].startswith(f"Section: Section {k}\n## Section {k}\n")
    q = body["questions"]["w002"]
    assert q["type"] == "noul" and "`windows.w002`" in q["instructions"] and set(q["criteria"]) == {"true", "false"}
    raw = json.dumps(body)
    for leak in (path, str(tmp_path), "secret-url", "Markdown Content", "fetched", KEY):
        assert leak not in raw
    untitled = make_page(tmp_path, sections([None]), name="untitled.md", title="", url="https://example.com/u")
    one(QUESTION, untitled)
    assert server.requests[-1]["body"]["state"]["page"] == {"url": "https://example.com/u"}


def test_no_key_sends_nothing_and_exits_4(tmp_path, server, monkeypatch, capsys):
    """Without a key every request would be refused; the call must stop
    before sending, say why, and exit 4 like the jev CLI."""
    monkeypatch.delenv("JEV_API_KEY")
    path = make_page(tmp_path, sections([0.9] * 2))
    r = one(QUESTION, path)
    assert r["status"] == "error" and r["error"]["kind"] == "no_key" and r["relevant"] is None
    assert r["unjudged"] == [{"lines": [12, r["file_lines"]], "reason": "no_key"}]
    code, out = cli(monkeypatch, capsys, QUESTION, path)
    assert code == 4 and out[0]["error"]["kind"] == "no_key" and not server.requests


def test_missing_client_returns_no_client_and_fetch_tools_still_work(tmp_path, server, monkeypatch):
    """The jev client lives outside this repo; when it is missing only the
    relevance tool may fail, loudly, while fetching and listing keep working."""
    missing = tmp_path / "missing-jev"
    monkeypatch.setenv("WEB_SIEVE_JEV", str(missing))
    monkeypatch.delitem(sys.modules, "web_sieve_jev")
    path = make_page(tmp_path, sections([0.9]))
    r = one(QUESTION, path)
    assert r["status"] == "error" and r["error"]["kind"] == "no_client" and str(missing) in r["error"]["message"]
    assert ws._exit_code([r]) == 1 and not server.requests and "web_sieve_jev" not in sys.modules
    incomplete = tmp_path / "incomplete-jev"
    incomplete.write_text("x = 1\n")
    monkeypatch.setenv("WEB_SIEVE_JEV", str(incomplete))
    r = one(QUESTION, path)
    assert r["error"]["kind"] == "no_client" and "lacks ask" in r["error"]["message"]
    cache = tmp_path / ".web_cache"
    url = "https://example.com/cached"
    make_page(tmp_path, "cached body\n", name=f"{ws._url_hash(url)}.md", url=url)
    fetched = json.loads(ws.read_url(url, str(cache)))
    assert fetched["cached"] is True and fetched["path"].endswith(f"{ws._url_hash(url)}.md")
    assert url in {e.get("url") for e in json.loads(ws.list_cache(str(cache)))}


def test_429_then_answer_is_ok_and_retry_is_reported(tmp_path, server):
    """A retried request that then answers is a full answer, but the retry
    must be visible, never silent."""
    server.script = [{"status": 429, "body": {"detail": "slow down"}}]
    r = one(QUESTION, make_page(tmp_path, sections([0.9, 0.1])))
    assert r["status"] == "ok" and r["requests"] == 1 and len(server.requests) == 2
    retries = [e for e in r["jev_events"] if e["event"] == "jev_retry"]
    assert retries and "HTTP 429" in retries[0]["reason"] and retries[0]["batch"] == 1


@pytest.mark.parametrize("first_p", [0.1, 0.9], ids=["no-judged-pass", "judged-pass"])
def test_429_until_give_up_is_partial_never_false(tmp_path, server, monkeypatch, capsys, first_p):
    """A batch Jev never answered is unknown, not irrelevant: the page is
    partial, relevant is null unless a judged window passed, the missing
    lines are listed, and the CLI exits 3."""
    server.rule = lambda body: ({"status": 429, "delay": 0.05, "body": {"detail": "busy"}}
                                if "w017" in body["questions"] else None)
    path = make_page(tmp_path, sections([first_p] + [0.1] * 19))
    lines, _, wins = windows_of(path)
    r = one(QUESTION, path)
    assert r["status"] == "partial" and r["error"]["kind"] == "gave_up"
    assert r["relevant"] is (True if first_p == 0.9 else None)
    assert r["unjudged"] == [{"lines": [wins[16]["start"], wins[19]["end"]], "reason": "gave_up"}]
    assert [w[2] for w in r["windows"][16:]] == [None] * 4
    assert any(e["event"] == "jev_gave_up" for e in r["jev_events"])
    code, out = cli(monkeypatch, capsys, QUESTION, path)
    assert code == 3 and out[0]["status"] == "partial"


def test_timeout_until_give_up_is_reported(tmp_path, server):
    """A Jev that hangs must end in a reported give-up within the policy
    deadline, not a wait without end or a silent empty result."""
    server.default = {"delay": 1.0}
    r = one(QUESTION, make_page(tmp_path, sections([0.9])))
    assert r["status"] == "error" and r["error"]["kind"] == "gave_up" and r["relevant"] is None
    assert "timed out" in r["error"]["message"] or "timeout" in r["error"]["message"]
    assert any(e["event"] == "jev_gave_up" for e in r["jev_events"])


def test_client_error_is_not_retried_and_stops_further_batches(tmp_path, server, monkeypatch, capsys):
    """A 422 repeats on every request; sending more after it wastes money
    and hides the fault. One request in flight makes the order exact."""
    monkeypatch.setattr(ws, "JEV_CONCURRENCY", 1)
    server.rule = lambda body: {"status": 422, "body": {"detail": "bad question"}}
    first = make_page(tmp_path, sections([0.9] * 40), name="first.md")
    second = make_page(tmp_path, sections([0.9] * 3), name="second.md")
    lines, _, wins = windows_of(first)
    r1, r2 = run(QUESTION, [first, second])
    assert len(server.requests) == 1
    assert r1["status"] == "error" and r1["error"]["kind"] == "client_error"
    assert "HTTP 422" in r1["error"]["message"] and "bad question" in r1["error"]["message"]
    assert r1["unjudged"] == [{"lines": [12, wins[15]["end"]], "reason": "client_error"},
                              {"lines": [wins[16]["start"], wins[39]["end"]], "reason": "not_sent_after_failure"}]
    assert r2["status"] == "error" and r2["error"]["kind"] == "not_sent_after_failure"
    assert "HTTP 422" in r2["error"]["message"]
    server.requests.clear()
    code, _ = cli(monkeypatch, capsys, QUESTION, first, second)
    assert code == 2 and len(server.requests) == 1


def _drop(a):
    a.pop("w002")
    return a


@pytest.mark.parametrize("mutate", [
    _drop,
    lambda a: {**a, "w002": {"type": "noul", "noul": "0.9"}},
    lambda a: {**a, "w002": {"type": "noul", "noul": True}},
    lambda a: {**a, "w002": {"type": "noul", "noul": float("nan")}},
    lambda a: {**a, "w002": {"type": "noul", "noul": 1.4}},
], ids=["missing", "string", "boolean", "nan", "above-one"])
def test_missing_or_invalid_noul_fails_the_whole_batch(tmp_path, server, monkeypatch, capsys, mutate):
    """One bad answer means the response cannot be trusted; keeping the
    other answers of that batch would accept a broken response in part."""
    server.default = {"mutate": mutate}
    path = make_page(tmp_path, sections([0.9] * 3))
    r = one(QUESTION, path)
    assert r["status"] == "error" and r["error"]["kind"] == "malformed" and r["relevant"] is None
    assert [w[2] for w in r["windows"]] == [None] * 3 and r["ranges"] == []
    assert len(server.requests) == 1
    code, _ = cli(monkeypatch, capsys, QUESTION, path)
    assert code == 5


def test_served_model_mismatch_is_a_warning(tmp_path, server):
    """The threshold is calibrated for jev-1.13.0; answers from another
    model are kept but the caller must be told."""
    server.default = {"model": "jev-1.14.0"}
    r = one(QUESTION, make_page(tmp_path, sections([0.9])))
    assert r["status"] == "ok"
    assert any("jev-1.14.0" in w and "jev-1.13.0" in w for w in r["warnings"])


# ── Cache ─────────────────────────────────────────────────────────


def test_second_identical_call_sends_no_requests(tmp_path, server):
    """Re-asking the same question about the same page must cost nothing
    and give the same answers."""
    path = make_page(tmp_path, sections([0.9] + [0.1] * 19))
    first = one(QUESTION, path)
    second = one(QUESTION, path)
    assert first["requests"] == 2 and second["requests"] == 0 and second["cache_hits"] == 2
    assert second["windows"] == first["windows"] and second["ranges"] == first["ranges"]
    assert second["input_tokens"] == 0 and len(server.requests) == 2


def test_changed_question_model_prompt_version_or_window_size_misses(tmp_path, server, monkeypatch):
    """An answer is valid only for the question, model, prompt and batch it
    was given under; reusing it under any other would return a wrong p. A
    window size that yields different windows misses (at 2000 tokens the
    three sections become one window); one that yields identical windows
    may hit, because the key is the content actually sent."""
    path = make_page(tmp_path, sections([0.9] * 3))
    assert one(QUESTION, path)["requests"] == 1
    assert one("A different question?", path)["requests"] == 1
    assert one(QUESTION, path, window_tokens=2000)["requests"] == 1
    assert one(QUESTION, path, section_line=False)["requests"] == 1  # the same windows with the heading as a field
    monkeypatch.setattr(ws, "JEV_MODEL", "jev-1.13.1")
    assert one(QUESTION, path)["requests"] == 1
    monkeypatch.setattr(ws, "JEV_MODEL", "jev-1.13.0")
    assert one(QUESTION, path)["requests"] == 0  # the original answers are still cached
    monkeypatch.setattr(ws, "PROMPT_VERSION", ws.PROMPT_VERSION + 1)
    assert one(QUESTION, path)["requests"] == 1  # a newer prompt version misses (and prunes the old rows)


def test_partial_failure_keeps_answered_batches(tmp_path, server):
    """The cache is the saved progress of a failed call: a rerun after the
    cause is fixed pays only for the batches that failed."""
    server.rule = lambda body: ({"status": 429, "delay": 0.05, "body": {"detail": "busy"}}
                                if "w017" in body["questions"] else None)
    path = make_page(tmp_path, sections([0.9] + [0.1] * 19))
    assert one(QUESTION, path)["status"] == "partial"
    server.rule = None
    server.requests.clear()
    r = one(QUESTION, path)
    assert r["status"] == "ok" and r["requests"] == 1 and r["cache_hits"] == 1
    assert server.asked_ids() == [["w017", "w018", "w019", "w020"]]


def test_cache_file_holds_no_page_text_or_question(tmp_path):
    """The cache sits beside the page in a project directory; it must hold
    hashes and numbers only, never page text, the question or the key."""
    path = fixture_page(tmp_path, "standard.md")
    question = "QUESTION-SENTINEL-77aa: how is a widget retired?"
    r = one(question, path)
    assert r["status"] == "ok"
    db = tmp_path / ".web_cache" / ws.ANSWERS_DB
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM answers").fetchone()[0] == len(r["windows"])
    raw = b"".join(p.read_bytes() for p in db.parent.glob(ws.ANSWERS_DB + "*"))
    for secret in ("SENTINEL-4b7e2c91", "QUESTION-SENTINEL-77aa", "widgetctl", KEY):
        assert secret.encode() not in raw


def test_unwritable_cache_still_returns_results_with_warning(tmp_path):
    """A cache that cannot be written must not cost the answers, and the
    caller must learn that reruns will not be free."""
    path = make_page(tmp_path, sections([0.9, 0.1]))
    folder = tmp_path / ".web_cache"
    folder.chmod(0o555)
    try:
        r = one(QUESTION, path)
    finally:
        folder.chmod(0o755)
    assert r["status"] == "ok" and r["relevant"] is True and r["requests"] == 1
    assert any("answers cache" in w for w in r["warnings"])


# ── Surface ───────────────────────────────────────────────────────


def test_mcp_tool_returns_results_in_source_order(tmp_path, server, monkeypatch):
    """Callers match results to their sources by position; a URL is fetched
    through the cache first, a failed fetch fails only that source, and the
    manifest is rebuilt once when something was fetched."""
    cache = tmp_path / ".web_cache"
    a = make_page(tmp_path, sections([0.9]), name="a.md")
    b = make_page(tmp_path, sections([0.1]), name="b.md")
    url, bad = "https://example.com/new", "https://example.com/gone"

    def fake_fetch(u, cache_dir):
        if u == bad:
            return {"url": u, "error": "404: Not Found", "detail": "gone"}
        return {"cached": False, "path": make_page(tmp_path, sections([0.9]), name=f"{ws._url_hash(u)}.md", url=u),
                "url": u, "title": "Test Page"}

    manifests = []
    monkeypatch.setattr(ws, "_fetch_one", fake_fetch)
    monkeypatch.setattr(ws, "_update_manifest", manifests.append)
    out = json.loads(ws.find_relevant_ranges(QUESTION, [b, url, a, bad], cache_dir=str(cache)))
    assert [r["source"] for r in out] == [b, url, a, bad]
    assert [r["status"] for r in out] == ["ok", "ok", "ok", "error"]
    assert [r["relevant"] for r in out] == [False, True, True, None]
    assert out[1]["url"] == url and out[1]["path"].endswith(f"{ws._url_hash(url)}.md")
    assert out[3]["error"]["kind"] == "fetch_failed" and "404" in out[3]["error"]["message"]
    assert manifests == [str(cache)]
    names = [t.name for t in asyncio.run(ws.mcp.list_tools())]
    assert "find_relevant_ranges" in names


def test_path_outside_web_cache_or_without_frontmatter_is_refused_and_sends_nothing(tmp_path, server):
    """Only web-sieve cache pages may be sent to TypeSafe; any other local
    file, including one reached through a symlink, is refused unread."""
    other = tmp_path / "notes"
    other.mkdir()
    outside = other / "page.md"
    outside.write_text(HEAD.format(url="https://x", title="t") + "private text\n")
    folder = tmp_path / ".web_cache"
    folder.mkdir()
    plain = folder / "plain.md"
    plain.write_text("no frontmatter here\n")
    no_url = folder / "nourl.md"
    no_url.write_text("---\ntitle: t\n---\nbody\n")
    link = folder / "link.md"
    link.symlink_to(outside)
    results = run(QUESTION, [str(outside), str(plain), str(no_url), str(link), str(folder / "missing.md")])
    assert [r["error"]["kind"] for r in results] == ["not_a_cached_page"] * 4 + ["not_found"]
    assert all(r["status"] == "error" and r["relevant"] is None for r in results)
    assert not server.requests


def test_cli_ranges_prints_json_and_dispatches(tmp_path, server, fakebin, jev):
    """`web-sieve ranges` must reach the CLI rather than start the MCP
    server, print the JSON list, and exit 0 when every page is ok."""
    path = make_page(tmp_path, sections([0.9, 0.1]))
    env = {"PATH": f"{fakebin}:/usr/bin:/bin", "HOME": str(tmp_path), "JEV_API_KEY": KEY,
           "JEV_API_BASE": server.url, "WEB_SIEVE_JEV": os.path.realpath(jev.__file__),
           "PYTHONDONTWRITEBYTECODE": "1"}
    out = subprocess.run([sys.executable, str(ROOT / "web-sieve.py"), "ranges", QUESTION, path],
                         capture_output=True, text=True, env=env, timeout=60)
    assert out.returncode == 0, out.stderr
    result, = json.loads(out.stdout)
    assert result["status"] == "ok" and result["relevant"] is True and result["source"] == path
    assert KEY not in out.stdout and KEY not in out.stderr


def test_cost_and_tokens_sum_from_usage(tmp_path, server):
    """Cost is reported from Jev's own usage counts so spend is never a
    guess; three requests of 100 input tokens are 300 tokens."""
    r = one(QUESTION, make_page(tmp_path, sections([0.1] * 40)))
    assert r["requests"] == 3 and r["input_tokens"] == 300
    assert r["cost_usd"] == round(300 * 0.042 / 1_000_000, 6) and r["model"] == "jev-1.13.0"


# ── Review additions (adversarial audit, 2026-10-03) ──────────────


def disk_lines(path):
    """The file's lines as Read, sed and wc number them: split on \\n only,
    with the \\r of a CRLF ending removed. A lone \\r is not a line break."""
    raw = Path(path).read_bytes().decode("utf-8")
    rows = raw.split("\n")
    if raw.endswith("\n"):
        rows.pop()
    return [r[:-1] if r.endswith("\r") else r for r in rows]


def call_mcp(name, arguments):
    """Call a tool through FastMCP (argument validation included); the parsed JSON text."""
    result = asyncio.run(ws.mcp.call_tool(name, arguments))
    blocks = result[0] if isinstance(result, tuple) else result
    return json.loads(blocks[0].text)


def selected_lines(lines, ranges):
    return [n for s, e in ranges for n in range(s, e + 1)]


# Windowing edge cases


@pytest.mark.parametrize("heading", ["# Intro", "Intro\n====="], ids=["atx", "setext"])
def test_heading_on_the_first_body_line_starts_the_first_window(tmp_path, heading):
    """A page that opens with a heading must not get an empty window before
    it, and the first window must carry that heading as its section label."""
    path = make_page(tmp_path, f"{heading}\n\n{words(120)}\n\n## Next\n\n{words(120, 2)}\n")
    lines, body_start, wins = windows_of(path)
    assert_partition(lines, body_start, wins, ws.WINDOW_TOKENS)
    assert wins[0]["start"] == body_start == 12 and lines[11] == heading.split("\n")[0]
    assert [w["section"] for w in wins] == ["Intro", "Intro > Next"]


def test_unclosed_code_fence_keeps_every_line_and_hides_headings_inside(tmp_path):
    """Jina output sometimes opens a fence it never closes. Everything after
    it is code to the end of the page: no line may be lost, no window may
    pass the maximum, a '#' line inside is not a heading, and a window may
    end inside the fence only because the next line would pass the maximum."""
    code = "\n\n".join(f"step_{k} {words(20, k)}" for k in range(30))
    body = f"# Top\n\n{words(100)}\n\n```python\n{code}\n## not a heading\n\n{words(300, 5)}\n"
    path = make_page(tmp_path, body)
    lines, body_start, wins = windows_of(path, window_tokens=100)
    assert_partition(lines, body_start, wins, 100)
    assert {w["section"] for w in wins} == {"Top"}
    fence = lines.index("```python") + 1
    inside = [k for k, w in enumerate(wins[:-1]) if w["start"] > fence]
    assert len(inside) >= 3
    for k in inside:
        first_next = lines[wins[k + 1]["start"] - 1]
        next_tokens = ws._est_tokens(first_next) if first_next.strip() else 0
        assert wins[k]["tokens"] + next_tokens > 200, f"{wins[k]['id']} ended inside the fence below the maximum"


@pytest.mark.parametrize("trailing", ["\n", ""], ids=["newline", "no-final-newline"])
def test_table_at_end_of_file_stays_in_one_window_and_ends_the_last_one(tmp_path, server, trailing):
    """A table that ends the file is the last thing on the page; it must be
    judged whole (it is under the maximum) and the last window must reach
    the last line, with or without a final newline."""
    rows = "\n".join(f"| r{k} | {words(5, k)} |" for k in range(8))
    table = f"| a | b |\n|---|---|\n{rows}"
    assert sum(ws._est_tokens(x) for x in table.split("\n")) <= 200
    path = make_page(tmp_path, f"## Data\n\n{words(150)}\n\n{table}{trailing}")
    lines, body_start, wins = windows_of(path, window_tokens=100)
    assert_partition(lines, body_start, wins, 100)
    header = lines.index("| a | b |") + 1
    assert lines[-1].startswith("| r7 ") and wins[-1]["end"] == len(lines)
    assert len({w["id"] for w in wins for n in range(header, len(lines) + 1) if w["start"] <= n <= w["end"]}) == 1
    r = one(QUESTION, path, window_tokens=100)
    assert r["status"] == "ok" and r["windows"][-1][1] == len(lines) == r["file_lines"] == len(disk_lines(path))


@pytest.mark.parametrize("char", ["x", "é"], ids=["ascii", "non-ascii"])
def test_line_over_maximum_without_whitespace_is_cut_exactly_at_the_limit(tmp_path, char):
    """A base64 blob or minified line has no whitespace to cut at; each
    segment must be as large as the limit allows, and the segments must
    rebuild the line exactly."""
    line = char * 5000
    path = make_page(tmp_path, f"## Blob\n\n{words(100)}\n\n{line}\n\n{words(100, 3)}\n")
    lines, body_start, wins = windows_of(path)
    assert_partition(lines, body_start, wins, ws.WINDOW_TOKENS)
    n = lines.index(line) + 1
    spans = [w["span"] for w in wins if w.get("line") == n]
    limit = 2 * ws.WINDOW_TOKENS
    assert "".join(line[a:b] for a, b in spans) == line and len(spans) >= 2
    assert all(ws._est_tokens(line[a:b]) <= limit for a, b in spans)
    assert all(ws._est_tokens(line[a:b + 1]) > limit for a, b in spans[:-1])


def test_crlf_page_numbers_lines_as_read_does(tmp_path, server):
    """A page saved with CRLF endings (text mode on Windows writes them) must
    give the same line numbers as Read, and no \\r may reach Jev."""
    folder = tmp_path / ".web_cache"
    folder.mkdir()
    url = "https://example.com/crlf"
    text = HEAD.format(url=url, title="CRLF") + PREAMBLE.format(url=url, title="CRLF") + sections([0.1, 0.9, 0.1])
    path = folder / "crlf.md"
    path.write_bytes(text.replace("\n", "\r\n").encode())
    lines, body_start, meta = ws._read_cached_page(str(path))
    assert body_start == 12 and meta == {"url": url, "title": "CRLF"} and lines == disk_lines(path)
    r = one(QUESTION, str(path))
    (start, end), = r["ranges"]
    read = disk_lines(path)[start - 1:end]
    assert read[0] == "## Section 2" and read[-1].startswith("[[p=0.9]]")
    assert all("\r" not in e["text"] for req in server.requests for e in req["body"]["state"]["windows"].values())


def test_lone_carriage_return_does_not_shift_line_numbers(tmp_path, server):
    """Read, sed and wc split lines on \\n only (checked against the Read tool
    on 2026-10-03). A lone \\r inside a line must not add a line, or every
    range after it points one line early."""
    body = f"## Intro\n\nfirst part\rsame line {words(100)}\n\n## Target\n\n[[p=0.9]] {words(100, 2)}\n"
    path = make_page(tmp_path, body)
    assert ws._read_cached_page(path)[0] == disk_lines(path)
    (start, end), = one(QUESTION, path)["ranges"]
    read = disk_lines(path)[start - 1:end]
    assert read[0] == "## Target" and read[-1].startswith("[[p=0.9]]")


@pytest.mark.parametrize("text", ["---\nurl: https://example.com/x\ntitle: X\n---\n",
                                  "---\nurl: https://example.com/x\ntitle: X\n---"], ids=["newline", "no-newline"])
def test_frontmatter_only_page_is_ok_empty_and_sends_nothing(tmp_path, server, text):
    """A page with frontmatter and nothing else has no body to judge: ok,
    not relevant, with the empty-body warning, and no request."""
    folder = tmp_path / ".web_cache"
    folder.mkdir()
    path = folder / "fm.md"
    path.write_text(text)
    r = one(QUESTION, str(path))
    assert r["status"] == "ok" and r["relevant"] is False and r["windows"] == [] and r["unjudged"] == []
    assert "page body is empty" in r["warnings"] and r["file_lines"] == 4 and not server.requests


def test_window_text_with_key_separators_and_json_characters_round_trips(tmp_path, server):
    """Window ids are the only link between an answer and its lines. Text
    that names another window's key, or carries JSON syntax, must reach Jev
    unchanged in its own window and must not move any answer."""
    tricky = 'See `windows.w002` and "windows.w003": {"noul": 1} \\ end.'
    body = (f"## A > B: {{w001}}\n\n[[p=0.9]] {tricky} {words(100)}\n\n"
            f"## Plain\n\n[[p=0.1]] {words(100, 2)}\n\n"
            f"## Third\n\n[[p=0.95]] w001 windows.w001 {words(100, 4)}\n")
    path = make_page(tmp_path, body)
    _, _, wins = windows_of(path)
    r = one(QUESTION, path)
    assert [w[2] for w in r["windows"]] == [0.9, 0.1, 0.95] and len(r["ranges"]) == 2
    sent = server.requests[0]["body"]["state"]["windows"]
    assert sent["w001"]["text"].startswith("Section: A > B: {w001}\n") and tricky in sent["w001"]["text"]
    assert [sent[w["id"]]["text"] for w in wins] == [f"Section: {w['heading']}\n{w['text']}" for w in wins]


# Range merging


@pytest.mark.parametrize("pattern", [[0.9, 0.1, 0.9], [0.1, 0.9, 0.1, 0.1, 0.9, 0.1], [0.9, 0.1, 0.1, 0.1, 0.9, 0.9]])
def test_ranges_hold_exactly_the_text_lines_of_selected_windows(tmp_path, pattern):
    """Each run of selected windows is one range, and the text lines in the
    ranges are exactly those of the selected windows: none from a window
    below the threshold, none missing, no line counted twice."""
    path = make_page(tmp_path, sections(pattern))
    lines, _, wins = windows_of(path)
    r = one(QUESTION, path)
    in_ranges = [n for n in selected_lines(lines, r["ranges"]) if lines[n - 1].strip()]
    cut = ws.DEFAULT_THRESHOLD
    chosen = [n for w, p in zip(wins, pattern) if p >= cut for n in range(w["start"], w["end"] + 1) if lines[n - 1].strip()]
    assert in_ranges == chosen
    runs = sum(1 for k, p in enumerate(pattern) if p >= cut and (k == 0 or pattern[k - 1] < cut))
    assert len(r["ranges"]) == runs and r["lines_selected"] == len(selected_lines(lines, r["ranges"]))


@pytest.mark.parametrize("before, lead, tail, after, expected", [
    (0.9, True, True, 0.9, "one"),
    (0.9, False, True, 0.1, "two"),
    (0.1, True, False, 0.9, "two"),
], ids=["selected-both-sides", "tail-segment-only", "lead-segment-only"])
def test_split_line_next_to_selected_windows_is_listed_once(tmp_path, before, lead, tail, after, expected):
    """A long line split into segments sits between ordinary windows. Its
    line must appear in exactly one range whichever segments pass, and
    ranges must be ordered and never overlap."""
    line = ("[[p=0.9]] " if lead else "") + words(1950) + (" [[p=0.9]]" if tail else "")
    body = f"## A\n\n[[p={before}]] {words(100)}\n\n{line}\n\n## B\n\n[[p={after}]] {words(100, 4)}\n"
    path = make_page(tmp_path, body)
    lines = ws._read_cached_page(path)[0]
    n = lines.index(line) + 1
    r = one(QUESTION, path)
    picked = selected_lines(lines, r["ranges"])
    assert picked.count(n) == 1 and len(picked) == len(set(picked)) == r["lines_selected"]
    assert all(a[1] < b[0] for a, b in zip(r["ranges"], r["ranges"][1:]))
    assert len(r["ranges"]) == (1 if expected == "one" else 2)


def test_threshold_zero_selects_every_judged_window_and_one_only_certain_ones(tmp_path):
    """The bounds are legal thresholds: 0 selects every judged window, even
    p = 0.0, and 1 selects only p = 1.0."""
    path = make_page(tmp_path, sections([0.0, 1.0, 0.0]))
    lines, _, wins = windows_of(path)
    r0 = one(QUESTION, path, threshold=0)
    assert r0["status"] == "ok" and r0["ranges"] == [trim(lines, wins[0]["start"], wins[2]["end"])]
    r1 = one(QUESTION, path, threshold=1)
    assert r1["ranges"] == [trim(lines, wins[1]["start"], wins[1]["end"])]


# Budgets


def test_page_over_max_windows_is_refused_while_other_pages_are_judged(tmp_path, server):
    """Refusing one large page must not stop the others in the call, and the
    refused page must say its whole body is unjudged."""
    big = make_page(tmp_path, sections([0.9] * 6), name="big.md")
    small = make_page(tmp_path, sections([0.9] * 2), name="small.md")
    rb, rs = run(QUESTION, [big, small], max_windows=3)
    assert rb["status"] == "error" and rb["error"]["kind"] == "too_many_windows" and rb["requests"] == 0
    assert rb["unjudged"] == [{"lines": [12, rb["file_lines"]], "reason": "too_many_windows"}]
    assert rs["status"] == "ok" and rs["relevant"] is True
    assert server.asked_ids() == [["w001", "w002"]]


def test_window_over_the_state_budget_is_not_sent_and_other_pages_are_judged(tmp_path, server, monkeypatch):
    """A window over the state budget on its own would be refused by Jev
    (422), and that client error would stop every other batch in the call.
    It must not be sent: its lines are unjudged with reason over_budget, the
    page is partial, and its other windows and the other pages are judged."""
    monkeypatch.setattr(ws, "STATE_TOKEN_BUDGET", 1500)
    body = (f"## Section 1\n\n[[p=0.9]] {words(300)}\n\n"
            f"## Section 2\n\n[[p=0.9]] OVERSIZE-SENTINEL {words(1600, 2)}\n\n"
            f"## Section 3\n\n[[p=0.1]] {words(300, 4)}\n")
    a = make_page(tmp_path, body, name="a.md")
    b = make_page(tmp_path, sections([0.9, 0.1]), name="b.md")
    lines, body_start, wins = windows_of(a, window_tokens=1000)
    meta = ws._read_cached_page(a)[2]
    assert [x["over_budget"] for x in ws._batches(wins, QUESTION, meta)] == [False, True, False]
    ra, rb = run(QUESTION, [a, b], window_tokens=1000)
    assert len(server.requests) == 3 and sorted(server.asked_ids()) == [["w001"], ["w001"], ["w003"]]
    assert "OVERSIZE-SENTINEL" not in json.dumps([req["body"] for req in server.requests])
    assert ra["status"] == "partial" and ra["relevant"] is True and ra["error"]["kind"] == "over_budget"
    assert [w[2] for w in ra["windows"]] == [0.9, None, 0.1] and ra["requests"] == 2
    assert ra["unjudged"] == [{"lines": [wins[1]["start"], wins[1]["end"]], "reason": "over_budget"}]
    assert any("w002 is over the token budget" in w for w in ra["warnings"])
    assert rb["status"] == "ok" and rb["relevant"] is True and ws._exit_code([ra, rb]) == 1


# Failure paths


def test_client_error_with_four_in_flight_sends_nothing_after_it(tmp_path, server, monkeypatch):
    """With 4 requests in flight, a 422 on one must stop every later batch:
    the server sees exactly the 4 already sent, the 3 that answer are kept
    and cached, and a rerun pays only for the rest."""
    monkeypatch.setattr(ws, "JEV_TIMEOUT_S", 2.0)
    server.rule = lambda body: ({"status": 422, "delay": 0.1, "body": {"detail": "bad"}}
                                if "w001" in body["questions"] else {"delay": 0.3})
    path = make_page(tmp_path, sections([0.1] * 160))  # 10 batches of 16
    lines, _, wins = windows_of(path)
    r = one(QUESTION, path)
    assert len(server.requests) == 4 and r["requests"] == 4
    assert r["status"] == "partial" and r["relevant"] is None and r["error"]["kind"] == "client_error"
    assert [w[2] for w in r["windows"]] == [None] * 16 + [0.1] * 48 + [None] * 96
    assert r["unjudged"] == [{"lines": [wins[0]["start"], wins[15]["end"]], "reason": "client_error"},
                             {"lines": [wins[64]["start"], wins[159]["end"]], "reason": "not_sent_after_failure"}]
    server.rule = None
    server.requests.clear()
    again = one(QUESTION, path)
    assert again["status"] == "ok" and again["requests"] == 7 and again["cache_hits"] == 3


@pytest.mark.parametrize("status", [500, 503])
def test_5xx_gives_up_after_exactly_the_policy_attempts_and_stops(tmp_path, server, monkeypatch, capsys, status):
    """web-sieve adds no retries: a server error is tried exactly as often as
    the client policy allows, then the call stops and says so (exit 3)."""
    monkeypatch.setattr(ws, "JEV_CONCURRENCY", 1)
    server.default = {"status": status, "body": {"detail": "down"}}
    path = make_page(tmp_path, sections([0.9] * 40))  # 3 batches
    r = one(QUESTION, path)
    assert len(server.requests) == FAST["attempts"] == 2
    assert r["status"] == "error" and r["relevant"] is None and r["error"]["kind"] == "gave_up"
    assert f"HTTP {status}" in r["error"]["message"]
    assert [e["event"] for e in r["jev_events"]] == ["jev_retry", "jev_gave_up"]
    assert [u["reason"] for u in r["unjudged"]] == ["gave_up", "not_sent_after_failure"]
    server.requests.clear()
    code, _ = cli(monkeypatch, capsys, QUESTION, path)
    assert code == 3 and len(server.requests) == 2


@pytest.mark.parametrize("payload", ["not json {", {"model": "jev-1.13.0", "usage": {}}, {"answers": []}],
                         ids=["not-json", "no-answers", "answers-not-object"])
def test_malformed_response_body_fails_the_batch_and_stops(tmp_path, server, monkeypatch, capsys, payload):
    """A 2xx that is not a usable answer is not retried, fails its batch,
    stops the call, and exits 5."""
    monkeypatch.setattr(ws, "JEV_CONCURRENCY", 1)
    server.default = {"body": payload}
    path = make_page(tmp_path, sections([0.9] * 20))  # 2 batches
    r = one(QUESTION, path)
    assert len(server.requests) == 1
    assert r["status"] == "error" and r["relevant"] is None and r["error"]["kind"] == "malformed"
    assert [u["reason"] for u in r["unjudged"]] == ["malformed", "not_sent_after_failure"]
    server.requests.clear()
    code, _ = cli(monkeypatch, capsys, QUESTION, path)
    assert code == 5 and len(server.requests) == 1


@pytest.mark.parametrize("value", [-0.01, 10 ** 400, float("inf")], ids=["negative", "huge-integer", "infinity"])
def test_out_of_range_probability_is_malformed_not_a_crash(tmp_path, server, value):
    """Any noul outside 0..1 fails its batch as malformed. A huge integer
    must not raise OverflowError out of the call and lose every result."""
    server.default = {"mutate": lambda a: {**a, "w002": {"type": "noul", "noul": value}}}
    r = one(QUESTION, make_page(tmp_path, sections([0.9] * 3)))
    assert r["status"] == "error" and r["error"]["kind"] == "malformed" and r["relevant"] is None


def test_extra_keys_in_the_response_are_ignored(tmp_path, server):
    """Fields Jev adds later (a top-level key, a confidence, an answer for an
    id that was not asked) must not break or shift the answers."""
    from fake_jev import marker_answers

    def rule(body):
        answers = {k: dict(v, confidence=0.3) for k, v in marker_answers(body).items()}
        answers["w999"] = {"type": "noul", "noul": 0.99}
        return {"body": {"model": "jev-1.13.0", "answers": answers, "trace": "x",
                         "usage": {"input_tokens": 7, "output_tokens": 0}}}

    server.rule = rule
    r = one(QUESTION, make_page(tmp_path, sections([0.9, 0.1])))
    assert r["status"] == "ok" and [w[2] for w in r["windows"]] == [0.9, 0.1] and len(r["ranges"]) == 1
    assert r["input_tokens"] == 7 and r["warnings"] == []


def test_truncated_fetch_fails_only_that_source(tmp_path, server, monkeypatch):
    """A Jina response cut short (Content-Length larger than the body) raises
    http.client.IncompleteRead, which is not an OSError. It must become that
    source's fetch_failed, not an exception that loses every other source."""
    import socket
    import threading
    import urllib.request

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def serve():
        conn, _ = listener.accept()
        conn.recv(65536)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\nContent-Type: text/markdown\r\n\r\nTitle: cut\n")
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    real = urllib.request.urlopen
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None: real(f"http://127.0.0.1:{listener.getsockname()[1]}/", timeout=5))
    ok_page = make_page(tmp_path, sections([0.9]), name="ok.md")
    try:
        r_url, r_ok = run(QUESTION, ["https://example.com/truncated", ok_page], cache_dir=str(tmp_path / ".web_cache"))
    finally:
        listener.close()
    assert r_url["status"] == "error" and r_url["error"]["kind"] == "fetch_failed"
    assert "IncompleteRead" in r_url["error"]["message"]
    assert r_ok["status"] == "ok" and r_ok["relevant"] is True


# Cache


def test_changed_window_text_misses_only_the_batch_that_holds_it(tmp_path, server):
    """An edited page must not reuse answers about text that is gone, and
    must not re-ask batches whose text is unchanged."""
    path = make_page(tmp_path, sections([0.1] * 20))
    assert one(QUESTION, path)["requests"] == 2
    text = Path(path).read_text()
    Path(path).write_text(text.replace("## Section 18\n", "## Section 18 edited\n"))
    server.requests.clear()
    r = one(QUESTION, path)
    assert r["requests"] == 1 and r["cache_hits"] == 1
    assert server.asked_ids() == [["w017", "w018", "w019", "w020"]]


def test_identical_windows_in_one_batch_keep_their_own_cached_answers(tmp_path, server):
    """Repeated blocks (a nav bar at top and bottom, a repeated notice) give
    identical windows in one request. Jev answers each under its own id; a
    rerun from the cache must return those answers, not one of them twice."""
    block = f"## Repeated\n\n{words(100)}\n\n"
    path = make_page(tmp_path, block + block)
    _, _, wins = windows_of(path)
    assert len(wins) == 2 and wins[0]["text"] == wins[1]["text"] and wins[0]["section"] == wins[1]["section"]
    server.default = {"mutate": lambda a: {**a, "w001": {"type": "noul", "noul": 0.9},
                                           "w002": {"type": "noul", "noul": 0.1}}}
    first = one(QUESTION, path)
    second = one(QUESTION, path)
    assert [w[2] for w in first["windows"]] == [0.9, 0.1] and first["relevant"] is True
    assert second["requests"] == 0 and second["windows"] == first["windows"] and second["ranges"] == first["ranges"]


# Key handling


def test_key_never_appears_in_output_stderr_or_cache_on_failure_paths(tmp_path, server, fakebin, jev):
    """Retry and give-up events go to stderr and into the output; none of
    them, nor the answers cache, may carry the key."""
    server.rule = lambda body: {"status": 429, "body": {"detail": "busy"}} if "w001" in body["questions"] else None
    path = make_page(tmp_path, sections([0.9] * 20))
    env = {"PATH": f"{fakebin}:/usr/bin:/bin", "HOME": str(tmp_path), "JEV_API_KEY": KEY,
           "JEV_API_BASE": server.url, "WEB_SIEVE_JEV": os.path.realpath(jev.__file__),
           "PYTHONDONTWRITEBYTECODE": "1"}
    out = subprocess.run([sys.executable, str(ROOT / "web-sieve.py"), "ranges", QUESTION, path],
                         capture_output=True, text=True, env=env, timeout=60)
    assert out.returncode == 3 and "jev_retry" in out.stderr and "jev_gave_up" in out.stderr
    assert json.loads(out.stdout)[0]["jev_events"]
    cache = b"".join(p.read_bytes() for p in (tmp_path / ".web_cache").glob(ws.ANSWERS_DB + "*"))
    assert cache  # the second batch was answered and cached
    for blob in (out.stdout, out.stderr, cache.decode("latin-1")):
        assert KEY not in blob


def test_no_key_through_the_mcp_layer_is_an_error_object_not_an_exception(tmp_path, server, monkeypatch):
    """Through FastMCP, a missing key (environment empty, keychain stub
    finds nothing) is a per-source error object the caller can read."""
    monkeypatch.delenv("JEV_API_KEY")
    path = make_page(tmp_path, sections([0.9]))
    r, = call_mcp("find_relevant_ranges", {"question": QUESTION, "sources": [path]})
    assert r["status"] == "error" and r["relevant"] is None
    assert r["error"] == {"kind": "no_key", "message": "no JEV_API_KEY in the environment or the keychain"}
    assert not server.requests


# Path safety


def test_dotdot_symlinked_cache_dir_and_paths_outside_cache_dir(tmp_path, server):
    """The rule is about the real file: '..' cannot step out of a .web_cache,
    a .web_cache that is a symlink to another folder is not a cache, and a
    page in another project's .web_cache is accepted (cache_dir is only for
    URL sources), with its answers stored beside it."""
    proj, other = tmp_path / "proj", tmp_path / "other"
    proj.mkdir()
    other.mkdir()
    make_page(proj, sections([0.9]), name="p.md")
    elsewhere = make_page(other, sections([0.9]), name="e.md")
    notes = proj / "notes"
    notes.mkdir()
    (notes / "n.md").write_text(HEAD.format(url="https://x", title="t") + "PRIVATE-NOTE\n")
    private = tmp_path / "Documents"
    private.mkdir()
    (private / "d.md").write_text(HEAD.format(url="https://x", title="t") + "PRIVATE-DOC\n")
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / ".web_cache").symlink_to(private)
    sources = [str(proj / ".web_cache" / ".." / "notes" / "n.md"),
               str(notes / ".." / ".web_cache" / "p.md"),
               elsewhere,
               str(notes / "n.md"),
               str(linked / ".web_cache" / "d.md")]
    results = run(QUESTION, sources, cache_dir=str(proj / ".web_cache"))
    assert [r["status"] for r in results] == ["error", "ok", "ok", "error", "error"]
    assert {results[k]["error"]["kind"] for k in (0, 3, 4)} == {"not_a_cached_page"}
    assert (other / ".web_cache" / ws.ANSWERS_DB).exists()
    sent = json.dumps([r["body"] for r in server.requests])
    assert len(server.requests) == 2 and "PRIVATE" not in sent


# Surface parity


def test_mcp_tool_and_cli_return_the_same_json(tmp_path, server, monkeypatch, capsys):
    """The MCP tool (through FastMCP's argument handling) and the CLI share
    one code path; given the same inputs and an empty cache they must print
    the same results, apart from elapsed time."""
    a = make_page(tmp_path, sections([0.9, 0.1, 0.9]), name="a.md")
    b = make_page(tmp_path, sections([0.1] * 3), name="b.md")
    missing = str(tmp_path / ".web_cache" / "missing.md")
    via_mcp = call_mcp("find_relevant_ranges", {"question": f"  {QUESTION}  ", "sources": [a, b, missing],
                                                "threshold": 0.5, "window_tokens": 400, "max_windows": 1000})
    for f in (tmp_path / ".web_cache").glob(ws.ANSWERS_DB + "*"):
        f.unlink()
    code, via_cli = cli(monkeypatch, capsys, f"  {QUESTION}  ", a, b, missing, "--threshold", "0.5",
                        "--window-tokens", "400", "--max-windows", "1000")
    for r in via_mcp + via_cli:
        assert r.pop("elapsed_ms") >= 0
    assert via_mcp == via_cli and code == 1 and len(server.requests) == 4
    assert [r["status"] for r in via_cli] == ["ok", "ok", "error"] and via_cli[0]["question"] == QUESTION


# Concurrency


def test_four_requests_in_flight_and_answers_assembled_in_window_order(tmp_path, server, monkeypatch):
    """Requests run 4 at a time and finish in any order; every answer must
    land on its own window, in document order, for every source, and all
    answers written by the 4 worker threads must be in the cache."""
    monkeypatch.setattr(ws, "JEV_TIMEOUT_S", 2.0)

    def rule(body):
        if body["state"]["page"]["title"] == "Second":
            return {"delay": 0.01}  # the second source, sent last, finishes before most of the first
        batch = (int(min(body["questions"])[1:]) - 1) // 16 + 1
        return {"delay": 0.05 * (9 - batch)}  # within the first source, batch 1 is the slowest

    server.rule = rule
    p1 = [round(0.003 * k, 3) for k in range(1, 97)]
    p1[49] = 0.95
    p2 = [round(0.86 + 0.004 * k, 3) for k in range(1, 33)]  # all at or above the default threshold
    one_path = make_page(tmp_path, sections(p1), name="one.md")
    two_path = make_page(tmp_path, sections(p2), name="two.md", title="Second")
    r1, r2 = run(QUESTION, [one_path, two_path])
    assert server.max_active == 4 and len(server.requests) == 8
    assert server.finished[0] != server.asked_ids()[0]  # the first sent was not the first to finish
    assert [w[2] for w in r1["windows"]] == p1 and [w[2] for w in r2["windows"]] == p2
    assert r1["status"] == r2["status"] == "ok" and len(r1["ranges"]) == 1 and len(r2["ranges"]) == 1
    server.rule = None
    a1, a2 = run(QUESTION, [one_path, two_path])
    assert a1["requests"] == a2["requests"] == 0 and a1["cache_hits"] == 6 and a2["cache_hits"] == 2
    assert a1["windows"] == r1["windows"] and a2["windows"] == r2["windows"]


def test_hedge_is_reported_and_the_first_answer_kept(tmp_path, server, monkeypatch):
    """A hedged duplicate is a second request to Jev; it must show in
    jev_events, and the page must still be judged once."""
    monkeypatch.setattr(ws, "JEV_TIMEOUT_S", 2.0)
    monkeypatch.setattr(ws, "JEV_HEDGE_AFTER_S", 0.1)
    server.script = [{"delay": 0.6}]  # the first request hangs past the hedge; the duplicate answers at once
    r = one(QUESTION, make_page(tmp_path, sections([0.9, 0.1])))
    assert r["status"] == "ok" and r["requests"] == 1 and len(server.requests) == 2
    assert [e["event"] for e in r["jev_events"]] == ["jev_hedge", "jev_done"]


@pytest.mark.parametrize("tokens", [10 ** 400, -100], ids=["huge", "negative"])
def test_absurd_usage_is_a_warning_not_a_crash_or_a_negative_cost(tmp_path, server, tokens):
    """Cost is input_tokens x price. A usage count that is not a sane
    non-negative integer must not raise OverflowError after every request
    has been paid for, and must not lower the reported cost."""
    from fake_jev import marker_answers

    server.rule = lambda body: {"body": {"model": "jev-1.13.0", "answers": marker_answers(body),
                                         "usage": {"input_tokens": tokens, "output_tokens": 0}}}
    r = one(QUESTION, make_page(tmp_path, sections([0.9, 0.1])))
    assert r["status"] == "ok" and r["relevant"] is True
    assert r["input_tokens"] == 0 and r["cost_usd"] == 0.0
    assert any("usage.input_tokens" in w and "undercounted" in w for w in r["warnings"])


def test_jev_client_loads_through_a_symlink_and_a_dangling_link_is_no_client(tmp_path, server, monkeypatch, jev):
    """~/.local/bin/jev may be a symlink into another checkout. The client
    loads from the link's real file; when that checkout is removed the link
    dangles, and the tool must report no_client naming the link, not crash."""
    real = tmp_path / "worktree" / "bin" / "jev"
    real.parent.mkdir(parents=True)
    shutil.copy(os.path.realpath(jev.__file__), real)
    link = tmp_path / "local-bin-jev"
    link.symlink_to(real)
    monkeypatch.setenv("WEB_SIEVE_JEV", str(link))
    monkeypatch.delitem(sys.modules, "web_sieve_jev")
    assert ws._load_jev().__file__ == os.path.realpath(link)
    path = make_page(tmp_path, sections([0.9]))
    assert one(QUESTION, path)["status"] == "ok"
    shutil.rmtree(tmp_path / "worktree")
    monkeypatch.delitem(sys.modules, "web_sieve_jev")
    r = one(QUESTION, path)
    assert r["status"] == "error" and r["error"]["kind"] == "no_client" and str(link) in r["error"]["message"]
    assert len(server.requests) == 1


def test_unexpected_worker_exception_is_a_failed_batch_not_a_crash(tmp_path, server, monkeypatch, capsys, jev):
    """An exception the jev client does not define (a bug, an exhausted
    resource) must not escape the call: the batch fails with the exception's
    class and message, the call stops as it does for a Jev error, batches
    already answered stay cached, and the MCP tool and the CLI still return
    JSON with no traceback."""
    monkeypatch.setattr(ws, "JEV_CONCURRENCY", 1)
    real_ask = jev.ask

    def ask(request, *args, **kwargs):
        if request["state"]["page"].get("title") == "Boom":
            raise RuntimeError("injected failure")
        return real_ask(request, *args, **kwargs)

    monkeypatch.setattr(jev, "ask", ask)
    good = make_page(tmp_path, sections([0.9, 0.1]), name="good.md")
    boom = make_page(tmp_path, sections([0.9] * 20), name="boom.md", title="Boom")  # 2 batches
    rg, rb = call_mcp("find_relevant_ranges", {"question": QUESTION, "sources": [good, boom]})
    assert rg["status"] == "ok" and rg["relevant"] is True
    assert rb["status"] == "error" and rb["relevant"] is None and rb["ranges"] == []
    assert rb["error"] == {"kind": "internal_error", "message": "RuntimeError: injected failure"}
    assert [u["reason"] for u in rb["unjudged"]] == ["internal_error", "not_sent_after_failure"]
    assert len(server.requests) == 1  # only the good page reached Jev; the second Boom batch was not attempted
    assert "Traceback" not in capsys.readouterr().err
    assert one(QUESTION, good)["requests"] == 0  # the batch answered before the failure is cached
    monkeypatch.setattr(sys, "argv", ["web-sieve", "ranges", QUESTION, good, boom])
    with pytest.raises(SystemExit) as info:
        ws._cli()
    captured = capsys.readouterr()
    assert info.value.code == 1 and json.loads(captured.out)[1]["error"]["kind"] == "internal_error"
    assert "Traceback" not in captured.err and "web_sieve_internal_error" in captured.err


# ── Section line, max_windows 2,000 and the answers-cache prune ───


def section_lines_of(path, **kw):
    """The first line of the text each window is sent with (Section line on)."""
    _, _, wins = windows_of(path, **kw)
    meta = ws._read_cached_page(path)[2]
    sent = {}
    for batch in ws._batches(wins, QUESTION, meta, section_line=True):
        sent.update(batch["request"]["state"]["windows"])
    return [sent[w["id"]]["text"].split("\n", 1)[0] for w in wins]


def test_section_line_names_the_nearest_heading_and_its_parents(tmp_path):
    """A 200-token window often starts in the middle of a section, and the
    Section line is then the only place Jev learns which section it is in.
    It must name the nearest heading above the window with that heading's
    parents, and drop a heading once a sibling or a higher heading closes it."""
    body = (f"# Top\n\n{words(100)}\n\n## Mid\n\n{words(100, 1)}\n\n### Low\n\n{words(150, 2)}\n\n"
            f"{words(150, 3)}\n\n{words(150, 4)}\n\n## Mid two\n\n{words(100, 5)}\n\n# Top two\n\n{words(100, 6)}\n")
    path = make_page(tmp_path, body)
    lines, _, wins = windows_of(path)
    assert section_lines_of(path) == ["Section: Top", "Section: Top > Mid", "Section: Top > Mid > Low",
                                      "Section: Top > Mid > Low", "Section: Top > Mid two", "Section: Top two"]
    inherited = wins[3]  # starts mid-section: no heading line of its own
    assert not any(lines[j - 1].startswith("#") for j in range(inherited["start"], inherited["end"] + 1))


def test_section_line_follows_setext_headings(tmp_path):
    """Setext headings (a line underlined with === or ---) are level 1 and 2,
    the same as # and ##, so they must build the same path."""
    body = f"Guide\n=====\n\n{words(100)}\n\nInstall\n-------\n\n{words(150, 1)}\n\n{words(150, 2)}\n\n{words(150, 3)}\n"
    path = make_page(tmp_path, body)
    assert section_lines_of(path) == ["Section: Guide", "Section: Guide > Install", "Section: Guide > Install"]


def test_window_without_a_heading_above_it_gets_section_none(tmp_path):
    """Text before the first heading, or on a page with none, has no section;
    the line says so rather than being left out, so every window is sent in
    the same shape."""
    path = make_page(tmp_path, f"{words(150)}\n\n{words(150, 1)}\n\n{words(150, 2)}\n\n## Later\n\n{words(100, 3)}\n")
    assert section_lines_of(path) == ["Section: (none)", "Section: (none)", "Section: Later"]
    plain = make_page(tmp_path, f"{words(100)}\n", name="plain.md")
    assert section_lines_of(plain) == ["Section: (none)"]


def test_heading_lookalikes_inside_a_code_fence_are_not_headings(tmp_path):
    """A shell comment (# ...) or an underlined line inside a code block is
    code. Taken as a heading, it would replace the real section in the
    Section line of every later window."""
    body = (f"## Real\n\n```bash\n# a comment, not a heading\nrun --now\n```\n\n{words(210)}\n\n"
            f"{words(100, 1)}\n\n~~~\nSetext lookalike\n---\n~~~\n\n{words(210, 2)}\n\n{words(100, 3)}\n")
    path = make_page(tmp_path, body)
    assert section_lines_of(path) == ["Section: Real"] * 3


def test_section_line_is_capped_at_160_characters_keeping_the_nearest_heading(tmp_path):
    """A long heading path costs tokens in every window under it. The cap
    drops top-level headings first, because the nearest heading says most
    about the window; one heading is cut to 80 characters, as in range_detail."""
    a, b, c = "A" * 100, "B" * 70, "C" * 70
    assert ws._heading_path((b, c), True) == f"{b} > {c}"  # 143 characters: kept whole
    assert ws._heading_path((a, b, c), True) == f"… > {b} > {c}"  # 227 characters before the cap
    deep = ws._heading_path(tuple(f"H{k} {'x' * 75}" for k in range(6)), True)
    assert len(deep) <= ws.SECTION_CAP_CHARS == 160 and deep == f"… > H5 {'x' * 75}"
    assert ws._heading_path(("y" * 300,), True) == "y" * 80 + "…"
    assert ws._heading_path((), True) == "(none)"
    body = f"# {a}\n\n{words(50)}\n\n## {b}\n\n{words(50, 1)}\n\n### {c}\n\n{words(210, 2)}\n\n{words(100, 3)}\n"
    assert section_lines_of(make_page(tmp_path, body))[-1] == f"Section: … > {b} > {c}"


def test_section_line_is_sent_to_jev_but_never_part_of_ranges_or_window_text(tmp_path, server):
    """The Section line exists only in the request. Ranges are file line
    numbers for Read, so they must be the same with the line on or off, and
    the window text, which the search index also uses, must not contain it.
    Off sends the earlier shape: the heading path in its own field."""
    body = f"## Install\n\n{words(210)}\n\n[[p=0.9]] {words(100, 1)}\n\n## Other\n\n{words(100, 2)}\n"
    path = make_page(tmp_path, body)
    on = one(QUESTION, path)
    sent_on = server.requests[0]["body"]["state"]["windows"]["w002"]
    off = one(QUESTION, path, section_line=False)
    sent_off = server.requests[1]["body"]["state"]["windows"]["w002"]
    _, _, wins = windows_of(path)
    assert sent_on == {"text": f"Section: Install\n{wins[1]['text']}"} and not wins[1]["text"].startswith("Section:")
    assert sent_off == {"section": "Install", "text": wins[1]["text"]}
    assert on["ranges"] == off["ranges"] == [trim(ws._read_cached_page(path)[0], wins[1]["start"], wins[1]["end"])]
    assert [w[:2] for w in on["windows"]] == [w[:2] for w in off["windows"]]
    assert on["range_detail"][0]["section"] == "Install"


def test_heading_is_part_of_the_answer_key(tmp_path):
    """The same text under two different headings is two different pieces of
    evidence, and Jev sees the heading. An answer cached under one heading
    must never be served for the other, with the heading sent either way."""
    common = f"[[p=0.9]] {words(100, 7)}"
    a = make_page(tmp_path, f"## Alpha\n\n{words(210)}\n\n{common}\n", name="a.md")
    b = make_page(tmp_path, f"## Beta\n\n{words(210)}\n\n{common}\n", name="b.md")
    meta = {"title": "Test Page", "url": "https://example.com/page"}
    wa, wb = windows_of(a)[2][1], windows_of(b)[2][1]
    assert wa["text"] == wb["text"] == common and (wa["id"], wa["heading"], wb["heading"]) == ("w002", "Alpha", "Beta")
    for section_line in (True, False):  # one window per batch, so only the window's own entry differs
        key_a, key_b = (ws._batches([w], QUESTION, meta, 1, section_line)[0]["keys"] for w in (wa, wb))
        assert key_a != key_b
    relabelled = dict(wb, heading="Alpha", section="Alpha")
    assert ws._batches([relabelled], QUESTION, meta, 1)[0]["keys"] == ws._batches([wa], QUESTION, meta, 1)[0]["keys"]


def test_default_max_windows_is_2000(tmp_path, server, monkeypatch, capsys):
    """At 200-token windows the largest cached pages have about 1,500
    windows, and the old limit of 1,000 refused some of them. A page between
    the two limits is judged with the MCP tool's default; a page over 2,000
    is refused with the CLI's default, and nothing is sent for it."""
    assert ws.MAX_WINDOWS == 2000
    judged = make_page(tmp_path, "".join(f"{words(210, k)}\n\n" for k in range(1001)), name="judged.md")
    refused = make_page(tmp_path, "".join(f"{words(210, k)}\n\n" for k in range(2001)), name="refused.md")
    (r,) = call_mcp("find_relevant_ranges", {"question": QUESTION, "sources": [judged]})
    assert r["status"] == "ok" and len(r["windows"]) == 1001 and r["requests"] == math.ceil(1001 / 16)
    sent = len(server.requests)
    code, (out,) = cli(monkeypatch, capsys, QUESTION, refused)
    assert code == 1 and out["error"]["kind"] == "too_many_windows" and "max_windows is 2000" in out["error"]["message"]
    assert len(server.requests) == sent


def test_answers_cache_prunes_rows_of_older_prompt_versions_and_shrinks(tmp_path, server, monkeypatch):
    """After a change of window size or prompt version no key can match an
    old answer again, so without a prune the file would keep them forever.
    The first open under a newer version deletes them and vacuums the file;
    the new answers are kept, and a later open writes nothing."""
    folder = tmp_path / ".web_cache"
    folder.mkdir()
    db = folder / ws.ANSWERS_DB
    conn = sqlite3.connect(db)  # a file written by version 1: no prompt_version column, user_version 0
    with conn:
        conn.execute("CREATE TABLE answers (key TEXT PRIMARY KEY, p REAL NOT NULL, served_model TEXT NOT NULL, "
                     "created_at TEXT NOT NULL)")
        conn.executemany("INSERT INTO answers VALUES (?, ?, ?, ?)",
                         [(f"{k:064x}", 0.5, "jev-1.13.0", "2026-10-04T00:00:00+00:00") for k in range(5000)])
    conn.close()
    before = db.stat().st_size
    path = make_page(tmp_path, sections([0.9, 0.1]))

    def rows():
        conn = sqlite3.connect(db)
        try:
            return (conn.execute("SELECT prompt_version, COUNT(*) FROM answers GROUP BY prompt_version").fetchall(),
                    conn.execute("PRAGMA user_version").fetchone()[0])
        finally:
            conn.close()

    r = one(QUESTION, path)
    assert r["status"] == "ok" and r["requests"] == 1 and r["warnings"] == []
    assert rows() == ([(ws.PROMPT_VERSION, 2)], ws.PROMPT_VERSION)
    assert db.stat().st_size < before / 4  # vacuumed, not only emptied
    cache = ws._AnswerCache(str(folder))
    assert cache.pruned == 0 and cache.errors == []  # already pruned for this version: nothing to do
    cache.close()
    assert one(QUESTION, path)["requests"] == 0  # the new answers survive later opens
    monkeypatch.setattr(ws, "PROMPT_VERSION", ws.PROMPT_VERSION + 1)
    assert one(QUESTION, path)["requests"] == 1
    assert rows() == ([(ws.PROMPT_VERSION, 2)], ws.PROMPT_VERSION)
