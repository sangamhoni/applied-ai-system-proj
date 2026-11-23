"""Natural-language daily planner for PawPal+ (Project 2).

Turns a plain-English request like

    "I have one hour tonight. Feed both pets, give Pichu medication,
     and walk Mochi if there is enough time."

into a real daily schedule, by driving the existing rule-based Scheduler with an
AI extraction layer. The flow is an agentic workflow — the AI plans, acts, and
checks its own work:

    1. interpret  - the LLM reads the request and extracts structured tasks
                    (task, pet, duration, priority, time, deadline, recurrence)
                    plus available minutes, with a self-reported confidence score.
    2. build      - those become Owner / Pet / Task objects.
    3. plan       - Scheduler.generate_plan() fits the highest-priority tasks
                    into the available time.
    4. check      - we look for time conflicts, missed deadlines, and skipped tasks.
    5. review     - the LLM inspects the result and either approves it or revises
                    the task list (steps 2-4 then run again).
    6. explain    - the LLM writes a short summary of what was scheduled, delayed,
                    or skipped.

The model runs on Groq's free API (openai/gpt-oss-120b, configurable via GROQ_MODEL). The rule-based core in
``pawpal_system.py`` is reused unchanged — this module only adds the language
layer and the self-correction loop. The SDK is imported lazily so the pure
helpers below stay importable and testable without the ``groq`` package or a key.

Guardrails: every model response is validated and coerced before it touches the
scheduler; malformed JSON, out-of-range enums, and invalid tasks are logged and
handled instead of crashing. Everything the planner does is logged through the
``pawpal.nl_planner`` logger.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime

from pawpal_system import Owner, Pet, Scheduler, Task

logger = logging.getLogger("pawpal.nl_planner")
logger.addHandler(logging.NullHandler())  # library-safe default; app/CLI configure output

# Free model on Groq's API. Groq's catalog changes over time, so this is overridable
# via the GROQ_MODEL env var. List what your key can access with:
#   python -c "from groq import Groq; [print(m.id) for m in Groq().models.list().data]"
MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")

# How many times the model may revise the task list before we settle for the last plan.
MAX_REVISIONS = 2

# Allowed enum values, kept in sync with pawpal_system.
_SPECIES = ("dog", "cat", "other")
_PRIORITIES = ("low", "medium", "high")
_FREQUENCIES = ("once", "daily", "weekly")

# Map common model word-choices onto our fixed enums (a guardrail against drift).
_PRIORITY_SYNONYMS = {
    "urgent": "high", "critical": "high", "important": "high", "high": "high",
    "normal": "medium", "medium": "medium", "med": "medium", "moderate": "medium",
    "optional": "low", "whenever": "low", "low": "low", "nice to have": "low",
}


class PlannerError(RuntimeError):
    """Raised when the AI planner cannot run (missing key, no network, bad model output)."""


_EXTRACT_SYSTEM = (
    "You extract structured pet-care tasks from a pet owner's plain-English request, "
    "for a day planner. Respond with a SINGLE json object and nothing else.\n\n"
    "Shape:\n"
    "{\n"
    '  "available_minutes": <integer total minutes available>,\n'
    '  "confidence": <number 0..1, how confident you are in this extraction>,\n'
    '  "assumptions": [<short strings describing anything you inferred>],\n'
    '  "tasks": [\n'
    "    {\n"
    '      "title": <short task name>,\n'
    '      "pet": <pet or person the task is for>,\n'
    '      "species": "dog" | "cat" | "other",\n'
    '      "duration_minutes": <integer minutes, > 0>,\n'
    '      "priority": "low" | "medium" | "high",\n'
    '      "time": <"HH:MM" 24-hour start time, or null>,\n'
    '      "deadline": <"HH:MM" 24-hour time it must be done by, or null>,\n'
    '      "frequency": "once" | "daily" | "weekly"\n'
    "    }\n"
    "  ]\n"
    "}\n\n"
    "Rules: infer sensible durations and priorities. Medication and time-critical care are "
    "high priority; walks and feeding are usually medium; enrichment and grooming are low. "
    "A task the owner says to do only 'if there is time' is low priority. Set 'time' only if a "
    "start time is given or implied, and 'deadline' only if the owner says something must happen "
    "'by' a time. Default frequency to 'daily'. If the request does not state how much time is "
    "available, use the provided default."
)

_REVIEW_SYSTEM = (
    "You are reviewing a pet-care schedule that a rule-based planner produced from your "
    "extracted tasks. Respond with a SINGLE json object and nothing else.\n\n"
    "Shape:\n"
    "{\n"
    '  "decision": "approve" | "revise",\n'
    '  "reason": <one sentence>,\n'
    '  "confidence": <number 0..1>,\n'
    '  "revised_tasks": <null when approving, or the COMPLETE replacement task list '
    "using the same task shape as before>\n"
    "}\n\n"
    "Approve if the plan is a reasonable use of the available time and nothing important was "
    "dropped. Revise only when a change would clearly help: a time-critical task was skipped but "
    "could fit if a lower-value task were shortened or dropped; a deadline is missed and moving "
    "the task's time would fix it; or two tasks conflict and one can be shifted. When revising, "
    "return the full task list, keep durations realistic, and never invent tasks the owner did "
    "not mention."
)

_EXPLAIN_SYSTEM = (
    "You explain a finished pet-care schedule to the owner in a warm, concise way. "
    "Cover what was scheduled, what was delayed or skipped and why (usually not enough time), "
    "and call out any time conflicts or missed deadlines. Two short paragraphs at most. "
    "Do not invent tasks or times that are not in the data. Plain text, no markdown headings."
)


# --- Result type ---

@dataclass
class DayPlan:
    """Everything the UI needs to show the result of one natural-language request."""

    request: str
    available_minutes: int
    planned: list[Task]
    skipped: list[Task]
    conflicts: list[str]
    missed_deadlines: list[str]
    invalid: list[str]
    explanation: str
    tasks: list[dict]     # the final structured task list the model settled on
    confidence: float     # the model's self-reported extraction confidence (0..1)
    assumptions: list[str]
    iterations: int
    trace: list[str] = field(default_factory=list)  # human-readable step-by-step reasoning trace

    def used_minutes(self) -> int:
        """Return the total minutes the planned tasks take."""
        return sum(task.duration_minutes for task in self.planned)

    def is_reliable(self) -> bool:
        """Simple reliability gate: confident, understood every task, no missed deadlines."""
        return self.confidence >= 0.6 and not self.invalid and not self.missed_deadlines

    def write_trace(self, path: str) -> str:
        """Write the agentic reasoning trace to a markdown file; return the path."""
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n\n".join(self.trace) + "\n")
        logger.info("wrote reasoning trace to %s", path)
        return path


# --- Pure helpers (no model needed; covered by tests) ---

def _to_minutes(hhmm: str | None) -> int | None:
    """Turn a 'HH:MM' string into minutes after midnight, or None if blank/invalid."""
    if not hhmm:
        return None
    try:
        parsed = datetime.strptime(str(hhmm).strip(), "%H:%M")
    except ValueError:
        return None
    return parsed.hour * 60 + parsed.minute


def _minutes_to_hhmm(minutes: int) -> str:
    """Format minutes-after-midnight back into 'HH:MM'."""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _normalize_task(item: dict) -> dict:
    """Coerce a model-produced task's enum fields onto our fixed vocabularies (a guardrail)."""
    out = dict(item)
    priority = str(item.get("priority", "medium")).strip().lower()
    out["priority"] = _PRIORITY_SYNONYMS.get(priority, priority if priority in _PRIORITIES else "medium")
    frequency = str(item.get("frequency", "daily")).strip().lower()
    out["frequency"] = frequency if frequency in _FREQUENCIES else "daily"
    species = str(item.get("species", "other")).strip().lower()
    out["species"] = species if species in _SPECIES else "other"
    return out


