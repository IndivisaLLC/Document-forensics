"""
docforensics.py
===============
An air-gap-compatible, standard-library-first document text-triage tool.

Combines ten extraction methods into a single pipeline:
  1.  Python AST analysis       — string literals, imports, function/class names
  2.  Regex pattern matching     — emails, URLs, dates, money, phones, citations
  3.  Rule-based NER             — persons, orgs, statutes, SEC filings, case names
  4.  TF-IDF term weighting      — cross-document term importance scoring
  5.  N-gram extraction          — unigrams, bigrams, trigrams with frequencies
  6.  Co-occurrence graph        — sliding-window keyword relationship mapping
  7.  Document structure         — headings, paragraphs, lists, tables
  8.  Readability metrics        — Flesch Reading Ease, Gunning Fog Index
  9.  SHA-256 fingerprinting     — source-byte hash (integrity) + normalized-text
                                   hash (deduplication); see fingerprint() for the
                                   distinction and its limits
  10. Specialised flag patterns  — pluggable domain-specific regex groups

Supported input formats: .docx, .txt, .md, .py (stdlib only); .pdf (needs pypdf)
Output: structured dict (JSON-serialisable) + optional batch CSV report

Scope / honesty note
--------------------
Results are HEURISTIC and intended for first-pass triage and manual review.
This tool is NOT, in its current form, a chain-of-custody, evidentiary-
authentication, or full-fidelity e-discovery engine. Specifically:
  - Pattern extraction (currency, statutes, dates, entities) is approximate and
    known to mis-parse some values; treat counts as leads, not facts.
  - DOCX extraction reads only w:t / w:delText runs in word/document.xml
    (Transitional WordprocessingML). Headers, footers, comments, foot/endnotes,
    embedded objects, and Strict-namespace parts are NOT inspected.
  - Matches carry no byte offsets, page coordinates, or paragraph/run IDs.
Every extraction now carries a typed `outcome` (OK / PARTIAL / NO_TEXT /
FAILED / MISSING_DEPENDENCY / UNSUPPORTED) so a parse failure can never be
silently analysed as document text.

Usage
-----
    from docforensics import ingest_and_extract
    result = ingest_and_extract("contract.docx")
    print(result.to_dict())

    from docforensics import batch_process
    batch_process("/path/to/documents", "report.csv")

    from docforensics import extract
    result = extract("The quick brown fox...")

Extending patterns
------------------
Add an re.compile() entry to DOMAIN_PATTERNS and it will appear as a summary
and batch-CSV column automatically. (Entries added to PATTERNS are matched and
appear in the per-document regex output, but do NOT create dedicated CSV
columns — only DOMAIN_PATTERNS keys do.)

License: MIT
"""

import os
import re
import io
import ast
import csv
import json
import math
import hashlib
import textwrap
import zipfile
import xml.etree.ElementTree as ET
from enum import Enum
from pathlib import Path
from dataclasses import dataclass, field
from collections import Counter
from typing import Any


# ── Resource limits (hostile-input bounds) ────────────────────────────────────
# These bound MEMORY and archive expansion. They do NOT neutralise the quadratic
# CPU cost of some built-in regex patterns on adversarial text (e.g. the ORG
# pattern) — that is a pattern-level fix, deferred. Do not batch-process
# untrusted documents without external per-process timeouts.

MAX_FILE_SIZE    = 100 * 1024 * 1024   # 100 MB — container/file on disk
MAX_DECOMPRESSED =  50 * 1024 * 1024   # 50 MB — decompressed docx document.xml (zip-bomb guard)
MAX_TEXT_SIZE    =  25 * 1024 * 1024   # 25 MB — analysed-text cap (memory bound)

SUPPORTED_EXTS = {".docx", ".pdf", ".txt", ".md", ".py"}


# ── Typed extraction outcomes ─────────────────────────────────────────────────

class Outcome(str, Enum):
    """Typed result of a parse/extraction step. str-valued for JSON output."""
    OK                 = "OK"                  # text extracted normally
    PARTIAL            = "PARTIAL"             # text extracted, but something was lossy
    NO_TEXT            = "NO_TEXT"             # parsed fine, no extractable text (image-only, empty, unknown namespace)
    FAILED             = "FAILED"             # could not parse the file
    MISSING_DEPENDENCY = "MISSING_DEPENDENCY" # required optional dependency not installed
    UNSUPPORTED        = "UNSUPPORTED"        # file type / structure not supported


@dataclass
class ParseResult:
    """Result of a parser. Carries typed outcome and structured side-channels."""
    text:      str = ""
    outcome:   Outcome = Outcome.OK
    detail:    str = ""
    deletions: list[str] = field(default_factory=list)   # structured w:delText runs (DOCX only)
    coverage:  dict = field(default_factory=dict)         # what was / was not inspected


# ── 1. Python AST analysis ────────────────────────────────────────────────────

