# Agent harness boundary

## Implemented surface

The repository implements the deterministic side of a research-agent harness.
Its public boundary is a small CLI plus one typed JSON artifact.

```text
request
  -> bounded connector calls
  -> normalize and deduplicate
  -> candidates.json
  -> external reviewing model
  -> human-approved delivery
  -> reported / confirmed ledger state
```

The external model can reason over the result, but it cannot change how evidence is collected or silently mutate delivery state.

## Context

`AGENTS.md` carries stable operating policy.
The schema carries field-level semantics.
The CLI help carries the available tool surface.
The JSON output carries evidence, coverage notes, and review flags for the current run.

This is progressive disclosure rather than one large prompt.
Stable policy stays in the repository, while run-specific facts stay in the tool result.

## Tools

The agent receives domain commands instead of general-purpose execution:

- `sources` lists available connectors.
- `hunt` performs bounded collection and writes the canonical contract.
- `mark-seen` records that a batch was sent but not yet acknowledged.
- `confirm-seen` records positive acknowledgement.
- `pending-seen` exposes deliveries that may have been lost.

## State and failure recovery

`hunt` never writes delivery state.
This prevents a crash between collection and delivery from hiding candidates permanently.

The ledger uses two phases:

1. `reported` means the delivery was attempted.
2. `confirmed` means the receiver acknowledged it.

Reported entries that remain unconfirmed can become eligible again.
The worst case is a duplicate report, not a silently lost result.

## Evaluation signals

The offline suite validates the deterministic harness invariants:

- Tool budgets stop unbounded connector use.
- Source failures remain visible.
- Identity rules are stable across runs.
- The JSON contract survives serialization and validation.
- CLI exit codes distinguish empty results from errors.
- Delivery transitions are idempotent and recoverable.

The repository does not claim to evaluate model quality.
A production integration should add trajectory and final-output evals around the external reviewing model.
