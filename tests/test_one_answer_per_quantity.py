"""Every endpoint gives the same answer to the same question.

The expensive bugs in this project were never wrong formulas. They were two
correct formulas with no single owner:

  * CTL merged per-field from intervals.icu and a local EWMA, so the readiness
    card and the calendar disagreed about the rider's fitness;
  * a ride's duration meaning elapsed time in one reader and moving time in
    another, so one ride read 118 minutes and 91 on the same screen;
  * a week's TSS target counting the sessions still in the plan while the ride
    total counted the whole calendar week, so a mid-week replan read 499/138
    where intervals.icu read 499/387.

Every one was found by a rider noticing two screens disagree. This asks all of
them at once: boot the app over one fixture, call every GET route, file every
number under the question it answers, and fail when one question has two
answers. tools/arch/quantities.py holds the probe and can be run by hand.

The scenarios matter more than the endpoint list. A healthy app agrees with
itself; the disagreements appear when intervals.icu is unreachable and one
reader falls back while another does not, or after a replan when one reader
covers the calendar week and another covers what is left of the plan.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools" / "arch"))
sys.path.insert(0, str(ROOT / "tests"))

import quantities as Q                                   # noqa: E402
from quantity_fixture import SCENARIOS, build            # noqa: E402


def _run(scenario: str, shift: bool = False):
    with build(shift_one_endpoint=shift, scenario=scenario) as (client, fixtures):
        import app as app_module
        observations, skipped = Q.probe(
            client, Q.get_routes(app_module.app), fixtures)
    return observations, skipped


def _describe(found: list) -> str:
    out = []
    for f in found:
        out.append(f"{f['quantity']} ({f['scope']}), spread {f['spread']}:")
        out += [f"    {a['value']:>10}  {a['endpoint']}{a['path']}"
                for a in f["answers"]]
    return "\n".join(out)


# Nothing pinned. `week_prescribed` was here: `tss_target` meant the ramp's
# budget in /api/plan and the prescribed sum everywhere else, 500 against 447.
# The responses now name the two questions apart (tss_budget, planned_tss)
# and the list is empty. Adding to it needs a reason in writing.
KNOWN_GAPS: set = set()


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_one_question_one_answer(scenario):
    observations, _ = _run(scenario)
    assert observations, "the probe collected nothing: the fixture is not wired up"
    found = [f for f in Q.disagreements(observations)
             if f["quantity"] not in KNOWN_GAPS]
    assert not found, (
        f"endpoints disagree in the {scenario!r} state:\n{_describe(found)}")


def test_nothing_is_pinned_that_should_not_be():
    """A pinned gap that quietly grows is worse than no pin."""
    observations, _ = _run("normal")
    disagreeing = {f["quantity"] for f in Q.disagreements(observations)}
    assert disagreeing == KNOWN_GAPS, (
        f"the app disagrees with itself about {sorted(disagreeing)}; the "
        f"pinned list is {sorted(KNOWN_GAPS)}. If the week-load rename landed, "
        f"delete the pin; if something new broke, it is in that difference.")


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_the_probe_can_see_a_disagreement(scenario):
    """The control, and the reason to believe the test above.

    /api/readiness is made to answer with a CTL 7 higher than every other
    door. A probe that reports agreement here is measuring nothing, which is
    the state this one shipped in twice while it was being written: once
    comparing only leaf keys named exactly "ctl" (missing `ctl_actual` beside
    it), once patching the module attribute while FastAPI served the function
    object it had captured at registration.
    """
    observations, _ = _run(scenario, shift=True)
    found = Q.disagreements(observations, "ctl")
    assert found, (
        f"the probe reported agreement in {scenario!r} while /api/readiness "
        f"was answering with a CTL 7 higher than everything else")
    answers = {a["endpoint"] for f in found for a in f["answers"]}
    assert len(answers) >= 2, f"only one door was compared: {answers}"


def test_the_probe_reaches_the_endpoints_a_rider_reads():
    """Coverage, pinned. Silence is indistinguishable from agreement, so the
    readers a rider actually looks at have to be contributing numbers -- when
    one stops, this fails instead of quietly measuring less.

    /api/today-session earned its place here: it nests the prescription under
    `planned` with no session type and no date, so a leaf-name scan filed one
    number from it (its FTP) and compared its minutes and TSS with nothing.
    """
    observations, _ = _run("normal")
    reporting = {o.endpoint.split("?")[0] for o in observations}
    for path in ("/api/readiness", "/api/calendar", "/api/weekly-plan",
                 "/api/week-summary", "/api/today-session", "/api/rider-stats",
                 "/api/wellness", "/api/activities", "/api/plan"):
        assert any(e.startswith(path) for e in reporting), (
            f"{path} reported no tracked quantity; either it stopped answering "
            f"or the probe stopped listening")
