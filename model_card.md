# Model Card — PawPal+ Natural-Language Daily Planner

**Author:** Sangam Subedi
**System:** A natural-language pet-care day planner that uses Groq's Llama 3.3 (70B)
to turn a plain-English request into structured tasks, then drives a rule-based
scheduler and lets the model review and revise its own plan.
**Model used:** `llama-3.3-70b-versatile` via the Groq API (free tier), temperature 0.

---

## What the system is for

I give it a sentence like *"I have one hour tonight, feed both pets, give Pichu
medication by 8pm, and walk Mochi if there's time."* It extracts each task (pet,
duration, priority, time, deadline, recurrence), fits them into the available time
with my existing scheduler, checks for conflicts and missed deadlines, revises if
needed, and explains what it scheduled or skipped. The AI does the language
understanding and the self-correction; the scheduler does the deterministic math.

---

## Limitations and biases

- **The model estimates missing facts.** When I don't give a duration or priority,
  the model guesses (e.g. "medication = high priority, 5 minutes"). Those guesses
  come from training-data norms, so they reflect a fairly Western, dog-and-cat view
  of pet care. A task it hasn't seen framed that way may get a poor duration or
  priority. I surface every guess in the "assumptions" list and a confidence score
  so the owner can catch bad inferences instead of trusting them blindly.
- **Time parsing is only as good as the prompt.** Vague phrasing ("later",
  "after work") has no clock time, so those tasks become untimed and skip conflict
  and deadline checks. The system doesn't invent a time — it just can't reason about
  one that isn't there.
- **No real-world awareness.** It doesn't know travel time between a walk and a vet
  visit, or that two pets can't be fed at the exact same minute by one person. It
  flags overlapping *times* as conflicts but treats durations as the only cost.
- **Non-determinism is bounded, not eliminated.** I run at temperature 0 for
  consistency, but the model can still word things differently or occasionally
  mis-extract. The rule-based scheduler is fully deterministic; the variability
  lives entirely in the extraction and explanation steps.
- **English-only, single day.** It plans one day at a time and assumes English input.

## Could it be misused, and how I prevent that

The planner only produces a pet-care to-do list, so the misuse surface is small, but
a few things matter:

- **Over-trusting it for health-critical timing.** Someone could treat a medication
  schedule it produced as medically authoritative. It is not — it is a reminder tool.
  I keep the model away from dosages entirely (it only schedules *when*, never *how
  much*), show a confidence score, and flag missed medication deadlines loudly so a
  wrong or skipped time is visible rather than silent.
- **Prompt-injected or junk input.** A user could paste a huge or adversarial string.
  I cap `max_tokens`, validate and coerce every field the model returns, drop tasks
  that fail validation (with a logged reason), and never `exec`/`eval` model output —
  it can only ever become a `Task` object inside the scheduler.
- **Cost / abuse.** The free Groq tier is rate-limited, and the key is read from an
  environment variable, never committed. There is no write access to anything outside
  the in-memory schedule.

## What surprised me while testing reliability

Two things. First, how much the **self-review loop actually helped**: when I
intentionally fed a request where a low-priority task hogged the time budget and a
medication got skipped, the model's review step noticed the skipped high-priority
task and revised the list so it fit — without me writing any rule for that case. The
agentic loop caught something my greedy scheduler couldn't.

Second, how **fragile raw model JSON can be** before guardrails. Early on, the model
sometimes returned `"priority": "urgent"` or wrapped the JSON in ```` ```json ````
fences, which crashed my parser. Testing with a fake client forced me to add enum
coercion and fence-stripping, and my automated tests for those paths now fail loudly
if I ever remove them. The lesson: the interesting bugs weren't in the AI's
reasoning, they were at the boundary where its text becomes my program's data.

## Collaboration with AI during this project

I built this with an AI coding assistant (Claude Code). It scaffolded the agentic
pipeline, the Groq integration, the tests, and this write-up; I directed the design
decisions and corrected it where it went wrong.

- **A suggestion I accepted:** structuring the feature as an *agentic workflow* with a
  distinct review-and-revise step, rather than a single extraction call. I accepted it
  because it directly satisfies the "plan, act, and check its own work" requirement and
  genuinely improved results on tight-time-budget cases (see above). It's the backbone
  of the system.

- **A suggestion I did not accept as written:** the assistant first implemented the
  whole thing on Anthropic's Claude API. I rejected that — not because it was wrong, but
  because the Anthropic API has no free tier and I won't pay for a class project. I had
  it switch the entire backend to Groq's free Llama 3.3 instead, which meant moving from
  Anthropic's "strict tool use" to Groq's JSON mode plus my own validation and coercion.
  That trade-off cost me a little robustness (I validate the schema myself now instead of
  the API guaranteeing it) in exchange for being genuinely free and reproducible for
  anyone grading it. I also declined an optional offline rule-based fallback extractor it
  proposed — it was out of scope and added a second, lower-quality code path I'd have to
  maintain and test, so I kept one clear AI path plus honest error handling instead.

What I learned: the AI is excellent at producing a working first draft fast, but the
judgment calls — cost, scope, where to put the guardrails, what "reliable enough" means
— were mine to make, and those are what actually shaped the system.
