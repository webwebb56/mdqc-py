# MD QC SOP — research findings and proposed structure

**Author:** Andrew Webb
**Date:** 2026-07-30
**Status:** Plan for review. No SOP content written yet.
**Deliverable:** an HTML SOP, hosted by Mass Dynamics, covering mdqc (the local
agent) and the MD Platform, written so Evosep can lift it into the Lupo IFU.

---

## 1. The brief, as actually stated

Two constraints come directly from the customer rather than from us:

**Stoyan, 2026-07-23:**

> "We will likely merge the MDQC SOP with rest of the Lupo IFU. You can write
> the SOP without a specific template and we will do the transfer to the final
> documents. Perhaps indicate logos etc you would like included. Next 2 weeks
> is fine, we plan to send Lupo for beta tests in week 32 or 33."

**Paula, 2026-07-23:**

> "We'll send through a proposed setup and SOPs for you to review, and we can
> use your first Copenhagen lab installation to test them."

Three things follow:

1. **No Evosep template to fill in.** We write freely; Evosep re-flows it.
2. **The content must be liftable.** Evosep transfers sections into the IFU by
   hand, so structure must be clean and self-contained per section — no
   cross-references that break when a section is moved, no meaning carried by
   layout alone.
3. **Logo placement is our call to propose.** Explicitly requested.

Timeline: Lupo beta week 32/33, so the draft is wanted inside ~2 weeks of the
23rd. Already tracked as PLAN_2026-07-24 §7 item 11.

---

## 2. What Evosep's own documentation looks like

