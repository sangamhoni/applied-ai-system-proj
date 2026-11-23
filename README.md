# 🐾 PawPal+ — Natural-Language Daily Planner (Project 2)

An AI layer on top of my Module 2 PawPal+ scheduler: describe your day in plain
English and it plans it for you, checks its own work, and explains the result.

> **Original project (Modules 1–3): PawPal+.** PawPal+ is a Streamlit app I built to
> help a busy pet owner plan a day of pet care. The original version let you add pets
> and care tasks (walks, feeding, meds, grooming), then built a daily plan that fit the
> owner's available time, put the highest-priority tasks first, warned about overlapping
> times, and automatically brought recurring tasks back. All of that logic lives in
> `pawpal_system.py` and is fully tested — Project 2 reuses it unchanged.

---

## Title and Summary

**PawPal+ Natural-Language Daily Planner.** Instead of entering every task into a form,
you type something like *"I have one hour tonight, feed both pets, give Pichu medication
by 8pm, and walk Mochi if there's time"* and the app turns it into a real schedule. It
matters because the hardest part of staying consistent with pet care isn't the math of
fitting tasks into an hour — the scheduler already does that — it's the friction of
entering everything. Natural language removes that friction, and an **agentic workflow**
(the AI plans, acts, then checks and revises its own plan) makes the result trustworthy
instead of a one-shot guess.

**Advanced AI feature: Agentic Workflow.** The AI extracts structured tasks, hands them
to the deterministic scheduler, inspects conflicts / missed deadlines / skipped tasks,
and then *revises its own task list* and re-plans — up to two revisions — before
explaining the final plan. The extraction and revisions directly drive the schedule;
this is not a standalone script bolted on beside a normal answer.

**Reliability feature (also included):** automated tests with a fake model client,
a model-reported **confidence score**, a reliability gate, input validation / enum
coercion guardrails, and step-by-step logging.

---

## Architecture Overview

See [`diagrams/architecture.mmd`](diagrams/architecture.mmd) for the full diagram.
Data flows **input → process → output** like this:

1. **Input** — the owner's plain-English request enters through the Streamlit UI (`app.py`).
2. **Interpret (LLM)** — Groq's Llama 3.3 extracts structured tasks (pet, duration,
   priority, time, deadline, recurrence), the available minutes, and a confidence score,
   returned as JSON.
3. **Guardrails** — the JSON is validated and coerced (bad enums mapped to valid ones,
   invalid tasks dropped and logged) before anything reaches the scheduler.
4. **Build + Plan** — tasks become `Owner`/`Pet`/`Task` objects and the original
   `Scheduler.generate_plan()` fits the highest-priority ones into the time budget.
5. **Check / Evaluate** — the system computes time conflicts, missed deadlines, skipped
   tasks, and a reliability verdict.
6. **Review (LLM)** — the model sees that diagnosis and either **approves** or **revises**
   the task list; a revision loops back to step 4.
7. **Explain (LLM) → Output** — the model writes a short summary, and a `DayPlan`
   (schedule + warnings + confidence + explanation) is returned to the UI.

**Where humans and tests check the AI** (bottom of the diagram):
- **Automated tests** (`tests/test_nl_planner.py`) verify the agentic loop with a fake
  model client — no API key or network needed.
- **Human review** — I (Sangam Subedi) check outputs against the confidence badge and the
  evaluation table in the Testing Summary below.
- **Logging** (`pawpal.nl_planner`) records every step and every error.

The rule-based core (`pawpal_system.py`) stays deterministic; all AI variability is
confined to the interpret/review/explain steps.

---

## Setup Instructions

```bash
# 1. Clone and enter the project
git clone git@github.com:sangamhoni/applied-ai-system-proj.git
cd applied-ai-system-proj

# 2. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Get a FREE Groq API key (no credit card) at https://console.groq.com/keys
#    then set it in your shell:
export GROQ_API_KEY=your_key_here  # Windows PowerShell: $env:GROQ_API_KEY="your_key_here"
```

Run it:

