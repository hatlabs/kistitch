import math

import pytest
from shapely.geometry import LineString, Point, box
from shapely.ops import unary_union

from kistitch.plan import QUANTISATION
from kistitch.returnpath import (
    NoBoardOutlineError,
    Outcome,
    PlaneReturn,
    ReturnFields,
    measure_returns,
)
from kistitch.returnvia import LayerChange, ReferencePlane
from kistitch.snapshot import Pad, PadKind, Snapshot, Track, Via, Zone

FOUR_LAYERS = ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")
INNER = ("In1.Cu", "In2.Cu")
BOARD = box(0, 0, 40, 20)
DISTANCE = 1.5
SIG = (10.0, 10.0)
VIA_RADIUS = 0.3
VIA_DRILL = 0.3
# The barrel-to-barrel gap between two such vias is their centre distance less this.
BARRELS = VIA_DRILL
ANTIPAD = VIA_RADIUS + 0.2
# 8-connected steps overstate a straight run by up to about 8 %.
STEP_ERROR = 0.08


def via(net, pos, dia=2 * VIA_RADIUS, drill=VIA_DRILL):
    return Via(id=f"{net}@{pos}", net=net, position=pos, diameter=dia, drill=drill)


def signal_via(pos=SIG, net="SIG"):
    """A via joining F.Cu and B.Cu tracks: its ends reference In1 and In2."""
    x, y = pos
    tracks = [
        Track(f"{net}:{L}@{pos}", net, L, 0.2, LineString([pos, (x - 3, y)]))
        for L in ("F.Cu", "B.Cu")
    ]
    return via(net, pos), tracks


def holes(*discs):
    return unary_union([Point(p).buffer(r) for p, r in discs])


def gnd_zone(fills, net="GND", outline=BOARD, layers=INNER):
    return Zone(net=net, layers=tuple(layers), priority=0, outline=outline, fills=fills)


def board(signals, returns=(), fills=None, zones=None, pads=(), edge=True):
    """Signal vias with F.Cu/B.Cu tracks over GND planes on In1 and In2.

    By default both planes are the board minus each signal via's antipad.
    """
    vias, tracks = [], []
    for s in signals:
        v, t = signal_via(s)
        vias.append(v)
        tracks += t
    if zones is None:
        if fills is None:
            plain = BOARD.difference(holes(*((s, ANTIPAD) for s in signals)))
            fills = {L: plain for L in INNER}
        zones = [gnd_zone(fills)]
    outline = [[(0, 0), (40, 0), (40, 20), (0, 20), (0, 0)]] if edge else []
    return Snapshot(
        copper_layers=FOUR_LAYERS,
        vias=vias + [via(n, p) for n, p in returns],
        tracks=tracks,
        zones=zones,
        pads=list(pads),
        edge_paths=outline,
    )


def measure(snap, distance=DISTANCE) -> dict[str, PlaneReturn]:
    """Results of the via at SIG by plane layer."""
    return {
        r.plane.layer: r
        for r in measure_returns(snap, None, distance)
        if r.position == SIG and r.plane
    }


def polar(r, degrees, centre=SIG):
    a = math.radians(degrees)
    return (centre[0] + r * math.cos(a), centre[1] + r * math.sin(a))


def assert_close(measured, straight):
    assert abs(measured - straight) <= QUANTISATION + STEP_ERROR * straight


@pytest.mark.parametrize("degrees", [0, 22.5, 45])
@pytest.mark.parametrize("r, served", [(1.7, True), (2.0, False)])
def test_unobstructed_plane_measures_barrel_to_barrel(degrees, r, served):
    results = measure(board([SIG], [("GND", polar(r, degrees))]))

    assert set(results) == set(INNER)
    for res in results.values():
        assert res.outcome is Outcome.MEASURED
        assert res.served is served
        assert_close(res.distance, r - BARRELS)


