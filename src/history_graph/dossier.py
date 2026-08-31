"""One-paper lineage dossiers: what led to it, where it is heading, what it fuses.

All ranking is deterministic given the same API payloads:
- upstream ancestors ranked by bridge-ness (shared by several frontier works,
  or referenced from siblings in the same hop), then cited-by;
- downstream citing works ranked by citation acceleration — recent counts vs
  the prior period from OpenAlex ``counts_by_year`` — so "rising" beats "old";
- fusion: connected components over the hub's direct references, linked when
  two references share an ancestor: components = the schools the paper joins.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .client import OpenAlexClient
from .insight import concept_weights
from .paths import (
    CONCEPT_FIELDS,
    DEFAULT_CACHE_DIR,
    GraphProbe,
    _brief,
    bare,
)

META_FIELDS = ("id", "doi", "title", "publication_year", "cited_by_count", "counts_by_year")
CONCEPT_GAP_SURPRISING = 0.7


def momentum(work: dict[str, Any]) -> dict[str, Any]:
    """Citation acceleration anchored on the record's own latest year (deterministic)."""
    rows = sorted(
        ((int(r.get("year") or 0), int(r.get("cited_by_count") or 0))
         for r in work.get("counts_by_year") or []),
    )
    if len(rows) < 3:
        return {"score": 0.0, "recent": 0, "prior": 0}
    last_year = rows[-1][0]
    recent = sum(c for y, c in rows if y > last_year - 2)
    prior = sum(c for y, c in rows if last_year - 4 < y <= last_year - 2)
    return {
        "score": round((recent + 1) / (prior + 1), 2),
        "recent": recent,
        "prior": prior,
        "last_year": last_year,
    }


def _gap(ca: dict[str, float], cb: dict[str, float]) -> float | None:
    if not ca or not cb:
        return None
    inter = sum(min(ca[n], cb[n]) for n in set(ca) & set(cb))
    union = sum(max(ca.get(n, 0.0), cb.get(n, 0.0)) for n in set(ca) | set(cb))
    return round(1.0 - inter / union, 4) if union else None


def _schools(probe: GraphProbe, refs: list[str]) -> list[list[str]]:
    parent = {r: r for r in refs}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(sorted(refs)):
        ra = set(probe.refs(a))
        for b in sorted(refs)[i + 1:]:
            if ra & set(probe.refs(b)):
                parent[find(a)] = find(b)
    groups: dict[str, list[str]] = {}
    for r in refs:
        groups.setdefault(find(r), []).append(r)
    for g in groups.values():
        g.sort()
    return sorted(groups.values(), key=lambda g: (g[0], g))


