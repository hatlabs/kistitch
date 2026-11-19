import math
from dataclasses import replace

import pytest
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import unary_union

from kistitch import drc
from kistitch.placement import markers_for
from kistitch.plan import QUANTISATION
from kistitch.progress import Cancelled, Progress, Stage
from kistitch.raster import PIXEL
from kistitch.returnpath import Outcome, measure_returns
from kistitch.returnvia import ReturnViaOptions
from kistitch.returnvia_command import with_returns
from kistitch.returnvia_planner import (
    NO_ROOM,
    RELIES_ON,
    WOULD_OVERLAP,
    ReturnViaPlanner,
)
from kistitch.snapshot import Pad, PadKind, Snapshot, Track, Via, ViaSize, Zone

FOUR_LAYERS = ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")
INNER = ("In1.Cu", "In2.Cu")
BOARD = box(0, 0, 40, 20)
DISTANCE = 1.5
SIG = (10.0, 10.0)
VIA_RADIUS = 0.3
ANTIPAD = VIA_RADIUS + 0.2
# Thicker than a return's passable disc reaches past its own copper, so no
# return can bridge it.
WALL = 0.5
TWO_GROUNDS = ("GND", "GND_ISO")


def via(net, pos):
    return Via(id=f"{net}@{pos}", net=net, position=pos, diameter=0.6, drill=0.3)


def signal_via(pos, net="SIG"):
    """A via tied to F.Cu and B.Cu by stubs within its own copper."""
    x, y = pos
    stub = LineString([pos, (x - 0.1, y)])
    tracks = [Track(f"{net}:{L}@{pos}", net, L, 0.2, stub) for L in ("F.Cu", "B.Cu")]
    return via(net, pos), tracks


def holes(*discs):
    return unary_union([Point(p).buffer(r) for p, r in discs])


def flush(*signals):
    return BOARD.difference(holes(*((s, ANTIPAD) for s in signals)))


def gnd_zone(fills, net="GND", outline=BOARD):
    return Zone(net=net, layers=tuple(fills), priority=0, outline=outline, fills=fills)


def board(
    signals, returns=(), fills=None, zones=None, pads=(), tracks=(), via_sizes=None
):
    """Signal vias over GND planes on In1 and In2, by default the board
    minus each signal via's antipad."""
    vias, ties = [], []
    for s in signals:
        v, t = signal_via(s)
        vias.append(v)
        ties += t
    if zones is None:
        zones = [gnd_zone(fills or dict.fromkeys(INNER, flush(*signals)))]
    return Snapshot(
        copper_layers=FOUR_LAYERS,
        vias=vias + [via(n, p) for n, p in returns],
        tracks=ties + list(tracks),
        zones=zones,
        pads=list(pads),
        edge_paths=[list(BOARD.exterior.coords)],
        via_sizes=via_sizes or {},
    )


def plan(snap, forbidden=(), **kwargs):
    return ReturnViaPlanner()(snap, ReturnViaOptions(**kwargs), list(forbidden))


def check(snap, result, ground_nets=None):
    """The `--check` measurement of the board with the planned vias."""
    return measure_returns(with_returns(snap, result.units), ground_nets, DISTANCE)


def unserved(results):
    return {r.position for r in results if not r.served}


def other_net_copper(area):
    return Pad("X@", None, "X", PadKind.SMD, "1", (0, 0), 0.0, {"F.Cu": area})


def slotted_in1(returns=(("GND", (SIG[0] + 1.4, SIG[1])),), pads=()):
    """A thin slot in In1 between SIG and the east; In2 is whole."""
    slot = box(SIG[0] + 0.6, SIG[1] - 2, SIG[0] + 0.7, SIG[1] + 2)
    fills = {"In1.Cu": flush(SIG).difference(slot), "In2.Cu": flush(SIG)}
    return board([SIG], returns, fills=fills, pads=pads), slot


