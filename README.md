# docforensics

An **air-gap-compatible, standard-library-first document text-triage tool.** It
extracts configurable textual indicators, basic entities, terms, document
structure, and selected DOCX revision text for exploratory analysis of `.docx`,
`.txt`, `.md`, `.py`, and `.pdf` files, and writes a flat CSV/JSON summary.

**Results are heuristic and intended for first-pass triage and manual review.
docforensics is _not_, in its current form, a chain-of-custody, evidentiary-
authentication, or full-fidelity e-discovery engine.** See
[Scope & limitations](#scope--limitations) before relying on any output.

---

## What it does

Ten extraction passes over each document:

| # | Method | Notes |
|---|---|---|
| 1 | Python AST analysis | string literals, imports, function/class names — parsed, never executed |
| 2 | Regex extraction | emails, URLs, dates, money, phones, citations, percentages — **values are approximate** (see limitations) |
| 3 | Rule-based NER | persons, orgs, statutes, SEC filings, case names — **recall/precision are limited** |
| 4 | TF-IDF | cross-document term-importance scoring (not a financial-figure comparison) |
| 5 | N-grams | unigram / bigram / trigram frequency counts |
| 6 | Co-occurrence graph | weighted edges over filtered keywords (general terms, not named entities); deterministic output |
| 7 | Document structure | line-based headings / paragraphs / lists / tables (plain text & Markdown) |
| 8 | Readability | Flesch Reading Ease, Gunning Fog (approximate syllables) |
| 9 | SHA-256 fingerprinting | `source_sha256` (integrity) + `normalized_text_sha256` (dedup) — see [Hashing](#hashing) |
| 10 | Domain flag patterns | pluggable regex groups; each becomes a summary + CSV column |

Every extraction carries a **typed outcome** — `OK`, `PARTIAL`, `NO_TEXT`,
`FAILED`, `MISSING_DEPENDENCY`, or `UNSUPPORTED` — so a parse failure is never
silently analysed as document text.

## Install

No install step for the core. Python **3.10+**, standard library only.

```sh
git clone https://github.com/IndivisaLLC/Document-forensics
cd Document-forensics
```

PDF support is **optional** and the one external dependency:

```sh
pip install pypdf      # required only for .pdf; all other formats are stdlib-only
```

Without `pypdf`, PDFs return `outcome=MISSING_DEPENDENCY` (they are not analysed
as an error string).

## Usage

```python
from docforensics import ingest_and_extract, batch_process, extract

# Single file (reads raw bytes, computes source_sha256, typed outcome)
result = ingest_and_extract("contract.docx")
print(result.to_dict()["outcome"])        # OK / PARTIAL / NO_TEXT / FAILED / ...

# Batch a folder → CSV (deterministic order, one read per file)
batch_process("/path/to/documents", "report.csv")

# Raw in-memory text (no source file → source_sha256 is None)
result = extract("The quick brown fox...")
```

Run the built-in demo:

```sh
python docforensics.py          # prints a demo extraction, writes demo_output.json
```

## Hashing

Two **distinct** hashes, with different meanings — do not conflate them:

- **`source_sha256` (integrity).** SHA-256 of the original file bytes, computed
  before any decoding or normalization. This is the value to store for
  integrity: it proves the bytes are unchanged between hashing and verification.
  Present only when raw file bytes are available (e.g. `ingest_and_extract`);
  `None` for raw in-memory text.
- **`normalized_text_sha256` (deduplication).** SHA-256 of whitespace-collapsed,
  lowercased extracted text. A **near-duplicate key, not tamper-evidence** —
  different source bytes (case, whitespace, metadata, undecodable bytes,
  different no-text PDFs) can collapse to the same value.

> An integrity hash is **necessary but not sufficient** for chain of custody,
> which additionally requires a documented handling log, trusted timestamps, and
> access control. docforensics provides none of those.

## Batch CSV columns

One row per file. Columns include the typed `outcome` and `detail`, both hashes
(`source_sha256`, `normalized_text_sha256`), readability, structure counts,
regex/entity **counts** (leads, not verified values), per-`DOMAIN_PATTERNS`
flag counts, and `deleted_runs`.

`deleted_runs` counts genuine `w:delText` nodes in a DOCX (not a text marker),
so it **cannot be forged** by ordinary document text. String cells are
neutralised against spreadsheet formula injection. Files are read exactly once.

## Extending

Add an `re.compile()` entry to `DOMAIN_PATTERNS` and it becomes a summary and
CSV column automatically. (Entries added to `PATTERNS` are matched and appear in
the per-document regex output but do **not** create dedicated CSV columns — only
`DOMAIN_PATTERNS` keys do.)

## Resource limits

Hostile-input bounds are enforced: container file size (`MAX_FILE_SIZE`, 100 MB),
decompressed DOCX `document.xml` (`MAX_DECOMPRESSED`, 50 MB — a zip-bomb guard),
and analysed-text length (`MAX_TEXT_SIZE`, 25 MB). These bound **memory and
archive expansion.** They do **not** neutralise the quadratic CPU cost of some
built-in regex patterns on adversarial text — do not batch untrusted documents
without an external per-process timeout.

## Scope & limitations

**Verified / supported**
- Air-gap compatible: no network, socket, subprocess, telemetry, or cloud calls.
- Standard-library core; `pypdf` optional for PDF only.
- `source_sha256` integrity hashing of original bytes.
- Typed extraction outcomes; parse failures are never analysed as text.
- Deterministic output (stable across `PYTHONHASHSEED` and filesystem ordering).
- Structured, un-forgeable DOCX `w:delText` capture.

**Known-limited (treat counts as leads, not facts)**
- Currency/statute/date/entity patterns mis-parse some values (e.g.
  `$2.4 billion`, ISO dates, several statute formats, non-ASCII names).
- DOCX extraction reads only `w:t` / `w:delText` in `word/document.xml`
  (Transitional WordprocessingML). Headers, footers, comments, foot/endnotes,
  embedded objects, and Strict-namespace parts are **not** inspected; run-
  boundary spacing is approximate.
- Matches carry **no** byte offsets, page coordinates, or paragraph/run IDs.

**Not implemented**
- Cross-document financial-figure comparison (added/removed/changed amounts).
- Chain-of-custody / evidentiary authentication.
- OCR for image-only / scanned PDFs (these return `NO_TEXT`).

Correcting the extraction patterns, adding byte-span provenance, and full DOCX
fidelity are planned but **not yet present**.

## Tests

`tests/` covers every `parse_pdf` branch against real fixtures: text (single and
multi-page) → `OK`, image-only/scanned and blank → `NO_TEXT`, encrypted and
malformed → `FAILED`, and pypdf-absent → `MISSING_DEPENDENCY`. The suite also
asserts a failed parse is never analysed as text yet still carries
`source_sha256`.

```sh
python tests/test_pdf.py          # standalone, needs only pypdf
# or, under pytest:  pip install pytest && pytest tests/
```

Fixtures are committed under `tests/fixtures/`; regenerate them with
`python tests/make_pdf_fixtures.py` (dev deps: `reportlab pillow img2pdf pypdf`).

## License

MIT — see the `LICENSE` file and the module header.
