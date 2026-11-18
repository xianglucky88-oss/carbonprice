"""Behavioural verification for the collection pipeline.

Covers: small/large trades, duplicate pages, rate limiting, out-of-order
backfill, partial failure, full re-run, incremental continuation, rule
updates, and the isolation of bad records from good trades and progress.
"""
import json
import sqlite3

import pytest

from carbonprice import framework, kernel
from carbonprice.pipeline import run
from carbonprice.store import Store

TS = "2030-01-0%dT00:00:00Z"


def rec(i, day, value, source="EX"):
    return {"id": "%s-%03d" % (source, i), "name": "Allowance",
            "value": value, "status": "active", "ts": TS % day}


def make_transport(pages):
    """Fake transport: {page: (status, items)}; missing pages are empty."""
    class FakeTransport:
        cookies = {}

        def request(self, method, path, params=None, json_body=None, headers=None):
            page = int((params or {}).get("page", 1))
            status, items = pages.get(page, (200, []))
            body = json.dumps({"items": items or []})
            return status, {}, body

    return FakeTransport()


@pytest.fixture
def con():
    c = sqlite3.connect(":memory:")
    Store(c)  # initialise schema
    yield c
    c.close()


def payloads(c):
    return sorted(json.dumps(p, sort_keys=True, ensure_ascii=False)
                  for p in Store(c).all_records())


def test_small_and_large_trades_are_both_kept(monkeypatch, con):
    monkeypatch.setattr(framework, "Transport",
                        lambda: make_transport({1: (200, [rec(1, 1, 58.5),
                                                          rec(2, 1, 2500000)])}))
    result = run(con=con)
    assert result["ingested"] == 2
    stored = {p["id"]: p for p in Store(con).all_records()}
    small, large = stored["EX-001"], stored["EX-002"]
    assert small["price_status"] == "ok" and small["scale_factor"] == 1
    assert large["price_status"] == "ok" and large["scale_factor"] == 1000
    assert large["raw_value"] == "2500000" and large["quarantined"] is False


def test_full_rerun_is_idempotent(monkeypatch, con):
    pages = {1: (200, [rec(1, 1, 58.5), rec(2, 2, 60.1)])}
    monkeypatch.setattr(framework, "Transport", lambda: make_transport(pages))
    first = run(con=con)
    snapshot = payloads(con)
    second = run(con=con)  # 全量重跑
    assert first["ingested"] == 2
    assert second["ingested"] == 0
    assert payloads(con) == snapshot
    assert second["watermark"] == first["watermark"]


def test_duplicate_pages_are_deduplicated(monkeypatch, con):
    rows = [rec(i, 1, 50 + i) for i in range(1, 5)]
    pages = {1: (200, rows), 2: (200, rows[:2])}  # page 2 repeats page 1
    monkeypatch.setattr(framework, "Transport", lambda: make_transport(pages))
    result = run(con=con)
    assert result["fetched"] == 6
    assert result["duplicates"] == 2
    assert result["count"] == 4


def test_rate_limit_is_retried_with_backoff():
    from mockapp.main import app
    app.__dict__.pop("_flaky_n", None)  # /probe returns 429 twice, then 200
    sleeper = framework.Sleeper()
    http = framework.Http(framework.Transport(), sleeper)
    status, _hdr, body, attempts = http.get("/probe")
    assert status == 200
    assert attempts == 2
    assert json.loads(body)["ok"] is True
    assert sleeper.slept > 0  # politeness + exponential backoff recorded


def test_arrival_order_does_not_change_the_day(monkeypatch):
    a, b = rec(1, 1, 58.5, "S1"), rec(2, 1, "58.6", "S2")

    def fresh():
        c = sqlite3.connect(":memory:")
        Store(c)
        return c

    # Order 1: both sources in one batch.
    c1 = fresh()
    monkeypatch.setattr(framework, "Transport", lambda: make_transport({1: (200, [a, b])}))
    run(con=c1)
    # Order 2: the same sources arrive in two batches, reversed.
    c2 = fresh()
    monkeypatch.setattr(framework, "Transport", lambda: make_transport({1: (200, [b])}))
    run(con=c2)
    monkeypatch.setattr(framework, "Transport", lambda: make_transport({1: (200, [a])}))
    run(con=c2)

    assert payloads(c1) == payloads(c2)
    assert Store(c1).watermark() == Store(c2).watermark()
    day1 = [p for p in Store(c1).all_records() if p["ts"] == a["ts"]]
    day2 = [p for p in Store(c2).all_records() if p["ts"] == a["ts"]]
    assert kernel.interpret_day(day1) == kernel.interpret_day(day2)
    c1.close()
    c2.close()


