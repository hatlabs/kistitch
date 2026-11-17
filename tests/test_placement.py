import dataclasses
import os
import sys
import tempfile
import time
from datetime import datetime
from itertools import pairwise

import kipy.board_types
import pytest
from kipy.board import BoardLayer as BL
from kipy.board_types import BoardSegment, unwrap
from kipy.errors import ApiError, ConnectionError
from kipy.geometry import Vector2
from kipy.proto.common import base_types_pb2
from kipy.proto.common.commands.editor_commands_pb2 import (
    CreateItemsResponse,
    ItemStatusCode,
)
from kipy.proto.common.envelope_pb2 import ApiStatusCode
from kipy.proto.common.types import DocumentSpecifier
from loguru import logger

from kistitch import commit, drc, placement
from kistitch.commit import CommitOutcome, Marker, MarkerKind
from kistitch.plan import Plan, PlannedUnit, Stub, UnfixableSpot
from kistitch.progress import Cancelled, Stage
from kistitch.returnpath import NoBoardOutlineError
from kistitch.snapshot import NM_PER_MM, Snapshot, ViaSize

VIA = ViaSize(0.6, 0.3)
REAL_DRC_RUNNER = drc.DrcRunner
BOARD_CHANGES = {
    "refill_zones",
    "begin_commit",
    "create_items",
    "push_commit",
    "remove_items",
}


SQUARE = [(0, 0), (20, 0), (20, 20), (0, 20), (0, 0)]


def edge_cuts(corners):
    """Edge.Cuts segments joining `corners` in order."""
    segments = []
    for a, b in pairwise(corners):
        seg = BoardSegment()
        seg.layer = BL.BL_Edge_Cuts
        seg.start = Vector2.from_xy(round(a[0] * NM_PER_MM), round(a[1] * NM_PER_MM))
        seg.end = Vector2.from_xy(round(b[0] * NM_PER_MM), round(b[1] * NM_PER_MM))
        segments.append(seg)
    return segments


def net(name):
    n = kipy.board_types.Net()
    n.name = name
    return n


class FakeClient:
    """Answers CreateItems the way KiCad does: one status per item."""

    def __init__(self, board):
        self.board = board

    def send(self, command, response_type):
        assert response_type is CreateItemsResponse
        self.board.calls.append("create_items")
        items = [unwrap(a) for a in command.items]
        self.board.created.append(items)
        response = CreateItemsResponse()
        for packed, item in zip(command.items, items):
            result = response.created_items.add()
            result.item.CopyFrom(packed)
            if self.board.refuse(item):
                result.status.code = ItemStatusCode.ISC_INVALID_DATA
                result.status.error_message = "no overlapping layers"
            else:
                result.status.code = ItemStatusCode.ISC_OK
        return response


class RecordingBoard:
    name = "b.kicad_pcb"
    document = DocumentSpecifier()

    def __init__(self, refuse=lambda item: False, project_dir=""):
        self.calls = []
        self.refuse = refuse
        self.project_dir = project_dir
        self.created = []
        self.client = FakeClient(self)
        # Successive get_as_string results; the last one repeats.
        self.texts = ["(kicad_pcb)"]
        self.edges = edge_cuts(SQUARE)
        self.shapes = []
        self.removed = []
        self.visible = [BL.BL_F_Cu, BL.BL_Edge_Cuts]

    def __getattr__(self, attr):
        def record(*args, **kwargs):
            self.calls.append(attr)

        return record

    def get_nets(self):
        return [net("GND"), net("+3.3V")]

    def get_as_string(self):
        return self.texts.pop(0) if len(self.texts) > 1 else self.texts[0]

    def get_shapes(self):
        return [*self.edges, *self.shapes]

    def remove_items(self, items):
        self.calls.append("remove_items")
        self.removed.extend(items)

    def get_layer_by_name(self, name):
        return {"F.Cu": BL.BL_F_Cu, "B.Cu": BL.BL_B_Cu}[name]

    def get_project(self):
        return type("P", (), {"path": self.project_dir})()

    def begin_commit(self):
        self.calls.append("begin_commit")
        return "commit"

    def push_commit(self, c, message=""):
        self.calls.append(("push_commit", message))

    def drop_commit(self, c):
        self.calls.append("drop_commit")

    def refill_zones(self, block=True, max_poll_seconds=0):
        self.calls.append("refill_zones")

    def get_visible_layers(self):
        return list(self.visible)

    def set_visible_layers(self, layers):
        self.calls.append(("set_visible_layers", list(layers)))
        self.visible = list(layers)


class FakeKiCad:
    def __init__(self, board, cli_path=""):
        self.board = board
        self.cli_path = cli_path
        self.pings = []

    def get_board(self):
        return self.board

    def get_kicad_binary_path(self, name):
        return self.cli_path

    def ping(self):
        result = self.pings.pop(0) if self.pings else None
        if isinstance(result, Exception):
            raise result


def unit(x, y, net="GND", stub=None):
    return PlannedUnit(position=(x, y), net=net, size=VIA, stub=stub)


def never_plan(snapshot, options, forbidden):
    raise AssertionError("planner must not run")


def board_changes(board):
    return [
        c for c in board.calls if (c[0] if isinstance(c, tuple) else c) in BOARD_CHANGES
    ]


# ---- fail before any board change ----------------------------------------------


def test_missing_kicad_cli_fails_before_any_board_change(monkeypatch):
    monkeypatch.setattr(drc.shutil, "which", lambda name: None)
    monkeypatch.setattr(drc, "KNOWN_KICAD_CLI_PATHS", ())
    board = RecordingBoard()

    with pytest.raises(drc.KiCadCliNotFound):
        placement.run_placement(
            never_plan, None, placement.RunSettings("test"), FakeKiCad(board)
        )

    assert board_changes(board) == []


def test_open_board_outline_fails_before_any_board_change(monkeypatch):
    monkeypatch.setattr(drc.shutil, "which", lambda name: None)
    monkeypatch.setattr(drc, "KNOWN_KICAD_CLI_PATHS", ())
    board = RecordingBoard()
    board.edges = edge_cuts(SQUARE[:-1])

    with pytest.raises(NoBoardOutlineError):
        placement.run_placement(
            never_plan, None, placement.RunSettings("test"), FakeKiCad(board)
        )

    assert board_changes(board) == []


def test_open_outline_around_a_closed_cutout_fails_before_any_board_change(
    monkeypatch,
):
    monkeypatch.setattr(drc.shutil, "which", lambda name: None)
    monkeypatch.setattr(drc, "KNOWN_KICAD_CLI_PATHS", ())
    board = RecordingBoard()
    cutout = [(8, 8), (12, 8), (12, 12), (8, 12), (8, 8)]
    board.edges = [*edge_cuts(SQUARE[:-1]), *edge_cuts(cutout)]

    with pytest.raises(NoBoardOutlineError):
        placement.run_placement(
            never_plan, None, placement.RunSettings("test"), FakeKiCad(board)
        )

    assert board_changes(board) == []


