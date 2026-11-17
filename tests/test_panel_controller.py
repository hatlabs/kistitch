import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

import pytest
from kipy.errors import ConnectionError as KiCadConnectionError
from loguru import logger
from panel_fakes import (
    BOARD_FILE,
    PROJECT,
    FakeBoard,
    FakeKiCad,
    busy,
    return_result,
    run_result,
    stitch_result,
)
from shapely.geometry import Polygon

from kistitch import drc, pourstitch
from kistitch.clear_command import Cleared
from kistitch.clearvias import RemovableVias
from kistitch.commit import CommitOutcome
from kistitch.padvia import PadviaOptions, PadviaPlanner
from kistitch.panel import controller as controller_module
from kistitch.panel.controller import (
    READY,
    Banner,
    ClearJob,
    Connection,
    Controller,
    CountJob,
    Field,
    Job,
    PadJob,
    Picker,
    RemoveJob,
    ReturnJob,
    StepFields,
    StitchJob,
    run_job,
)
from kistitch.panel.present import Icon, Step, View
from kistitch.panel.probe import (
    BoardId,
    BoardNets,
    BoardSwitchedError,
    NoBoardError,
    Probe,
)
from kistitch.plan import StitchClass
from kistitch.progress import Cancelled, Progress, Stage
from kistitch.returnvia import DEFAULT_DISTANCE, ReturnViaOptions
from kistitch.snapshot import Snapshot, ViaSize, Zone

START = datetime(2026, 11, 17, 14, 2, 31)
BOARD = BoardId(f"{PROJECT}/{BOARD_FILE}", BOARD_FILE)
PROGRESS = Progress()
OTHER = BoardId("/work/other/other.kicad_pcb", "other.kicad_pcb")
INNER = ("In1.Cu", "In2.Cu")
OUTER = ("F.Cu", "B.Cu")
ZONES: dict[str, tuple[str, ...]] = {
    "GND": INNER,
    "GND_ISO": INNER,
    "+3V3": OUTER,
    "+5V": OUTER,
    "VBUS": OUTER,
}


def nets_of(
    zones: dict[str, tuple[str, ...]], extra: Sequence[str] = ("SIG",)
) -> BoardNets:
    layers = tuple(sorted({L for ls in zones.values() for L in ls}))
    return BoardNets(
        Snapshot(
            copper_layers=layers,
            zones=[Zone(n, ls, 0, Polygon()) for n, ls in zones.items()],
        ),
        frozenset([*zones, *extra]),
    )


@dataclass
class Clock:
    now: datetime = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


@dataclass
class Prober:
    """Answers each probe with `answer`: a probe, or an error to raise."""

    answer: Probe | BaseException = field(
        default_factory=lambda: Probe(BOARD, nets_of(ZONES))
    )
    calls: int = 0

    def __call__(self) -> Probe:
        self.calls += 1
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


@dataclass
class Runner:
    """Records each job and the board it was for, reports `stages`, calls
    `during` after each and ends with `outcome`: a result, an error to
    raise, or a callable giving either from the job."""

    outcome: object = None
    stages: Sequence[tuple[Stage, tuple[int, int] | None]] = (
        (Stage.REFILL, None),
        (Stage.READ, None),
        (Stage.PLAN, (1, 3)),
        (Stage.DRC, (2, 5)),
    )
    during: Callable[[Stage], None] = lambda _stage: None
    jobs: list[Job] = field(default_factory=list)
    boards: list[str] = field(default_factory=list)
    log: Sequence[str] = ()

    def __call__(self, job: Job, board: str, progress: Progress) -> object:
        self.jobs.append(job)
        self.boards.append(board)
        for line in self.log:
            logger.info(line)
        for stage, count in self.stages:
            progress.enter(stage, count) if stage is not Stage.COMMIT else (
                progress.report(stage, count)
            )
            self.during(stage)
        outcome = self.outcome
        if callable(outcome) and not isinstance(outcome, type):
            outcome = outcome(job)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


Work = Callable[[], None]


class Spawned:
    """Holds the work handed to worker threads until the test runs it."""

    def __init__(self) -> None:
        self.waiting: list[Callable[[], None]] = []
        self.daemons: list[bool] = []

    def __call__(self, work: Callable[[], None], *, daemon: bool) -> None:
        self.waiting.append(work)
        self.daemons.append(daemon)

    def finish(self) -> None:
        while self.waiting:
            self.waiting.pop(0)()


@dataclass
class Panel:
    controller: Controller
    runner: Runner
    prober: Prober
    clock: Clock
    spawned: Spawned
    closed: list[bool]

    def finish(self) -> None:
        self.spawned.finish()


def panel(
    outcome: object = None,
    *,
    probe: Prober | None = None,
    connect: bool = True,
) -> Panel:
    runner = Runner(outcome)
    prober = probe or Prober()
    clock = Clock()
    spawned = Spawned()
    closed: list[bool] = []
    c = Controller(
        post=lambda f: f(),
        spawn=spawned,
        probe=prober,
        run=runner,
        clock=clock,
        closable=lambda: closed.append(True),
    )
    p = Panel(c, runner, prober, clock, spawned, closed)
    if connect:
        c.activate()
        p.finish()
    return p


def place(p: Panel, step: Step, outcome: object) -> None:
    p.runner.outcome = outcome
    assert p.controller.place(step)
    p.finish()


def heatmap(p: Panel, step: Step, outcome: object, view: View | None = None) -> None:
    p.runner.outcome = outcome
    assert p.controller.heatmap(step, view)
    p.finish()


POWER_PLACED = stitch_result((0.8,), placement=run_result(placed=27))
PADS_PLACED = run_result(placed=35, unfixable=3)
RETURN_HEATMAP = return_result(unserved=14)
GROUND_HEATMAP = stitch_result((0.0,), distance=3.0)


def test_controller_present_and_probe_import_no_qt() -> None:
    code = (
        "import sys, kistitch.panel.controller, kistitch.panel.present, "
        "kistitch.panel.probe; "
        "print(sorted(m for m in sys.modules if m.startswith('PySide6')))"
    )

    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )

    assert out.stdout.strip() == "[]"