class ASTTextVisitor(ast.NodeVisitor):
    """Walk a Python AST and pull every string literal, import, function, and class."""

    def __init__(self):
        self.strings:   list[dict] = []
        self.imports:   list[str]  = []
        self.functions: list[str]  = []
        self.classes:   list[str]  = []

    def visit_Constant(self, node):
        if isinstance(node.value, str) and node.value.strip():
            self.strings.append({
                "value": node.value.strip(),
                "line":  node.lineno,
                "col":   node.col_offset,
            })
        self.generic_visit(node)

    def visit_Import(self, node):
        for alias in node.names:
            self.imports.append(alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        module = node.module or ""
        for alias in node.names:
            self.imports.append(f"{module}.{alias.name}")
        self.generic_visit(node)

    def visit_FunctionDef(self, node):
        self.functions.append(node.name)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        self.classes.append(node.name)
        self.generic_visit(node)


def extract_ast_text(source: str) -> dict:
    """Parse Python source and return structured AST text data."""
    try:
        tree = ast.parse(source)
        visitor = ASTTextVisitor()
        visitor.visit(tree)
        return {
            "strings":    visitor.strings,
            "imports":    visitor.imports,
            "functions":  visitor.functions,
            "classes":    visitor.classes,
            "node_types": Counter(type(n).__name__ for n in ast.walk(tree)),
        }
    except SyntaxError as e:
        return {"error": str(e)}


# ── 2. Regex patterns ─────────────────────────────────────────────────────────
# NOTE: these patterns are UNCHANGED in this revision. Several are known to
# mis-parse values (e.g. "$2.4 billion" → "$2","4 billion"; statutes missed;
# ISO dates missed). Correcting them is a separate, deferred workstream; they
# are left intact here so extraction output is unchanged while the forensic
# blockers (hashing, outcomes, determinism, limits) are addressed.

PATTERNS: dict[str, re.Pattern] = {
    # Standard document extractions
    "emails":      re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+"),
    "urls":        re.compile(r"https?://[^\s\"'>]+"),
    "dates":       re.compile(
        r"\b(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|"
        r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4})\b",
        re.IGNORECASE,
    ),
    "money_usd":   re.compile(r"\$[\d,]+(?:\.\d{2})?|\b\d+(?:\.\d+)?\s*(?:million|billion|USD)\b", re.I),
    "money_gbp":   re.compile(r"£[\d,]+(?:\.\d{2})?|\b\d+(?:\.\d+)?\s*(?:million|billion)\s*(?:GBP|pounds)\b", re.I),
    "money_eur":   re.compile(r"€[\d,]+(?:\.\d{2})?|\b\d+(?:\.\d+)?\s*(?:million|billion)\s*(?:EUR|euros)\b", re.I),
    "phone":       re.compile(r"\b(?:\+?1[-.\s]?)?(?:\(\d{3}\)|\d{3})[-.\s]?\d{3}[-.\s]?\d{4}\b"),
    "citations":   re.compile(r"\[\d+\]|\(\w[\w\s]+,\s*\d{4}\)"),
    "sections":    re.compile(r"^(?:#{1,6}|\d+\.|[A-Z][A-Z\s]{2,}:)\s+.+", re.MULTILINE),
    "code_blocks": re.compile(r"```[\s\S]*?```|`[^`]+`"),
    "percentages": re.compile(r"\b\d+(?:\.\d+)?%"),
    "ticker":      re.compile(r"\b[A-Z]{1,5}(?::[A-Z]{1,5})?\b(?=\s+(?:stock|shares|NYSE|NASDAQ))"),
}

# ── Domain-specific flag patterns (pluggable) ─────────────────────────────────
# Each key becomes a summary + batch-CSV column. These are UNCHANGED in this
# revision (see note above). `ENTITY_NAME` is a literal placeholder: until you
# replace it with a real entity, this pattern matches the literal text
# "ENTITY_NAME" (i.e. effectively nothing in real documents) — it is not a
# wildcard. Customise before use.

DOMAIN_PATTERNS: dict[str, re.Pattern] = {

    # ── Legal / regulatory ────────────────────────────────────────────────────
    "named_individuals": re.compile(
        r"(?i)\b(?:Mr\.|Ms\.|Dr\.|CEO|CFO|CLO|CRO)\s+[A-Z][a-z]+"
    ),

    # ── Financial exposure ────────────────────────────────────────────────────
    "currency_exposure": re.compile(
        r"(?i)(?:£|\$|€|GBP|USD|EUR)\s*\d+\s*(?:m|million|bn|billion)"
        r"|\b\d+\s*(?:m|million|bn|billion)\s*(?:GBP|USD|EUR|pounds|dollars|euros)\b"
    ),

    # Entity name appearing near irregularity keywords within 50 characters.
    # Replace ENTITY_NAME with the organisation you are analysing (literal match).
    "entity_irregularities": re.compile(
        r"(?i)(?:ENTITY_NAME).{0,50}"
        r"(?:fraud|misrepresent|irregularit|shortfall|breach|default|red\s+flag|toxic)"
    ),

    # ── Valuation / accounting ────────────────────────────────────────────────
    "valuation_suppression": re.compile(
        r"(?i)(?:NAV|Net\s*Asset\s*Value|Q[1-4]|quarter).{0,40}"
        r"(?:avoid|delay|defer|hold|push\s+out).{0,30}"
        r"(?:write[- ]?down|mark[- ]?down|impairment|haircut|provision)"
    ),

    # ── Structured finance / asset-backed ────────────────────────────────────
    "facility_distress": re.compile(
        r"(?i)(?:warehouse|facility|credit\s+line|ABF|SPV|trust).{0,30}"
        r"(?:default|frozen|suspend|administration|breach|accelerat|recall)"
    ),

    # ── Concealment indicators ────────────────────────────────────────────────
    "concealment_language": re.compile(
        r"(?i)(?:keep\s+off|do\s+not\s+(?:include|disclose|mention|report)|"
        r"exclude\s+from|omit\s+from|leave\s+out\s+of).{0,30}"
        r"(?:filing|report|disclosure|minutes|record|draft|final)"
    ),
}

