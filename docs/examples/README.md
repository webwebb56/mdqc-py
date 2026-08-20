# mdqc payload examples

Reference payloads for the MD platform ingest team.

## Current set — generated from mdqc v0.5.11

The nine `payload_0*.json` files are produced by running the real
`Spool.enqueue` and `gold_standards` code paths, so their shape is exactly
what an agent emits — not hand-written. Values are synthetic and modelled on
the Evosep diagnostic panel at 200 SPD (eight targets, ~6.4 min gradient).
No customer data or credentials appear in any of them.

Regenerate with `scripts` — see the generator referenced in the release
commit; re-run it after any schema change so these do not drift.

| File | Covers |
|---|---|
| `payload_01_ssc0_baseline_source.json` | SSC₀ — one of the runs the baseline was built from |
| `payload_02_qcb_healthy.json` | QC B at full load → `peak_area_verdict: "ok"` |
| `payload_03_qcb_75pct_warn.json` | QC B at 75% → `"warn"`, `dilution_pct: 75` |
| `payload_04_qcb_50pct_fail.json` | QC B at 50% → `"fail"`, `dilution_pct: 50` |
| `payload_05_mis_extracted_target.json` | One target wrongly integrated → `target_extraction_suspect: true` on that peptide |
| `payload_06_custom_thresholds.json` | Same run as 03, tuned thresholds → `thresholds_source: "custom"` |
| `payload_07_qca.json` | QC A → verdict **withheld**, see below |
| `payload_08_no_baseline.json` | No baseline recorded → `baseline_context` and `comparison_metrics` both `null` |
| `payload_09_failed_extraction.json` | Failed extraction — still spooled, empty `target_metrics` |

### Four things worth knowing before building against these

**1. `comparison_metrics` and `baseline_context` are null until a baseline
exists.** An instrument is in that state from installation until an engineer
saves a gold standard on the agent's Gold Standards page. Payload 08 is that
state — treat it as normal, not as an error.

**2. `peak_area_verdict` is withheld for QC A and blanks.** The warn/fail
bands measure deviation from a ratio of 1.0, which only holds where the run
should match the SSC₀ reference — SSC₀ itself and QC B. QC A is ~1 µg lysate
against a 50 ng reference, roughly 6× on column, and the exact figure is still
to be established. Scoring it against QC B's bands marked every QC A run as
failing. When the verdict is withheld, `peak_area_verdict` is `null` and
`peak_area_verdict_note` says why — so a withheld verdict is distinguishable
from missing data. `median_peak_area_ratio` is still emitted either way.

**3. `thresholds_source` tells you whether an instrument has been tuned.**
Thresholds are editable in the agent's Settings page, because Evosep asked for
them to be adjustable in the field without a release. A `warn` from a tuned
instrument does not mean the same as a `warn` from a stock one, and the values
alone cannot tell you which you have without tracking our shipped defaults per
agent version. Worth surfacing anywhere instruments are compared side by side.

**4. Every raw input is emitted alongside every derived flag.** Nothing here
has to be taken on trust: `thresholds_applied` records the exact values that
produced a verdict, and the per-peptide deviations are all present, so the
platform can re-derive under its own policy or ignore the flags entirely.
Given the thresholds are provisional, that is the expected path.

### Threshold provenance

Defaults come from Evosep's first-draft decision matrix (Stoyan Stoychev,
2026-07-28), derived from a timsTOF HT dilution series. **They are
provisional** — the series is being repeated across further platforms, and
Evosep has since indicated that retention-time limits should move from % CV to
standard deviation in seconds. Do not treat them as an acceptance
specification.

---

## Earlier examples

### Files (pre-v0.5.0)

### [`example_payload_v1.1.json`](example_payload_v1.1.json)

**Real successful extraction**, 48 targets, lightly sanitized.

- `schema_version`: `1.1` (current as of mdqc v0.4.0)
- `agent_version`: `0.2.3` (predates the peptide-class system — so
  `target_metrics` rows do not carry `protein_name` / `peptide_class` /
  `peptide_class_purpose`; those fields will appear on v0.4.0+ payloads)
- `run.control_type`: `BLANK` (this was an `eb_inbetween` between-run blank)
- `run_metrics`: `targets_found: 48 / targets_expected: 48`, full recovery