def test_slot_in_one_plane_gets_a_return_on_its_near_side():
    snap, slot = slotted_in1()
    before = {r.plane.layer: r.served for r in measure_returns(snap, None, DISTANCE)}
    assert before == {"In1.Cu": False, "In2.Cu": True}

    result = plan(snap)

    (u,) = result.units
    assert Point(u.position).buffer(VIA_RADIUS).bounds[2] <= slot.bounds[0]
    assert not unserved(check(snap, result))


def test_spot_across_a_slot_is_rejected_even_with_clearance():
    # Other-net copper leaves room only across the slot, where In1 has no
    # short path back to the via.
    near_side = box(5, 5, SIG[0] + 0.8, 15).difference(holes((SIG, VIA_RADIUS)))
    snap, _ = slotted_in1(returns=(), pads=[other_net_copper(near_side)])

    result = plan(snap)

    assert not result.units
    assert [(s.position, s.extent) for s in result.unfixable] == [(SIG, VIA_RADIUS)]


def test_ends_failing_on_two_ground_nets_get_one_via_each():
    # GND under F.Cu, GND_ISO under B.Cu; each also pours an outer layer so
    # a return via of it reaches two layers.
    plain = flush(SIG)
    zones = [
        gnd_zone(dict.fromkeys(("F.Cu", "In1.Cu"), plain)),
        gnd_zone(dict.fromkeys(("In2.Cu", "B.Cu"), plain), net="GND_ISO"),
    ]
    snap = board([SIG], zones=zones)

    result = plan(snap, ground_nets=TWO_GROUNDS)

    assert sorted(u.net for u in result.units) == ["GND", "GND_ISO"]
    assert not unserved(check(snap, result, TWO_GROUNDS))


def test_spot_on_the_outermost_ring_passes_the_check():
    # A keep-off leaves room only on the last ring, 1.85 mm out: beyond the
    # served threshold centre to centre, within it barrel to barrel.
    hole = Pad("h", None, "", PadKind.NPTH, "", SIG, 0.4, {}, clearance_override=1.25)
    snap = board([SIG], pads=[hole])

    result = plan(snap)

    (u,) = result.units
    assert math.dist(u.position, SIG) > DISTANCE + QUANTISATION
    assert not unserved(check(snap, result))


def test_via_served_by_a_via_planned_earlier_gets_none_of_its_own():
    later = (SIG[0] + 0.8, SIG[1] + 0.9)
    snap = board([SIG, later])

    result = plan(snap)

    assert len(result.units) == 1
    assert not unserved(check(snap, result))


def test_earlier_via_serves_a_via_barrel_to_barrel_beyond_its_centre_reach():
    # SIG's via lands on its first ring, 0.9 mm out to the east; `later`
    # lies about 1.75 mm beyond it, 1.45 mm barrel to barrel.
    later = (SIG[0] + 0.9 + 1.75, SIG[1])
    snap = board([SIG, later])

    result = plan(snap)

    (u,) = result.units
    assert math.dist(u.position, later) > DISTANCE + QUANTISATION + PIXEL
    assert not unserved(check(snap, result))


def test_search_covers_only_the_planes_earlier_vias_leave_unserved():
    # A's via, planned first, serves B on In1 through the open west side of
    # a C-shaped wall around B, but not on In2, where a wall west of B
    # blocks it. Every spot left for B lies outside the C, so it serves B on
    # In2 only: a search over both planes would find none.
    b = (20.0, 10.0)
    a = (b[0] - 2.35, b[1])
    inner = box(b[0] - 0.6, b[1] - 0.6, b[0] + 0.6, b[1] + 0.6)
    outer = inner.buffer(WALL, join_style="mitre")
    c_wall = outer.difference(inner).difference(box(0, 0, b[0] - 0.6, 20))
    west_wall = box(b[0] - 0.6 - WALL, b[1] - 2, b[0] - 0.6, b[1] + 2)
    fills = {
        "In1.Cu": flush(a, b).difference(c_wall),
        "In2.Cu": flush(a, b).difference(west_wall),
    }
    snap = board([a, b], fills=fills)

    result = plan(snap)

    first, second = result.units
    assert math.dist(first.position, a) < math.dist(first.position, b)
    assert not outer.contains(Point(second.position))
    assert not unserved(check(snap, result))
    alone = check(snap, type(result)(units=(second,)))
    assert {r.plane.layer for r in alone if r.position == b and not r.served} == {
        "In1.Cu"
    }