def test_ground_via_at_exactly_the_distance_on_an_axis_is_served():
    results = measure(board([SIG], [("GND", (SIG[0] + DISTANCE + BARRELS, SIG[1]))]))
    assert all(r.served for r in results.values())


def test_wide_zone_clearance_still_measures_from_the_void_edge():
    wide = BOARD.difference(holes((SIG, VIA_RADIUS + 0.5)))
    snap = board([SIG], [("GND", polar(1.4, 0))], fills={L: wide for L in INNER})

    for res in measure(snap).values():
        assert res.served
        assert_close(res.distance, 1.4 - BARRELS)


def test_row_of_vias_with_merged_antipads_measures_across_the_void():
    row = [(SIG[0] + 0.8 * k, SIG[1]) for k in range(-2, 3)]
    ret = (SIG[0], SIG[1] + 1.3)
    snap = board(row, [("GND", ret)])

    results = [r for r in measure_returns(snap, None, DISTANCE) if r.plane]
    assert len(results) == 2 * len(row)
    for res in results:
        straight = math.dist(res.position, ret) - BARRELS
        assert res.outcome is Outcome.MEASURED
        assert_close(res.distance, straight)
        assert res.served is (straight <= DISTANCE)


def test_return_beyond_a_large_pad_antipad_is_measured_across_the_void():
    pad_antipad = box(SIG[0] + 0.3, SIG[1] - 1.5, SIG[0] + 1.3, SIG[1] + 1.5)
    fill = BOARD.difference(holes((SIG, ANTIPAD)).union(pad_antipad))
    snap = board([SIG], [("GND", polar(1.4, 0))], fills={L: fill for L in INNER})

    for res in measure(snap).values():
        assert res.served
        assert_close(res.distance, 1.4 - BARRELS)


def test_island_nearer_than_the_main_pour_does_not_hide_the_pour():
    island = box(SIG[0] + 0.55, SIG[1] - 0.2, SIG[0] + 0.75, SIG[1] + 0.2)
    fill = BOARD.difference(holes((SIG, 1.0))).union(island)
    snap = board([SIG], [("GND", polar(1.4, 90))], fills={L: fill for L in INNER})

    for res in measure(snap).values():
        assert res.served
        assert_close(res.distance, 1.4 - BARRELS)


def test_thermal_relief_return_is_measured_from_its_drill():
    ret = polar(1.4, 0)
    fill = BOARD.difference(holes((SIG, ANTIPAD), (ret, VIA_RADIUS + 0.3)))
    snap = board([SIG], [("GND", ret)], fills={L: fill for L in INNER})

    for res in measure(snap).values():
        assert res.served
        assert_close(res.distance, 1.4 - BARRELS)


def test_ground_via_without_fill_within_thermal_reach_is_no_return():
    ret = polar(1.2, 0)
    gap = BOARD.difference(holes((SIG, ANTIPAD), (ret, VIA_RADIUS + 0.5)))
    flush = BOARD.difference(holes((SIG, ANTIPAD)))
    snap = board([SIG], [("GND", ret)], fills={"In1.Cu": gap, "In2.Cu": flush})

    results = measure(snap)
    assert results["In1.Cu"].outcome is Outcome.MEASURED
    assert results["In1.Cu"].distance == math.inf
    assert not results["In1.Cu"].served
    assert results["In2.Cu"].served


def test_fill_inside_the_vias_drill_is_no_negative_hop():
    snap = board([SIG], [("GND", SIG)], fills={L: BOARD for L in INNER})

    for res in measure(snap).values():
        assert res.distance == 0


def test_no_fill_near_the_via_is_its_own_outcome():
    far = box(15, 0, 20, 20)
    flush = BOARD.difference(holes((SIG, ANTIPAD)))
    snap = board(
        [SIG], [("GND", polar(1.4, 0))], fills={"In1.Cu": far, "In2.Cu": flush}
    )

    results = measure(snap)
    assert results["In1.Cu"].outcome is Outcome.NO_GROUND_FILL
    assert not results["In1.Cu"].served
    assert results["In1.Cu"].reason == "no GND fill nearby on In1.Cu"
    assert results["In2.Cu"].served