```bash
streamlit run app.py     # interactive app (manual scheduler + AI planner)
python nl_planner.py     # terminal demo of the AI planner (needs GROQ_API_KEY)
python main.py           # terminal demo of the original scheduler (no key needed)
python -m pytest         # run all 53 tests (no key needed)
```

The manual scheduler, the terminal demo, and the whole test suite work **without** a
key — only the AI planner needs `GROQ_API_KEY`. If the key is missing, the app shows a
clear message instead of crashing.

---

## Sample Interactions

These are representative runs of the AI planner (temperature 0, so they reproduce closely
when you run them with your own key). Each shows the plain-English **input** and the
resulting **output**.

### Example 1 — a tight evening with a medication deadline

**Input:**
> "I have one hour tonight. Feed both pets Mochi and Pichu, give Pichu medication by 8pm,
> and walk Mochi for half an hour if there is enough time."

**Output (schedule):**

| Time | Task | Pet | Minutes | Priority |
|------|------|-----|---------|----------|
| — | Medication | Pichu | 5 | high |
| — | Feed | Mochi | 10 | medium |
| — | Feed | Pichu | 10 | medium |
| — | Walk | Mochi | 30 | low |

**Confidence: 90% · reliability check passed · AI revisions: 0**

**AI explanation:**
> Everything fit in your hour. I scheduled Pichu's medication first since it's
> time-critical and due by 8pm, then feeding for both Mochi and Pichu, and finally
> Mochi's 30-minute walk — which fit, so it made the cut even though it was optional.
> Total: 55 of 60 minutes used, no conflicts and no missed deadlines.

### Example 2 — not enough time (a task gets delayed)

**Input:**
> "I only have 25 minutes this morning. Walk Mochi for 30 minutes, feed Luna, and brush Luna."

**Output:** Walk (30 min) doesn't fit 25 minutes, so the scheduler keeps the shorter tasks.

| Time | Task | Pet | Minutes | Priority |
|------|------|-----|---------|----------|
| — | Feed | Luna | 10 | medium |
| — | Brush | Luna | 10 | low |

**Delayed / skipped:** Walk (Mochi, 30 min) — not enough time left.

**AI explanation:**
> With only 25 minutes I couldn't fit Mochi's 30-minute walk, so I scheduled the two
> quick Luna tasks (feeding and brushing, 20 minutes total) and left the walk for when
> you have more time.

### Example 3 — a missed deadline gets flagged

**Input:**
> "I have 20 minutes. Give Pichu medication by 7pm (takes 10 min), and give Mochi a
> 15-minute bath."

**Output:** both don't fit together; the reliability check warns about the medication deadline.

**⚠️ Reliability check:** a deadline was missed — double-check the plan.
**Missed deadline:** Medication (Pichu) can't be done by its 19:00 deadline — no time left.

**AI explanation:**
> Your 20 minutes can't cover both a 15-minute bath and Pichu's 10-minute medication, and
> the medication has a hard 7pm deadline, so I flagged it. I'd prioritise the medication
> and move Mochi's bath to later.

---

## Design Decisions

- **Reuse the rule-based scheduler; add AI only as a language + judgment layer.** The
  original `Scheduler` is deterministic and fully tested, so I kept all the "which tasks
  fit" math there and used the model only for what it's genuinely good at: understanding
  messy English and reviewing a result. *Trade-off:* the AI can't change the scheduling
  algorithm, only the task list it's given — which I consider a feature (predictable core).
- **Agentic loop instead of one extraction call.** The review-and-revise step lets the
  model catch cases like "a high-priority med got skipped because a low-priority task ate
  the budget." *Trade-off:* extra API calls (up to two more) and a bit more latency, in
  exchange for noticeably better plans on tight-budget inputs.
- **Groq free tier over a paid API.** I originally had this on a paid API; I switched the
  whole backend to Groq's free Llama 3.3 so it costs nothing and anyone can run it.
  *Trade-off:* I use JSON mode + my own validation/coercion instead of a provider's
  guaranteed strict schema, so the guardrails in `_normalize_task` / `_parse_json` are
  doing real work.
