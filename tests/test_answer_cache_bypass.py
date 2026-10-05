"""WEB_SIEVE_NO_ANSWER_CACHE=1 makes every answers-cache lookup a miss while still writing.

Why: the calibration timing check needs pairs that are actually sent; a rerun served from
the cache has no wall time and can never adopt. The bypass must not stop writes, so a
measured run still refreshes the cache for later callers.
"""

import importlib.util
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))


@pytest.fixture
def ws():
    spec = importlib.util.spec_from_file_location("web_sieve_mod", os.path.join(HERE, "..", "web-sieve.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bypass_misses_but_still_writes(ws, tmp_path, monkeypatch):
    cache = ws._AnswerCache(str(tmp_path))  # the constructor takes the cache directory
    cache.put([("k1", 0.9, "jev-1.13.0")])
    assert cache.get(["k1"]) == {"k1": (0.9, "jev-1.13.0")}
    monkeypatch.setenv("WEB_SIEVE_NO_ANSWER_CACHE", "1")
    assert cache.get(["k1"]) is None
    cache.put([("k2", 0.4, "jev-1.13.0")])
    monkeypatch.delenv("WEB_SIEVE_NO_ANSWER_CACHE")
    assert cache.get(["k1", "k2"]) == {"k1": (0.9, "jev-1.13.0"), "k2": (0.4, "jev-1.13.0")}


def test_other_values_do_not_bypass(ws, tmp_path, monkeypatch):
    cache = ws._AnswerCache(str(tmp_path))
    cache.put([("k1", 0.9, "jev-1.13.0")])
    monkeypatch.setenv("WEB_SIEVE_NO_ANSWER_CACHE", "0")
    assert cache.get(["k1"]) == {"k1": (0.9, "jev-1.13.0")}
