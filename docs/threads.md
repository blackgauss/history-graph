# Threads: hypotheses about lineage, graded against citations

A thread is a *claim* — "X influenced Y through these intermediate works" —
materialized as entries with dates, kinds (`paper`/`patent`/`event`), and
`related:` edges. It is a hypothesis until the citation graph agrees with it.

## Two homes, one gate

| state | location | written by | trust |
|---|---|---|---|
| candidate | `data/candidates/<slug>.yaml` (gitignored) | agents | unproven |
| curated | `data/seed/computing_thread.yaml` | humans | lineage of record |

`promote_thread` / `apply_proposals` refuse to write the curated files unless the
**server** environment has `HG_ALLOW_CURATED_WRITES=1` — an agent's tool call
cannot forge it, so autonomous sessions can propose forever but bless never.

## Format

```yaml
# data/candidates/heap-lineage.yaml
claim: "Binary heaps (1964) → meldable heaps (1970s) → amortized heaps (1987)"
entries:
  - id: williams-heapsort-1964
    date: "1964-03"
    kind: paper
    title: "Algorithm 232: Heapsort"
    refs:
      doi: "10.1145/363219.363264"
  - id: fredman-tarjan-fibonacci-1987
    date: "1987-07"
    kind: paper
    title: "Fibonacci heaps and their uses..."
    refs:
      doi: "10.1145/42282.42289"
    related: [williams-heapsort-1964]   # the edge the graph must support
```

## Lifecycle (what to tell an agent, in English)

1. **Propose** — *"create a thread that …"* → `propose_thread(slug, claim, seed_dois)`.
2. **Grow** — *"what's missing between these?"* → `grow_thread(slug)` ranks
   candidates: works cited by both sides of a weak link beat mere neighbors.
   Add picks with `add_thread_entries` (schema-checked, dedup by `id`).
3. **Test** — *"check the thread"* → `test_thread(slug)`, the scorecard. Thread
   edges = chronological neighbours + declared `related` pairs; each edge is
   labeled with how OpenAlex metadata backs it:
   - **cites-earlier** — one side's reference list contains the other, in the
     right temporal direction: the strong causal edge.
   - **co-cited** — both cite a common ancestor: corroborating context, not causation.
   - **needs-text** — no metadata edge but plausible (e.g. 1960s papers whose
     reference lists don't survive, or unindexed records); bridged only by PDF
     full text. An edge that is the *sole* connection between components is
     flagged `bridge` — those are the fetch-first list.
   - **citation-anachronism** — the "citation" points from older to newer: an
     error in your dates or identities, reported loudly.
   - **unresolved** — entry not resolvable: `dead` (404 — wrong identifier) or
     `probe_error` (OpenAlex couldn't answer — rate limit/budget; NOT "doesn't
     exist"). Either way nothing is silently counted false.
   Thread verdict: `sound` → `retest` (API flakiness, don't trust yet) →
   `needs-text` (only text evidence can close it) → `gaps`.
4. **Improve** — repeat 2–3 until only honest `needs-text` edges remain.
   This is how the `backprop-to-ctr` candidate found its long-way-round bridges
   (Richardson'07 GBDT → McMahan'13 → Friedman'01): the scorecard rejected the
   direct 1986→2012 link and the frontier named the middle.
5. **Promote** — you read `test_thread`, then *"promote it"* (or run the CLI),
   with the human flag set. Entries append to the curated YAML, comment
   formatting preserved, and DVC turns them into reports.

## Related quarantine flows

- `propose_seed(doi, reason)` / `propose_event(entry, evidence)` — single
  additions; human `apply_proposals` approves the batch.
- Probe sessions also land here automatically when they cite sources
  ("[proposal evidence: …]" notes keep the provenance).

## Inspecting existing threads

`list_candidate_threads` → slugs; `test_thread` → their scorecards; curated
counts via `thread_status`.
