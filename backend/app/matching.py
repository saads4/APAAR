"""Indic-aware name + DOB matching with Fellegi-Sunter (1969) style log-likelihood weights.

No labelled data is needed: m/u probabilities are expert priors documented below. The output is
always explainable: per-field agreement level, weight (bits), and field-level fix suggestions.
"""
import math
import re
from datetime import date
from typing import Optional

from rapidfuzz import fuzz

HONORIFICS = {"shri", "shree", "smt", "sri", "kum", "kumari", "mr", "mrs", "ms", "miss", "dr", "late", "sh"}
# Common abbreviations / spelling families seen on Indian records.
CANON = {
    "mohd": "mohammed", "md": "mohammed", "mohamad": "mohammed", "mohammad": "mohammed",
    "muhammad": "mohammed", "mohamed": "mohammed", "mohmmed": "mohammed", "muhammed": "mohammed",
    "kr": "kumar", "kmr": "kumar", "chandra": "chandra", "chander": "chandra", "chandr": "chandra",
    "lakshmi": "laxmi", "laxmi": "laxmi", "krishna": "krishna", "krushna": "krishna", "kishan": "krishna",
    "sai": "sai", "shyam": "shyam", "syam": "shyam", "ramesh": "ramesh", "rajesh": "rajesh",
}


def _tokens(name: str) -> list[str]:
    name = name.lower()
    name = re.sub(r"[^\w\s]", " ", name, flags=re.UNICODE)
    toks = [t for t in name.split() if t and t not in HONORIFICS]
    return [CANON.get(t, t) for t in toks]


def phonetic(tok: str) -> str:
    """Rule-based phonetic skeleton for romanised Indian names (Sharma~Sarma, Khan~Kan, Vijay~Wijay)."""
    t = tok.lower()
    for a, b in (("ph", "f"), ("sh", "s"), ("ck", "k"), ("q", "k"), ("w", "v"), ("z", "j"),
                 ("aa", "a"), ("ee", "i"), ("ii", "i"), ("oo", "u"), ("uu", "u"), ("ou", "u")):
        t = t.replace(a, b)
    t = re.sub(r"(?<=[kgtdbpjc])h", "", t)  # aspirates: kh gh th dh bh ph jh ch -> unaspirated
    t = re.sub(r"h$", "", t)
    t = re.sub(r"y$", "i", t)
    t = re.sub(r"(.)\1+", r"\1", t)
    return t


def token_sim(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if len(a) <= 2 and len(b) > 2 and a.rstrip(".")[0] == b[0]:
        return 0.9  # initial vs full token
    if len(b) <= 2 and len(a) > 2 and b.rstrip(".")[0] == a[0]:
        return 0.9
    pa, pb = phonetic(a), phonetic(b)
    if pa == pb:
        return 0.96
    return fuzz.ratio(pa, pb) / 100.0


def name_score(submitted: str, registry: str) -> float:
    A, B = _tokens(submitted), _tokens(registry)
    if not A or not B:
        return 0.0
    short, long_ = (A, B) if len(A) <= len(B) else (B, A)
    pool = list(long_)
    total = 0.0
    for t in short:  # greedy best alignment; order-insensitive (surname-first records)
        best, bi = 0.0, -1
        for i, u in enumerate(pool):
            s = token_sim(t, u)
            if s > best:
                best, bi = s, i
        if bi >= 0:
            pool.pop(bi)
        total += best
    return round(0.75 * total / len(short) + 0.25 * total / len(long_), 4)


def dob_level(submitted: date, registry: date) -> str:
    if submitted == registry:
        return "EXACT"
    if submitted.day <= 12 and registry.day <= 12 and submitted.day == registry.month \
            and submitted.month == registry.day and submitted.year == registry.year:
        return "SWAPPED"
    diffs = (submitted.day != registry.day) + (submitted.month != registry.month) + (submitted.year != registry.year)
    return "ONE_FIELD" if diffs == 1 else "DIFFERENT"


# (m, u) per agreement level: P(level | same person), P(level | different persons).
NAME_MU = {"AGREE": (0.90, 0.005), "PARTIAL": (0.08, 0.02), "DISAGREE": (0.02, 0.975)}
DOB_MU = {"EXACT": (0.93, 0.0004), "SWAPPED": (0.03, 0.0002), "ONE_FIELD": (0.03, 0.003), "DIFFERENT": (0.01, 0.9966)}
PRIOR_ODDS = 1.0  # the ID was supplied with the claim, so "same person" starts at 50%
PASS_NAME = 0.85
PROBABLE = 0.60


def name_level(score: float) -> str:
    return "AGREE" if score >= PASS_NAME else "PARTIAL" if score >= 0.70 else "DISAGREE"


def match(name: str, dob: date, reg_name: str, reg_dob: date) -> dict:
    ns = name_score(name, reg_name)
    nl, dl = name_level(ns), dob_level(dob, reg_dob)
    w_name = math.log2(NAME_MU[nl][0] / NAME_MU[nl][1])
    w_dob = math.log2(DOB_MU[dl][0] / DOB_MU[dl][1])
    odds = PRIOR_ODDS * 2 ** (w_name + w_dob)
    prob = odds / (1 + odds)
    passed = nl == "AGREE" and dl == "EXACT"
    fixes: list[dict] = []
    if not passed and prob >= PROBABLE:  # only reveal registry values for a probable match (anti-enumeration)
        if dl != "EXACT":
            why = "day and month appear swapped" if dl == "SWAPPED" else "one date component differs"
            fixes.append({"field": "dob", "submitted": dob.isoformat(), "suggested": reg_dob.isoformat(), "reason": why})
        if nl != "AGREE":
            fixes.append({"field": "name", "submitted": name, "suggested": reg_name,
                          "reason": "name differs from the registry spelling"})
    return {"passed": passed, "probable": prob >= PROBABLE, "prob": round(prob, 4), "name_score": ns,
            "name_level": nl, "dob_level": dl, "weights_bits": {"name": round(w_name, 2), "dob": round(w_dob, 2)},
            "fixes": fixes}


def parse_dob(v: str | date) -> Optional[date]:
    if isinstance(v, date):
        return v
    v = (v or "").strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", v)
    try:
        if m:
            return date(int(m[1]), int(m[2]), int(m[3]))
        m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", v)
        if m:
            return date(int(m[3]), int(m[2]), int(m[1]))
    except ValueError:
        return None
    return None
