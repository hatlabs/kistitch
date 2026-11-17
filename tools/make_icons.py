"""Draw the KiCad plugin's icons: a game of tic-tac-toe won by vias.

Writes the toolbar icons of the plugin's action (light and dark, 24 and 48 px)
into ``pcm/plugins/`` and the 64 px package icon that KiCad's Plugin and
Content Manager lists into ``pcm/resources/icon.png``. Run it after changing
the glyph and commit the PNGs:

    .venv/bin/python tools/make_icons.py
"""

from dataclasses import dataclass
from pathlib import Path

from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.patches import Circle

PCM = Path(__file__).resolve().parent.parent / "pcm"

TOOLBAR_SIZES = (24, 48)
PACKAGE_SIZE = 64


@dataclass(frozen=True)
class Palette:
    board: str
    via: str
    cross: str


# Dark on KiCad's light toolbars, light on its dark ones, with the board
# muted so that the marks stand out.
LIGHT_THEME = Palette(board="#9a9a9a", via="#2b2b2b", cross="#2b2b2b")
DARK_THEME = Palette(board="#8a8a8a", via="#e0e0e0", cross="#e0e0e0")
# The PCM dialog can be light or dark: copper vias and grey board and
# crosses read on both.
PACKAGE = Palette(board="#b4b4b4", via="#c87533", cross="#8c8c8c")

# The glyph in a unit square, in pixels of the 24 px icon: a 3x3 board drawn
# as tracks, three vias (rings around their drill holes) winning on the
# diagonal, and two crosses.
PIXEL = 1 / 24
BOARD_INSET = PIXEL
LINE_WIDTH = 1.5 * PIXEL
VIA_RADIUS = 3 * PIXEL
DRILL_RADIUS = PIXEL
CROSS_HALF = 2.2 * PIXEL
VIAS = ((0, 0), (1, 1), (2, 2))
CROSSES = ((2, 0), (0, 1))


def cell_centre(index: int) -> float:
    return (index + 0.5) / 3


def draw(path: Path, size: int, palette: Palette) -> None:
    dpi = 100
    points = size * 72 / dpi  # points per unit of the glyph
    fig = Figure(figsize=(size / dpi, size / dpi), dpi=dpi)
    ax = fig.add_axes((0, 0, 1, 1))
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_aspect("equal")
    ax.axis("off")

    def line(xs: tuple[float, float], ys: tuple[float, float], colour: str) -> None:
        ax.add_line(
            Line2D(
                xs,
                ys,
                color=colour,
                linewidth=LINE_WIDTH * points,
                solid_capstyle="round",
            )
        )

    for edge in (1 / 3, 2 / 3):
        line((edge, edge), (BOARD_INSET, 1 - BOARD_INSET), palette.board)
        line((BOARD_INSET, 1 - BOARD_INSET), (edge, edge), palette.board)

    ring_width = VIA_RADIUS - DRILL_RADIUS
    for column, row in VIAS:
        ax.add_patch(
            Circle(
                (cell_centre(column), cell_centre(row)),
                VIA_RADIUS - ring_width / 2,
                fill=False,
                edgecolor=palette.via,
                linewidth=ring_width * points,
            )
        )

    for column, row in CROSSES:
        x, y = cell_centre(column), cell_centre(row)
        line(
            (x - CROSS_HALF, x + CROSS_HALF),
            (y - CROSS_HALF, y + CROSS_HALF),
            palette.cross,
        )
        line(
            (x - CROSS_HALF, x + CROSS_HALF),
            (y + CROSS_HALF, y - CROSS_HALF),
            palette.cross,
        )

    fig.savefig(path, dpi=dpi, transparent=True, metadata={"Software": None})


def main() -> None:
    for theme, palette in (("light", LIGHT_THEME), ("dark", DARK_THEME)):
        for size in TOOLBAR_SIZES:
            draw(PCM / "plugins" / f"icon-{theme}-{size}.png", size, palette)
    draw(PCM / "resources" / "icon.png", PACKAGE_SIZE, PACKAGE)


if __name__ == "__main__":
    main()
