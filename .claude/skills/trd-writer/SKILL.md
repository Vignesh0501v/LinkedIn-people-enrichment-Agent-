---
name: trd-writer
description: Turns an approved project brief into a Technical Requirements Document (TRD) — tech stack, database, hosting, auth, integrations, non-functional requirements — by asking a small number of targeted technical questions. Use right after business-analyst produces an approved brief, before planner breaks work into tasks.
version: 0.1.0
---

# TRD Writer

The brief says *what* to build. The TRD says *how* — the technical decisions that every
specialist downstream will build against. Wrong or missing technical decisions cost far
more to unwind after code exists than to nail down now.

## When to use this skill

- Right after `business-analyst` produces an approved brief, for any project or feature
  substantial enough to need real technical decisions (new service, new data store, a
  stack choice that isn't already fixed by the existing project).
- Skip this for a small feature inside an already-established codebase where the stack,
  database, and conventions are all already decided — go straight to `planner`.

## Step 1: check what's already decided

Before asking anything, check `.agents/standards/index.yml` (see `discover-standards` /
`inject-standards`) and the existing project structure. If this is a brownfield project,
most technical questions are already answered by what's there — don't ask the user to
re-decide their own stack. Only ask about genuinely new decisions this feature introduces.

## Step 2: ask, don't assume — but recommend

For each open technical decision, ask **one question at a time**, in this format:

```
**Question:** <plain-language question, ends with ?>
Why it matters: <one sentence — what breaks or gets expensive to change if this is wrong>

**Recommended:** <option> — <one-sentence reasoning>

| Option | Description |
|---|---|
| A | ... |
| B | ... |
| C | ... |

Reply with a letter, "yes" to accept the recommendation, or your own answer.
```

Cap at **5 questions** for a single TRD pass. Prioritize by impact: a wrong database choice
is expensive to reverse; a wrong linter config isn't — ask about the former, default the
latter silently. Cover, in priority order, whatever is still undecided:

1. **Stack** — language/framework (only if not already fixed by the project)
2. **Data** — database/storage choice, and the core entities' rough shape
3. **Auth** — how users/services authenticate, if the feature touches access control
4. **Integrations** — external services/APIs this depends on
5. **Non-functional** — the one or two constraints that actually matter here (expected
   scale, latency target, offline support) — skip generic boilerplate NFRs nobody asked for

Never ask about decisions that don't materially change the plan or the code — that's
scope creep in the elicitation itself.

## Output: the TRD

```
# TRD: <project/feature name>

## Stack
## Data model (entities, key relationships — sketch, not a full schema)
## API/contract shape (if applicable)
## Auth & access control
## External integrations
## Non-functional constraints
## Definition of Ready
  - [ ] Every open technical decision above is resolved, not "TBD"
  - [ ] Data model covers every entity the brief's user stories touch
  - [ ] Any breaking constraint (must integrate with X, must run on Y) is stated explicitly
```

Keep it a decision record, not a design document — enough for `planner` and `manager` to
delegate without re-litigating these choices per specialist.

## Handoff

Once approved, hand the TRD to `planner` alongside the brief. `planner` turns brief + TRD
into the ordered task list; `manager` reads both before delegating so every specialist
brief already carries the technical decisions it needs, instead of re-deciding them.
