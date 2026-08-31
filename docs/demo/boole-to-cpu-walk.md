# How did Boolean algebra reach the modern CPU?

*A deterministic replay (HTTP cassettes) of an agent walking this question
purely through the `history-graph` MCP tools. Each step shows the tool call
and its result. Reproduce with `uv run python scripts/demo_lineage_walk.py`.*

## 1. Inspect the curated thread -> spot the gap

Nothing in 1800-1944 mentions **Boole** or **switching circuits**; the jump
from Turing's symbols to ENIAC's hardware has no bridge.

- `1809` Theoria motus corporum coelestium (introduces least squares)
- `1865` A Dynamical Theory of the Electromagnetic Field
- `1876-03-06` Improvements in Telegraphy (the telephone patent)
- `1877` Bell Telephone Company founded
- `1893` Bell telephone master patent expires
- `1899` AT&T acquires American Bell's assets
- `1907` Thermionic triode (vacuum tube) invented
- `1925` Mervin J. Kelly joins Bell Labs
- `1936` On Computable Numbers, with an Application to the Entscheidungsproblem

## 2. Resolve candidates via OpenAlex

```
{
  "openalex_id": "https://openalex.org/W1578044518",
  "doi": "https://doi.org/10.1017/cbo9780511693090",
  "title": "An Investigation of the Laws of Thought",
  "year": 2009,
  "type": "book",
  "cited_by": 649,
  "authors": [
    "George Boole"
  ],
  "author_count": 1,
  "venue": "Cambridge University Press eBooks",
  "referenced_works": []
}
```

```
{
  "openalex_id": "https://openalex.org/W2053619330",
  "doi": "https://doi.org/10.1109/t-aiee.1938.5057767",
  "title": "A symbolic analysis of relay and switching circuits",
  "year": 1938,
  "type": "article",
  "cited_by": 1019,
  "authors": [
    "Claude E. Shannon"
  ],
  "author_count": 1,
  "venue": "Transactions of the American Institute of Electrical Engineers",
  "referenced_works": [
    "https://openalex.org/W2971268466"
  ]
}
```

*The 1854 original resolves only by title (pre-DOI era); the 1938 MIT thesis
has its own record.*

## 3. Hit the metadata wall

`references_of(W1995875735)` -> **0 references**. OpenAlex has no reference trail for
this era of works, so metadata alone cannot walk the link. The agent has to go
read the paper.

## 4. Fetch the full text and pull an actual quote

`fetch_pdf('10.1109/t-aiee.1938.5057767')` -> downloaded via sci-hub (11-page thesis).

`search_fulltext("Boolean")` ->

> …f the calculus of propositions. The algebra of logie1-3 originated by George Boole, is a symbolic method of investigating logical relationships. The symbols of Boolean algebra admit of two logical interpretations. If interpreted in terms of classes, the variables are not limited to the two possible values 0 and 1. This interp…

*That sentence is the Boolean->circuits link, in Shannon's own words.*

## 5. Propose into quarantine (schema-validated, evidence-required)

- `propose_seed` -> proposed: `10.1109/t-aiee.1938.5057767`
- `propose_event` -> proposed: `boole-laws-of-thought-1854`
- `propose_event` -> proposed: `shannon-switching-circuits-1938`

## 6. Human approves; curated inputs grow

`apply_proposals` -> events ['boole-laws-of-thought-1854', 'shannon-switching-circuits-1938'], seeds ['10.1109/t-aiee.1938.5057767']

Appended to `computing_thread.yaml` (comments preserved):

```yaml
  - id: boole-laws-of-thought-1854
    date: "1854"
    kind: paper
    title: "An Investigation of the Laws of Thought"
    who: "George Boole"
    refs:
      title_search: "An Investigation of the Laws of Thought"
    related: [turing-computable-numbers-1936]
    notes: "[proposal evidence: OpenAlex W-record for the 1854 original (no references: pre-DOI era)]"

  - id: shannon-switching-circuits-1938
    date: "1938"
    kind: paper
    title: "A symbolic analysis of relay and switching circuits"
    who: "Claude Shannon"
    refs:
      doi: "10.1109/t-aiee.1938.5057767"
    related: [boole-laws-of-thought-1854, eniac-completed-1945]
    notes: "[proposal evidence: thesis quote: f the calculus of propositions. The algebra of logie1-3 originated by George Boole, is a symbolic method of investigating logical relationships. The symbols of ...]"

```

*Next `dvc repro` re-resolves papers, rebuilds report.md, and the citation-graph diff becomes reviewable like any other code change.*

---
*Nothing above touched the network at generation time - the same walk runs
in `tests/test_lineage_scenario.py` as a regression guard.*