def build_scheduler(
    owner_name: str, available_minutes: int, tasks: list[dict]
) -> tuple[Scheduler, list[tuple[Task, int | None]], list[str]]:
    """Build an Owner (with pets and tasks) and a Scheduler from extracted task dicts.

    Returns the scheduler, a list of (task, deadline_in_minutes) pairs for deadline
    checking, and a list of human-readable messages for any tasks that failed validation.
    """
    owner = Owner(owner_name or "You", max(0, int(available_minutes or 0)))
    pets: dict[str, Pet] = {}
    built: list[tuple[Task, int | None]] = []
    invalid: list[str] = []

    for raw in tasks:
        item = _normalize_task(raw)
        pet_name = (item.get("pet") or "Pet").strip() or "Pet"
        if pet_name not in pets:
            pet = Pet(pet_name, item["species"])
            owner.add_pet(pet)
            pets[pet_name] = pet
        try:
            task = Task(
                title=str(item["title"]),
                duration_minutes=int(item["duration_minutes"]),
                priority=item["priority"],
                time=(item.get("time") or ""),
                frequency=item["frequency"],
            )
        except (ValueError, KeyError, TypeError) as error:
            note = f"{item.get('title', '(untitled)')}: {error}"
            invalid.append(note)
            logger.warning("dropped invalid task: %s", note)
            continue
        pets[pet_name].add_task(task)
        built.append((task, _to_minutes(item.get("deadline"))))

    return Scheduler(owner), built, invalid


