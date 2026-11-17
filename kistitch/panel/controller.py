"""The panel's state and everything its window shows, free of Qt.

The panel connects (`Connection`) by probing the board when its window is
activated or on Retry, never while an action is active. An action is one
Place or Heatmap of a step, or a Clean up action; one is active at a time,
from its start until its result or error arrives, and Remove vias stays
active from its count through the confirmation to its removal. A step or
Clean up action asked for while a probe reads the board is held, shown
as starting, until the probe answers: it runs if the board is the one
read before, and is dropped with the probe's outcome otherwise; a newer
request or Cancel replaces or discards it. Try again starts at once,
since it repeats an action on the board it was made for. Every action runs
as a `Job` on a worker with the board identity of the last probe, so a run
refuses when KiCad shows another board. A probe that answers after an
action started is set aside, and the board is read again once the panel
is idle.

Each step's status line holds its latest Heatmap or Place result in the
session and when it came. The panel tracks which result left the markers
and which the heatmap on the board, so that a later action that replaces
or clears them notes it on that line; an undo or edit in KiCad goes
unseen. A cancelled, busy or failed action leaves every status line
as it was, and so does a placement whose commit is unknown. A result is
shown as it is, even when Cancel was pressed: the stop can reach the
worker after its commit. Only Remove vias' count, which cannot stop, is
discarded on Cancel.

All methods run on one thread, the controller's. Workers report back
through the injected `post`, which runs a callable on that thread; the
work itself goes to `spawn`, which runs it on a new worker thread, a
daemon one for a probe.
"""

import math
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from functools import partial
from typing import Literal, Protocol

import kipy
from kipy.errors import ApiError
from kipy.errors import ConnectionError as KiCadConnectionError
from loguru import logger

from kistitch import pourstitch, pourstitch_command, returnvia_command
from kistitch.clear_command import Cleared, clear
from kistitch.clearvias import RemovableVias, find_removable_vias
from kistitch.commit import CommitOutcome
from kistitch.overlay import DEFAULT_OVERLAY_LAYERS, Outputs
from kistitch.padvia import PadviaOptions, PadviaPlanner
from kistitch.panel.present import (
    BUSY,
    LOST,
    NO_BOARD,
    NO_VIAS,
    NOTHING_MEASURED,
    OVERLAY_LAYERS,
    Confirmation,
    HeatmapResult,
    Icon,
    Kind,
    Outcome,
    PlaceResult,
    Step,
    Summary,
    View,
    cancelled,
    cleared_outcome,
    confirmation,
    failure,
    heatmap_drawn,
    heatmap_outcome,
    heatmap_summary,
    nothing_measured,
    place_summary,
    placed_outcome,
    placement_of,
    removed_outcome,
    source,
)
from kistitch.panel.probe import (
    BoardId,
    BoardNets,
    BoardSwitchedError,
    GroundSource,
    NoBoardError,
    Probe,
    board_id,
    connect,
    open_board,
)
from kistitch.placement import RunResult, RunSettings, run_placement
from kistitch.plan import (
    POURSTITCH_FALLBACK_VIA,
    POURSTITCH_GROUND_DISTANCE,
    POURSTITCH_POWER_DISTANCE,
    StitchClass,
)
from kistitch.pourstitch_command import StitchResult
from kistitch.progress import Count, Progress, Stage
from kistitch.returnvia import DEFAULT_DISTANCE, ReturnViaOptions
from kistitch.returnvia_command import ReturnResult
from kistitch.snapshot import ViaSize, kicad_busy

RUN_TIMEOUT_MS = 120_000
"""A run's KiCad timeout, for reading and committing large boards."""
PADVIA_COMMAND = "padvia"

TITLE = "kistitch"
READY = "Ready. The steps are in their usual order; run any of them at any time."
CONNECTING = "Connecting to KiCad…"
READING = "Reading the board…"
NOT_CONNECTED = "Not connected"
NO_RESULT = "No result yet"
NEEDS_GROUND = "Needs ground nets: choose them at the top"
NO_GROUND_POWER = "No ground net: stitches every net with pours"
BUSY_HINT = (
    "KiCad is busy: close any dialog or tool in the PCB editor. The panel "
    "shows the board as last read."
)
CANCEL = "Cancel (Esc)"
CANCELLING = "Cancelling…"
TOO_LATE = "Committing — too late to cancel."
SHORT_LIST = 3
"""A chosen net list longer than this shows its first two and a count."""
SHOWN_OF_LONG = 2


class Connection(Enum):
    CONNECTING = "connecting"
    CONNECTED = "connected"
    NO_KICAD = "no KiCad"
    NO_BOARD = "no board"


class Picker(Enum):
    """The net pickers: Setup's ground nets, a step's nets, Clean up's."""

    GROUND = "ground"
    PAD_VIAS = "pad vias"
    POWER = "power"
    STITCH_GROUND = "ground stitching"
    CLEAN_UP = "clean up"