def test_prune_rounds_plan_as_a_fresh_planner_given_the_forbidden_spots():
    snap = board([SIG])
    options = ReturnViaOptions()
    planner = ReturnViaPlanner()

    (first,) = planner(snap, options, []).units
    second = planner(snap, options, [first.position])

    assert second.units and second.units[0].position != first.position
    assert second == ReturnViaPlanner()(snap, options, [first.position])


def crowded():
    """Other-net F.Cu tracks every 1 mm, leaving SIG only 0.8 mm gaps."""
    rows = [SIG[1] + 0.5 + k for k in range(-4, 4)]
    return [
        Track(f"X{y}", "X", "F.Cu", 0.2, LineString([(5, y), (15, y)])) for y in rows
    ]


def test_forced_via_leaves_its_via_unserved_and_unmarked():
    snap = board([SIG], tracks=crowded())

    result = plan(snap, force=True)

    (u,) = result.units
    assert u.forced
    assert unserved(check(snap, result)) == {SIG}
    prune = drc.PruneResult(result, (), (), (), 1)
    assert u.position not in {m.position for m in markers_for(prune, True)}


def test_no_ground_fill_end_is_unfixable_and_never_planned():
    # In1 has a GND outline around SIG but no fill; GND pours In2 and B.Cu.
    around = box(SIG[0] - 0.8, SIG[1] - 0.8, SIG[0] + 0.8, SIG[1] + 0.8)
    zones = [
        gnd_zone({"In1.Cu": Polygon()}, outline=around),
        gnd_zone(dict.fromkeys(("In2.Cu", "B.Cu"), flush(SIG))),
    ]
    snap = board([SIG], zones=zones)

    result = plan(snap, force=True)

    (u,) = result.units
    assert not u.forced
    assert [(s.position, s.reason, s.extent) for s in result.unfixable] == [
        (SIG, "no GND fill nearby on In1.Cu", VIA_RADIUS)
    ]
    after = {r.plane.layer: r for r in check(snap, result)}
    assert after["In1.Cu"].outcome is Outcome.NO_GROUND_FILL
    assert after["In2.Cu"].served


def test_end_outside_every_ground_zone_is_unfixable_and_the_other_end_planned():
    # GND pours F.Cu, and In1 everywhere but around SIG: the F.Cu end finds
    # no plane, the B.Cu end references the F.Cu pour.
    around = box(SIG[0] - 0.6, SIG[1] - 0.6, SIG[0] + 0.6, SIG[1] + 0.6)
    zones = [
        gnd_zone({"F.Cu": flush(SIG)}),
        gnd_zone({"In1.Cu": flush(SIG)}, outline=BOARD.difference(around)),
    ]
    snap = board([SIG], zones=zones)

    result = plan(snap, ground_nets=["GND"], force=True)

    (u,) = result.units
    assert not u.forced
    assert [(s.position, s.reason) for s in result.unfixable] == [
        (SIG, "outside every ground zone for F.Cu")
    ]
    after = check(snap, result, ["GND"])
    assert [(r.layers, r.served) for r in after] == [
        (("B.Cu",), True),
        (("F.Cu",), False),
    ]


def test_planned_run_leaves_only_the_unfixable_vias_unserved():
    crowded_via = (SIG[0], SIG[1])
    slotted = (25.0, 10.0)
    plain = (30.0, 5.0)
    slot = box(slotted[0] + 0.6, slotted[1] - 2, slotted[0] + 0.7, slotted[1] + 2)
    fill = flush(crowded_via, slotted, plain)
    snap = board(
        [crowded_via, slotted, plain],
        returns=[("GND", (slotted[0] + 1.4, slotted[1]))],
        fills={"In1.Cu": fill.difference(slot), "In2.Cu": fill},
        tracks=crowded(),
    )

    result = plan(snap)

    assert {s.position for s in result.unfixable} == {crowded_via}
    assert len(result.units) == 2
    assert unserved(check(snap, result)) == {crowded_via}