def find_missed_deadlines(scheduler: Scheduler, built: list[tuple[Task, int | None]]) -> list[str]:
    """Return a warning for each task with a deadline that the plan does not meet.

    A deadline is missed when the task was skipped entirely, or when it is scheduled at
    a time whose finish runs past the deadline.
    """
    skipped = scheduler.skipped_tasks
    missed: list[str] = []
    for task, deadline in built:
        if deadline is None:
            continue
        due = _minutes_to_hhmm(deadline)
        if task in skipped:
            missed.append(
                f"{task.title} ({task.pet_name}) can't be done by its {due} deadline — no time left"
            )
        elif task.time:
            finish = task.start_minutes() + task.duration_minutes
            if finish > deadline:
                missed.append(
                    f"{task.title} ({task.pet_name}) finishes at {_minutes_to_hhmm(finish)}, "
                    f"past its {due} deadline"
                )
    return missed


def _diagnostics_text(
    scheduler: Scheduler, missed: list[str], invalid: list[str], available: int
) -> str:
    """Render the plan outcome as plain text for the model to review or explain."""
    lines = [f"Available time: {available} minutes.", ""]

    lines.append("Planned:")
    if scheduler.planned_tasks:
        for task in scheduler.sort_by_time(scheduler.planned_tasks):
            when = task.time or "any time"
            lines.append(f"  - {task.title} for {task.pet_name} at {when}, "
                         f"{task.duration_minutes} min, {task.priority} priority")
    else:
        lines.append("  (nothing)")

    lines.append("Skipped (did not fit in the time budget):")
    if scheduler.skipped_tasks:
        for task in scheduler.skipped_tasks:
            lines.append(f"  - {task.title} for {task.pet_name}, "
                         f"{task.duration_minutes} min, {task.priority} priority")
    else:
        lines.append("  (none)")

    lines.append("Time conflicts:")
    lines.extend(f"  - {warning}" for warning in scheduler.conflicts)
    if not scheduler.conflicts:
        lines.append("  (none)")

    lines.append("Missed deadlines:")
    lines.extend(f"  - {warning}" for warning in missed)
    if not missed:
        lines.append("  (none)")

    if invalid:
        lines.append("Ignored (could not be understood):")
        lines.extend(f"  - {note}" for note in invalid)

    return "\n".join(lines)


# --- Model-backed steps ---

