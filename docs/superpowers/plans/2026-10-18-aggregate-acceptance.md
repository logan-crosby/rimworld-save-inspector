# Final acceptance aggregation

Owner: root; Beads `raf-hb2`; policy `v2026.09.26.1`.

The aggregate verifies retained evidence, rather than interpreting a successful CI configuration as product success. G0–G7, H0–H7, L0–L7 and A0–A7 are mandatory. Public jobs produce intermediate evidence; the aggregate remains incomplete without private/native and fresh reviewer receipts.

1. Freeze independent validator tests first: a synthetic complete evidence bundle passes validator self-testing only; missing native receipts, wrong candidate/package hashes, skipped/failed/zero tests, altered logs, duplicate receipts, incomplete requirements and missing fresh review fail.
2. Add a read-only bundle validator. Bind all gate receipts to an externally supplied immutable candidate (commit, source tree, wheel, sdist and recorder hashes), frozen requirement register and retained artifact hashes. Require command identities, exact test outcomes and artifact references. Retain existing rigorous G validators as mandatory gate producers; the aggregate does not waive their semantic checks.
3. Require every mandatory requirement to map to passing, authenticated gate checks. Require fresh reviewers distinct from implementers for consequential installed/native/chronology/preservation/fault/discovery/performance results. Keep unavailable optional paid inference separate from required offline adapter checks.
4. Exercise and retain intentionally failed synthetic checks, missing-receipt and wrong-candidate runs. No synthetic bundle can certify real native/private performance.
5. Wire public CI suites and cross-language/build/package checks, while keeping private inputs and licensed assemblies off public runners. Final invocation uses actual local/private/native producers and the final candidate only.

Completion requires all real producers and fresh acceptance to pass. A passing validator unit suite proves only validator behavior.