# Merge for single-pass extraction
ALL_PATTERNS = {**PATTERNS, **DOMAIN_PATTERNS}


def extract_regex(text: str) -> dict:
    """Run all patterns against text and return match lists keyed by pattern name."""
    return {name: pat.findall(text) for name, pat in ALL_PATTERNS.items()}


# ── 3. Tokenisation / sentences ───────────────────────────────────────────────

def tokenize(text: str) -> list[str]:
    """Whitespace + punctuation tokeniser. Returns lowercase tokens ≥ 2 chars."""
    return re.findall(r"\b[a-zA-Z''\-]{2,}\b", text.lower())


def split_sentences(text: str) -> list[str]:
    """Naive sentence splitter on terminal punctuation."""
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def extract_ngrams(tokens: list[str], n: int) -> Counter:
    """Count all n-grams in a token list."""
    return Counter(
        " ".join(tokens[i : i + n]) for i in range(len(tokens) - n + 1)
    )


# ── 4. TF-IDF ─────────────────────────────────────────────────────────────────

def tfidf(documents: list[str], top_n: int = 20) -> dict[str, list[tuple]]:
    """
    Compute TF-IDF across a list of documents (pure Python, no dependencies).
    Returns the top_n scored terms per document.

    NOTE: this scores term importance; it does NOT compare financial figures or
    produce an added/removed/changed-value report. (Unchanged in this revision.)
    """
    tokenized = [tokenize(d) for d in documents]
    vocab     = {t for doc in tokenized for t in doc}
    N         = len(documents)

    # df lookup via sets (membership is O(1)); unchanged in score terms.
    doc_sets = [set(toks) for toks in tokenized]
    idf: dict[str, float] = {
        term: math.log((N + 1) / (sum(1 for ds in doc_sets if term in ds) + 1)) + 1
        for term in vocab
    }

    results = {}
    for i, (doc, tokens) in enumerate(zip(documents, tokenized)):
        tf     = Counter(tokens)
        total  = len(tokens) or 1
        scores = {t: (tf[t] / total) * idf[t] for t in tf}
        # Canonical, hash-seed-independent ordering: score desc, then term asc.
        results[f"doc_{i}"] = sorted(scores.items(), key=lambda x: (-x[1], x[0]))[:top_n]

    return results


# ── 5. Named entity extraction (rule-based) ───────────────────────────────────
# UNCHANGED. Known limitations: misses lowercase/all-caps/foreign entities,
# can cross headings/newlines, misses the demo's own statutes. Deferred.

NE_PATTERNS: dict[str, re.Pattern] = {
    "PERSON":  re.compile(
        r"\b(?:Mr\.|Mrs\.|Ms\.|Dr\.|Prof\.)\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*"
    ),
    "ORG":     re.compile(
        r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\s+"
        r"(?:Inc|LLC|Ltd|Corp|Group|Capital|Management|Partners|Foundation|Institute)\b"
    ),
    "STATUTE": re.compile(r"\b(?:\d+\s+U\.S\.C\.|CFR|§)\s*[\d.]+\b"),
    "FILING":  re.compile(r"\b(?:10-K|10-Q|8-K|S-1|20-F|DEF\s*14A)\b"),
    "CASE":    re.compile(r"\b[A-Z][a-z]+\s+v\.?\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\b"),
}


def extract_entities(text: str) -> dict:
    """Rule-based named entity extraction. No ML models required."""
    return {label: pat.findall(text) for label, pat in NE_PATTERNS.items()}


# ── 6. Co-occurrence graph ────────────────────────────────────────────────────

_STOP_WORDS = frozenset("""
    the a an and or but in on at to for of with by from as is was are were
    be been being have has had do does did will would could should may might
    shall can its it this that these those i we you he she they not no nor
    so yet both either neither one two all any some such also just very more
""".split())


def build_cooccurrence_graph(text: str, window: int = 5, top_n: int = 30) -> dict:
    """
    Slide a window over filtered tokens and count co-occurrences.
    Returns a weighted adjacency list with DETERMINISTIC ordering (independent
    of PYTHONHASHSEED).

    This is a graph of general filtered keywords — NOT specifically named
    entities.
    """
    tokens = [
        t for t in tokenize(text)
        if t not in _STOP_WORDS and len(t) > 3
    ]
    pairs: Counter = Counter()
    for i, token in enumerate(tokens):
        for other in tokens[i + 1 : i + window]:
            if token != other:
                pairs[tuple(sorted([token, other]))] += 1

    # Canonical ordering: weight desc, then pair asc — stable across hash seeds.
    top_pairs = sorted(pairs.items(), key=lambda kv: (-kv[1], kv[0]))[:top_n]
    nodes = sorted({n for (a, b), _ in top_pairs for n in (a, b)})
    return {
        "edges": [{"source": a, "target": b, "weight": w} for (a, b), w in top_pairs],
        "nodes": nodes,
    }


