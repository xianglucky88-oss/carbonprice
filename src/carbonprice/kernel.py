"""Price interpretation kernel.

Turns a raw source record into an explainable price result.  Every result
keeps the source expression (``raw_value``), the applicable unit scale
(``scale_factor``) and the judgement reasons (``rationale``), so situations
that used to collapse into the same number stay distinguishable:

* ``zero_trade`` – the source explicitly reported zero (零成交)
* ``no_quote``   – the source published no quote at all (没有报价)
* ``negative``   – a negative value, invalid for carbon prices
* ``abnormal``   – obviously broken input (unparseable / non-finite / oversized)
* ``ok``         – a usable number, possibly large or expressed in a
                   coarser unit (单位变了); the raw expression is preserved

``process`` is a pure function of its input: no clocks, no randomness, no
batch state.  Receiving the same content twice therefore yields the same
result, and a result never depends on the order records arrived in.

``RULES_VERSION`` is stamped onto every result.  When the rules change,
bump the version: previously published judgements keep their old version
and rationale, so they remain explainable by the rules of their time.
"""
from __future__ import annotations

import json
import math
import re

RULES_VERSION = "v2-2026-11-18"

# A raw magnitude at or above this bound cannot be a plausible carbon
# market figure in any unit; such input is quarantined as abnormal.
PLAUSIBLE_RAW_MAX = 1e12

# Per-unit magnitude bound: at or above it the source is presumed to
# express the value in a coarser unit (千元 / 百万元 ...).  The raw
# expression is always preserved alongside the normalised value.
UNIT_BOUND = 1e6
UNIT_STEP = 1000.0
UNIT_SCALE_MAX = 10 ** 9

STATUS_OK = "ok"
STATUS_ZERO = "zero_trade"
STATUS_NO_QUOTE = "no_quote"
STATUS_NEGATIVE = "negative"
STATUS_ABNORMAL = "abnormal"

#: Statuses whose records are kept for audit but excluded from aggregates
#: and from driving collection progress.
QUARANTINED_STATUSES = frozenset({STATUS_NEGATIVE, STATUS_ABNORMAL})

#: Source expressions that mean "no quote was published" (没有报价).
NO_QUOTE_TOKENS = frozenset({
    "", "-", "--", "—", "/", "n/a", "na", "null", "none", "nil",
    "no quote", "not available",
    "无", "暂无", "无报价", "暂无报价", "没有报价", "无数据", "停牌",
})

#: Source expressions that mean "explicitly zero turnover" (零成交).
ZERO_TOKENS = frozenset({"零成交", "零报价"})

_SCI_RE = re.compile(r"\d\s*[eE][+-]?\d")

_STATUS_RANK = {
    STATUS_OK: 0,
    STATUS_ZERO: 1,
    STATUS_NO_QUOTE: 2,
    STATUS_NEGATIVE: 3,
    STATUS_ABNORMAL: 4,
}


