"""Draw the documentation figures.

Writes ``docs/images/return-path.svg``: a cross-section of a 4-layer board
showing a signal that changes layers through a via, and its return current
crossing between the two ground planes through a nearby ground via. Run it
after changing the drawing and commit the SVG:

    .venv/bin/python scripts/make_figures.py
"""

from itertools import pairwise
from pathlib import Path

import matplotlib as mpl
from matplotlib.axes import Axes
from matplotlib.figure import Figure
from matplotlib.patches import FancyArrowPatch, Rectangle

IMAGES = Path(__file__).resolve().parent.parent / "docs" / "images"

# An SVG shown through <img> follows the viewer's OS colour scheme, not the
# page's, so one palette has to read on both white and GitHub's dark grey.
# The board is opaque so that colours drawn on it do not depend on the page.
DIELECTRIC = "#56653f"
COPPER = "#d9893b"
SIGNAL = "#3b8eea"
RETURN = "#f0508c"
PART = "#8b949e"
TEXT = "#8b949e"
TEXT_ON_BOARD = "#e6edf3"
# Copper that sits beside the cut rather than in it.
BEHIND_ALPHA = 0.4

# Layer stack in drawing units, bottom up. The prepreg and core keep roughly
# the proportions of a typical 1.6 mm board; the copper is exaggerated so it
# shows at all.
COPPER_THICKNESS = 0.09
PREPREG = 0.34
CORE = 1.45
L4_BOTTOM = 0.0
L4_TOP = L4_BOTTOM + COPPER_THICKNESS
L3_BOTTOM = L4_TOP + PREPREG
L3_TOP = L3_BOTTOM + COPPER_THICKNESS
L2_BOTTOM = L3_TOP + CORE
L2_TOP = L2_BOTTOM + COPPER_THICKNESS
L1_BOTTOM = L2_TOP + PREPREG
L1_TOP = L1_BOTTOM + COPPER_THICKNESS
CORE_MIDDLE = (L3_TOP + L2_BOTTOM) / 2

BOARD_LEFT = 0.0
BOARD_RIGHT = 10.0

# The cut follows the signal, so a through ground via beside the trace can
# only be shown overlapping it: it is drawn faded where the L1 trace passes
# in front. On L3 the return current passes the signal via's antipad beside
# the cut, which the dashed run stands for.
DRIVER_LEFT = 0.6
LOAD_RIGHT = 9.4
PART_WIDTH = 1.2
PART_HEIGHT = 0.42
PIN_INSET = 0.4  # how far a trace reaches under its part
SIGNAL_VIA_X = 5.6
GROUND_VIA_X = 4.4
VIA_OUTER = 0.34
VIA_WALL = 0.07
PAD_WIDTH = 0.62
ANTIPAD_WIDTH = 0.95
# Abutting patches leave an antialiased hairline; overlapping ones do not.
OVERLAP = 0.05

SIGNAL_OFFSET = 0.2  # from the trace, outside the board
RETURN_OFFSET = 0.1  # from the plane surface, inside the prepreg
PART_CLEARANCE = 0.25  # between a part and the end of a current path
ANTIPAD_CLEARANCE = 0.12  # between an antipad edge and the dashed run
ARROW_WIDTH = 2.2
ARROW_HEAD = "-|>,head_length=0.6,head_width=0.3"
ARROW_SCALE = 20
ARROW_HALF_LENGTH = 0.25
MIN_RUN_FOR_HEAD = 4 * ARROW_HALF_LENGTH
DASHES = (0, (2, 2))
# Matplotlib draws patches at z-order 1 and lines at 2. The dashed run sits
# above the board but below the via walls, so it reads as passing behind.
BEHIND_ZORDER = 1.5
WALL_ZORDER = 2.5

FONT_SIZE = 11
LABEL_X = BOARD_RIGHT + 0.25
LABEL_CLEARANCE = 0.3
LEGEND_X = DRIVER_LEFT
LEGEND_LINE = 0.7
LEGEND_ROWS = (-0.45, -0.85)
FIGURE_SIZE = (9.0, 4.2)
X_LIMITS = (-0.3, 12.2)
Y_LIMITS = (-1.15, L1_TOP + 1.05)


def box(
    ax: Axes,
    left: float,
    right: float,
    bottom: float,
    top: float,
    colour: str,
    alpha: float = 1.0,
    zorder: float = 1,
) -> None:
    ax.add_patch(
        Rectangle(
            (left, bottom),
            right - left,
            top - bottom,
            color=colour,
            alpha=alpha,
            lw=0,
            zorder=zorder,
        )
    )


