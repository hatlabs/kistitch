"""The panel window's building blocks, drawn from the controller's state.

Colours come from the palette: the secondary colour of captions and line 2
of a status line is the palette's placeholder text, the running card's
accent bar its highlight. The only colour of the panel's own is the error
red of an invalid field (`error_colour`), picked for a light or a dark
palette, and the only style sheet is that field's border. Each colour is
picked again when the palette changes, as when the system turns dark.
"""

from collections.abc import Callable, Sequence

from PySide6.QtCore import QEvent, QPoint, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPalette, QWheelEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QSizePolicy,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from kistitch.panel.controller import StatusLine
from kistitch.panel.present import Icon

ERROR_LIGHT = "#C4161C"
ERROR_DARK = "#FF6B62"
DARK = 0.5
"""A palette whose window is darker than this lightness is dark."""
ICON_SIZE = 16
MIN_PICKER_CHARS = 8
ACCENT_WIDTH = 3
INDENT = 20
"""px from a card's edge to its status lines and options, past the arrow."""
LINE_GAP = 2
"""px between the lines of one block of text."""
ITEM_GAP = 6
"""px between items side by side, such as an icon and its text."""
POPUP_MARGINS = (8, 6, 8, 6)
"""A net drop-down's popup: left, top, right, bottom, px."""
PALETTE_CHANGES = (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange)
ICONS = {
    Icon.DONE: QStyle.StandardPixmap.SP_DialogApplyButton,
    Icon.WARNING: QStyle.StandardPixmap.SP_MessageBoxWarning,
    Icon.INFO: QStyle.StandardPixmap.SP_MessageBoxInformation,
    Icon.ERROR: QStyle.StandardPixmap.SP_MessageBoxCritical,
}


def is_dark(palette: QPalette) -> bool:
    return palette.color(QPalette.ColorRole.Window).lightnessF() < DARK


def error_colour(palette: QPalette) -> QColor:
    return QColor(ERROR_DARK if is_dark(palette) else ERROR_LIGHT)


def secondary(palette: QPalette) -> QColor:
    return palette.color(QPalette.ColorRole.PlaceholderText)


class Caption(QLabel):
    """A wrapped label in a colour picked from its palette, by default the
    secondary colour."""

    def __init__(
        self, text: str = "", colour: Callable[[QPalette], QColor] = secondary
    ) -> None:
        super().__init__(text)
        self.setWordWrap(True)
        self.set_colour(colour)

    def set_colour(self, colour: Callable[[QPalette], QColor]) -> None:
        self.colour = colour
        self._tint()

    def _tint(self) -> None:
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.WindowText, self.colour(palette))
        self.setPalette(palette)

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in PALETTE_CHANGES:
            self._tint()


def caption(text: str = "") -> Caption:
    return Caption(text)


