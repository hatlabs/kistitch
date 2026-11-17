"""The panel's window and Qt application (`main`), one per KiCad instance:
`main` brings an open panel for the same KiCad forward instead of opening
another, and brings its own window forward at start (`instance`).

`PanelWindow` stacks, top to bottom: the banner when KiCad or a board is
missing, Setup (ground nets and automatic refills), the step cards and
Clean up in a scroll area that takes extra height, and the activity area
pinned at the bottom. It mirrors its `Controller` on every change and
keeps no state of its own beyond which parts are open: each click and
edit goes to the controller, and each refresh sets the widgets from it.
A timer refreshes every 30 s for the relative times in the status lines,
and every second while an action runs, for its elapsed time.

The board is probed when the window is activated and on Retry. Esc
cancels the running action; Return does nothing. Closing while an action
can still be cancelled asks first, a question withdrawn once the action
ends or commits; while it commits, the window waits for the push but not
the refill after it (`Controller.close`).
"""

import signal
import sys
import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from functools import partial

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QCloseEvent,
    QFont,
    QGuiApplication,
    QKeyEvent,
    QResizeEvent,
    QShowEvent,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QCheckBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from kistitch.panel.controller import (
    BUSY_BANNER,
    CANCEL,
    STEP_PICKERS,
    Controller,
    Field,
    Picker,
    RunJob,
    Spawn,
    run_job,
)
from kistitch.panel.instance import (
    Claim,
    bring_forward,
    claim,
    kicad_socket,
    kicad_token,
    serve,
    server_name,
)
from kistitch.panel.present import OVERLAY_LAYERS, Confirmation, Icon, Step, View
from kistitch.panel.probe import Probe, Prober
from kistitch.panel.widgets import (
    ACCENT_WIDTH,
    INDENT,
    ITEM_GAP,
    LINE_GAP,
    AccentBar,
    Caption,
    Disclosure,
    FocusedSpinBox,
    IconLabel,
    LinkButton,
    NetDropDown,
    StatusView,
    ViaField,
    bold,
    button_row,
    caption,
    error_colour,
    rule,
    set_text,
    wrapped,
)

WIDTH = 420
MIN_WIDTH = 380
MARGINS = (12, 10, 12, 10)
"""Left, top, right, bottom, px."""
SCREEN_SHARE = 0.9
"""The most of the screen's height the window takes by itself."""
IDLE_TICK_MS = 30_000
RUN_TICK_MS = 1_000
DETAILS_LINES = 6
DISTANCE_MAX = 100.0
DISTANCE_STEP = 0.1
CARD_GAP = 6
BLOCK_GAP = 8
CARD_SPACING = 4
"""px between a card's accent bar and its content."""
ROW_GAP = 4
"""px between the rows of a card's options, and around them."""
COLUMN_GAP = 8
"""px between a field's label and the field."""

VIA_HINT = "diameter/drill, e.g. 0.6/0.3"
DISTANCE_TEXT = {
    Step.POWER: "through the copper",
    Step.RETURN: "from a via to its return",
    Step.GROUND: "through the copper",
}
FORCE_TEXT = "Placeholder vias; DRC finds them to fix by hand"
REFILL_ON = (
    "Before Heatmap and Place, after Place and Remove vias. Each refill adds "
    "an undo step in KiCad."
)
REFILL_OFF = "Off: steps use the fills as they are. Refill in KiCad (B) after edits."
CONNECTOR = "┊ then adjust pads by hand"
VIEWS = {
    View.UNSERVED: ("&Unserved vias", "Copper around via ends without a return"),
    View.ALL_COPPER: ("&All copper", "Return distance over all copper"),
}
CLEAR_TEXT = f"Markers on User.Eco2, kistitch heatmaps on {OVERLAY_LAYERS}"
CLEAR_TIP = "Hand-drawn shapes on User.Eco2 go too."
REMOVE_TEXT = (
    "Unlocked vias on their own net's pour that no track or pad ties down, "
    "hand-placed ones included"
)
CLOSE_QUESTION = "A step is running. Cancel it and close?"


