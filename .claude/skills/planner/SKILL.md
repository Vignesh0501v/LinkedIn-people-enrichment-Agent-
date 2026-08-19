---
name: planner
description: Turns an approved project brief and TRD into a scoped, ordered implementation plan that the manager agent can execute. Use after business-analyst (and trd-writer, if the work needed one) produce their documents, or whenever a request needs breaking into ordered, scoped units before any specialist starts work.
version: 0.1.0
---

# Planner

Turn goals and technical decisions into an ordered list of scoped units of work — the input
the `manager` agent needs to delegate without guessing.

## When to use this skill

- Right after `business-analyst` produces an approved brief (and `trd-writer` a TRD, if the
  work needed one — see that skill's guidance on when it's needed).
- Whenever a request is too large or too ambiguous to hand directly to `manager` for
  decomposition — planning the shape of the work, not doing the work.

If a TRD exists, read it alongside the brief: its stack/data-model/contract decisions
determine what `api-developer`, `backend-developer`, and `frontend-developer` units actually
need to do, not just that they need to happen.

## What a good plan contains

For each unit of work:
- **Scope** — what it covers, in one or two sentences.
- **Owner area** — which specialist would take it (frontend / backend / api / test /
  review), or "manager direct" if it's small enough not to need delegation at all.
- **Depends on** — which earlier units must land first (e.g. API contract before frontend
  consumes it).
- **Acceptance criterion** — how anyone (the reviewer, the user) knows it's actually done.

## Ordering rules

1. Contract-defining work (API shapes, shared schemas) comes before the work that consumes
   it.
2. Independent units are marked as such explicitly, so the manager knows what can run in
   parallel.
3. Testing and review land after the implementation they cover, never before.
4. Keep units small enough that each maps to roughly one specialist delegation — a unit
   that needs three specialists to finish is really three units.

## Output format

```
# Plan: <project/feature name>

1. <unit> — owner: <area> — depends on: <none|unit #> — done when: <criterion>
2. ...
```

Definition of ready before handing this to `manager`: every unit has an owner area, its
dependencies are explicit, its acceptance criterion is concrete enough that `code-reviewer`
could check it without asking the user what "done" means, and — if a TRD exists — every
item in its own Definition of Ready checklist is resolved, not left "TBD."

## Handoff

Hand the finished plan to the `manager` agent (or `/build-feature`) unit by unit, or as a
whole plan if the manager should sequence it. Don't hand over goals or a brief directly —
translate them into this format first, that's the point of this skill.
