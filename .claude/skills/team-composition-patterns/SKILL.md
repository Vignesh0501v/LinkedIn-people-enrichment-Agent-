---
name: team-composition-patterns
description: Decide whether a task needs a delegated specialist at all, how many, and which ones. Use before spawning any sub-agent from core-dev-team — when sizing a team, choosing between a single specialist and a multi-specialist team, or deciding a task is small enough to just do directly.
version: 0.1.0
---

# Team Composition Patterns

## Rule 1: default to no delegation

Most requests don't need a sub-agent. Do it directly when the task touches one file/area,
or is a quick lookup/fix. Delegation overhead (cold-start context, tool re-declaration,
round-trip) only pays for itself when the task is substantial enough that keeping its
exploration/output out of your own context is worth more than that overhead.

## Rule 2: smallest team that covers the request

| Complexity | Team | When |
|---|---|---|
| Trivial | 0 (manager does it) | Single file, quick fix, lookup |
| Simple | 1 specialist | Single area, self-contained |
| Moderate | 2 specialists | Two areas with a clear interface (e.g. api + frontend) |
| Complex | 3-4 specialists | Full-stack feature, or implementation + test + review |

Two specialists with non-overlapping ownership beats four with fuzzy ones. Overlapping
coverage produces duplicate work and wastes tokens on redundant exploration.

## Preset shapes

- **Single-area fix**: manager does it directly, no delegation.
- **Full-stack feature (well-scoped)**: `api-developer` (defines contract) →
  `backend-developer` + `frontend-developer` in parallel (consume contract) →
  `test-engineer` → `code-reviewer`.
- **New project or major feature (ambiguous scope)**: `business-analyst` (brief) →
  `trd-writer` (TRD, if real technical decisions are needed) → `planner` (task list) →
  manager delegates the above shapes → `acceptance-check` before reporting done.
- **Backend-only feature**: `backend-developer` → `test-engineer` → `code-reviewer`.
- **Website/UI recreation from a URL**: `frontend-recreate` alone — it's self-contained,
  doesn't need the full brief/TRD/plan pipeline unless the user wants it integrated into a
  larger feature.
- **Bug fix**: manager investigates and fixes directly if scoped to one file; otherwise the
  owning specialist fixes it and `test-engineer` adds a regression test.
- **Pre-ship check**: `code-reviewer` alone, read-only, no implementation change.

## Anti-patterns

- Spawning a specialist for a task the manager could finish in one or two tool calls.
- Assigning the same file to two specialists "to be safe" — creates merge conflicts and
  wasted work instead of preventing them.
- Running `code-reviewer` before implementation is actually complete — review what's done,
  not what's planned.
