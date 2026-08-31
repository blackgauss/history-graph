"""Insight scoring: which citation hops are structurally surprising?

A hop is interesting when it crosses conceptual communities (high
concept_gap) — routine citations within a field score near zero. Concepts come
from OpenAlex work records (deprecated but still populated); missing concepts
score as neutral, never as zero, so sparse metadata is not mistaken for
uninteresting.
"""

from __future__ import annotations

from typing import Any

CONCEPT_TOP = 8


def concept_weights(work: dict[str, Any]) -> dict[str, float]:
    concepts = work.get("concepts") or []
    ranked = sorted(
        concepts, key=lambda c: (-float(c.get("score") or 0), str(c.get("display_name")))
    )
    return {
        str(c.get("display_name")): float(c.get("score") or 0.0)
        for c in ranked[:CONCEPT_TOP]
        if c.get("display_name")
    }


def concept_gap(a: dict[str, Any], b: dict[str, Any], *, neutral: float = 0.4) -> float:
    """1 - similarity of top-concept sets; neutral when metadata lacks concepts."""
    wa, wb = concept_weights(a), concept_weights(b)
    if not wa or not wb:
        return neutral
    names_a, names_b = set(wa), set(wb)
    inter = sum(min(wa[n], wb[n]) for n in names_a & names_b)
    union = sum(max(wa.get(n, 0.0), wb.get(n, 0.0)) for n in names_a | names_b)
    if union <= 0:
        return neutral
    return round(1.0 - inter / union, 4)


def summarize_path(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    """Score a chain of work dicts (endpoint -> endpoint) hop by hop."""
    gaps = [concept_gap(a, b) for a, b in zip(nodes, nodes[1:], strict=False)]
    score = round(sum(gaps) / len(gaps), 4) if gaps else 0.0
    return {
        "score": score,
        "hop_gaps": gaps,
        "surprising_hops": [i for i, g in enumerate(gaps) if g >= 0.7],
    }
