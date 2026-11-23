# AI Interactions & Agent Reasoning Traces

This file documents the **agentic workflow** in the natural-language planner and saves an
example of the agent's intermediate reasoning (not just terminal output).

## The agent workflow

`nl_planner.plan_day` runs a multi-step, decision-making chain over the Groq GPT-OSS 120B
model and the rule-based scheduler:

1. **interpret (LLM call)** — extract structured tasks + available time + confidence.
2. **build + plan** — construct `Owner`/`Pet`/`Task` objects and run `Scheduler.generate_plan()`.
3. **check** — compute conflicts, missed deadlines, skipped tasks.
4. **review (LLM call)** — the model sees the diagnosis and decides **approve** or **revise**;
   a revision loops back to step 2 (up to 2 times).
5. **explain (LLM call)** — summarize the final plan.

Every run records these steps to a markdown trace. In the app and the CLI demo the trace is
written to **`logs/last_run.md`**, so the agent's intermediate reasoning is saved to a file,
not just printed. Each `DayPlan` also carries the trace in `plan.trace`, and
`plan.write_trace(path)` writes it anywhere.

## Example reasoning trace

Captured from the "late-finishing task misses its deadline" scenario (see `evaluate.py`).
It shows the agent extracting the task, planning it, checking it, catching that the grooming
session runs 30 minutes past its 8pm deadline, and the review step before the explanation:

```markdown
# Reasoning trace

**Request:** I have two hours tonight. Groom Mochi starting at 7pm for 90 minutes, and it needs to be done by 8pm.

## Step 1 — interpret (LLM)

- Available: **120 min** · confidence **0.90**
- Assumptions: none

Extracted tasks:
[
  {
    "title": "Grooming",
    "pet": "Mochi",
    "species": "dog",
    "duration_minutes": 90,
    "priority": "medium",
    "time": "19:00",
    "deadline": "20:00",
    "frequency": "daily"
  }
]

## Iteration 1 — build + plan + check

Available time: 120 minutes.

Planned:
  - Grooming for Mochi at 19:00, 90 min, medium priority
Skipped (did not fit in the time budget):
  (none)
Time conflicts:
  (none)
Missed deadlines:
  - Grooming (Mochi) finishes at 20:30, past its 20:00 deadline

## Step 5 — review (LLM)

Decision: **approve**

## Step 6 — explain (LLM)

<the model's plain-English summary of the plan goes here>
```

> The committed example above was produced with the deterministic offline harness so it is
> reproducible without an API key. Running the app or `python nl_planner.py` with a real
> `GROQ_API_KEY` regenerates `logs/last_run.md` with the live model's wording.

## Agent Workflow reflection (stretch SF7)

**What task did I give the agent?** Turn a free-text pet-care request into a workable daily
schedule, and have it check and fix its own plan rather than trusting a single extraction.

**What did it do?** It extracted structured tasks, ran them through the scheduler, inspected
the result (conflicts, missed deadlines, skipped tasks), and either approved or revised the
task list before explaining the outcome.

**What did I verify or fix manually?** I verified the revision loop with scripted tests and
had to add guardrails where the model's JSON didn't match my schema (out-of-vocabulary
priorities, code-fenced JSON). Those boundary fixes are documented in `model_card.md`.