class Poster(QObject):
    """Runs callables posted from any thread on the thread it lives on,
    through a queued signal; once closed, drops them, since a worker may
    answer after the window is gone."""

    _posted = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._posted.connect(self._call, Qt.ConnectionType.QueuedConnection)
        self._lock = threading.Lock()
        self._closed = False

    def post(self, call: Callable[[], None]) -> bool:
        """Whether `call` will run: not once closed."""
        # Under the lock, so no post emits once `close` has returned.
        with self._lock:
            if self._closed:
                return False
            self._posted.emit(call)
            return True

    def close(self) -> None:
        with self._lock:
            self._closed = True

    def _call(self, call: Callable[[], None]) -> None:
        call()


def indented(widget: QWidget, left: int) -> QWidget:
    holder = QWidget()
    row = QHBoxLayout(holder)
    row.setContentsMargins(left, 0, 0, 0)
    row.addWidget(widget)
    return holder


def label_for(text: str, buddy: QWidget) -> QLabel:
    label = QLabel(text)
    label.setBuddy(buddy)
    return label


class StepCard(QWidget):
    """One step: its header with Heatmap and Place, its status line, and
    its options behind the header's disclosure."""

    opened = Signal()

    def __init__(self, step: Step, number: int, controller: Controller) -> None:
        super().__init__()
        self.step = step
        self.controller = controller
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(CARD_SPACING)
        self.bar = AccentBar()
        outer.addWidget(self.bar)
        body = QVBoxLayout()
        body.setSpacing(LINE_GAP)
        outer.addLayout(body, 1)

        header = QHBoxLayout()
        self.disclosure = Disclosure(f"{number}  {step.label}", strong=True)
        header.addWidget(self.disclosure)
        header.addStretch(1)
        self.heatmap: QPushButton | QToolButton | None = None
        self.heatmap_slot = QWidget()
        self.views: dict[View, QAction] = {}
        if step is Step.RETURN:
            self.heatmap = self._view_button()
        elif step is not Step.PAD_VIAS:
            self.heatmap = QPushButton("Heatmap")
            self.heatmap.clicked.connect(lambda: controller.heatmap(step))
        header.addWidget(self.heatmap or self.heatmap_slot)
        self.place = QPushButton("Place")
        self.place.clicked.connect(lambda: controller.place(step))
        header.addWidget(self.place)
        body.addLayout(header)

        self.status = StatusView()
        body.addWidget(indented(self.status, INDENT))
        self.options = self._options()
        self.options.setVisible(False)
        body.addWidget(self.options)
        self.disclosure.disclosed.connect(self._disclose)

    def _view_button(self) -> QToolButton:
        button = QToolButton()
        # Match the other step buttons; some platforms give tool buttons a
        # smaller font.
        button.setFont(QApplication.font("QPushButton"))
        button.setText("Heatmap")
        button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        button.clicked.connect(lambda: self.controller.heatmap(Step.RETURN))
        menu = QMenu(button)
        group = QActionGroup(menu)
        group.setExclusionPolicy(QActionGroup.ExclusionPolicy.ExclusiveOptional)
        for view, (text, description) in VIEWS.items():
            action = menu.addAction(text)
            action.setCheckable(True)
            group.addAction(action)
            action.triggered.connect(
                lambda _checked=False, v=view: self.controller.heatmap(Step.RETURN, v)
            )
            self.views[view] = action
            menu.addAction(description).setEnabled(False)
        button.setMenu(menu)
        return button

    def _options(self) -> QWidget:
        step = self.step
        box = QWidget()
        grid = QGridLayout(box)
        grid.setContentsMargins(INDENT, ROW_GAP, 0, ROW_GAP)
        grid.setHorizontalSpacing(COLUMN_GAP)
        grid.setVerticalSpacing(ROW_GAP)
        row = 0
        self.nets: NetDropDown | None = None
        self.nets_gone = caption()
        if step in STEP_PICKERS:
            self.nets = NetDropDown("All")
            picker = STEP_PICKERS[step]
            self.nets.chosen.connect(lambda nets: self.controller.choose(picker, nets))
            grid.addWidget(label_for("&Nets", self.nets), row, 0)
            grid.addWidget(self.nets, row, 1)
            grid.addWidget(self.nets_gone, row + 1, 1)
            row += 2
        self.distance: FocusedSpinBox | None = None
        self.distance_error = Caption(colour=error_colour)
        if step in DISTANCE_TEXT:
            self.distance = FocusedSpinBox()
            self.distance.setDecimals(2)
            self.distance.setRange(0.0, DISTANCE_MAX)
            self.distance.setSingleStep(DISTANCE_STEP)
            self.distance.valueChanged.connect(self._edited)
            line = QHBoxLayout()
            line.setSpacing(ITEM_GAP)
            line.addWidget(self.distance)
            line.addWidget(QLabel("mm"))
            line.addWidget(caption(DISTANCE_TEXT[step]), 1)
            grid.addWidget(label_for("&Distance", self.distance), row, 0)
            grid.addLayout(line, row, 1)
            grid.addWidget(self.distance_error, row + 1, 1)
            row += 2
        self.via = ViaField("netclass", VIA_HINT)
        self.via.edit.textEdited.connect(self._edited)
        grid.addWidget(
            label_for("&Via size", self.via.edit), row, 0, Qt.AlignmentFlag.AlignTop
        )
        grid.addWidget(self.via, row, 1)
        row += 1
        self.force: QCheckBox | None = None
        if step is Step.RETURN:
            self.force = QCheckBox("&Force a via where none fits")
            self.force.clicked.connect(self._edited)
            grid.addWidget(self.force, row, 0, 1, 2)
            grid.addWidget(indented(caption(FORCE_TEXT), INDENT), row + 1, 0, 1, 2)
            row += 2
        self.more: Disclosure | None = None
        self.fallback: ViaField | None = None
        if step in (Step.POWER, Step.GROUND):
            self.more = Disclosure("&More options")
            self.fallback = ViaField("", "")
            self.fallback.edit.textEdited.connect(self._edited)
            extra = QWidget()
            line = QGridLayout(extra)
            line.setContentsMargins(INDENT, 0, 0, 0)
            line.addWidget(
                label_for("Fallback v&ia", self.fallback.edit),
                0,
                0,
                Qt.AlignmentFlag.AlignTop,
            )
            line.addWidget(self.fallback, 0, 1)
            line.addWidget(caption("for narrow spots"), 0, 2, Qt.AlignmentFlag.AlignTop)
            line.setColumnStretch(2, 1)
            extra.setVisible(False)
            self.more_box = extra
            self.more.disclosed.connect(self._more)
            grid.addWidget(self.more, row, 0, 1, 2, Qt.AlignmentFlag.AlignLeft)
            grid.addWidget(extra, row + 1, 0, 1, 2)
            row += 2
        self.restore = QPushButton("&Restore defaults")
        self.restore.clicked.connect(lambda: self.controller.restore_defaults(step))
        grid.addLayout(button_row(self.restore), row, 0, 1, 2)
        grid.setColumnStretch(1, 1)
        return box

    def _disclose(self, shown: bool) -> None:
        self.options.setVisible(shown)
        self.opened.emit()

    def _more(self, shown: bool) -> None:
        self.more_box.setVisible(shown)
        self.opened.emit()

    def _edited(self) -> None:
        c = self.controller
        fields = c.fields(self.step)
        fields = replace(
            fields,
            via=self.via.edit.text(),
            distance=self.distance.value() if self.distance else fields.distance,
            fallback_via=self.fallback.edit.text()
            if self.fallback
            else fields.fallback_via,
            force=self.force.isChecked() if self.force else fields.force,
        )
        c.set_fields(self.step, fields)

    def refresh(self) -> None:
        c = self.controller
        step = self.step
        running = c.running_line(step)
        self.bar.light(bool(running))
        self.status.show_status(c.status(step), running)
        enabled = c.step_enabled(step)
        self.place.setEnabled(enabled)
        if self.heatmap is not None:
            self.heatmap.setEnabled(enabled)
        shown = c.heatmap_view
        for view, action in self.views.items():
            action.setChecked(view is shown)

        fields = c.fields(step)
        errors = c.field_errors(step)
        read_only = not c.editable
        if self.distance is not None and fields.distance is not None:
            if self.distance.value() != fields.distance:
                self.distance.blockSignals(True)
                self.distance.setValue(fields.distance)
                self.distance.blockSignals(False)
            self.distance.setReadOnly(read_only)
        set_text(self.distance_error, errors.get(Field.DISTANCE, ""))
        self.via.show_field(fields.via, errors.get(Field.VIA, ""), read_only)
        if self.fallback is not None:
            self.fallback.show_field(
                fields.fallback_via, errors.get(Field.FALLBACK_VIA, ""), read_only
            )
        if self.force is not None:
            self.force.setChecked(fields.force)
            self.force.setEnabled(c.editable)
        if self.nets is not None:
            picker = c.picker(STEP_PICKERS[step])
            self.nets.show_nets(picker.text, picker.candidates, picker.checked)
            self.nets.setEnabled(picker.enabled)
            set_text(self.nets_gone, picker.gone)
        self.restore.setEnabled(c.editable)