Read from [evosep.com/support/documentation](https://www.evosep.com/support/documentation/)
(Eno and One product pages). Their published set is more differentiated than a
single "manual":

| Type | ID pattern | What it contains |
|---|---|---|
| User Manual | `UM-001A`, `UM-002`, `UM-017`, `UM-019`, `UM-021` | Setup, use, maintenance recommendations, support/service/warranty policy. One per product **and per variant** (a separate Pod manual per MS vendor) |
| Advanced User Guide | `UM-003B` | Companion to the UM. Advanced functions, connections to specific CDS/ion sources, troubleshooting |
| Troubleshooting Guide | companion, cites parent UM | "Lists potential **failure modes** with their possible **causes** and **resolutions**" |
| Quick Start Guide | per software | Chromeleon, Chronos, HyStar, SCIEX OS, MassHunter — one each |
| Software Installation Guide | `UM-004A` | Driver install, per CDS |
| Protocol | e.g. Evotip sample loading | Step-by-step, required materials, best-practice notes |
| Application Note | e.g. per-SPD standard methods | Expected pressures, gradients, reproducibility figures |

Observations that should shape our document:

- **Document IDs carry a revision letter** (`UM-001A`, `UM-003B`). Revision is
  part of the identity, not a footnote.
- **Software gets its own document class.** mdqc is software, so in Evosep's
  taxonomy it maps to *Software Installation Guide + Quick Start Guide*, not to
  a hardware User Manual. Worth mirroring — it sets reader expectations.
- **Troubleshooting is a separate document with a fixed shape**:
  failure mode → possible cause → resolution. Our existing troubleshooting
  table in `EVOSEP_PROTOTYPE_SETUP.md` §8 is already in that shape.
- **Vendor variants are split, not merged.** Evosep ships four Pod manuals
  rather than one with four branches. mdqc supports five vendors; we should
  decide deliberately whether vendor specifics are inline or annexed
  (recommendation: inline, since the differences are small — file extension and
  stability window — but flagged in a single table).

## 3. What we already have to work from

| Source | Reusable for | Gap |
|---|---|---|
| `docs/EVOSEP_PROTOTYPE_SETUP.md` (343 lines) | ~70% of Part A. Prereqs, install, config, verification, troubleshooting table, quick reference | Written for Stoyan as a *prototype* doc. Too much internal reasoning, assumes Python fluency, no baseline workflow, no platform half |
| `README.md` | Positioning, architecture summary | Developer-facing |
| `installer/README.md` | Service registration, NSSM | Build-time, not operator-facing |
| `update.md` §6 | Payload schema for the data annex | Platform-team audience |
| `docs/PLAN_2026-07-24.md` §5 | The conceptual model (local optimum, ratios) | Internal planning voice |
| Stoyan's decision matrix (2026-07-28) | The interpretation section — the core of Part B | Provisional, one instrument |

**Screenshots are the real gap.** The repo contains exactly one image
(`docs/images/dashboard.png`). A usable SOP needs roughly 15–25: wizard steps,
Settings panels, the Gold Standards page, the platform module setup, the
traffic-light and longitudinal views. See §7.

---

## 4. The structural decision that matters most

**Two audiences, not one.** This comes straight from the workflow Stoyan
described on the 23rd — the field engineer commissions the instrument and
records the gold standard; the lab user lives with it afterwards and
re-baselines after a column change.

Writing one undifferentiated document would force the engineer to wade through
daily-operation material and the lab user to wade through IT prerequisites.
Evosep already solves this by splitting Installation Guide from Quick Start
Guide, so the split is also familiar to them.

I propose **one HTML document with three clearly separated parts**, each
independently liftable into the IFU:

- **Part A — Installation and commissioning.** Performed once, by a field
  service engineer. Ends in a signed acceptance checklist and a recorded
  baseline.
- **Part B — Routine operation.** Ongoing, by lab staff. What runs by itself,
  what to look at, what a red light means, when to re-baseline.
- **Part C — Maintenance, troubleshooting and reference.**

One file rather than three because it is hosted (single URL, single search) and
because Evosep is going to disassemble it anyway. Parts are anchored so support
can point at `#c1-failure-modes`.

---

## 5. Proposed structure

### Front matter

- Document control block — title, document ID (slot for Evosep's `UM-xxx`
  scheme), revision letter, date, owner, applies-to versions (mdqc ≥ 0.5.8)
- **Logo slots** — co-branded header, MD left / Evosep right, plus a note on
  where Evosep's IFU logo conventions should override. *(Explicitly requested.)*
- Purpose and scope — what this covers and what it does not
- Audience and how to use this document (the A/B/C signpost)
- Definitions: SSC₀, QC A, QC B, EB/PB, SPD, Evotip, Lupo, gold standard /
  local optimum, baseline, idotp, dotp, digest efficiency, payload
- Prerequisites at a glance — one checklist page an engineer can scan before
  travelling to site

### Part A — Installation and commissioning (field engineer)

| § | Content | Notes |
|---|---|---|
| A1 | Site and IT prerequisites | Windows version, disk, Skyline install, **outbound HTTPS egress**, service-account permissions, whether the PC is allowed on the network at all |
| A2 | Install the agent | Release ZIP path and pip path; verify with `mdqc --version` |
| A3 | First-run wizard — instrument, vendor, watch folder, file pattern | Screenshot per step |
| A4 | Skyline method and report | Where `QC_Method.sky` and the `.skyr` live; how to point at a custom report; the embedded-replicates footgun |
| A5 | **Filename convention** | See §6 — this is a hard requirement, not a nicety |
| A6 | Peptide classes and digest efficiency | Mapping the Protein column; why the miss-cleavage pair is excluded from recovery |
| A7 | Single-file verification | Drop one raw file, confirm targets found, confirm a payload appears. Pass/fail gate before proceeding |
| A8 | Connect to Mass Dynamics | Account, API token, Development vs Production, restart, confirm a 201. Include the **retention setting** step — raise before the token goes in |
| A9 | Record the gold standard | Run 15–20 SSC₀ at the QC SPD; Gold Standards page; selecting representative runs; reading CV%; saving and activating |
| A10 | Commissioning acceptance checklist | Printable, signed, left with the customer |

### Part B — Routine operation (lab user)

| § | Content |
|---|---|
| B1 | What happens automatically — the loop, in one diagram |
| B2 | The agent web UI — Dashboard, Failed, Logs; what "healthy" looks like |
| B3 | The MD Platform — signing in, workspace, **adding your instrument to a module's filter** (Stoyan hit this twice; it is the single most common "my data isn't showing" cause) |
| B4 | Reading the traffic light and longitudinal views |
| B5 | **Interpreting a result** — the decision matrix: RT deviation, dot products, peak area vs SSC₀; distinguishing a mis-extracted target from a genuine LC-MS drop |
| B6 | Escalation table — symptom → likely domain (LC-MS / sample prep / extraction) → action → who to contact |
| B7 | Re-baselining after a column change, clean, or service |
| B8 | Routine checks — daily glance, weekly review, monthly |

### Part C — Maintenance, troubleshooting, reference

| § | Content |
|---|---|
| C1 | Failure modes — Evosep's symptom / cause / resolution three-column format |
| C2 | File locations and what each folder is for |
| C3 | Collecting a support bundle — logs, failed payloads, config; what to send and to whom |
| C4 | Upgrading the agent |
| C5 | Data handling and IT review — what leaves the instrument PC (derived numbers, never raw files), where it goes, how auth works, offline behaviour |
| C6 | Uninstall / decommission |

### Annexes

- Annex 1 — `config.toml` reference, every key, default, and effect
- Annex 2 — Filename convention specification with worked examples
- Annex 3 — Payload field reference (condensed from `update.md` §6)
- Annex 4 — Default QC thresholds and their provenance, marked provisional
- Annex 5 — Commissioning checklist (printable)
- Annex 6 — Revision history

---

## 6. Two things I want to raise now, not later

**The filename convention is an unstated hard dependency.** mdqc derives control
type, SPD, dilution and well position from the filename. Stoyan's files happen
to follow a convention that parses cleanly — all 88 of his timsTOF HT files
classify at HIGH confidence. A customer who names files differently gets silent
misclassification: runs typed as `SAMPLE` are skipped entirely, and a missing
SPD means no baseline can be matched. Today this is documented nowhere
customer-facing. The SOP is the right place to make it a stated requirement,
with a validation step in A5 and the full specification in Annex 2.

**Thresholds must be presented as provisional.** Stoyan's decision matrix is a
first draft from one instrument, and I have already found one boundary case
where the stated 10% warn threshold misses the condition it was chosen to catch.
The SOP should state the defaults, say where they came from, say they are
configurable, and avoid any language that reads as a specification the
instrument is expected to meet. Getting this wrong would bake a provisional
number into a document that ships to customers inside a regulated-adjacent IFU.

---

## 7. Practical constraints on the HTML

- **Single self-contained file.** Instrument PCs are frequently offline or on a
  restricted VLAN. No CDN, no external fonts, no analytics. Everything inlined,
  images as data URIs or a sibling folder shipped alongside.
- **Must print cleanly to PDF.** Evosep transfers content into the IFU, and
  engineers print the commissioning checklist. Needs a print stylesheet: page
  breaks between parts, expanded links, no fixed navigation in print.
- **Stable deep anchors** so support can cite `#c1-failure-modes`.
- **Version-stamped**, with an explicit "applies to mdqc ≥ 0.5.8" and a
  revision-history annex, because the UI it describes changes per release.
- **Screenshots are the long pole.** Roughly 15–25 needed, ideally captured on a
  real instrument rather than my synthetic test environment, and re-captured
  when the UI moves.

---

## 8. Open questions

**Resolved 2026-07-30 (Andrew):**

1. ~~**Scope of the platform half.**~~ **One document.** The MD Platform
   sections stay in, alongside the agent sections. Implemented as Part B §B3–B4.
2. ~~**Screenshots.**~~ **Placeholders for now**, each carrying a capture brief.
   Shot list is Annex 6 of the SOP, marked for removal before issue.
3. ~~**Branding.**~~ **Co-branded.** MD mark left, Evosep wordmark right,
   embedded as data URIs so the file stays self-contained.

**Still open:**

4. **Document ID.** Adopt Evosep's `UM-xxx` scheme so it slots into their
   register, or use an MD scheme and let them re-number on transfer? The
   document-control block currently carries an "Evosep to assign" placeholder.
5. **Review path.** Does this go to Stoyan and Paula together, and does Evosep
   QA review before it enters the IFU?
6. **Sign-off record.** Does the commissioning checklist (Annex 5) need to be a
   controlled record Evosep retains, or is it a convenience for the engineer?
7. **Tone.** Currently pitched formally, to sit alongside Evosep's own manuals.
   More starched than MD's usual voice — confirm that is the right register.

---

## 9. Suggested sequence

1. Agree §5 structure and the §8 questions (this document)
2. Draft Parts A and C first — mostly re-leveling existing content, and they are
   what the Copenhagen installation actually needs
3. Capture screenshots against v0.5.8 during or before that install
4. Draft Part B, which depends on the decision matrix settling across platforms
5. Build the HTML with print stylesheet, review as PDF
6. Send to Stoyan and Paula; use the Copenhagen install as the field test, per
   Paula's own suggestion
