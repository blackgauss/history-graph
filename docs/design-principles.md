# Software Design Principles

These principles apply across the repository. The examples are tailored to an
agent-first research system, but the principles are intended to guide any
feature added here.

## Motivation

Readability is a design goal. Correctness, clarity, and concision reinforce one
another: a reader should be able to understand what a component promises before
having to understand how it fulfills that promise.

State the intended behavior first—a proposition, contract, or invariant—then
make the implementation read like the argument for why that behavior holds.
Types, interfaces, and tests should help a reader verify that argument. This is
not a requirement to formalize every function; it is a reminder to make the
reasoning visible.

## System propositions

Research workflows should preserve these properties:

- **Discovery:** A search result identifies its source and retains the query or
  criteria that found it. Ranking is a useful heuristic, not evidence that a
  result is true or complete.
- **Identity:** Records have stable canonical identifiers where possible, but
  retain the identifiers assigned by each source. Do not silently merge records
  merely because their titles look alike.
- **Citation direction:** A citation edge has an explicit direction and relation
  (for example, `citing → cited`). “References of” and “cited by” are distinct
  operations. Paper-to-paper, patent-to-patent, and paper-to-patent links are
  distinct relation types.
- **Evidence:** Factual claims in a research result can be traced to source
  records and, when available, a passage, page, or other locator.
- **Completeness:** An empty result, a partial result, and a failed lookup are
  different outcomes. Pagination limits, source coverage, and truncation must
  be visible; a provider failure must never be presented as proof that no link
  exists.
- **Human review:** Exploratory findings and proposed additions remain distinct
  from curated facts. Changes to curated research data use an explicit review
  step.
- **Confidentiality:** Public-source material and internal hypotheses or
  invention notes have distinct storage and access boundaries. Private material
  must not be sent to a public provider as an incidental part of a query.
- **Robustness:** External sources can change, rate-limit, return incomplete
  records, or block automated clients. Represent these outcomes explicitly and
  preserve enough information to retry or investigate them.

Each feature should state which propositions apply and how its interface makes
them observable.

## Types communicate meaning

Variable names distinguish values; types should communicate their shape and
category. Prefer semantic types when they prevent meaningful mistakes—for
example, distinguishing a DOI from an OpenAlex work ID, a patent publication
number, or a source URL. Use product types to keep related facts together and
sum types to represent mutually exclusive outcomes.

Use type hints where they make an interface substantially easier to understand,
not as ceremony. A result that can be found, missing, partial, or unavailable is
usually clearer as a named set of outcomes than as several booleans or a
sentinel value. The set of valid states should be apparent from the type or
schema.

## Hide decisions likely to change

**Principle:** Place a boundary around a decision likely to change, then hide
that decision behind the boundary. For every shared type, ask which design
decision it exposes.

**Application here:** Keep provider-specific request formats, pagination,
identifier quirks, and retry behavior inside source adapters. Shared research
logic should consume stable domain concepts—records, evidence, citation edges,
and lookup status—not Google/OpenAlex/Semantic Scholar response payloads.

## Primary facts are fields; derived values are functions

**Principle:** A quantity treated as a primitive fact by the domain should be a
first-class value in the interface. Do not make an implementation-specific
representation the only way to express it. Derived values should be computed
from primary facts, not used as substitutes for them.

**Application here:** Preserve a provider's citation relation, publication
date, or source identifier as retrieved facts. Relevance scores, inferred
lineage, deduplication decisions, and summaries are derived results; expose
their method and inputs rather than overwriting the underlying records.

## Carry provenance with decision-driving values

**Principle:** A value used to make or recommend a decision travels with its
supporting provenance. This makes the result auditable, replayable, and
available for comparison or shadow evaluation.

**Application here:** A citation edge should identify its source provider and
source record. A quoted claim should retain its document and locator. A
ranked suggestion should distinguish retrieved evidence from the ranking
heuristic. Keep retrieval time and relevant transformation/version metadata
where they are needed to reproduce the result.

## A set of booleans is a sum type waiting to be named

**Principle:** If only some combinations of flags are valid, represent the
valid modes as a union of named alternatives instead of independent booleans.

**Application here:** Model outcomes such as complete, partial, unavailable,
and not found explicitly. Do not permit contradictory states such as both
“complete” and “provider failed.” Apply the same rule to configuration modes.

## Make seams composable

**Principle:** Prefer interfaces that compose. When a new behavior can wrap an
existing interface instead of requiring edits throughout the system, keep that
seam explicit.

**Application here:** Search providers, citation resolvers, ranking stages,
and evidence enrichers should be replaceable or composable behind small
contracts. A new provider should usually require a new adapter and contract
tests—not provider-specific branches throughout agent workflows.

## Dependencies point toward stability

**Principle:** Keep the foundation minimal. Frequently changing details should
depend on stable concepts; stable concepts should not depend on volatile
features.

**Application here:** The shared layer should contain only concepts many parts
of the system must agree on, such as identifiers, source provenance, citation
direction, and result completeness. Provider fields, ranking formulas, and
particular research-thread schemas belong in more changeable layers unless
there is a demonstrated need to promote them.

## Match the relevant literature and domain language

**Principle:** A domain expert should be able to express established concepts
without translating them into implementation-specific jargon.

**Application here:** Use explicit concepts such as reference, citation,
patent family, evidence, and claim where appropriate. Keep formulas and
assumptions visible in analysis code. Do not collapse a formal domain relation
into a generic “related” edge merely to simplify an implementation.

## Test designs by anticipated change

Before building a feature, list plausible changes and check whether each would
force a change to the stable foundation. For this repository, examples include:

- adding or replacing a public-data provider;
- a provider changing identifier formats, response fields, or pagination;
- a patent family having multiple publication numbers;
- a source returning no results, partial results, a rate limit, a bot wall, or
  an error;
- following a citation in either direction or across publication types;
- changing ranking or summarization without changing the underlying evidence.

If a likely change would spread provider-specific logic through the foundation,
reconsider the boundary before implementation.

## Test source contracts, not just local code paths

Offline unit tests should cover parsing, normalization, edge direction,
pagination, and all distinct result states using fixtures representative of
real provider responses. For citation features, use known records and verify
that returned edges point the right way; where possible, cross-check a paper's
reference against the reciprocal citing-paper result.

Keep live integration checks separate from deterministic tests, but run them
when validating provider behavior. A passing mock-only suite does not establish
that a live endpoint still accepts the request or returns relevant records.
Never convert a source error, limit, or incomplete page into a successful empty
result.

## Agent-facing interfaces

An agent tool is an API. Give each tool a narrow purpose, explicit inputs, and
outputs that preserve identifiers, provenance, and completeness. Make
side-effects apparent; keep exploratory tools read-only where possible and
require review for changes to curated data. Prefer structured, bounded results
that let an agent decide the next step without guessing what an empty or
truncated response means.

## Suggested change workflow

1. State the behavior as a proposition or contract.
2. List likely future changes and failure modes.
3. Identify the decision most likely to change and put it behind a seam.
4. Define the domain types and distinct result states.
5. Add representative contract tests, including negative and partial outcomes.
6. Implement the smallest change that satisfies the contract.
7. Run deterministic tests and, when external providers are involved, a focused
   live check. Report coverage limits rather than implying completeness.
