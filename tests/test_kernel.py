import importlib

import pytest

from carbonprice import kernel


def test_transform_module_has_a_public_entrypoint():
    module = importlib.import_module("carbonprice.kernel")
    assert callable(getattr(module, "process", None))
    result = module.process({"id": "sample", "name": "item", "value": 7, "status": "active", "ts": "t1"})
    assert isinstance(result, dict)


# --------------------------------------------------------------------------- #
# Classification: 零成交 / 没有报价 / 单位变了 / 负值 / 科学计数法 / 异常输入
# must stay distinguishable, each with source expression, scale and rationale.
# --------------------------------------------------------------------------- #
CASES = [
    # (raw, expected status, expected value_norm, expected flags)
    (58.5, "ok", 58.5, set()),
    ("58.5", "ok", 58.5, set()),
    (0, "zero_trade", 0.0, {"explicit_zero"}),
    ("0.00", "zero_trade", 0.0, {"explicit_zero"}),
    ("零成交", "zero_trade", 0.0, {"explicit_zero"}),
    ("", "no_quote", None, {"placeholder"}),
    (None, "no_quote", None, {"placeholder"}),
    ("--", "no_quote", None, {"placeholder"}),
    ("暂无报价", "no_quote", None, {"placeholder"}),
    (-12.5, "negative", -12.5, {"negative"}),
    ("1.25e2", "ok", 125.0, {"scientific_notation"}),
    (2500000, "ok", 2500.0, {"unit_scaled"}),
    ("2.5e6", "ok", 2500.0, {"scientific_notation", "unit_scaled"}),
    (5e15, "abnormal", None, {"oversized"}),
    ("1e999", "abnormal", None, {"scientific_notation", "non_finite"}),
    ("not-a-number", "abnormal", None, {"unparseable"}),
]


@pytest.mark.parametrize("raw,status,value_norm,flags", CASES)
def test_classification_is_distinguishable_and_explained(raw, status, value_norm, flags):
    result = kernel.process({"id": "x", "value": raw, "ts": "t"})
    assert result["price_status"] == status
    assert result["value_norm"] == value_norm
    assert flags <= set(result["price_flags"])
    assert result["quarantined"] == (status in ("negative", "abnormal"))
    # Every judgement carries its source expression, scale and reasons.
    assert "raw_value" in result
    assert result["scale_factor"] >= 1
    assert result["rationale"] and all(isinstance(r, str) for r in result["rationale"])
    assert result["rules_version"] == kernel.RULES_VERSION


def test_zero_trade_is_not_no_quote():
    zero = kernel.process({"value": "零成交"})
    none = kernel.process({"value": "暂无报价"})
    assert zero["price_status"] == "zero_trade"
    assert zero["value_norm"] == 0.0
    assert none["price_status"] == "no_quote"
    assert none["value_norm"] is None
    assert zero["raw_value"] == "零成交"
    assert none["raw_value"] == "暂无报价"


def test_unit_change_preserves_source_expression_and_scale():
    result = kernel.process({"value": 2500000})
    assert result["raw_value"] == "2500000"   # 来源表达保留
    assert result["scale_factor"] == 1000     # 适用尺度保留
    assert result["value_norm"] == 2500.0
    assert result["was_large"] == 1           # 兼容旧字段
    assert any("unit" in r for r in result["rationale"])


def test_process_is_pure_and_repeatable():
    record = {"id": "a", "value": "2.5e6", "ts": "t"}
    snapshot = dict(record)
    first = kernel.process(record)
    second = kernel.process(record)
    assert record == snapshot          # input untouched
    assert first == second             # 重复收到同一内容，结果不再改变


def test_reduce_is_order_independent():
    records = [kernel.process({"id": i, "value": v}) | {"id": i}
               for i, v in (("a", 1), ("b", 2), ("c", 3))]
    shuffled = [records[2], records[0], records[1]]
    assert kernel.reduce(records) == kernel.reduce(shuffled)


def test_interpret_day_ignores_arrival_order():
    a = {"id": "S-1", "ts": "2030-01-02", "value": 58.5}
    b = {"id": "S-2", "ts": "2030-01-02", "value": "58.6"}
    ra, rb = (dict(r, **kernel.process(r)) for r in (a, b))
    forward = kernel.interpret_day([ra, rb])
    backward = kernel.interpret_day([rb, ra])
    assert forward == backward
    assert forward["sources"] == ["S-1", "S-2"]
    assert set(forward["raw_expressions"]) == {"58.5", "58.6"}
    assert forward["rationale"]