def test_unsaved_board_fails_before_any_board_change(tmp_path):
    cli = tmp_path / "kicad-cli"
    cli.write_text("#!/bin/sh\necho 10.0.7\n")
    cli.chmod(0o755)
    board = RecordingBoard(project_dir="")

    with pytest.raises(drc.ProjectFilesError):
        placement.run_placement(
            never_plan, None, placement.RunSettings("test"), FakeKiCad(board, str(cli))
        )

    assert board_changes(board) == []


# ---- run ------------------------------------------------------------------------


class FakeDrcRunner:
    """Reports a clearance violation on every planned via at a bad position,
    on top of the `base` report."""

    base: tuple[drc.Violation, ...] = ()

    def __init__(self, kicad_cli, project, board_text, workdir, progress=None):
        self.bad = {(5, 5)}
        self.planned: list[drc.Violation] = []

    def baseline(self):
        return list(self.base)

    def report(self, plan):
        return [
            *self.base,
            *(
                drc.Violation(
                    "clearance",
                    "c",
                    "error",
                    (drc.ViolationItem("Via [GND]", u.position),),
                )
                for u in plan.units
                if u.position in self.bad
            ),
        ]

    def new_violations(self, plan):
        self.planned = self.report(plan)
        return drc.new_violations(self.base, self.planned)


def plan_at(*positions, unfixable=()):
    def planner(snapshot, options, forbidden):
        return Plan(
            units=tuple(unit(*p) for p in positions if p not in forbidden),
            unfixable=unfixable,
        )

    return planner


@pytest.fixture
def saved_board(tmp_path, monkeypatch):
    """A RecordingBoard saved to disk, with kicad-cli and DRC faked."""
    cli = tmp_path / "kicad-cli"
    cli.write_text("#!/bin/sh\necho 10.0.7\n")
    cli.chmod(0o755)
    (tmp_path / "b.kicad_pcb").write_text("")
    (tmp_path / "b.kicad_pro").write_text("{}")
    monkeypatch.setattr(
        placement.Snapshot,
        "from_board",
        staticmethod(lambda board: Snapshot(copper_layers=("F.Cu", "B.Cu"))),
    )
    monkeypatch.setattr(drc, "DrcRunner", FakeDrcRunner)
    board = RecordingBoard(project_dir=str(tmp_path))
    return board, FakeKiCad(board, str(cli))


def pushes(board):
    return [c for c in board.calls if isinstance(c, tuple) and c[0] == "push_commit"]


def test_run_commits_the_pruned_units_in_one_commit(saved_board):
    board, kicad = saved_board

    result = placement.run_placement(
        plan_at((1, 1), (5, 5)), None, placement.RunSettings("test"), kicad
    )

    assert [u.position for u in result.prune.plan.units] == [(1, 1)]
    assert [d.unit.position for d in result.prune.dropped] == [(5, 5)]
    assert board.calls.count("begin_commit") == 1 and len(pushes(board)) == 1
    (placed,) = board.created
    assert [(v.position.x, v.position.y) for v in placed] == [(1_000_000, 1_000_000)]
    assert result.commit is CommitOutcome.LANDED
    assert result.placed == 1
    assert result.splits == 0


def test_dry_run_commits_nothing(saved_board):
    board, kicad = saved_board

    result = placement.run_placement(
        plan_at((1, 1)), None, placement.RunSettings("test", dry_run=True), kicad
    )

    assert [u.position for u in result.prune.plan.units] == [(1, 1)]
    assert "begin_commit" not in board.calls and pushes(board) == []
    assert board.created == []
    assert result.commit is CommitOutcome.NOTHING
    assert result.placed == 0


def test_empty_plan_without_markers_commits_nothing(saved_board):
    board, kicad = saved_board
    unfixable = (UnfixableSpot((3, 3), "GND", "no room"),)

    result = placement.run_placement(
        plan_at(unfixable=unfixable),
        None,
        placement.RunSettings("test", mark_unfixable=False),
        kicad,
    )

    assert "begin_commit" not in board.calls and pushes(board) == []
    assert result.commit is CommitOutcome.NOTHING
    assert result.placed == 0


def test_run_whose_push_loses_the_connection_reports_an_unknown_outcome(
    saved_board,
):
    board, kicad = saved_board

    def lost(c, message=""):
        board.calls.append(("push_commit", message))
        raise ConnectionError("timed out")

    board.push_commit = lost

    result = placement.run_placement(
        plan_at((1, 1)), None, placement.RunSettings("test"), kicad
    )

    assert result.commit is CommitOutcome.UNKNOWN
    assert result.placed == 0
    assert board.calls.count("refill_zones") == 1  # before the snapshot only


def eco2_cross():
    return commit.eco2_segment((7, 7), (8, 8))


def test_run_replaces_earlier_markers_in_the_same_commit(saved_board):
    board, kicad = saved_board
    stale = eco2_cross()
    other = commit.eco2_segment((0, 0), (1, 1))
    other.layer = BL.BL_F_SilkS
    board.shapes = [stale, other]
    unfixable = (UnfixableSpot((3, 3), "GND", "no room"),)

    placement.run_placement(
        plan_at((1, 1), unfixable=unfixable), None, placement.RunSettings("t"), kicad
    )

    assert board.removed == [stale]
    assert board.calls.count("begin_commit") == 1 and len(pushes(board)) == 1
    begin, push = board.calls.index("begin_commit"), board.calls.index(pushes(board)[0])
    assert begin < board.calls.index("remove_items") < push
    placed, marks = board.created
    assert len(placed) == 1 and all(m.layer == BL.BL_Eco2_User for m in marks)


def test_run_with_nothing_to_place_still_removes_earlier_markers(saved_board):
    board, kicad = saved_board
    stale = eco2_cross()
    board.shapes = [stale]

    placement.run_placement(plan_at(), None, placement.RunSettings("t"), kicad)

    assert board.removed == [stale] and board.created == []
    assert board.calls.count("begin_commit") == 1 and len(pushes(board)) == 1


def test_planner_error_keeps_earlier_markers_and_commits_nothing(saved_board):
    board, kicad = saved_board
    board.shapes = [eco2_cross()]

    with pytest.raises(ValueError, match="no nets"):
        placement.run_placement(no_nets, None, placement.RunSettings("t"), kicad)

    assert board.removed == [] and "begin_commit" not in board.calls


def no_nets(snapshot, options, forbidden):
    raise ValueError("no nets")


def test_dry_run_keeps_earlier_markers(saved_board):
    board, kicad = saved_board
    board.shapes = [eco2_cross()]

    placement.run_placement(
        plan_at((1, 1)), None, placement.RunSettings("t", dry_run=True), kicad
    )

    assert board.removed == [] and "remove_items" not in board.calls
    assert "begin_commit" not in board.calls