STEP_PICKERS: Mapping[Step, Picker] = {
    Step.PAD_VIAS: Picker.PAD_VIAS,
    Step.POWER: Picker.POWER,
    Step.GROUND: Picker.STITCH_GROUND,
}
NEEDS_GROUND_STEPS = frozenset({Step.RETURN, Step.GROUND})


class Field(Enum):
    DISTANCE = "distance"
    VIA = "via"
    FALLBACK_VIA = "fallback via"


@dataclass(frozen=True)
class StepFields:
    """A step's options as the user gives them; an empty size means the
    default: the netclass's via, or `POURSTITCH_FALLBACK_VIA`."""

    distance: float | None = None
    via: str = ""
    fallback_via: str = ""
    force: bool = False


FALLBACK_TEXT = (
    f"{POURSTITCH_FALLBACK_VIA.diameter:g}/{POURSTITCH_FALLBACK_VIA.drill:g}"
)
DEFAULT_FIELDS: Mapping[Step, StepFields] = {
    Step.PAD_VIAS: StepFields(),
    Step.POWER: StepFields(
        distance=POURSTITCH_POWER_DISTANCE, fallback_via=FALLBACK_TEXT
    ),
    Step.RETURN: StepFields(distance=DEFAULT_DISTANCE),
    Step.GROUND: StepFields(
        distance=POURSTITCH_GROUND_DISTANCE, fallback_via=FALLBACK_TEXT
    ),
}


# What a worker runs.


@dataclass(frozen=True)
class PadJob:
    options: PadviaOptions
    refill: bool


@dataclass(frozen=True)
class StitchJob:
    options: pourstitch.Options
    refill: bool
    heatmap: bool


@dataclass(frozen=True)
class ReturnJob:
    options: ReturnViaOptions
    refill: bool
    view: View | None
    """The heatmap's view; None places vias."""


@dataclass(frozen=True)
class CountJob:
    nets: tuple[str, ...] | None
    refill: bool


@dataclass(frozen=True)
class RemoveJob:
    vias: RemovableVias
    refill: bool


@dataclass(frozen=True)
class ClearJob:
    pass


Job = PadJob | StitchJob | ReturnJob | CountJob | RemoveJob | ClearJob
Result = RunResult | HeatmapResult | RemovableVias | Cleared
RunJob = Callable[[Job, str, Progress], Result]


def run_job(
    job: Job,
    board: str,
    progress: Progress,
    make: Callable[[], kipy.KiCad] = lambda: connect(RUN_TIMEOUT_MS),
) -> Result:
    """Run `job` on a KiCad connection of its own, refusing with
    `BoardSwitchedError` unless KiCad shows `board`."""
    kicad = make()
    found = open_board(kicad)
    shown = board_id(found).identity
    if shown != board:
        raise BoardSwitchedError(f"KiCad shows {shown}, not {board}")
    match job:
        case PadJob():
            return run_placement(
                PadviaPlanner(progress),
                job.options,
                RunSettings(PADVIA_COMMAND, refill=job.refill),
                kicad,
                progress=progress,
            )
        case StitchJob():
            return pourstitch_command.execute(
                job.options,
                Outputs(overlay=job.heatmap),
                check=job.heatmap,
                refill=job.refill,
                kicad=kicad,
                progress=progress,
            )
        case ReturnJob():
            return returnvia_command.execute(
                job.options,
                Outputs(overlay=job.view is not None),
                check=job.view is not None,
                full_map=job.view is View.ALL_COPPER,
                refill=job.refill,
                kicad=kicad,
                progress=progress,
            )
        case CountJob():
            return find_removable_vias(kicad, found, job.nets, job.refill)
        case RemoveJob():
            return clear(
                kicad,
                found,
                DEFAULT_OVERLAY_LAYERS,
                job.vias,
                refill=job.refill,
                progress=progress,
            )
        case ClearJob():
            return clear(kicad, found, DEFAULT_OVERLAY_LAYERS, progress=progress)


Post = Callable[[Callable[[], None]], None]
"""Runs a callable on the controller's thread."""


class Spawn(Protocol):
    """Runs a callable on a new worker thread, a daemon one if `daemon`."""

    def __call__(self, work: Callable[[], None], *, daemon: bool) -> None: ...


# What the window shows.


@dataclass(frozen=True)
class Banner:
    headline: str
    body: str


NO_KICAD_BANNER = Banner(
    "Cannot reach KiCad",
    "Start KiCad 10 with the API server on (Preferences > Plugins > Enable "
    "KiCad API) and open a board in the PCB editor.",
)
NO_BOARD_BANNER = Banner(
    "No board open",
    "Open a board in KiCad's PCB editor. If a board is open, close other "
    "KiCad windows.",
)
BUSY_BANNER = Banner(
    "KiCad is busy",
    "Close any dialog or tool in KiCad's PCB editor, then retry.",
)


