"""The panel's window on Qt's offscreen platform; skipped without the `gui`
extra."""

import os
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import pytest

pytest.importorskip("PySide6.QtWidgets")
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from kipy.errors import ConnectionError as KiCadConnectionError
from panel_fakes import (
    BOARD,
    Clock,
    Prober,
    Runner,
    busy,
    return_result,
    run_result,
    stitch_result,
)
from PySide6.QtCore import QCoreApplication, QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QPalette, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QMessageBox,
    QPushButton,
    QToolButton,
    QWidget,
)

from kistitch.clear_command import Cleared
from kistitch.clearvias import RemovableVias
from kistitch.commit import CommitOutcome
from kistitch.panel.controller import (
    DEFAULT_FIELDS,
    READY,
    ClearJob,
    Controller,
    Picker,
    ReturnJob,
    StitchJob,
)
from kistitch.panel.present import Icon, Step, View
from kistitch.panel.widgets import ERROR_DARK, NetDropDown
from kistitch.panel.window import (
    CLOSE_QUESTION,
    IDLE_TICK_MS,
    MIN_WIDTH,
    RUN_TICK_MS,
    WIDTH,
    PanelWindow,
    Poster,
)
from kistitch.pourstitch import StitchClass
from kistitch.progress import Stage

VIAS = RemovableVias(
    nets=None,
    counts={"GND": 240, "GND_ISO": 20, "+3V3": 40, "+5V": 12},
    unfilled_zones={},
    ids=frozenset(str(i) for i in range(312)),
)


WAIT_STEP_MS = 20
WAIT_STEPS = 100


@pytest.fixture(scope="session")
def app() -> QApplication:
    existing = QApplication.instance()
    if isinstance(existing, QApplication):
        return existing
    return QApplication([])


def settle() -> None:
    """Deliver what workers posted."""
    for _ in range(3):
        QCoreApplication.processEvents()


def wait_until(condition: Callable[[], bool]) -> None:
    """Process events until `condition` holds, as an animated click needs."""
    for _ in range(WAIT_STEPS):
        if condition():
            return
        QTest.qWait(WAIT_STEP_MS)
    assert condition()


@dataclass
class Held:
    """Runs probes at once, or holds them until `answer` if `hold_probes`,
    and holds runs until `finish`."""

    waiting: list[Callable[[], None]] = field(default_factory=list)
    hold_probes: bool = False
    probes: list[Callable[[], None]] = field(default_factory=list)

    def __call__(self, work: Callable[[], None], *, daemon: bool) -> None:
        if not daemon:
            self.waiting.append(work)
        elif self.hold_probes:
            self.probes.append(work)
        else:
            work()

    def answer(self) -> None:
        while self.probes:
            self.probes.pop(0)()
        settle()

    def finish(self) -> None:
        while self.waiting:
            self.waiting.pop(0)()
        settle()


@dataclass
class Made:
    window: PanelWindow
    runner: Runner
    clock: Clock
    held: Held


Make = Callable[..., Made]


@pytest.fixture
def make(app: QApplication) -> Iterator[Make]:
    made: list[PanelWindow] = []

    def build(
        prober: Prober | None = None,
        spawn: Callable[..., None] | None = None,
    ) -> Made:
        runner = Runner(stages=())
        clock = Clock()
        held = Held()
        w = PanelWindow(prober or Prober(), runner, spawn=spawn or held, clock=clock)
        made.append(w)
        w.show()
        w.controller.activate()
        settle()
        return Made(w, runner, clock, held)

    yield build
    for w in made:
        w.poster.close()
        w.hide()
        w.deleteLater()
    settle()


def message_box(w: PanelWindow) -> QMessageBox:
    box = w.close_box or w.confirm
    assert box is not None
    return box


def button(box: QMessageBox, text: str) -> QPushButton:
    (found,) = [b for b in box.buttons() if b.text() == text]
    assert isinstance(found, QPushButton)
    return found