def test_run_with_nothing_to_place_and_no_markers_commits_nothing(saved_board):
    board, kicad = saved_board
    board.shapes = [commit.eco2_segment((0, 0), (1, 1))]
    board.shapes[0].layer = BL.BL_F_SilkS

    placement.run_placement(plan_at(), None, placement.RunSettings("t"), kicad)

    assert "begin_commit" not in board.calls and board.removed == []


def test_board_edited_during_the_run_is_not_committed(saved_board):
    board, kicad = saved_board
    board.texts = ["(kicad_pcb)", "(kicad_pcb (edited))"]

    with pytest.raises(placement.BoardChangedError, match="run again"):
        placement.run_placement(
            plan_at((1, 1)), None, placement.RunSettings("test"), kicad
        )

    assert "begin_commit" not in board.calls and board.created == []
    assert board.removed == []


def test_board_changed_after_an_unconfirmed_refill_names_the_refill(
    saved_board, monkeypatch
):
    board, kicad = saved_board
    board.texts = ["(kicad_pcb)", "(kicad_pcb (refilled))"]
    monkeypatch.setattr(placement, "refill_tolerant", lambda kicad, board, **_: False)

    with pytest.raises(placement.BoardChangedError, match="zone refill"):
        placement.run_placement(
            plan_at((1, 1)), None, placement.RunSettings("test"), kicad
        )


def test_board_changed_after_a_confirmed_refill_does_not_blame_it(saved_board):
    board, kicad = saved_board
    board.texts = ["(kicad_pcb)", "(kicad_pcb (edited))"]

    with pytest.raises(placement.BoardChangedError) as error:
        placement.run_placement(
            plan_at((1, 1)), None, placement.RunSettings("test"), kicad
        )

    assert "refill" not in str(error.value)


NO_SPLITS = placement.RunSettings("test", allow_splits=False)


def split_runner(violation_type):
    """A DRC runner reporting a connectivity violation no planned via is named in."""

    class SplitRunner(FakeDrcRunner):
        def report(self, plan):
            return [
                drc.Violation(
                    violation_type,
                    "split",
                    "error",
                    (drc.ViolationItem("Pad 1 [+5V] of J1 on F.Cu", (20, 20)),),
                )
            ]

    return SplitRunner


@pytest.mark.parametrize("violation_type", ["unconnected_items", "isolated_copper"])
def test_unattributed_split_refuses_to_commit_when_splits_are_not_allowed(
    saved_board, monkeypatch, violation_type
):
    monkeypatch.setattr(drc, "DrcRunner", split_runner(violation_type))
    board, kicad = saved_board

    with pytest.raises(placement.SplitError, match="without --no-allow-splits"):
        placement.run_placement(plan_at((1, 1)), None, NO_SPLITS, kicad)

    assert "begin_commit" not in board.calls and board.created == []


@pytest.mark.parametrize("violation_type", ["unconnected_items", "isolated_copper"])
def test_split_commits_with_a_marker_at_the_split_by_default(
    saved_board, monkeypatch, log, violation_type
):
    monkeypatch.setattr(drc, "DrcRunner", split_runner(violation_type))
    board, kicad = saved_board

    result = placement.run_placement(
        plan_at((1, 1)), None, placement.RunSettings("test"), kicad
    )

    assert any("committing anyway" in line for line in log)

    assert len(pushes(board)) == 1
    _, marks = board.created
    assert len(marks) == 4  # one square
    assert placement.markers_for(result.prune) == (
        Marker((20, 20), MarkerKind.UNATTRIBUTED),
    )
    assert commit.MARKER_LAYER in board.visible
    assert result.splits == 1


@pytest.mark.parametrize(
    "allow_splits, outcome, level",
    [
        (True, "would commit with markers", "WARNING"),
        (False, "would refuse to commit", "ERROR"),
    ],
)
def test_dry_run_reports_a_split_without_failing(
    saved_board, monkeypatch, allow_splits, outcome, level
):
    monkeypatch.setattr(drc, "DrcRunner", split_runner("unconnected_items"))
    board, kicad = saved_board
    settings = placement.RunSettings("test", dry_run=True, allow_splits=allow_splits)
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(str(m)), format="{level.name} {message}")
    try:
        result = placement.run_placement(plan_at((1, 1)), None, settings, kicad)
    finally:
        logger.remove(sink)

    assert [v.type for v in result.prune.unattributed] == ["unconnected_items"]
    assert "begin_commit" not in board.calls
    assert any(
        line.startswith(f"{level} ") and f"a real run {outcome}" in line
        for line in lines
    )
    assert any(line.startswith(f"{level}   connectivity:") for line in lines)


def test_unattributed_non_connectivity_violation_still_commits(
    saved_board, monkeypatch
):
    monkeypatch.setattr(drc, "DrcRunner", split_runner("copper_sliver"))
    board, kicad = saved_board

    placement.run_placement(plan_at((1, 1)), None, NO_SPLITS, kicad)

    assert len(pushes(board)) == 1


def unconnected(*pads):
    return drc.Violation(
        "unconnected_items",
        "Missing connection",
        "error",
        tuple(drc.ViolationItem(f"Pad {n} [+3V3] of U1", pos) for n, pos in pads),
    )


BASE_UNCONNECTED = unconnected(("A", (1, 2)), ("B", (3, 4)))


def test_non_connectivity_violation_on_a_split_net_is_not_counted(
    saved_board, monkeypatch, log
):
    sliver = drc.Violation(
        "copper_sliver",
        "Copper sliver",
        "warning",
        (drc.ViolationItem("Zone [+3V3] on In1.Cu", (5, 6)),),
    )
    monkeypatch.setattr(drc, "DrcRunner", reporting([], [sliver, BASE_UNCONNECTED]))
    _, kicad = saved_board

    placement.run_placement(plan_at((1, 1)), None, placement.RunSettings("test"), kicad)

    assert any("1 new connectivity violations on +3V3" in line for line in log)
    assert not any("connectivity:" in line and "sliver" in line for line in log)


def reporting(base, planned):
    class ReportRunner(FakeDrcRunner):
        def report(self, plan):
            return list(planned)

    ReportRunner.base = tuple(base)
    return ReportRunner


def test_existing_unconnected_item_that_moved_is_not_a_split(saved_board, monkeypatch):
    # A refill around the new vias re-anchors an existing unconnected item:
    # same net, same count, different items and positions.
    moved = unconnected(("C", (7, 8)), ("D", (9, 10)))
    monkeypatch.setattr(drc, "DrcRunner", reporting([BASE_UNCONNECTED], [moved]))
    board, kicad = saved_board

    result = placement.run_placement(plan_at((1, 1)), None, NO_SPLITS, kicad)

    assert result.prune.unattributed == (moved,)
    assert len(pushes(board)) == 1
    assert placement.markers_for(result.prune) == (
        Marker((7, 8), MarkerKind.UNATTRIBUTED),
    )