class Setup(QWidget):
    """Ground nets and automatic refills, which apply to every step."""

    def __init__(self, controller: Controller) -> None:
        super().__init__()
        self.controller = controller
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(COLUMN_GAP)
        grid.setVerticalSpacing(LINE_GAP)
        self.ground = NetDropDown("Use detected")
        self.ground.chosen.connect(lambda nets: controller.choose(Picker.GROUND, nets))
        grid.addWidget(label_for("&Ground nets", self.ground), 0, 0)
        grid.addWidget(self.ground, 0, 1)
        found = QHBoxLayout()
        found.setSpacing(ITEM_GAP)
        self.found = caption()
        self.use_detected = LinkButton("Use detected")
        self.use_detected.clicked.connect(controller.use_detected)
        found.addWidget(self.found, 1)
        found.addWidget(self.use_detected)
        grid.addLayout(found, 1, 1)
        self.gone = caption()
        grid.addWidget(self.gone, 2, 1)
        self.refill = QCheckBox("&Refill zones automatically")
        self.refill.clicked.connect(controller.set_refill)
        grid.addWidget(self.refill, 3, 0, 1, 2)
        self.refill_text = caption()
        grid.addWidget(indented(self.refill_text, INDENT), 4, 0, 1, 2)
        grid.setColumnStretch(1, 1)

    def refresh(self) -> None:
        c = self.controller
        ground = c.ground
        self.ground.show_nets(ground.text, ground.candidates, ground.chosen)
        self.ground.setEnabled(ground.enabled)
        set_text(self.found, ground.origin)
        self.use_detected.setVisible(ground.use_detected)
        self.use_detected.setEnabled(ground.enabled)
        set_text(self.gone, ground.gone)
        self.refill.setChecked(c.refill)
        self.refill.setEnabled(c.refill_enabled)
        self.refill_text.setText(REFILL_ON if c.refill else REFILL_OFF)


