"""Fakes and builders the panel's test modules share."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import kipy.board_types as kbt
from kipy.board_types import Net
from kipy.errors import ApiError
from kipy.proto.common.envelope_pb2 import ApiStatusCode
from kipy.proto.common.types.base_types_pb2 import DocumentSpecifier
from loguru import logger
from shapely.geometry import Polygon

from kistitch import drc
from kistitch.commit import CommitOutcome
from kistitch.panel.controller import Job
from kistitch.panel.probe import BoardId, BoardNets, Probe
from kistitch.placement import RunResult
from kistitch.plan import Plan, PlannedUnit, UnfixableSpot
from kistitch.pourstitch import NetCoverage
from kistitch.pourstitch_command import StitchResult
from kistitch.progress import Progress, Stage
from kistitch.returnvia_command import ReturnResult
from kistitch.snapshot import Snapshot, ViaSize, Zone

BOARD_FILE = "main.kicad_pcb"
PROJECT = "/work/main"


def busy() -> ApiError:
    return ApiError("busy", code=ApiStatusCode.AS_BUSY)


@dataclass
class FakeBoard:
    zones: list[kbt.Zone] = field(default_factory=list)
    nets: list[str] = field(default_factory=list)
    filename: str = BOARD_FILE
    project: str = PROJECT

    @property
    def document(self) -> DocumentSpecifier:
        d = DocumentSpecifier()
        d.board_filename = self.filename
        d.project.path = self.project
        return d

    @property
    def name(self) -> str:
        return self.filename

    def get_zones(self) -> list[kbt.Zone]:
        return self.zones

    def get_nets(self) -> list[Net]:
        out = []
        for n in self.nets:
            net = Net()
            net.name = n
            out.append(net)
        return out


@dataclass
class FakeKiCad:
    board: FakeBoard | None
    error: Exception | None = None

    def get_board(self) -> FakeBoard:
        if self.error is not None:
            raise self.error
        if self.board is None:
            raise ApiError("Expected to be able to retrieve at least one board")
        return self.board


VIA = ViaSize(0.6, 0.3)


def run_result(
    placed: int = 0,
    unfixable: int = 0,
    splits: int = 0,
    commit: CommitOutcome = CommitOutcome.LANDED,
    forced: int = 0,
) -> RunResult:
    units = tuple(
        PlannedUnit((float(i), 0.0), "GND", VIA, forced=i < forced)
        for i in range(placed)
    )
    spots = tuple(
        UnfixableSpot((float(i), 5.0), "GND", "no room") for i in range(unfixable)
    )
    return RunResult(
        Snapshot(copper_layers=("F.Cu", "B.Cu")),
        drc.PruneResult(Plan(units, spots), (), (), (), 1),
        commit=commit if placed or unfixable or splits else CommitOutcome.NOTHING,
        splits=splits,
    )


def stitch_result(
    uncovered: tuple[float, ...] = (0.0,),
    unstitchable: float = 0.0,
    distance: float = 10.0,
    placement: RunResult | None = None,
    enabled: tuple[str, ...] = (),
    placed_overlays: bool = True,
) -> StitchResult:
    coverage = tuple(
        NetCoverage(f"N{i}", u, unstitchable if i == 0 else 0.0)
        for i, u in enumerate(uncovered)
    )
    return StitchResult(
        distance=distance,
        coverage=coverage,
        placement=placement,
        layers_enabled=enabled,
        overlays_placed=placed_overlays and bool(coverage),
    )


def return_result(
    unserved: int = 0,
    total: int = 230,
    placement: RunResult | None = None,
    full_map: bool = False,
    placed_overlays: bool = True,
) -> ReturnResult:
    return ReturnResult(
        distance=1.5,
        unserved=unserved,
        total=total,
        placement=placement,
        unfixable=0 if placement is None else placement.unfixable,
        forced=0
        if placement is None
        else sum(u.forced for u in placement.prune.plan.units),
        full_map=full_map,
        layers_enabled=(),
        overlays_placed=placed_overlays and total > 0 and (full_map or unserved > 0),
    )


START = datetime(2026, 11, 17, 14, 2, 31)
BOARD = BoardId(f"{PROJECT}/{BOARD_FILE}", BOARD_FILE)
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
