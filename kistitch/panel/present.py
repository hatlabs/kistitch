"""The panel's wording of results and errors, free of CLI flags.

Status lines (`place_summary`, `heatmap_summary`) say where a step stands
on the board; outcomes (`Outcome`) say how the last action ended, in the
activity area. The CLI keeps its own error exits.
"""

import traceback
from dataclasses import dataclass
from enum import Enum

from kipy.errors import ApiError
from kipy.errors import ConnectionError as KiCadConnectionError

from kistitch import drc
from kistitch.clear_command import Cleared
from kistitch.clearvias import RemovableVias
from kistitch.commit import CommitOutcome, CreateItemsError, UnknownNetsError
from kistitch.overlay import DEFAULT_OVERLAY_LAYERS, OverlayLayersError
from kistitch.panel.probe import BoardSwitchedError, NoBoardError
from kistitch.placement import BoardChangedError, RunResult, SplitError
from kistitch.pourstitch import OtherClassNetsError
from kistitch.pourstitch_command import StitchResult
from kistitch.progress import Cancelled
from kistitch.returnvia import NoGroundNetsError
from kistitch.returnvia_command import ReturnResult
from kistitch.snapshot import NoBoardOutlineError, kicad_busy

OVERLAY_LAYERS = f"{DEFAULT_OVERLAY_LAYERS[0]}–{DEFAULT_OVERLAY_LAYERS[-1]}"
SMALL_AREA = 0.05
"""mm²; below this an area keeps two decimals, so it doesn't read as 0.0."""


class Step(Enum):
    PAD_VIAS = "Pad vias"
    POWER = "Power stitching"
    RETURN = "Return vias"
    GROUND = "Ground stitching"

    @property
    def label(self) -> str:
        return self.value


class Kind(Enum):
    """What an action does."""

    PLACE = "place"
    HEATMAP = "heatmap"
    COUNT = "count"
    REMOVE = "remove"
    CLEAR = "clear"


class View(Enum):
    """The return vias heatmap's view."""

    UNSERVED = "unserved vias"
    ALL_COPPER = "all copper"


class Icon(Enum):
    DONE = "done"
    WARNING = "warning"
    INFO = "info"
    ERROR = "error"


@dataclass(frozen=True)
class Summary:
    """Line 1 of a status line, and a split warning under it if any."""

    icon: Icon
    lines: tuple[str, ...]


@dataclass(frozen=True)
class Outcome:
    """How the last action ended: a headline, a body, and a detail for the
    Details disclosure."""

    icon: Icon
    headline: str
    body: str = ""
    detail: str = ""
    unexpected: bool = False
    """An error kistitch does not expect: Details open by themselves, and
    the detail is its traceback."""


@dataclass(frozen=True)
class Confirmation:
    """The question before Remove vias removes what it counted."""

    title: str
    headline: str
    counts: str
    body: str
    unfilled: tuple[str, ...]
    button: str


PlaceResult = RunResult | StitchResult | ReturnResult
HeatmapResult = StitchResult | ReturnResult


def counted(n: int, noun: str, plural: str | None = None) -> str:
    return f"{n} {noun if n == 1 else plural or noun + 's'}"


def area(mm2: float) -> str:
    return f"{mm2:.2f}" if 0 < mm2 < SMALL_AREA else f"{mm2:.1f}"


def source(kind: Kind, view: View | None) -> str:
    """What produced a status line: `Place`, `Heatmap` or `Heatmap: <view>`."""
    if kind is Kind.PLACE:
        return "Place"
    return "Heatmap" if view is None else f"Heatmap: {view.value}"


def placement_of(result: PlaceResult) -> RunResult:
    if isinstance(result, RunResult):
        return result
    assert result.placement is not None
    return result.placement


def _with_splits(icon: Icon, line: str, placement: RunResult) -> Summary:
    if not placement.splits:
        return Summary(icon, (line,))
    split = counted(placement.splits, "possible pour split")
    return Summary(Icon.WARNING, (line, f"{split} marked (squares)"))


def place_summary(result: PlaceResult) -> Summary:
    placement = placement_of(result)
    vias = f"Placed {counted(placement.placed, 'via')}"
    match result:
        case RunResult():
            if placement.unfixable:
                pads = counted(placement.unfixable, "pad")
                have = "has" if placement.unfixable == 1 else "have"
                return _with_splits(
                    Icon.WARNING, f"{vias}; {pads} {have} no room (circled)", placement
                )
            if not placement.placed:
                return _with_splits(
                    Icon.DONE, "Nothing to place: every pad has a via", placement
                )
            return _with_splits(Icon.DONE, vias, placement)
        case StitchResult():
            d = f"{result.distance:g} mm"
            uncovered = sum(c.uncovered_mm2 for c in result.coverage)
            if uncovered:
                return _with_splits(
                    Icon.WARNING,
                    f"{vias}; {area(uncovered)} mm² still beyond {d}",
                    placement,
                )
            return _with_splits(Icon.DONE, f"{vias}; every point within {d}", placement)
        case ReturnResult():
            forced = f"; {result.forced} forced" if result.forced else ""
            if result.unfixable:
                line = f"{vias}; {counted(result.unfixable, 'via')} unfixable (circled)"
            elif result.unserved:
                line = f"{vias}; {_unserved(result)}"
            else:
                line = f"{vias}; every via served"
            icon = (
                Icon.WARNING
                if result.unfixable or result.unserved or result.forced
                else Icon.DONE
            )
            return _with_splits(icon, line + forced, placement)


