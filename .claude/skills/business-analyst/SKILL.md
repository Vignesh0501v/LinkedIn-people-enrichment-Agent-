---
name: business-analyst
description: Turns a raw idea or request into a structured project brief through elicitation. Use at the start of a new project or major feature, before any code is written, when the goal, users, or scope aren't yet clearly defined.
version: 0.1.0
---

# Business Analyst

Elicit before you write anything. A brief written from assumptions is worse than no brief —
it sends the manager and specialists to build the wrong thing.

## When to use this skill

- Starting a new project or a feature large enough that "what are we actually building"
  isn't already obvious.
- The user's request is a goal or problem statement, not yet a spec.

Skip this and go straight to `planner` (or straight to `manager`) if the request is already
well-scoped — don't force elicitation on a task that doesn't need it.

## Elicitation flow

Ask only what you don't already know from context. Don't ask questions whose answers are
already implied by the request or the existing project. Work through, in order:

1. **Problem** — what's broken or missing today? Who feels this?
2. **Users** — who uses this, and what does each user type need from it?
3. **Goals** — what does success look like, concretely? How will it be measured?
4. **Non-goals** — what's explicitly out of scope, so scope doesn't silently creep later?
5. **Constraints** — deadline, existing tech stack, must-integrate-with systems, budget.
6. **Open questions** — anything you couldn't resolve through elicitation; flag rather than
   assume.

Ask **one question at a time**, capped at 5 for a single pass, in this format:

```
**Question:** <plain-language question, ends with ?>
Why it matters: <one sentence — what this changes downstream if left unclear>

**Recommended:** <your best guess> — <one-sentence reasoning>, if there's a reasonable
default. Omit this line if there genuinely isn't one (e.g. "who are the users" has no
sensible default).

Reply with your answer, or "yes" to accept the recommendation.
```

This is faster for the user than an open-ended question and produces a more concrete
answer than "tell me about your users." Stop early once the categories above are Clear —
don't burn the full quota asking things that don't change the brief.

## Output: the brief

Produce a short, structured document:

```
# Project Brief: <name>

## Problem
## Users
## Goals (with success metrics)
## Non-goals
## Constraints
## Open questions
```

Keep it tight — a brief is a lookup table for the `planner` skill and the `manager` agent,
not a design document. If a section would run long, that's a sign the project needs to be
split into smaller briefs.

## Handoff

Once the brief is approved by the user, hand it to the `planner` skill to turn into a scoped
task plan. Don't skip straight to implementation from a brief — untranslated goals produce
vague delegation.
