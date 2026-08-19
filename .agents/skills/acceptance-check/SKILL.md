---
name: acceptance-check
description: Verifies completed work against the project's brief (success metrics) and TRD (Definition of Ready), not just code correctness. Use as the final gate before manager reports a task done, whenever a brief and/or TRD exist for the work.
version: 0.1.0
---

# Acceptance Check

`code-reviewer` checks whether the code is correct. This skill checks whether it's the
**right** code — the thing the brief actually asked for. The two catch different failure
modes; skipping this one lets a technically-clean implementation of the wrong thing through.

## When to use this skill

- Before `manager` reports any task done, if a `business-analyst` brief and/or `trd-writer`
  TRD exist for the work.
- Not needed for small tasks that never went through brief/TRD — `code-reviewer` alone is
  the gate there.

## Process

1. Re-read the brief's **Goals** (with success metrics) and **Non-goals**.
2. Re-read the TRD's **Definition of Ready** checklist, if one exists.
3. For each goal/metric, check concretely — don't rubber-stamp:
   - Is there something a user could do, right now, that satisfies this goal?
   - If the metric is measurable (e.g. "loads in under 2s"), was it actually checked, not
     assumed?
4. Check the **Non-goals** weren't quietly built anyway — scope creep is a quality defect
   too, not just a nice-to-have avoided.
5. Check every unchecked item in the TRD's Definition of Ready — if still unresolved, that's
   a finding, not something to silently wave through.

## Reporting

- If everything checks out: say so plainly, don't manufacture caveats.
- If something's missing: name the specific goal/criterion, what's missing, and which
  specialist's file it likely belongs to — `manager` routes it back from there.
- Never mark "accepted" because the code looks reasonable — only because the specific goal
  it was meant to satisfy is demonstrably met.