@pytest.mark.parametrize("dry_run", [False, True])
def test_splits_count_only_new_connectivity_violations_among_the_markers(
    saved_board, monkeypatch, dry_run
):
    moved = unconnected(("C", (7, 8)), ("D", (9, 10)))
    split = drc.Violation(
        "isolated_copper",
        "split",
        "error",
        (drc.ViolationItem("Zone [+5V] on In2.Cu", (11, 12)),),
    )
    sliver = drc.Violation(
        "copper_sliver",
        "Copper sliver",
        "warning",
        (drc.ViolationItem("Zone [GND] on In1.Cu", (5, 6)),),
    )
    monkeypatch.setattr(
        drc, "DrcRunner", reporting([BASE_UNCONNECTED], [moved, split, sliver])
    )
    _, kicad = saved_board
    unfixable = (UnfixableSpot((3, 3), "GND", "no room"),)

    result = placement.run_placement(
        plan_at((1, 1), unfixable=unfixable),
        None,
        placement.RunSettings("test", dry_run=dry_run),
        kicad,
    )

    assert len(placement.markers_for(result.prune)) == 4
    assert result.splits == 1


def test_unfixable_counts_each_spot_once_over_its_layers():
    spots = (
        UnfixableSpot((3, 3), "GND", "no room", "F.Cu"),
        UnfixableSpot((3, 3), "GND", "no room", "B.Cu"),
        UnfixableSpot((3, 3), "+3.3V", "no room"),
        UnfixableSpot((4, 4), "GND", "no room"),
    )
    prune = drc.PruneResult(Plan(unfixable=spots), (), (), (), 1)

    result = placement.RunResult(
        Snapshot(copper_layers=("F.Cu",)), prune, commit=CommitOutcome.NOTHING
    )

    assert result.unfixable == 3


def test_second_unconnected_item_on_the_same_net_is_a_split(saved_board, monkeypatch):
    extra = unconnected(("C", (7, 8)), ("D", (9, 10)))
    monkeypatch.setattr(
        drc, "DrcRunner", reporting([BASE_UNCONNECTED], [BASE_UNCONNECTED, extra])
    )
    board, kicad = saved_board

    with pytest.raises(placement.SplitError, match=r"\+3V3"):
        placement.run_placement(plan_at((1, 1)), None, NO_SPLITS, kicad)

    assert "begin_commit" not in board.calls


# ---- commit ------------------------------------------------------------------------


def test_commit_creates_items_and_markers_in_one_commit():
    board = RecordingBoard()
    stub = Stub(path=((0, 0), (1, 0)), width=0.3, layer="F.Cu")
    units = [unit(1, 0, stub=stub), unit(5, 5, net="+3.3V")]
    markers = [Marker((9, 9), MarkerKind.UNFIXABLE)]

    created = commit.commit_units(FakeKiCad(board), board, "pourstitch", units, markers)

    placed, marks = board.created
    assert [type(i).__name__ for i in placed] == ["Via", "Track", "Via"]
    via, track, _ = placed
    assert (via.position.x, via.position.y) == (1_000_000, 0)
    assert via.diameter == 600_000 and via.drill_diameter == 300_000
    assert via.net.name == "GND"
    assert track.layer == BL.BL_F_Cu and track.width == 300_000
    assert all(m.layer == BL.BL_Eco2_User for m in marks)
    (push,) = pushes(board)
    assert push[1].startswith("kistitch pourstitch ")
    assert board.calls.count("begin_commit") == 1
    assert created is CommitOutcome.LANDED


def test_commit_of_nets_gone_from_the_board_raises_before_committing():
    board = RecordingBoard()

    with pytest.raises(commit.UnknownNetsError, match="VBUS"):
        commit.commit_units(
            FakeKiCad(board), board, "padvia", [unit(1, 0, net="VBUS")], []
        )

    assert "begin_commit" not in board.calls


def test_unit_items_make_one_track_per_stub_leg():
    stub = Stub(path=((0, 0), (1, 1), (3, 1)), width=0.3, layer="F.Cu")
    nets = {"GND": kipy.board_types.Net()}
    nets["GND"].name = "GND"

    items = commit.unit_items([unit(3, 1, stub=stub)], nets, {"F.Cu": BL.BL_F_Cu})

    via, *tracks = items
    assert isinstance(via, kipy.board_types.Via)
    mm = lambda v: (v.x / NM_PER_MM, v.y / NM_PER_MM)
    assert [(mm(t.start), mm(t.end)) for t in tracks] == [
        ((0, 0), (1, 1)),
        ((1, 1), (3, 1)),
    ]
    for t in tracks:
        assert t.width == 300_000
        assert t.layer == BL.BL_F_Cu
        assert t.net.name == "GND"


def test_commit_refused_item_raises_and_drops_the_commit():
    # KiCad reports refusals per item instead of failing the request, and
    # kipy's create_items discards those statuses.
    board = RecordingBoard(refuse=lambda item: isinstance(item, kipy.board_types.Track))
    stub = Stub(path=((0, 0), (1, 0)), width=0.3, layer="F.Cu")

    with pytest.raises(commit.CreateItemsError, match="no overlapping layers"):
        commit.commit_units(
            FakeKiCad(board), board, "padvia", [unit(1, 0, stub=stub)], []
        )

    assert "drop_commit" in board.calls
    assert not any(isinstance(c, tuple) and c[0] == "push_commit" for c in board.calls)


@pytest.fixture
def log():
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(str(m)), format="{level} {message}")
    yield lines
    logger.remove(sink)


class PushFailingBoard(RecordingBoard):
    def __init__(self, error):
        super().__init__()
        self.error = error

    def push_commit(self, c, message=""):
        self.calls.append(("push_commit", message))
        raise self.error


def test_push_timeout_keeps_the_commit_and_warns(log):
    # The commit may have landed: dropping it then would be wrong.
    board = PushFailingBoard(ConnectionError("timed out"))

    outcome = commit.commit_units(FakeKiCad(board), board, "x", [unit(1, 1)], [])  # type: ignore[arg-type]

    assert outcome is CommitOutcome.UNKNOWN
    assert "drop_commit" not in board.calls
    assert any(
        line.startswith("WARNING") and "lost the connection" in line and "undo" in line
        for line in log
    )


def test_push_timeout_skips_the_refill_after_the_commit(log):
    # A lost connection would leave the refill waiting out its deadline.
    board = PushFailingBoard(ConnectionError("timed out"))

    commit.commit_units(FakeKiCad(board), board, "x", [unit(1, 1)], [])  # type: ignore[arg-type]

    assert "refill_zones" not in board.calls
    assert any(
        line.startswith("WARNING") and commit.UNKNOWN_PUSH_REFILL_MESSAGE in line
        for line in log
    )


def test_push_timeout_without_refills_does_not_ask_for_a_refill(log):
    board = PushFailingBoard(ConnectionError("timed out"))

    commit.commit_units(FakeKiCad(board), board, "x", [unit(1, 1)], [], refill=False)  # type: ignore[arg-type]

    assert not any(commit.UNKNOWN_PUSH_REFILL_MESSAGE in line for line in log)


