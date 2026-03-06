# Document-forensics
Unified Text Extraction Engine (Air-Gapped E-Discovery) A zero-dependency, portable forensic text extraction pipeline designed for stealthy, local e-discovery and investigative data analysis.
# docforensics

A zero-dependency document forensics and text extraction engine for Python.

Combines ten extraction methods into a single pipeline and outputs a structured
JSON result or a batch CSV report across an entire folder of documents.

```
$ python docforensics.py
──────────────────────────────────────────────────────────────────────
  DOMAIN FLAGS
  ⚑ currency_exposure     1 hit(s)  ['£180 m']
  ⚑ facility_distress     1 hit(s)  ['facility default']
```

-----

## Why this exists

Most document analysis tools require heavyweight NLP frameworks (spaCy, NLTK,
transformers) just to answer simple questions like:

- Which of these 400 contracts mentions a facility default?
- Does this public filing contain the same financial figures as the internal draft?
- Were any paragraphs deleted from this Word document before it was filed?
- Which documents share the same SHA-256 hash as the version I have?

`docforensics` answers all of these using nothing but the Python standard library
(plus one optional dependency for PDFs).

-----

## Features

|Method                       |What it does                                                                                                                       |
|-----------------------------|-----------------------------------------------------------------------------------------------------------------------------------|
|**Python AST analysis**      |Parses `.py` files into a syntax tree — extracts string literals, imports, function and class names, node type counts              |
|**Regex extraction**         |Emails, URLs, dates, USD/GBP/EUR amounts, phone numbers, citations, section headings, code blocks, percentages, stock tickers      |
|**Rule-based NER**           |Persons (title-prefixed), organisations (Inc/LLC/Corp etc.), statutes (CFR/USC), SEC filing types (10-K/10-Q/8-K), case citations  |
|**TF-IDF**                   |Pure-Python cross-document term weighting. Pass an internal draft alongside a public filing to surface term divergence between them|
|**N-gram extraction**        |Unigrams, bigrams, trigrams with frequency counts                                                                                  |
|**Co-occurrence graph**      |Sliding-window keyword relationship mapping — returns a weighted adjacency list ready for graph visualisation                      |
|**Document structure**       |Headings (with depth), paragraphs, bullet/numbered lists, pipe-delimited tables                                                    |
|**Readability metrics**      |Flesch Reading Ease and Gunning Fog Index                                                                                          |
|**SHA-256 fingerprinting**   |Tamper-evident identity for every document processed                                                                               |
|**Pluggable domain patterns**|Add your own `re.compile()` entries to `DOMAIN_PATTERNS` — they auto-appear as columns in the batch CSV                            |

-----

## Installation

```bash
# Core engine — no dependencies
git clone https://github.com/your-username/docforensics
cd docforensics

# Optional: PDF support
pip install pypdf
```

Requires Python 3.10+.

-----

## Usage

### Single file

```python
from docforensics import ingest_and_extract

result = ingest_and_extract("contract.docx")
print(result.to_dict())
```

Supports: `.docx` `.pdf` `.txt` `.md` `.py`

### Raw text

```python
from docforensics import extract

result = extract("The quick brown fox...")
result.to_dict()  # JSON-serialisable dict
```

### Batch folder → CSV

```python
from docforensics import batch_process

batch_process("/path/to/documents", "report.csv")
```

Walks the folder recursively, processes every supported file, and writes one
row per document to `report.csv`.

### Cross-document TF-IDF

Pass an internal draft alongside the public version to see which terms appear
in one but not the other:

```python
from docforensics import ingest_and_extract

internal = open("draft_v3.txt").read()
public   = open("filed_version.txt").read()

# Run extraction on the internal doc, with the public filing as context
result = ingest_and_extract("draft_v3.txt", extra_documents=[public])

# doc_0 = internal draft terms, doc_1 = public filing terms
print(result.tfidf["doc_0"][:10])
print(result.tfidf["doc_1"][:10])
```

-----

## DOCX track-changes (deleted text)

A `.docx` file is a ZIP archive containing XML. Word’s Track Changes feature
stores deleted text in `<w:delText>` nodes — content that is invisible in the
rendered document but still present in the raw file.

`docforensics` reads the XML directly and flags deleted passages:

```python
from docforensics import docx_to_text

text = docx_to_text("agreement.docx", include_deleted=True)
# Deleted passages appear as: [DELETED_TEXT: the original removed content]
```

The `deleted_text_blocks` column in the batch CSV counts these occurrences
per file, making it easy to triage which documents had content removed.

-----

## Adding domain-specific patterns

Edit the `DOMAIN_PATTERNS` dict in `docforensics.py`. Every key you add
automatically becomes a column in the batch CSV output.

```python
DOMAIN_PATTERNS: dict[str, re.Pattern] = {

    # Detect specific entity names near risk keywords
    "entity_risk": re.compile(
        r"(?i)(?:Your Entity Name).{0,50}"
        r"(?:default|breach|fraud|shortfall)"
    ),

    # Detect valuation deferral language
    "valuation_suppression": re.compile(
        r"(?i)(?:NAV|Net\s*Asset\s*Value).{0,40}"
        r"(?:avoid|delay|defer).{0,30}"
        r"(?:write[- ]?down|impairment)"
    ),

    # Add as many as you need...
}
```

-----

## Batch CSV columns

|Column                                 |Source                                               |
|---------------------------------------|-----------------------------------------------------|
|`file`                                 |Full file path                                       |
|`extension`                            |File type                                            |
|`sha256`                               |SHA-256 of normalised text — use for chain-of-custody|
|`word_count`                           |Total words                                          |
|`flesch_score`                         |Flesch Reading Ease (higher = more readable)         |
|`fog_index`                            |Gunning Fog Index (lower = more readable)            |
|`headings`                             |Number of document headings                          |
|`paragraphs`                           |Number of paragraphs                                 |
|`emails`                               |Email addresses found                                |
|`urls`                                 |URLs found                                           |
|`dates`                                |Date references found                                |
|`money_usd` / `money_gbp` / `money_eur`|Currency amounts by denomination                     |
|`orgs`                                 |Organisation names (rule-based)                      |
|`persons`                              |Persons with formal titles                           |
|`statutes`                             |Statutory references (CFR, USC, §)                   |
|`filings`                              |SEC filing type references                           |
|`cases`                                |Case citation patterns (X v. Y)                      |
|`top_unigrams`                         |Top 5 single terms (pipe-separated)                  |
|`top_bigrams`                          |Top 5 two-word phrases                               |
|`deleted_text_blocks`                  |Count of `<w:delText>` nodes in DOCX files           |
|*(domain keys)*                        |One column per entry in `DOMAIN_PATTERNS`            |
|`error`                                |Non-empty if processing failed                       |

-----

## Use cases

- **Legal discovery** — batch-triage hundreds of contracts or filings for
  specific clauses, counterparty names, or risk terms before manual review
- **Compliance auditing** — compare internal drafts against final filed
  versions to identify what was added, removed, or changed
- **Investigative research** — surface deleted text, cross-reference financial
  figures between documents, build a co-occurrence graph of named entities
- **Code archaeology** — run AST analysis across a codebase to extract all
  string literals, find hardcoded values, or map import dependencies

-----

## Requirements

- Python 3.10+
- Standard library only for `.docx`, `.txt`, `.md`, `.py`
- `pip install pypdf` for `.pdf` support

-----

## License

MIT