@dataclass(frozen=True)
class StatusLine:
    """A step's status line: line 1 with its icon (none without a result),
    line 2 (`detail`) with what produced it and when, the clock time as a
    tooltip, and a note on why the step can't run as is."""

    icon: Icon | None
    lines: tuple[str, ...]
    detail: str
    tooltip: str
    note: str


@dataclass(frozen=True)
class NetPicker:
    candidates: tuple[str, ...]
    chosen: tuple[str, ...] | None
    """None: every candidate, following the board."""
    text: str
    gone: str
    """Which chosen nets left the board, until the next choice."""
    enabled: bool


@dataclass(frozen=True)
class GroundNets:
    candidates: tuple[str, ...]
    chosen: tuple[str, ...]
    text: str
    origin: str
    """Where the nets come from: how they were detected, or the user's
    choice."""
    use_detected: bool
    """Whether the choice is the user's, so Use detected shows."""
    enabled: bool
    gone: str
    """Which chosen nets left the board, until the next choice."""


@dataclass(frozen=True)
class Activity:
    """The activity area while an action runs."""

    headline: str
    stage: str
    count: Count | None
    """Progress within the stage, for the bar; None: busy indicator."""
    elapsed: str
    hint: str
    cancel_enabled: bool
    cancel_label: str


Since = Literal["replaced", "cleared"]
"""What a later action did to the markers or heatmap a result left."""


@dataclass(eq=False)
class _Record:
    """A status line's result, with what later actions did to what it left."""

    summary: Summary
    source: str
    at: datetime
    view: View | None = None
    markers_since: Since | None = None
    heatmap_since: Since | None = None


@dataclass(frozen=True)
class _Action:
    kind: Kind
    step: Step | None
    board: str
    job: Job
    view: View | None = None


@dataclass
class _Run:
    action: _Action
    started: datetime
    stop: threading.Event = field(default_factory=threading.Event)
    stage: Stage | None = None
    count: Count | None = None
    committed: bool = False


