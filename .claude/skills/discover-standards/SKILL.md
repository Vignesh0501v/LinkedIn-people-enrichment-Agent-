---
name: discover-standards
description: Extracts a project's real coding conventions (patterns not explained by the framework defaults) into small per-topic standard files plus an index. Use once when this system is first wired into a project, and again whenever a new convention emerges worth documenting.
version: 0.1.0
---

# Discover Standards

Specialist agents produce generic, "textbook" code unless they know a project's actual
conventions. This skill captures those conventions once, cheaply, so every future
delegation can reference them instead of specialists guessing or reinventing per task.

## When to use this skill

- First time this repo's agents/skills are wired into a project (see main README).
- Whenever a specialist's or reviewer's output reveals an undocumented convention worth
  making explicit (e.g. `code-reviewer` keeps flagging the same pattern mismatch).

## Process

1. **Pick a focus area.** If not specified, scan the project structure and propose 3-5
   candidate areas (e.g. API routes, database/models, components, auth, testing). Ask the
   user to pick one — don't try to cover the whole codebase in one pass.
2. **Read 5-10 representative files** in that area.
3. **Look for what's unusual, opinionated, or tribal** — a specific choice that could have
   gone differently and that a new contributor (human or agent) wouldn't guess without being
   told. Skip anything that's just "how the framework works."
4. **Confirm with the user** before writing anything: list the candidate conventions found,
   let them pick which to document, add, or skip.
5. **For each confirmed convention**, ask one clarifying question about *why* it exists
   (helps future agents know when the convention applies vs. when it's fine to deviate),
   then draft it and confirm before writing the file.
6. **Write** to `.agents/standards/<topic>/<name>.md` — short, scannable, no prose padding:

   ```
   # <Convention name>

   <The rule, stated directly.>

   - <Exception or edge case, if any>
   - <Common mistake this prevents>
   ```

7. **Update `.agents/standards/index.yml`**:

   ```yaml
   <topic>:
     <name>:
       description: <one line, written for matching against future tasks>
   ```

## Output discipline

Every standard file must be small enough that loading it costs nothing — this is reference
material pulled in on demand (see `inject-standards`), not injected into every session.
If a standard is running long, it's actually two standards, or it's design documentation
that belongs elsewhere, not a standard.
