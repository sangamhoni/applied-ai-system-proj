"""Tests for the natural-language planner's logic (no network / API key needed).

The model-backed steps (interpret / review / explain) are exercised with a tiny fake
Groq client so the agentic loop can be tested offline. The helpers that build tasks
and find missed deadlines are tested directly.
"""

import json

import pytest

import nl_planner
from nl_planner import (
    DayPlan,
    PlannerError,
    build_scheduler,
    find_missed_deadlines,
    plan_day,
)


def _task(title, pet, minutes, priority, *, time=None, deadline=None, species="dog", frequency="daily"):
    return {
        "title": title, "pet": pet, "species": species, "duration_minutes": minutes,
        "priority": priority, "time": time, "deadline": deadline, "frequency": frequency,
    }


# --- _to_minutes ---

@pytest.mark.parametrize(
    "value, expected",
    [("08:00", 480), ("7:45", 465), ("00:00", 0), ("23:59", 1439), ("", None), (None, None), ("nope", None)],
)
def test_to_minutes(value, expected):
    assert nl_planner._to_minutes(value) == expected


# --- enum coercion (a guardrail) ---

def test_normalize_task_coerces_synonyms_and_bad_values():
    out = nl_planner._normalize_task(
        {"priority": "URGENT", "frequency": "monthly", "species": "dragon"}
    )
    assert out["priority"] == "high"      # "urgent" -> high
    assert out["frequency"] == "daily"    # unknown -> default
    assert out["species"] == "other"      # unknown -> other


# --- build_scheduler ---

def test_build_scheduler_creates_pets_and_tasks():
    tasks = [
        _task("Walk", "Mochi", 30, "high", time="07:30"),
        _task("Feed", "Luna", 10, "medium", species="cat"),
    ]

    scheduler, built, invalid = build_scheduler("Sangam", 60, tasks)

    assert invalid == []
    assert [pet.name for pet in scheduler.owner.pets] == ["Mochi", "Luna"]
    assert len(scheduler.owner.get_all_tasks()) == 2
    assert built[0][0].time == "07:30"  # normalized by Task


def test_build_scheduler_reuses_one_pet_for_repeated_name():
    tasks = [_task("Walk", "Mochi", 30, "high"), _task("Breakfast", "Mochi", 10, "high")]

    scheduler, built, invalid = build_scheduler("Sangam", 60, tasks)

    assert len(scheduler.owner.pets) == 1
    assert len(scheduler.owner.pets[0].tasks) == 2


def test_build_scheduler_collects_invalid_tasks_without_crashing():
    tasks = [
        _task("Walk", "Mochi", 0, "high"),      # invalid: duration <= 0
        _task("   ", "Mochi", 10, "high"),      # invalid: empty title
        _task("Meds", "Mochi", 5, "high"),      # valid
    ]

    scheduler, built, invalid = build_scheduler("Sangam", 60, tasks)

    assert len(invalid) == 2
    assert len(built) == 1
    assert built[0][0].title == "Meds"


# --- find_missed_deadlines ---

def test_skipped_task_with_deadline_is_missed():
    tasks = [_task("Walk", "Mochi", 30, "high"), _task("Meds", "Mochi", 20, "low", deadline="20:00")]
    # Only 30 minutes: the high-priority 30-min Walk fits, the low-priority Meds is skipped.
    scheduler, built, _ = build_scheduler("Sangam", 30, tasks)
    scheduler.generate_plan()

    missed = find_missed_deadlines(scheduler, built)

    assert len(missed) == 1
    assert "Meds" in missed[0] and "20:00" in missed[0]


def test_timed_task_finishing_after_deadline_is_missed():
    tasks = [_task("Long walk", "Mochi", 90, "high", time="19:00", deadline="20:00")]
    # 90-min task fits the budget, but 19:00 + 90 = 20:30 > 20:00 deadline.
    scheduler, built, _ = build_scheduler("Sangam", 120, tasks)
    scheduler.generate_plan()

    missed = find_missed_deadlines(scheduler, built)

    assert len(missed) == 1
    assert "20:30" in missed[0]