def wheel(widget: QWidget) -> bool:
    """Turn the wheel a notch up over the middle of `widget`; whether the
    widget took the event."""
    centre = QPointF(widget.rect().center())
    event = QWheelEvent(
        centre,
        QPointF(widget.mapToGlobal(centre)),
        QPoint(),
        QPoint(0, 120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    widget.event(event)
    return event.isAccepted()


class TestLayout:
    def test_four_cards_in_order_with_their_actions(self, make: Make) -> None:
        w = make().window

        assert list(w.cards) == list(Step)
        titles = [card.disclosure.text() for card in w.cards.values()]
        assert titles == [
            "1  Pad vias",
            "2  Power stitching",
            "3  Return vias",
            "4  Ground stitching",
        ]
        pads = w.cards[Step.PAD_VIAS]
        assert pads.heatmap is None and pads.place.text() == "Place"
        returns = w.cards[Step.RETURN].heatmap
        assert isinstance(returns, QToolButton)
        assert returns.popupMode() is QToolButton.ToolButtonPopupMode.MenuButtonPopup
        menu = returns.menu()
        assert menu is not None
        views = [a.text() for a in menu.actions() if a.isCheckable()]
        assert views == ["&Unserved vias", "&All copper"]
        for step in (Step.POWER, Step.GROUND):
            assert isinstance(w.cards[step].heatmap, QPushButton)

    def test_shows_the_board_and_ground_nets_after_reading_it(self, make: Make) -> None:
        w = make().window

        assert w.windowTitle() == f"kistitch — {BOARD.name}"
        assert w.setup.ground.text() == "GND, GND_ISO"
        assert w.setup.found.text() == "Detected: zones on two or more inner layers"
        assert not w.banner.isVisible()
        assert w.activity.idle.text() == READY
        assert all(card.place.isEnabled() for card in w.cards.values())

    def test_has_its_default_and_least_width(self, make: Make) -> None:
        w = make().window

        assert w.width() == WIDTH
        assert w.minimumWidth() == MIN_WIDTH

    def test_cards_open_to_their_options_and_keep_their_buttons(
        self, make: Make
    ) -> None:
        card = make().window.cards[Step.POWER]
        assert not card.options.isVisible()

        card.disclosure.click()

        assert card.options.isVisible()
        assert card.place.isVisible()
        assert card.distance is not None and card.distance.value() == 10.0
        assert card.nets is not None and card.nets.text() == "All power nets (3)"


class TestOptions:
    def test_edits_reach_the_controller(self, make: Make) -> None:
        w = make().window
        power = w.cards[Step.POWER]
        returns = w.cards[Step.RETURN]
        assert power.distance is not None and power.fallback is not None
        assert returns.force is not None

        power.distance.setValue(4.5)
        power.fallback.edit.selectAll()
        QTest.keyClicks(power.fallback.edit, "0.4/0.2")
        returns.force.click()

        c = w.controller
        assert c.fields(Step.POWER).distance == 4.5
        assert c.fields(Step.POWER).fallback_via == "0.4/0.2"
        assert c.fields(Step.RETURN).force

    def test_the_wheel_steps_the_distance_only_once_focused(self, make: Make) -> None:
        w = make().window
        card = w.cards[Step.POWER]
        card.disclosure.click()
        distance = card.distance
        assert distance is not None and not distance.hasFocus()

        assert not wheel(distance)

        assert w.controller.fields(Step.POWER) == DEFAULT_FIELDS[Step.POWER]
        w.activateWindow()
        distance.setFocus()
        wait_until(distance.hasFocus)

        assert wheel(distance)

        assert w.controller.fields(Step.POWER).distance == 10.1

    def test_restore_defaults_resets_fields_and_nets(self, make: Make) -> None:
        w = make().window
        card = w.cards[Step.POWER]
        assert card.distance is not None
        card.distance.setValue(4.5)
        w.controller.choose(Picker.POWER, ["+5V"])

        card.restore.click()

        assert w.controller.fields(Step.POWER) == DEFAULT_FIELDS[Step.POWER]
        assert w.controller.picker(Picker.POWER).chosen is None


def chosen(c: Controller, picker: Picker) -> tuple[str, ...] | None:
    if picker is Picker.GROUND:
        return c.ground.chosen if c.ground.use_detected else None
    return c.picker(picker).chosen


DropDownOf = Callable[[PanelWindow], NetDropDown | None]
DROP_DOWNS: list[tuple[DropDownOf, Picker]] = [
    (lambda w: w.setup.ground, Picker.GROUND),
    (lambda w: w.cards[Step.PAD_VIAS].nets, Picker.PAD_VIAS),
    (lambda w: w.cards[Step.POWER].nets, Picker.POWER),
    (lambda w: w.cards[Step.GROUND].nets, Picker.STITCH_GROUND),
    (lambda w: w.cleanup.nets, Picker.CLEAN_UP),
]


class TestNetDropDowns:
    @pytest.mark.parametrize(("drop_down", "picker"), DROP_DOWNS)
    def test_a_checkbox_chooses_and_reset_goes_back_to_the_default(
        self, make: Make, drop_down: DropDownOf, picker: Picker
    ) -> None:
        w = make().window
        nets = drop_down(w)
        assert nets is not None
        boxes = nets.popup.findChildren(QCheckBox)
        assert boxes

        boxes[0].click()

        checked = tuple(sorted(b.text() for b in boxes if b.isChecked()))
        assert chosen(w.controller, picker) == checked
        nets.reset.click()
        assert chosen(w.controller, picker) is None

    def test_the_default_shows_every_candidate_checked(self, make: Make) -> None:
        nets = make().window.cleanup.nets
        boxes = nets.popup.findChildren(QCheckBox)

        assert [b.text() for b in boxes] == list(nets.candidates)
        assert all(b.isChecked() for b in boxes)

    def test_the_wheel_passes_over_a_drop_down(self, make: Make) -> None:
        assert not wheel(make().window.setup.ground)


class TestSetup:
    def test_the_refill_checkbox_sets_refills(self, make: Make) -> None:
        w = make().window
        assert w.controller.refill

        w.setup.refill.click()

        assert not w.controller.refill
        assert w.setup.refill_text.text().startswith("Off:")

    def test_use_detected_goes_back_to_the_detected_ground_nets(
        self, make: Make
    ) -> None:
        w = make().window
        w.controller.choose(Picker.GROUND, ["GND"])
        assert w.setup.use_detected.isVisible()

        w.setup.use_detected.click()

        assert w.setup.ground.text() == "GND, GND_ISO"
        assert not w.setup.use_detected.isVisible()


class TestPalette:
    def test_colours_follow_a_change_to_a_dark_palette(
        self, make: Make, app: QApplication
    ) -> None:
        w = make().window
        card = w.cards[Step.POWER]
        card.disclosure.click()
        QTest.keyClicks(card.via.edit, "0.3/0.6")
        assert card.distance is not None
        card.distance.setValue(0.0)
        dark = QPalette(QColor("#202020"))
        grey = QColor("#9a9a9a")
        dark.setColor(QPalette.ColorRole.PlaceholderText, grey)
        saved = app.palette()

        def text_colour(widget: QWidget) -> QColor:
            return widget.palette().color(QPalette.ColorRole.WindowText)

        try:
            app.setPalette(dark)
            settle()

            assert text_colour(w.setup.found) == grey
            assert text_colour(card.via.caption) == QColor(ERROR_DARK)
            assert ERROR_DARK.lower() in card.via.edit.styleSheet().lower()
            assert card.distance_error.isVisible()
            assert text_colour(card.distance_error) == QColor(ERROR_DARK)
        finally:
            app.setPalette(saved)
            settle()


class TestStatus:
    def test_a_result_from_a_worker_thread_shows_after_events(self, make: Make) -> None:
        threads: list[threading.Thread] = []

        def spawn(work: Callable[[], None], *, daemon: bool) -> None:
            thread = threading.Thread(target=work, daemon=daemon)
            threads.append(thread)
            thread.start()

        m = make(spawn=spawn)
        for thread in threads:
            thread.join()
        settle()
        m.runner.outcome = run_result(placed=35)
        m.window.cards[Step.PAD_VIAS].place.click()
        for thread in threads:
            thread.join()
        status = m.window.cards[Step.PAD_VIAS].status
        assert status.lines.text() == "No result yet"

        settle()

        assert status.lines.text() == "Placed 35 vias"
        assert status.icon.icon is Icon.DONE
        assert status.detail.text() == "Place · just now"
        assert status.detail.toolTip() == "At 14:02:31"

    def test_relative_times_refresh_on_the_timer(self, make: Make) -> None:
        m = make()
        m.runner.outcome = run_result(placed=35)
        m.window.cards[Step.PAD_VIAS].place.click()
        m.held.finish()
        assert m.window._timer.interval() == IDLE_TICK_MS
        m.clock.advance(minutes=6)

        m.window._timer.timeout.emit()

        detail = m.window.cards[Step.PAD_VIAS].status.detail
        assert detail.text() == "Place · 6 min ago"

    def test_running_card_shows_its_line_and_the_activity_ticks_faster(
        self, make: Make
    ) -> None:
        m = make()

        m.window.cards[Step.POWER].place.click()

        card = m.window.cards[Step.POWER]
        assert card.status.running.text() == "Placing vias…"
        assert card.bar.lit
        assert not any(c.place.isEnabled() for c in m.window.cards.values())
        assert not m.window.cleanup.clear.isEnabled()
        assert card.via.edit.isReadOnly()
        assert m.window.activity.running.isVisible()
        assert m.window.activity.cancel.text() == "Cancel (Esc)"
        assert m.window._timer.interval() == RUN_TICK_MS

    def test_an_invalid_via_size_marks_the_field_and_disables_its_step(
        self, make: Make
    ) -> None:
        w = make().window
        card = w.cards[Step.RETURN]
        card.disclosure.click()

        QTest.keyClicks(card.via.edit, "0.3/0.6")

        assert card.via.caption.text() == "Drill must be smaller than the diameter"
        assert not card.place.isEnabled()
        assert w.cards[Step.POWER].place.isEnabled()

    def test_heatmap_menu_marks_the_view_on_the_board(self, make: Make) -> None:
        m = make()
        card = m.window.cards[Step.RETURN]
        assert not any(a.isChecked() for a in card.views.values())
        m.runner.outcome = return_result(unserved=14, full_map=True)

        card.views[View.ALL_COPPER].trigger()
        m.held.finish()

        assert card.views[View.ALL_COPPER].isChecked()
        assert not card.views[View.UNSERVED].isChecked()
        assert card.status.detail.text() == "Heatmap: all copper · just now"

    def test_heatmap_buttons_make_their_steps_heatmaps(self, make: Make) -> None:
        m = make()
        m.runner.outcome = stitch_result((0.0,))
        for step, kind in (
            (Step.POWER, StitchClass.POWER),
            (Step.GROUND, StitchClass.GROUND),
        ):
            heatmap = m.window.cards[step].heatmap
            assert heatmap is not None

            heatmap.click()
            m.held.finish()

            job = m.runner.jobs[-1]
            assert isinstance(job, StitchJob)
            assert job.heatmap and job.options.stitch_class is kind
        m.runner.outcome = return_result(unserved=14)
        heatmap = m.window.cards[Step.RETURN].heatmap
        assert heatmap is not None

        heatmap.click()
        m.held.finish()

        job = m.runner.jobs[-1]
        assert isinstance(job, ReturnJob) and job.view is View.UNSERVED


class TestKeys:
    def test_esc_while_running_cancels(self, make: Make) -> None:
        m = make()
        m.window.cards[Step.POWER].place.click()

        QTest.keyClick(m.window, Qt.Key.Key_Escape)

        assert m.window.activity.cancel.text() == "Cancelling…"
        assert not m.window.activity.cancel.isEnabled()

    def test_activation_reads_the_board(self, make: Make) -> None:
        prober = Prober()
        w = make(prober).window
        other = QWidget()
        other.show()
        other.activateWindow()
        wait_until(lambda: not w.isActiveWindow())
        before = prober.calls

        w.activateWindow()

        wait_until(lambda: prober.calls > before)
        other.close()

    def test_place_during_a_probe_starts_once_the_board_is_read(
        self, make: Make
    ) -> None:
        m = make()
        m.held.hold_probes = True
        m.window.controller.activate()
        assert m.held.probes
        card = m.window.cards[Step.PAD_VIAS]
        m.runner.outcome = run_result(placed=35)

        card.place.click()

        assert card.status.running.text() == "Placing vias…"
        assert m.window.activity.stage.text() == "Reading the board"
        assert m.window.activity.cancel.isEnabled()
        assert not m.held.waiting
        m.held.answer()
        assert m.held.waiting
        m.held.finish()
        assert card.status.lines.text() == "Placed 35 vias"

    def test_return_triggers_no_action(self, make: Make) -> None:
        m = make()
        place = m.window.cards[Step.PAD_VIAS].place
        place.setFocus()

        QTest.keyClick(place, Qt.Key.Key_Return)
        QTest.keyClick(m.window, Qt.Key.Key_Enter)

        assert not m.window.controller.running
        assert not m.held.waiting


class TestClear:
    def test_clear_clears(self, make: Make) -> None:
        m = make()
        m.runner.outcome = Cleared(vias=0, commit=CommitOutcome.NOTHING)
        m.window.cleanup.disclosure.click()

        m.window.cleanup.clear.click()
        m.held.finish()

        assert isinstance(m.runner.jobs[-1], ClearJob)


class TestRemoveVias:
    def test_asks_with_cancel_as_default_and_the_count_on_the_button(
        self, make: Make
    ) -> None:
        m = make()
        m.runner.outcome = VIAS
        m.window.cleanup.disclosure.click()

        m.window.cleanup.remove.click()
        m.held.finish()

        box = message_box(m.window)
        cancel = box.defaultButton()
        assert cancel is not None and cancel.text() == "Cancel"
        assert box.focusWidget() is cancel
        assert box.escapeButton() is cancel
        assert button(box, "Remove 312 vias")
        assert box.text() == "312 vias will be removed"
        assert "GND 240 · GND_ISO 20 · +3V3 40 · +5V 12" in box.informativeText()

    def test_esc_cancels(self, make: Make) -> None:
        m = make()
        m.runner.outcome = VIAS
        m.window.cleanup.remove.click()
        m.held.finish()
        box = message_box(m.window)

        QTest.keyClick(box, Qt.Key.Key_Escape)
        wait_until(lambda: m.window.confirm is None)

        assert m.window.controller.confirmation is None
        assert not m.held.waiting

    def test_the_named_button_removes(self, make: Make) -> None:
        m = make()
        m.runner.outcome = VIAS
        m.window.cleanup.remove.click()
        m.held.finish()

        button(message_box(m.window), "Remove 312 vias").click()

        assert m.window.controller.running
        assert m.window.activity.headline.text() == "Removing stitching vias"


class TestClose:
    def test_keep_running_leaves_the_window_open(self, make: Make) -> None:
        m = make()
        m.window.cards[Step.POWER].place.click()

        m.window.close()
        box = message_box(m.window)
        assert box.text() == CLOSE_QUESTION
        keep = box.defaultButton()
        assert keep is not None and keep.text() == "Keep running"
        keep.click()
        settle()

        assert m.window.isVisible()
        assert m.window.controller.running
        assert m.window.activity.cancel.isEnabled()

    def test_cancel_and_close_stops_the_run_and_closes(self, make: Make) -> None:
        m = make()
        m.runner.stages = ((Stage.READ, None),)
        m.window.cards[Step.POWER].place.click()
        m.window.close()

        button(message_box(m.window), "Cancel and close").click()
        assert m.window.isVisible()
        m.held.finish()

        assert not m.window.isVisible()
        assert not m.window.controller.running

    def test_while_committing_waits_for_the_push_not_the_refill(
        self, make: Make
    ) -> None:
        m = make()
        m.runner.outcome = stitch_result((0.0,), placement=run_result(placed=27))
        m.runner.stages = ((Stage.COMMIT, None), (Stage.REFILL, None))
        seen: list[tuple[Stage, bool]] = []

        def during(stage: Stage) -> None:
            settle()
            if stage is Stage.COMMIT:
                m.window.close()
                settle()
            seen.append((stage, m.window.isVisible()))

        m.runner.during = during
        m.window.cards[Step.POWER].place.click()
        m.held.finish()

        assert m.window.close_box is None
        assert seen == [(Stage.COMMIT, True), (Stage.REFILL, False)]

    def test_closing_while_idle_closes(self, make: Make) -> None:
        m = make()

        assert m.window.close()
        assert not m.window.isVisible()
        calls: list[int] = []
        m.window.poster.post(lambda: calls.append(1))
        settle()
        assert calls == []

    @pytest.mark.parametrize("dismiss", [QMessageBox.close, QMessageBox.reject])
    def test_dismissing_the_question_lets_closing_ask_again(
        self, make: Make, dismiss: Callable[[QMessageBox], object]
    ) -> None:
        m = make()
        m.window.cards[Step.POWER].place.click()
        m.window.close()
        box = message_box(m.window)

        dismiss(box)
        settle()

        assert m.window.close_box is None
        m.window.close()
        assert m.window.close_box is not None
        assert m.window.close_box is not box

    def test_the_question_goes_when_the_run_ends(self, make: Make) -> None:
        m = make()
        m.runner.outcome = run_result(placed=35)
        m.window.cards[Step.PAD_VIAS].place.click()
        m.window.close()
        box = message_box(m.window)

        m.held.finish()

        assert m.window.close_box is None
        assert not box.isVisible()
        assert m.window.isVisible()
        assert m.window.activity.ended.isVisible()

    def test_the_question_goes_once_the_run_commits(self, make: Make) -> None:
        m = make()
        m.runner.outcome = stitch_result((0.0,), placement=run_result(placed=27))
        m.runner.stages = (
            (Stage.READ, None),
            (Stage.COMMIT, None),
            (Stage.REFILL, None),
        )
        asked: list[QMessageBox] = []
        open_at_commit: list[QMessageBox | None] = []

        def during(stage: Stage) -> None:
            settle()
            if stage is Stage.READ:
                m.window.close()
                asked.append(message_box(m.window))
            elif stage is Stage.COMMIT:
                open_at_commit.append(m.window.close_box)
                # Answered too late: the run can no longer be cancelled.
                button(asked[0], "Cancel and close").click()
                settle()

        m.runner.during = during
        m.window.cards[Step.POWER].place.click()
        m.held.finish()

        assert open_at_commit == [None]
        assert m.window.isVisible()
        assert m.window.activity.ended.isVisible()


class TestConnection:
    def test_without_kicad_the_banner_asks_to_retry(self, make: Make) -> None:
        prober = Prober(KiCadConnectionError("no KiCad"))
        m = make(prober)
        w = m.window

        assert w.banner.isVisible()
        assert w.banner.headline.text() == "Cannot reach KiCad"
        assert w.windowTitle() == "kistitch"
        assert not any(c.place.isEnabled() for c in w.cards.values())
        assert not w.setup.ground.isEnabled()
        assert w.setup.refill.isEnabled()
        assert w.activity.idle.text() == "Not connected"

        prober.answer = Prober().answer
        w.banner.retry.click()
        settle()

        assert not w.banner.isVisible()
        assert w.cards[Step.PAD_VIAS].place.isEnabled()

    def test_busy_before_any_read_shows_the_busy_banner(self, make: Make) -> None:
        w = make(Prober(busy())).window

        assert w.banner.isVisible()
        assert w.banner.headline.text() == "KiCad is busy"
        assert w.banner.icon.icon is Icon.WARNING
        assert w.banner.retry.isVisible()
        assert not any(c.place.isEnabled() for c in w.cards.values())

    def test_busy_offers_try_again(self, make: Make) -> None:
        m = make()
        m.runner.outcome = busy()
        m.window.cards[Step.RETURN].place.click()
        m.held.finish()
        activity = m.window.activity
        assert activity.outcome.text() == "KiCad is busy — nothing committed"
        assert activity.try_row.isVisible()
        m.runner.outcome = return_result(placement=run_result(placed=61))

        activity.try_again.click()
        m.held.finish()

        assert m.window.cards[Step.RETURN].status.lines.text() == (
            "Placed 61 vias; every via served"
        )
        assert not activity.try_row.isVisible()


class TestDetails:
    def test_details_keep_their_state_and_copy_the_log(self, make: Make) -> None:
        m = make()
        m.runner.log = ("returnvia: 61 vias planned",)
        m.runner.outcome = return_result(placement=run_result(placed=61))
        m.window.cards[Step.RETURN].place.click()
        m.held.finish()
        activity = m.window.activity
        assert activity.details.isVisible()
        assert not activity.log_box.isVisible()

        activity.details.click()
        m.window.cards[Step.RETURN].place.click()
        m.held.finish()

        assert activity.log_box.isVisible()
        activity.copy.click()
        assert QGuiApplication.clipboard().text() == "returnvia: 61 vias planned"


class TestPoster:
    def test_drops_calls_after_close(self, app: QApplication) -> None:
        poster = Poster()
        calls: list[int] = []
        poster.post(lambda: calls.append(1))
        settle()
        poster.close()

        poster.post(lambda: calls.append(2))
        settle()

        assert calls == [1]
