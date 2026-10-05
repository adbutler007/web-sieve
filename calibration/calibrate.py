#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp[cli]<2"]
# ///
"""Calibrate find_relevant_ranges against labelled pages (docs/jev-relevance-spec.md, section 15).

Files, all under calibration/data/, which is gitignored: the repository is
public and the page list and labels name private project directories.

    pages.json             the calibration pages: [{"n", "page", "title", "why"}],
                           `page` relative to --root (default ~/Projects)
    labels.jsonl           one line per page from a Sonnet labeller (spec 15.2):
                           {"page", "sha256", "questions": [{"kind": "local"|"spread"|"absent",
                            "question", "reasoning", "relevant", "ranges": [[start, end], ...]}]}
    labels_second.jsonl    optional, a second labeller for some pages, same questions (agreement ceiling)
    haiku_baseline.jsonl   one line per (page, question) from a Haiku subagent (spec 15.3):
                           {"page", "question", "output": "<the agent's full text>", "usage": {...}}
    results/YYYY-MM-DD.json  written by a run: every window's p per pair and configuration,
                           the metrics, and the decision
    run.status             pid, start time and estimate; an EXIT line when the run ends

Usage:
    uv run --script calibration/calibrate.py --check     # pages and labels only; no Jev requests
    uv run --script calibration/calibrate.py --one       # smoke test: one pair, the default configuration
    gtimeout 1200 uv run --script calibration/calibrate.py --configs all
    gtimeout 1200 uv run --script calibration/calibrate.py --configs C1,C2,C2h

--configs default runs the configuration that matches web-sieve.py's
constants (WINDOW_TOKENS, WINDOWS_PER_REQUEST, STRIP_LINKS, SECTION_LINE).

The run stops at the first result whose status is not ok, writes what it
has to the output file and exits 1. Answers are cached in each page's
.web_cache/jev_answers.sqlite, so a rerun pays only for what is missing.
"""

import argparse
import hashlib
import json
import os
import statistics
import sys
import time
from datetime import date, datetime, timezone
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
CONFIGS = {  # name: (window_tokens, windows per request, strip links, Section line); "h" adds the Section line
    "C1": (400, 16, True, False), "C2": (200, 16, True, False), "C2h": (200, 16, True, True),
    "C3": (800, 16, True, False), "C4": (400, 1, True, False), "C5": (400, 4, True, False),
    "C6": (400, 16, False, False),
}
KINDS = ("local", "spread", "absent")
THRESHOLDS = [round(0.05 * k, 2) for k in range(1, 20)]
MIN_RECALL = 0.90                # decision rule step 1
RECALL_SLACK = 0.02              # step 3: recall at least the Haiku recall minus this
TIE_PRECISION = 0.02             # step 2: precision ties go to fewer requests
MAX_MEDIAN_WALL_S = 2.0          # step 3
BRIDGE_MIN_GAIN, BRIDGE_MAX_EXTRA = 0.02, 0.10  # spec 7.3
HEADING_RECALL_SLACK = 0.01      # the Section line stays when, at DEFAULT_THRESHOLD, recall is at least the
                                 # recall without it minus this and precision is not lower (2026-10-05)
ESTIMATE_S_PER_CONFIG = 120


def load_web_sieve():
    loader = SourceFileLoader("web_sieve", os.path.join(os.path.dirname(HERE), "web-sieve.py"))
    module = module_from_spec(spec_from_loader("web_sieve", loader))
    loader.exec_module(module)
    return module


ws = load_web_sieve()


def fail(message: str) -> None:
    sys.exit(f"calibrate: {message}")


def atomic_write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def read_jsonl(path: str) -> list:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def load_pages(root: str) -> dict:
    """{relative page: {"path", "sha256", "lines", "body_start", "meta", ...}} from pages.json."""
    listing = os.path.join(DATA, "pages.json")
    if not os.path.exists(listing):
        fail(f"{listing} is missing; it lists the calibration pages (spec 15.1)")
    pages = {}
    for entry in json.load(open(listing, encoding="utf-8")):
        path = os.path.join(root, entry["page"])
        try:
            lines, body_start, meta = ws._read_cached_page(path)
        except ws._SourceError as e:
            fail(f"page {entry['n']} {entry['page']}: {e.kind}: {e.message}")
        with open(path, "rb") as f:
            sha = hashlib.sha256(f.read()).hexdigest()
        pages[entry["page"]] = dict(entry, path=path, sha256=sha, lines=lines, body_start=body_start, meta=meta)
    return pages