def dielectric(ax: Axes) -> None:
    """The board between L4 and L1, open where the vias are in the cut."""
    ground_left = GROUND_VIA_X - VIA_OUTER / 2
    ground_right = GROUND_VIA_X + VIA_OUTER / 2
    signal_left = SIGNAL_VIA_X - VIA_OUTER / 2
    signal_right = SIGNAL_VIA_X + VIA_OUTER / 2
    for left, right in (
        (BOARD_LEFT, ground_left),
        (ground_right, signal_left),
        (signal_right, BOARD_RIGHT),
    ):
        box(ax, left, right, L4_TOP, L1_BOTTOM, DIELECTRIC)
    # Above L2 the ground via is beside the cut, behind the dielectric in it.
    box(
        ax,
        ground_left - OVERLAP,
        ground_right + OVERLAP,
        L2_TOP,
        L1_BOTTOM,
        DIELECTRIC,
    )


def plane(ax: Axes, bottom: float) -> None:
    """A ground plane joined to the ground via, cleared around the signal via."""
    top = bottom + COPPER_THICKNESS
    ground_left = GROUND_VIA_X - VIA_OUTER / 2
    ground_right = GROUND_VIA_X + VIA_OUTER / 2
    box(ax, BOARD_LEFT, ground_left, bottom, top, COPPER)
    box(ax, ground_right, SIGNAL_VIA_X - ANTIPAD_WIDTH / 2, bottom, top, COPPER)
    box(ax, SIGNAL_VIA_X + ANTIPAD_WIDTH / 2, BOARD_RIGHT, bottom, top, COPPER)


def via(ax: Axes, x: float, bottom: float, top: float, alpha: float = 1.0) -> None:
    """A plated hole in section: two copper walls around an empty drill."""
    left = x - VIA_OUTER / 2
    right = x + VIA_OUTER / 2
    box(ax, left, left + VIA_WALL, bottom, top, COPPER, alpha, WALL_ZORDER)
    box(ax, right - VIA_WALL, right, bottom, top, COPPER, alpha, WALL_ZORDER)


def pad(ax: Axes, x: float, bottom: float, alpha: float = 1.0) -> None:
    left = x - PAD_WIDTH / 2
    right = x + PAD_WIDTH / 2
    box(ax, left, right, bottom, bottom + COPPER_THICKNESS, COPPER, alpha)


def arrowhead(
    ax: Axes, tail: tuple[float, float], tip: tuple[float, float], colour: str
) -> None:
    ax.add_patch(
        FancyArrowPatch(
            tail,
            tip,
            arrowstyle=ARROW_HEAD,
            mutation_scale=ARROW_SCALE,
            color=colour,
            lw=0,
        )
    )


def current(ax: Axes, points: list[tuple[float, float]], colour: str) -> None:
    """A path with an arrowhead midway along each run long enough for one."""
    xs, ys = zip(*points)
    ax.plot(xs, ys, color=colour, lw=ARROW_WIDTH, solid_joinstyle="miter")
    for (x0, y0), (x1, y1) in pairwise(points):
        length = abs(x1 - x0) + abs(y1 - y0)
        if length < MIN_RUN_FOR_HEAD:
            continue
        mid_x, mid_y = (x0 + x1) / 2, (y0 + y1) / 2
        dx = (x1 - x0) / length * ARROW_HALF_LENGTH
        dy = (y1 - y0) / length * ARROW_HALF_LENGTH
        arrowhead(ax, (mid_x - dx, mid_y - dy), (mid_x + dx, mid_y + dy), colour)


def text(
    ax: Axes,
    x: float,
    y: float,
    label: str,
    colour: str = TEXT,
    align: str = "left",
    italic: bool = False,
) -> None:
    ax.text(
        x,
        y,
        label,
        color=colour,
        fontsize=FONT_SIZE,
        ha=align,
        va="center",
        style="italic" if italic else "normal",
    )