def _client(client=None):
    """Return a Groq client, importing the SDK lazily and mapping failures to PlannerError."""
    if client is not None:
        return client
    try:
        from groq import Groq
    except ImportError as error:  # pragma: no cover - depends on environment
        raise PlannerError("The 'groq' package is not installed. Run: pip install groq") from error
    if not os.environ.get("GROQ_API_KEY"):
        raise PlannerError(
            "GROQ_API_KEY is not set. Get a free key at https://console.groq.com "
            "and run: export GROQ_API_KEY=your_key"
        )
    try:
        return Groq()
    except Exception as error:  # pragma: no cover - credential/config issues
        raise PlannerError(f"Could not create the Groq client: {error}") from error


def _chat(client, system: str, user: str, *, json_mode: bool, max_tokens: int = 2000) -> str:
    """Send one chat request and return the model's text, mapping API errors to PlannerError."""
    kwargs = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0,  # deterministic — same request gives the same plan
        "max_tokens": max_tokens,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    try:
        response = client.chat.completions.create(**kwargs)
    except Exception as error:  # pragma: no cover - network/API errors
        logger.error("model call failed: %s", error)
        raise PlannerError(f"The AI request failed: {error}") from error
    return response.choices[0].message.content or ""


def _parse_json(text: str) -> dict:
    """Parse a JSON object from the model, tolerating accidental code fences."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned[cleaned.find("{"):]
    try:
        data = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError) as error:
        logger.error("model returned non-JSON output: %r", text[:200])
        raise PlannerError("The AI returned something that wasn't valid JSON.") from error
    if not isinstance(data, dict):
        raise PlannerError("The AI returned JSON that wasn't an object.")
    return data


def interpret(request: str, default_minutes: int, client=None) -> tuple[int, list[dict], float, list[str]]:
    """Step 1: extract available minutes, structured tasks, confidence, and assumptions."""
    client = _client(client)
    logger.info("interpreting request (%d chars)", len(request))
    user = (
        f"Default available minutes if the request does not say: {default_minutes}\n\n"
        f"Request:\n{request}"
    )
    data = _parse_json(_chat(client, _EXTRACT_SYSTEM, user, json_mode=True))
    available = int(data.get("available_minutes") or default_minutes)
    tasks = data.get("tasks") or []
    confidence = float(data.get("confidence") or 0.0)
    assumptions = [str(a) for a in (data.get("assumptions") or [])]
    logger.info("extracted %d task(s), available=%d min, confidence=%.2f",
                len(tasks), available, confidence)
    return available, tasks, confidence, assumptions


def review(request: str, tasks: list[dict], diagnostics: str, client=None) -> tuple[str, list[dict] | None]:
    """Step 5: approve the plan or return a revised task list."""
    client = _client(client)
    user = (
        f"Original request:\n{request}\n\n"
        f"Tasks you extracted:\n{json.dumps(tasks)}\n\n"
        f"What the planner produced:\n{diagnostics}\n\n"
        "Approve this plan, or revise the task list to improve it."
    )
    data = _parse_json(_chat(client, _REVIEW_SYSTEM, user, json_mode=True))
    decision = str(data.get("decision", "approve")).strip().lower()
    revised = data.get("revised_tasks")
    logger.info("review decision=%s (%s)", decision, data.get("reason", ""))
    return decision, revised


def explain(request: str, diagnostics: str, client=None) -> str:
    """Step 6: a short, friendly explanation of the final plan."""
    client = _client(client)
    user = f"Original request:\n{request}\n\nFinal plan:\n{diagnostics}\n\nExplain it to the owner."
    text = _chat(client, _EXPLAIN_SYSTEM, user, json_mode=False, max_tokens=1024).strip()
    logger.info("generated explanation (%d chars)", len(text))
    return text


# --- Orchestration ---

def plan_day(
    request: str,
    *,
    owner_name: str = "You",
    default_minutes: int = 60,
    max_revisions: int = MAX_REVISIONS,
    client=None,
) -> DayPlan:
    """Run the full interpret -> build -> plan -> check -> review -> explain workflow.

    Returns a DayPlan describing the final schedule. Raises PlannerError if the AI steps
    cannot run (missing key, no network, malformed output, etc.).
    """
    if not request.strip():
        raise PlannerError("Please describe what you need to get done.")

    client = _client(client)
    trace: list[str] = [f"# Reasoning trace\n\n**Request:** {request}"]

    available, tasks, confidence, assumptions = interpret(request, default_minutes, client)
    trace.append(
        f"## Step 1 — interpret (LLM)\n\n"
        f"- Available: **{available} min** · confidence **{confidence:.2f}**\n"
        f"- Assumptions: {assumptions or 'none'}\n\n"
        f"Extracted tasks:\n```json\n{json.dumps(tasks, indent=2)}\n```"
    )

    scheduler: Scheduler | None = None
    missed: list[str] = []
    invalid: list[str] = []
    iterations = 0

    for step in range(max(1, max_revisions + 1)):
        iterations = step + 1
        scheduler, built, invalid = build_scheduler(owner_name, available, tasks)
        scheduler.generate_plan()
        missed = find_missed_deadlines(scheduler, built)
        logger.info("iteration %d: planned=%d skipped=%d conflicts=%d missed=%d invalid=%d",
                    iterations, len(scheduler.planned_tasks), len(scheduler.skipped_tasks),
                    len(scheduler.conflicts), len(missed), len(invalid))
        diagnostics = _diagnostics_text(scheduler, missed, invalid, available)
        trace.append(f"## Iteration {iterations} — build + plan + check\n\n```\n{diagnostics}\n```")

        if step == max_revisions:
            trace.append("## Step 5 — review\n\nRevision budget exhausted; keeping this plan.")
            break  # out of revisions; keep this plan
        decision, revised = review(request, tasks, diagnostics, client)
        trace.append(f"## Step 5 — review (LLM)\n\nDecision: **{decision}**")
        if decision != "revise" or not revised:
            break
        tasks = revised  # loop again with the revised task list

    diagnostics = _diagnostics_text(scheduler, missed, invalid, available)
    explanation = explain(request, diagnostics, client)
    trace.append(f"## Step 6 — explain (LLM)\n\n{explanation}")

    plan = DayPlan(
        request=request,
        available_minutes=available,
        planned=list(scheduler.planned_tasks),
        skipped=list(scheduler.skipped_tasks),
        conflicts=list(scheduler.conflicts),
        missed_deadlines=missed,
        invalid=invalid,
        explanation=explanation,
        tasks=tasks,
        confidence=confidence,
        assumptions=assumptions,
        iterations=iterations,
        trace=trace,
    )
    logger.info("plan complete: %d planned, %d skipped, reliable=%s",
                len(plan.planned), len(plan.skipped), plan.is_reliable())
    return plan


def _demo() -> None:  # pragma: no cover - manual demo, needs a Groq key
    """Run the example request from the command line (requires GROQ_API_KEY)."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    request = (
        "I have one hour tonight. Feed both pets Mochi and Pichu, give Pichu medication "
        "by 8pm, and walk Mochi for half an hour if there is enough time."
    )
    print(f"\nRequest:\n  {request}\n")
    try:
        plan = plan_day(request, owner_name="Sangam", default_minutes=60)
    except PlannerError as error:
        print(f"Could not build a plan: {error}")
        return

    print(f"\nAvailable: {plan.available_minutes} min | used: {plan.used_minutes()} min "
          f"| confidence: {plan.confidence:.2f} | AI revisions: {plan.iterations - 1}\n")
    print("Planned:")
    for task in plan.planned:
        print(f"  {task.time or '--:--'}  {task.title} ({task.pet_name}, {task.duration_minutes} min)")
    for warning in plan.conflicts:
        print(f"  conflict: {warning}")
    for warning in plan.missed_deadlines:
        print(f"  deadline: {warning}")
    print(f"\n{plan.explanation}\n")
    plan.write_trace("logs/last_run.md")
    print("Reasoning trace written to logs/last_run.md")


if __name__ == "__main__":
    _demo()