class TestRuns:
    def test_place_on_return_vias_builds_its_options_and_reports(self) -> None:
        p = panel()
        p.controller.set_fields(Step.RETURN, StepFields(distance=1.5))

        place(p, Step.RETURN, return_result(placement=run_result(placed=61)))

        (job,) = p.runner.jobs
        assert isinstance(job, ReturnJob)
        assert job.view is None
        assert job.options.distance == 1.5
        assert job.options.ground_nets is None
        assert job.options.via is None
        assert job.refill
        assert p.runner.boards == [BOARD.identity]
        assert p.spawned.daemons[-1] is False
        status = p.controller.status(Step.RETURN)
        assert status.icon is Icon.DONE
        assert status.lines == ("Placed 61 vias; every via served",)
        assert status.detail == "Place · just now"
        assert p.controller.outcome is not None
        assert p.controller.outcome.headline == "Return vias: placed 61 vias"

    def test_while_a_run_is_active_every_action_is_disabled(self) -> None:
        p = panel(POWER_PLACED)
        c = p.controller

        assert c.place(Step.POWER)

        assert c.running
        assert not any(c.step_enabled(s) for s in Step)
        assert not c.cleanup_enabled
        assert not c.refill_enabled
        assert not c.editable
        assert not c.place(Step.PAD_VIAS)
        assert not c.heatmap(Step.RETURN)
        assert not c.remove_vias()
        assert not c.clear()
        assert c.running_line(Step.POWER) == "Placing vias…"
        assert c.running_line(Step.PAD_VIAS) == ""
        p.finish()
        assert not c.running
        assert all(c.step_enabled(s) for s in Step)
        assert c.cleanup_enabled
        assert c.editable

    def test_cancel_sets_the_stop_flag_and_keeps_the_previous_status(self) -> None:
        p = panel()
        place(p, Step.POWER, POWER_PLACED)
        before = p.controller.status(Step.POWER)
        p.runner.during = lambda stage: p.controller.cancel()
        p.clock.advance(minutes=1)

        place(p, Step.POWER, lambda job: Cancelled())

        assert p.controller.outcome is not None
        assert p.controller.outcome.headline == "Cancelled — no vias committed."
        after = p.controller.status(Step.POWER)
        assert after.lines == before.lines
        assert after.detail == "Place · 1 min ago"

    def test_cancel_turns_into_cancelling_until_the_run_ends(self) -> None:
        p = panel(POWER_PLACED)
        c = p.controller
        c.place(Step.POWER)
        assert c.activity is not None
        assert c.activity.cancel_enabled
        assert c.activity.cancel_label == "Cancel (Esc)"

        c.cancel()

        assert c.activity is not None
        assert not c.activity.cancel_enabled
        assert c.activity.cancel_label == "Cancelling…"

    def test_the_run_stops_at_its_next_stage_after_cancel(self) -> None:
        p = panel()
        place(p, Step.POWER, POWER_PLACED)
        p.runner.stages = ((Stage.READ, None), (Stage.PLAN, None), (Stage.DRC, None))
        entered = []

        def cancel_once(stage: Stage) -> None:
            entered.append(stage)
            p.controller.cancel()

        p.runner.during = cancel_once

        place(p, Step.POWER, POWER_PLACED)

        assert entered == [Stage.READ]
        assert p.controller.outcome is not None
        assert p.controller.outcome.headline == "Cancelled — no vias committed."

    def test_cancelled_heatmap(self) -> None:
        p = panel()
        p.runner.during = lambda stage: p.controller.cancel()

        heatmap(p, Step.POWER, lambda job: Cancelled())

        assert p.controller.outcome is not None
        assert p.controller.outcome.headline == "Cancelled — no heatmap placed."
        assert p.controller.status(Step.POWER).lines == ("No result yet",)

    def test_cancel_after_the_committing_stage_shows_the_landed_result(self) -> None:
        p = panel()
        p.runner.stages = ((Stage.READ, None), (Stage.COMMIT, None))

        def cancel_late(stage: Stage) -> None:
            if stage is Stage.COMMIT:
                assert p.controller.activity is not None
                assert not p.controller.activity.cancel_enabled
                assert p.controller.activity.hint == "Committing — too late to cancel."
                p.controller.cancel()

        p.runner.during = cancel_late

        place(p, Step.POWER, POWER_PLACED)

        assert p.controller.outcome is not None
        assert p.controller.outcome.headline == "Power stitching: placed 27 vias"
        assert p.controller.status(Step.POWER).lines == (
            "Placed 27 vias; 0.8 mm² still beyond 10 mm",
        )

    def test_a_cancel_racing_the_commit_still_shows_the_landed_result(self) -> None:
        p = panel()
        p.runner.stages = ((Stage.READ, None),)
        p.runner.during = lambda stage: p.controller.cancel()

        place(p, Step.PAD_VIAS, PADS_PLACED)

        assert p.controller.status(Step.PAD_VIAS).lines == (
            "Placed 35 vias; 3 pads have no room (circled)",
        )

    def test_refills_unchecked_build_runs_with_refills_off(self) -> None:
        p = panel()
        p.controller.set_refill(False)

        place(p, Step.PAD_VIAS, PADS_PLACED)
        heatmap(p, Step.GROUND, GROUND_HEATMAP)

        assert [j.refill for j in p.runner.jobs] == [False, False]

    def test_activity_shows_the_stage_its_count_and_elapsed_time(self) -> None:
        p = panel(POWER_PLACED)
        p.runner.stages = ((Stage.DRC, (2, 5)),)
        seen = []

        def look(stage: Stage) -> None:
            p.clock.advance(seconds=42)
            a = p.controller.activity
            assert a is not None
            seen.append((a.headline, a.stage, a.count, a.elapsed, a.hint))

        p.runner.during = look

        place(p, Step.POWER, POWER_PLACED)

        assert seen == [
            (
                "Power stitching: placing vias",
                "Checking with DRC, round 2 of up to 5",
                (2, 5),
                "0:42",
                "No vias are committed before the end of the run.",
            )
        ]

    def test_planning_counts_the_steps_items(self) -> None:
        p = panel()
        p.runner.stages = ((Stage.PLAN, (1, 3)),)
        seen = []
        p.runner.during = lambda stage: seen.append(p.controller.activity.stage)  # type: ignore[union-attr]

        place(p, Step.POWER, POWER_PLACED)
        place(p, Step.PAD_VIAS, PADS_PLACED)

        assert seen == ["Planning vias, net 1 of 3", "Planning vias, pad 1 of 3"]

    def test_refill_after_the_commit_says_committed(self) -> None:
        p = panel()
        p.runner.stages = ((Stage.COMMIT, None), (Stage.REFILL, None))
        hints = []
        p.runner.during = lambda stage: hints.append(p.controller.activity.hint)  # type: ignore[union-attr]

        place(p, Step.POWER, POWER_PLACED)

        assert hints == [
            "Committing — too late to cancel.",
            "Committed. Refilling zones…",
        ]

    def test_heatmap_activity_is_about_the_heatmap(self) -> None:
        p = panel()
        p.runner.stages = ((Stage.MEASURE, None), (Stage.COMMIT, None))
        seen = []

        def look(stage: Stage) -> None:
            a = p.controller.activity
            assert a is not None
            seen.append((a.headline, a.stage, a.hint))
            assert p.controller.running_line(Step.GROUND) == "Making the heatmap…"

        p.runner.during = look

        heatmap(p, Step.GROUND, GROUND_HEATMAP)

        assert seen == [
            (
                "Ground stitching: making the heatmap",
                "Measuring",
                "No heatmap is placed before the end of the run.",
            ),
            (
                "Ground stitching: making the heatmap",
                "Placing them on User.1–User.4",
                "Committing — too late to cancel.",
            ),
        ]

    def test_details_hold_the_runs_log(self) -> None:
        p = panel()
        p.runner.log = ("returnvia: 61 vias planned", "after 2 DRC rounds")

        place(p, Step.RETURN, return_result(placement=run_result(placed=61)))

        assert p.controller.details == "returnvia: 61 vias planned\nafter 2 DRC rounds"
        assert not p.controller.details_open


