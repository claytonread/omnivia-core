# Q1 noncanonical examples — deliberately empty

This directory deliberately contains **no example JSON bytes** for any of the
nine Q1-A evidence schemas.

## Why nothing is here yet

Materializing an example instance means committing concrete campaign,
frontier, oracle, root-hash, or other fixture bytes to the repo. The
release-matrix and materialization gate that would admit those bytes as
qualification input is not open in this part of the work. Until that gate
opens, generating example JSON here would create fixture-shaped bytes with no
authority behind them.

## What this absence does not mean

The absence of examples in this directory must not be read as, and does not
constitute, any of the following:

- acceptance of the Q1-A schemas or dataset,
- publication of a canonical or reference dataset,
- an assertion that any generator, oracle, or policy evaluator is effective
  or correct,
- a benchmark result or benchmark authority of any kind,
- a production profile or production-readiness signal,
- selection of any adapter, backend, or vector-search implementation.

## What is here instead

Only the nine JSON Schema documents under `qualification/semantic_index/q1/schemas/`
and their tests. Those schemas define the shape evidence must take; they do
not themselves assert that any instance of that shape exists, is valid data,
or has been accepted.
