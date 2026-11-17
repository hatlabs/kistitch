"""Find the stitching vias that `clear --vias` removes."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

import kipy
import kipy.board
from loguru import logger

from kistitch.commit import REFILLS_OFF_MESSAGE, refill_tolerant
from kistitch.returnvia import filled_layers, tied_layers
from kistitch.snapshot import Snapshot, Via


def stitching_vias(snapshot: Snapshot, nets: Iterable[str] | None = None) -> list[Via]:
    """Unlocked vias in a fill of their own net that no track or pad ties down.

    `nets` limits the search to those nets.
    A track or a lock is how a hand-placed via tells itself apart.
    """
    wanted = None if nets is None else set(nets)
    return [
        v
        for v in snapshot.vias
        if (wanted is None or v.net in wanted)
        and not v.locked
        and filled_layers(snapshot, v)
        and not tied_layers(snapshot, v)
    ]


@dataclass(frozen=True, kw_only=True)
class RemovableVias:
    """Stitching vias counted for removal, per net, and the selected nets'
    zones without a fill, whose vias can't be judged and stay."""

    nets: tuple[str, ...] | None
    counts: Mapping[str, int]
    unfilled_zones: Mapping[str, int]
    ids: frozenset[str]

    @property
    def total(self) -> int:
        return len(self.ids)


def find_removable_vias(
    kicad: kipy.KiCad,
    board: kipy.board.Board,
    nets: Sequence[str] | None = None,
    refill: bool = True,
) -> RemovableVias:
    """Count the stitching vias on `nets`, or on every net, after refilling
    the zones if `refill`. Changes nothing but the fills."""
    # Stitching vias are picked by the fills they touch, so stale fills
    # would pick the wrong ones.
    refilled = False
    if not refill:
        logger.info(REFILLS_OFF_MESSAGE)
    elif refill_tolerant(kicad, board):
        refilled = True
    else:
        logger.warning("Zone refill not confirmed; fills may be stale")
    snapshot = Snapshot.from_board(board)
    unfilled = _unfilled_zones(snapshot, nets)
    _warn_unclearable_nets(snapshot, nets, unfilled, refilled)
    found = stitching_vias(snapshot, nets)
    return RemovableVias(
        nets=None if nets is None else tuple(nets),
        counts=dict(Counter(v.net for v in found)),
        unfilled_zones=dict(unfilled),
        ids=frozenset(v.id for v in found),
    )


def _selected_nets(snapshot: Snapshot, nets: Sequence[str] | None) -> set[str]:
    return {z.net for z in snapshot.copper_zones} if nets is None else set(nets)


def _unfilled_zones(snapshot: Snapshot, nets: Sequence[str] | None) -> Counter[str]:
    """The zones without a fill per selected net."""
    selected = _selected_nets(snapshot, nets)
    return Counter(
        z.net for z in snapshot.copper_zones if z.net in selected and not z.fills
    )


def _warn_unclearable_nets(
    snapshot: Snapshot,
    nets: Sequence[str] | None,
    unfilled: Counter[str],
    refilled: bool,
) -> None:
    """Warn about selected nets whose vias can't be judged by their fills.

    After a confirmed refill an empty zone fills to nothing, so asking for
    another refill would only repeat it.
    """
    zoned = {z.net for z in snapshot.copper_zones}
    for net in sorted(_selected_nets(snapshot, nets) - zoned):
        logger.warning(f"No copper zone on net {net}; it has no vias to clear")
    for net, n in sorted(unfilled.items()):
        zones = f"{n} zone{'s' if n > 1 else ''} of {net} {'have' if n > 1 else 'has'}"
        them = "them" if n > 1 else "it"
        if refilled:
            logger.warning(
                f"{zones} no fill after the refill, so vias that touch only {them} stay"
            )
        else:
            logger.warning(
                f"{zones} no fill, so vias that touch only {them} stay; press B "
                "in KiCad to refill zones and run again"
            )