def pocketed(*pockets, signals=(SIG,)):
    """Other-net F.Cu copper around `signals` except the given pockets."""
    vias = tuple((p, VIA_RADIUS) for p in signals)
    return other_net_copper(box(4, 4, 18, 16).difference(holes(*vias, *pockets)))


def test_pending_via_served_by_a_via_planned_later_is_not_unfixable():
    # A has no spot of its own; B's via, planned later, serves it too.
    a = SIG
    b_via = (
        round(a[0] + 1.225 * math.cos(math.radians(5)), 3),
        round(a[1] + 1.225 * math.sin(math.radians(5)), 3),
    )
    b = (round(b_via[0] + 1.0, 3), b_via[1])
    snap = board([a, b], pads=[pocketed((b_via, 0.63), signals=(a, b))])

    result = plan(snap)

    assert [u.position for u in result.units] == [b_via]
    assert not result.unfixable
    assert not unserved(check(snap, result))


def slot_between(a, b, pockets):
    """A wide slot in both planes east of `a`; room only in `pockets`."""
    slot = box(a[0] + 0.6, a[1] - 4, a[0] + 1.1, a[1] + 4)
    fills = dict.fromkeys(INNER, flush(a, b).difference(slot))
    return board([a, b], fills=fills, pads=[pocketed(*pockets, signals=(a, b))])


def test_full_clearance_via_never_stands_in_for_another():
    # B's via sits across the slot from A, within a straight 1.5 mm of it.
    a, b, b_via = SIG, (SIG[0] + 2.5, SIG[1]), (SIG[0] + 1.5, SIG[1])
    snap = slot_between(a, b, [(b_via, 0.65)])

    result = plan(snap)

    assert [u.position for u in result.units] == [b_via]
    assert [(s.position, s.reason) for s in result.unfixable] == [
        (a, "no clear spot reaches the plane within 1.5 mm")
    ]


def test_forced_via_of_another_ground_never_stands_in():
    a, b = SIG, (SIG[0] + 1.2, SIG[1])
    left, right = box(0, 0, 10.6, 20), box(10.6, 0, 40, 20)
    plain = flush(a, b)
    zones = [
        gnd_zone({L: plain.intersection(left) for L in INNER}, outline=left),
        gnd_zone(
            {L: plain.intersection(right) for L in INNER}, "GND_ISO", outline=right
        ),
    ]
    pockets = pocketed(((10.2, 10.9), 0.45), ((12.2, 10.0), 0.45), signals=(a, b))
    snap = board([b, a], zones=zones, pads=[pockets])

    result = plan(snap, ground_nets=TWO_GROUNDS, force=True)

    assert sorted((u.net, u.forced) for u in result.units) == [
        ("GND", True),
        ("GND_ISO", True),
    ]
    assert not result.unfixable


def test_via_relying_on_a_forced_via_is_unfixable_and_names_it():
    # A's forced via lands across the slot from A, beside B, which then has
    # no spot of its own.
    a, b = SIG, (SIG[0] + 2.5, SIG[1])
    snap = slot_between(a, b, [((11.5, 10.0), 0.45), ((13.5, 10.0), 0.45)])

    result = plan(snap, force=True)

    (u,) = result.units
    assert u.forced
    x, y = u.position
    assert [(s.position, s.reason) for s in result.unfixable] == [
        (b, f"relies on forced via at ({x:.3f}, {y:.3f})")
    ]
    prune = drc.PruneResult(result, (), (), (), 1)
    assert b in {m.position for m in markers_for(prune, True)}


def test_no_clear_spot_is_no_room():
    snap = board([SIG], pads=[pocketed()])

    result = plan(snap)

    assert [(s.reason, s.layer) for s in result.unfixable] == [
        ("no room for a return via", "In1.Cu, In2.Cu")
    ]