class TestStatusLines:
    def test_no_result_yet(self) -> None:
        s = panel().controller.status(Step.PAD_VIAS)

        assert (s.icon, s.lines, s.detail) == (None, ("No result yet",), "")

    def test_a_place_after_another_steps_place_replaces_its_markers(self) -> None:
        p = panel()
        place(p, Step.PAD_VIAS, PADS_PLACED)

        place(p, Step.POWER, POWER_PLACED)

        assert (
            p.controller.status(Step.PAD_VIAS).detail
            == "Place · just now (markers since replaced)"
        )
        assert p.controller.status(Step.POWER).detail == "Place · just now"

    def test_a_place_that_committed_nothing_replaces_no_markers(self) -> None:
        p = panel()
        place(p, Step.PAD_VIAS, PADS_PLACED)

        place(p, Step.POWER, stitch_result((0.0,), placement=run_result()))

        assert p.controller.status(Step.PAD_VIAS).detail == "Place · just now"

    def test_return_vias_all_copper_heatmap(self) -> None:
        p = panel()

        heatmap(p, Step.RETURN, return_result(full_map=True), View.ALL_COPPER)

        (job,) = p.runner.jobs
        assert isinstance(job, ReturnJob)
        assert job.view is View.ALL_COPPER
        assert (
            p.controller.status(Step.RETURN).detail == "Heatmap: all copper · just now"
        )
        assert p.controller.heatmap_view is View.ALL_COPPER

    def test_return_vias_heatmap_defaults_to_unserved_vias(self) -> None:
        p = panel()

        heatmap(p, Step.RETURN, RETURN_HEATMAP)

        (job,) = p.runner.jobs
        assert isinstance(job, ReturnJob)
        assert job.view is View.UNSERVED
        assert p.controller.status(Step.RETURN).detail == (
            "Heatmap: unserved vias · just now"
        )
        assert p.controller.heatmap_view is View.UNSERVED

    def test_a_heatmap_after_another_replaces_its_heatmap_and_markers(self) -> None:
        p = panel()
        heatmap(p, Step.RETURN, RETURN_HEATMAP)

        heatmap(p, Step.GROUND, GROUND_HEATMAP)

        assert p.controller.status(Step.RETURN).detail == (
            "Heatmap: unserved vias · just now (markers and heatmap since replaced)"
        )
        assert p.controller.heatmap_view is None

    def test_a_heatmap_after_a_place_replaces_its_markers(self) -> None:
        p = panel()
        place(p, Step.PAD_VIAS, PADS_PLACED)

        heatmap(p, Step.POWER, stitch_result((5.0,)))

        assert p.controller.status(Step.PAD_VIAS).detail == (
            "Place · just now (markers since replaced)"
        )

    def test_a_place_after_a_heatmap_replaces_only_its_markers(self) -> None:
        p = panel()
        heatmap(p, Step.RETURN, RETURN_HEATMAP)

        place(p, Step.PAD_VIAS, PADS_PLACED)

        assert p.controller.status(Step.RETURN).detail == (
            "Heatmap: unserved vias · just now (markers since replaced)"
        )

    def test_a_heatmap_kicad_refused_replaces_nothing(self) -> None:
        p = panel()
        heatmap(p, Step.RETURN, RETURN_HEATMAP)

        heatmap(p, Step.POWER, stitch_result((5.0,), placed_overlays=False))

        assert p.controller.status(Step.RETURN).detail == (
            "Heatmap: unserved vias · just now"
        )
        assert p.controller.status(Step.POWER).lines == (
            "5.0 mm² beyond 10 mm on 1 of 1 nets",
        )

    def test_clear_marks_markers_and_heatmap_cleared(self) -> None:
        p = panel()
        place(p, Step.PAD_VIAS, PADS_PLACED)
        heatmap(p, Step.RETURN, RETURN_HEATMAP)
        place(p, Step.POWER, stitch_result((0.8,), placement=run_result(27, splits=1)))
        p.runner.outcome = Cleared(vias=0, commit=CommitOutcome.LANDED)

        assert p.controller.clear()
        p.finish()

        assert isinstance(p.runner.jobs[-1], ClearJob)
        assert p.controller.status(Step.RETURN).detail == (
            "Heatmap: unserved vias · just now "
            "(markers since replaced, heatmap since cleared)"
        )
        assert p.controller.status(Step.POWER).detail == (
            "Place · just now (markers since cleared)"
        )
        assert p.controller.outcome is not None
        assert p.controller.outcome.headline == "Cleared markers and heatmaps."

    def test_no_heatmap_needed_replaces_the_heatmap_and_markers(self) -> None:
        p = panel()
        heatmap(p, Step.RETURN, RETURN_HEATMAP)

        heatmap(p, Step.GROUND, stitch_result(()))

        assert p.controller.status(Step.RETURN).detail == (
            "Heatmap: unserved vias · just now (markers and heatmap since replaced)"
        )
        assert p.controller.outcome is not None
        assert p.controller.outcome.headline == "Ground stitching: no heatmap needed"

    def test_no_heatmap_needed_leaves_no_heatmap_on_the_board(self) -> None:
        p = panel()

        heatmap(p, Step.RETURN, return_result())

        assert p.controller.heatmap_view is None
        p.runner.outcome = Cleared(vias=0, commit=CommitOutcome.LANDED)
        p.controller.clear()
        p.finish()
        assert p.controller.status(Step.RETURN).detail == (
            "Heatmap: unserved vias · just now"
        )

    def test_chosen_nets_that_lost_their_pours_measure_nothing(self) -> None:
        p = panel()
        heatmap(p, Step.RETURN, RETURN_HEATMAP)
        p.controller.choose(Picker.STITCH_GROUND, ("GND_ISO",))

        heatmap(p, Step.GROUND, stitch_result(()))

        c = p.controller
        assert c.outcome is not None
        assert (c.outcome.icon, c.outcome.headline) == (
            Icon.WARNING,
            "Ground stitching: nothing measured",
        )
        status = c.status(Step.GROUND)
        assert (status.icon, status.lines) == (
            Icon.WARNING,
            ("Nothing measured: the chosen nets have no pours",),
        )
        # The run still took the earlier heatmap and markers off the board.
        assert c.status(Step.RETURN).detail == (
            "Heatmap: unserved vias · just now (markers and heatmap since replaced)"
        )

    def test_a_place_that_left_no_markers_owns_none(self) -> None:
        p = panel()
        place(p, Step.POWER, POWER_PLACED)

        place(p, Step.PAD_VIAS, PADS_PLACED)

        assert p.controller.status(Step.POWER).detail == "Place · just now"

    def test_a_stitching_heatmap_owns_no_markers(self) -> None:
        p = panel()
        heatmap(p, Step.GROUND, GROUND_HEATMAP)

        place(p, Step.PAD_VIAS, PADS_PLACED)

        assert p.controller.status(Step.GROUND).detail == "Heatmap · just now"

    def test_a_return_vias_heatmap_with_every_via_served_owns_no_markers(
        self,
    ) -> None:
        p = panel()
        heatmap(p, Step.RETURN, return_result(full_map=True), View.ALL_COPPER)

        place(p, Step.PAD_VIAS, PADS_PLACED)

        assert p.controller.status(Step.RETURN).detail == (
            "Heatmap: all copper · just now"
        )

    def test_a_place_with_splits_owns_the_markers(self) -> None:
        p = panel()
        place(p, Step.POWER, stitch_result((0.0,), placement=run_result(27, splits=1)))

        place(p, Step.PAD_VIAS, PADS_PLACED)

        assert p.controller.status(Step.POWER).detail == (
            "Place · just now (markers since replaced)"
        )

    def test_a_stitching_place_owns_no_markers_for_unfixable_spots(self) -> None:
        p = panel()
        place(
            p, Step.POWER, stitch_result((0.8,), placement=run_result(27, unfixable=2))
        )

        place(p, Step.PAD_VIAS, PADS_PLACED)

        assert p.controller.status(Step.POWER).detail == "Place · just now"

    def test_a_landed_place_that_measured_nothing_replaces_the_markers(self) -> None:
        p = panel()
        place(p, Step.PAD_VIAS, PADS_PLACED)
        p.controller.choose(Picker.STITCH_GROUND, ("GND_ISO",))
        landed = replace(run_result(), commit=CommitOutcome.LANDED)

        place(p, Step.GROUND, stitch_result((), placement=landed))

        assert p.controller.status(Step.PAD_VIAS).detail == (
            "Place · just now (markers since replaced)"
        )
        p.runner.outcome = Cleared(vias=0, commit=CommitOutcome.LANDED)
        assert p.controller.clear()
        p.finish()
        assert p.controller.status(Step.PAD_VIAS).detail == (
            "Place · just now (markers since replaced)"
        )

    def test_relative_time_and_tooltip(self) -> None:
        p = panel()
        place(p, Step.PAD_VIAS, PADS_PLACED)

        p.clock.advance(minutes=6, seconds=5)

        s = p.controller.status(Step.PAD_VIAS)
        assert s.detail == "Place · 6 min ago"
        assert s.tooltip == "At 14:02:31"

    def test_relative_time_in_hours(self) -> None:
        p = panel()
        place(p, Step.PAD_VIAS, PADS_PLACED)

        p.clock.advance(hours=1, minutes=20)
        assert p.controller.status(Step.PAD_VIAS).detail == "Place · 1 h 20 min ago"
        p.clock.advance(minutes=40)
        assert p.controller.status(Step.PAD_VIAS).detail == "Place · 2 h ago"

    def test_outcome_time(self) -> None:
        p = panel()

        place(p, Step.PAD_VIAS, PADS_PLACED)

        assert p.controller.outcome_time == "14:02"

    def test_a_switched_board_clears_every_status_line(self) -> None:
        p = panel()
        place(p, Step.PAD_VIAS, PADS_PLACED)
        p.prober.answer = Probe(OTHER, nets_of(ZONES))

        p.controller.activate()
        p.finish()

        assert p.controller.status(Step.PAD_VIAS).lines == ("No result yet",)
        assert p.controller.title == "kistitch — other.kicad_pcb"

    def test_the_same_board_keeps_the_status_lines(self) -> None:
        p = panel()
        place(p, Step.PAD_VIAS, PADS_PLACED)

        p.controller.activate()
        p.finish()

        assert p.prober.calls == 2
        assert p.controller.status(Step.PAD_VIAS).lines == (
            "Placed 35 vias; 3 pads have no room (circled)",
        )

    def test_a_split_gets_its_own_line(self) -> None:
        p = panel()

        place(p, Step.POWER, stitch_result((0.0,), placement=run_result(27, splits=2)))

        assert p.controller.status(Step.POWER).lines == (
            "Placed 27 vias; every point within 10 mm",
            "2 possible pour splits marked (squares)",
        )