def body_lines(page: dict) -> set:
    """Non-blank body lines: the universe for line-level metrics."""
    return {n for n in range(page["body_start"], len(page["lines"]) + 1) if page["lines"][n - 1].strip()}


def lines_in(ranges: list, universe: set) -> set:
    return {n for start, end in ranges for n in range(start, end + 1)} & universe


def check_labels(records: list, pages: dict, name: str, complete: bool = True) -> list:
    """Spec 15.2 checks: sha256 matches, three questions of the three kinds,
    every range inside the body, absent has no ranges. Returns the pairs."""
    pairs, seen = [], set()
    for rec in records:
        page = pages.get(rec.get("page"))
        if page is None:
            fail(f"{name}: page {rec.get('page')!r} is not in pages.json")
        if rec.get("sha256") != page["sha256"]:
            fail(f"{name}: sha256 of {rec['page']} does not match the file (the page changed or the label is stale)")
        if rec["page"] in seen:
            fail(f"{name}: {rec['page']} is labelled twice")
        seen.add(rec["page"])
        questions = rec.get("questions") or []
        if sorted(q.get("kind") for q in questions) != sorted(KINDS):
            fail(f"{name}: {rec['page']} needs exactly one local, one spread and one absent question")
        for q in questions:
            ranges = q.get("ranges") or []
            for start, end in ranges:
                if not page["body_start"] <= start <= end <= len(page["lines"]):
                    fail(f"{name}: {rec['page']} {q['kind']} range {[start, end]} is outside the body "
                         f"[{page['body_start']}, {len(page['lines'])}]")
            if q["kind"] == "absent" and (ranges or q.get("relevant") is not False):
                fail(f"{name}: {rec['page']} absent question must have relevant false and no ranges")
            if q["kind"] != "absent" and (not ranges or q.get("relevant") is not True):
                fail(f"{name}: {rec['page']} {q['kind']} question must have relevant true and ranges")
            universe = body_lines(page)
            pairs.append({"id": f"{page['n']}-{q['kind']}", "page": rec["page"], "kind": q["kind"],
                          "question": q["question"], "gold": lines_in(ranges, universe), "universe": universe})
    if complete and seen != set(pages):
        fail(f"{name}: pages without labels: {sorted(set(pages) - seen)}")
    return pairs


def score(pairs: list, predicted: list) -> dict:
    """Pooled line-level metrics; predicted is [(selected lines, page-level relevant)] per pair."""
    tp = fp = fn = selected = universe = 0
    by_kind, misses, alarms = {k: [0, 0] for k in KINDS if k != "absent"}, [], []
    for pair, (pred, relevant) in zip(pairs, predicted):
        pred = pred & pair["universe"]
        tp, fp, fn = tp + len(pred & pair["gold"]), fp + len(pred - pair["gold"]), fn + len(pair["gold"] - pred)
        selected, universe = selected + len(pred), universe + len(pair["universe"])
        if pair["kind"] in by_kind:
            by_kind[pair["kind"]][0] += len(pred & pair["gold"])
            by_kind[pair["kind"]][1] += len(pair["gold"])
            if not relevant:
                misses.append(pair["id"])
        elif relevant:
            alarms.append(pair["id"])
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = 2 * precision * recall / (precision + recall) if precision and recall else 0.0
    return {"precision": precision, "recall": recall, "f1": f1,
            "recall_by_kind": {k: (v[0] / v[1] if v[1] else None) for k, v in by_kind.items()},
            "fraction_selected": selected / universe if universe else None,
            "page_misses": misses, "false_alarms": alarms}


def tool_prediction(result: dict, page: dict, threshold: float, bridge: bool) -> tuple:
    windows = [{"start": s, "end": e, "section": ""} for s, e, _ in result["windows"]]
    probs = [p for _, _, p in result["windows"]]
    if bridge:  # select one unselected window lying between two selected windows (spec 7.3)
        sel = [p is not None and p >= threshold for p in probs]
        probs = [threshold if 0 < i < len(probs) - 1 and not sel[i] and sel[i - 1] and sel[i + 1] else p
                 for i, p in enumerate(probs)]
    ranges = ws._merge_ranges(windows, probs, threshold, page["lines"])[0]
    return lines_in(ranges, body_lines(page)), any(p is not None and p >= threshold for p in probs)


def histogram(runs: list, pairs: list, pages: dict) -> dict:
    """Counts of window p in ten bins, for windows with at least one gold line and for the rest."""
    out = {"gold": [0] * 10, "other": [0] * 10}
    for pair, run in zip(pairs, runs):
        for start, end, p in run["result"]["windows"]:
            has_gold = any(n in pair["gold"] for n in range(start, end + 1))
            out["gold" if has_gold else "other"][min(int(p * 10), 9)] += 1
    return out


