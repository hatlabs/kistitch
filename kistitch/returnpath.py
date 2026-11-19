"""Return distance through the copper of each signal via's reference planes.

On a plane, the distance from a signal via or PTH pad to a return runs
barrel to barrel: a straight hop from its drill wall across its own void,
then the shortest path through the plane's ground fill, plus each return's
passable disc, to the nearest return's drill wall.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum

import numpy as np
import numpy.typing as npt
from scipy import ndimage
from shapely.geometry import Point
from shapely.geometry.base import BaseGeometry

from kistitch.plan import QUANTISATION, THERMAL_REACH
from kistitch.raster import (
    BoolMap,
    FloatMap,
    Grid,
    Pixel,
    geodesic,
    geodesic_in,
    touch,
)
from kistitch.returnvia import (
    OUTSIDE_GROUND,
    ChangeReferences,
    LayerChange,
    ReferencePlane,
    change_references,
    resolve_ground_nets,
)
from kistitch.snapshot import NoBoardOutlineError, PadKind, Point2, Snapshot, ViaSize

# The void floods through edge-sharing pixels only, so it never leaks through
# two fill pixels that meet at a corner.
VOID_CONNECTIVITY = ndimage.generate_binary_structure(2, 1)
ADJACENT = np.ones((3, 3), bool)


# Fill pixels next to a via's void, as row and column indices, and the
# straight hop (mm) from the via's drill wall to each.
Entries = tuple[npt.NDArray[np.intp], npt.NDArray[np.intp], FloatMap]

# A return's centre, drill diameter and passable disc.
Return = tuple[Point2, float, BaseGeometry]


def served_within(distance: float) -> float:
    """The largest return distance that serves a via or pad end."""
    return distance + QUANTISATION


class Outcome(Enum):
    MEASURED = "measured"
    NO_GROUND_FILL = "no ground fill"
    OUTSIDE_GROUND = "outside ground"


@dataclass(frozen=True)
class PlaneReturn:
    """Return distance of one signal via or PTH pad on one of its reference
    planes.

    `layers` are the connected layers that reference the plane. `distance`
    is inf when no copper path reaches a return, and for the outcomes other
    than MEASURED. `plane` is None outside every ground zone.
    """

    change: LayerChange
    layers: tuple[str, ...]
    plane: ReferencePlane | None
    distance: float
    served: bool
    outcome: Outcome

    @property
    def position(self) -> Point2:
        return self.change.position

    @property
    def net(self) -> str:
        return self.change.net

    @property
    def reason(self) -> str | None:
        """Why the via or pad can't be served on this plane, if no return via
        can fix it."""
        match self.outcome:
            case Outcome.NO_GROUND_FILL:
                assert self.plane is not None
                return f"no {self.plane.ground} fill nearby on {self.plane.layer}"
            case Outcome.OUTSIDE_GROUND:
                return f"{OUTSIDE_GROUND} for {self.layers[0]}"
        return None


class ReturnFields:
    """Per (plane, ground net): the fill and the distance field to its returns."""

    def __init__(self, snapshot: Snapshot) -> None:
        if snapshot.board_outline.is_empty:
            raise NoBoardOutlineError()
        self.snapshot = snapshot
        self.grid = Grid.covering(snapshot.board_outline.bounds)
        self._fills: dict[ReferencePlane, BoolMap] = {}
        self._fields: dict[ReferencePlane, FloatMap] = {}
        self._entry_cache: dict[
            tuple[Point2, float, ReferencePlane, float], Entries | None
        ] = {}

    def fill(self, plane: ReferencePlane) -> BoolMap:
        if plane not in self._fills:
            geom = self.snapshot.fill(plane.ground, plane.layer)
            self._fills[plane] = self.grid.mask(geom)
        return self._fills[plane]

    def field(self, plane: ReferencePlane) -> FloatMap:
        """Distance (mm) through the fill to the nearest return's drill wall."""
        if plane not in self._fields:
            mask = self.fill(plane).copy()
            seeds: list[Pixel] = []
            for centre, drill, disc in self._returns(plane):
                win = self.grid.bounds_window(disc.bounds)
                inside = self.grid.mask(disc, win)
                mask[win] |= inside
                seeds += self._seeds(centre, drill, win, inside)
            self._fields[plane] = geodesic(self.grid, mask, seeds)
        return self._fields[plane]

    def _seeds(
        self, centre: Point2, drill: float, win: tuple[slice, slice], inside: BoolMap
    ) -> list[Pixel]:
        """The pixels within the return's seed reach, else its centre pixel.
        The in-grid pixels of its disc when the centre is off the grid, as on
        or past the board outline."""
        g = self.grid
        px = g.index(centre)
        if not g.contains(px):
            seeds = inside
        else:
            seeds = g.mask(Point(centre).buffer(self._seed_reach(drill)), win)
            if not seeds.any():
                return [px]
        rr, cc = np.nonzero(seeds)
        return list(zip((rr + win[0].start).tolist(), (cc + win[1].start).tolist()))

    def _seed_reach(self, drill: float) -> float:
        """Radius of a return's seeds: its drill rounded to the nearest pixel,
        which errs as much inward as outward, as a centre pixel did."""
        return (drill + self.grid.pixel) / 2

    def _returns(self, plane: ReferencePlane) -> list[Return]:
        """The ground's vias and PTH pads.

        As for pourstitch stitches, one counts as a return only where fill
        lies within its thermal reach.
        """
        snap = self.snapshot
        candidates = [
            (v.position, v.drill, _via_shape(v.position, v.diameter))
            for v in snap.vias
            if v.net == plane.ground
        ] + [
            (p.position, p.drill, p.shapes[plane.layer])
            for p in snap.pads
            if p.net == plane.ground
            and p.kind is PadKind.PTH
            and p.drill
            and plane.layer in p.shapes
        ]
        reached = [(c, d, shape.buffer(THERMAL_REACH)) for c, d, shape in candidates]
        return [r for r in reached if self._reaches_fill(plane, r[2])]

    def _reaches_fill(self, plane: ReferencePlane, disc: BaseGeometry) -> bool:
        return bool(touch(self.grid, self.fill(plane), disc))

    def distance_at(
        self, change: LayerChange, plane: ReferencePlane, distance: float
    ) -> float | None:
        """Return distance of `change`; None with no fill near it."""
        entries = self._entries(change, plane, distance)
        if entries is None:
            return None
        rr, cc, hops = entries
        return float(np.min(self.field(plane)[rr, cc] + hops, initial=math.inf))

    def distance_from(
        self,
        centre: Point2,
        size: ViaSize,
        change: LayerChange,
        plane: ReferencePlane,
        distance: float,
    ) -> float:
        """`distance_at` with a via of `size` at `centre` as the only return.

        The field is limited to the served threshold, so beyond it this is
        inf. It is the same field `field` builds from that via, on less of
        the plane, so a via served here is served once the via is added.
        """
        entries = self._entries(change, plane, distance)
        disc = _via_shape(centre, size.diameter).buffer(THERMAL_REACH)
        if entries is None or not self._reaches_fill(plane, disc):
            return math.inf
        g = self.grid
        limit = served_within(distance)
        # Paths within the limit from any seed stay in here.
        win = g.window(g.index(centre), limit + self._seed_reach(size.drill))
        inside = g.mask(disc, win)
        mask = self.fill(plane)[win] | inside
        seeds = [
            (r - win[0].start, c - win[1].start)
            for r, c in self._seeds(centre, size.drill, win, inside)
        ]
        field = geodesic_in(mask, [s for s in seeds if mask[s]], g.pixel, limit)
        rr, cc, hops = entries
        rr, cc = rr - win[0].start, cc - win[1].start
        here = (rr >= 0) & (rr < mask.shape[0]) & (cc >= 0) & (cc < mask.shape[1])
        return float(np.min(field[rr[here], cc[here]] + hops[here], initial=math.inf))

    def _entries(
        self, change: LayerChange, plane: ReferencePlane, distance: float
    ) -> Entries | None:
        """The ways into the fill from `change`: pixels and the straight hop
        from its drill wall to each. None with no fill near it.

        Its void is the non-fill region around its centre, within `distance`
        of its drill wall; every fill pixel next to it is a way in.
        """
        key = (change.position, change.drill, plane, distance)
        if key not in self._entry_cache:
            self._entry_cache[key] = self._find_entries(*key)
        return self._entry_cache[key]

    def _find_entries(
        self, position: Point2, drill: float, plane: ReferencePlane, distance: float
    ) -> Entries | None:
        g = self.grid
        centre = g.index(position)
        if not g.contains(centre):
            return None
        win, near = g.disc(centre, distance + drill / 2)
        fill = self.fill(plane)[win]
        if not (fill & near).any():
            return None
        local = (centre[0] - win[0].start, centre[1] - win[1].start)
        rr, cc = np.nonzero(_void_edge(fill, near, local))
        centres = np.hypot(
            g.xs[win[1]][cc] - position[0], g.ys[win[0]][rr] - position[1]
        )
        hops = np.maximum(centres - drill / 2, 0.0)
        return rr + win[0].start, cc + win[1].start, hops

    def void(
        self, change: LayerChange, plane: ReferencePlane, distance: float
    ) -> tuple[tuple[slice, slice], BoolMap] | None:
        """The window around `change` and the void in `plane`'s fill that
        contains it, within `distance` of its drill wall: the antipad
        `distance_at` hops across. None off the grid."""
        g = self.grid
        centre = g.index(change.position)
        if not g.contains(centre):
            return None
        win, near = g.disc(centre, distance + change.drill / 2)
        local = (centre[0] - win[0].start, centre[1] - win[1].start)
        return win, _void(self.fill(plane)[win], near, local)

    def measure(
        self, references: Iterable[ChangeReferences], distance: float
    ) -> list[PlaneReturn]:
        """One result per (non-exempt via or pad, plane) and per layer with
        no plane."""
        out = []
        for refs in references:
            if refs.exempt:
                continue
            c = refs.change
            for plane in refs.planes:
                layers = tuple(L.layer for L in refs.layers if L.plane == plane)
                found = self.distance_at(c, plane, distance)
                outcome = (
                    Outcome.MEASURED if found is not None else Outcome.NO_GROUND_FILL
                )
                d = math.inf if found is None else found
                out.append(
                    PlaneReturn(
                        c,
                        layers,
                        plane,
                        d,
                        d <= served_within(distance),
                        outcome,
                    )
                )
            out += [
                PlaneReturn(
                    c, (L.layer,), None, math.inf, False, Outcome.OUTSIDE_GROUND
                )
                for L in refs.layers
                if L.plane is None
            ]
        return out


def _via_shape(position: Point2, diameter: float) -> BaseGeometry:
    return Point(position).buffer(diameter / 2)


def _void(fill: BoolMap, near: BoolMap, centre: Pixel) -> BoolMap:
    """The non-fill region containing `centre`, within `near`; empty when
    `centre` is on fill."""
    if fill[centre]:
        return np.zeros_like(fill)
    labels, _ = ndimage.label(~fill & near, VOID_CONNECTIVITY)
    return labels == labels[centre]


def _void_edge(fill: BoolMap, near: BoolMap, centre: Pixel) -> BoolMap:
    """Fill pixels next to the void containing `centre`, within `near`."""
    if fill[centre]:
        edge = np.zeros_like(fill)
        edge[centre] = True
        return edge
    void = _void(fill, near, centre)
    return ndimage.binary_dilation(void, ADJACENT) & fill & near


def measure_returns(
    snapshot: Snapshot, ground_nets: Sequence[str] | None, distance: float
) -> list[PlaneReturn]:
    """Return distance of every non-exempt signal via and PTH pad on each
    reference plane."""
    fields = ReturnFields(snapshot)
    grounds = resolve_ground_nets(snapshot, ground_nets)
    return fields.measure(change_references(snapshot, grounds), distance)