def test_timed_task_meeting_deadline_is_not_missed():
    tasks = [_task("Walk", "Mochi", 30, "high", time="19:00", deadline="20:00")]
    scheduler, built, _ = build_scheduler("Sangam", 60, tasks)
    scheduler.generate_plan()

    assert find_missed_deadlines(scheduler, built) == []


def test_tasks_without_deadlines_never_missed():
    tasks = [_task("Play", "Mochi", 15, "low")]
    scheduler, built, _ = build_scheduler("Sangam", 5, tasks)  # too little time; task skipped
    scheduler.generate_plan()

    assert find_missed_deadlines(scheduler, built) == []  # no deadline -> not a miss


# --- orchestration with a fake Groq client ---

class _Message:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Message(content)


class _Response:
    def __init__(self, content):
        self.choices = [_Choice(content)]


class _FakeGroq:
    """Scripts interpret -> review -> explain by inspecting each request's system prompt.

    Mirrors the ``client.chat.completions.create(...)`` surface the planner calls.
    """

    def __init__(self, extraction, review_decision="approve", revised=None, explanation="Done."):
        self.extraction = extraction
        self.review_decision = review_decision
        self.revised = revised
        self.explanation = explanation
        self.calls = []

        parent = self

        class _Completions:
            def create(self, **kwargs):
                parent.calls.append(kwargs)
                system = kwargs["messages"][0]["content"].lower()
                json_mode = kwargs.get("response_format") is not None
                if not json_mode:
                    return _Response(parent.explanation)             # explain step
                if "reviewing" in system:
                    return _Response(json.dumps({
                        "decision": parent.review_decision,
                        "reason": "ok",
                        "confidence": 0.8,
                        "revised_tasks": parent.revised,
                    }))
                return _Response(json.dumps(parent.extraction))      # interpret step

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()


def _extraction(minutes, tasks, confidence=0.9):
    return {"available_minutes": minutes, "confidence": confidence, "assumptions": [], "tasks": tasks}


def test_plan_day_happy_path_returns_dayplan():
    tasks = [_task("Walk", "Mochi", 30, "high"), _task("Bath", "Mochi", 40, "low")]
    client = _FakeGroq(_extraction(30, tasks))  # only Walk fits

    plan = plan_day("do stuff", owner_name="Sangam", default_minutes=30, client=client)

    assert isinstance(plan, DayPlan)
    assert [t.title for t in plan.planned] == ["Walk"]
    assert [t.title for t in plan.skipped] == ["Bath"]
    assert plan.explanation == "Done."
    assert plan.confidence == 0.9
    assert plan.iterations == 1  # approved on first review


def test_plan_day_applies_a_revision():
    first = [_task("Bath", "Mochi", 50, "low")]
    revised = [_task("Meds", "Mochi", 10, "high")]
    client = _FakeGroq(_extraction(30, first), review_decision="revise", revised=revised)

    plan = plan_day("do stuff", owner_name="Sangam", default_minutes=30, max_revisions=1, client=client)

    # After one revision the loop hits its cap and keeps the revised plan.
    assert [t.title for t in plan.planned] == ["Meds"]
    assert plan.tasks == revised
    assert plan.iterations == 2


def test_plan_day_flags_low_reliability_on_missed_deadline():
    tasks = [_task("Walk", "Mochi", 30, "high"), _task("Meds", "Mochi", 20, "low", deadline="20:00")]
    client = _FakeGroq(_extraction(30, tasks))  # Meds skipped -> deadline missed

    plan = plan_day("do stuff", owner_name="Sangam", default_minutes=30, client=client)

    assert plan.missed_deadlines  # Meds can't be done
    assert plan.is_reliable() is False


def test_plan_day_rejects_empty_request():
    with pytest.raises(PlannerError):
        plan_day("   ", client=_FakeGroq(_extraction(30, [])))


def test_parse_json_tolerates_code_fences():
    assert nl_planner._parse_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_parse_json_raises_on_garbage():
    with pytest.raises(PlannerError):
        nl_planner._parse_json("not json at all")