def haiku_prediction(record: dict, page: dict) -> tuple:
    """(selected lines, relevant, parsed) from a Haiku agent's full output."""
    text = record.get("output", "").strip()
    if text.startswith("```"):  # the first fenced block; an agent may add prose after it
        text = text[3:].split("```", 1)[0].removeprefix("json").strip()
    try:
        parsed = json.loads(text)
        ranges = [[int(a), int(b)] for a, b in parsed.get("ranges", [])]
        relevant = parsed.get("relevant") is True
    except (ValueError, TypeError, AttributeError):
        return set(), False, False
    return lines_in(ranges, body_lines(page)), relevant, True


def agreement(pairs_first: list, pairs_second: list) -> dict:
    """Line-level F1 between two labellers on the pages both labelled."""
    second = {(p["page"], p["kind"]): p for p in pairs_second}
    common = [p for p in pairs_first if (p["page"], p["kind"]) in second]
    s = score(common, [(second[(p["page"], p["kind"])]["gold"], True) for p in common])
    return {"pairs": len(common), "f1": s["f1"], "precision": s["precision"], "recall": s["recall"]}


def decide(metrics: dict, costs: dict, haiku: dict) -> dict:
    """Spec 15.5 (revised 2026-10-05): per configuration the highest threshold at which pooled
    recall is at least 0.90, recall is at least Haiku's minus RECALL_SLACK, and there are no page
    misses; then the highest precision (ties within 0.02 to fewer requests); then the remaining
    adoption condition (median wall time). A threshold that fails the recall or page-miss
    conditions is skipped in favour of the next lower one, so a configuration is never
    represented by a point that cannot be adopted."""
    haiku_recall = haiku.get("recall")

    def qualifies(r: dict) -> bool:
        return (not r["bridge"] and r["recall"] is not None and r["recall"] >= MIN_RECALL
                and not r["page_misses"]
                and haiku_recall is not None and r["recall"] >= haiku_recall - RECALL_SLACK)

    candidates = []
    for name, rows in metrics.items():
        ok = [r for r in rows if qualifies(r)]
        if ok:
            best = max(ok, key=lambda r: r["threshold"])
            candidates.append({"config": name, **best, "requests_needed": costs[name]["requests_needed"]})
    if not candidates:
        return {"adopt": False, "reason": (f"no configuration has a threshold with pooled recall >= {MIN_RECALL}, "
                                           f"recall >= Haiku's minus {RECALL_SLACK} and no page misses")}
    top = max(c["precision"] or 0 for c in candidates)
    close = [c for c in candidates if (c["precision"] or 0) >= top - TIE_PRECISION]
    chosen = min(close, key=lambda c: (c["requests_needed"], -(c["precision"] or 0)))
    wall = costs[chosen["config"]]["median_wall_s"]  # None when every pair was answered from the cache
    checks = {
        "recall_vs_haiku": chosen["recall"] >= haiku_recall - RECALL_SLACK,
        "no_page_misses": not chosen["page_misses"],
        "median_wall_s": wall is not None and wall <= MAX_MEDIAN_WALL_S,
    }
    note = ("" if wall is not None else
            "median wall time not measured: every pair came from the answers cache; the timing check "
            "needs a run with uncached pairs (a new question, or --from-results on a measured run)")
    bridged = next(r for r in metrics[chosen["config"]] if r["bridge"] and r["threshold"] == chosen["threshold"])
    bridge = ((bridged["recall"] or 0) - chosen["recall"] >= BRIDGE_MIN_GAIN
              and (bridged["fraction_selected"] or 0) <= (chosen["fraction_selected"] or 0) * (1 + BRIDGE_MAX_EXTRA))
    return {"adopt": all(checks.values()), "checks": checks, "note": note, "chosen": chosen, "bridge": bridge,
            "bridge_metrics": bridged, "haiku_recall": haiku_recall,
            "section_line": section_line_rule(metrics), "candidates": candidates}


def section_line_rule(metrics: dict) -> list:
    """For each configuration X with a twin Xh (the same plus the Section line), both run: at
    DEFAULT_THRESHOLD, keep the line when Xh's recall is at least X's minus HEADING_RECALL_SLACK and
    Xh's precision is not lower."""
    out, t = [], ws.DEFAULT_THRESHOLD
    for base in sorted(metrics):
        if base + "h" not in metrics:
            continue
        a, b = (next(r for r in metrics[n] if not r["bridge"] and r["threshold"] == t) for n in (base, base + "h"))
        keep = (b["recall"] or 0) >= (a["recall"] or 0) - HEADING_RECALL_SLACK and (b["precision"] or 0) >= (a["precision"] or 0)
        out.append({"without": base, "with": base + "h", "threshold": t, "recall": [a["recall"], b["recall"]],
                    "precision": [a["precision"], b["precision"]], "keep_section_line": keep})
    return out


