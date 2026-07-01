"""
Tests for pipeline.llm_validation — runnable as:
  pytest pipeline/tests/test_llm_validation.py -q
  python  pipeline/tests/test_llm_validation.py
"""

from __future__ import annotations

import sys
import os

# Allow running directly without an installed package.
_HERE = os.path.dirname(__file__)
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from pipeline.llm_validation import (
    clamp_prob,
    valid_enum,
    valid_bool,
    require_keys,
    sanitize_text,
    validate_estimate,
    LLM_MAY_PROVIDE,
    LLM_MUST_NOT_PROVIDE,
)

# ── Minimal test harness (no pytest dependency) ──────────────────────────────

_PASS = 0
_FAIL = 0
_FAILURES: list[str] = []


def _check(label: str, got, expected) -> None:
    global _PASS, _FAIL
    if got == expected:
        _PASS += 1
    else:
        _FAIL += 1
        _FAILURES.append(f"FAIL  {label}\n       got={got!r}  expected={expected!r}")


def _check_none(label: str, got) -> None:
    _check(label, got, None)


# ── clamp_prob ───────────────────────────────────────────────────────────────


def test_clamp_prob() -> None:
    _check("clamp_prob(0.5)", clamp_prob(0.5), 0.5)
    _check("clamp_prob(1.3) clamps to 1.0", clamp_prob(1.3), 1.0)
    _check("clamp_prob(-0.2) clamps to 0.0", clamp_prob(-0.2), 0.0)
    _check("clamp_prob('0.4') parses", clamp_prob("0.4"), 0.4)
    _check_none("clamp_prob(None) -> default", clamp_prob(None))
    _check_none("clamp_prob('abc') -> default", clamp_prob("abc"))
    # Bools must be INVALID — True is NOT 1.0
    _check_none("clamp_prob(True) -> INVALID", clamp_prob(True))
    _check_none("clamp_prob(False) -> INVALID", clamp_prob(False))
    _check_none("clamp_prob(nan) -> default", clamp_prob(float("nan")))
    _check_none("clamp_prob(inf) -> default", clamp_prob(float("inf")))
    # Edge: integer 0 and 1 are valid (not bool)
    _check("clamp_prob(0) -> 0.0", clamp_prob(0), 0.0)
    _check("clamp_prob(1) -> 1.0", clamp_prob(1), 1.0)
    # Custom default propagated
    _check("clamp_prob(None, default=0.5)", clamp_prob(None, default=0.5), 0.5)


# ── valid_enum ───────────────────────────────────────────────────────────────


def test_valid_enum() -> None:
    allowed = {"YES", "NO"}
    _check("valid_enum 'YES' exact", valid_enum("YES", allowed), "YES")
    _check("valid_enum 'yes' case-insensitive", valid_enum("yes", allowed), "YES")
    _check("valid_enum 'no' case-insensitive", valid_enum("no", allowed), "NO")
    _check_none("valid_enum 'MAYBE' not in set", valid_enum("MAYBE", allowed))
    _check_none("valid_enum 5 (non-str)", valid_enum(5, allowed))
    _check_none("valid_enum None", valid_enum(None, allowed))
    # With a custom default
    _check(
        "valid_enum 'MAYBE' with default='NO'",
        valid_enum("MAYBE", allowed, default="NO"),
        "NO",
    )
    # Whitespace tolerance
    _check("valid_enum ' yes ' strips", valid_enum(" yes ", allowed), "YES")


# ── valid_bool ───────────────────────────────────────────────────────────────


def test_valid_bool() -> None:
    _check("valid_bool(True)", valid_bool(True), True)
    _check("valid_bool(False)", valid_bool(False), False)
    _check_none("valid_bool('true') -> None", valid_bool("true"))
    _check_none("valid_bool(1) -> None", valid_bool(1))
    _check_none("valid_bool('yes') -> None", valid_bool("yes"))
    _check_none("valid_bool(None) -> None", valid_bool(None))
    _check_none("valid_bool(0) -> None", valid_bool(0))
    _check_none("valid_bool('false') -> None", valid_bool("false"))


# ── require_keys ─────────────────────────────────────────────────────────────


def test_require_keys() -> None:
    d = {"a": 1, "b": 2, "c": None}
    _check("require_keys all present non-None", require_keys(d, ["a", "b"]), True)
    _check("require_keys missing key", require_keys(d, ["a", "d"]), False)
    _check("require_keys value is None", require_keys(d, ["a", "c"]), False)
    _check("require_keys empty keys list", require_keys(d, []), True)
    _check("require_keys non-dict", require_keys("not a dict", ["a"]), False)


# ── sanitize_text ─────────────────────────────────────────────────────────────


def test_sanitize_text() -> None:
    _check("sanitize_text basic", sanitize_text("hello"), "hello")
    _check("sanitize_text strips", sanitize_text("  hello  "), "hello")
    _check(
        "sanitize_text collapses newline",
        sanitize_text("line1\nline2"),
        "line1 line2",
    )
    _check(
        "sanitize_text collapses CRLF",
        sanitize_text("line1\r\nline2"),
        "line1 line2",
    )
    _check(
        "sanitize_text truncates",
        sanitize_text("a" * 400, max_len=10),
        "a" * 10,
    )
    _check("sanitize_text coerces int", sanitize_text(42), "42")
    _check("sanitize_text coerces None", sanitize_text(None), "None")


# ── validate_estimate ─────────────────────────────────────────────────────────


