# Baseline-vs-assistant rubric

Same fixture, same oracle (`fixtures/oracle.json`), same digest (`rules-digest.json`).

| Metric | How it is scored | Rules-only digest (measured, L1 [MODEL]) | Assistant explanation |
| --- | --- | --- | --- |
| Grounded claims | claims traceable to a digest field | all (it is the digest) | `not-run` |
| Ungrounded claims | claims with no digest field behind them | 0 | `not-run` |
| Numeric mismatches against `oracle.json` | any figure not equal to the oracle Decimal string | 0 (T1) | `not-run` |
| Confirmation burden | owner questions per month (duplicate-charge flags, uncertain entries) | 1 flag, 0 uncertain | `not-run` |
| Latency | seconds, measured end to end | not measured | `not-run` |
| Cost | tokens times the provider price, reported with the price source | 0 model tokens | `not-run` |

Reason for `not-run`: a model call needs a provider and a runtime that this research approval
does not authorize, and the fixture must not leave the repository. A numeric mismatch, or an
ungrounded claim that changes a figure, fails the run regardless of the other metrics.