def thermal_only(centre, hole):
    """GND pours In2 and B.Cu. In1's GND fill and outline stop `hole` short
    of `centre`, so a via there reaches In1 only through its thermal reach.
    Other-net copper leaves room for a via only at `centre`."""
    gap = Point(centre).buffer(hole)
    zones = [
        gnd_zone({"In1.Cu": flush(SIG).difference(gap)}, outline=BOARD.difference(gap)),
        gnd_zone(dict.fromkeys(("In2.Cu", "B.Cu"), flush(SIG))),
    ]
    return board([SIG], zones=zones, pads=[pocketed((centre, VIA_RADIUS + 0.35))])


def test_return_whose_centre_is_off_the_fill_serves_through_its_thermal_reach():
    centre = (SIG[0] + 1.0, SIG[1])
    snap = thermal_only(centre, VIA_RADIUS + 0.1)

    result = plan(snap)

    assert [u.position for u in result.units] == [centre]
    assert not unserved(check(snap, result))


def test_return_reach_follows_its_own_ground_nets_via_size():
    # GND_ISO's larger via reaches In1's fill past the gap; GND's would not.
    centre = (30.0 + 1.2, SIG[1])
    sig = (30.0, SIG[1])
    sizes = {"GND": ViaSize(0.6, 0.3), "GND_ISO": ViaSize(1.0, 0.5)}
    gap = Point(centre).buffer(0.75)
    left, right = box(0, 0, 20, 20), box(20, 0, 40, 20)
    plain = flush(sig)
    zones = [
        gnd_zone(
            dict.fromkeys(("In1.Cu", "In2.Cu", "B.Cu"), plain.intersection(left)),
            outline=left,
        ),
        gnd_zone(
            {"In1.Cu": plain.intersection(right).difference(gap)},
            "GND_ISO",
            right.difference(gap),
        ),
        gnd_zone(
            dict.fromkeys(("In2.Cu", "B.Cu"), plain.intersection(right)),
            "GND_ISO",
            right,
        ),
    ]
    room = holes((sig, VIA_RADIUS), (centre, 0.85))
    pocket = other_net_copper(box(24, 4, 38, 16).difference(room))
    snap = board([sig], zones=zones, pads=[pocket], via_sizes=sizes)

    result = plan(snap, ground_nets=TWO_GROUNDS)

    (u,) = result.units
    assert (u.net, u.position, u.size) == ("GND_ISO", centre, sizes["GND_ISO"])
    assert not unserved(check(snap, result, TWO_GROUNDS))


def test_planning_reports_each_target_and_stops_between_targets(recorder):
    snap = board([SIG, (SIG[0] + 0.8, SIG[1] + 0.9)])
    rec = recorder()

    ReturnViaPlanner(rec.progress)(snap, ReturnViaOptions(), [])

    assert rec.stages() == [(Stage.PLAN, (1, 2)), (Stage.PLAN, (2, 2))]

    stopping = recorder(stop_at=Stage.PLAN)
    with pytest.raises(Cancelled):
        ReturnViaPlanner(stopping.progress)(snap, ReturnViaOptions(), [])
    assert stopping.stages() == [(Stage.PLAN, (1, 2))]


def test_measuring_the_targets_stops_on_request():
    # The stop comes after the first via is measured, so only a check
    # inside the measurement can catch it before planning starts.
    checks = []
    stages = []
    progress = Progress(
        report=lambda stage, count: stages.append(stage),
        stop=lambda: checks.append(None) or len(checks) > 1,
    )
    snap = board([SIG, (SIG[0] + 0.8, SIG[1] + 0.9)])

    with pytest.raises(Cancelled):
        ReturnViaPlanner(progress)(snap, ReturnViaOptions(), [])
    assert Stage.PLAN not in stages


# ---- signal PTH pads -------------------------------------------------------------

PIN_RADIUS = 0.85
PIN_DRILL = 1.0
PIN_ANTIPAD_GAP = 0.2
PITCH = 2.54
# Other-net F.Cu tracks along a row, a clearance off its 1.7 mm pads, too
# wide for a spot beyond them to reach a pin.
ROW_TRACK_WIDTH = 1.0
ROW_TRACK_OFFSET = PIN_RADIUS + 0.3 + ROW_TRACK_WIDTH / 2