# ── 7. Document structure ─────────────────────────────────────────────────────

def extract_structure(text: str) -> dict:
    """
    Extract line-oriented structure: headings, paragraphs, list items, table rows.
    Works on plain text and Markdown. (DOCX structure is not reconstructed.)
    """
    lines     = text.splitlines()
    structure = {"headings": [], "lists": [], "paragraphs": [], "tables": []}
    buffer: list[str] = []

    for i, line in enumerate(lines):
        s = line.strip()
        if re.match(r"^#{1,6}\s+", s):
            level = len(re.match(r"^(#+)", s).group(1))
            structure["headings"].append({"level": level, "text": s.lstrip("#").strip(), "line": i})
        elif re.match(r"^(?:\d+\.|[-*•])\s+", s):
            structure["lists"].append({"text": s, "line": i})
        elif "|" in s and s.count("|") >= 2:
            structure["tables"].append({"row": s, "line": i})
        elif s:
            buffer.append(s)
        else:
            if buffer:
                structure["paragraphs"].append(" ".join(buffer))
                buffer = []

    if buffer:
        structure["paragraphs"].append(" ".join(buffer))

    return structure


# ── 8. Readability metrics ────────────────────────────────────────────────────

def readability(text: str) -> dict:
    """
    Compute Flesch Reading Ease and Gunning Fog Index (approximate syllables).
    Empty / word-free input returns zeros rather than a spurious one-word score.
    """
    sentences = split_sentences(text)
    words     = tokenize(text)

    if not words:
        return {
            "flesch_reading_ease": 0.0,
            "gunning_fog_index":   0.0,
            "avg_sentence_length": 0.0,
            "avg_word_syllables":  0.0,
            "sentence_count":      len(sentences),
            "word_count":          0,
        }

    syllables = sum(max(1, len(re.findall(r"[aeiou]+", w, re.I))) for w in words)
    n_s = len(sentences) or 1
    n_w = len(words)

    flesch = 206.835 - 1.015 * (n_w / n_s) - 84.6 * (syllables / n_w)
    fog    = 0.4 * (
        (n_w / n_s) +
        100 * sum(1 for w in words if len(re.findall(r"[aeiou]+", w, re.I)) >= 3) / n_w
    )
    return {
        "flesch_reading_ease": round(flesch, 2),
        "gunning_fog_index":   round(fog,    2),
        "avg_sentence_length": round(n_w / n_s, 2),
        "avg_word_syllables":  round(syllables / n_w, 2),
        "sentence_count":      n_s,
        "word_count":          n_w,
    }


# ── 9. Fingerprinting ─────────────────────────────────────────────────────────

def fingerprint(text: str, source_bytes: bytes | None = None) -> dict:
    """
    Produce an identity record with two DISTINCT hashes:

    source_sha256 (integrity)
        SHA-256 of the original file bytes, computed BEFORE any decoding or
        normalization. This is the value to store for integrity: it proves the
        bytes are unchanged between hashing and verification. It is present only
        when raw bytes are supplied (e.g. via ingest_and_extract); it is None
        for analysis of raw in-memory text. NOTE: an integrity hash is necessary
        but NOT sufficient for chain of custody, which additionally requires a
        documented handling log, trusted timestamps, and access control — none
        of which this tool provides.

    normalized_text_sha256 (deduplication)
        SHA-256 of whitespace-collapsed, lowercased extracted text. This is a
        NEAR-DUPLICATE key, NOT tamper-evidence: different source bytes (case,
        whitespace, metadata, undecodable bytes, different no-text PDFs) can
        collapse to the same value. Do not use it to assert a document was
        unaltered.
    """
    clean = re.sub(r"\s+", " ", text.lower().strip())
    fp: dict = {
        "source_sha256":          hashlib.sha256(source_bytes).hexdigest() if source_bytes is not None else None,
        "source_bytes":           len(source_bytes) if source_bytes is not None else None,
        "normalized_text_sha256": hashlib.sha256(clean.encode()).hexdigest(),
        "normalized_text_length": len(clean),
        "normalized_word_count":  len(clean.split()),
    }
    return fp


# ── 10. Main extraction pipeline ─────────────────────────────────────────────

@dataclass
class ExtractionResult:
    """Container for all extraction outputs. Call .to_dict() for JSON export."""
    outcome:      str  = Outcome.OK.value
    detail:       str  = ""
    fingerprint:  dict = field(default_factory=dict)
    structure:    dict = field(default_factory=dict)
    regex:        dict = field(default_factory=dict)
    entities:     dict = field(default_factory=dict)
    tfidf:        dict = field(default_factory=dict)
    ngrams:       dict = field(default_factory=dict)
    cooccurrence: dict = field(default_factory=dict)
    readability:  dict = field(default_factory=dict)
    ast_analysis: dict = field(default_factory=dict)
    deletions:    list = field(default_factory=list)   # structured DOCX w:delText runs
    coverage:     dict = field(default_factory=dict)   # what was / was not inspected
    summary:      dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


