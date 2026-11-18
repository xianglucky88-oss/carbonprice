# carbonprice

A small offline data-processing service with a local HTTP fixture and SQLite storage. It is self-contained and does not require network access.

## Usage

```text
make run       # process the bundled source
make run_inc   # process using the incremental command-line mode
make test      # run the test suite
make mock      # start the local fixture service
```

The package exposes a Python entry point under `src/carbonprice`. The fixture service supports the source formats used by the package, and the command-line entry point writes results to the local SQLite database. Re-running commands is safe for an existing database.

## Price interpretation

Every collected record is interpreted by `carbonprice.kernel` and stored
with its full judgement attached:

| field          | meaning                                                        |
| -------------- | -------------------------------------------------------------- |
| `raw_value`    | the source expression, exactly as received (来源表达)          |
| `value_norm`   | the normalised numeric value, or `null` when there is none     |
| `scale_factor` | the applicable unit scale (适用尺度), e.g. `1000` for 千元     |
| `price_status` | `ok` / `zero_trade` / `no_quote` / `negative` / `abnormal`     |
| `price_flags`  | observations such as `scientific_notation`, `unit_scaled`      |
| `quarantined`  | kept for audit but excluded from aggregates and progress       |
| `rationale`    | the rules that fired, in plain text (判断理由)                 |
| `rules_version`| the rule set that produced this judgement                      |

This keeps expressions that used to collapse into the same number
distinguishable: an explicit zero (零成交) is not a missing quote
(没有报价), a unit change (单位变了) preserves its raw expression and
scale, and negative, scientific-notation, legitimately large and obviously
abnormal inputs each carry their own explanation.

## Guarantees

* **Idempotent** — receiving the same content again changes nothing;
  re-running `make run` on an existing database is a no-op.
* **Order independent** — sources arriving in any order (including
  out-of-order backfill) yield the same stored interpretation of a trading
  day; `kernel.interpret_day` merges same-day sources deterministically.
* **Explainable over time** — judgements are stamped with `rules_version`.
  When the rules change, previously published judgements are retained with
  their original version and rationale.
* **Failure isolation** — a bad record is quarantined without affecting good
  trades, and never advances the watermark; a partially failing fetch
  stores the good pages but holds the watermark so nothing is skipped.