class CleanUp(QWidget):
    """Clear markers and heatmaps, and Remove vias, behind a disclosure."""

    opened = Signal()

    def __init__(self, controller: Controller) -> None:
        super().__init__()
        self.controller = controller
        column = QVBoxLayout(self)
        column.setContentsMargins(ACCENT_WIDTH + CARD_SPACING, 0, 0, 0)
        column.setSpacing(LINE_GAP)
        self.disclosure = Disclosure("Clean up", strong=True)
        column.addWidget(self.disclosure, 0, Qt.AlignmentFlag.AlignLeft)
        self.body = QWidget()
        grid = QGridLayout(self.body)
        grid.setContentsMargins(INDENT, ROW_GAP, 0, ROW_GAP)
        grid.setVerticalSpacing(ROW_GAP)
        self.clear = QPushButton("&Clear markers and heatmaps")
        self.clear.setToolTip(CLEAR_TIP)
        self.clear.clicked.connect(controller.clear)
        grid.addWidget(self.clear, 0, 0, 1, 2, Qt.AlignmentFlag.AlignLeft)
        grid.addWidget(caption(CLEAR_TEXT), 1, 0, 1, 2)
        grid.setRowMinimumHeight(2, BLOCK_GAP)
        self.nets = NetDropDown("All")
        self.nets.chosen.connect(lambda nets: controller.choose(Picker.CLEAN_UP, nets))
        grid.addWidget(label_for("Stitching vias &on", self.nets), 3, 0)
        grid.addWidget(self.nets, 3, 1)
        self.gone = caption()
        grid.addWidget(self.gone, 4, 1)
        self.remove = QPushButton("Remove &vias…")
        self.remove.clicked.connect(controller.remove_vias)
        grid.addLayout(button_row(self.remove), 5, 0, 1, 2)
        grid.addWidget(caption(REMOVE_TEXT), 6, 0, 1, 2)
        grid.setColumnStretch(1, 1)
        self.body.setVisible(False)
        column.addWidget(self.body)
        self.disclosure.disclosed.connect(self._disclose)

    def _disclose(self, shown: bool) -> None:
        self.body.setVisible(shown)
        self.opened.emit()

    def refresh(self) -> None:
        c = self.controller
        enabled = c.cleanup_enabled
        self.clear.setEnabled(enabled)
        self.remove.setEnabled(enabled)
        picker = c.picker(Picker.CLEAN_UP)
        self.nets.show_nets(picker.text, picker.candidates, picker.checked)
        self.nets.setEnabled(picker.enabled)
        set_text(self.gone, picker.gone)