def extract(
    text: str,
    *,
    is_python_source: bool = False,
    extra_documents:  list[str] | None = None,
    source_bytes:     bytes | None = None,
) -> ExtractionResult:
    """
    Run all ten extraction methods and return a unified ExtractionResult.

    text             : Primary text (plain, Markdown, or Python source).
    is_python_source : If True, run full AST analysis on the text.
    extra_documents  : Additional documents for cross-document TF-IDF scoring.
    source_bytes     : Original file bytes, for the integrity hash (source_sha256).
                       Pass None when analysing raw in-memory text.
    """
    result = ExtractionResult()

    # Memory bound: cap analysed text length (flagged, not silent).
    if len(text) > MAX_TEXT_SIZE:
        text = text[:MAX_TEXT_SIZE]
        result.outcome = Outcome.PARTIAL.value
        result.detail = f"analysed text truncated to MAX_TEXT_SIZE ({MAX_TEXT_SIZE} bytes)"
        result.coverage["text_truncated"] = True

    result.fingerprint  = fingerprint(text, source_bytes=source_bytes)
    result.structure    = extract_structure(text)
    result.regex        = extract_regex(text)
    result.entities     = extract_entities(text)
    result.tfidf        = tfidf([text] + (extra_documents or []), top_n=25)
    result.cooccurrence = build_cooccurrence_graph(text)
    result.readability  = readability(text)

    tokens = tokenize(text)
    result.ngrams = {
        "unigrams": extract_ngrams(tokens, 1).most_common(20),
        "bigrams":  extract_ngrams(tokens, 2).most_common(20),
        "trigrams": extract_ngrams(tokens, 3).most_common(15),
    }

    if is_python_source:
        result.ast_analysis = extract_ast_text(text)
    else:
        attempt = extract_ast_text(text)
        if "error" not in attempt:
            result.ast_analysis = attempt

    # Summary roll-up — one value per key for easy CSV flattening
    result.summary = {
        "outcome":                result.outcome,
        "source_sha256":          result.fingerprint.get("source_sha256"),
        "normalized_text_sha256": result.fingerprint.get("normalized_text_sha256"),
        "total_headings":         len(result.structure.get("headings", [])),
        "total_paragraphs":       len(result.structure.get("paragraphs", [])),
        "emails_found":           len(result.regex.get("emails", [])),
        "urls_found":             len(result.regex.get("urls", [])),
        "dates_found":            len(result.regex.get("dates", [])),
        "money_usd":              len(result.regex.get("money_usd", [])),
        "money_gbp":              len(result.regex.get("money_gbp", [])),
        "money_eur":              len(result.regex.get("money_eur", [])),
        "orgs_found":             len(result.entities.get("ORG", [])),
        "persons_found":          len(result.entities.get("PERSON", [])),
        "statutes_found":         len(result.entities.get("STATUTE", [])),
        "filings_found":          len(result.entities.get("FILING", [])),
        "cases_found":            len(result.entities.get("CASE", [])),
        "top_unigrams":           result.ngrams["unigrams"][:10],
        "top_bigrams":            result.ngrams["bigrams"][:10],
        "graph_node_count":       len(result.cooccurrence.get("nodes", [])),
        "flesch_score":           result.readability.get("flesch_reading_ease"),
        "normalized_word_count":  result.fingerprint.get("normalized_word_count"),
        "readability_word_count": result.readability.get("word_count"),
        "deleted_runs":           len(result.deletions),
        # Domain flag counts (one per DOMAIN_PATTERNS key)
        **{k: len(result.regex.get(k, [])) for k in DOMAIN_PATTERNS},
    }

    return result


# ── Document parsers ──────────────────────────────────────────────────────────
# Parsers return a ParseResult with a typed outcome. They NEVER return an error
# string as document text — a failure is reported as outcome=FAILED and the
# caller does not analyse it.

