---
name: inject-standards
description: Loads only the project standards relevant to the current task into context, using the index built by discover-standards. Use before delegating a specialist task, or any time work should match a project's actual conventions instead of generic defaults.
version: 0.1.0
---

# Inject Standards

Progressive disclosure for conventions: read the index, pull in only what's relevant to the
task at hand. Never load the whole standards set — that defeats the point of keeping them
small and topic-scoped.

## When to use this skill

- `manager`, before writing a specialist's brief — pull in whatever standards match that
  specialist's owned area, and fold the relevant lines directly into the brief.
- Anyone, on demand, when about to work in an area that might have documented conventions.

## Process

1. Read `.agents/standards/index.yml`. If it doesn't exist, skip silently — the project has
   no documented standards yet; suggest `discover-standards` if the gap seems worth closing,
   but don't block on it.
2. Match the task's area/keywords against the index's descriptions.
3. Read only the matched standard files — not the whole `standards/` tree.
4. Fold their content directly into the current task/brief as short, direct rules, not as a
   file reference the specialist has to go look up separately (specialists start with no
   memory of this session; a pointer they can't act on is wasted).

## When there's no match

If nothing in the index matches, say so and proceed without forcing a fit — a convention
that doesn't apply here shouldn't be pasted in anyway.