def test_slot_in_one_plane_fails_only_the_end_that_references_it():
    # The return's passable disc (radius + THERMAL_REACH) must not bridge
    # the slot, so the return sits 1.4 mm away rather than closer.
    ret = polar(1.4, 0)
    slot = box(SIG[0] + 0.6, SIG[1] - 2, SIG[0] + 0.7, SIG[1] + 2)
    flush = BOARD.difference(holes((SIG, ANTIPAD)))
    snap = board(
        [SIG], [("GND", ret)], fills={"In1.Cu": flush.difference(slot), "In2.Cu": flush}
    )

    results = measure(snap)
    assert not results["In1.Cu"].served
    assert results["In1.Cu"].distance > 2 * 2
    assert results["In2.Cu"].served
    assert_close(results["In2.Cu"].distance, 1.4 - BARRELS)


# Midpoint and inward direction of each board outline edge.
EDGES = {
    "left": ((0.0, 10.0), (1, 0)),
    "right": ((40.0, 10.0), (-1, 0)),
    "top": ((20.0, 0.0), (0, 1)),
    "bottom": ((20.0, 20.0), (0, -1)),
}
OPPOSITE = {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}
# Less than the return's passable disc radius, so the disc still reaches fill.
PAST_EDGE = 0.5
INSET = 1.0


def inset(edge, d):
    (x, y), (dx, dy) = EDGES[edge]
    return (x + d * dx, y + d * dy)


def results_at(snap, pos):
    return [r for r in measure_returns(snap, None, DISTANCE) if r.position == pos]


@pytest.mark.parametrize("edge", EDGES)
@pytest.mark.parametrize("offset", [0.0, PAST_EDGE])
def test_return_on_or_past_the_outline_serves_a_via_inside(edge, offset):
    sig = inset(edge, INSET)
    snap = board([sig], [("GND", inset(edge, -offset))])

    results = results_at(snap, sig)
    assert len(results) == len(INNER)
    assert all(r.served for r in results)


@pytest.mark.parametrize("edge", EDGES)
def test_return_past_the_outline_does_not_serve_the_far_side(edge):
    sig = inset(OPPOSITE[edge], INSET)
    snap = board([sig], [("GND", inset(edge, -PAST_EDGE))])

    results = results_at(snap, sig)
    assert len(results) == len(INNER)
    assert not any(r.served for r in results)


CASTELLATION_DRILL = 1.0
CASTELLATION_RADIUS = 0.9


@pytest.mark.parametrize("edge", EDGES)
def test_castellated_return_measures_alike_on_every_outline_edge(edge):
    centres = 2.3
    sig = inset(edge, centres)
    pos = inset(edge, 0.0)
    shapes = {L: Point(pos).buffer(CASTELLATION_RADIUS) for L in FOUR_LAYERS}
    pin = Pad("c", None, "GND", PadKind.PTH, "1", pos, CASTELLATION_DRILL, shapes)
    snap = board([sig], pads=[pin])

    results = results_at(snap, sig)
    assert len(results) == len(INNER)
    for res in results:
        assert_close(res.distance, centres - CASTELLATION_DRILL / 2 - VIA_DRILL / 2)