def test_push_refused_by_kicad_drops_the_commit_and_raises():
    # An ApiError is KiCad's answer: nothing landed, so the commit is dropped.
    board = PushFailingBoard(ApiError("bad", code=ApiStatusCode.AS_BAD_REQUEST))

    with pytest.raises(ApiError):
        commit.commit_units(FakeKiCad(board), board, "x", [unit(1, 1)], [])  # type: ignore[arg-type]

    assert board.calls[-1] == "drop_commit"


def test_push_failing_otherwise_is_raised_without_dropping_the_commit():
    board = PushFailingBoard(RuntimeError("unexpected"))

    with pytest.raises(RuntimeError):
        commit.commit_units(FakeKiCad(board), board, "x", [unit(1, 1)], [])  # type: ignore[arg-type]

    assert "drop_commit" not in board.calls


def test_refill_error_after_the_commit_is_only_a_warning(log):
    class RefillFailingBoard(RecordingBoard):
        def refill_zones(self, block=True, max_poll_seconds=0):
            raise ApiError("bad", code=ApiStatusCode.AS_BAD_REQUEST)

    board = RefillFailingBoard()

    commit.commit_units(FakeKiCad(board), board, "x", [unit(1, 1)], [])  # type: ignore[arg-type]

    assert len(pushes(board)) == 1
    assert any(line.startswith("WARNING") and "refill" in line for line in log)


def visibility_changes(board):
    return [
        c for c in board.calls if isinstance(c, tuple) and c[0] == "set_visible_layers"
    ]


def test_commit_with_markers_shows_the_marker_layer_keeping_visible_ones():
    board = RecordingBoard()
    before = list(board.visible)

    commit.commit_units(
        FakeKiCad(board),
        board,
        "x",
        [unit(1, 1)],
        [Marker((9, 9), MarkerKind.UNFIXABLE)],
    )

    assert set(board.visible) == {*before, commit.MARKER_LAYER}
    assert len(visibility_changes(board)) == 1


def test_commit_with_marker_layer_already_visible_leaves_visibility_alone():
    board = RecordingBoard()
    board.visible.append(commit.MARKER_LAYER)

    commit.commit_units(
        FakeKiCad(board), board, "x", [], [Marker((9, 9), MarkerKind.UNATTRIBUTED)]
    )

    assert visibility_changes(board) == []


def test_commit_only_removing_markers_leaves_visibility_alone():
    board = RecordingBoard()
    (stale,) = commit.marker_items([Marker((7, 7), MarkerKind.UNFIXABLE)])

    commit.commit_units(FakeKiCad(board), board, "x", [unit(1, 1)], [], [stale])

    assert board.removed == [stale]
    assert visibility_changes(board) == []


@pytest.mark.parametrize(
    "error",
    [
        ApiError("busy", code=ApiStatusCode.AS_BUSY),
        ConnectionError("timed out"),
    ],
)
def test_failing_to_show_the_marker_layer_after_the_commit_only_warns(log, error):
    class HiddenBoard(RecordingBoard):
        def get_visible_layers(self):
            raise error

    board = HiddenBoard()

    created = commit.commit_units(
        FakeKiCad(board),
        board,
        "x",
        [unit(1, 1)],
        [Marker((9, 9), MarkerKind.UNFIXABLE)],
    )

    assert created is CommitOutcome.LANDED
    assert len(pushes(board)) == 1
    assert any(
        line.startswith("WARNING") and "Eco2" in line and "visible" in line
        for line in log
    )


@pytest.mark.parametrize(
    "error",
    [
        ApiError("busy", code=ApiStatusCode.AS_BUSY),
        ConnectionError("timed out"),
    ],
)
def test_failing_to_set_the_visible_layers_after_the_commit_only_warns(log, error):
    class RefusingBoard(RecordingBoard):
        def set_visible_layers(self, layers):
            raise error

    board = RefusingBoard()

    created = commit.commit_units(
        FakeKiCad(board),
        board,
        "x",
        [unit(1, 1)],
        [Marker((9, 9), MarkerKind.UNFIXABLE)],
    )

    assert created is CommitOutcome.LANDED
    assert len(pushes(board)) == 1
    assert "drop_commit" not in board.calls
    assert any(
        line.startswith("WARNING") and "Eco2" in line and "visible" in line
        for line in log
    )


def test_push_timeout_with_markers_leaves_visibility_alone():
    # Whether the markers landed is unknown, so their layer stays as it was.
    board = PushFailingBoard(ConnectionError("timed out"))

    commit.commit_units(
        FakeKiCad(board),
        board,
        "x",
        [unit(1, 1)],
        [Marker((9, 9), MarkerKind.UNFIXABLE)],
    )

    assert visibility_changes(board) == []


def test_commit_with_unknown_net_fails_before_opening_a_commit():
    board = RecordingBoard()

    with pytest.raises(ValueError, match="NOPE"):
        commit.commit_units(FakeKiCad(board), board, "x", [unit(1, 1, net="NOPE")], [])

    assert board_changes(board) == []


def test_commit_label_carries_command_and_timestamp():
    assert (
        commit.commit_label("padvia", datetime(2026, 10, 26, 14, 5, 9))
        == "kistitch padvia 2026-10-26 14:05:09"
    )


def test_unattributed_marker_is_a_square_centred_on_the_spot():
    items = commit.marker_items([Marker((10, 20), MarkerKind.UNATTRIBUTED)])

    assert len(items) == 4
    assert all(isinstance(s, kipy.board_types.BoardSegment) for s in items)
    xs = [p.x for s in items for p in (s.start, s.end)]
    ys = [p.y for s in items for p in (s.start, s.end)]
    assert (min(xs) + max(xs)) / 2 == 10_000_000
    assert (min(ys) + max(ys)) / 2 == 20_000_000
    assert max(xs) - min(xs) == 2 * commit.MARKER_HALF_SIZE * NM_PER_MM


def test_unfixable_marker_is_an_unfilled_circle_on_the_spot():
    (item,) = commit.marker_items([Marker((10, 20), MarkerKind.UNFIXABLE)])

    assert isinstance(item, kipy.board_types.BoardCircle)
    assert item.layer == commit.MARKER_LAYER
    assert (item.center.x, item.center.y) == (10_000_000, 20_000_000)
    assert item.attributes.stroke.width == commit.MARKER_WIDTH * NM_PER_MM
    assert item.proto.shape.attributes.fill.fill_type == base_types_pb2.GFT_UNFILLED


def test_markers_for_marks_unfixable_and_unattributed_but_not_forced():
    forced = PlannedUnit((2, 2), "GND", VIA, forced=True)

    result = drc.PruneResult(
        plan=Plan(
            units=(unit(1, 1), forced), unfixable=(UnfixableSpot((3, 3), "GND", "x"),)
        ),
        dropped=(),
        forced=(),
        unattributed=(
            drc.Violation(
                "copper_sliver", "s", "warning", (drc.ViolationItem("Zone", (4, 4)),)
            ),
        ),
        rounds=1,
    )

    markers = placement.markers_for(result)

    assert [(m.position, m.kind) for m in markers] == [
        ((3, 3), MarkerKind.UNFIXABLE),
        ((4, 4), MarkerKind.UNATTRIBUTED),
    ]

    without_unfixable = placement.markers_for(result, mark_unfixable=False)

    assert [m.kind for m in without_unfixable] == [MarkerKind.UNATTRIBUTED]