def lineage_dossier(
    client: OpenAlexClient,
    work_ref: str,
    *,
    depth: int = 2,
    limit: int = 8,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    cache_name: str = "graph_cache.json",
) -> dict[str, Any]:
    from .paths import resolve_endpoint

    probe = GraphProbe(client, citing_cap=12, cache_path=cache_dir / cache_name)
    hub = resolve_endpoint(client, work_ref)
    if hub is None:
        return {"found": False, "reason": f"unresolved work: {work_ref}"}
    hub_id = bare(hub["id"])
    probe.works[hub_id] = hub
    probe.fetch_works([hub_id], force=True, select=META_FIELDS)
    probe._refs.pop(hub_id, None)  # meta refetch dropped refs; force re-derive

    concepts: dict[str, dict[str, float]] = {}

    def fetch_concepts(ids: set[str]) -> None:
        for work in client.get_works(sorted(ids), select=CONCEPT_FIELDS):
            concepts[bare(work["id"])] = concept_weights(work)

    # upstream: ancestors per hop, bridge-ranked
    upstream: list[dict[str, Any]] = []
    frontier = [hub_id]
    seen = {hub_id}
    for hop in range(1, max(depth, 1) + 1):
        support: dict[str, int] = {}
        for node in frontier:
            for anc in set(probe.refs(node)):
                if anc not in seen:
                    support[anc] = support.get(anc, 0) + 1
        if not support:
            break
        probe.fetch_works(sorted(support), force=True, select=META_FIELDS)
        bridge_extra = {
            anc for a in support for anc in set(probe.refs(a)) if anc in support and anc != a
        }
        nodes = sorted(
            support,
            key=lambda i: (
                not (support[i] > 1 or i in bridge_extra),
                -int((probe.works.get(i) or {}).get("cited_by_count") or 0),
                i,
            ),
        )[:limit]
        fetch_concepts({hub_id, *nodes})
        hub_c = concepts.get(hub_id, {})
        for i in nodes:
            gap = _gap(hub_c, concepts.get(i, {}))
            upstream.append({
                "hop": hop,
                **_brief(probe.works.get(i) or {"id": i}),
                "cited_by": (probe.works.get(i) or {}).get("cited_by_count"),
                "support": support[i],
                "bridge": support[i] > 1 or i in bridge_extra,
                "concept_gap": gap,
                "surprising": gap is not None and gap >= CONCEPT_GAP_SURPRISING,
            })
        seen |= set(nodes)
        frontier = nodes

    # downstream: citing works, acceleration-ranked
    downstream: list[dict[str, Any]] = []
    frontier = [hub_id]
    seen_down = {hub_id} | {u["id"] for u in upstream}
    for hop in range(1, max(depth, 1) + 1):
        cand: set[str] = set()
        for node in frontier:
            cand |= {c for c in probe.citing(node) if c not in seen_down}
        if not cand:
            break
        probe.fetch_works(sorted(cand), force=True, select=META_FIELDS)
        nodes = sorted(
            cand,
            key=lambda i: (
                -momentum(probe.works.get(i) or {})["score"],
                -int((probe.works.get(i) or {}).get("cited_by_count") or 0),
                i,
            ),
        )[:limit]
        for i in nodes:
            m = momentum(probe.works.get(i) or {})
            downstream.append({
                "hop": hop,
                **_brief(probe.works.get(i) or {"id": i}),
                "momentum": m["score"],
                "recent_cites": m["recent"],
            })
        seen_down |= set(nodes)
        frontier = nodes

    # fusion of schools over direct references
    refs = probe.refs(hub_id)
    fuses = _schools(probe, refs) if refs else []
    probe.fetch_works({m for g in fuses for m in g})

    notes: list[str] = []
    if not refs:
        notes.append("references unknown (metadata-blind: early work or unindexed record)")
    if any(u.get("surprising") for u in upstream):
        notes.append("surprising edges: concept gap to at least one ancestor; check full text")

    out = {
        "found": True,
        "hub": {**_brief(probe.works.get(hub_id) or hub), "id": hub_id},
        "upstream": upstream,
        "downstream": downstream,
        "fuses": [_group_brief(probe, group) for group in fuses],
        "notes": notes,
    }
    out["story"] = story(out)
    probe.save()
    return out


def _group_brief(probe: GraphProbe, group: list[str]) -> list[dict[str, Any]]:
    out = []
    for m in group:
        work = probe.works.get(m) or {"id": m}
        out.append({
            "id": m,
            "doi": str(work.get("doi") or "").removeprefix("https://doi.org/"),
            "title": (work.get("title") or "")[:90],
            "year": work.get("publication_year"),
        })
    return out


def story(dossier: dict[str, Any]) -> str:
    hub = dossier["hub"]
    lines = [f"# {hub['title']} ({hub['year']})"]
    lines.append("\n## What led to it")
    for u in dossier["upstream"]:
        marks = "".join([
            " [bridge]" if u["bridge"] else "",
            " [concept jump]" if u.get("surprising") else "",
            f" [shared by {u['support']}]" if u["support"] > 1 else "",
        ])
        lines.append(f"- hop {u['hop']}, {u['year']}: {u['title']} — {u['cited_by']} cites{marks}")
    if not dossier["upstream"]:
        lines.append("- (references invisible in metadata)")
    lines.append("\n## Where it is heading (by citation acceleration)")
    for d in dossier["downstream"]:
        arrow = " ↑" if d["momentum"] > 1.3 else ""
        lines.append(
            f"- hop {d['hop']}, {d['year']}: {d['title']} — momentum {d['momentum']}{arrow}"
        )
    if not dossier["downstream"]:
        lines.append("- (no citers found)")
    fuses_groups = dossier.get("fuses") or []
    if fuses_groups:
        lines.append(f"\n## Fuses {len(fuses_groups)} research schools — it joins:")
        for group in fuses_groups:
            lines.append("- " + "; ".join(g["title"] for g in group))
    for note in dossier.get("notes") or []:
        lines.append(f"\n> caveat: {note}")
    return "\n".join(lines) + "\n"