class TestFailures:
    def test_busy_offers_try_again_with_the_same_options(self) -> None:
        p = panel()
        p.controller.set_fields(Step.POWER, StepFields(distance=8.0, fallback_via=""))
        place(p, Step.POWER, busy())
        assert p.controller.outcome is not None
        assert p.controller.outcome.headline == "KiCad is busy — nothing committed"
        assert p.controller.try_again_available
        assert p.controller.status(Step.POWER).lines == ("No result yet",)
        p.controller.set_fields(Step.POWER, StepFields(distance=9.0))

        p.runner.outcome = POWER_PLACED
        assert p.controller.try_again()
        p.finish()

        first, second = p.runner.jobs
        assert first == second
        assert isinstance(second, StitchJob)
        assert second.options.distance == 8.0
        assert not p.controller.try_again_available

    def test_try_again_needs_the_connection(self) -> None:
        p = panel()
        place(p, Step.POWER, busy())
        p.prober.answer = KiCadConnectionError("gone")

        p.controller.activate()
        p.finish()

        assert not p.controller.try_again_available

    def test_try_again_goes_once_another_action_starts(self) -> None:
        p = panel()
        place(p, Step.POWER, busy())

        place(p, Step.PAD_VIAS, PADS_PLACED)

        assert not p.controller.try_again_available
        assert not p.controller.try_again()

    def test_a_lost_connection_moves_to_the_disconnected_state(self) -> None:
        p = panel()
        place(p, Step.PAD_VIAS, PADS_PLACED)

        place(p, Step.POWER, KiCadConnectionError("gone"))

        c = p.controller
        assert c.outcome is not None
        assert c.outcome.headline == "Lost the connection to KiCad"
        assert c.connection is Connection.NO_KICAD
        assert c.banner is not None
        assert c.banner.headline == "Cannot reach KiCad"
        assert not any(c.step_enabled(s) for s in Step)
        assert c.status(Step.PAD_VIAS).lines == (
            "Placed 35 vias; 3 pads have no room (circled)",
        )

    @pytest.mark.parametrize(
        ("ending", "headline"),
        [
            (KiCadConnectionError("gone"), "Cannot reach KiCad"),
            (NoBoardError("none"), "No board open"),
            (
                stitch_result(
                    (0.0,), placement=run_result(27, commit=CommitOutcome.UNKNOWN)
                ),
                "Cannot reach KiCad",
            ),
        ],
    )
    def test_losing_the_connection_after_a_busy_probe_shows_the_reason(
        self, ending: object, headline: str
    ) -> None:
        p = panel()
        p.prober.answer = busy()
        p.controller.activate()
        p.finish()
        assert p.controller.busy_hint

        place(p, Step.POWER, ending)

        banner = p.controller.banner
        assert banner is not None
        assert banner.headline == headline

    def test_an_unknown_commit_outcome_is_a_lost_connection(self) -> None:
        p = panel()

        place(
            p,
            Step.POWER,
            stitch_result(
                (0.0,), placement=run_result(27, commit=CommitOutcome.UNKNOWN)
            ),
        )

        assert p.controller.outcome is not None
        assert p.controller.outcome.headline == "Lost the connection to KiCad"
        assert p.controller.status(Step.POWER).lines == ("No result yet",)
        assert p.controller.connection is Connection.NO_KICAD

    def test_an_unexpected_error_opens_details_with_the_traceback(self) -> None:
        p = panel()
        p.runner.log = ("returnvia: planning",)

        place(p, Step.RETURN, KeyError("boom"))

        c = p.controller
        assert c.outcome is not None
        assert c.outcome.headline == "Return vias failed — unexpected error"
        assert c.details_open
        assert "returnvia: planning" in c.details
        assert "Traceback" in c.details
        assert "KeyError: 'boom'" in c.details

    def test_details_hold_the_detail_of_an_expected_failure(self) -> None:
        p = panel()
        p.runner.log = ("padvia: checking",)

        place(p, Step.PAD_VIAS, drc.DrcError("kicad-cli DRC failed (2)"))

        assert p.controller.details == "padvia: checking\n\nkicad-cli DRC failed (2)"
        assert not p.controller.details_open

    def test_details_open_is_kept_once_set(self) -> None:
        p = panel()
        p.controller.set_details_open(True)

        place(p, Step.PAD_VIAS, PADS_PLACED)

        assert p.controller.details_open

    def test_board_switched_during_a_run_changes_no_status_line(self) -> None:
        p = panel()
        place(p, Step.POWER, POWER_PLACED)

        place(p, Step.POWER, BoardSwitchedError("other"))

        assert p.controller.outcome is not None
        assert p.controller.outcome.headline == (
            "Power stitching didn't run — KiCad shows another board"
        )
        assert p.controller.status(Step.POWER).lines == (
            "Placed 27 vias; 0.8 mm² still beyond 10 mm",
        )
        assert p.prober.calls == 2

    def test_busy_keeps_the_status_line(self) -> None:
        p = panel()
        place(p, Step.POWER, POWER_PLACED)

        place(p, Step.POWER, busy())

        assert p.controller.status(Step.POWER).lines == (
            "Placed 27 vias; 0.8 mm² still beyond 10 mm",
        )
        assert p.controller.running_line(Step.POWER) == ""

    def test_project_files_error_keeps_the_status_line(self) -> None:
        p = panel()
        place(p, Step.PAD_VIAS, PADS_PLACED)

        place(p, Step.PAD_VIAS, drc.ProjectFilesError("not saved"))

        c = p.controller
        assert c.outcome is not None
        assert c.outcome.headline == "Pad vias failed — nothing committed"
        assert "--" not in c.outcome.body
        assert c.status(Step.PAD_VIAS).lines == (
            "Placed 35 vias; 3 pads have no room (circled)",
        )
        assert c.connection is Connection.CONNECTED

    def test_no_board_during_a_run_moves_to_no_board(self) -> None:
        p = panel()

        place(p, Step.PAD_VIAS, NoBoardError("none"))

        assert p.controller.connection is Connection.NO_BOARD