This is what a 100%-recovery payload looks like in steady state. The
`extra_metrics` dict carries the split MS1 + Fragment area columns
(`Total Area MS1`, `Total Area Fragment`) and per-precursor metadata
(`Precursor Charge`) from the bundled Skyline report; the canonical
`peak_area` is the sum of the two.

### [`example_payload_v0.4_with_peptide_classes.json`](example_payload_v0.4_with_peptide_classes.json)

**Synthetic** — illustrates the v0.4.0+ shape with peptide-class
annotations. Five target_metrics rows showing both classes:

- 3 × `peptide_class_purpose: "recovery"` (the non-reactive QC targets)
- 2 × `peptide_class_purpose: "digest_efficiency"` (a miss-cleavage pair)

`run_metrics.targets_found / targets_expected` is `47 / 48` — note this
is computed **after** peptide-class filtering: the digest-efficiency pair
is excluded from both numerator and denominator (see
[`src/mdqc/peptide_classes.py`](../../src/mdqc/peptide_classes.py)
`filter_for_recovery`).

The digest-efficiency ratio for the latest run is computed as
`0miss_area / (0miss_area + 1miss_area)` where 0-miss is the shorter
peptide. For this example: `4_200_000 / (4_200_000 + 870_000) ≈ 0.829`
(82.9 % digestion completeness — a healthy number for tryptic digestion).

### [`example_payload_failed.json`](example_payload_failed.json)

**Real failure**, reconstructed from a pilot incident on 2026-05-07
where the `.raw` file vanished mid-extraction (likely Windows Defender
quarantine — see [`update.md`](../../update.md) §3 backstory).

- `extraction.status`: `FAILED`
- `extraction.error_message`: full uncapped Skyline stdout (see v0.2.3
  in the release table)
- `target_metrics`: `[]` (empty)
- `run_metrics`: `{}` (empty)

The platform should ingest failed payloads too — they're important for
the dashboard's recent-activity / failed-runs UI.

## Conventions Peppe should know

| | |
|---|---|
| All timestamps | ISO-8601 with explicit timezone (always UTC for ones mdqc generates; `acquisition_time` is the raw-file mtime — usually local time but with the local timezone offset embedded) |
| `null` in any field | Means "value was not available at extraction time", **not** "value is zero". The two are operationally distinct |
| Fields starting with `_` | Python-only debug aids — **not part of the cross-language contract**. The platform should ignore them on ingest |
| `extra_metrics` keys | Free-form strings copied verbatim from the Skyline CSV header. Operators add columns to their `.skyr`; we don't try to normalise. Preserve on ingest, render the dict as-is |
| Numeric values that look like integers | Always emitted as JSON numbers (often `.0`-suffixed when they happen to be whole). Don't downcast: a Skyline-reported `0` for an MS1-only metric is informative, distinct from `null` |
| `peak_area = MS1 + Fragment` rule | For DIA reports that split the columns. See `extractor/report.py` `peak_area` resolution chain |

## Validating ingest

Quick smoke test for a server-side ingestion endpoint:

```bash
# All three should be accepted
curl -X POST $API/v1/qc/payloads -H "Authorization: Bearer ..." \
     -H "Content-Type: application/json" \
     -d @example_payload_v1.1.json

curl -X POST $API/v1/qc/payloads -H "Authorization: Bearer ..." \
     -H "Content-Type: application/json" \
     -d @example_payload_v0.4_with_peptide_classes.json

curl -X POST $API/v1/qc/payloads -H "Authorization: Bearer ..." \
     -H "Content-Type: application/json" \
     -d @example_payload_failed.json
```

Suggested validation rules (server-side):

- `schema_version`: must be present, accept any `1.x`, reject `2.x` (forward-only)
- `payload_id` / `correlation_id` / `agent_id` / `agent_version`: must be present and non-empty
- `run.control_type`: must be one of `SSC0` / `QC_A` / `QC_B` / `BLANK` / `SAMPLE`
- `extraction.status`: must be one of `SUCCESS` / `FAILED` / `SKIPPED`
- When `status == SUCCESS`, `target_metrics` must be non-empty
- When `status == FAILED`, `error_message` must be present and `target_metrics` may be empty
- Duplicate `payload_id` should be a 200 idempotent (the agent retries on network failure)
