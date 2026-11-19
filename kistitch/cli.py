import argparse
import importlib.metadata
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import kipy.board
import kipy.board_types
from kipy.errors import ApiError
from loguru import logger

import kistitch.overlay
from kistitch import clear_command, clearvias
from kistitch.overlay import DEFAULT_OVERLAY_LAYERS
from kistitch.padvia import PadviaOptions, PadviaPlanner
from kistitch.placement import (
    BoardChangedError,
    RunSettings,
    SplitError,
    run_placement,
)
from kistitch.plan import (
    POURSTITCH_FALLBACK_VIA,
    POURSTITCH_GROUND_DISTANCE,
    POURSTITCH_POWER_DISTANCE,
    StitchClass,
)
from kistitch.returnvia import DEFAULT_DISTANCE, NoGroundNetsError, ReturnViaOptions
from kistitch.snapshot import (
    NoBoardOutlineError,
    ViaSize,
    kicad_busy,
)

# Placement failures that leave the board untouched and need no traceback.
PLACEMENT_ERRORS = (SplitError, BoardChangedError, NoBoardOutlineError)

KICAD_BUSY_MESSAGE = (
    "KiCad is busy, most likely with a tool or dialog open in the PCB editor; "
    "close it and run again"
)


@contextmanager
def _exit_on(*errors: type[Exception]) -> Iterator[None]:
    """Report an expected failure as one log line and exit 1."""
    try:
        yield
    except errors as e:
        logger.error(str(e))
        raise SystemExit(1) from None


@contextmanager
def _exit_on_busy() -> Iterator[None]:
    """Report KiCad refusing a call while a tool or dialog is open, and exit 1."""
    try:
        yield
    except ApiError as e:
        if not kicad_busy(e):
            raise
        logger.error(KICAD_BUSY_MESSAGE)
        raise SystemExit(1) from None


def parse_via_size(text: str) -> ViaSize:
    """Parse DIAMETER/DRILL in mm, e.g. 0.6/0.3."""
    try:
        dia, drill = (float(v) for v in text.split("/"))
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected DIAMETER/DRILL, got {text!r}")
    if not 0 < drill < dia:
        raise argparse.ArgumentTypeError(f"need 0 < drill < diameter, got {text!r}")
    return ViaSize(dia, drill)


def add_pourstitch_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "pourstitch",
        help="Stitch pours so every point is within a distance of a via",
        description="Stitch the ground nets' pours, or with --power every other "
        "net's pours, so every point is within a distance of a via.",
    )
    p.add_argument(
        "--power",
        action="store_true",
        help="Stitch the nets that are not ground instead of the ground nets",
    )
    p.add_argument(
        "--nets",
        nargs="+",
        metavar="NET",
        help="Nets to stitch, within the run's class (default: every net of the "
        "class with pours on two or more layers)",
    )
    _add_ground_nets_argument(p)
    p.add_argument(
        "--distance",
        "-d",
        type=float,
        help="Maximum distance to a stitch, through the copper, in mm "
        f"(default: {POURSTITCH_GROUND_DISTANCE:g} for ground, "
        f"{POURSTITCH_POWER_DISTANCE:g} with --power)",
    )
    p.add_argument(
        "--via",
        type=parse_via_size,
        metavar="DIAMETER/DRILL",
        help="Via size in mm, overriding the net's netclass size",
    )
    p.add_argument(
        "--fallback-via",
        type=parse_via_size,
        default=POURSTITCH_FALLBACK_VIA,
        metavar="DIAMETER/DRILL",
        help="Smaller via size in mm for spots the netclass size doesn't fit",
    )
    _add_heatmap_arguments(p, "Only measure the existing stitching; place no vias")
    p.add_argument("--kicad-cli", metavar="PATH", help="kicad-cli binary used for DRC")
    _add_allow_splits_argument(p)


def _add_heatmap_arguments(parser: argparse.ArgumentParser, check_help: str) -> None:
    parser.add_argument("--check", action="store_true", help=check_help)
    parser.add_argument(
        "--render", type=Path, metavar="PATH", help="Write the heatmap panels to PATH"
    )
    parser.add_argument(
        "--overlay",
        action="store_true",
        help="Place per-layer heatmaps on the board as reference images",
    )
    parser.add_argument(
        "--overlay-layers",
        nargs="+",
        metavar="LAYER",
        default=list(DEFAULT_OVERLAY_LAYERS),
        help="User layers for the overlays, one per copper layer in stack order",
    )