def test_summary_lists_each_forced_via_at_warning(log):
    # A forced via that meets the clearance rule has no DRC violation and no
    # marker, so the log is the only record of where it is.
    result = drc.PruneResult(
        plan=Plan(
            units=(
                unit(1, 1),
                PlannedUnit((2.5, 3.25), "GND", VIA, forced=True),
                PlannedUnit((4, 5), "GND2", VIA, forced=True),
            )
        ),
        dropped=(),
        forced=(),
        unattributed=(),
        rounds=1,
    )

    placement.log_summary("returnvia", result)

    warnings = [line for line in log if line.startswith("WARNING")]
    assert any("2 forced vias" in line for line in warnings)
    assert any("forced via: GND at (2.500, 3.250)" in line for line in warnings)
    assert any("forced via: GND2 at (4.000, 5.000)" in line for line in warnings)


# ---- refill --------------------------------------------------------------------


class RefillBoard:
    """Raises `error` from every refill, or each of `errors` in turn."""

    def __init__(self, error=None, errors=()):
        self.error = error
        self.errors = list(errors)
        self.refills = 0
        self.blocking = []

    def refill_zones(self, block=True, max_poll_seconds=0):
        self.refills += 1
        self.blocking.append(block)
        error = self.errors.pop(0) if self.errors else self.error
        if error:
            raise error


def busy():
    return ApiError("busy", code=ApiStatusCode.AS_BUSY)


