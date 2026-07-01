"""
LLM -> Python Boundary Validation
==================================
Rule: the LLM is a judgment oracle, not a calculator.

It may produce ONLY:
  - a probability in [0,1]
  - a categorical label from a fixed set
  - a short rationale string

It must NEVER supply dollar amounts, position sizes, edge/EV figures, P&L, or
any arithmetic result.  ALL real math is done in Python from verified market
data.  This module sits at the boundary and enforces that contract by validating
and clamping every LLM output before it can touch any downstream calculation.
Never trust; always clamp.
"""

from __future__ import annotations

import math
from typing import Optional

# ── Documentation constants (also grep-able) ────────────────────────────────

LLM_MAY_PROVIDE = (
    "a probability in [0,1]",
    "a categorical judgment from a fixed set",
    "a short rationale string",
)

LLM_MUST_NOT_PROVIDE = (
    "any dollar amount",
    "any position size",
    "any edge/EV",
    "any P&L",
    "any arithmetic result",
)


# ── Validators ───────────────────────────────────────────────────────────────


def clamp_prob(x, default: Optional[float] = None) -> Optional[float]:
    """Coerce *x* to a float probability in [0, 1].

    Returns *default* (None by default) for any value that cannot be safely
    interpreted as a finite probability:
      - Python bool (True/False) is rejected — the subclass relationship to int
        makes silent coercion dangerous.
      - NaN and infinite values are rejected.
      - Strings are attempted via float(); failure returns default.
      - Out-of-range finite values are clamped to [0, 1].
    """
    # Reject bools before any numeric check — bool is a subclass of int.
    if isinstance(x, bool):
        return default

    if isinstance(x, (int, float)):
        if not math.isfinite(x):
            return default
        return float(max(0.0, min(1.0, x)))

    if isinstance(x, str):
        try:
            v = float(x)
        except (ValueError, TypeError):
            return default
        if not math.isfinite(v):
            return default
        return float(max(0.0, min(1.0, v)))

    return default


def valid_enum(x, allowed: set, default=None) -> Optional[str]:
    """Return the canonical member of *allowed* that matches *x* case-insensitively.

    *allowed* should be a set of str.  If *x* is not a str, or if no
    case-insensitive match exists, *default* is returned.
    """
    if not isinstance(x, str):
        return default

    x_upper = x.strip().upper()
    for member in allowed:
        if isinstance(member, str) and member.upper() == x_upper:
            return member

    return default


def valid_bool(x) -> Optional[bool]:
    """Accept only real Python bool True/False; everything else returns None.

    Strings like "true"/"yes", integers 1/0, and None are all rejected so that
    a malformed LLM response cannot silently pass a boolean gate.
    """
    if type(x) is bool:  # strict: no subclasses
        return x
    return None


def require_keys(d: dict, keys: list) -> bool:
    """Return True only if every key in *keys* is present in *d* and not None."""
    if not isinstance(d, dict):
        return False
    return all(k in d and d[k] is not None for k in keys)


def sanitize_text(x, max_len: int = 300) -> str:
    """Coerce *x* to str, strip whitespace, collapse newlines, and truncate.

    Safe for logging or display; never raises.
    """
    try:
        text = str(x)
    except Exception:
        text = ""
    text = text.strip().replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    # Collapse runs of whitespace introduced by newline replacement
    import re
    text = re.sub(r" {2,}", " ", text)
    return text[:max_len]


def validate_estimate(
    d: dict,
    *,
    prob_key: str = "fair_prob",
    conf_key: str = "confidence",
) -> Optional[dict]:
    """Validate a raw LLM probability estimate dict.

    Returns ``{"fair_prob": <clamped float>, "confidence": <clamped float>}``
    or ``None`` if the probability field is missing or invalid.

    Rules
    -----
    - ``fair_prob`` is mandatory and must survive ``clamp_prob``; otherwise None.
    - ``confidence`` is optional; defaults to 0.5 if absent or invalid.
    - Both values are clamped to [0, 1].
    """
    if not isinstance(d, dict):
        return None

    raw_prob = d.get(prob_key)
    fair_prob = clamp_prob(raw_prob)
    if fair_prob is None:
        return None

    raw_conf = d.get(conf_key)
    confidence = clamp_prob(raw_conf)
    if confidence is None:
        confidence = 0.5

    return {"fair_prob": fair_prob, "confidence": confidence}