def jsonable(value):
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(type(value).__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--labels", default=os.path.join(DATA, "labels.jsonl"))
    parser.add_argument("--labels-second", default=os.path.join(DATA, "labels_second.jsonl"))
    parser.add_argument("--haiku", default=os.path.join(DATA, "haiku_baseline.jsonl"))
    parser.add_argument("--root", default=os.path.expanduser("~/Projects"), help="directory pages.json is relative to")
    parser.add_argument("--configs", default="default",
                        help="default, all, or names separated by commas (for example C1,C2,C2h)")
    parser.add_argument("--one", action="store_true",
                        help="smoke test: one pair with the first configuration named, nothing written")
    parser.add_argument("--check", action="store_true", help="check pages.json and the labels; send nothing")
    parser.add_argument("--out", default=os.path.join(DATA, "results", f"{datetime.now().strftime('%Y-%m-%dT%H%M')}.json"),
                        help="results file; the default carries the date and time so a rerun never overwrites a measured run")
    parser.add_argument("--from-results", metavar="FILE",
                        help="re-run the decision rule on a saved results file and print it; sends nothing")
    args = parser.parse_args()

    if args.from_results:
        with open(args.from_results) as f:
            saved = json.load(f)
        decision = decide(saved["metrics"], saved["costs"], saved.get("haiku") or {})
        print(json.dumps(decision, indent=2, default=jsonable))
        return 0 if decision["adopt"] else 1

    pages = load_pages(args.root)
    if args.check and not os.path.exists(args.labels):
        for p in sorted(pages.values(), key=lambda p: p["n"]):
            print(f"{p['n']}  {p['page']}  sha256={p['sha256']}  body=[{p['body_start']}, {len(p['lines'])}]")
        print(f"no labels yet at {args.labels}")
        return 0
    if not os.path.exists(args.labels):
        fail(f"{args.labels} is missing; write the labels first (spec 15.2)")
    pairs = check_labels(read_jsonl(args.labels), pages, "labels")
    second = (check_labels(read_jsonl(args.labels_second), pages, "labels_second", complete=False)
              if os.path.exists(args.labels_second) else [])
    if args.check:
        print(f"labels ok: {len(pairs)} pairs on {len(pages)} pages" + (f"; second labeller: {len(second)} pairs" if second else ""))
        return 0

    current = (ws.WINDOW_TOKENS, ws.WINDOWS_PER_REQUEST, ws.STRIP_LINKS, ws.SECTION_LINE)
    if args.configs == "default":
        names = [next((n for n, c in CONFIGS.items() if c == current), None)]
        if names[0] is None:
            fail(f"no configuration matches web-sieve.py's constants {current}; add one to CONFIGS")
    elif args.configs == "all":
        names = list(CONFIGS)
    else:
        names = [n.strip() for n in args.configs.split(",") if n.strip()]
        unknown = [n for n in names if n not in CONFIGS]
        if unknown or not names:
            fail(f"unknown configuration {unknown or args.configs!r}; known: {', '.join(CONFIGS)}")
    names = names[:1] if args.one else names
    todo = pairs[:1] if args.one else pairs
    status_path = os.path.join(DATA, "run.status")
    if not args.one:
        atomic_write(status_path, f"pid={os.getpid()} start={datetime.now(timezone.utc).isoformat()} "
                                  f"estimate_s={ESTIMATE_S_PER_CONFIG * len(names)} configs={','.join(names)} "
                                  f"pairs={len(todo)}\n")
    runs, code = {name: [] for name in names}, 0
    for name in names:
        window_tokens, batch_size, strip_links, section_line = CONFIGS[name]
        for pair in todo:
            page = pages[pair["page"]]
            result = ws._relevance(pair["question"], [page["path"]], os.path.dirname(page["path"]), None,
                                   window_tokens, ws.MAX_WINDOWS, batch_size=batch_size, strip_links=strip_links,
                                   section_line=section_line)[0]
            runs[name].append({"pair": pair["id"], "result": result})
            print(f"{name} {pair['id']:<10} {result['status']:<8} requests={result['requests']:<3} "
                  f"hits={result['cache_hits']:<3} tokens={result['input_tokens']:<7} "
                  f"cost=${result['cost_usd']:.6f} ms={result['elapsed_ms']}", flush=True)
            for warning in result["warnings"]:
                print(f"  warning: {warning}", flush=True)
            if result["status"] != "ok":
                print(f"stopping: {pair['id']} under {name} is {result['status']}: {result.get('error')}", flush=True)
                code = 1
                break
        if code:
            break
    if args.one:
        r, pair = runs[names[0]][0]["result"], todo[0]
        print(json.dumps({"config": names[0], "pair": pair["id"], "question": pair["question"], "status": r["status"],
                          "ranges": r["ranges"], "gold_lines": len(pair["gold"]), "requests": r["requests"],
                          "input_tokens": r["input_tokens"], "cost_usd": r["cost_usd"],
                          "elapsed_ms": r["elapsed_ms"]}, indent=2))
        return code

    report = {"date": datetime.now(timezone.utc).isoformat(), "model": ws.JEV_MODEL,
              "prompt_version": ws.PROMPT_VERSION, "configs": {n: CONFIGS[n] for n in names},
              "labels": args.labels, "pairs": pairs, "runs": runs}
    if not code:
        haiku_records = read_jsonl(args.haiku) if os.path.exists(args.haiku) else []
        by_pair = {(h["page"], h["question"]): h for h in haiku_records}
        haiku_pred, unparsed, missing = [], 0, 0
        for pair in pairs:
            record = by_pair.get((pair["page"], pair["question"]))
            if record is None:
                missing += 1
                haiku_pred.append((set(), False))
                continue
            lines, relevant, parsed = haiku_prediction(record, pages[pair["page"]])
            unparsed += not parsed
            haiku_pred.append((lines, relevant))
        haiku = dict(score(pairs, haiku_pred), records=len(haiku_records), missing=missing, unparsed=unparsed,
                     usage=[h.get("usage") for h in haiku_records]) if haiku_records else {}
        if not haiku_records:
            print(f"warning: no Haiku baseline at {args.haiku}; the recall check cannot pass, so the decision is not to adopt")
        elif missing or unparsed:
            print(f"warning: Haiku baseline has {missing} missing and {unparsed} unparsable outputs, scored as no ranges")
        metrics, costs, hists = {}, {}, {}
        for name in names:
            results = [run["result"] for run in runs[name]]
            metrics[name] = [dict(score(pairs, [tool_prediction(r, pages[p["page"]], t, bridge)
                                                for r, p in zip(results, pairs)]), threshold=t, bridge=bridge)
                             for t in THRESHOLDS for bridge in (False, True)]
            # Timing counts only pairs sent in full: a cache hit takes no time and would flatter the check.
            walls = [r["elapsed_ms"] / 1000 for r in results if r["cache_hits"] == 0]
            costs[name] = {"requests": sum(r["requests"] for r in results),
                           "requests_needed": sum(r["requests"] + r["cache_hits"] for r in results),
                           "input_tokens": sum(r["input_tokens"] for r in results),
                           "cost_usd": round(sum(r["cost_usd"] for r in results), 6),
                           "uncached_pairs": len(walls), "wall_s": round(sum(walls), 3),
                           "median_wall_s": statistics.median(walls) if walls else None}
            hists[name] = histogram(runs[name], pairs, pages)
        report.update(metrics=metrics, costs=costs, histograms=hists, haiku=haiku,
                      agreement=agreement(pairs, second) if second else None,
                      decision=decide(metrics, costs, haiku))
        for name in names:
            print(f"\n{name} {CONFIGS[name]} cost {costs[name]}")
            print("  t     P      R      F1     selected  misses  alarms")
            for row in metrics[name]:
                if not row["bridge"]:
                    print(f"  {row['threshold']:.2f}  {row['precision'] or 0:.3f}  {row['recall'] or 0:.3f}  "
                          f"{row['f1']:.3f}  {row['fraction_selected'] or 0:.3f}     "
                          f"{len(row['page_misses']):<6}  {len(row['false_alarms'])}")
        if haiku:
            print(f"\nHaiku baseline: P={haiku['precision']} R={haiku['recall']} misses={haiku['page_misses']}")
        if report["agreement"]:
            print(f"labeller agreement: {report['agreement']}")
        print("\ndecision:", json.dumps({k: v for k, v in report["decision"].items() if k != "candidates"},
                                        default=jsonable, indent=2))
    atomic_write(args.out, json.dumps(report, default=jsonable, indent=1))
    print(f"wrote {args.out}")
    with open(status_path, "a") as f:
        f.write(f"EXIT code={code} end={datetime.now(timezone.utc).isoformat()}\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