def pin(pos, net="SIG", shape=None):
    """A PTH header pin, `shape` (default a 1.7 mm disc) on every layer."""
    copper = shape if shape is not None else Point(pos).buffer(PIN_RADIUS)
    return Pad(
        f"{net}@{pos}",
        None,
        net,
        PadKind.PTH,
        "1",
        pos,
        PIN_DRILL,
        dict.fromkeys(FOUR_LAYERS, copper),
    )


def square(pos, half=PIN_RADIUS):
    x, y = pos
    return box(x - half, y - half, x + half, y + half)


def pin_board(pins, signals=(), others=(), tracks=()):
    """`pins` routed on F.Cu and B.Cu, beside signal vias at `signals` and
    unrouted `others`; the GND planes clear every non-ground pin."""
    ties = [
        Track(
            f"{p.id}:{L}",
            p.net,
            L,
            0.2,
            LineString([p.position, (p.position[0] - 0.1, p.position[1])]),
        )
        for p in pins
        for L in ("F.Cu", "B.Cu")
    ]
    antipads = unary_union(
        [
            p.shapes["In1.Cu"].buffer(PIN_ANTIPAD_GAP)
            for p in (*pins, *others)
            if p.kind is PadKind.PTH and p.net != "GND"
        ]
    )
    fills = dict.fromkeys(INNER, flush(*signals).difference(antipads))
    return board(
        list(signals), fills=fills, pads=[*pins, *others], tracks=[*ties, *tracks]
    )


def test_unserved_header_pin_gets_a_return_the_check_counts():
    snap = pin_board([pin(SIG)])
    assert unserved(measure_returns(snap, None, DISTANCE)) == {SIG}

    result = plan(snap)

    (u,) = result.units
    assert not u.forced
    assert math.dist(u.position, SIG) <= DISTANCE + QUANTISATION
    assert not result.unfixable
    assert not unserved(check(snap, result))


def test_fine_pitch_pin_beside_a_ground_pin_needs_no_return():
    small = 0.5
    sig = pin(SIG, shape=Point(SIG).buffer(small))
    gnd_at = (SIG[0] + 1.27, SIG[1])
    gnd = pin(gnd_at, net="GND", shape=Point(gnd_at).buffer(small))
    snap = pin_board([sig], others=[gnd])

    result = plan(snap)

    assert not result.units and not result.unfixable


def test_return_lands_beside_the_long_side_of_a_rectangular_pad():
    # Its corners reach 1.6 mm: rings starting there would start past reach.
    half_w, half_h = 1.5, 0.6
    x, y = SIG
    rect = box(x - half_w, y - half_h, x + half_w, y + half_h)
    snap = pin_board([pin(SIG, shape=rect)])

    result = plan(snap)

    (u,) = result.units
    assert abs(u.position[0] - x) < half_w
    assert not unserved(check(snap, result))


def test_one_return_serves_two_adjacent_pins():
    a, b = SIG, (SIG[0] + PITCH, SIG[1])
    snap = pin_board([pin(a), pin(b, net="SIG2")])

    result = plan(snap)

    assert len(result.units) == 1
    assert not unserved(check(snap, result))


def test_pin_and_nearby_via_share_one_return():
    via_at = (SIG[0] + 2.2, SIG[1])
    snap = pin_board([pin(SIG)], signals=[via_at])

    result = plan(snap)

    assert len(result.units) == 1
    assert not unserved(check(snap, result))


def row_parts(offsets=(-PITCH, 0.0, PITCH), measured=(0.0,)):
    """A row of pins between other-net tracks, by x offset from SIG: a
    square pin at 0 and round pins elsewhere. Pins at the offsets in
    `measured` are routed, the rest are not; as `pin_board`'s pins, others
    and tracks. With 2.54 mm neighbours no spot near the square pin is
    clear."""
    x, y = SIG
    pins, others = [], []
    for i, dx in enumerate(offsets):
        pos = (x + dx, y)
        p = pin(pos, net=f"P{i}", shape=square(pos) if dx == 0 else None)
        (pins if dx in measured else others).append(p)
    tracks = [
        Track(
            f"X{dy}",
            "X",
            "F.Cu",
            ROW_TRACK_WIDTH,
            LineString([(x - 6, y + dy), (x + 8, y + dy)]),
        )
        for dy in (-ROW_TRACK_OFFSET, ROW_TRACK_OFFSET)
    ]
    return pins, others, tracks