def parse_docx(
    data: bytes,
    *,
    capture_deletions: bool = True,
    max_decompressed: int = MAX_DECOMPRESSED,
) -> ParseResult:
    """
    Extract visible text and (structured) deleted runs from a .docx byte buffer.

    Visible text  : concatenation of word/document.xml <w:t> runs (space-joined,
                    matching prior behaviour — run-boundary fidelity is deferred).
    Deletions     : list of <w:delText> run strings, captured SEPARATELY (never
                    inlined into the analysed text, so the count cannot be forged
                    by ordinary document text).

    Only Transitional WordprocessingML w:t / w:delText in word/document.xml are
    read. Headers/footers/comments/foot-endnotes/embedded objects and the Strict
    namespace are NOT inspected (reported in coverage).

    Zip-bomb guard: the decompressed document.xml is bounded to max_decompressed
    bytes; a declared or actual size over the cap yields outcome=FAILED.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = z.namelist()
            if "word/document.xml" not in names:
                return ParseResult(
                    outcome=Outcome.UNSUPPORTED,
                    detail="archive has no word/document.xml",
                    coverage={"parts_available": names[:50]},
                )
            declared = z.getinfo("word/document.xml").file_size
            if declared > max_decompressed:
                return ParseResult(
                    outcome=Outcome.FAILED,
                    detail=f"document.xml declared {declared} bytes exceeds cap "
                           f"{max_decompressed} (possible zip bomb)",
                )
            with z.open("word/document.xml") as f:
                xml_bytes = f.read(max_decompressed + 1)
            if len(xml_bytes) > max_decompressed:
                return ParseResult(
                    outcome=Outcome.FAILED,
                    detail=f"document.xml exceeds decompressed cap {max_decompressed} "
                           f"(possible zip bomb)",
                )
    except zipfile.BadZipFile as e:
        return ParseResult(outcome=Outcome.FAILED, detail=f"not a valid zip/docx: {e}")
    except Exception as e:  # noqa: BLE001 — any zip failure is a parse failure
        return ParseResult(outcome=Outcome.FAILED, detail=f"docx read error: {e}")

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        return ParseResult(outcome=Outcome.FAILED, detail=f"malformed document.xml: {e}")

    W       = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    t_tag   = f"{{{W}}}t"
    del_tag = f"{{{W}}}delText"

    visible:   list[str] = []
    deletions: list[str] = []
    for node in root.iter():
        if node.tag == t_tag and node.text:
            visible.append(node.text.strip())
        elif capture_deletions and node.tag == del_tag and node.text:
            deletions.append(node.text.strip())

    text = " ".join(x for x in visible if x)
    coverage = {
        "parts_read":   ["word/document.xml"],
        "namespace":    "transitional-only",
        "delText_runs": len(deletions),
        "note": "only w:t/w:delText in word/document.xml (Transitional WordprocessingML) "
                "are read; headers/footers/comments/foot-endnotes/embedded objects and "
                "Strict-namespace parts are NOT inspected; run-boundary fidelity deferred",
    }
    if text.strip():
        return ParseResult(text=text, outcome=Outcome.OK, deletions=deletions, coverage=coverage)
    return ParseResult(
        text="",
        outcome=Outcome.NO_TEXT,
        detail="no readable w:t runs (empty, image-only, or unsupported namespace)",
        deletions=deletions,
        coverage=coverage,
    )


def parse_pdf(data: bytes) -> ParseResult:
    """
    Extract the text layer from a PDF byte buffer. Requires pypdf.
    Image-only / scanned PDFs (no text layer) yield outcome=NO_TEXT, not success.
    """
    try:
        import pypdf
    except ImportError:
        return ParseResult(
            outcome=Outcome.MISSING_DEPENDENCY,
            detail="pypdf not installed (pip install pypdf); PDF text extraction unavailable",
        )

    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        pages  = [(page.extract_text() or "") for page in reader.pages]
    except Exception as e:  # noqa: BLE001 — any pypdf failure is a parse failure
        return ParseResult(outcome=Outcome.FAILED, detail=f"pdf read error: {e}")

    text = "\n".join(pages).strip()
    coverage = {"pages": len(pages), "note": "text layer only; image-only/scanned PDFs yield no text"}
    if text:
        return ParseResult(text=text, outcome=Outcome.OK, coverage=coverage)
    return ParseResult(
        text="",
        outcome=Outcome.NO_TEXT,
        detail="no extractable text layer (image-only or scanned PDF)",
        coverage=coverage,
    )


def parse_flat(data: bytes, *, is_python: bool = False) -> ParseResult:
    """
    Decode a flat text file (.txt/.md/.py). Non-UTF-8 bytes are REPLACED (not
    silently dropped) and the result is flagged PARTIAL so lossy decoding is
    visible. The source_sha256 (raw bytes) captures the true identity regardless.
    """
    try:
        text  = data.decode("utf-8")
        lossy = False
    except UnicodeDecodeError:
        text  = data.decode("utf-8", errors="replace")
        lossy = True

    coverage = {"decode": "utf-8", "lossy_decode": lossy, "is_python": is_python}
    if not text.strip():
        return ParseResult(text=text, outcome=Outcome.NO_TEXT, detail="file contains no text",
                           coverage=coverage)
    if lossy:
        return ParseResult(text=text, outcome=Outcome.PARTIAL,
                           detail="non-UTF-8 bytes replaced during decode", coverage=coverage)
    return ParseResult(text=text, outcome=Outcome.OK, coverage=coverage)


def _worst(a: str, b: str) -> str:
    """Return the more severe of two outcome values."""
    order = {
        Outcome.OK.value: 0, Outcome.PARTIAL.value: 1, Outcome.NO_TEXT.value: 2,
        Outcome.UNSUPPORTED.value: 3, Outcome.MISSING_DEPENDENCY.value: 4, Outcome.FAILED.value: 5,
    }
    return a if order.get(a, 0) >= order.get(b, 0) else b


def ingest_and_extract(
    filepath: str | Path,
    *,
    max_file_size: int = MAX_FILE_SIZE,
    **kwargs,
) -> ExtractionResult:
    """
    Detect file type, read raw bytes (for the integrity hash), parse with a typed
    outcome, and run the extraction pipeline. A parse failure is returned as a
    result with outcome=FAILED/MISSING_DEPENDENCY/UNSUPPORTED and the source hash
    — it is NEVER analysed as document text.

    Supported extensions: .docx  .pdf  .txt  .md  .py
    """
    path   = Path(filepath)
    suffix = path.suffix.lower()

    def _bare(outcome: Outcome, detail: str, source_bytes: bytes | None = None,
              coverage: dict | None = None) -> ExtractionResult:
        res = ExtractionResult(outcome=outcome.value, detail=detail)
        res.fingerprint = fingerprint("", source_bytes=source_bytes)
        res.coverage = coverage or {}
        res.summary = {
            "outcome":                outcome.value,
            "source_sha256":          res.fingerprint.get("source_sha256"),
            "normalized_text_sha256": res.fingerprint.get("normalized_text_sha256"),
            **{k: 0 for k in DOMAIN_PATTERNS},
        }
        return res

    if suffix not in SUPPORTED_EXTS:
        return _bare(Outcome.UNSUPPORTED, f"unsupported file type: {path.suffix}")

    try:
        size = path.stat().st_size
    except OSError as e:
        return _bare(Outcome.FAILED, f"cannot stat file: {e}")
    if size > max_file_size:
        return _bare(Outcome.FAILED, f"file {size} bytes exceeds cap {max_file_size}")

    try:
        source_bytes = path.read_bytes()
    except OSError as e:
        return _bare(Outcome.FAILED, f"cannot read file: {e}")

    if suffix == ".docx":
        pr = parse_docx(source_bytes)
    elif suffix == ".pdf":
        pr = parse_pdf(source_bytes)
    else:  # .txt / .md / .py
        pr = parse_flat(source_bytes, is_python=(suffix == ".py"))

    # Hard parse failures: do NOT analyse — return the outcome + source hash only.
    if pr.outcome in (Outcome.FAILED, Outcome.MISSING_DEPENDENCY, Outcome.UNSUPPORTED):
        return _bare(pr.outcome, pr.detail, source_bytes=source_bytes, coverage=pr.coverage)

    # Text extracted (possibly empty for NO_TEXT) — analyse it.
    res = extract(pr.text, source_bytes=source_bytes,
                  is_python_source=(suffix == ".py"), **kwargs)
    res.deletions = pr.deletions
    res.coverage  = {**pr.coverage, **res.coverage}
    res.outcome   = _worst(res.outcome, pr.outcome.value)
    if pr.detail:
        res.detail = pr.detail if not res.detail else f"{pr.detail}; {res.detail}"
    res.summary["outcome"]      = res.outcome
    res.summary["deleted_runs"] = len(res.deletions)
    return res


# ── Batch processing ──────────────────────────────────────────────────────────

def _csv_safe(value: Any) -> Any:
    """Neutralise spreadsheet formula injection in string cells."""
    if isinstance(value, str) and value and value[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def batch_process(
    folder:     str | Path,
    output_csv: str | Path = "batch_report.csv",
) -> Path:
    """
    Recursively walk a folder (DETERMINISTIC order), extract every supported
    document once, and write a flat CSV summary. Each row = one file.

    Columns include the typed outcome, both hashes, and a structured deleted-run
    count (DOCX w:delText nodes only — not a forgeable text marker). Files are
    read exactly once. String cells are neutralised against CSV formula injection.
    """
    folder     = Path(folder)
    output_csv = Path(output_csv)

    all_files = sorted(
        Path(root) / fname
        for root, _, files in os.walk(folder)
        for fname in files
        if Path(fname).suffix.lower() in SUPPORTED_EXTS
    )

    print(f"Found {len(all_files)} file(s) under '{folder}'. Processing...")

    rows: list[dict] = []
    for filepath in all_files:
        row = {"file": str(filepath), "extension": filepath.suffix.lower(),
               "outcome": "", "detail": "", "error": ""}
        try:
            result = ingest_and_extract(filepath).to_dict()
            s   = result["summary"]
            r   = result["readability"]
            fp  = result["fingerprint"]

            row.update({
                "outcome":                result["outcome"],
                "detail":                 result.get("detail", ""),
                # Hashes
                "source_sha256":          fp.get("source_sha256") or "",
                "normalized_text_sha256": fp.get("normalized_text_sha256", ""),
                "source_bytes":           fp.get("source_bytes") or 0,
                # Readability
                "flesch_score": r.get("flesch_reading_ease", ""),
                "fog_index":    r.get("gunning_fog_index", ""),
                # Structure
                "headings":     s.get("total_headings", 0),
                "paragraphs":   s.get("total_paragraphs", 0),
                # Standard regex (counts only; values known-approximate)
                "emails":       s.get("emails_found", 0),
                "urls":         s.get("urls_found", 0),
                "dates":        s.get("dates_found", 0),
                "money_usd":    s.get("money_usd", 0),
                "money_gbp":    s.get("money_gbp", 0),
                "money_eur":    s.get("money_eur", 0),
                # Named entities
                "orgs":         s.get("orgs_found", 0),
                "persons":      s.get("persons_found", 0),
                "statutes":     s.get("statutes_found", 0),
                "filings":      s.get("filings_found", 0),
                "cases":        s.get("cases_found", 0),
                # Top terms
                "top_unigrams": " | ".join(t for t, _ in (s.get("top_unigrams") or [])[:5]),
                "top_bigrams":  " | ".join(t for t, _ in (s.get("top_bigrams")  or [])[:5]),
                # Structured deleted runs (DOCX w:delText nodes; not forgeable)
                "deleted_runs": s.get("deleted_runs", 0),
                # Domain flag counts (dynamic — one column per DOMAIN_PATTERNS key)
                **{k: s.get(k, 0) for k in DOMAIN_PATTERNS},
            })
        except Exception as e:  # noqa: BLE001 — record, never abort the batch
            row["outcome"] = Outcome.FAILED.value
            row["error"]   = str(e)

        rows.append({k: _csv_safe(v) for k, v in row.items()})
        mark = {"OK": "✓", "PARTIAL": "~", "NO_TEXT": "∅"}.get(row.get("outcome", ""), "✗")
        print(f"  {mark}  [{row.get('outcome',''):<18}] {filepath.name}")

    fieldnames = [
        "file", "extension", "outcome", "detail",
        "source_sha256", "normalized_text_sha256", "source_bytes",
        "flesch_score", "fog_index",
        "headings", "paragraphs",
        "emails", "urls", "dates",
        "money_usd", "money_gbp", "money_eur",
        "orgs", "persons", "statutes", "filings", "cases",
        "top_unigrams", "top_bigrams",
        "deleted_runs",
        *DOMAIN_PATTERNS.keys(),
        "error",
    ]

    with open(output_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    print(f"\n✓  Report written → {output_csv}  ({len(rows)} rows)")
    return output_csv


# ── CLI demo ──────────────────────────────────────────────────────────────────

DEMO_TEXT = textwrap.dedent("""
    # Acquisition Due Diligence — Summary Report

    ## Executive Overview

    Acme Capital Partners LLC retained outside counsel to conduct due diligence
    on Target Corp. (NYSE: TGTX) prior to the proposed $2.4 billion acquisition.
    The review covered SEC filings (10-K, 10-Q) filed between January 2019 and
    December 2023, internal board minutes, and facility financing agreements.

    ## Key Findings

    Contact: compliance@acmecapital.com | Legal: counsel@hartwell-llp.com
    Review period: Jan 15, 2019 – Dec 31, 2023 | Updated: March 3, 2024

    The review identified the following items requiring further disclosure:
    - Credit facility (ABF warehouse) covenant status — see Exhibit C
    - Net Asset Value calculation methodology — Q3 2022 differs from prior periods
    - £180 million contingent liability not reflected in latest 10-K
    - Pending matter: Greenfield v. Target Corp., filed October 2022

    ## Statutory Obligations

    Reporting obligations under 15 U.S.C. § 78m and 17 CFR § 240.10b-5 require
    that all material facts be disclosed. Outside counsel flagged two instances
    where draft language was revised to remove references to the warehouse
    facility default status before the final filing was submitted.

    For reference: https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany
