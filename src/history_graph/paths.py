"""Bidirectional BFS over citation links between two works.

Finds short undirected paths through the OpenAlex citation graph, preferring
conceptually surprising junctions. Endpoints accept DOIs, OpenAlex IDs, or
titles. Expansion is budgeted (nodes explored, citing results kept per node)
so cost is predictable, and every ordering is by explicit keys so replays and
cassettes produce identical paths.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .client import OpenAlexClient
from .insight import summarize_path

DEFAULT_CACHE_DIR = Path("data/cache")
COMPACT_FIELDS = ("id", "doi", "title", "publication_year", "cited_by_count", "referenced_works")
CITE_FIELDS = ("id", "doi", "title", "publication_year", "cited_by_count")
CONCEPT_FIELDS = ("id", "concepts")


def bare(wid: str) -> str:
    """OpenAlex returns ids as URLs but referenced_works as bare ids; key on bare."""
    return str(wid).removeprefix("https://openalex.org/")


def _norm(work: dict[str, Any] | None) -> dict[str, Any] | None:
    return None if work is None else {**work, "id": bare(work["id"])}


def resolve_endpoint(client: OpenAlexClient, ref: str) -> dict[str, Any] | None:
    """Accept a DOI, an OpenAlex work ID, or a title; return a compact work."""
    ref = ref.strip()
    if ref.startswith("W") and ref[1:].isdigit():
        return _norm(next(iter(client.get_works([ref], select=COMPACT_FIELDS)), None))
    if ref.lower().startswith("https://doi.org/"):
        ref = ref[len("https://doi.org/") :]
    if ref.lower().startswith("doi:"):
        ref = ref[4:]
    if ref.startswith("10."):
        return _norm(client.get_work_by_doi(ref.lower()))
    return _norm(client.get_work_by_title(ref))


class GraphProbe:
    """Lazy adjacency over the citation graph with a JSON disk cache."""

    def __init__(
        self,
        client: OpenAlexClient,
        *,
        citing_cap: int = 24,
        cache_path: Path | None = None,
    ) -> None:
        self.client = client
        self.citing_cap = citing_cap
        self.cache_path = cache_path
        self.works: dict[str, dict[str, Any]] = {}
        self.adjacency: dict[str, list[str]] = {}
        self._refs: dict[str, list[str]] = {}
        self._citing: dict[str, list[str]] = {}
        if cache_path and cache_path.exists():
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            self.works.update(data.get("works", {}))
            self.adjacency.update(data.get("adjacency", {}))

    def save(self) -> None:
        if self.cache_path is None:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(
            json.dumps(
                {"works": self.works, "adjacency": self.adjacency},
                indent=1,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def fetch_works(self, ids: list[str], *, force: bool = False, select=None) -> None:
        # NOTE: requests are keyed by the FULL sorted id set, never by the
        # cache-derived subset -- otherwise replay drifts against the cassette
        # whenever cache state differs (every id/field fetched is deduped by key).
        wanted = sorted(set(ids))
        if not force:
            fresh = [i for i in wanted if i not in self.works]
            if not fresh:
                return
        for start in range(0, len(wanted), 50):
            for work in self.client.get_works(
                wanted[start : start + 50], select=select or COMPACT_FIELDS
            ):
                wid = bare(work["id"])
                self.works[wid] = {**self.works.get(wid, {}), **_norm(work)}

    def refs(self, wid: str) -> list[str]:
        """Referenced works of wid (all, deterministic bare ids)."""
        if wid not in self._refs:
            work = self.works.get(wid) or {}
            if "referenced_works" not in work:
                self.fetch_works([wid], force=True, select=COMPACT_FIELDS)
                work = self.works.get(wid) or {}
            refs = [bare(r) for r in work.get("referenced_works") or []]  # noqa: E501
            if refs:
                self.fetch_works(refs)
            self._refs[wid] = refs
        return self._refs[wid]

    def citing(self, wid: str) -> list[str]:
        """Top-cited works citing wid (page-capped, bare ids)."""
        if wid not in self._citing:
            self._citing[wid] = [
                bare(w["id"])
                for w in sorted(
                    self.client.iter_citing_works(
                        wid, select=CITE_FIELDS, max_pages=1,
                        per_page=max(40, self.citing_cap * 2),
                    ),
                    key=lambda w: (-int(w.get("cited_by_count") or 0), w["id"]),
                )[: self.citing_cap]
            ]
        return self._citing[wid]

    def neighbors(self, wid: str) -> list[str]:
        """Deterministic neighbourhood: all references + top-cited citers."""
        if wid in self.adjacency:
            return self.adjacency[wid]
        pool = sorted(
            set(self.refs(wid)) | set(self.citing(wid)),
            key=lambda i: (-int((self.works.get(i) or {}).get("cited_by_count") or 0), i),
        )
        self.adjacency[wid] = pool
        return pool


def _chain(parents: dict[str, str | None], meet: str, *, reverse: bool) -> list[str]:
    chain = [meet]
    node = parents[meet]
    while node is not None:
        chain.append(node)
        node = parents[node]
    return list(reversed(chain)) if reverse else chain


def citation_path(
    client: OpenAlexClient,
    from_ref: str,
    to_ref: str,
    *,
    max_nodes: int = 80,
    max_paths: int = 3,
    citing_cap: int = 24,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    cache_name: str = "graph_cache.json",
) -> dict[str, Any]:
    """Shortest-ish citation path(s) between two works. Deterministic."""
    cache_path = Path(cache_dir) / cache_name
    probe = GraphProbe(client, citing_cap=citing_cap, cache_path=cache_path)
    source = resolve_endpoint(client, from_ref)
    target = resolve_endpoint(client, to_ref)
    if source is None or target is None:
        missing = from_ref if source is None else to_ref
        return {"found": False, "reason": f"unresolved endpoint: {missing}"}
    if bare(source["id"]) == bare(target["id"]):
        return {"found": False, "reason": "same work", "work": bare(source["id"])}

    sid, tid = bare(source["id"]), bare(target["id"])
    fwd: dict[str, str | None] = {sid: None}
    bwd: dict[str, str | None] = {tid: None}
    distf = {sid: 0}
    distb = {tid: 0}
    frontier = {"fwd": [sid], "bwd": [tid]}
    meets: list[tuple[str, int]] = []
    expansions = 0

    while (frontier["fwd"] or frontier["bwd"]) and expansions < max_nodes:
        if not frontier["fwd"]:
            side = "bwd"
        elif not frontier["bwd"]:
            side = "fwd"
        else:
            side = "fwd" if len(frontier["fwd"]) <= len(frontier["bwd"]) else "bwd"
        parents, dist, odist = (
            (fwd, distf, distb) if side == "fwd" else (bwd, distb, distf)
        )
        nxt: list[str] = []
        for node in frontier[side]:
            if expansions >= max_nodes:
                break
            expansions += 1
            for nb in probe.neighbors(node):
                if nb in parents:
                    continue
                parents[nb] = node
                dist[nb] = dist[node] + 1
                if nb in odist:
                    meets.append((nb, dist[nb] + odist[nb]))
                else:
                    nxt.append(nb)
        frontier[side] = nxt
        best = min((t for _, t in meets), default=None)
        if best is not None:
            bound = min(
                min((distf[n] for n in frontier["fwd"]), default=float("inf")),
                min((distb[n] for n in frontier["bwd"]), default=float("inf")),
            )
            if bound + 1 >= best:
                break

    probe.save()
    if not meets:
        return {
            "found": False,
            "reason": "budget exhausted before paths met",
            "source": _brief(source),
            "target": _brief(target),
            "explored": expansions,
        }

    best = min(total for _, total in meets)
    paths: list[list[str]] = []
    for meet, total in sorted(meets):
        if total != best:
            continue
        ids = _chain(fwd, meet, reverse=True) + _chain(bwd, meet, reverse=False)[1:]
        if ids not in paths:
            paths.append(ids)
    paths.sort(key=lambda p: (len(p), p[0], p[-1]))
    paths = paths[: max(max_paths, 1)]

    probe.fetch_works(sorted({i for p in paths for i in p}))
    concepts: dict[str, list[dict[str, Any]]] = {}
    for work in client.get_works(sorted({i for p in paths for i in p}), select=CONCEPT_FIELDS):
        concepts[bare(work["id"])] = work.get("concepts") or []

    counted: dict[str, int] = {}
    for p in paths:
        for i in p:
            counted[i] = counted.get(i, 0) + 1
    shared = sorted(
        i for i, n in counted.items() if n == len(paths) and i not in (source["id"], target["id"])
    )

    out_paths = []
    for p in paths:
        merged = []
        for wid in p:
            w = dict(probe.works.get(wid) or {})
            w["concepts"] = concepts.get(wid, [])
            merged.append(w)
        out_paths.append({
            "ids": p,
            "nodes": [_brief(w) for w in merged],
            "edges": _edge_labels(probe, p),
            **summarize_path(merged),
        })
    out_paths.sort(key=lambda p: (-p["score"], p["ids"][0]))
    return {
        "found": True,
        "source": _brief(source),
        "target": _brief(target),
        "paths": out_paths,
        "bridges": [_brief(probe.works.get(b) or {"id": b}) for b in shared] or None,
        "explored": expansions,
    }


def _edge_labels(probe: GraphProbe, ids: list[str]) -> list[dict[str, str]]:
    labels = []
    for a, b in zip(ids, ids[1:], strict=False):
        probe.fetch_works([a, b], force=any(
            "referenced_works" not in (probe.works.get(x) or {}) for x in (a, b)
        ))
        ab = (probe.works.get(a) or {}).get("referenced_works") or []
        ba = (probe.works.get(b) or {}).get("referenced_works") or []
        kind = "a-cites-b" if b in ab else "b-cites-a" if a in ba else "related"
        labels.append({"from": a, "to": b, "kind": kind})
    return labels


def _brief(work: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": work.get("id"),
        "doi": str(work.get("doi") or "").removeprefix("https://doi.org/"),
        "title": (work.get("title") or "")[:90],
        "year": work.get("publication_year"),
        "cited_by": work.get("cited_by_count"),
    }