def _add_allow_splits_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--allow-splits",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Commit even when DRC finds new unconnected or isolated copper "
        "that no via could be blamed for, marking the spots (default); "
        "--no-allow-splits refuses to commit instead",
    )


def _add_ground_nets_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--ground-nets",
        nargs="+",
        metavar="NET",
        help="Ground nets (default: nets with zones on two or more inner layers, "
        "else GND)",
    )


def execute_returnvia(args):
    from kistitch import returnvia_command

    options = ReturnViaOptions(
        distance=args.distance,
        ground_nets=tuple(args.ground_nets) if args.ground_nets else None,
        via=args.via,
        force=args.force,
    )
    outputs = kistitch.overlay.Outputs(
        render=args.render,
        overlay=args.overlay,
        overlay_layers=tuple(args.overlay_layers),
    )
    with _exit_on(
        NoGroundNetsError,
        kistitch.overlay.OverlayLayersError,
        *PLACEMENT_ERRORS,
    ):
        returnvia_command.execute(
            options,
            outputs,
            dry_run=args.dry_run,
            check=args.check,
            kicad_cli=args.kicad_cli,
            allow_splits=args.allow_splits,
            full_map=args.full_map,
            refill=args.refill,
        )


def parse_args():
    parser = argparse.ArgumentParser()
    _add_version(parser)

    parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Do everything except commit the changes",
    )
    parser.add_argument(
        "--refill",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Refill the zones before measuring or placing, after committing, "
        "and before and after clear --vias (on by default). Each refill adds "
        "an undo step in KiCad; with --no-refill, the fills are used as they are",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    padvia_parser = subparsers.add_parser(
        "padvia", help="Place a via and stub for every unserved SMD pad"
    )
    padvia_parser.add_argument(
        "--nets",
        nargs="+",
        metavar="NET",
        help="Nets whose SMD pads get vias, among pads with their net's fill, "
        "wide enough for a via, within reach on another layer "
        "(default: every such net)",
    )
    padvia_parser.add_argument(
        "--via",
        type=parse_via_size,
        metavar="DIAMETER/DRILL",
        help="Via size in mm, overriding the net's netclass size",
    )
    padvia_parser.add_argument(
        "--kicad-cli", metavar="PATH", help="kicad-cli binary used for DRC"
    )
    _add_allow_splits_argument(padvia_parser)

    add_pourstitch_parser(subparsers)

    clear_parser = subparsers.add_parser(
        "clear",
        help="Remove annotations from User.Eco2 and kistitch's overlay images",
    )
    clear_parser.add_argument(
        "--overlay-layers",
        nargs="+",
        metavar="LAYER",
        default=list(DEFAULT_OVERLAY_LAYERS),
        help="User layers to remove kistitch overlays from",
    )
    clear_parser.add_argument(
        "--vias",
        action="store_true",
        help="Also remove unlocked vias touching a pour of their own net that no "
        "track or pad ties down",
    )
    clear_parser.add_argument(
        "--nets",
        nargs="+",
        metavar="NET",
        help="Nets whose vias --vias removes (default: every net with a zone)",
    )
    returnvia_parser = subparsers.add_parser(
        "returnvia", help="Place a ground return via near every signal via and PTH pad"
    )
    returnvia_parser.add_argument(
        "--distance",
        "-d",
        type=float,
        default=DEFAULT_DISTANCE,
        help="Maximum distance from a via or pad to its return via, in mm",
    )
    _add_ground_nets_argument(returnvia_parser)
    returnvia_parser.add_argument(
        "--via",
        type=parse_via_size,
        metavar="DIAMETER/DRILL",
        help="Return via size in mm (default: the ground net's netclass)",
    )
    returnvia_parser.add_argument(
        "--force",
        action="store_true",
        help="Place best-effort vias where none fits with full clearance",
    )
    _add_heatmap_arguments(
        returnvia_parser, "Only measure the existing returns; place no vias"
    )
    returnvia_parser.add_argument(
        "--full-map",
        action="store_true",
        help="Map all copper, not only the copper around unserved via and pad ends",
    )
    returnvia_parser.add_argument(
        "--kicad-cli", metavar="PATH", help="kicad-cli binary used for DRC"
    )
    _add_allow_splits_argument(returnvia_parser)

    args = parser.parse_args()
    if args.command == "clear" and args.nets and not args.vias:
        clear_parser.error("--nets requires --vias")
    return args


def execute_clear(args):
    kicad = kipy.KiCad()
    board = kicad.get_board()
    vias = (
        clearvias.find_removable_vias(kicad, board, args.nets, args.refill)
        if args.vias
        else None
    )
    clear_command.clear(
        kicad,
        board,
        args.overlay_layers,
        vias,
        refill=args.refill,
        dry_run=args.dry_run,
    )


def execute_padvia(args):
    options = PadviaOptions(
        nets=None if args.nets is None else tuple(args.nets), via=args.via
    )
    settings = RunSettings(
        "padvia",
        dry_run=args.dry_run,
        kicad_cli=args.kicad_cli,
        allow_splits=args.allow_splits,
        refill=args.refill,
    )
    with _exit_on(*PLACEMENT_ERRORS):
        run_placement(PadviaPlanner(), options, settings)


def execute_pourstitch(args):
    from kistitch import pourstitch, pourstitch_command

    options = pourstitch.Options(
        nets=None if args.nets is None else tuple(args.nets),
        stitch_class=StitchClass.POWER if args.power else StitchClass.GROUND,
        ground_nets=tuple(args.ground_nets) if args.ground_nets else None,
        distance=args.distance,
        via=args.via,
        fallback_via=args.fallback_via,
    )
    outputs = kistitch.overlay.Outputs(
        render=args.render,
        overlay=args.overlay,
        overlay_layers=tuple(args.overlay_layers),
    )
    with _exit_on(
        NoGroundNetsError,
        pourstitch.OtherClassNetsError,
        kistitch.overlay.OverlayLayersError,
        *PLACEMENT_ERRORS,
    ):
        pourstitch_command.execute(
            options,
            outputs,
            dry_run=args.dry_run,
            check=args.check,
            kicad_cli=args.kicad_cli,
            allow_splits=args.allow_splits,
            refill=args.refill,
        )


DEFAULT_LOG_LEVEL = "INFO"
LOG_LEVEL_VARIABLE = "LOGURU_LEVEL"


def configure_logging() -> None:
    """Log to stderr at INFO, or at the level in LOGURU_LEVEL.

    Loguru's own default sink logs at DEBUG.
    """
    logger.remove()
    logger.add(sys.stderr, level=os.environ.get(LOG_LEVEL_VARIABLE, DEFAULT_LOG_LEVEL))


GUI_EXTRA_MESSAGE = (
    "The panel needs PySide6, which comes with the `gui` extra: "
    "uv tool install --python 3.14 'kistitch[gui]'"
)
GUI_EXTRA_EXIT = 3
"""`kistitch-panel`'s exit code without the `gui` extra, apart from a
failure (1) and a usage error (2): the KiCad plugin's launcher keys on it
to offer adding the extra, and keeps its own copy, since it cannot import
kistitch."""


def _add_version(parser: argparse.ArgumentParser) -> None:
    """`--version`, printing `kistitch X.Y.Z`: the plugin's launcher parses
    that line to check the installed version."""
    parser.add_argument(
        "--version",
        action="version",
        version=f"kistitch {importlib.metadata.version('kistitch')}",
    )


def parse_panel_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="kistitch-panel",
        description="Open a window beside KiCad's PCB editor that runs "
        "kistitch's steps on the open board: pad vias, power stitching, "
        "return vias and ground stitching, with their heatmaps, and clears "
        "markers, heatmaps and stitching vias. Needs the gui extra.",
    )
    _add_version(parser)
    return parser.parse_args(argv)


def panel_main(argv: list[str] | None = None) -> None:
    """The `kistitch-panel` command."""
    parse_panel_args(argv)
    configure_logging()
    try:
        from kistitch.panel.window import main as open_panel
    except ImportError as e:
        if (e.name or "").partition(".")[0] != "PySide6":
            raise
        logger.error(f"{GUI_EXTRA_MESSAGE} ({e})")
        raise SystemExit(GUI_EXTRA_EXIT) from None
    open_panel()


def main():
    configure_logging()
    args = parse_args()

    with _exit_on_busy():
        match args.command:
            case "padvia":
                execute_padvia(args)
            case "pourstitch":
                execute_pourstitch(args)
            case "returnvia":
                execute_returnvia(args)
            case "clear":
                execute_clear(args)


if __name__ == "__main__":
    main()