def test_ground_via_of_another_net_does_not_serve_the_plane():
    iso = box(5, 5, 15, 15)
    antipads = holes((SIG, ANTIPAD), (polar(1.0, 0), ANTIPAD))
    zones = [
        gnd_zone({"In1.Cu": iso.difference(antipads)}, "GND_ISO", iso, ["In1.Cu"]),
        gnd_zone({"In2.Cu": BOARD.difference(holes((SIG, ANTIPAD)))}, "GND"),
    ]
    snap = board([SIG], [("GND", polar(1.0, 0))], zones=zones)

    results = measure_returns(snap, ["GND", "GND_ISO"], DISTANCE)
    by_layer = {r.plane.layer: r for r in results if r.plane}
    assert by_layer["In1.Cu"].plane == ReferencePlane("In1.Cu", "GND_ISO")
    assert not by_layer["In1.Cu"].served
    assert by_layer["In2.Cu"].plane == ReferencePlane("In2.Cu", "GND")
    assert by_layer["In2.Cu"].served


def test_ground_pth_pad_is_a_return_but_an_smd_pad_is_not():
    pos = polar(1.4, 0)
    shapes = {L: Point(pos).buffer(0.5) for L in FOUR_LAYERS}

    def pad(kind, drill):
        return Pad("p", None, "GND", kind, "1", pos, drill, shapes)

    pth = measure(board([SIG], pads=[pad(PadKind.PTH, 0.4)]))
    smd = measure(board([SIG], pads=[pad(PadKind.SMD, 0.0)]))

    assert all(r.served for r in pth.values())
    assert not any(r.served for r in smd.values())


PIN_DRILL = 1.075
PIN_RADIUS = 0.9


def test_ground_pin_whose_hole_wall_is_within_the_distance_serves():
    # Centre to centre is beyond the served threshold; wall to wall is not.
    centres = 1.9
    pos = polar(centres, 0)
    shapes = {L: Point(pos).buffer(PIN_RADIUS) for L in FOUR_LAYERS}
    pin = Pad("p", None, "GND", PadKind.PTH, "1", pos, PIN_DRILL, shapes)
    assert centres - VIA_DRILL / 2 > DISTANCE + QUANTISATION

    for res in measure(board([SIG], pads=[pin])).values():
        assert res.served
        assert_close(res.distance, centres - VIA_DRILL / 2 - PIN_DRILL / 2)


def test_return_drill_too_thin_to_round_to_a_pixel_seeds_its_centre():
    # (11.4, 10.0) is a pixel corner: the nearest pixel centres lie 0.071 mm
    # away, beyond the drill rounded to the 0.1 mm grid.
    thin = 0.02
    snap = board([SIG])
    snap.vias = [*snap.vias, via("GND", polar(1.4, 0), drill=thin)]

    for res in measure(snap).values():
        assert res.served
        assert_close(res.distance, 1.4 - VIA_DRILL / 2 - thin / 2)


def test_two_ends_on_one_plane_are_one_measurement():
    zones = [
        gnd_zone({"In1.Cu": BOARD.difference(holes((SIG, ANTIPAD)))}, layers=["In1.Cu"])
    ]
    snap = board([SIG], [("GND", polar(1.0, 0))], zones=zones)

    (res,) = measure_returns(snap, ["GND"], DISTANCE)
    assert res.layers == ("F.Cu", "B.Cu")
    assert res.plane == ReferencePlane("In1.Cu", "GND")
    assert res.served


def test_layer_without_reference_plane_is_unserved_outside_ground():
    zones = [
        gnd_zone({L: box(20, 0, 40, 20) for L in INNER}, outline=box(20, 0, 40, 20))
    ]
    snap = board([SIG], zones=zones)

    results = measure_returns(snap, ["GND"], DISTANCE)
    assert [(r.layers, r.outcome, r.served) for r in results] == [
        (("F.Cu",), Outcome.OUTSIDE_GROUND, False),
        (("B.Cu",), Outcome.OUTSIDE_GROUND, False),
    ]
    assert results[0].plane is None
    assert results[0].reason == "outside every ground zone for F.Cu"


def test_exempt_via_is_not_measured():
    lone = via("SIG", SIG)
    snap = Snapshot(
        copper_layers=FOUR_LAYERS,
        vias=[lone],
        zones=[gnd_zone({L: BOARD for L in INNER})],
        edge_paths=[[(0, 0), (40, 0), (40, 20), (0, 20), (0, 0)]],
    )
    assert measure_returns(snap, None, DISTANCE) == []