def wrapped(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    return label


def bold(label: QLabel) -> QLabel:
    font = QFont(label.font())
    font.setBold(True)
    label.setFont(font)
    return label


def rule() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFrameShadow(QFrame.Shadow.Sunken)
    return line


def set_text(label: QLabel, text: str) -> None:
    """Show `text`, hiding the label when it is empty."""
    label.setText(text)
    label.setVisible(bool(text))


class IconLabel(QLabel):
    """A standard icon, or an empty space as wide when there is none."""

    def __init__(self) -> None:
        super().__init__()
        self.setFixedSize(ICON_SIZE, ICON_SIZE)
        self.icon: Icon | None = None

    def show_icon(self, icon: Icon | None) -> None:
        self.icon = icon
        if icon is None:
            self.clear()
            return
        pixmap = (
            self.style()
            .standardIcon(ICONS[icon])
            .pixmap(QSize(ICON_SIZE, ICON_SIZE), self.devicePixelRatioF())
        )
        self.setPixmap(pixmap)


class Disclosure(QToolButton):
    """An arrow and a title that show or hide a part of the panel;
    `disclosed` hears each click's new state."""

    disclosed = Signal(bool)

    def __init__(self, text: str, *, strong: bool = False) -> None:
        super().__init__()
        self.setText(text)
        self.setAutoRaise(True)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.setArrowType(Qt.ArrowType.RightArrow)
        self.is_open = False
        self.clicked.connect(lambda: self.set_open(not self.is_open))
        if strong:
            font = QFont(self.font())
            font.setBold(True)
            self.setFont(font)

    def set_open(self, shown: bool, *, notify: bool = True) -> None:
        if shown == self.is_open:
            return
        self.is_open = shown
        self.setArrowType(Qt.ArrowType.DownArrow if shown else Qt.ArrowType.RightArrow)
        if notify:
            self.disclosed.emit(shown)


class NetDropDown(QComboBox):
    """A drop-down whose popup lists the candidate nets with checkboxes,
    and a button that goes back to the default; the closed drop-down
    summarises the choice. `chosen` hears the checked nets after each
    click, or None for the default. The wheel scrolls past it, since it
    has no items to step through."""

    chosen = Signal(object)

    def __init__(self, reset: str) -> None:
        super().__init__()
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.setMinimumContentsLength(MIN_PICKER_CHARS)
        self.addItem("")
        self.popup = QMenu(self)
        self._list = QWidget()
        self._boxes: dict[str, QCheckBox] = {}
        self._rows = QVBoxLayout()
        self._rows.setContentsMargins(0, 0, 0, 0)
        self._rows.setSpacing(LINE_GAP)
        body = QWidget()
        column = QVBoxLayout(body)
        column.setContentsMargins(*POPUP_MARGINS)
        self._list.setLayout(self._rows)
        column.addWidget(self._list)
        column.addWidget(rule())
        self.reset = QPushButton(reset)
        self.reset.clicked.connect(self._reset)
        column.addWidget(self.reset, 0, Qt.AlignmentFlag.AlignLeft)
        action = QWidgetAction(self.popup)
        action.setDefaultWidget(body)
        self.popup.addAction(action)

    def showPopup(self) -> None:
        self.popup.setMinimumWidth(self.width())
        self.popup.popup(self.mapToGlobal(QPoint(0, self.height())))

    def hidePopup(self) -> None:
        self.popup.close()
        super().hidePopup()

    def wheelEvent(self, event: QWheelEvent) -> None:
        event.ignore()

    def text(self) -> str:
        return self.itemText(0)

    def setText(self, text: str) -> None:
        self.setItemText(0, text)

    @property
    def candidates(self) -> tuple[str, ...]:
        return tuple(self._boxes)

    def show_nets(
        self, text: str, candidates: Sequence[str], checked: Sequence[str]
    ) -> None:
        self.setText(text)
        if tuple(candidates) != self.candidates:
            for box in self._boxes.values():
                self._rows.removeWidget(box)
                box.deleteLater()
            self._boxes = {}
            for net in candidates:
                box = QCheckBox(net)
                box.clicked.connect(self._clicked)
                self._rows.addWidget(box)
                self._boxes[net] = box
        on = set(checked)
        for net, box in self._boxes.items():
            box.setChecked(net in on)

    def _clicked(self) -> None:
        nets = [net for net, box in self._boxes.items() if box.isChecked()]
        self.chosen.emit(nets or None)

    def _reset(self) -> None:
        self.popup.close()
        self.chosen.emit(None)


class StatusView(QWidget):
    """A step's status line: what is running on it, line 1 with its icon,
    line 2 in the secondary colour with the clock time as its tooltip, and
    why the step can't run as is."""

    def __init__(self) -> None:
        super().__init__()
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(ITEM_GAP)
        grid.setVerticalSpacing(LINE_GAP)
        self.running = wrapped()
        self.icon = IconLabel()
        self.lines = wrapped()
        self.detail = caption()
        self.note = wrapped()
        grid.addWidget(self.running, 0, 0, 1, 2)
        grid.addWidget(self.icon, 1, 0, Qt.AlignmentFlag.AlignTop)
        grid.addWidget(self.lines, 1, 1)
        grid.addWidget(self.detail, 2, 1)
        grid.addWidget(self.note, 3, 1)
        grid.setColumnStretch(1, 1)

    def show_status(self, status: StatusLine, running: str) -> None:
        set_text(self.running, running)
        self.icon.show_icon(status.icon)
        self.lines.setText("\n".join(status.lines))
        set_text(self.detail, status.detail)
        self.detail.setToolTip(status.tooltip)
        set_text(self.note, status.note)


class FocusedSpinBox(QDoubleSpinBox):
    """A spin box the wheel steps only once it has focus; otherwise the
    wheel scrolls what holds it."""

    def __init__(self) -> None:
        super().__init__()
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, event: QWheelEvent) -> None:
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class ViaField(QWidget):
    """A via size edit with its caption, which an error replaces in red."""

    def __init__(self, placeholder: str, hint: str) -> None:
        super().__init__()
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(LINE_GAP)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(placeholder)
        self.edit.setMaximumWidth(self.edit.fontMetrics().horizontalAdvance("0" * 16))
        self.hint = hint
        self.caption = caption(hint)
        column.addWidget(self.edit)
        column.addWidget(self.caption)
        self.error = ""

    def show_field(self, text: str, error: str, read_only: bool) -> None:
        if self.edit.text() != text:
            self.edit.setText(text)
        self.edit.setReadOnly(read_only)
        if error == self.error:
            return
        self.error = error
        self._show_error()

    def _show_error(self) -> None:
        if self.error:
            red = error_colour(self.palette()).name()
            self.edit.setStyleSheet(f"QLineEdit {{ border: 1px solid {red}; }}")
            self.caption.setText(self.error)
            self.caption.set_colour(error_colour)
        else:
            self.edit.setStyleSheet("")
            self.caption.setText(self.hint)
            self.caption.set_colour(secondary)
        self.caption.setVisible(bool(self.caption.text()))

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in PALETTE_CHANGES:
            self._show_error()


class AccentBar(QFrame):
    """The bar on a card's left edge, in the highlight colour while the
    card's step runs."""

    def __init__(self) -> None:
        super().__init__()
        self.setFixedWidth(ACCENT_WIDTH)
        self.setAutoFillBackground(True)
        self.lit = False
        self.light(False)

    def light(self, on: bool) -> None:
        self.lit = on
        palette = self.palette()
        colour = (
            palette.color(QPalette.ColorRole.Highlight)
            if on
            else QColor(Qt.GlobalColor.transparent)
        )
        palette.setColor(QPalette.ColorRole.Window, colour)
        self.setPalette(palette)

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in PALETTE_CHANGES:
            self.light(self.lit)


class LinkButton(QPushButton):
    """A flat button in the link colour."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.setFlat(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._tint()

    def _tint(self) -> None:
        palette = self.palette()
        palette.setColor(
            QPalette.ColorRole.ButtonText, palette.color(QPalette.ColorRole.Link)
        )
        self.setPalette(palette)

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in PALETTE_CHANGES:
            self._tint()


def button_row(*buttons: QWidget, left: int = 0) -> QHBoxLayout:
    """Buttons aligned right, after `left` px of indent."""
    row = QHBoxLayout()
    row.setContentsMargins(left, 0, 0, 0)
    row.addStretch(1)
    for button in buttons:
        row.addWidget(button)
    return row