- **Confidence + reliability gate instead of silent trust.** The model self-reports
  confidence, and `DayPlan.is_reliable()` combines it with "did every task validate" and
  "any missed deadlines." *Trade-off:* self-reported confidence isn't ground truth, but
  paired with the deterministic deadline/validation checks it's a useful signal.
- **Deadlines added in the AI layer, not the core.** The original `Task` has no intraday
  deadline, so I track deadlines in `nl_planner` and check them against the plan, leaving
  `pawpal_system.py` and its 28 tests untouched.

---

## Testing Summary

**Automated tests:** `python -m pytest` → **53 passed** (32 for the original scheduler in
`tests/test_pawpal.py`, 21 for the AI layer in `tests/test_nl_planner.py`). The AI-layer
tests use a **fake Groq client** that scripts the interpret → review → explain responses,
so the full agentic loop — including a revision that changes the plan — is tested with no
key and no network.

**What worked:** task extraction → scheduling → deadline checking is solid, and the
revision loop correctly swaps in a better task list when I script the model to revise.
Enum coercion and JSON-fence stripping are covered by their own tests.

**What didn't / what I learned:** the fragile part was never the scheduling — it was the
boundary where model text becomes program data. Before guardrails, `"priority": "urgent"`
and ```` ```json ```` fences crashed the parser. I learned to validate and coerce every
field, and to test those failure paths explicitly. The model also can't reason about
time it isn't given (vague phrasing → untimed tasks), which I accept rather than hide.

**Human evaluation** (scenarios I ran and judged, in a parseable table):

| Test input | Evaluation criteria | Result |
|------------|--------------------|--------|
| "One hour, feed both pets, meds by 8pm, walk if time" | All tasks fit, med scheduled, no missed deadline | Pass |
| "Only 25 min: 30-min walk, feed Luna, brush Luna" | Walk delayed, shorter tasks kept, clear explanation | Pass |
| "20 min: 10-min meds by 7pm + 15-min bath" | Flags the missed medication deadline | Pass |
| Empty request | Handled gracefully, no crash | Pass (raises a clear `PlannerError`) |
| Model returns `"priority": "urgent"` | Coerced to a valid priority, not dropped | Pass |
| Model wraps JSON in ```` ``` ```` fences | Parsed without crashing | Pass |
| No `GROQ_API_KEY` set | Clear message, no crash | Pass |

**Reliability note:** 53/53 automated tests pass; the AI struggles when the request gives
no clock time (those tasks stay untimed and skip conflict/deadline checks). Confidence
scores on the examples above averaged ~0.9; the deterministic scheduler and deadline
checks make the final verdict reliable even when the model's wording varies.

---

## Reflection

Building this taught me that the interesting engineering in an AI feature isn't the model
call — it's everything around it: deciding what the AI should *not* do (I kept it away from
the scheduling math and medication dosages), putting guardrails exactly where text turns
into data, and defining what "reliable enough" means so the system can flag its own weak
answers. The agentic review loop also changed how I think about AI output — letting the
model check its own work caught mistakes a single call would have shipped.

> My graded responsible-AI reflection — how I collaborated with AI, one suggestion I
> accepted and one I rejected/revised, limitations, and misuse prevention — is in
> [`model_card.md`](model_card.md).

---

## Project Structure

| File | What it is |
|------|------------|
| `pawpal_system.py` | Original logic layer: `Owner`, `Pet`, `Task`, `Scheduler` (unchanged) |
| `nl_planner.py` | **AI layer:** Groq extraction + the agentic plan/check/revise workflow |
| `app.py` | Streamlit UI (manual scheduler **and** the AI planner) |
| `main.py` | Terminal demo of the original scheduler |
| `tests/test_pawpal.py` | 32 tests for the scheduler |
| `tests/test_nl_planner.py` | 21 tests for the AI layer (offline, fake model client) |
| `diagrams/architecture.mmd` | System architecture diagram (Mermaid source) |
| `diagrams/uml_final.mmd` | UML class diagram of the logic layer (Mermaid source) |
| `assets/` | Where architecture images are exported for embedding |
| `model_card.md` | Responsible-AI reflection (limitations, misuse, collaboration) |

**Tested by:** Sangam Subedi