class BannerBox(QFrame):
    """Why nothing below can run: KiCad can't be reached or has no board."""

    def __init__(self, controller: Controller) -> None:
        super().__init__()
        self.controller = controller
        self.setFrameShape(QFrame.Shape.StyledPanel)
        grid = QGridLayout(self)
        grid.setHorizontalSpacing(ITEM_GAP)
        self.icon = IconLabel()
        self.headline = bold(wrapped())
        self.body = wrapped()
        self.retry = QPushButton("Re&try")
        self.retry.clicked.connect(controller.retry)
        grid.addWidget(self.icon, 0, 0, Qt.AlignmentFlag.AlignTop)
        grid.addWidget(self.headline, 0, 1)
        grid.addWidget(self.body, 1, 1)
        grid.addLayout(button_row(self.retry), 2, 1)
        grid.setColumnStretch(1, 1)

    def refresh(self) -> None:
        banner = self.controller.banner
        self.setVisible(banner is not None)
        if banner is not None:
            self.icon.show_icon(Icon.WARNING if banner is BUSY_BANNER else Icon.ERROR)
            self.headline.setText(banner.headline)
            self.body.setText(banner.body)


class ActivityArea(QWidget):
    """What runs now, with its progress and Cancel; else the last outcome
    with Try again and Details, or what the panel waits for."""

    def __init__(self, controller: Controller) -> None:
        super().__init__()
        self.controller = controller
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(LINE_GAP)

        self.running = QWidget()
        run = QVBoxLayout(self.running)
        run.setContentsMargins(0, 0, 0, 0)
        run.setSpacing(LINE_GAP)
        top = QHBoxLayout()
        self.headline = wrapped()
        self.elapsed = QLabel()
        figures = QFont(self.elapsed.font())
        figures.setFeature(QFont.Tag("tnum"), 1)
        self.elapsed.setFont(figures)
        top.addWidget(self.headline, 1)
        top.addWidget(self.elapsed, 0, Qt.AlignmentFlag.AlignTop)
        self.stage = wrapped()
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.hint = caption()
        self.cancel = QPushButton(CANCEL)
        self.cancel.clicked.connect(controller.cancel)
        run.addLayout(top)
        run.addWidget(self.stage)
        run.addWidget(self.bar)
        run.addWidget(self.hint)
        run.addLayout(button_row(self.cancel))
        column.addWidget(self.running)

        self.busy = QWidget()
        busy = QHBoxLayout(self.busy)
        busy.setContentsMargins(0, 0, 0, 0)
        busy.setSpacing(ITEM_GAP)
        busy_icon = IconLabel()
        busy_icon.show_icon(Icon.WARNING)
        self.busy_text = wrapped()
        busy.addWidget(busy_icon, 0, Qt.AlignmentFlag.AlignTop)
        busy.addWidget(self.busy_text, 1)
        column.addWidget(self.busy)

        self.idle = wrapped()
        column.addWidget(self.idle)

        self.ended = QWidget()
        grid = QGridLayout(self.ended)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(ITEM_GAP)
        grid.setVerticalSpacing(LINE_GAP)
        self.icon = IconLabel()
        self.outcome = bold(wrapped())
        self.time = QLabel()
        self.body = wrapped()
        self.try_again = QPushButton("Try a&gain")
        self.try_again.clicked.connect(controller.try_again)
        grid.addWidget(self.icon, 0, 0, Qt.AlignmentFlag.AlignTop)
        grid.addWidget(self.outcome, 0, 1)
        grid.addWidget(self.time, 0, 2, Qt.AlignmentFlag.AlignTop)
        grid.addWidget(self.body, 1, 1, 1, 2)
        self.try_row = QWidget()
        self.try_row.setLayout(button_row(self.try_again))
        grid.addWidget(self.try_row, 2, 1, 1, 2)
        grid.setColumnStretch(1, 1)
        column.addWidget(self.ended)

        self.details = Disclosure("D&etails")
        self.details.disclosed.connect(controller.set_details_open)
        column.addWidget(self.details, 0, Qt.AlignmentFlag.AlignLeft)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFixedHeight(
            self.log.fontMetrics().lineSpacing() * DETAILS_LINES
            + 2 * self.log.frameWidth()
            + 2 * round(self.log.document().documentMargin())
        )
        self.copy = QPushButton("Cop&y")
        self.copy.clicked.connect(self._copy)
        self.log_box = QWidget()
        log = QVBoxLayout(self.log_box)
        log.setContentsMargins(0, 0, 0, 0)
        log.addWidget(self.log)
        log.addLayout(button_row(self.copy))
        column.addWidget(self.log_box)

    def _copy(self) -> None:
        QGuiApplication.clipboard().setText(self.log.toPlainText())

    def refresh(self) -> None:
        c = self.controller
        activity = c.activity
        self.running.setVisible(activity is not None)
        if activity is not None:
            self.headline.setText(activity.headline)
            self.elapsed.setText(activity.elapsed)
            set_text(self.stage, activity.stage)
            if activity.count is None:
                self.bar.setRange(0, 0)
            else:
                done, total = activity.count
                self.bar.setRange(0, total)
                self.bar.setValue(done)
            set_text(self.hint, activity.hint)
            self.cancel.setText(activity.cancel_label)
            self.cancel.setEnabled(activity.cancel_enabled)
        idle = activity is None
        self.busy.setVisible(idle and bool(c.busy_hint))
        self.busy_text.setText(c.busy_hint)
        outcome = c.outcome
        self.ended.setVisible(idle and outcome is not None)
        self.idle.setVisible(idle and outcome is None)
        self.idle.setText(c.idle_text)
        if outcome is not None:
            self.icon.show_icon(outcome.icon)
            self.outcome.setText(outcome.headline)
            self.time.setText(c.outcome_time)
            set_text(self.body, outcome.body)
        self.try_row.setVisible(c.try_again_available)
        details = c.details
        if self.log.toPlainText() != details:
            self.log.setPlainText(details)
        self.details.setVisible(idle and bool(details))
        self.details.set_open(c.details_open, notify=False)
        self.log_box.setVisible(idle and bool(details) and c.details_open)


