"""Fakes and builders the panel's test modules share."""

from dataclasses import dataclass, field

import kipy.board_types as kbt
from kipy.board_types import Net
from kipy.errors import ApiError
from kipy.proto.common.envelope_pb2 import ApiStatusCode
from kipy.proto.common.types.base_types_pb2 import DocumentSpecifier

from kistitch import drc
from kistitch.commit import CommitOutcome
from kistitch.placement import RunResult
from kistitch.plan import Plan, PlannedUnit, UnfixableSpot
from kistitch.pourstitch import NetCoverage
from kistitch.pourstitch_command import StitchResult
from kistitch.returnvia_command import ReturnResult
from kistitch.snapshot import Snapshot, ViaSize

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