class TestConnection:
    def test_first_probe_is_out_while_connecting(self) -> None:
        p = panel(connect=False)
        c = p.controller

        assert c.connection is Connection.CONNECTING
        assert c.idle_text == "Connecting to KiCad…"
        assert not c.step_enabled(Step.PAD_VIAS)

        c.activate()
        assert p.spawned.daemons == [True]
        p.finish()

        assert c.connection is Connection.CONNECTED
        assert c.idle_text == READY
        assert c.title == "kistitch — main.kicad_pcb"

    def test_no_kicad_disables_the_steps_and_retry_probes_again(self) -> None:
        p = panel(probe=Prober(KiCadConnectionError("refused")))
        c = p.controller

        assert c.connection is Connection.NO_KICAD
        assert c.title == "kistitch"
        assert c.idle_text == "Not connected"
        assert c.banner is not None
        assert c.banner.headline == "Cannot reach KiCad"
        assert not any(c.step_enabled(s) for s in Step)
        assert not c.cleanup_enabled
        assert not c.picker(Picker.POWER).enabled
        assert c.picker(Picker.POWER).text == ""
        assert c.editable
        assert c.refill_enabled

        p.prober.answer = Probe(BOARD, nets_of(ZONES))
        c.retry()
        p.finish()

        assert c.connection is Connection.CONNECTED
        assert c.banner is None
        assert all(c.step_enabled(s) for s in Step)

    def test_no_board_open(self) -> None:
        p = panel(probe=Prober(NoBoardError("none")))

        assert p.controller.connection is Connection.NO_BOARD
        assert p.controller.banner is not None
        assert p.controller.banner.headline == "No board open"
        assert "close other KiCad windows" in p.controller.banner.body

    def test_a_busy_probe_keeps_the_last_board_and_hints(self) -> None:
        p = panel()
        p.prober.answer = busy()

        p.controller.activate()
        p.finish()

        c = p.controller
        assert c.connection is Connection.CONNECTED
        assert c.busy_hint
        assert c.title == "kistitch — main.kicad_pcb"
        assert all(c.step_enabled(s) for s in Step)

        p.prober.answer = Probe(BOARD, nets_of(ZONES))
        c.activate()
        p.finish()
        assert not c.busy_hint

    def test_activation_during_a_run_sends_no_probe(self) -> None:
        p = panel(POWER_PLACED)
        p.controller.place(Step.POWER)

        p.controller.activate()

        assert len(p.spawned.waiting) == 1
        p.finish()
        assert p.prober.calls == 1

    def test_a_probe_out_sends_no_second(self) -> None:
        p = panel(connect=False)

        p.controller.activate()
        p.controller.activate()

        assert len(p.spawned.waiting) == 1

    def test_actions_stay_enabled_while_reading_the_board(self) -> None:
        p = panel()
        c = p.controller

        c.activate()

        assert all(c.step_enabled(s) for s in Step)
        assert c.cleanup_enabled
        assert c.picker(Picker.POWER).enabled
        assert c.ground.enabled
        assert c.idle_text == "Reading the board…"

    def test_an_action_while_reading_runs_once_the_same_board_is_read(self) -> None:
        p = panel(POWER_PLACED)
        c = p.controller
        c.activate()

        assert c.place(Step.POWER)

        assert p.runner.jobs == []
        assert len(p.spawned.waiting) == 1
        assert c.running
        assert c.running_line(Step.POWER) == "Placing vias…"
        activity = c.activity
        assert activity is not None
        assert (activity.headline, activity.stage, activity.cancel_enabled) == (
            "Power stitching: placing vias",
            "Reading the board",
            True,
        )
        p.finish()
        assert [type(j) for j in p.runner.jobs] == [StitchJob]
        assert p.runner.boards == [BOARD.identity]
        assert c.status(Step.POWER).detail == "Place · just now"
        assert c.activity is None

    def test_a_newer_action_while_reading_replaces_the_held_one(self) -> None:
        p = panel(Cleared(vias=0, commit=CommitOutcome.LANDED))
        c = p.controller
        c.activate()
        assert c.place(Step.POWER)

        assert c.clear()
        p.finish()

        assert [type(j) for j in p.runner.jobs] == [ClearJob]

    def test_an_action_while_reading_is_refused_on_another_board(self) -> None:
        p = panel(POWER_PLACED)
        c = p.controller
        c.activate()
        assert c.place(Step.POWER)
        p.prober.answer = Probe(OTHER, nets_of(ZONES))

        p.finish()

        assert p.runner.jobs == []
        assert c.title == "kistitch — other.kicad_pcb"
        assert c.outcome is not None
        assert c.outcome.headline == (
            "Power stitching didn't run — KiCad shows another board"
        )
        assert c.activity is None
        assert c.running_line(Step.POWER) == ""

    def test_an_action_while_reading_is_dropped_when_kicad_is_gone(self) -> None:
        p = panel(POWER_PLACED)
        c = p.controller
        c.activate()
        assert c.place(Step.POWER)
        p.prober.answer = KiCadConnectionError("gone")

        p.finish()

        assert p.runner.jobs == []
        assert c.outcome is not None
        assert c.outcome.headline == "Lost the connection to KiCad"
        assert c.connection is Connection.NO_KICAD
        assert c.activity is None

    def test_an_action_while_reading_kicad_busy_offers_try_again(self) -> None:
        p = panel(POWER_PLACED)
        c = p.controller
        c.activate()
        assert c.place(Step.POWER)
        p.prober.answer = busy()

        p.finish()

        assert p.runner.jobs == []
        assert c.outcome is not None
        assert c.outcome.headline == "KiCad is busy — nothing committed"
        assert c.try_again_available
        assert c.try_again()
        p.finish()
        assert [type(j) for j in p.runner.jobs] == [StitchJob]

    def test_cancel_discards_an_action_held_while_reading(self) -> None:
        p = panel(POWER_PLACED)
        c = p.controller
        c.activate()
        assert c.place(Step.POWER)

        c.cancel()

        assert c.activity is None
        assert not c.running
        p.finish()
        assert p.runner.jobs == []
        assert c.outcome is None

    def test_try_again_while_reading_replaces_a_held_action(self) -> None:
        p = panel()
        place(p, Step.POWER, busy())
        c = p.controller
        c.activate()
        assert c.place(Step.PAD_VIAS)
        p.runner.outcome = POWER_PLACED

        assert c.try_again()
        p.finish()

        assert [type(j) for j in p.runner.jobs] == [StitchJob, StitchJob]
        assert c.activity is None

    def test_closing_discards_an_action_held_while_reading(self) -> None:
        p = panel(POWER_PLACED)
        c = p.controller
        c.activate()
        assert c.place(Step.POWER)

        assert c.close()

        p.finish()
        assert p.runner.jobs == []

    @staticmethod
    def try_again_while_reading(p: Panel, outcome: object) -> tuple[Work, Work]:
        """Try again a busy run while a probe is out: the probe's work and
        the run's, both waiting."""
        place(p, Step.POWER, busy())
        p.controller.activate()
        p.runner.outcome = outcome
        assert p.controller.try_again()
        probe_work, run_work = p.spawned.waiting
        p.spawned.waiting.clear()
        return probe_work, run_work

    def test_a_probe_answering_after_a_run_started_is_read_again_after(self) -> None:
        p = panel()
        probe_work, run_work = self.try_again_while_reading(p, POWER_PLACED)
        p.prober.answer = Probe(OTHER, nets_of(ZONES))

        probe_work()

        assert p.controller.title == "kistitch — main.kicad_pcb"
        assert p.spawned.waiting == []
        run_work()
        p.finish()
        assert p.prober.calls == 3
        assert p.controller.title == "kistitch — other.kicad_pcb"

    def test_another_board_with_a_probe_out_ends_with_it_read(self) -> None:
        p = panel()
        probe_work, run_work = self.try_again_while_reading(
            p, BoardSwitchedError("other")
        )
        p.prober.answer = Probe(OTHER, nets_of(ZONES))

        run_work()
        probe_work()
        p.finish()

        assert p.prober.calls == 3
        assert p.controller.title == "kistitch — other.kicad_pcb"
        assert all(p.controller.step_enabled(s) for s in Step)

    def test_a_busy_probe_with_no_board_read_offers_retry(self) -> None:
        p = panel(probe=Prober(busy()))
        c = p.controller

        assert c.banner == Banner(
            "KiCad is busy",
            "Close any dialog or tool in KiCad's PCB editor, then retry.",
        )
        assert c.busy_hint == ""
        assert c.idle_text == "Not connected"
        assert not any(c.step_enabled(s) for s in Step)

        p.prober.answer = Probe(BOARD, nets_of(ZONES))
        c.retry()
        p.finish()
        assert c.banner is None
        assert c.connection is Connection.CONNECTED

    def test_a_busy_probe_after_a_lost_connection_says_busy(self) -> None:
        p = panel()
        place(p, Step.POWER, KiCadConnectionError("gone"))
        p.prober.answer = busy()

        p.controller.activate()
        p.finish()

        assert p.controller.banner is not None
        assert p.controller.banner.headline == "KiCad is busy"

    def test_no_board_after_a_busy_probe_drops_the_busy_hint(self) -> None:
        p = panel()
        p.prober.answer = busy()
        p.controller.activate()
        p.finish()
        p.prober.answer = NoBoardError("none")

        p.controller.activate()
        p.finish()

        assert p.controller.busy_hint == ""
        assert p.controller.banner is not None
        assert p.controller.banner.headline == "No board open"

    def test_an_unexpected_probe_error_is_logged_as_no_kicad(self, log) -> None:
        p = panel(probe=Prober(KeyError("boom")))

        assert p.controller.connection is Connection.NO_KICAD
        assert any("The panel's probe failed" in line for line in log)

    def test_going_away_keeps_the_status_lines(self) -> None:
        p = panel()
        place(p, Step.PAD_VIAS, PADS_PLACED)
        p.prober.answer = KiCadConnectionError("gone")

        p.controller.activate()
        p.finish()

        assert p.controller.status(Step.PAD_VIAS).lines != ("No result yet",)