def test_out_of_order_backfill_never_moves_progress_backwards(monkeypatch, con):
    monkeypatch.setattr(framework, "Transport",
                        lambda: make_transport({1: (200, [rec(3, 3, 61.0)])}))
    run(con=con)
    assert Store(con).watermark() == TS % 3
    # 乱序补报: an older trading day arrives after a newer one.
    monkeypatch.setattr(framework, "Transport",
                        lambda: make_transport({1: (200, [rec(1, 1, 55.0)])}))
    run(con=con)
    assert Store(con).watermark() == TS % 3
    assert Store(con).count() == 2


def test_partial_failure_keeps_good_pages_and_holds_watermark(monkeypatch, con):
    rows = [rec(i, i, 50 + i) for i in range(1, 5)]
    broken = {1: (200, rows), 2: (500, None), 3: (200, [rec(5, 5, 66.0)])}
    monkeypatch.setattr(framework, "Transport", lambda: make_transport(broken))
    result = run(con=con)
    assert result["page_errors"] == [{"page": 2, "status": 500}]
    assert result["count"] == 5          # good pages unaffected
    assert Store(con).watermark() is None  # 局部失败不得推进进度

    fixed = {1: (200, rows), 2: (200, [rec(5, 5, 66.0)])}
    monkeypatch.setattr(framework, "Transport", lambda: make_transport(fixed))
    again = run(con=con)
    assert again["page_errors"] == []
    assert again["ingested"] == 0        # everything was already stored
    assert Store(con).watermark() == TS % 5


def test_bad_record_is_quarantined_and_cannot_advance_progress(monkeypatch, con):
    good = rec(1, 1, 58.5)
    bad = dict(rec(2, 1, "garbage"), ts="9999-12-31T00:00:00Z")
    monkeypatch.setattr(framework, "Transport",
                        lambda: make_transport({1: (200, [good, bad])}))
    result = run(con=con)
    assert result["quarantined"] == 1
    assert result["count"] == 2           # kept for audit …
    assert Store(con).watermark() == good["ts"]  # … but progress follows good data
    stored = {p["id"]: p for p in Store(con).all_records()}
    assert stored[good["id"]]["price_status"] == "ok"
    assert stored[bad["id"]]["quarantined"] is True
    assert stored[bad["id"]]["rationale"]


def test_kernel_failure_is_isolated_per_record(monkeypatch, con):
    original = kernel.process

    def flaky(record):
        if record.get("id") == "EX-002":
            raise RuntimeError("boom")
        return original(record)

    monkeypatch.setattr(kernel, "process", flaky)
    monkeypatch.setattr(framework, "Transport",
                        lambda: make_transport({1: (200, [rec(1, 1, 58.5),
                                                          rec(2, 2, 60.1)])}))
    result = run(con=con)
    assert result["count"] == 2
    assert result["quarantined"] == 1
    stored = {p["id"]: p for p in Store(con).all_records()}
    assert stored["EX-001"]["price_status"] == "ok"  # 坏记录不影响正常成交
    assert "kernel failed" in stored["EX-002"]["rationale"][0]
    assert Store(con).watermark() == TS % 1


def test_incremental_continuation_ingests_only_new_rows(monkeypatch, con):
    monkeypatch.setattr(framework, "Transport",
                        lambda: make_transport({1: (200, [rec(1, 1, 50.0),
                                                          rec(2, 2, 51.0)])}))
    run(con=con)
    later = {1: (200, [rec(1, 1, 50.0), rec(2, 2, 51.0), rec(3, 3, 52.0)])}
    monkeypatch.setattr(framework, "Transport", lambda: make_transport(later))
    result = run(mode="incremental", con=con)  # 增量续跑
    assert result["ingested"] == 1
    assert Store(con).count() == 3
    assert Store(con).watermark() == TS % 3
    again = run(mode="incremental", con=con)
    assert again["ingested"] == 0


def test_rule_update_keeps_past_judgements_explainable(monkeypatch, con):
    original_version = kernel.RULES_VERSION
    pages = {1: (200, [rec(1, 1, 2500000)])}
    monkeypatch.setattr(framework, "Transport", lambda: make_transport(pages))
    run(con=con)
    old = Store(con).all_records()
    assert [p["rules_version"] for p in old] == [original_version]

    monkeypatch.setattr(kernel, "RULES_VERSION", "v-test-next")
    run(con=con)  # same content, updated rules
    versions = sorted(p["rules_version"] for p in Store(con).all_records())
    assert versions == sorted([original_version, "v-test-next"])
    # 过去发布的判断仍带着当时的依据。
    kept = [p for p in Store(con).all_records()
            if p["rules_version"] == original_version]
    assert kept and kept[0]["rationale"] and kept[0]["raw_value"] == "2500000"
    assert run(con=con)["ingested"] == 0  # still idempotent under new rules