def _unserved(result: ReturnResult) -> str:
    return (
        f"{result.unserved} of {counted(result.total, 'via')} unserved within "
        f"{result.distance:g} mm"
    )


def heatmap_summary(result: HeatmapResult) -> Summary:
    match result:
        case StitchResult():
            d = f"{result.distance:g} mm"
            beyond = [c for c in result.coverage if c.uncovered_mm2]
            unstitchable = sum(c.unstitchable_mm2 for c in result.coverage)
            suffix = (
                f"; {area(unstitchable)} mm² can't be stitched" if unstitchable else ""
            )
            if beyond:
                total = sum(c.uncovered_mm2 for c in beyond)
                return Summary(
                    Icon.WARNING,
                    (
                        (
                            f"{area(total)} mm² beyond {d} on {len(beyond)} of "
                            f"{len(result.coverage)} nets{suffix}"
                        ),
                    ),
                )
            return Summary(Icon.DONE, (f"Every point within {d} of a stitch{suffix}",))
        case ReturnResult():
            if result.unserved:
                return Summary(Icon.WARNING, (_unserved(result),))
            return Summary(
                Icon.DONE,
                (
                    (
                        f"All {counted(result.total, 'via')} served within "
                        f"{result.distance:g} mm"
                    ),
                ),
            )


def heatmap_drawn(result: HeatmapResult) -> bool:
    """Whether the heatmap had anything to map; with nothing, the run only
    removed the earlier heatmaps."""
    match result:
        case StitchResult():
            return bool(result.coverage)
        case ReturnResult():
            return result.total > 0 and (result.full_map or result.unserved > 0)


def placed_outcome(step: Step, result: PlaceResult, summary: Summary) -> Outcome:
    placement = placement_of(result)
    body = summary.lines[0]
    if not placement.placed and not placement.splits:
        return Outcome(summary.icon, f"{step.label}: nothing to place", body)
    vias = (
        f"{step.label}: placed {counted(placement.placed, 'via')}"
        if placement.placed
        else f"{step.label}: no vias placed"
    )
    if placement.splits:
        return Outcome(
            Icon.WARNING,
            f"{vias}; {counted(placement.splits, 'possible pour split')}",
            "DRC found new unconnected or isolated copper not caused by a single "
            "via. The squares on User.Eco2 mark it; fix it by hand or undo in KiCad.",
        )
    return Outcome(Icon.DONE, vias, body)


def heatmap_outcome(step: Step, result: HeatmapResult, summary: Summary) -> Outcome:
    body = summary.lines[0]
    if result.layers_enabled:
        body += f"\nEnabled {', '.join(result.layers_enabled)}"
    if not heatmap_drawn(result):
        return Outcome(Icon.DONE, f"{step.label}: no heatmap needed", body)
    if not result.overlays_placed:
        return Outcome(
            Icon.WARNING,
            f"{step.label} heatmap not placed",
            "KiCad refused the heatmap images. Details lists where they are, "
            "to place them by hand.\n" + body,
        )
    view = (
        " (all copper)" if isinstance(result, ReturnResult) and result.full_map else ""
    )
    return Outcome(Icon.DONE, f"{step.label} heatmap{view} on {OVERLAY_LAYERS}", body)


_CANCELLED = {
    Kind.PLACE: "Cancelled — no vias committed.",
    Kind.HEATMAP: "Cancelled — no heatmap placed.",
    Kind.COUNT: "Cancelled — no vias removed.",
    Kind.REMOVE: "Cancelled — no vias removed.",
    Kind.CLEAR: "Cancelled — nothing cleared.",
}


def cancelled(kind: Kind) -> Outcome:
    return Outcome(Icon.INFO, _CANCELLED[kind])


NO_VIAS = Outcome(Icon.INFO, "No stitching vias to remove on these nets.")


def removed_outcome(cleared: Cleared) -> Outcome:
    return Outcome(
        Icon.DONE,
        f"Removed {counted(cleared.vias, 'via')}.",
        "Zones refilled." if cleared.refilled else "",
    )


NOTHING_MEASURED = Summary(
    Icon.WARNING, ("Nothing measured: the chosen nets have no pours",)
)


def nothing_measured(step: Step) -> Outcome:
    """A stitching step whose chosen nets all lost their pours."""
    return Outcome(
        Icon.WARNING,
        f"{step.label}: nothing measured",
        "None of the chosen nets has pours on the board now. Choose the nets again.",
    )


def cleared_outcome(cleared: Cleared) -> Outcome:
    if cleared.commit is CommitOutcome.NOTHING:
        return Outcome(Icon.INFO, "Nothing to clear.")
    return Outcome(Icon.DONE, "Cleared markers and heatmaps.")


