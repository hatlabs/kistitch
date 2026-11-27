"""The `pourstitch` command: plan, verify and commit, then render and overlay."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import kipy
import kipy.board
from loguru import logger

from kistitch import overlay, render
from kistitch.commit import CommitOutcome
from kistitch.placement import (
    RunResult,
    RunSettings,
    check_outline,
    measure_only,
    run_placement,
)
from kistitch.plan import Plan
from kistitch.pourstitch import (
    LayerCoverage,
    NetCoverage,
    Options,
    PourStitcher,
    by_net,
    summarise,
)
from kistitch.progress import NO_PROGRESS, Progress, Stage
from kistitch.raster import CoverageMap
from kistitch.snapshot import Snapshot

COMMAND = "pourstitch"
OVERLAY_COMMIT = "kistitch pourstitch overlay"
OVER_COLOUR = "#ff2a6d"
EMPTY_TITLE = "Pour stitching: no {nets} nets to stitch"
STYLE = render.HeatmapStyle(
    title=(
        "Pour stitching: filled = planned via, ring = existing via, "
        "cyan x = no via fits, grey = out of reach of any two-layer spot"
    ),
    colourbar_label=(
        "distance to nearest stitch through the copper (mm); pink > {distance:g} mm"
    ),
    png_prefix="pourstitch",
    colormap="viridis",
    over_colour=OVER_COLOUR,
    unreachable_colour=OVER_COLOUR,
)


@dataclass(frozen=True, kw_only=True)
class StitchResult:
    """The coverage after the run at `distance`, with the placement unless
    the run only measured.

    After a placement the coverage is of the planned board: it holds once
    the placement landed (`placement.commit`).
    """

    distance: float
    coverage: tuple[NetCoverage, ...]
    placement: RunResult | None
    layers_enabled: tuple[str, ...]
    overlays_placed: bool


def _log_coverage(
    stitcher: PourStitcher,
    snapshot: Snapshot,
    options: Options,
    coverage: Sequence[LayerCoverage],
) -> None:
    counts = stitcher.stitch_counts(snapshot, options)
    for c in coverage:
        unstitchable = (
            f"; {c.unstitchable_mm2:.2f} mm² can't be stitched (no two-layer spot within reach)"
            if c.unstitchable_mm2
            else ""
        )
        logger.info(
            f"  {c.net} on {c.layer}: {c.uncovered_mm2:.2f} mm² ({c.uncovered_pixels} px) "
            f"beyond {options.max_distance:g} mm; {counts[c.net]} existing stitches"
            + unstitchable
        )


def markers(
    snapshot: Snapshot, plan: Plan, maps: Sequence[CoverageMap]
) -> dict[render.MapKey, render.Markers]:
    """Per map: the net's existing vias as rings, its planned vias as dots
    and its unfixable spots, in plan order, as crosses."""
    return {
        (m.net, m.layer): render.Markers(
            rings=tuple(
                (v.position, v.diameter / 2) for v in snapshot.vias if v.net == m.net
            ),
            dots=tuple(
                (u.position, u.size.diameter / 2) for u in plan.units if u.net == m.net
            ),
            crosses=tuple(
                (s.layer, s.position) for s in plan.unfixable if s.net == m.net
            ),
        )
        for m in maps
    }


def execute(
    options: Options,
    outputs: overlay.Outputs,
    *,
    dry_run: bool = False,
    check: bool = False,
    kicad_cli: str | None = None,
    allow_splits: bool = True,
    refill: bool = True,
    kicad: kipy.KiCad | None = None,
    progress: Progress = NO_PROGRESS,
) -> StitchResult:
    """Run pourstitch against the open board.

    `check` only measures the existing stitching: no planning, no kicad-cli,
    no vias. With overlays, it removes the markers in the overlay commit,
    since the heatmap shows what they marked. Overlays are placed in their
    own commit; a dry run only writes their PNGs. A run stops on request up
    to its overlay commit, or up to its via commit once it attempts one.
    """
    kicad = kicad or kipy.KiCad()
    board = kicad.get_board()
    check_outline(board)
    layers = overlay.prepare_overlay_layers(board, outputs, dry_run)

    stitcher = PourStitcher(progress)
    result: RunResult | None = None
    if check:
        snapshot, plan = measure_only(kicad, board, refill, progress, dry_run=dry_run)
    else:
        result = run_placement(
            stitcher,
            options,
            RunSettings(
                COMMAND,
                dry_run,
                kicad_cli,
                mark_unfixable=False,
                allow_splits=allow_splits,
                refill=refill,
            ),
            kicad,
            progress=progress,
        )
        snapshot, plan = result.snapshot, result.prune.plan

    # A placement that attempted its commit is past its last stop point;
    # one that committed nothing still stops before measuring.
    committed = result is not None and result.commit is not CommitOutcome.NOTHING
    after = progress.without_stop() if committed else progress
    after.enter(Stage.MEASURE)
    grid = stitcher.grid(snapshot, options)
    maps = stitcher.coverage(snapshot, options, plan.units)
    coverage = summarise(maps, options.max_distance, grid.pixel)
    logger.info(f"{COMMAND} coverage" + (" (check only)" if check else ""))
    _log_coverage(stitcher, snapshot, options, coverage)

    if outputs.render is not None or outputs.overlay:
        after.enter(Stage.DRAW)
    if outputs.render is not None:
        if maps:
            render.render_panels(
                outputs.render,
                snapshot,
                grid,
                maps,
                markers(snapshot, plan, maps),
                options.max_distance,
                STYLE,
            )
        else:
            # Replaces the previous run's heatmap, which would no longer apply.
            render.render_outline(
                outputs.render,
                snapshot,
                grid,
                EMPTY_TITLE.format(nets=options.stitch_class.value),
            )
        logger.info(f"Wrote {outputs.render}")

    # After a lost placement push the overlay commit would go over the same
    # connection; the PNGs are written for placing by hand instead.
    place = layers.place and (
        result is None or result.commit is not CommitOutcome.UNKNOWN
    )
    if layers.place and not place:
        logger.warning(overlay.LOST_PUSH_OVERLAY_MESSAGE)
    overlays_placed = False
    if outputs.overlay:
        # On the board, existing and planned vias show as themselves.
        overlays_placed = overlay.show_overlays(
            board,
            (
                lambda out_dir: render.write_overlays(
                    out_dir, grid, maps, {}, options.max_distance, STYLE
                )
            )
            if maps
            else None,
            snapshot,
            outputs,
            place,
            OVERLAY_COMMIT,
            () if check else None,
            after,
            dry_run=dry_run,
        )
    return StitchResult(
        distance=options.max_distance,
        coverage=by_net(coverage),
        placement=result,
        layers_enabled=layers.enabled,
        overlays_placed=overlays_placed,
    )
