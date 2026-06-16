# mdqc payload examples

Reference payloads for the MD platform ingest team. Generated from real
pilot data, sanitized (usernames + watch-folder paths replaced with
placeholders, `agent_id` replaced with a demo string).

The canonical schema is documented in [../../update.md §6](../../update.md).
Source of truth is [`src/mdqc/types.py`](../../src/mdqc/types.py) (the
`QcPayload` / `TargetMetric` / `RunMetrics` dataclasses) and the payload
construction in [`src/mdqc/spool/store.py`](../../src/mdqc/spool/store.py)
(method `Spool.enqueue`, lines ~137-210).

## Files

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