class TestGroundNets:
    def test_detected_ground_nets_and_where_they_come_from(self) -> None:
        g = panel().controller.ground

        assert g.chosen == ("GND", "GND_ISO")
        assert g.text == "GND, GND_ISO"
        assert g.origin == "Detected: zones on two or more inner layers"
        assert not g.use_detected
        assert g.enabled

    def test_gnd_fallback(self) -> None:
        p = panel(probe=Prober(Probe(BOARD, nets_of({"GND": OUTER}))))

        assert p.controller.ground.origin == (
            "Detected: GND (no net has zones on two inner layers)"
        )

    def test_editing_shows_your_choice_and_use_detected_restores(self) -> None:
        c = panel().controller

        c.choose(Picker.GROUND, ("GND",))

        assert c.ground.origin == "Your choice"
        assert c.ground.use_detected
        assert c.ground.chosen == ("GND",)

        c.use_detected()

        assert c.ground.chosen == ("GND", "GND_ISO")
        assert not c.ground.use_detected

    def test_choosing_none_goes_back_to_detection(self) -> None:
        c = panel().controller
        c.choose(Picker.GROUND, ("GND",))

        c.choose(Picker.GROUND, ())

        assert not c.ground.use_detected

    def test_a_board_switch_resets_to_detection_but_keeps_step_options(self) -> None:
        p = panel()
        c = p.controller
        c.choose(Picker.GROUND, ("GND",))
        c.set_fields(Step.RETURN, StepFields(distance=2.0))
        p.prober.answer = Probe(OTHER, nets_of(ZONES))

        c.activate()
        p.finish()

        assert not c.ground.use_detected
        assert c.ground.chosen == ("GND", "GND_ISO")
        assert c.fields(Step.RETURN).distance == 2.0

    def test_narrowing_ground_nets_drops_one_from_ground_stitching(self) -> None:
        c = panel().controller
        c.choose(Picker.STITCH_GROUND, ("GND", "GND_ISO"))

        c.choose(Picker.GROUND, ("GND",))

        assert c.picker(Picker.STITCH_GROUND).chosen == ("GND",)
        assert c.picker(Picker.STITCH_GROUND).candidates == ("GND",)

    def test_chosen_ground_nets_reach_the_runs(self) -> None:
        p = panel()
        p.controller.choose(Picker.GROUND, ("GND",))

        heatmap(p, Step.GROUND, GROUND_HEATMAP)

        (job,) = p.runner.jobs
        assert isinstance(job, StitchJob)
        assert job.options.ground_nets == ("GND",)
        assert job.options.stitch_class is StitchClass.GROUND

    def test_untouched_ground_nets_leave_detection_to_the_runs(self) -> None:
        p = panel()

        heatmap(p, Step.GROUND, GROUND_HEATMAP)
        place(p, Step.RETURN, return_result(placement=run_result(placed=1)))

        stitch, ret = p.runner.jobs
        assert isinstance(stitch, StitchJob) and isinstance(ret, ReturnJob)
        assert stitch.options.ground_nets is None
        assert ret.options.ground_nets is None

    def test_without_ground_nets_return_and_ground_stitching_wait(self) -> None:
        p = panel(probe=Prober(Probe(BOARD, nets_of({"+5V": OUTER}, ()))))
        c = p.controller

        assert c.ground.chosen == ()
        assert not c.step_enabled(Step.RETURN)
        assert not c.step_enabled(Step.GROUND)
        assert c.step_enabled(Step.POWER)
        assert c.step_enabled(Step.PAD_VIAS)
        for step in (Step.RETURN, Step.GROUND):
            assert c.status(step).note == "Needs ground nets: choose them at the top"
        assert (
            c.status(Step.POWER).note == "No ground net: stitches every net with pours"
        )
        assert c.status(Step.PAD_VIAS).note == ""
        assert not c.place(Step.RETURN)

    def test_power_stitching_without_ground_nets_detects_none(self) -> None:
        p = panel(probe=Prober(Probe(BOARD, nets_of({"+5V": OUTER}, ()))))

        place(p, Step.POWER, POWER_PLACED)

        (job,) = p.runner.jobs
        assert isinstance(job, StitchJob)
        assert job.options.ground_nets is None
        assert job.options.stitch_class is StitchClass.POWER


class TestPickers:
    def test_candidates_per_picker(self) -> None:
        c = panel().controller

        assert c.picker(Picker.PAD_VIAS).candidates == (
            "+3V3",
            "+5V",
            "GND",
            "GND_ISO",
            "VBUS",
        )
        assert c.picker(Picker.POWER).candidates == ("+3V3", "+5V", "VBUS")
        assert c.picker(Picker.STITCH_GROUND).candidates == ("GND", "GND_ISO")
        assert (
            c.picker(Picker.CLEAN_UP).candidates == c.picker(Picker.PAD_VIAS).candidates
        )

    def test_untouched_text_counts_the_candidates(self) -> None:
        c = panel().controller

        assert c.picker(Picker.POWER).text == "All power nets (3)"
        assert c.picker(Picker.STITCH_GROUND).text == "All ground nets (2)"
        assert c.picker(Picker.PAD_VIAS).text == "All nets with pours (5)"

    def test_chosen_text_lists_and_elides(self) -> None:
        c = panel().controller

        c.choose(Picker.POWER, ("+3V3", "+5V"))
        assert c.picker(Picker.POWER).text == "+3V3, +5V"

        c.choose(Picker.PAD_VIAS, ("+3V3", "+5V", "GND", "VBUS"))
        assert c.picker(Picker.PAD_VIAS).text == "+3V3, +5V, … (4)"

    def test_a_chosen_net_gone_from_the_board_is_dropped_and_named(self) -> None:
        p = panel()
        c = p.controller
        c.choose(Picker.POWER, ("+5V", "VBUS"))
        gone = dict(ZONES)
        del gone["VBUS"]
        p.prober.answer = Probe(BOARD, nets_of(gone))

        c.activate()
        p.finish()

        assert c.picker(Picker.POWER).chosen == ("+5V",)
        assert c.picker(Picker.POWER).gone == "VBUS is no longer on the board"

        c.choose(Picker.POWER, ("+5V", "+3V3"))
        assert c.picker(Picker.POWER).gone == ""

    def test_an_untouched_default_follows_the_board(self) -> None:
        p = panel()
        c = p.controller
        more = dict(ZONES, VCC=OUTER)
        p.prober.answer = Probe(BOARD, nets_of(more))

        c.activate()
        p.finish()

        assert c.picker(Picker.POWER).chosen is None
        assert c.picker(Picker.POWER).text == "All power nets (4)"

    def test_chosen_nets_reach_the_options_and_all_means_none(self) -> None:
        p = panel()
        p.controller.choose(Picker.PAD_VIAS, ("+5V",))
        place(p, Step.PAD_VIAS, PADS_PLACED)
        p.controller.choose(Picker.PAD_VIAS, None)
        place(p, Step.PAD_VIAS, PADS_PLACED)

        first, second = p.runner.jobs
        assert isinstance(first, PadJob) and isinstance(second, PadJob)
        assert first.options.nets == ("+5V",)
        assert second.options.nets is None