def process(record):
    """Interpret one record.  Pure: same input always yields same output."""
    raw = record.get("value")
    raw_value = raw if (raw is None or isinstance(raw, str)) else str(raw)
    flags, rationale = [], []
    status, value_norm, scale = STATUS_OK, None, 1

    number = None
    if raw is None:
        status = STATUS_NO_QUOTE
        flags.append("placeholder")
        rationale.append(
            "rule=no-quote: field 'value' is absent; the source published no "
            "quote (没有报价), kept distinct from an explicit zero")
    elif isinstance(raw, bool):
        status = STATUS_ABNORMAL
        flags.append("unparseable")
        rationale.append(
            "rule=type: boolean %r is not a price expression; quarantined" % raw)
    elif isinstance(raw, str):
        text = raw.strip()
        if text.lower() in NO_QUOTE_TOKENS:
            status = STATUS_NO_QUOTE
            flags.append("placeholder")
            rationale.append(
                "rule=no-quote-token: raw expression %r means the source "
                "published no quote (没有报价); value_norm left empty, not zero"
                % text)
        elif text in ZERO_TOKENS:
            status = STATUS_ZERO
            value_norm = 0.0
            flags.append("explicit_zero")
            rationale.append(
                "rule=zero-token: raw expression %r explicitly reports zero "
                "turnover (零成交); distinct from a missing quote" % text)
        else:
            if _SCI_RE.search(text):
                flags.append("scientific_notation")
                rationale.append(
                    "rule=notation: %r uses scientific notation; parsed "
                    "exactly as a number" % text)
            try:
                number = float(text)
            except ValueError:
                status = STATUS_ABNORMAL
                flags.append("unparseable")
                rationale.append(
                    "rule=parse: raw expression %r is not numeric; quarantined "
                    "so it cannot distort prices or progress" % text)
    elif isinstance(raw, (int, float)):
        number = float(raw)
    else:
        status = STATUS_ABNORMAL
        flags.append("unparseable")
        rationale.append(
            "rule=type: unsupported value type %s; quarantined"
            % type(raw).__name__)

    if number is not None and status == STATUS_OK:
        if not math.isfinite(number):
            status = STATUS_ABNORMAL
            flags.append("non_finite")
            rationale.append(
                "rule=finite: value %r is not finite; quarantined" % raw_value)
        elif number == 0:
            status = STATUS_ZERO
            value_norm = 0.0
            flags.append("explicit_zero")
            rationale.append(
                "rule=explicit-zero: explicit zero quote (零成交); a real "
                "observation, not a missing quote")
        elif number < 0:
            status = STATUS_NEGATIVE
            value_norm = round(number, 4)
            flags.append("negative")
            rationale.append(
                "rule=non-negative: carbon prices cannot be negative; kept "
                "for audit and quarantined from aggregates and progress")
        elif abs(number) >= PLAUSIBLE_RAW_MAX:
            status = STATUS_ABNORMAL
            flags.append("oversized")
            rationale.append(
                "rule=plausibility: |value| >= %.0e exceeds any plausible "
                "market magnitude; quarantined" % PLAUSIBLE_RAW_MAX)
        else:
            v = number
            while abs(v) >= UNIT_BOUND and scale <= UNIT_SCALE_MAX:
                v /= UNIT_STEP
                scale *= int(UNIT_STEP)
            if scale > 1:
                flags.append("unit_scaled")
                rationale.append(
                    "rule=unit-scale: raw magnitude %.6g exceeds the per-unit "
                    "bound; interpreted with scale_factor=%d (单位尺度), raw "
                    "expression preserved" % (number, scale))
            else:
                rationale.append(
                    "rule=parse: numeric value within the normal range")
            value_norm = round(v, 4)

    return {
        "raw_value": raw_value,
        "value_norm": value_norm,
        "scale_factor": scale,
        "was_large": int(scale > 1),
        "price_status": status,
        "price_flags": sorted(set(flags)),
        "quarantined": status in QUARANTINED_STATUSES,
        "rationale": rationale,
        "rules_version": RULES_VERSION,
    }


def _canon(record):
    return json.dumps(record, sort_keys=True, ensure_ascii=False, default=str)


def reduce(records):
    """Canonical, arrival-order-independent ordering of a batch."""
    return sorted(records, key=_canon)


def interpret_day(records):
    """Deterministic interpretation of one trading day across sources.

    The result never depends on the order the sources arrived in:
    candidates are ranked by (quarantined, status rank, canonical content)
    and the winner is returned together with every source expression seen.
    """
    candidates = list(records)
    if not candidates:
        return None
    ordered = sorted(
        candidates,
        key=lambda r: (bool(r.get("quarantined")),
                       _STATUS_RANK.get(r.get("price_status"), 9),
                       _canon(r)))
    chosen = ordered[0]
    return {
        "ts": chosen.get("ts"),
        "price_status": chosen.get("price_status"),
        "value_norm": chosen.get("value_norm"),
        "scale_factor": chosen.get("scale_factor", 1),
        "sources": sorted(str(r.get("id", "")) for r in candidates),
        "raw_expressions": sorted({str(r.get("raw_value")) for r in candidates}),
        "rationale": [
            "rule=day-merge: %d source expression(s) ranked by (quarantined, "
            "status, content); arrival order ignored" % len(candidates)
        ] + list(chosen.get("rationale", [])),
        "rules_version": chosen.get("rules_version", RULES_VERSION),
    }