def ago(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h ago" if not minutes else f"{hours} h {minutes} min ago"


def elapsed(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    return f"{seconds // 60}:{seconds % 60:02d}"


def names(nets: Sequence[str]) -> str:
    if len(nets) <= SHORT_LIST:
        return ", ".join(nets)
    return ", ".join(nets[:SHOWN_OF_LONG]) + f", … ({len(nets)})"


def parse_via(text: str) -> ViaSize | str | None:
    """The size in `text`, None when it is empty, or what is wrong with it."""
    text = text.strip()
    if not text:
        return None
    try:
        diameter, drill = (float(v) for v in text.split("/"))
    except ValueError:
        return "Expected diameter/drill, e.g. 0.6/0.3"
    if not (math.isfinite(diameter) and math.isfinite(drill)):
        return "Expected diameter/drill, e.g. 0.6/0.3"
    if diameter <= 0 or drill <= 0:
        return "Sizes must be more than 0"
    if drill >= diameter:
        return "Drill must be smaller than the diameter"
    return ViaSize(diameter, drill)


def _notes(record: _Record) -> str:
    markers, heatmap = record.markers_since, record.heatmap_since
    if markers and markers == heatmap:
        return f"(markers and heatmap since {markers})"
    parts = [f"markers since {markers}"] if markers else []
    if heatmap:
        parts.append(f"heatmap since {heatmap}")
    return f"({', '.join(parts)})" if parts else ""


def _measured_nothing(action: _Action, result: PlaceResult) -> bool:
    """Whether a stitching step's chosen nets all lost their pours."""
    job = action.job
    return (
        isinstance(result, StitchResult)
        and not result.coverage
        and isinstance(job, StitchJob)
        and job.options.nets is not None
    )


_RUNNING_LINES = {Kind.PLACE: "Placing vias…", Kind.HEATMAP: "Making the heatmap…"}
_PLAN_ITEMS = {
    Step.PAD_VIAS: "pad",
    Step.POWER: "net",
    Step.GROUND: "net",
    Step.RETURN: "via",
}


class Controller:
    """The panel's state; `notify` hears each change, and `closable` hears
    when a close the window asked for mid-run can go ahead."""

    def __init__(
        self,
        *,
        post: Post,
        spawn: Spawn,
        probe: Callable[[], Probe],
        run: RunJob = run_job,
        clock: Callable[[], datetime] = datetime.now,
        notify: Callable[[], None] = lambda: None,
        closable: Callable[[], None] = lambda: None,
    ) -> None:
        self._post = post
        self._spawn = spawn
        self._probe = probe
        self._run_job = run
        self._clock = clock
        self._notify = notify
        self._closable = closable

        self._connection = Connection.CONNECTING
        self._board: BoardId | None = None
        self._nets: BoardNets | None = None
        self._busy = False
        self._probing = False
        self._reprobe = False
        """Whether to read the board once the panel is idle, after a probe
        answered too late to count."""
        self._runs = 0
        """How many actions have started; a probe sent before the latest
        is set aside."""

        self._refill = True
        self._fields: dict[Step, StepFields] = dict(DEFAULT_FIELDS)
        self._choices: dict[Picker, tuple[str, ...] | None] = dict.fromkeys(Picker)
        self._gone_nets: dict[Picker, tuple[str, ...]] = {p: () for p in Picker}

        self._records: dict[Step, _Record] = {}
        self._markers_owner: _Record | None = None
        self._heatmap_owner: _Record | None = None

        self._run: _Run | None = None
        self._held: _Run | None = None
        """An action asked for while the board was being read, launched
        once the probe answers with the same board."""
        self._pending: tuple[RemovableVias, Confirmation, str] | None = None
        """A count waiting for confirmation, with the board it was for."""
        self._outcome: Outcome | None = None
        self._outcome_at: datetime | None = None
        self._retry: _Action | None = None
        self._log: list[str] = []
        self._details_open = False
        self._closing = False

    # What the window shows.

    @property
    def connection(self) -> Connection:
        return self._connection

    @property
    def _connected(self) -> bool:
        return self._connection is Connection.CONNECTED

    @property
    def title(self) -> str:
        if self._board is None or not self._connected:
            return TITLE
        return f"{TITLE} — {self._board.name}"

    @property
    def banner(self) -> Banner | None:
        if self._busy and not self._connected:
            return BUSY_BANNER
        match self._connection:
            case Connection.NO_KICAD:
                return NO_KICAD_BANNER
            case Connection.NO_BOARD:
                return NO_BOARD_BANNER
            case _:
                return None

    @property
    def busy_hint(self) -> str:
        return BUSY_HINT if self._busy and self._connected else ""

    @property
    def idle_text(self) -> str:
        if self._busy and not self._connected:
            return NOT_CONNECTED
        match self._connection:
            case Connection.CONNECTING:
                return CONNECTING
            case Connection.CONNECTED:
                return READING if self._probing else READY
            case _:
                return NOT_CONNECTED

    @property
    def running(self) -> bool:
        return self._run is not None or self._held is not None

    @property
    def _active(self) -> bool:
        return self._run is not None or self._pending is not None

    @property
    def editable(self) -> bool:
        """Whether fields take edits; read-only while an action is active."""
        return not self._active

    @property
    def refill(self) -> bool:
        return self._refill

    @property
    def refill_enabled(self) -> bool:
        return not self._active

    def fields(self, step: Step) -> StepFields:
        return self._fields[step]

    def field_errors(self, step: Step) -> Mapping[Field, str]:
        f = self._fields[step]
        errors: dict[Field, str] = {}
        if f.distance is not None and not (
            math.isfinite(f.distance) and f.distance > 0
        ):
            errors[Field.DISTANCE] = "Distance must be more than 0"
        for which, text in ((Field.VIA, f.via), (Field.FALLBACK_VIA, f.fallback_via)):
            parsed = parse_via(text)
            if isinstance(parsed, str):
                errors[which] = parsed
        return errors

    @property
    def _ready(self) -> bool:
        """Connected, with no action active. An action asked for while the
        board is being read waits for it."""
        return self._connected and not self._active

    def step_enabled(self, step: Step) -> bool:
        return (
            self._ready
            and not self.field_errors(step)
            and not (step in NEEDS_GROUND_STEPS and not self._grounds)
        )

    @property
    def cleanup_enabled(self) -> bool:
        return self._ready

    def status(self, step: Step) -> StatusLine:
        note = ""
        if self._nets is not None and not self._grounds:
            if step in NEEDS_GROUND_STEPS:
                note = NEEDS_GROUND
            elif step is Step.POWER:
                note = NO_GROUND_POWER
        record = self._records.get(step)
        if record is None:
            return StatusLine(None, (NO_RESULT,), "", "", note)
        detail = f"{record.source} · {ago(self._clock() - record.at)}"
        notes = _notes(record)
        return StatusLine(
            record.summary.icon,
            record.summary.lines,
            f"{detail} {notes}" if notes else detail,
            f"At {record.at:%H:%M:%S}",
            note,
        )

    def running_line(self, step: Step) -> str:
        run = self._run or self._held
        if run is None or run.action.step is not step:
            return ""
        return _RUNNING_LINES[run.action.kind]

    @property
    def heatmap_view(self) -> View | None:
        """The return vias heatmap view on the board, if any."""
        owner = self._heatmap_owner
        return owner.view if owner is not None else None

    @property
    def activity(self) -> Activity | None:
        run = self._run or self._held
        if run is None:
            return None
        action = run.action
        stoppable = not run.committed and not run.stop.is_set()
        return Activity(
            self._headline(action),
            self._stage_text(run),
            run.count,
            elapsed(self._clock() - run.started),
            self._hint(run),
            stoppable,
            CANCELLING if run.stop.is_set() and not run.committed else CANCEL,
        )

    @staticmethod
    def _headline(action: _Action) -> str:
        match action.kind:
            case Kind.PLACE:
                assert action.step is not None
                return f"{action.step.label}: placing vias"
            case Kind.HEATMAP:
                assert action.step is not None
                return f"{action.step.label}: making the heatmap"
            case Kind.COUNT:
                return "Counting stitching vias…"
            case Kind.REMOVE:
                return "Removing stitching vias"
            case Kind.CLEAR:
                return "Clearing markers and heatmaps"

    @staticmethod
    def _stage_text(run: _Run) -> str:
        count = run.count
        match run.stage:
            case None:
                return ""
            case Stage.REFILL:
                return "Refilling zones"
            case Stage.READ:
                return "Reading the board"
            case Stage.PLAN:
                if count is None or run.action.step is None:
                    return "Planning vias"
                item = _PLAN_ITEMS[run.action.step]
                return f"Planning vias, {item} {count[0]} of {count[1]}"
            case Stage.DRC:
                if count is None:
                    return "Checking with DRC"
                return f"Checking with DRC, round {count[0]} of up to {count[1]}"
            case Stage.COMMIT:
                if run.action.kind is Kind.HEATMAP:
                    return f"Placing them on {OVERLAY_LAYERS}"
                return "Committing"
            case Stage.MEASURE:
                return "Measuring"
            case Stage.DRAW:
                return "Drawing the heatmaps"

    @staticmethod
    def _hint(run: _Run) -> str:
        if run.committed:
            if run.stage is Stage.REFILL:
                return "Committed. Refilling zones…"
            return TOO_LATE
        match run.action.kind:
            case Kind.PLACE:
                return "No vias are committed before the end of the run."
            case Kind.HEATMAP:
                return "No heatmap is placed before the end of the run."
            case Kind.COUNT:
                return "Nothing is removed before you confirm."
            case Kind.REMOVE | Kind.CLEAR:
                return "Nothing is removed before the end of the run."

    @property
    def outcome(self) -> Outcome | None:
        return self._outcome

    @property
    def outcome_time(self) -> str:
        at = self._outcome_at
        return "" if at is None else f"{at:%H:%M}"

    @property
    def try_again_available(self) -> bool:
        return self._retry is not None and self._connected and not self._active

    @property
    def details(self) -> str:
        """The last action's log, and its outcome's detail."""
        text = "\n".join(self._log)
        outcome = self._outcome
        if outcome is not None and outcome.detail:
            text = f"{text}\n\n{outcome.detail}" if text else outcome.detail
        return text

    @property
    def details_open(self) -> bool:
        return self._details_open

    @property
    def confirmation(self) -> Confirmation | None:
        return None if self._pending is None else self._pending[1]

    @property
    def close_asks(self) -> bool:
        """Whether closing now should first ask to cancel the run."""
        run = self._run
        return run is not None and not run.committed and not self._closing

    # Ground nets and net pickers.

    @property
    def _detected(self) -> tuple[tuple[str, ...], GroundSource]:
        if self._nets is None:
            return (), GroundSource.NONE
        return self._nets.detected

    @property
    def _grounds(self) -> tuple[str, ...]:
        choice = self._choices[Picker.GROUND]
        return choice if choice is not None else self._detected[0]

    @property
    def ground(self) -> GroundNets:
        found_by = self._detected[1]
        typed = self._choices[Picker.GROUND] is not None
        if typed:
            origin = "Your choice"
        else:
            match found_by:
                case GroundSource.INNER_ZONES:
                    origin = "Detected: zones on two or more inner layers"
                case GroundSource.FALLBACK:
                    origin = "Detected: GND (no net has zones on two inner layers)"
                case GroundSource.NONE:
                    origin = (
                        ""
                        if self._nets is None
                        else "None detected: no net has zones on two inner "
                        "layers and there is no GND net"
                    )
        listed = self._connected
        return GroundNets(
            self._candidates(Picker.GROUND) if listed else (),
            self._grounds,
            names(self._grounds) if listed else "",
            origin,
            typed,
            self._ready,
            self._gone(Picker.GROUND),
        )

    def _candidates(self, picker: Picker) -> tuple[str, ...]:
        board = self._nets
        if board is None:
            return ()
        zoned = board.zoned
        grounds = set(self._grounds)
        match picker:
            case Picker.GROUND:
                return tuple(sorted({*zoned, *self._detected[0]}))
            case Picker.PAD_VIAS | Picker.CLEAN_UP:
                return zoned
            case Picker.POWER:
                return tuple(
                    n for n in zoned if board.stitchable(n) and n not in grounds
                )
            case Picker.STITCH_GROUND:
                return tuple(n for n in zoned if board.stitchable(n) and n in grounds)

    def _gone(self, picker: Picker) -> str:
        gone = self._gone_nets[picker]
        if not gone:
            return ""
        return f"{', '.join(gone)} {'is' if len(gone) == 1 else 'are'} no longer on the board"

    def picker(self, picker: Picker) -> NetPicker:
        """The picker as the window shows it: empty without a board to
        list nets from."""
        listed = self._nets is not None and self._connected
        candidates = self._candidates(picker) if listed else ()
        chosen = self._choices[picker]
        if not listed:
            text = ""
        elif chosen is None:
            match picker:
                case Picker.POWER:
                    text = f"All power nets ({len(candidates)})"
                case Picker.STITCH_GROUND:
                    text = f"All ground nets ({len(candidates)})"
                case _:
                    text = f"All nets with pours ({len(candidates)})"
        else:
            text = names(chosen)
        return NetPicker(
            candidates,
            chosen,
            text,
            self._gone(picker),
            self._ready,
        )

    # What the window asks.

    def activate(self) -> None:
        """The window was activated: probe the board, unless an action is
        active or a probe is out."""
        if self._active or self._probing or self._closing:
            return
        self._probing = True
        runs = self._runs
        probe = self._probe

        def work() -> None:
            try:
                found = probe()
            # A worker that dies silently leaves the controller waiting.
            except BaseException as e:  # noqa: BLE001
                self._post(partial(self._probe_failed, runs, e))
                return
            self._post(partial(self._probed, runs, found))

        self._spawn(work, daemon=True)
        self._notify()

    def retry(self) -> None:
        self.activate()

    def set_refill(self, on: bool) -> None:
        if self.refill_enabled:
            self._refill = on
            self._notify()

    def set_fields(self, step: Step, fields: StepFields) -> None:
        if self.editable:
            self._fields[step] = fields
            self._notify()

    def restore_defaults(self, step: Step) -> None:
        if not self.editable:
            return
        self._fields[step] = DEFAULT_FIELDS[step]
        picker = STEP_PICKERS.get(step)
        if picker is not None:
            self._choices[picker] = None
            self._gone_nets[picker] = ()
        self._notify()

    def choose(self, picker: Picker, nets: Sequence[str] | None) -> None:
        """Choose `picker`'s nets; None or none at all is every candidate,
        and for Setup's ground nets detection."""
        if not self.editable:
            return
        self._choices[picker] = tuple(sorted(nets)) if nets else None
        self._gone_nets[picker] = ()
        self._rederive()
        self._notify()

    def use_detected(self) -> None:
        self.choose(Picker.GROUND, None)

    def set_details_open(self, is_open: bool) -> None:
        self._details_open = is_open
        self._notify()

    def place(self, step: Step) -> bool:
        if not self.step_enabled(step):
            return False
        return self._start(Kind.PLACE, step, self._job(step, None, heatmap=False))

    def heatmap(self, step: Step, view: View | None = None) -> bool:
        """Make `step`'s heatmap; return vias' in `view`, by default the
        unserved vias."""
        if step is Step.PAD_VIAS or not self.step_enabled(step):
            return False
        if step is Step.RETURN:
            view = view or View.UNSERVED
        return self._start(
            Kind.HEATMAP, step, self._job(step, view, heatmap=True), view
        )

    def remove_vias(self) -> bool:
        """Count the stitching vias Clean up's nets have, to ask before
        removing them."""
        if not self.cleanup_enabled:
            return False
        return self._start(
            Kind.COUNT, None, CountJob(self._choices[Picker.CLEAN_UP], self._refill)
        )

    def confirm_removal(self) -> bool:
        pending = self._pending
        if pending is None:
            return False
        vias, _, board = pending
        self._pending = None
        return self._start(
            Kind.REMOVE, None, RemoveJob(vias, self._refill), board=board
        )

    def decline_removal(self) -> None:
        if self._pending is not None:
            self._pending = None
            self._probe_if_idle()
            self._notify()

    def clear(self) -> bool:
        if not self.cleanup_enabled:
            return False
        return self._start(Kind.CLEAR, None, ClearJob())

    def try_again(self) -> bool:
        action = self._retry
        if action is None or not self._connected or self._active:
            return False
        return self._launch(_Run(action, self._clock()))

    def cancel(self) -> None:
        if self._held is not None:
            self._held = None
            self._notify()
            return
        run = self._run
        if run is None or run.committed or run.stop.is_set():
            return
        run.stop.set()
        self._notify()

    def close(self) -> bool:
        """Whether the window may close now. Mid-run it may not: the run
        is cancelled if it still can be, and `closable` hears when it has
        ended."""
        self._held = None
        if self._run is None:
            return True
        self._closing = True
        self.cancel()
        self._notify()
        return False

    # Starting actions.

    def _job(self, step: Step, view: View | None, *, heatmap: bool) -> Job:
        f = self._fields[step]
        via = parse_via(f.via)
        fallback = parse_via(f.fallback_via) or POURSTITCH_FALLBACK_VIA
        assert not isinstance(via, str) and not isinstance(fallback, str)
        grounds = self._choices[Picker.GROUND]
        match step:
            case Step.PAD_VIAS:
                return PadJob(
                    PadviaOptions(nets=self._choices[Picker.PAD_VIAS], via=via),
                    self._refill,
                )
            case Step.POWER | Step.GROUND:
                power = step is Step.POWER
                return StitchJob(
                    pourstitch.Options(
                        nets=self._choices[STEP_PICKERS[step]],
                        stitch_class=StitchClass.POWER if power else StitchClass.GROUND,
                        ground_nets=grounds,
                        distance=f.distance,
                        via=via,
                        fallback_via=fallback,
                    ),
                    self._refill,
                    heatmap,
                )
            case Step.RETURN:
                return ReturnJob(
                    ReturnViaOptions(
                        distance=DEFAULT_DISTANCE if f.distance is None else f.distance,
                        ground_nets=grounds,
                        via=via,
                        force=f.force,
                    ),
                    self._refill,
                    view,
                )

    def _start(
        self,
        kind: Kind,
        step: Step | None,
        job: Job,
        view: View | None = None,
        *,
        board: str | None = None,
    ) -> bool:
        assert self._board is not None
        action = _Action(kind, step, board or self._board.identity, job, view)
        run = _Run(action, self._clock())
        if self._probing:
            run.stage = Stage.READ
            self._held = run
            self._notify()
            return True
        return self._launch(run)

    def _launch(self, run: _Run) -> bool:
        action = run.action
        self._retry = None
        self._held = None
        self._runs += 1
        self._run = run
        self._log = []
        self._notify()
        post = self._post
        run_job = self._run_job

        def report(stage: Stage, count: Count | None) -> None:
            post(partial(self._staged, run, stage, count))

        progress = Progress(report, run.stop.is_set)

        def work() -> None:
            thread = threading.get_ident()
            lines: list[str] = []
            sink = logger.add(
                lambda m: lines.append(m.record["message"]),
                level="INFO",
                filter=lambda record: record["thread"].id == thread,
                format="{message}",
            )
            try:
                ending: Result | BaseException = run_job(
                    action.job, action.board, progress
                )
            # A worker that dies silently leaves the controller waiting.
            except BaseException as e:  # noqa: BLE001
                ending = e
            finally:
                logger.remove(sink)
            post(partial(self._ended, run, ending, tuple(lines)))

        self._spawn(work, daemon=False)
        return True

    # What the workers report.

    def _probed(self, runs: int, probe: Probe) -> None:
        self._probing = False
        if runs != self._runs:
            self._set_aside()
            return
        if self._board is not None and probe.board.identity != self._board.identity:
            self._switched()
        self._board = probe.board
        self._nets = probe.nets
        self._connection = Connection.CONNECTED
        self._busy = False
        self._rederive()
        held, self._held = self._held, None
        if held is not None:
            action = held.action
            if action.board == probe.board.identity:
                self._launch(held)
            else:
                error = BoardSwitchedError(action.board)
                self._show(failure(error, action.step, action.kind))
        self._notify()

    def _switched(self) -> None:
        """Forget what belonged to the board KiCad no longer shows: the
        status lines and a ground net choice."""
        self._records = {}
        self._markers_owner = None
        self._heatmap_owner = None
        self._choices[Picker.GROUND] = None
        self._gone_nets[Picker.GROUND] = ()
        self._retry = None

    def _rederive(self) -> None:
        """Keep each choice within its picker's candidates; a chosen net
        gone from the board is named in the picker's `gone`, and a choice
        left empty goes back to every candidate. Setup's ground nets go
        first, since two pickers' candidates follow them."""
        board = self._nets
        if board is None:
            return
        for picker in Picker:
            chosen = self._choices[picker]
            if chosen is None:
                continue
            candidates = set(self._candidates(picker))
            kept = tuple(n for n in chosen if n in candidates)
            gone = tuple(n for n in chosen if n not in board.names)
            if gone:
                self._gone_nets[picker] = tuple(
                    dict.fromkeys((*self._gone_nets[picker], *gone))
                )
            self._choices[picker] = kept or None

    def _set_aside(self) -> None:
        """A probe answered after an action started: read the board again
        once the panel is idle."""
        self._reprobe = True
        self._probe_if_idle()

    def _probe_if_idle(self) -> None:
        if self._reprobe and not self._active:
            self._reprobe = False
            self.activate()

    def _probe_failed(self, runs: int, error: BaseException) -> None:
        self._probing = False
        if runs != self._runs:
            self._set_aside()
            return
        held, self._held = self._held, None
        match error:
            case ApiError() if kicad_busy(error):
                self._busy = True
                dropped = BUSY
                if held is not None:
                    self._retry = held.action
            case NoBoardError():
                self._connection = Connection.NO_BOARD
                self._busy = False
                dropped = NO_BOARD
            case KiCadConnectionError():
                self._connection = Connection.NO_KICAD
                self._busy = False
                dropped = LOST
            case _:
                logger.opt(exception=error).error("The panel's probe failed")
                self._connection = Connection.NO_KICAD
                self._busy = False
                dropped = LOST
        if held is not None:
            self._show(dropped)
        self._notify()

    def _staged(self, run: _Run, stage: Stage, count: Count | None) -> None:
        if run is not self._run:
            return
        if stage is Stage.COMMIT:
            run.committed = True
        run.stage = stage
        run.count = count
        self._notify()

    def _ended(
        self, run: _Run, ending: Result | BaseException, log: tuple[str, ...]
    ) -> None:
        action = run.action
        self._run = None
        self._log = list(log)
        if isinstance(ending, BaseException):
            self._failed(action, ending)
        else:
            self._finished(action, run, ending)
        self._probe_if_idle()
        self._notify()
        if self._closing and self._run is None:
            self._closable()

    def _failed(self, action: _Action, error: BaseException) -> None:
        outcome = failure(error, action.step, action.kind)
        self._show(outcome)
        if outcome.unexpected:
            logger.opt(exception=error).error("The panel's run failed")
            self._details_open = True
        match error:
            case ApiError() if kicad_busy(error):
                self._retry = action
            case KiCadConnectionError():
                self._connection = Connection.NO_KICAD
                self._busy = False
            case NoBoardError():
                self._connection = Connection.NO_BOARD
                self._busy = False
            case BoardSwitchedError():
                self.activate()

    def _lost(self) -> None:
        self._show(LOST)
        self._connection = Connection.NO_KICAD
        self._busy = False

    def _show(self, outcome: Outcome) -> None:
        self._outcome = outcome
        self._outcome_at = self._clock()

    def _finished(self, action: _Action, run: _Run, result: Result) -> None:
        match action.kind:
            case Kind.PLACE:
                assert isinstance(result, RunResult | StitchResult | ReturnResult)
                self._placed(action, result)
            case Kind.HEATMAP:
                assert isinstance(result, StitchResult | ReturnResult)
                self._mapped(action, result)
            case Kind.COUNT:
                assert isinstance(result, RemovableVias)
                self._counted(action, run, result)
            case Kind.REMOVE | Kind.CLEAR:
                assert isinstance(result, Cleared)
                self._cleared(action, result)

    def _record(self, action: _Action, summary: Summary) -> _Record:
        assert action.step is not None
        record = _Record(
            summary, source(action.kind, action.view), self._clock(), action.view
        )
        self._records[action.step] = record
        return record

    def _placed(self, action: _Action, result: PlaceResult) -> None:
        assert action.step is not None
        placement = placement_of(result)
        if placement.commit is CommitOutcome.UNKNOWN:
            self._lost()
            return
        empty = _measured_nothing(action, result)
        summary = NOTHING_MEASURED if empty else place_summary(result)
        record = self._record(action, summary)
        # The commit takes the earlier markers off the board even when the
        # run measured nothing. Stitching runs circle no unfixable spots.
        if placement.commit is CommitOutcome.LANDED:
            circled = 0 if isinstance(result, StitchResult) else placement.unfixable
            left = bool(circled or placement.splits)
            self._replace_markers(record if left else None)
        if empty:
            self._show(nothing_measured(action.step))
        else:
            self._show(placed_outcome(action.step, result, summary))

    def _mapped(self, action: _Action, result: HeatmapResult) -> None:
        assert action.step is not None
        empty = _measured_nothing(action, result)
        summary = NOTHING_MEASURED if empty else heatmap_summary(result)
        record = self._record(action, summary)
        drawn = heatmap_drawn(result)
        # With nothing to draw the run still takes the earlier heatmap and
        # markers off the board.
        if result.overlays_placed or not drawn:
            left = isinstance(result, ReturnResult) and result.unserved > 0
            self._replace_markers(record if left else None)
            if self._heatmap_owner is not None:
                self._heatmap_owner.heatmap_since = "replaced"
            self._heatmap_owner = record if drawn else None
        if empty:
            self._show(nothing_measured(action.step))
        else:
            self._show(heatmap_outcome(action.step, result, summary))

    def _replace_markers(self, owner: _Record | None) -> None:
        """The markers on the board were replaced, by `owner`'s if it
        left any."""
        if self._markers_owner is not None:
            self._markers_owner.markers_since = "replaced"
        self._markers_owner = owner

    def _counted(self, action: _Action, run: _Run, vias: RemovableVias) -> None:
        if run.stop.is_set():
            self._show(cancelled(Kind.COUNT))
        elif not vias.total:
            self._show(NO_VIAS)
        else:
            self._pending = (vias, confirmation(vias), action.board)

    def _cleared(self, action: _Action, result: Cleared) -> None:
        if result.commit is CommitOutcome.UNKNOWN:
            self._lost()
            return
        if result.commit is CommitOutcome.LANDED:
            if self._markers_owner is not None:
                self._markers_owner.markers_since = "cleared"
            if self._heatmap_owner is not None:
                self._heatmap_owner.heatmap_since = "cleared"
            self._markers_owner = self._heatmap_owner = None
        if action.kind is Kind.CLEAR:
            self._show(cleared_outcome(result))
        elif not result.vias:
            self._show(NO_VIAS)
        else:
            self._show(removed_outcome(result))