class TestFields:
    def test_defaults_per_step(self) -> None:
        c = panel().controller

        assert c.fields(Step.POWER) == StepFields(distance=10.0, fallback_via="0.5/0.3")
        assert c.fields(Step.RETURN) == StepFields(distance=1.5)
        assert c.fields(Step.GROUND) == StepFields(distance=3.0, fallback_via="0.5/0.3")
        assert c.fields(Step.PAD_VIAS) == StepFields()

    def test_non_finite_sizes_and_distances_are_invalid(self) -> None:
        c = panel().controller

        c.set_fields(Step.RETURN, StepFields(distance=float("nan"), via="0.6/nan"))

        assert set(c.field_errors(Step.RETURN)) == {Field.VIA, Field.DISTANCE}
        assert not c.step_enabled(Step.RETURN)

    def test_drill_not_smaller_than_the_diameter_disables_only_that_step(self) -> None:
        c = panel().controller

        c.set_fields(Step.RETURN, StepFields(distance=1.5, via="0.3/0.6"))

        assert c.field_errors(Step.RETURN) == {
            Field.VIA: "Drill must be smaller than the diameter"
        }
        assert not c.step_enabled(Step.RETURN)
        assert c.step_enabled(Step.POWER)
        assert c.cleanup_enabled
        assert not c.place(Step.RETURN)

    @pytest.mark.parametrize(
        ("text", "message"),
        [
            ("0.6", "Expected diameter/drill, e.g. 0.6/0.3"),
            ("a/b", "Expected diameter/drill, e.g. 0.6/0.3"),
            ("0.6/0", "Sizes must be more than 0"),
        ],
    )
    def test_invalid_via_sizes(self, text: str, message: str) -> None:
        c = panel().controller

        c.set_fields(Step.PAD_VIAS, StepFields(via=text))

        assert c.field_errors(Step.PAD_VIAS) == {Field.VIA: message}

    def test_invalid_fallback_and_distance(self) -> None:
        c = panel().controller

        c.set_fields(Step.GROUND, StepFields(distance=0.0, fallback_via="0.3/0.5"))

        assert c.field_errors(Step.GROUND) == {
            Field.DISTANCE: "Distance must be more than 0",
            Field.FALLBACK_VIA: "Drill must be smaller than the diameter",
        }

    def test_sizes_reach_the_options_and_empty_means_default(self) -> None:
        p = panel()
        p.controller.set_fields(
            Step.POWER, StepFields(distance=10.0, via="0.8/0.4", fallback_via="")
        )

        place(p, Step.POWER, POWER_PLACED)

        (job,) = p.runner.jobs
        assert isinstance(job, StitchJob)
        assert job.options.via == ViaSize(0.8, 0.4)
        assert job.options.fallback_via == ViaSize(0.5, 0.3)
        assert job.options.stitch_class is StitchClass.POWER

    def test_an_empty_return_vias_distance_is_the_default(self) -> None:
        p = panel()
        p.controller.set_fields(Step.RETURN, StepFields(distance=None))

        place(p, Step.RETURN, return_result(placement=run_result(placed=1)))

        (job,) = p.runner.jobs
        assert isinstance(job, ReturnJob)
        assert job.options.distance == DEFAULT_DISTANCE

    def test_force_reaches_return_vias(self) -> None:
        p = panel()
        p.controller.set_fields(Step.RETURN, StepFields(distance=1.5, force=True))

        place(p, Step.RETURN, return_result(placement=run_result(placed=1)))

        (job,) = p.runner.jobs
        assert isinstance(job, ReturnJob)
        assert job.options.force

    def test_restore_defaults_resets_the_steps_fields_and_nets(self) -> None:
        c = panel().controller
        c.set_fields(Step.POWER, StepFields(distance=4.0, via="0.3/0.6"))
        c.choose(Picker.POWER, ("+5V",))
        c.set_fields(Step.RETURN, StepFields(distance=2.0))

        c.restore_defaults(Step.POWER)

        assert c.fields(Step.POWER) == StepFields(distance=10.0, fallback_via="0.5/0.3")
        assert c.picker(Picker.POWER).chosen is None
        assert c.fields(Step.RETURN).distance == 2.0

    def test_fields_are_read_only_while_running(self) -> None:
        p = panel(POWER_PLACED)
        p.controller.place(Step.POWER)

        p.controller.set_fields(Step.POWER, StepFields(distance=1.0))
        p.controller.choose(Picker.POWER, ("+5V",))
        p.controller.set_refill(False)

        assert p.controller.fields(Step.POWER).distance == 10.0
        assert p.controller.picker(Picker.POWER).chosen is None
        assert p.controller.refill
        p.finish()


VIAS = RemovableVias(
    nets=None,
    counts={"GND": 240, "GND_ISO": 20, "+3V3": 40, "+5V": 12},
    unfilled_zones={},
    ids=frozenset(str(i) for i in range(312)),
)


class TestRemoveVias:
    def test_counts_then_asks_then_removes(self) -> None:
        p = panel()
        place(p, Step.PAD_VIAS, PADS_PLACED)
        p.controller.choose(Picker.CLEAN_UP, ("GND", "+5V"))
        p.runner.outcome = VIAS
        p.runner.stages = ()
        c = p.controller

        assert c.remove_vias()
        assert c.activity is not None
        assert c.activity.headline == "Counting stitching vias…"
        p.finish()

        count = p.runner.jobs[-1]
        assert isinstance(count, CountJob)
        assert count.nets == ("+5V", "GND")
        assert count.refill
        confirm = c.confirmation
        assert confirm is not None
        assert confirm.headline == "312 vias will be removed"
        assert confirm.button == "Remove 312 vias"
        assert not c.step_enabled(Step.PAD_VIAS)
        assert not c.cleanup_enabled

        p.runner.outcome = Cleared(vias=312, commit=CommitOutcome.LANDED, refilled=True)
        assert c.confirm_removal()
        p.finish()

        remove = p.runner.jobs[-1]
        assert isinstance(remove, RemoveJob)
        assert remove.vias is VIAS
        assert c.confirmation is None
        assert c.outcome is not None
        assert (c.outcome.icon, c.outcome.headline, c.outcome.body) == (
            Icon.DONE,
            "Removed 312 vias.",
            "Zones refilled.",
        )
        assert c.status(Step.PAD_VIAS).detail == (
            "Place · just now (markers since cleared)"
        )

    def test_without_refills_says_nothing_of_them(self) -> None:
        p = panel(VIAS)
        p.runner.stages = ()
        p.controller.set_refill(False)
        p.controller.remove_vias()
        p.finish()
        p.runner.outcome = Cleared(vias=312, commit=CommitOutcome.LANDED)

        p.controller.confirm_removal()
        p.finish()

        remove = p.runner.jobs[-1]
        assert isinstance(remove, RemoveJob)
        assert not remove.refill
        assert p.controller.outcome is not None
        assert p.controller.outcome.body == ""

    def test_zero_found_needs_no_dialog(self) -> None:
        p = panel(
            RemovableVias(nets=None, counts={}, unfilled_zones={}, ids=frozenset())
        )
        p.runner.stages = ()

        p.controller.remove_vias()
        p.finish()

        assert p.controller.confirmation is None
        assert p.controller.outcome is not None
        assert p.controller.outcome.headline == (
            "No stitching vias to remove on these nets."
        )
        assert p.controller.cleanup_enabled

    def test_declining_removes_nothing(self) -> None:
        p = panel(VIAS)
        p.runner.stages = ()
        p.controller.remove_vias()
        p.finish()

        p.controller.decline_removal()

        assert p.controller.confirmation is None
        assert [type(j) for j in p.runner.jobs] == [CountJob]
        assert p.controller.cleanup_enabled

    def test_cancel_during_counting_discards_the_count(self) -> None:
        p = panel(VIAS)
        p.runner.stages = ()
        c = p.controller
        c.remove_vias()

        c.cancel()
        p.finish()

        assert c.confirmation is None
        assert c.outcome is not None
        assert c.outcome.headline == "Cancelled — no vias removed."
        assert [type(j) for j in p.runner.jobs] == [CountJob]
        assert c.cleanup_enabled

    def test_an_unknown_removal_is_a_lost_connection(self) -> None:
        p = panel(VIAS)
        p.runner.stages = ()
        p.controller.remove_vias()
        p.finish()
        p.runner.outcome = Cleared(vias=0, commit=CommitOutcome.UNKNOWN)

        p.controller.confirm_removal()
        p.finish()

        assert p.controller.outcome is not None
        assert p.controller.outcome.headline == "Lost the connection to KiCad"
        assert p.controller.connection is Connection.NO_KICAD

    def test_a_removal_runs_on_the_counted_board(self) -> None:
        p = panel(VIAS)
        p.runner.stages = ()
        p.controller.remove_vias()
        p.finish()
        p.runner.outcome = Cleared(vias=312, commit=CommitOutcome.LANDED)

        p.controller.confirm_removal()
        p.finish()

        assert p.runner.boards == [BOARD.identity, BOARD.identity]