def confirmation(vias: RemovableVias) -> Confirmation:
    total = vias.total
    unfilled = tuple(
        f"{counted(n, 'zone')} of {net} {'has' if n == 1 else 'have'} no fill, so "
        f"vias touching only {'it' if n == 1 else 'them'} stay. Refill zones (B) "
        "in KiCad first to include them."
        for net, n in vias.unfilled_zones.items()
    )
    return Confirmation(
        title="Remove stitching vias?",
        headline=f"{counted(total, 'via')} will be removed",
        counts=" · ".join(f"{net} {n}" for net, n in vias.counts.items()),
        body=(
            "Every unlocked via that touches a pour of its own net and that no "
            "track or pad ties down goes, including vias you placed by hand. "
            "Markers and heatmaps go too."
        ),
        unfilled=unfilled,
        button=f"Remove {counted(total, 'via')}",
    )


BUSY = Outcome(
    Icon.WARNING,
    "KiCad is busy — nothing committed",
    "A dialog or tool is open in the PCB editor. Close it, then try again.",
)
LOST = Outcome(
    Icon.ERROR,
    "Lost the connection to KiCad",
    "If it happened while committing, check KiCad's undo history before running again.",
)
NO_BOARD = Outcome(
    Icon.ERROR,
    "No board open — nothing changed",
    "Open a board in KiCad's PCB editor, then try again.",
)

_CLEAN_UP_LABELS = {
    Kind.COUNT: "Remove vias",
    Kind.REMOVE: "Remove vias",
    Kind.CLEAR: "Clear markers and heatmaps",
}


def _label(step: Step | None, kind: Kind) -> str:
    if step is None:
        return _CLEAN_UP_LABELS[kind]
    return f"{step.label} heatmap" if kind is Kind.HEATMAP else step.label


def failure(error: BaseException, step: Step | None, kind: Kind) -> Outcome:
    """What the activity area says of an action that failed with `error`;
    `step` is None for Clean up's actions."""
    label = _label(step, kind)
    nothing = "nothing placed" if kind is Kind.HEATMAP else "nothing committed"
    failed = f"{label} failed — {nothing}"
    match error:
        case Cancelled():
            return cancelled(kind)
        case ApiError() if kicad_busy(error):
            return BUSY
        case KiCadConnectionError():
            return LOST
        case NoBoardError():
            return NO_BOARD
        case BoardSwitchedError():
            return Outcome(
                Icon.WARNING,
                f"{label} didn't run — KiCad shows another board",
                "The panel had read another board. It reads this one now; check "
                "the settings, then run again.",
            )
        case BoardChangedError():
            return Outcome(
                Icon.ERROR,
                failed,
                "The board changed while the run was going. Run it again; it plans "
                "against the board as it is now.",
            )
        case NoGroundNetsError():
            return Outcome(
                Icon.ERROR,
                f"{label} needs ground nets",
                "No net has zones on two or more inner layers and there is no GND "
                "net. Choose the ground nets at the top.",
            )
        case NoBoardOutlineError():
            return Outcome(
                Icon.ERROR,
                failed,
                "The board has no outline on Edge.Cuts. Draw one, then run again.",
            )
        case drc.KiCadCliNotFound():
            return Outcome(
                Icon.ERROR,
                failed,
                "kicad-cli, KiCad's command-line tool, was not found next to the "
                "running KiCad, on PATH or in the standard install locations.",
            )
        case drc.ProjectFilesError():
            return Outcome(
                Icon.ERROR,
                failed,
                "DRC reads the board's project files from disk. Save the board in "
                "KiCad, with its project file next to it, then run again.",
                str(error),
            )
        case drc.DrcError():
            return Outcome(
                Icon.ERROR,
                failed,
                "KiCad's DRC (kicad-cli) failed. The details below say why.",
                str(error),
            )
        case OverlayLayersError():
            return Outcome(
                Icon.ERROR,
                failed,
                f"Heatmaps go on {OVERLAY_LAYERS}, one layer per copper layer, and "
                "KiCad did not enable them all, or the board has more copper layers "
                "than that. Enable them in Board Setup → Board Editor Layers.",
                str(error),
            )
        case SplitError():
            return Outcome(
                Icon.ERROR,
                failed,
                "DRC found new unconnected or isolated copper not caused by a "
                "single via.",
                str(error),
            )
        case OtherClassNetsError():
            return Outcome(
                Icon.ERROR,
                failed,
                f"The chosen nets don't belong to {label}. Choose the nets again.",
            )
        case UnknownNetsError():
            return Outcome(
                Icon.ERROR,
                failed,
                "A chosen net is no longer on the board. Choose the nets again, "
                "then run.",
                str(error),
            )
        case CreateItemsError() | ApiError():
            return Outcome(
                Icon.ERROR,
                f"{label} failed — KiCad refused the change",
                "KiCad rejected the request, so nothing changed. Try again; if it "
                "persists, the details below say why.",
                str(error),
            )
        case _:
            return Outcome(
                Icon.ERROR,
                f"{label} failed — unexpected error",
                "kistitch hit an error it does not expect. Please report it with "
                "the details below.",
                "".join(traceback.format_exception(error)),
                unexpected=True,
            )
