"""Return vias: a ground via near every via and PTH pad on a non-ground net.

Each one's reference planes are resolved here; measuring the distance
through them is `returnpath`'s job, and planning by it `returnvia_planner`'s.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass

import shapely
from loguru import logger
from shapely.geometry import Point
from shapely.geometry.base import BaseGeometry

from kistitch.snapshot import Pad, PadKind, Point2, Snapshot, Via, ViaSize

DEFAULT_DISTANCE = 1.5
FALLBACK_GROUND = "GND"
DISTANCE_EPSILON = 1e-9

OUTSIDE_GROUND = "outside every ground zone"


@dataclass(frozen=True)
class ReturnViaOptions:
    distance: float = DEFAULT_DISTANCE
    ground_nets: tuple[str, ...] | None = None
    via: ViaSize | None = None
    force: bool = False


class NoGroundNetsError(ValueError):
    pass


def resolve_ground_nets(
    snapshot: Snapshot, ground_nets: Iterable[str] | None
) -> list[str]:
    """The given ground nets, else the detected ones, else the net named GND.

    Detection needs zones on two inner layers, which two-layer boards and
    single-plane stackups never have; their ground is the net named GND.
    """
    if ground_nets:
        named = sorted(ground_nets)
        for n in named:
            if not _has_copper(snapshot, n):
                logger.warning(
                    f"Ground net {n} has no zones, vias or pads on the board; check --ground-nets"
                )
        return named
    grounds = snapshot.default_ground_nets()
    if grounds:
        return grounds
    if _has_copper(snapshot, FALLBACK_GROUND):
        logger.info(
            f"No net has zones on two inner layers; using {FALLBACK_GROUND} as ground"
        )
        return [FALLBACK_GROUND]
    raise NoGroundNetsError(
        f"No ground net found: no net has zones on two inner layers and there is "
        f"no {FALLBACK_GROUND} net; name the ground nets with --ground-nets"
    )


def _has_copper(snapshot: Snapshot, net: str) -> bool:
    return any(
        item.net == net
        for item in (*snapshot.copper_zones, *snapshot.vias, *snapshot.pads)
    )


# ---- reference planes --------------------------------------------------------

MIN_CONNECTED_LAYERS = 2


@dataclass(frozen=True)
class ReferencePlane:
    layer: str
    ground: str


@dataclass(frozen=True, eq=False)
class LayerChange:
    """A signal via or PTH pad: copper through the board, measured from
    `position`, its centre.

    `copper` is its copper per layer. `inscribed` and `circumscribed` are the
    nearest and farthest reach of its outline from `position` on any layer.
    """

    id: str
    net: str
    position: Point2
    copper: Mapping[str, BaseGeometry]
    inscribed: float
    circumscribed: float

    @classmethod
    def of_via(cls, v: Via, layers: Iterable[str]) -> LayerChange:
        """A via, its disc on each of `layers`."""
        radius = v.diameter / 2
        disc = Point(v.position).buffer(radius)
        return cls(v.id, v.net, v.position, dict.fromkeys(layers, disc), radius, radius)

    @classmethod
    def of_pad(cls, p: Pad) -> LayerChange:
        centre = Point(p.position)
        shapes = list(p.shapes.values())
        # A PTH pad's copper may carry its drill as a hole; the reach is to its rim.
        rims = shapely.get_exterior_ring(shapely.get_parts(shapes))
        return cls(
            p.id,
            p.net,
            p.position,
            p.shapes,
            min(shapely.distance(centre, rims).tolist(), default=0.0),
            max((centre.hausdorff_distance(s) for s in shapes), default=0.0),
        )

    def touches(self, layer: str, geometry: BaseGeometry, reach: float = 0.0) -> bool:
        """Whether `geometry` lies within `reach` of the copper on `layer`."""
        if layer not in self.copper or geometry.is_empty:
            return False
        return geometry.distance(self.copper[layer]) <= reach + DISTANCE_EPSILON


@dataclass(frozen=True)
class LayerReference:
    """A layer a via or pad connects on; `plane` None means outside every
    ground zone."""

    layer: str
    plane: ReferencePlane | None


@dataclass(frozen=True)
class ChangeReferences:
    change: LayerChange
    # In stack order.
    layers: tuple[LayerReference, ...]

    @property
    def exempt(self) -> bool:
        """Copper connecting on fewer than two layers carries no layer change."""
        return len(self.layers) < MIN_CONNECTED_LAYERS

    @property
    def planes(self) -> tuple[ReferencePlane, ...]:
        """Distinct reference planes: each is one return measurement."""
        return tuple(dict.fromkeys(c.plane for c in self.layers if c.plane))


def layer_changes(snapshot: Snapshot, grounds: Sequence[str]) -> list[LayerChange]:
    """Every via and PTH pad on a non-ground net, by position."""
    vias = [
        LayerChange.of_via(v, snapshot.copper_layers)
        for v in snapshot.vias
        if v.net and v.net not in grounds
    ]
    pads = [
        LayerChange.of_pad(p)
        for p in snapshot.pads
        if p.kind is PadKind.PTH and p.net and p.net not in grounds
    ]
    return sorted(vias + pads, key=lambda c: c.position)


def change_references(
    snapshot: Snapshot, grounds: Sequence[str]
) -> list[ChangeReferences]:
    """Connected layers and their reference planes for every non-ground via
    and PTH pad."""
    return [
        ChangeReferences(
            c,
            tuple(
                LayerReference(L, _reference_plane(snapshot, c.position, L, grounds))
                for L in _connected_layers(snapshot, c)
            ),
        )
        for c in layer_changes(snapshot, grounds)
    ]


def reference_outlines(
    snapshot: Snapshot, layer: str, grounds: Sequence[str]
) -> Iterator[tuple[ReferencePlane, BaseGeometry]]:
    """Ground zone outlines that can be `layer`'s reference, in precedence order.

    The nearest other layer in stack order comes first, the upper one of two
    equidistant layers, then on a layer the highest zone priority.
    """
    stack = snapshot.copper_layers
    i = stack.index(layer)
    for step in range(1, len(stack)):
        for j in (i - step, i + step):
            if not 0 <= j < len(stack):
                continue
            zones = [
                z
                for z in snapshot.copper_zones
                if z.net in grounds and stack[j] in z.layers
            ]
            for z in sorted(zones, key=lambda z: -z.priority):
                assert z.net is not None
                yield ReferencePlane(stack[j], z.net), z.outline


def _reference_plane(
    snapshot: Snapshot, position: Point2, layer: str, grounds: Sequence[str]
) -> ReferencePlane | None:
    """The first reference outline containing `position`; overlapping
    outlines on a layer resolve by zone priority."""
    pt = Point(position)
    return next(
        (
            plane
            for plane, outline in reference_outlines(snapshot, layer, grounds)
            if outline.contains(pt)
        ),
        None,
    )


def tied_layers(snapshot: Snapshot, c: LayerChange) -> set[str]:
    """Layers where a same-net track or another same-net pad touches `c`."""
    layers = {
        t.layer
        for t in snapshot.tracks
        if t.net == c.net and c.touches(t.layer, t.centreline, t.width / 2)
    }
    return layers | {
        L
        for p in snapshot.pads
        if p.net == c.net and p.id != c.id
        for L, shape in p.shapes.items()
        if c.touches(L, shape)
    }


def filled_layers(snapshot: Snapshot, c: LayerChange) -> set[str]:
    """Layers where a fill of `c`'s own net touches it."""
    return {L for L in c.copper if c.touches(L, snapshot.fill(c.net, L))}


def _connected_layers(snapshot: Snapshot, c: LayerChange) -> list[str]:
    """Layers where same-net copper (track, pad or fill) touches `c`."""
    layers = tied_layers(snapshot, c) | filled_layers(snapshot, c)
    return [L for L in snapshot.copper_layers if L in layers]