""")


def main():
    print("=" * 70)
    print("  DOCFORENSICS — DEMO RUN  (heuristic triage; see module docstring)")
    print("=" * 70)

    # Raw-text analysis: no source file, so source_sha256 is None by design.
    result = extract(DEMO_TEXT)
    output = result.to_dict()

    print(f"\n  outcome: {output['outcome']}  (raw-text input → source_sha256 is None)")
    print(f"  normalized_text_sha256: {output['fingerprint']['normalized_text_sha256'][:32]}…")

    print("\n── SUMMARY ──────────────────────────────────────────────────────────")
    for k, v in output["summary"].items():
        if k not in ("top_unigrams", "top_bigrams"):
            print(f"  {k:<26} {v}")

    print("\n── REGEX HITS (counts are leads, values known-approximate) ───────────")
    for cat, hits in output["regex"].items():
        if hits:
            print(f"  {cat}: {hits}")

    print("\n── NAMED ENTITIES ───────────────────────────────────────────────────")
    for etype, ents in output["entities"].items():
        if ents:
            print(f"  {etype}: {ents}")

    print("\n── READABILITY ──────────────────────────────────────────────────────")
    for k, v in output["readability"].items():
        print(f"  {k:<26} {v}")

    print("\n── DOMAIN FLAGS ─────────────────────────────────────────────────────")
    for k in DOMAIN_PATTERNS:
        hits = output["regex"].get(k, [])
        flag = "⚑ " if hits else "  "
        print(f"  {flag}{k:<26} {len(hits)} hit(s)  {hits[:2]}")

    out = Path("demo_output.json")
    out.write_text(json.dumps(output, indent=2, default=str))
    print(f"\n✓  Full JSON → {out}")
    print("=" * 70)


if __name__ == "__main__":
    main()