def row(**kwargs):
    pins, others, tracks = row_parts(**kwargs)
    return pin_board(pins, others=others, tracks=tracks)


def test_blocked_pin_is_unfixable_with_a_marker_enclosing_it():
    snap = row()

    result = plan(snap)

    assert not result.units
    (s,) = result.unfixable
    assert (s.position, s.net, s.reason) == (SIG, "P1", NO_ROOM)
    assert s.extent == pytest.approx(math.hypot(PIN_RADIUS, PIN_RADIUS))
    assert unserved(check(snap, result)) == {SIG}


def test_force_places_a_blocked_pins_via_at_maximum_clearance():
    snap = row()

    result = plan(snap, force=True)

    (u,) = result.units
    assert u.forced
    # Force reaches a drill wall within the distance of the pin's.
    assert math.dist(u.position, SIG) - (PIN_DRILL + u.size.drill) / 2 <= DISTANCE
    # The roomiest spot within reach leaves 0.2 mm, between the square pin's
    # corner and a track; the 5 degree rings come within 0.04 mm.
    copper = [p.shapes["F.Cu"] for p in snap.pads] + [
        t.centreline.buffer(t.width / 2) for t in snap.tracks if t.net == "X"
    ]
    gap = min(Point(u.position).buffer(VIA_RADIUS).distance(c) for c in copper)
    assert gap == pytest.approx(0.2, abs=0.04)
    assert not result.unfixable


def test_forced_via_of_one_pin_stands_in_for_its_neighbour():
    # A tight west neighbour leaves the square pin room only to the east,
    # toward the next routed pin.
    snap = row(offsets=(-2.0, 0.0, PITCH, 2 * PITCH), measured=(0.0, PITCH))

    result = plan(snap, force=True)

    (u,) = result.units
    assert u.forced
    x, y = u.position
    east = (SIG[0] + PITCH, SIG[1])
    assert [(s.position, s.reason) for s in result.unfixable] == [
        (east, RELIES_ON.format(x=x, y=y))
    ]


def test_force_reports_a_pin_whose_every_spot_overlaps_other_net_copper():
    # Other-net copper fills the plane's antipad gap around the pin.
    gap_edge = PIN_RADIUS + PIN_ANTIPAD_GAP
    blanket = other_net_copper(
        box(4, 4, 16, 16).difference(Point(SIG).buffer(gap_edge))
    )
    snap = pin_board([pin(SIG)], others=[blanket])

    result = plan(snap, force=True)

    assert not result.units
    (s,) = result.unfixable
    assert s.reason == WOULD_OVERLAP
    assert s.extent == pytest.approx(PIN_RADIUS, rel=0.01)


def test_pin_outside_every_ground_zone_marks_the_whole_pad():
    outside = (35.0, 10.0)
    snap = pin_board([pin(outside, shape=square(outside))])
    snap = replace(
        snap, zones=[gnd_zone(dict.fromkeys(INNER, flush()), outline=box(0, 0, 20, 20))]
    )

    result = plan(snap, force=True)

    assert not result.units
    assert result.unfixable
    for s in result.unfixable:
        assert s.position == outside
        assert s.extent == pytest.approx(math.hypot(PIN_RADIUS, PIN_RADIUS))


def test_planned_run_leaves_only_the_unfixable_pins_unserved():
    rect_at = (30.0, 15.0)
    rect = box(rect_at[0] - 1.5, rect_at[1] - 0.6, rect_at[0] + 1.5, rect_at[1] + 0.6)
    pins, others, tracks = row_parts()
    pins += [pin((30.0, 5.0), net="A"), pin(rect_at, net="B", shape=rect)]
    snap = pin_board(pins, others=others, tracks=tracks)

    result = plan(snap)

    assert {s.position for s in result.unfixable} == {SIG}
    assert len(result.units) == 2
    assert unserved(check(snap, result)) == {SIG}