PAD_RADIUS = 0.6
PAD_ANTIPAD = PAD_RADIUS + 0.2
PAD_DRILL = 1.0


def signal_pad(pos=SIG, net="SIG", layers=("F.Cu", "B.Cu")):
    """A PTH pad on every layer with tracks on `layers`."""
    shapes = {L: Point(pos).buffer(PAD_RADIUS) for L in FOUR_LAYERS}
    x, y = pos
    tracks = [
        Track(f"{net}:{L}@pad", net, L, 0.2, LineString([pos, (x + 3, y)]))
        for L in layers
    ]
    p = Pad(f"pad:{net}@{pos}", None, net, PadKind.PTH, "1", pos, PAD_DRILL, shapes)
    return p, tracks


def pad_board(returns=(), layers=("F.Cu", "B.Cu"), zones=None, pads=()):
    """A signal PTH pad at SIG over GND planes on In1 and In2."""
    p, tracks = signal_pad(layers=layers)
    if zones is None:
        plain = BOARD.difference(holes((SIG, PAD_ANTIPAD)))
        zones = [gnd_zone({L: plain for L in INNER})]
    return Snapshot(
        copper_layers=FOUR_LAYERS,
        vias=[via(n, pos) for n, pos in returns],
        tracks=tracks,
        zones=zones,
        pads=[p, *pads],
        edge_paths=[[(0, 0), (40, 0), (40, 20), (0, 20), (0, 0)]],
    )


def test_signal_pth_pad_beyond_its_return_is_unserved_on_each_plane():
    results = measure_returns(pad_board([("GND", polar(2.5, 0))]), None, DISTANCE)

    assert sorted(r.plane.layer for r in results) == list(INNER)
    for r in results:
        assert r.position == SIG
        assert r.net == "SIG"
        assert r.change.id == signal_pad()[0].id
        assert r.outcome is Outcome.MEASURED
        assert not r.served


def test_ground_pth_pad_within_the_distance_serves_a_signal_pth_pad():
    pos = polar(1.4, 0)
    gnd = Pad(
        "gnd", None, "GND", PadKind.PTH, "2", pos, 0.4,
        {L: Point(pos).buffer(0.3) for L in FOUR_LAYERS},
    )  # fmt: skip

    results = measure_returns(pad_board(pads=[gnd]), None, DISTANCE)

    assert len(results) == len(INNER)
    assert all(r.served for r in results)


def test_signal_pth_pad_is_measured_from_its_drill_wall():
    results = measure_returns(pad_board([("GND", polar(2.0, 0))]), None, DISTANCE)

    assert len(results) == len(INNER)
    for r in results:
        assert r.served
        assert_close(r.distance, 2.0 - PAD_DRILL / 2 - VIA_DRILL / 2)


def test_signal_pth_pad_routed_on_one_layer_is_not_measured():
    snap = pad_board([("GND", polar(2.0, 0))], layers=("F.Cu",))
    assert measure_returns(snap, None, DISTANCE) == []


def test_signal_pth_pad_outside_every_ground_zone_is_outside_ground():
    far = box(20, 0, 40, 20)
    snap = pad_board(zones=[gnd_zone({L: far for L in INNER}, outline=far)])

    results = measure_returns(snap, ["GND"], DISTANCE)
    assert [(r.layers, r.outcome) for r in results] == [
        (("F.Cu",), Outcome.OUTSIDE_GROUND),
        (("B.Cu",), Outcome.OUTSIDE_GROUND),
    ]


def test_signal_pth_pad_with_no_ground_fill_near_it_has_no_ground_fill():
    far = box(15, 0, 20, 20)
    snap = pad_board(zones=[gnd_zone({L: far for L in INNER})])

    results = measure_returns(snap, ["GND"], DISTANCE)
    assert len(results) == len(INNER)
    assert all(r.outcome is Outcome.NO_GROUND_FILL for r in results)