def draw_return_path(path: Path) -> None:
    fig = Figure(figsize=FIGURE_SIZE)
    ax = fig.add_axes((0, 0, 1, 1))
    ax.set_xlim(*X_LIMITS)
    ax.set_ylim(*Y_LIMITS)
    ax.axis("off")

    driver_right = DRIVER_LEFT + PART_WIDTH
    load_left = LOAD_RIGHT - PART_WIDTH

    dielectric(ax)
    plane(ax, L2_BOTTOM)
    plane(ax, L3_BOTTOM)
    via(ax, GROUND_VIA_X, L4_BOTTOM, L2_TOP)
    pad(ax, GROUND_VIA_X, L4_BOTTOM)
    via(ax, GROUND_VIA_X, L2_TOP, L1_TOP, BEHIND_ALPHA)
    pad(ax, GROUND_VIA_X, L1_BOTTOM, BEHIND_ALPHA)
    box(ax, driver_right - PIN_INSET, SIGNAL_VIA_X, L1_BOTTOM, L1_TOP, COPPER)
    box(ax, SIGNAL_VIA_X, load_left + PIN_INSET, L4_BOTTOM, L4_TOP, COPPER)
    pad(ax, SIGNAL_VIA_X, L1_BOTTOM)
    pad(ax, SIGNAL_VIA_X, L4_BOTTOM)
    via(ax, SIGNAL_VIA_X, L4_BOTTOM, L1_TOP)
    box(ax, DRIVER_LEFT, driver_right, L1_TOP, L1_TOP + PART_HEIGHT, PART)
    box(ax, load_left, LOAD_RIGHT, L4_BOTTOM - PART_HEIGHT, L4_BOTTOM, PART)

    above_l1 = L1_TOP + SIGNAL_OFFSET
    below_l4 = L4_BOTTOM - SIGNAL_OFFSET
    current(
        ax,
        [
            (driver_right + PART_CLEARANCE, above_l1),
            (SIGNAL_VIA_X, above_l1),
            (SIGNAL_VIA_X, below_l4),
            (load_left - PART_CLEARANCE, below_l4),
        ],
        SIGNAL,
    )

    above_l2 = L2_TOP + RETURN_OFFSET
    below_l3 = L3_BOTTOM - RETURN_OFFSET
    beside_left = SIGNAL_VIA_X - ANTIPAD_WIDTH / 2 - ANTIPAD_CLEARANCE
    beside_right = SIGNAL_VIA_X + ANTIPAD_WIDTH / 2 + ANTIPAD_CLEARANCE
    current(
        ax,
        [(load_left - PART_CLEARANCE, below_l3), (beside_right, below_l3)],
        RETURN,
    )
    ax.plot(
        (beside_right, beside_left),
        (below_l3, below_l3),
        color=RETURN,
        lw=ARROW_WIDTH,
        linestyle=DASHES,
        zorder=BEHIND_ZORDER,
    )
    current(
        ax,
        [
            (beside_left, below_l3),
            (GROUND_VIA_X, below_l3),
            (GROUND_VIA_X, above_l2),
            (driver_right + PART_CLEARANCE, above_l2),
        ],
        RETURN,
    )

    text(
        ax,
        DRIVER_LEFT + PART_WIDTH / 2,
        L1_TOP + PART_HEIGHT + LABEL_CLEARANCE,
        "driver",
        align="center",
    )
    text(
        ax,
        LOAD_RIGHT - PART_WIDTH / 2,
        L4_BOTTOM - PART_HEIGHT - LABEL_CLEARANCE,
        "load",
        align="center",
    )
    for label, bottom in (
        ("L1", L1_BOTTOM),
        ("L2 GND", L2_BOTTOM),
        ("L3 GND", L3_BOTTOM),
        ("L4", L4_BOTTOM),
    ):
        text(ax, LABEL_X, bottom + COPPER_THICKNESS / 2, label)
    text(ax, LABEL_X, CORE_MIDDLE, "core", italic=True)
    text(
        ax,
        GROUND_VIA_X - VIA_OUTER,
        CORE_MIDDLE,
        "ground via\n(beside the trace)",
        TEXT_ON_BOARD,
        align="right",
    )
    text(
        ax,
        SIGNAL_VIA_X + VIA_OUTER,
        CORE_MIDDLE,
        "signal\nvia",
        TEXT_ON_BOARD,
    )

    for y, colour, label in zip(
        LEGEND_ROWS, (SIGNAL, RETURN), ("signal current", "return current")
    ):
        line_end = LEGEND_X + LEGEND_LINE
        ax.plot((LEGEND_X, line_end), (y, y), color=colour, lw=ARROW_WIDTH)
        arrowhead(ax, (line_end - 2 * ARROW_HALF_LENGTH, y), (line_end, y), colour)
        text(ax, line_end + LABEL_CLEARANCE, y, label, colour)

    fig.savefig(path, transparent=True, metadata={"Date": None})


def main() -> None:
    # Keep text as text, and make the output the same on every run.
    mpl.rcParams["svg.fonttype"] = "none"
    mpl.rcParams["svg.hashsalt"] = "kistitch"
    draw_return_path(IMAGES / "return-path.svg")


if __name__ == "__main__":
    main()