class TestClose:
    def test_closing_while_idle_closes(self) -> None:
        assert panel().controller.close()

    def test_closing_mid_run_asks_then_cancels_and_closes_after(self) -> None:
        p = panel(POWER_PLACED)
        c = p.controller
        c.place(Step.POWER)
        assert c.close_asks

        assert not c.close()
        assert c.activity is not None
        assert c.activity.cancel_label == "Cancelling…"
        p.finish()

        assert p.closed == [True]

    def test_closing_while_committing_waits_without_asking(self) -> None:
        p = panel()
        p.runner.stages = ((Stage.COMMIT, None),)
        asked = []
        p.runner.during = lambda stage: asked.append(p.controller.close_asks)

        place(p, Step.POWER, POWER_PLACED)

        assert asked == [False]

    def test_closing_while_committing_waits_for_the_push_not_the_refill(
        self,
    ) -> None:
        p = panel()
        p.runner.stages = ((Stage.COMMIT, None), (Stage.REFILL, None))
        seen = []

        def during(stage: Stage) -> None:
            if stage is Stage.COMMIT:
                assert not p.controller.close()
            seen.append((stage, list(p.closed)))

        p.runner.during = during
        place(p, Step.POWER, POWER_PLACED)

        assert seen == [(Stage.COMMIT, []), (Stage.REFILL, [True])]
        assert p.closed == [True]

    def test_closing_during_the_refill_after_the_commit_closes_at_once(
        self,
    ) -> None:
        p = panel()
        p.runner.stages = ((Stage.COMMIT, None), (Stage.REFILL, None))
        answers = []
        p.runner.during = lambda stage: (
            answers.append(p.controller.close()) if stage is Stage.REFILL else None
        )

        place(p, Step.POWER, POWER_PLACED)

        assert answers == [True]
        assert p.closed == []


class TestRunJob:
    @pytest.fixture
    def calls(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
        seen: list[tuple[str, dict]] = []

        def record(name: str) -> Callable[..., str]:
            def call(*args: object, **kwargs: object) -> str:
                seen.append((name, {"args": args, **kwargs}))
                return name

            return call

        for name in ("run_placement", "find_removable_vias", "clear"):
            monkeypatch.setattr(controller_module, name, record(name))
        monkeypatch.setattr(
            controller_module.pourstitch_command, "execute", record("pourstitch")
        )
        monkeypatch.setattr(
            controller_module.returnvia_command, "execute", record("returnvia")
        )
        return seen

    @staticmethod
    def run(job: Job, board: str = BOARD.identity) -> object:
        kicad = FakeKiCad(FakeBoard())
        return run_job(job, board, PROGRESS, make=lambda: kicad)  # type: ignore[arg-type,return-value]

    def test_refuses_when_kicad_shows_another_board(self, calls) -> None:
        with pytest.raises(BoardSwitchedError):
            self.run(ClearJob(), OTHER.identity)

        assert calls == []

    def test_pad_vias_place_with_refills_off(self, calls) -> None:
        options = PadviaOptions(nets=("+5V",))

        assert self.run(PadJob(options, refill=False)) == "run_placement"

        ((_, call),) = calls
        assert isinstance(call["args"][0], PadviaPlanner)
        assert call["args"][1] is options
        assert call["args"][2].refill is False
        assert call["progress"] is PROGRESS

    def test_stitching_heatmap_checks_with_an_overlay(self, calls) -> None:
        self.run(StitchJob(pourstitch.Options(), refill=True, heatmap=True))

        ((name, call),) = calls
        assert name == "pourstitch"
        assert call["check"] and call["args"][1].overlay
        assert call["refill"]

    def test_stitching_place_places_no_heatmap(self, calls) -> None:
        self.run(StitchJob(pourstitch.Options(), refill=True, heatmap=False))

        ((_, call),) = calls
        assert not call["check"] and not call["args"][1].overlay

    @pytest.mark.parametrize(
        ("view", "check", "full_map"),
        [
            (None, False, False),
            (View.UNSERVED, True, False),
            (View.ALL_COPPER, True, True),
        ],
    )
    def test_return_vias_per_view(self, calls, view, check, full_map) -> None:
        self.run(ReturnJob(ReturnViaOptions(), refill=False, view=view))

        ((name, call),) = calls
        assert name == "returnvia"
        assert (call["check"], call["full_map"], call["args"][1].overlay) == (
            check,
            full_map,
            check,
        )
        assert call["refill"] is False

    def test_count_remove_and_clear(self, calls) -> None:
        self.run(CountJob(("GND",), refill=False))
        self.run(RemoveJob(VIAS, refill=True))
        self.run(ClearJob())

        count, remove, cleared = calls
        assert count[1]["args"][2:] == (("GND",), False)
        assert remove[1]["args"][3] is VIAS and remove[1]["refill"]
        assert cleared[0] == "clear" and len(cleared[1]["args"]) == 3


class TestLogCapture:
    def test_details_hold_only_the_workers_lines(self) -> None:
        p = panel()

        def other_thread_logs(stage: Stage) -> None:
            t = threading.Thread(target=lambda: logger.info("from another thread"))
            t.start()
            t.join()

        p.runner.during = other_thread_logs
        p.runner.log = ("from the run",)

        place(p, Step.PAD_VIAS, PADS_PLACED)

        assert p.controller.details == "from the run"


def test_pickers_are_empty_while_disconnected() -> None:
    p = panel()

    place(p, Step.POWER, KiCadConnectionError("gone"))

    c = p.controller
    assert (c.picker(Picker.POWER).text, c.picker(Picker.POWER).candidates) == ("", ())
    assert c.ground.text == ""
    assert not c.ground.enabled


def test_pad_vias_has_no_heatmap() -> None:
    p = panel()

    assert not p.controller.heatmap(Step.PAD_VIAS)
    assert p.spawned.waiting == []
