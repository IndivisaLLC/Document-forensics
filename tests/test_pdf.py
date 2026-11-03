"""
PDF-path coverage for docforensics.parse_pdf / ingest_and_extract.

Runs under pytest, or standalone:  python tests/test_pdf.py
Needs only `pypdf` at runtime (fixtures are committed under tests/fixtures/).
If pypdf is absent, every PDF parse must report MISSING_DEPENDENCY, and the
suite asserts exactly that instead.
"""

import os
import sys
import importlib.util

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_FIX = os.path.join(_HERE, "fixtures")

# import the module under test by path (no install required)
_spec = importlib.util.spec_from_file_location("docforensics", os.path.join(_ROOT, "docforensics.py"))
d = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(d)

try:
    import pypdf  # noqa: F401
    HAVE_PYPDF = True
except ImportError:
    HAVE_PYPDF = False

# fixture -> outcome expected when pypdf IS available
EXPECT = {
    "text_simple.pdf":    d.Outcome.OK.value,
    "text_multipage.pdf": d.Outcome.OK.value,
    "image_only.pdf":     d.Outcome.NO_TEXT.value,
    "blank.pdf":          d.Outcome.NO_TEXT.value,
    "encrypted.pdf":      d.Outcome.FAILED.value,
    "malformed.pdf":      d.Outcome.FAILED.value,
}


def _outcome(fixture: str) -> dict:
    return d.ingest_and_extract(os.path.join(_FIX, fixture)).to_dict()


def test_fixtures_present():
    for f in EXPECT:
        assert os.path.exists(os.path.join(_FIX, f)), f"missing fixture {f}"


def test_pdf_outcomes():
    for fixture, expected in EXPECT.items():
        r = _outcome(fixture)
        exp = expected if HAVE_PYPDF else d.Outcome.MISSING_DEPENDENCY.value
        assert r["outcome"] == exp, f"{fixture}: got {r['outcome']}, expected {exp}"


def test_text_pdf_flows_into_pipeline():
    if not HAVE_PYPDF:
        return  # covered by test_pdf_outcomes (MISSING_DEPENDENCY)
    r = _outcome("text_simple.pdf")
    assert r["outcome"] == d.Outcome.OK.value
    assert r["regex"]["emails"] == ["compliance@acme.com"]
    assert r["fingerprint"]["normalized_word_count"] > 0


def test_multipage_coverage_reports_pages():
    if not HAVE_PYPDF:
        return
    r = _outcome("text_multipage.pdf")
    assert r["coverage"].get("pages") == 3


def test_failed_parse_not_analyzed_but_hashed():
    """A failed parse must carry the source-byte hash yet never be analyzed as text."""
    if not HAVE_PYPDF:
        return
    r = _outcome("encrypted.pdf")
    assert r["outcome"] == d.Outcome.FAILED.value
    assert r["regex"] == {}            # not analyzed as document text
    assert r["entities"] == {}
    assert r["fingerprint"]["source_sha256"] is not None  # integrity hash still computed


def test_missing_dependency_branch():
    """With pypdf unavailable, parse_pdf must report MISSING_DEPENDENCY, not crash."""
    saved = sys.modules.get("pypdf")
    sys.modules["pypdf"] = None  # force ImportError inside parse_pdf
    try:
        pr = d.parse_pdf(open(os.path.join(_FIX, "text_simple.pdf"), "rb").read())
    finally:
        if saved is not None:
            sys.modules["pypdf"] = saved
        else:
            sys.modules.pop("pypdf", None)
    assert pr.outcome == d.Outcome.MISSING_DEPENDENCY


def _main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t(); print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1; print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed  (pypdf={'present' if HAVE_PYPDF else 'absent'})")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_main())
