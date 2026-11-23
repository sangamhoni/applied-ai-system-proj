"""Evaluation harness for the PawPal+ natural-language planner.

Runs a set of predefined requests through the full planner and checks that each one
produces the expected behavior (right tasks planned/skipped, deadlines flagged,
conflicts caught, reliability verdict correct). Prints a pass/fail summary with the
model's confidence for each case.

Two modes:

    python evaluate.py           # offline: deterministic, scripted model responses
                                 #   (no API key needed — reproducible for anyone)
    python evaluate.py --live    # live: calls the real Groq model (needs GROQ_API_KEY)

Offline mode feeds each scenario a fixed extraction so the result is deterministic and
the harness itself can be trusted. Live mode exercises the real model and checks the
same behavioral expectations (which are written loosely enough to tolerate wording).
"""

from __future__ import annotations

import json
import sys

from nl_planner import DayPlan, PlannerError, plan_day


# --- scripted (offline) model client ---

class _ScriptedClient:
    """A stand-in for the Groq client that returns a fixed extraction for one scenario."""

    def __init__(self, extraction: dict):
        self._extraction = extraction
        parent = self

        class _Completions:
            def create(self, **kwargs):
                system = kwargs["messages"][0]["content"].lower()
                json_mode = kwargs.get("response_format") is not None
                content = parent._content(system, json_mode)
                return type("R", (), {"choices": [type("C", (), {
                    "message": type("M", (), {"content": content})()})()]})()

        self.chat = type("Chat", (), {"completions": _Completions()})()

    def _content(self, system: str, json_mode: bool) -> str:
        if not json_mode:  # explain step
            return "Here is your plan."
        if "reviewing" in system:  # review step: always approve in offline mode
            return json.dumps({"decision": "approve", "reason": "looks good",
                               "confidence": 0.85, "revised_tasks": None})
        return json.dumps(self._extraction)  # interpret step


def _extraction(minutes, tasks, confidence=0.9):
    return {"available_minutes": minutes, "confidence": confidence, "assumptions": [], "tasks": tasks}


def _task(title, pet, minutes, priority, *, time=None, deadline=None, species="dog"):
    return {"title": title, "pet": pet, "species": species, "duration_minutes": minutes,
            "priority": priority, "time": time, "deadline": deadline, "frequency": "daily"}


# --- scenarios: (name, request, scripted extraction, expectation check) ---

def _planned_titles(plan: DayPlan) -> set[str]:
    return {t.title for t in plan.planned}


def _skipped_titles(plan: DayPlan) -> set[str]:
    return {t.title for t in plan.skipped}


SCENARIOS = [
    {
        "name": "Everything fits the hour",
        "request": ("I have one hour tonight. Feed both pets Mochi and Pichu, give Pichu "
                    "medication, and walk Mochi for half an hour if there is enough time."),
        "extraction": _extraction(60, [
            _task("Medication", "Pichu", 5, "high"),
            _task("Feed", "Mochi", 10, "medium"),
            _task("Feed", "Pichu", 10, "medium"),
            _task("Walk", "Mochi", 30, "low"),
        ]),
        "check": lambda p: (
            "Medication" in _planned_titles(p) and not p.skipped and p.is_reliable(),
            "medication planned, nothing skipped, reliable",
        ),
    },
    {
        "name": "Not enough time delays the long walk",
        "request": "I only have 25 minutes this morning. Walk Mochi for 30 minutes, feed Luna, and brush Luna.",
        "extraction": _extraction(25, [
            _task("Walk", "Mochi", 30, "medium"),
            _task("Feed", "Luna", 10, "medium", species="cat"),
            _task("Brush", "Luna", 10, "low", species="cat"),
        ]),
        "check": lambda p: (
            "Walk" in _skipped_titles(p) and "Feed" in _planned_titles(p),
            "30-min walk skipped, shorter tasks kept",
        ),
    },
    {
        "name": "Late-finishing task misses its deadline",
        "request": ("I have two hours tonight. Groom Mochi starting at 7pm for 90 minutes, "
                    "and it needs to be done by 8pm."),
        "extraction": _extraction(120, [
            _task("Grooming", "Mochi", 90, "medium", time="19:00", deadline="20:00"),
        ]),
        "check": lambda p: (
            bool(p.missed_deadlines) and not p.is_reliable(),
            "deadline miss flagged, reliability gate fails",
        ),
    },
    {
        "name": "Overlapping times are caught as a conflict",
        "request": "I have an hour. Feed Mochi at 8am and feed Luna at 8am.",
        "extraction": _extraction(60, [
            _task("Feed", "Mochi", 10, "medium", time="08:00"),
            _task("Feed", "Luna", 10, "medium", time="08:00", species="cat"),
        ]),
        "check": lambda p: (bool(p.conflicts), "time conflict detected"),
    },
]


def run(live: bool) -> int:
    """Run every scenario, print a summary, and return the number of failures."""
    mode = "LIVE (real Groq model)" if live else "OFFLINE (scripted, deterministic)"
    print(f"\nPawPal+ planner evaluation — mode: {mode}\n" + "=" * 64)

    passed = 0
    confidences: list[float] = []
    for scenario in SCENARIOS:
        client = None if live else _ScriptedClient(scenario["extraction"])
        try:
            plan = plan_day(scenario["request"], owner_name="Sangam",
                            default_minutes=60, client=client)
        except PlannerError as error:
            print(f"[ERROR] {scenario['name']}: {error}")
            continue

        ok, criterion = scenario["check"](plan)
        confidences.append(plan.confidence)
        status = "PASS" if ok else "FAIL"
        if ok:
            passed += 1
        print(f"[{status}] {scenario['name']}")
        print(f"        expected: {criterion}")
        print(f"        planned={sorted(_planned_titles(plan))} "
              f"skipped={sorted(_skipped_titles(plan))} "
              f"conflicts={len(plan.conflicts)} missed={len(plan.missed_deadlines)} "
              f"confidence={plan.confidence:.2f} reliable={plan.is_reliable()}")

    total = len(SCENARIOS)
    avg_conf = sum(confidences) / len(confidences) if confidences else 0.0
    print("=" * 64)
    print(f"Result: {passed}/{total} passed · average confidence {avg_conf:.2f}")
    return total - passed


if __name__ == "__main__":
    live_mode = "--live" in sys.argv
    failures = run(live=live_mode)
    sys.exit(1 if failures else 0)
