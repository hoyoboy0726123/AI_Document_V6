"""
Spec-ID extractor for knowledge graph construction.

ISO/IEC/MIL/IEEE/ASTM/SAE/JIS/CNS spec identifiers follow well-known formats
— regex catches ~90% of mentions reliably. LLM is reserved for relation
classification (see kg_relations.py), not extraction.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, List, Set, Tuple


@dataclass(frozen=True)
class SpecMatch:
    canonical_id: str
    type: str
    surface: str
    span: Tuple[int, int]


_SPEC_PATTERNS: List[Tuple[str, re.Pattern]] = [
    # ISO 9001 / ISO 9001:2015 / ISO/IEC 27001:2022 / ISO 14971-1:2019
    (
        "ISO",
        re.compile(
            r"\bISO(?:/IEC|/ASTM|/TS|/TR)?\s+(\d{3,5})(?:-(\d{1,3}))?(?::(\d{4}))?\b",
            re.IGNORECASE,
        ),
    ),
    # IEC 60068-2-30 / IEC 61010-1:2010
    (
        "IEC",
        re.compile(
            r"\bIEC\s+(\d{4,5})(?:-(\d{1,3}))?(?:-(\d{1,3}))?(?::(\d{4}))?\b",
            re.IGNORECASE,
        ),
    ),
    # MIL-STD-810G / MIL-STD-810H Method 514.6 (we only capture the std ID)
    (
        "MIL_STD",
        re.compile(
            r"\bMIL[-\s]STD[-\s](\d{2,4}[A-Z]?)\b",
            re.IGNORECASE,
        ),
    ),
    # MIL-HDBK-217F
    (
        "MIL_HDBK",
        re.compile(
            r"\bMIL[-\s]HDBK[-\s](\d{2,4}[A-Z]?)\b",
            re.IGNORECASE,
        ),
    ),
    # MIL-DTL-38999
    (
        "MIL_DTL",
        re.compile(
            r"\bMIL[-\s]DTL[-\s](\d{2,5}[A-Z]?)\b",
            re.IGNORECASE,
        ),
    ),
    # MIL-PRF-31032
    (
        "MIL_PRF",
        re.compile(
            r"\bMIL[-\s]PRF[-\s](\d{2,5}[A-Z]?)\b",
            re.IGNORECASE,
        ),
    ),
    # ASTM D638 / ASTM E84-23
    (
        "ASTM",
        re.compile(
            r"\bASTM\s+([A-Z])(\d{1,4})(?:[-/](\d{2,4}))?\b",
            re.IGNORECASE,
        ),
    ),
    # IEEE 802.11b / IEEE 802.1Q / IEEE 1156
    # Audit H9：捕捉群補上可選字母後綴 [A-Za-z]{0,2}。舊 pattern 的結尾 \b 在
    # "802.11b" 的 "11" 與 "b" 之間比對失敗 → 回溯只抓到 "IEEE 802"，使 802.11b /
    # 802.1Q / 802.3 全部塌陷成同一個 "IEEE 802" 實體，關係邊掛錯節點。
    (
        "IEEE",
        re.compile(
            r"\bIEEE\s+(\d{1,4}(?:\.\d{1,3})?[A-Za-z]{0,2})\b",
            re.IGNORECASE,
        ),
    ),
    # SAE J1455 / SAE AS9100D
    (
        "SAE",
        re.compile(
            r"\bSAE\s+([A-Z]{1,3})(\d{1,5}[A-Z]?)\b",
            re.IGNORECASE,
        ),
    ),
    # JIS Z 2371 / JIS C 60068-2-1
    (
        "JIS",
        re.compile(
            r"\bJIS\s+([A-Z])\s*(\d{3,5})(?:-(\d{1,3}))?(?:-(\d{1,3}))?\b",
            re.IGNORECASE,
        ),
    ),
    # CNS 14034 / CNS 14034:2021
    (
        "CNS",
        re.compile(
            r"\bCNS\s+(\d{3,6})(?::(\d{4}))?\b",
            re.IGNORECASE,
        ),
    ),
    # EN 61000-4-2 (European norm, mirrors IEC)
    (
        "EN",
        re.compile(
            r"\bEN\s+(\d{4,5})(?:-(\d{1,3}))?(?:-(\d{1,3}))?(?::(\d{4}))?\b",
            re.IGNORECASE,
        ),
    ),
    # UL 94 / UL 60950-1
    (
        "UL",
        re.compile(
            r"\bUL\s+(\d{1,5})(?:-(\d{1,3}))?\b",
            re.IGNORECASE,
        ),
    ),
]


def _canonicalize(type_: str, m: re.Match) -> str:
    """Build a canonical key from a regex match — drives entity dedup."""
    groups = [g for g in m.groups() if g]
    body = ""
    if type_ == "ISO":
        num, sub, year = (m.group(1), m.group(2), m.group(3))
        body = num + (f"-{sub}" if sub else "") + (f":{year}" if year else "")
        prefix = m.group(0).split(None, 1)[0].upper()  # preserves ISO/IEC, ISO/TS, etc.
        return f"{prefix} {body}"
    if type_ == "IEC":
        num, s1, s2, year = m.group(1), m.group(2), m.group(3), m.group(4)
        body = num + (f"-{s1}" if s1 else "") + (f"-{s2}" if s2 else "") + (f":{year}" if year else "")
        return f"IEC {body}"
    if type_ == "MIL_STD":
        return f"MIL-STD-{m.group(1).upper()}"
    if type_ == "MIL_HDBK":
        return f"MIL-HDBK-{m.group(1).upper()}"
    if type_ == "MIL_DTL":
        return f"MIL-DTL-{m.group(1).upper()}"
    if type_ == "MIL_PRF":
        return f"MIL-PRF-{m.group(1).upper()}"
    if type_ == "ASTM":
        letter, num, year = m.group(1).upper(), m.group(2), m.group(3)
        body = f"{letter}{num}" + (f"-{year}" if year else "")
        return f"ASTM {body}"
    if type_ == "IEEE":
        return f"IEEE {m.group(1)}"
    if type_ == "SAE":
        return f"SAE {m.group(1).upper()}{m.group(2).upper()}"
    if type_ == "JIS":
        letter, num, s1, s2 = m.group(1).upper(), m.group(2), m.group(3), m.group(4)
        body = f"{letter} {num}" + (f"-{s1}" if s1 else "") + (f"-{s2}" if s2 else "")
        return f"JIS {body}"
    if type_ == "CNS":
        num, year = m.group(1), m.group(2)
        return f"CNS {num}" + (f":{year}" if year else "")
    if type_ == "EN":
        num, s1, s2, year = m.group(1), m.group(2), m.group(3), m.group(4)
        body = num + (f"-{s1}" if s1 else "") + (f"-{s2}" if s2 else "") + (f":{year}" if year else "")
        return f"EN {body}"
    if type_ == "UL":
        num, sub = m.group(1), m.group(2)
        return f"UL {num}" + (f"-{sub}" if sub else "")
    return " ".join(groups)


def _overlaps(span: Tuple[int, int], consumed: List[Tuple[int, int]]) -> bool:
    s, e = span
    for cs, ce in consumed:
        if s < ce and e > cs:
            return True
    return False


def extract_specs(text: str) -> List[SpecMatch]:
    """Return all unique spec-IDs found in text, preserving first occurrence span.

    Patterns are checked in declaration order; once a span is consumed by an
    earlier pattern (e.g. ISO/IEC covers the whole substring), later patterns
    skip overlapping spans so we don't double-count "ISO/IEC X" as both ISO and IEC.
    """
    if not text:
        return []

    seen: Set[str] = set()
    consumed: List[Tuple[int, int]] = []
    results: List[SpecMatch] = []
    for type_, pattern in _SPEC_PATTERNS:
        for m in pattern.finditer(text):
            span = (m.start(), m.end())
            if _overlaps(span, consumed):
                continue
            canonical = _canonicalize(type_, m)
            if canonical in seen:
                consumed.append(span)
                continue
            seen.add(canonical)
            consumed.append(span)
            results.append(
                SpecMatch(
                    canonical_id=canonical,
                    type=type_,
                    surface=m.group(0),
                    span=span,
                )
            )
    results.sort(key=lambda r: r.span[0])
    return results


def extract_unique_ids(texts: Iterable[str]) -> List[SpecMatch]:
    """Run extract_specs across many text chunks, dedup by canonical_id."""
    seen: Set[str] = set()
    out: List[SpecMatch] = []
    for t in texts:
        for sm in extract_specs(t):
            if sm.canonical_id in seen:
                continue
            seen.add(sm.canonical_id)
            out.append(sm)
    return out
