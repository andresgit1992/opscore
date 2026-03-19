from __future__ import annotations

import re
from typing import Dict


_RX_SPACES = re.compile(r"[ \t]+")
_RX_OC_NO = re.compile(r"orden\s+de\s+compra\s+n[o]?\s*\d{3,}", re.IGNORECASE)


RBU_ULTRA = [
    "rbu santiago s.a",
    "rbu santiago sa",
    "redbus urbano",
    "red movilidad",
]

METROPOL_ULTRA = [
    "metropol chile",
    "orden de compra nro",
    "orden compra nro",
    "rut proveedor",
    "nro proveedor",
]

GA_ULTRA = [
    "gran america",
    "granamerica",
    "orden de compra ga",
]


def detect_oc_type(text: str) -> str:
    if not text:
        return "UNKNOWN"

    t = _norm(text)

    early = _early_detect(t)
    if early:
        return early

    scores: Dict[str, int] = {
        "METROPOL": _score_metropol(t),
        "RBU": _score_rbu(t),
        "GRAN_AMERICA": _score_gran_america(t),
    }

    best_type = max(scores, key=lambda k: scores[k])
    best_score = scores[best_type]

    if best_score <= 0:
        return "UNKNOWN"

    if best_score < 2:
        return "UNKNOWN"

    sorted_types = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)

    if len(sorted_types) >= 2 and sorted_types[0][1] == sorted_types[1][1]:
        return _break_tie(t, sorted_types[0][0], sorted_types[1][0])

    return best_type


def _norm(s: str) -> str:
    s = (s or "").lower()
    s = s.replace("\u00a0", " ")
    s = _RX_SPACES.sub(" ", s)
    return s.strip()


def _has_any(t: str, keys: list[str]) -> bool:
    return any(k in t for k in keys)


def _count_hits(t: str, keys: list[str]) -> int:
    return sum(1 for k in keys if k in t)


def _early_detect(t: str) -> str | None:
    if _has_any(t, RBU_ULTRA):
        return "RBU"

    if _has_any(t, METROPOL_ULTRA):
        return "METROPOL"

    if _has_any(t, GA_ULTRA):
        return "GRAN_AMERICA"

    return None


def _score_metropol(t: str) -> int:
    strong = [
        "metropol",
        "metropol chile",
        "orden de compra nro",
        "orden compra nro",
        "rut proveedor",
        "nro proveedor",
    ]

    medium = [
        "proveedor",
        "rut:",
        "santiago",
    ]

    score = 0
    score += 5 * _count_hits(t, [
        "metropol chile",
        "orden de compra nro",
        "rut proveedor",
        "nro proveedor",
    ])
    score += 3 * _count_hits(t, strong)
    score += 1 * _count_hits(t, medium)

    if "orden de compra nro" in t or "orden compra nro" in t:
        score += 3

    if _has_any(t, ["rbu santiago", "redbus", "red movilidad"]):
        score -= 5

    return score


def _score_rbu(t: str) -> int:
    strong = [
        "rbu santiago s.a",
        "rbu santiago sa",
        "redbus urbano",
        "redbus",
        "red movilidad",
        "ministerio de transportes y telecomunicaciones",
    ]

    medium = [
        "orden de compra",
        "neto",
        "iva",
        "total",
        "item",
        "valor unitario",
        "valor total",
        "facturar a",
        "senores",
    ]

    score = 0
    score += 6 * _count_hits(t, [
        "rbu santiago s.a",
        "rbu santiago sa",
    ])
    score += 4 * _count_hits(t, strong)
    score += 1 * _count_hits(t, medium)

    if _RX_OC_NO.search(t):
        score += 4

    if _has_any(t, [
        "metropol chile",
        "rut proveedor",
        "nro proveedor",
        "orden de compra nro",
    ]):
        score -= 6

    return score


def _score_gran_america(t: str) -> int:
    strong = [
        "gran america",
        "granamerica",
        "granamerica s.a",
        "orden de compra ga",
    ]

    medium = [
        "orden de compra",
        "oc",
    ]

    score = 0
    score += 5 * _count_hits(t, [
        "gran america",
        "orden de compra ga",
    ])
    score += 3 * _count_hits(t, strong)
    score += 1 * _count_hits(t, medium)

    if _has_any(t, [
        "metropol chile",
        "orden de compra nro",
        "rut proveedor",
        "nro proveedor",
    ]):
        score -= 4

    if _has_any(t, [
        "rbu santiago",
        "redbus",
        "red movilidad",
    ]):
        score -= 4

    return score


def _break_tie(t: str, a: str, b: str) -> str:
    ultra = {
        "METROPOL": METROPOL_ULTRA,
        "RBU": RBU_ULTRA + ["ministerio de transportes y telecomunicaciones"],
        "GRAN_AMERICA": GA_ULTRA,
    }

    a_ultra = _count_hits(t, ultra.get(a, []))
    b_ultra = _count_hits(t, ultra.get(b, []))

    if a_ultra > b_ultra:
        return a

    if b_ultra > a_ultra:
        return b

    if _RX_OC_NO.search(t):
        return "RBU"

    if "orden de compra nro" in t or "orden compra nro" in t:
        return "METROPOL"

    return a
