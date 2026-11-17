"""The `clear` command: remove kistitch's markers, overlays and, if asked,
stitching vias in one commit."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

import kipy
import kipy.board
import kipy.board_types
from loguru import logger

from kistitch.clearvias import RemovableVias, stitching_vias
from kistitch.commit import (
    UNKNOWN_PUSH_REFILL_MESSAGE,
    CommitOutcome,
    commit_label,
    marker_shapes,
    push_tolerant,
    refill_after_commit,
)
from kistitch.overlay import board_placement, overlays_on
from kistitch.progress import NO_PROGRESS, Progress, Stage
from kistitch.snapshot import Snapshot, outline_from_edge_paths, read_edge_paths


@dataclass(frozen=True, kw_only=True)
class Cleared:
    """The vias known to have been removed, and how the commit went."""

    vias: int
    commit: CommitOutcome


def clear(
    kicad: kipy.KiCad,
    board: kipy.board.Board,
    overlay_layers: Sequence[str],
    vias: RemovableVias | None = None,
    refill: bool = True,
    dry_run: bool = False,
    progress: Progress = NO_PROGRESS,
) -> Cleared:
    """Remove the markers, kistitch's overlays and, of `vias`, those that
    are still stitching vias, in one commit; then refill the zones if `refill`,
    vias were removed and the commit is known to have landed. A stop is
    honoured up to the commit."""
    progress.enter(Stage.READ)
    shapes = board.get_shapes()
    eco2_shapes = marker_shapes(shapes)
    outline = outline_from_edge_paths(read_edge_paths(shapes))
    images = (
        []
        if outline.is_empty
        else overlays_on(board, overlay_layers, board_placement(outline.bounds))
    )
    removed = [] if vias is None else _still_removable(board, vias, dry_run)

    if not eco2_shapes and not images and not removed:
        logger.info("Nothing to clear")
        return Cleared(vias=0, commit=CommitOutcome.NOTHING)

    progress.enter(Stage.COMMIT)
    commit = board.begin_commit()
    board.remove_items([*eco2_shapes, *images, *removed])
    if dry_run:
        board.drop_commit(commit)
        return Cleared(vias=0, commit=CommitOutcome.NOTHING)
    if not push_tolerant(board, commit, commit_label("clear")):
        if removed and refill:
            logger.warning(UNKNOWN_PUSH_REFILL_MESSAGE)
        return Cleared(vias=0, commit=CommitOutcome.UNKNOWN)
    if removed and refill:
        # Close the fills over the holes the removed vias leave.
        progress.report(Stage.REFILL, None)
        refill_after_commit(kicad, board)
    return Cleared(vias=len(removed), commit=CommitOutcome.LANDED)


def _still_removable(
    board: kipy.board.Board, vias: RemovableVias, dry_run: bool
) -> list[kipy.board_types.Via]:
    """The counted vias that are stitching vias on the board as it is now."""
    found = [
        v
        for v in stitching_vias(Snapshot.from_board(board), vias.nets)
        if v.id in vias.ids
    ]
    if found:
        counts = Counter(v.net for v in found)
        logger.info(
            f"{'Would remove' if dry_run else 'Removing'} stitching vias: "
            + ", ".join(f"{n} on {net}" for net, n in counts.items())
        )
    ids = {v.id for v in found}
    return [v for v in board.get_vias() if v.id.value in ids]
