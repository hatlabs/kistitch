"""The panel's between-run read of the board (`Prober`): which board KiCad
shows, and the nets its pickers offer with the ground nets a run detects.

The read is light: the zones' nets and layers and the board's net names,
no fills or geometry. Detection follows `returnvia.resolve_ground_nets`
on that read.
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePath

import kipy
import kipy.board
from kipy.errors import ApiError
from kipy.errors import ConnectionError as KiCadConnectionError
from kipy.proto.board.board_types_pb2 import ZoneType
from kipy.util.board_layer import canonical_name
from shapely.geometry import Polygon

from kistitch.returnvia import FALLBACK_GROUND
from kistitch.snapshot import Snapshot, Zone, kicad_busy

PROBE_TIMEOUT_MS = 10_000
"""A probe's KiCad timeout: it reads little, and a stuck one blocks the
next."""


class NoBoardError(RuntimeError):
    """KiCad has no board open in its PCB editor."""


class BoardSwitchedError(RuntimeError):
    """KiCad shows another board than the one a run was started for."""


@dataclass(frozen=True)
class BoardId:
    identity: str
    """The board's full path when KiCad gives its project directory, else
    its file name: what tells two boards apart."""
    name: str
    """The board's file name, for the window title."""


class GroundSource(Enum):
    INNER_ZONES = "zones on two or more inner layers"
    FALLBACK = "the net named GND"
    NONE = "none found"


@dataclass(frozen=True)
class BoardNets:
    """The board's copper zones by net, and its net names."""

    snapshot: Snapshot
    """The zones' nets and layers only, without outlines or fills."""
    names: frozenset[str]

    @property
    def zoned(self) -> tuple[str, ...]:
        """The nets with copper zones, sorted."""
        return tuple(
            sorted({z.net for z in self.snapshot.copper_zones if z.net is not None})
        )

    def stitchable(self, net: str) -> bool:
        return self.snapshot.is_stitchable(net)

    @property
    def detected(self) -> tuple[tuple[str, ...], GroundSource]:
        """The ground nets a run detects, and how it finds them."""
        grounds = self.snapshot.default_ground_nets()
        if grounds:
            return tuple(grounds), GroundSource.INNER_ZONES
        if FALLBACK_GROUND in self.names:
            return (FALLBACK_GROUND,), GroundSource.FALLBACK
        return (), GroundSource.NONE


@dataclass(frozen=True)
class Probe:
    board: BoardId
    nets: BoardNets


def connect(timeout_ms: int) -> kipy.KiCad:
    return kipy.KiCad(timeout_ms=timeout_ms)


def open_board(kicad: kipy.KiCad) -> kipy.board.Board:
    """The board open in KiCad's PCB editor; `NoBoardError` when there is
    none, and KiCad's busy error as it is."""
    try:
        return kicad.get_board()
    except ApiError as e:
        if kicad_busy(e):
            raise
        raise NoBoardError(str(e)) from None


def board_id(board: kipy.board.Board) -> BoardId:
    document = board.document
    name = document.board_filename
    directory = document.project.path
    return BoardId(str(PurePath(directory) / name) if directory else name, name)


def read_nets(board: kipy.board.Board) -> BoardNets:
    zones = [
        Zone(
            net=z.net.name if z.net is not None and z.net.name else None,
            layers=tuple(canonical_name(L) for L in z.layers),
            priority=0,
            outline=Polygon(),
        )
        for z in board.get_zones()
        if z.type == ZoneType.ZT_COPPER
    ]
    layers = tuple(sorted({L for z in zones for L in z.layers if L.endswith(".Cu")}))
    return BoardNets(
        Snapshot(copper_layers=layers, zones=zones),
        frozenset(n.name for n in board.get_nets()),
    )


class Prober:
    """Reads the board on one KiCad connection of its own, made on first
    use and made again after it is lost. Not for two threads at once."""

    def __init__(
        self, make: Callable[[], kipy.KiCad] = lambda: connect(PROBE_TIMEOUT_MS)
    ) -> None:
        self._make = make
        self._kicad: kipy.KiCad | None = None

    def __call__(self) -> Probe:
        try:
            if self._kicad is None:
                self._kicad = self._make()
            board = open_board(self._kicad)
            return Probe(board_id(board), read_nets(board))
        except KiCadConnectionError:
            self._kicad = None
            raise