class FakeClock:
    """Time that moves only when the code under test sleeps or a ping waits."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_refill_success():
    board = RefillBoard(None)

    assert commit.refill_tolerant(FakeKiCad(None), board, sleep=lambda s: None)
    # kipy's blocking refill has no working time limit, so ours bounds the wait.
    assert board.blocking == [False]


def test_refill_gives_up_when_kicad_stays_busy():
    kicad = FakeKiCad(None)
    kicad.pings = [busy()] * 10
    clock = FakeClock()

    ok = commit.refill_tolerant(
        kicad, RefillBoard(None), timeout_s=3, poll_s=1, sleep=clock.sleep, clock=clock
    )

    assert not ok
    assert len(kicad.pings) == 7  # three polls, then the deadline


def test_refill_timeout_waits_until_kicad_answers():
    kicad = FakeKiCad(None)
    kicad.pings = [
        ConnectionError("timeout"),
        ApiError("busy", code=ApiStatusCode.AS_BUSY),
        None,
    ]
    sleeps = []

    ok = commit.refill_tolerant(
        kicad, RefillBoard(ConnectionError("timeout")), sleep=sleeps.append
    )

    assert ok
    assert len(sleeps) == 3


def test_refill_gives_up_after_timeout():
    kicad = FakeKiCad(None)
    kicad.pings = [ConnectionError("timeout")] * 10
    clock = FakeClock()

    ok = commit.refill_tolerant(
        kicad,
        RefillBoard(ConnectionError("timeout")),
        timeout_s=3,
        poll_s=1,
        sleep=clock.sleep,
        clock=clock,
    )

    assert not ok


def test_refill_wait_is_bounded_in_time_not_polls():
    # A ping to a KiCad that is filling waits out the client timeout, so a
    # poll count would stretch the wait to several times timeout_s.
    clock = FakeClock()
    pings = []

    class SlowKiCad:
        def ping(self):
            pings.append(clock.now)
            clock.sleep(2)
            raise ConnectionError("timeout")

    ok = commit.refill_tolerant(
        SlowKiCad(),  # type: ignore[arg-type]
        RefillBoard(None),
        timeout_s=6,
        poll_s=1,
        sleep=clock.sleep,
        clock=clock,
    )

    assert not ok
    assert len(pings) == 2
    assert clock.now <= 6 + 1 + 2  # at most one poll and one ping past it


def test_refill_accepted_at_the_deadline_still_asks_kicad_once():
    kicad = FakeKiCad(None)
    kicad.pings = [None]
    clock = FakeClock()

    ok = commit.refill_tolerant(
        kicad,
        RefillBoard(errors=[busy()] * 3),
        timeout_s=3,
        poll_s=1,
        sleep=clock.sleep,
        clock=clock,
    )

    assert ok
    assert kicad.pings == []


def test_refill_reraises_real_errors():
    with pytest.raises(ApiError):
        commit.refill_tolerant(
            FakeKiCad(None),
            RefillBoard(ApiError("bad", code=ApiStatusCode.AS_BAD_REQUEST)),
            sleep=lambda s: None,
        )


def test_refill_refused_as_busy_is_retried():
    # AS_BUSY means KiCad refused to start the refill, not that it is running.
    board = RefillBoard(errors=[busy(), busy()])
    sleeps = []

    ok = commit.refill_tolerant(FakeKiCad(None), board, sleep=sleeps.append)

    assert ok
    assert board.refills == 3
    assert len(sleeps) == 3  # two retries, then one wait for KiCad to answer


def test_refill_polls_at_the_module_interval_by_default(monkeypatch):
    monkeypatch.setattr(commit, "REFILL_POLL_S", 0.25)
    sleeps = []

    commit.refill_tolerant(
        FakeKiCad(None), RefillBoard(errors=[busy()]), sleep=sleeps.append
    )

    assert sleeps == [0.25, 0.25]


def test_refill_busy_is_logged_once_naming_the_likely_cause(log):
    board = RefillBoard(errors=[busy(), busy(), busy()])

    commit.refill_tolerant(FakeKiCad(None), board, sleep=lambda s: None)  # type: ignore[arg-type]

    busy_lines = [line for line in log if "busy" in line]
    assert len(busy_lines) == 1
    assert busy_lines[0].startswith("WARNING") and "dialog" in busy_lines[0]


def test_refill_busy_until_the_deadline_is_not_success():
    kicad = FakeKiCad(None)
    board = RefillBoard(busy())
    clock = FakeClock()

    ok = commit.refill_tolerant(
        kicad, board, timeout_s=3, poll_s=1, sleep=clock.sleep, clock=clock
    )

    assert not ok
    assert board.refills == 4  # the first call plus one per poll


def stop_after(calls):
    """A stop check that raises Cancelled from its `calls`-th call on."""
    seen = []

    def check():
        seen.append(None)
        if len(seen) >= calls:
            raise Cancelled()

    return check


def test_refill_stops_on_request_while_kicad_stays_busy():
    board = RefillBoard(busy())
    clock = FakeClock()

    with pytest.raises(Cancelled):
        commit.refill_tolerant(
            FakeKiCad(None),
            board,
            timeout_s=3,
            poll_s=1,
            sleep=clock.sleep,
            clock=clock,
            check=stop_after(1),
        )

    assert board.refills == 1


def test_refill_stops_on_request_while_waiting_for_the_fill():
    kicad = FakeKiCad(None)
    kicad.pings = [busy()] * 10

    with pytest.raises(Cancelled):
        commit.refill_tolerant(
            kicad, RefillBoard(None), sleep=lambda s: None, check=stop_after(2)
        )

    assert len(kicad.pings) == 9  # one ping, then the stop


@pytest.mark.parametrize("refilled", [True, False])
def test_measure_only_refills_and_snapshots_with_an_empty_plan(
    monkeypatch, log, refilled
):
    board, kicad, snap = object(), object(), Snapshot(copper_layers=("F.Cu",))
    refills = []
    monkeypatch.setattr(
        placement,
        "refill_tolerant",
        lambda k, b, **_: refills.append((k, b)) or refilled,
    )
    monkeypatch.setattr(Snapshot, "from_board", staticmethod(lambda b: snap))

    result = placement.measure_only(kicad, board)  # type: ignore[arg-type]

    assert result == (snap, Plan())
    assert refills == [(kicad, board)]
    stale = [m for m in log if "fills may be stale" in m]
    assert len(stale) == (0 if refilled else 1)
    assert all(m.startswith("WARNING") for m in stale)


@pytest.mark.parametrize("extent", [0.5, 2.0])
def test_unfixable_circle_encloses_the_spot_extent(extent):
    spot = UnfixableSpot((10, 20), "GND", "x", layer="F.Cu", extent=extent)
    result = drc.PruneResult(Plan(unfixable=(spot,)), (), (), (), 1)

    (item,) = commit.marker_items(placement.markers_for(result))

    assert item.radius() / NM_PER_MM == pytest.approx(extent + commit.MARKER_MARGIN)


def test_unfixable_circle_of_a_small_spot_keeps_the_minimum_diameter():
    spot = UnfixableSpot((10, 20), "GND", "x", layer="F.Cu", extent=0.01)
    result = drc.PruneResult(Plan(unfixable=(spot,)), (), (), (), 1)

    (item,) = commit.marker_items(placement.markers_for(result))

    assert 2 * item.radius() / NM_PER_MM == pytest.approx(0.7)


def test_unfixable_circle_without_extent_is_0_7_mm_across():
    (item,) = commit.marker_items([Marker((10, 20), MarkerKind.UNFIXABLE)])

    assert 2 * item.radius() / NM_PER_MM == pytest.approx(0.7)


def test_run_removes_earlier_circle_markers(saved_board):
    board, kicad = saved_board
    (stale,) = commit.marker_items([Marker((7, 7), MarkerKind.UNFIXABLE)])
    board.shapes = [stale]

    placement.run_placement(plan_at((1, 1)), None, placement.RunSettings("t"), kicad)

    assert board.removed == [stale]


class BusyPushBoard:
    def __init__(self):
        self.dropped = False

    def push_commit(self, commit, label):
        raise busy()

    def drop_commit(self, commit):
        self.dropped = True
        raise busy()


def test_a_busy_push_leaves_the_report_to_the_caller():
    errors: list[str] = []
    sink = logger.add(lambda m: errors.append(m), level="ERROR")
    board = BusyPushBoard()
    try:
        with pytest.raises(ApiError) as error:
            commit.push_tolerant(board, None, "padvia")
    finally:
        logger.remove(sink)

    assert error.value.code == ApiStatusCode.AS_BUSY
    assert error.value.__context__ is None
    assert not board.dropped
    assert errors == []


# ---- automatic refills off ----------------------------------------------------


def refills_off_lines(log):
    return [line for line in log if commit.REFILLS_OFF_MESSAGE in line]


def test_refills_off_message_asks_for_a_refill_before_the_run():
    # A refill during the run changes the fills and fails it with
    # BoardChangedError, so the message must not invite one.
    assert "before running kistitch" in commit.REFILLS_OFF_MESSAGE
    assert "after edits" not in commit.REFILLS_OFF_MESSAGE


def test_run_without_refills_refills_neither_before_nor_after_the_commit(
    saved_board, log
):
    board, kicad = saved_board

    placement.run_placement(
        plan_at((1, 1)), None, placement.RunSettings("t", refill=False), kicad
    )

    assert len(pushes(board)) == 1
    assert "refill_zones" not in board.calls
    (line,) = refills_off_lines(log)
    assert line.startswith("INFO")
    assert not [line for line in log if "refill not confirmed" in line]


def test_dry_run_without_refills_does_not_refill(saved_board, log):
    board, kicad = saved_board

    placement.run_placement(
        plan_at((1, 1)),
        None,
        placement.RunSettings("t", dry_run=True, refill=False),
        kicad,
    )

    assert "refill_zones" not in board.calls
    assert len(refills_off_lines(log)) == 1


def test_board_changed_without_refills_does_not_blame_a_refill(saved_board):
    board, kicad = saved_board
    board.texts = ["(kicad_pcb)", "(kicad_pcb (edited))"]

    with pytest.raises(placement.BoardChangedError) as error:
        placement.run_placement(
            plan_at((1, 1)), None, placement.RunSettings("t", refill=False), kicad
        )

    assert str(error.value) == "The board changed during the run; run again"
    assert "refill_zones" not in board.calls


def test_measure_only_without_refills_uses_the_fills_as_they_are(monkeypatch, log):
    board, kicad, snap = object(), object(), Snapshot(copper_layers=("F.Cu",))
    monkeypatch.setattr(
        placement,
        "refill_tolerant",
        lambda k, b, **_: pytest.fail("refilled with refills off"),
    )
    monkeypatch.setattr(Snapshot, "from_board", staticmethod(lambda b: snap))

    result = placement.measure_only(kicad, board, refill=False)  # type: ignore[arg-type]

    assert result == (snap, Plan())
    (line,) = refills_off_lines(log)
    assert line.startswith("INFO")
    assert not [line for line in log if "stale" in line]


def test_commit_without_refill_only_commits(log):
    board = RecordingBoard()

    commit.commit_units(
        FakeKiCad(board),  # type: ignore[arg-type]
        board,  # type: ignore[arg-type]
        "x",
        [unit(1, 1)],
        [],
        refill=False,
    )

    assert len(pushes(board)) == 1
    assert "refill_zones" not in board.calls
    assert not [line for line in log if "refill" in line.lower()]


# ---- progress and cancellation ------------------------------------------------


def stage_names(rec):
    return [(s.value, c) for s, c in rec.stages()]


def test_run_reports_its_stages_in_order(saved_board, recorder):
    _, kicad = saved_board
    rec = recorder()

    placement.run_placement(
        plan_at((1, 1), (5, 5)),
        None,
        placement.RunSettings("test"),
        kicad,
        progress=rec.progress,
    )

    assert stage_names(rec) == [
        ("refill", None),
        ("read", None),
        ("plan", None),
        ("drc", (1, drc.DEFAULT_MAX_ROUNDS)),
        ("plan", None),
        ("drc", (2, drc.DEFAULT_MAX_ROUNDS)),
        ("commit", None),
        ("refill", None),
    ]


def test_run_without_refills_reports_no_refill_stage(saved_board, recorder):
    _, kicad = saved_board
    rec = recorder()

    placement.run_placement(
        plan_at((1, 1)),
        None,
        placement.RunSettings("test", refill=False),
        kicad,
        progress=rec.progress,
    )

    assert [s for s, _ in stage_names(rec)] == ["read", "plan", "drc", "commit"]


def test_stop_before_the_run_starts_cancels_before_the_refill(saved_board, recorder):
    board, kicad = saved_board
    rec = recorder()
    rec.stopped = True

    with pytest.raises(Cancelled):
        placement.run_placement(
            never_plan,
            None,
            placement.RunSettings("test"),
            kicad,
            progress=rec.progress,
        )

    assert board_changes(board) == []


def test_stop_during_planning_cancels_without_a_commit(saved_board, recorder):
    board, kicad = saved_board
    rec = recorder()

    def planner(snapshot, options, forbidden):
        rec.stopped = True
        return Plan(units=(unit(1, 1),))

    with pytest.raises(Cancelled):
        placement.run_placement(
            planner, None, placement.RunSettings("test"), kicad, progress=rec.progress
        )

    assert "begin_commit" not in board.calls and not pushes(board)


def test_stop_during_the_last_drc_round_cancels_without_a_commit(
    saved_board, recorder, monkeypatch
):
    board, kicad = saved_board
    rec = recorder()

    class StoppingRunner(FakeDrcRunner):
        def new_violations(self, plan):
            rec.stopped = True
            return super().new_violations(plan)

    monkeypatch.setattr(drc, "DrcRunner", StoppingRunner)

    with pytest.raises(Cancelled):
        placement.run_placement(
            plan_at((1, 1)),
            None,
            placement.RunSettings("test"),
            kicad,
            progress=rec.progress,
        )

    assert "begin_commit" not in board.calls and not pushes(board)


def test_stop_after_the_board_changed_check_still_commits(saved_board, recorder):
    board, kicad = saved_board
    rec = recorder(board.calls, stop_at=Stage.COMMIT)

    result = placement.run_placement(
        plan_at((1, 1)),
        None,
        placement.RunSettings("test"),
        kicad,
        progress=rec.progress,
    )

    assert result.commit is CommitOutcome.LANDED
    assert len(pushes(board)) == 1
    # The refill after the commit runs too.
    assert stage_names(rec)[-1] == ("refill", None)
    assert board.calls[-1] == "refill_zones"


def test_committing_is_reported_after_the_last_stop_check_and_before_the_commit(
    saved_board, recorder
):
    board, kicad = saved_board
    board.texts = ["(kicad_pcb)", "(kicad_pcb)"]
    rec = recorder(board.calls)

    placement.run_placement(
        plan_at((1, 1)),
        None,
        placement.RunSettings("test"),
        kicad,
        progress=rec.progress,
    )

    committing = board.calls.index((Stage.COMMIT, None))
    last_check = len(board.calls) - 1 - board.calls[::-1].index("check")
    assert last_check == committing - 1
    assert board.calls.index("begin_commit") > committing


def test_measure_only_reports_refill_and_read(monkeypatch, recorder):
    snap = Snapshot(copper_layers=("F.Cu",))
    monkeypatch.setattr(placement, "refill_tolerant", lambda k, b, **_: True)
    monkeypatch.setattr(Snapshot, "from_board", staticmethod(lambda b: snap))
    rec = recorder()

    placement.measure_only(object(), object(), progress=rec.progress)  # type: ignore[arg-type]

    assert stage_names(rec) == [("refill", None), ("read", None)]


def test_measure_only_without_refills_reports_only_read(monkeypatch, recorder):
    monkeypatch.setattr(
        Snapshot, "from_board", staticmethod(lambda b: Snapshot(copper_layers=()))
    )
    rec = recorder()

    placement.measure_only(object(), object(), refill=False, progress=rec.progress)  # type: ignore[arg-type]

    assert stage_names(rec) == [("read", None)]


def test_measure_only_stopped_before_reading_cancels(monkeypatch, recorder):
    monkeypatch.setattr(placement, "refill_tolerant", lambda k, b, **_: True)
    monkeypatch.setattr(
        Snapshot, "from_board", staticmethod(lambda b: pytest.fail("board read"))
    )
    rec = recorder(stop_at=Stage.REFILL)

    with pytest.raises(Cancelled):
        placement.measure_only(object(), object(), progress=rec.progress)  # type: ignore[arg-type]


def refill_that_stops(refilled):
    """A refill that asks the stop check once, then records that it finished."""

    def refill(kicad, board, check):
        check()
        refilled.append(None)
        return True

    return refill


def test_measure_only_stops_during_the_refill(monkeypatch, recorder):
    refilled = []
    monkeypatch.setattr(placement, "refill_tolerant", refill_that_stops(refilled))
    rec = recorder(stop_at=Stage.REFILL)

    with pytest.raises(Cancelled):
        placement.measure_only(object(), object(), progress=rec.progress)  # type: ignore[arg-type]

    assert refilled == []


def test_placement_stops_during_the_refill_before_reading(
    saved_board, monkeypatch, recorder
):
    _, kicad = saved_board
    refilled = []
    monkeypatch.setattr(placement, "refill_tolerant", refill_that_stops(refilled))
    rec = recorder(stop_at=Stage.REFILL)

    with pytest.raises(Cancelled):
        placement.run_placement(
            plan_at((1, 1)),
            None,
            placement.RunSettings("test"),
            kicad,
            progress=rec.progress,
        )

    assert refilled == []


SLOW_DRC_CLI = """
import os, pathlib, sys, time
if sys.argv[1:] == ["version"]:
    print("10.0.7")
    sys.exit()
pathlib.Path(os.environ["KISTITCH_TEST_PID"]).write_text(str(os.getpid()))
time.sleep(60)
"""


def test_stop_during_drc_kills_kicad_cli_and_leaves_no_temp_files(
    saved_board, recorder, monkeypatch, tmp_path
):
    board, kicad = saved_board
    monkeypatch.setattr(drc, "DrcRunner", REAL_DRC_RUNNER)
    cli = tmp_path / "kicad-cli"
    cli.write_text(f"#!{sys.executable}\n{SLOW_DRC_CLI}")
    pid_file = tmp_path / "pid"
    monkeypatch.setenv("KISTITCH_TEST_PID", str(pid_file))
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    rec = recorder()
    rec.progress = dataclasses.replace(rec.progress, stop=pid_file.exists)

    started = time.monotonic()
    with pytest.raises(Cancelled):
        placement.run_placement(
            plan_at((1, 1)),
            None,
            placement.RunSettings("test"),
            kicad,
            progress=rec.progress,
        )

    assert time.monotonic() - started < 10
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)
    assert list(scratch.iterdir()) == []
    assert "begin_commit" not in board.calls