def test_via_and_pad_at_one_spot_are_measured_apart():
    v, via_tracks = signal_via()
    p, pad_tracks = signal_pad()
    plain = BOARD.difference(holes((SIG, PAD_ANTIPAD)))
    snap = Snapshot(
        copper_layers=FOUR_LAYERS,
        vias=[v, via("GND", polar(2.0, 0))],
        pads=[p],
        tracks=via_tracks + pad_tracks,
        zones=[gnd_zone({L: plain for L in INNER})],
        edge_paths=[[(0, 0), (40, 0), (40, 20), (0, 20), (0, 0)]],
    )

    results = measure_returns(snap, None, DISTANCE)
    assert len(results) == 2 * len(INNER)
    assert {r.change.id for r in results} == {v.id, p.id}
    by_id = {r.change.id: r.distance for r in results}
    assert by_id[v.id] - by_id[p.id] == pytest.approx((PAD_DRILL - VIA_DRILL) / 2)


def test_board_without_outline_raises_a_typed_error():
    with pytest.raises(NoBoardOutlineError):
        measure_returns(board([SIG], edge=False), None, DISTANCE)


# A terminal-block pin: its antipad edge lies beyond the distance from its
# centre, but within the distance of its drill wall.
BIG_DRILL = 1.3
BIG_RADIUS = 1.3
BIG_GAP = 1.35
BIG_RETURN = polar(BIG_DRILL / 2 + BIG_GAP + VIA_DRILL / 2, 0)


def big_pad_board(antipad):
    shapes = {L: Point(SIG).buffer(BIG_RADIUS) for L in FOUR_LAYERS}
    p = Pad("pin", None, "SIG", PadKind.PTH, "1", SIG, BIG_DRILL, shapes)
    x, y = SIG
    tracks = [
        Track(f"SIG:{L}@pin", "SIG", L, 0.2, LineString([SIG, (x - 3, y)]))
        for L in ("F.Cu", "B.Cu")
    ]
    fill = BOARD.difference(antipad)
    return Snapshot(
        copper_layers=FOUR_LAYERS,
        vias=[via("GND", BIG_RETURN)],
        tracks=tracks,
        zones=[gnd_zone({L: fill for L in INNER})],
        pads=[p],
        edge_paths=[[(0, 0), (40, 0), (40, 20), (0, 20), (0, 0)]],
    )


def test_pad_whose_antipad_edge_is_beyond_the_distance_of_its_centre_is_measured():
    antipad_edge = 1.7
    assert antipad_edge > DISTANCE
    snap = big_pad_board(holes((SIG, antipad_edge)))

    for res in measure(snap).values():
        assert res.outcome is Outcome.MEASURED
        assert res.served
        assert_close(res.distance, BIG_GAP)


def test_pad_with_only_part_of_its_antipad_rim_near_its_centre_hops_straight_across():
    # Its long sides lie within the distance of its centre, its ends beyond.
    x, y = SIG
    antipad = box(x - 1.7, y - 1.45, x + 1.7, y + 1.45)
    snap = big_pad_board(antipad)

    for res in measure(snap).values():
        assert res.outcome is Outcome.MEASURED
        assert_close(res.distance, BIG_GAP)


def test_pads_void_reaches_as_far_as_its_measurement_hops():
    antipad_edge = 1.7
    snap = big_pad_board(holes((SIG, antipad_edge)))
    fields = ReturnFields(snap)

    found = fields.void(
        LayerChange.of_pad(snap.pads[0]), ReferencePlane("In1.Cu", "GND"), DISTANCE
    )

    assert found is not None
    win, void = found
    rim = fields.grid.index((SIG[0] + antipad_edge - fields.grid.pixel, SIG[1]))
    assert void[rim[0] - win[0].start, rim[1] - win[1].start]