def test_validate_estimate() -> None:
    # Happy path
    result = validate_estimate({"fair_prob": 0.8, "confidence": 0.6})
    _check("validate_estimate fair_prob", result and result["fair_prob"], 0.8)
    _check("validate_estimate confidence", result and result["confidence"], 0.6)

    # Clamping: 1.5 -> 1.0, missing confidence -> 0.5
    result2 = validate_estimate({"fair_prob": 1.5})
    _check("validate_estimate clamps prob to 1.0", result2 and result2["fair_prob"], 1.0)
    _check(
        "validate_estimate default confidence 0.5",
        result2 and result2["confidence"],
        0.5,
    )

    # Invalid prob string
    _check_none(
        "validate_estimate 'x' prob -> None",
        validate_estimate({"fair_prob": "x"}),
    )

    # Bool prob must be rejected
    _check_none(
        "validate_estimate True prob -> None",
        validate_estimate({"fair_prob": True}),
    )

    # Missing fair_prob
    _check_none(
        "validate_estimate missing fair_prob -> None",
        validate_estimate({"confidence": 0.9}),
    )

    # Non-dict input
    _check_none("validate_estimate non-dict -> None", validate_estimate("not a dict"))

    # Invalid confidence falls back to default 0.5
    result3 = validate_estimate({"fair_prob": 0.3, "confidence": "bad"})
    _check("validate_estimate bad confidence -> 0.5", result3 and result3["confidence"], 0.5)

    # Negative prob clamps to 0.0
    result4 = validate_estimate({"fair_prob": -0.1})
    _check("validate_estimate negative prob clamps to 0.0", result4 and result4["fair_prob"], 0.0)


# ── Module-level constants ────────────────────────────────────────────────────


def test_constants() -> None:
    _check(
        "LLM_MAY_PROVIDE is tuple",
        isinstance(LLM_MAY_PROVIDE, tuple),
        True,
    )
    _check(
        "LLM_MUST_NOT_PROVIDE is tuple",
        isinstance(LLM_MUST_NOT_PROVIDE, tuple),
        True,
    )
    _check(
        "LLM_MUST_NOT_PROVIDE has dollar-amount entry",
        any("dollar" in s for s in LLM_MUST_NOT_PROVIDE),
        True,
    )


# ── Runner ────────────────────────────────────────────────────────────────────


def _run_all() -> None:
    test_clamp_prob()
    test_valid_enum()
    test_valid_bool()
    test_require_keys()
    test_sanitize_text()
    test_validate_estimate()
    test_constants()

    total = _PASS + _FAIL
    print(f"\n{'='*60}")
    print(f"Results: {_PASS}/{total} passed, {_FAIL} failed")
    if _FAILURES:
        print()
        for msg in _FAILURES:
            print(msg)
    print("="*60)
    if _FAIL:
        sys.exit(1)
    else:
        print("ALL PASS")


# ── pytest-compatible entry points ────────────────────────────────────────────
# Each pytest-discoverable function calls the same sub-test logic but raises on
# failure so pytest captures individual failures.


def _assert(label: str, got, expected) -> None:
    assert got == expected, f"{label}: got={got!r} expected={expected!r}"


def test_clamp_prob_pytest() -> None:
    _assert("0.5", clamp_prob(0.5), 0.5)
    _assert("1.3->1.0", clamp_prob(1.3), 1.0)
    _assert("-0.2->0.0", clamp_prob(-0.2), 0.0)
    _assert("'0.4'->0.4", clamp_prob("0.4"), 0.4)
    assert clamp_prob(None) is None
    assert clamp_prob("abc") is None
    assert clamp_prob(True) is None, "bool True must be INVALID"
    assert clamp_prob(False) is None, "bool False must be INVALID"
    assert clamp_prob(float("nan")) is None
    assert clamp_prob(float("inf")) is None
    _assert("int 0->0.0", clamp_prob(0), 0.0)
    _assert("int 1->1.0", clamp_prob(1), 1.0)


def test_valid_enum_pytest() -> None:
    allowed = {"YES", "NO"}
    _assert("exact", valid_enum("YES", allowed), "YES")
    _assert("lower", valid_enum("yes", allowed), "YES")
    assert valid_enum("MAYBE", allowed) is None
    assert valid_enum(5, allowed) is None


def test_valid_bool_pytest() -> None:
    assert valid_bool(True) is True
    assert valid_bool(False) is False
    assert valid_bool("true") is None
    assert valid_bool(1) is None
    assert valid_bool("yes") is None
    assert valid_bool(None) is None


def test_require_keys_pytest() -> None:
    d = {"a": 1, "b": 2}
    assert require_keys(d, ["a", "b"]) is True
    assert require_keys(d, ["a", "c"]) is False
    assert require_keys({"a": None}, ["a"]) is False


def test_sanitize_text_pytest() -> None:
    assert sanitize_text("  hi\nbye  ") == "hi bye"
    assert sanitize_text("a" * 400, max_len=10) == "a" * 10


def test_validate_estimate_pytest() -> None:
    r = validate_estimate({"fair_prob": 0.8, "confidence": 0.6})
    assert r == {"fair_prob": 0.8, "confidence": 0.6}

    r2 = validate_estimate({"fair_prob": 1.5})
    assert r2 == {"fair_prob": 1.0, "confidence": 0.5}

    assert validate_estimate({"fair_prob": "x"}) is None
    assert validate_estimate({"fair_prob": True}) is None
    assert validate_estimate({"confidence": 0.9}) is None

    r3 = validate_estimate({"fair_prob": -0.1})
    assert r3 == {"fair_prob": 0.0, "confidence": 0.5}


if __name__ == "__main__":
    _run_all()