class StepList(QScrollArea):
    """The steps and Clean up, scrolling when the window is too short for
    them; it asks for their full height."""

    def __init__(self, content: QWidget) -> None:
        super().__init__()
        self.setWidget(content)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def sizeHint(self) -> QSize:
        hint = super().sizeHint()
        content = self.widget()
        layout = content.layout() if content is not None else None
        if layout is None:
            return hint
        width = self.viewport().width()
        height = (
            layout.heightForWidth(width)
            if layout.hasHeightForWidth()
            else layout.sizeHint().height()
        )
        return QSize(hint.width(), height + 2 * self.frameWidth())

    def minimumSizeHint(self) -> QSize:
        return QSize(super().minimumSizeHint().width(), self.fontMetrics().height() * 6)


class PanelWindow(QWidget):
    def __init__(
        self,
        probe: Callable[[], Probe],
        run: RunJob = run_job,
        *,
        spawn: Spawn,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        super().__init__()
        self._sized = False
        self._fitting = False
        self._user_sized = False
        """Whether the user resized the window: from then on it only grows
        to fit what it shows."""
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setMinimumWidth(MIN_WIDTH)
        self.poster = Poster(self)
        self.controller = Controller(
            post=self.poster.post,
            spawn=spawn,
            probe=probe,
            run=run,
            clock=clock,
            notify=self.refresh,
            closable=self._closable,
        )
        self.confirm: QMessageBox | None = None
        self.close_box: QMessageBox | None = None

        c = self.controller
        self.banner = BannerBox(c)
        self.setup = Setup(c)
        self.cards = {
            step: StepCard(step, number, c) for number, step in enumerate(Step, 1)
        }
        self.cleanup = CleanUp(c)
        self.activity = ActivityArea(c)
        self._lay_out()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self.refresh()

    def _lay_out(self) -> None:
        column = QVBoxLayout(self)
        column.setContentsMargins(*MARGINS)
        column.setSpacing(BLOCK_GAP)
        column.addWidget(self.banner)
        column.addWidget(self.setup)
        column.addWidget(rule())

        steps = QWidget()
        listed = QVBoxLayout(steps)
        listed.setContentsMargins(0, 0, 0, 0)
        listed.setSpacing(CARD_GAP)
        for step, card in self.cards.items():
            listed.addWidget(card)
            card.opened.connect(self._fit)
            if step is Step.PAD_VIAS:
                connector = caption(CONNECTOR)
                listed.addWidget(
                    indented(connector, ACCENT_WIDTH + CARD_SPACING + INDENT)
                )
        listed.addWidget(rule())
        listed.addWidget(self.cleanup)
        listed.addStretch(1)
        self.cleanup.opened.connect(self._fit)
        self.activity.details.disclosed.connect(lambda _shown: self._fit())
        self.steps = StepList(steps)
        steps.installEventFilter(self)
        column.addWidget(self.steps, 1)
        column.addWidget(rule())
        column.addWidget(self.activity)

        heatmaps = [card.heatmap for card in self.cards.values() if card.heatmap]
        width = max(b.sizeHint().width() for b in heatmaps)
        for card in self.cards.values():
            (card.heatmap or card.heatmap_slot).setFixedWidth(width)
        width = max(card.place.sizeHint().width() for card in self.cards.values())
        for card in self.cards.values():
            card.place.setFixedWidth(width)
        height = max(card.place.sizeHint().height() for card in self.cards.values())
        for button in heatmaps:
            button.setFixedHeight(height)

    def refresh(self) -> None:
        c = self.controller
        self.setWindowTitle(c.title)
        self.banner.refresh()
        self.setup.refresh()
        for card in self.cards.values():
            card.refresh()
        self.cleanup.refresh()
        self.activity.refresh()
        self._confirm(c.confirmation)
        if self.close_box is not None and not c.close_asks:
            box, self.close_box = self.close_box, None
            box.done(0)
        interval = RUN_TICK_MS if c.running else IDLE_TICK_MS
        if self._timer.interval() != interval or not self._timer.isActive():
            self._timer.start(interval)

    def _content_height(self) -> int:
        """The height the window needs at its width, within the screen."""
        layout = self.layout()
        assert layout is not None
        self.steps.updateGeometry()
        layout.activate()
        height = layout.heightForWidth(self.width())
        screen = self.screen()
        if screen is not None:
            cap = int(screen.availableGeometry().height() * SCREEN_SHARE)
            height = min(height, cap)
        return height

    def _fit(self) -> None:
        """Fit the window's height to its content."""
        self._fitting = True
        self.resize(self.width(), self._content_height())
        self._fitting = False

    def _follow_content(self) -> None:
        if not self._sized:
            return
        if not self._user_sized or self._content_height() > self.height():
            self._fit()

    def event(self, event: QEvent) -> bool:
        handled = super().event(event)
        if event.type() is QEvent.Type.LayoutRequest:
            self._follow_content()
        return handled

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        # Size hints of nested labels settle through posted layout
        # requests; the step list's content has its own, after which its
        # height is known.
        if watched is self.steps.widget() and event.type() is QEvent.Type.LayoutRequest:
            QTimer.singleShot(0, self._follow_content)
        return False

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if self._sized and not self._fitting and event.spontaneous():
            self._user_sized = True

    def _confirm(self, confirmation: Confirmation | None) -> None:
        if confirmation is None:
            if self.confirm is not None:
                box, self.confirm = self.confirm, None
                box.done(0)
            return
        if self.confirm is not None:
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(confirmation.title)
        box.setText(confirmation.headline)
        box.setInformativeText(
            "\n\n".join(
                [confirmation.counts, confirmation.body, *confirmation.unfilled]
            )
        )
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        remove = box.addButton(
            confirmation.button, QMessageBox.ButtonRole.DestructiveRole
        )
        box.setDefaultButton(cancel)
        box.setEscapeButton(cancel)
        box.setWindowModality(Qt.WindowModality.WindowModal)

        def answered(button: QAbstractButton) -> None:
            if self.confirm is not box:
                return
            self.confirm = None
            if button is remove:
                self.controller.confirm_removal()
            else:
                self.controller.decline_removal()

        box.buttonClicked.connect(answered)
        box.rejected.connect(lambda: answered(cancel))
        self.confirm = box
        box.open()
        cancel.setFocus()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.controller.cancel()
            event.accept()
        else:
            super().keyPressEvent(event)

    def showEvent(self, event: QShowEvent) -> None:
        # Sized once shown, when the style's metrics apply.
        if not self._sized:
            self._sized = True
            self.resize(WIDTH, self.height())
            self._fit()
        super().showEvent(event)

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() is QEvent.Type.ActivationChange and self.isActiveWindow():
            self.controller.activate()

    def _closable(self) -> None:
        self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        c = self.controller
        if c.close_asks:
            event.ignore()
            self._ask_to_close()
            return
        if c.close():
            self._timer.stop()
            self.poster.close()
            event.accept()
        else:
            event.ignore()

    def _ask_to_close(self) -> None:
        if self.close_box is not None:
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("A step is running")
        box.setText(CLOSE_QUESTION)
        keep = box.addButton("Keep running", QMessageBox.ButtonRole.RejectRole)
        stop = box.addButton("Cancel and close", QMessageBox.ButtonRole.AcceptRole)
        box.setDefaultButton(keep)
        box.setEscapeButton(keep)
        box.setWindowModality(Qt.WindowModality.WindowModal)

        def answered(button: QAbstractButton) -> None:
            # The window closes once the run ends or pushes (`_closable`).
            if button is stop and self.controller.close_asks:
                self.controller.close()

        def finished() -> None:
            if self.close_box is box:
                self.close_box = None

        box.buttonClicked.connect(answered)
        box.finished.connect(finished)
        self.close_box = box
        box.open()


def start_thread(work: Callable[[], None], *, daemon: bool) -> None:
    threading.Thread(target=work, daemon=daemon).start()


def main() -> None:
    """Open the panel and run until it is closed, or, when a panel for the
    same KiCad is open, bring that one forward and return (`instance`)."""
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    outcome, server = claim(server_name(kicad_socket(), kicad_token()))
    if outcome == Claim.RAISED:
        return
    window = PanelWindow(Prober(), run_job, spawn=start_thread)
    serve(server, partial(bring_forward, window))
    bring_forward(window)
    window.controller.activate()
    # Qt's event loop holds Python's handler back; the default one stops
    # the panel on Ctrl+C in the terminal that started it.
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    app.exec()
    server.close()
