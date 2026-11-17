"""Draw the KiCad plugin's icons: stitching vias in a copper pour.

Writes the toolbar icons of the plugin's action (light and dark, 24 and 48 px)
into ``pcm/plugins/`` and the 64 px package icon that KiCad's Plugin and
Content Manager lists into ``pcm/resources/icon.png``. Run it after changing
the glyph and commit the PNGs:

    .venv/bin/python scripts/make_icons.py
"""

from pathlib import Path

from matplotlib.figure import Figure
from matplotlib.patches import Circle, FancyBboxPatch

PCM = Path(__file__).resolve().parent.parent / "pcm"

# Dark on KiCad's light toolbars, light on its dark ones.
LIGHT_THEME = "#2b2b2b"
DARK_THEME = "#e0e0e0"
# Copper reads on both the light and the dark PCM dialog.
COPPER = "#c87533"

TOOLBAR_SIZES = (24, 48)
PACKAGE_SIZE = 64

# The glyph in a unit square: a rounded pour, faint so that the vias stand
# out, with a grid of vias drawn as rings around their drill holes. Sizes
# are whole pixels of the 24 px icon, so that the rings stay sharp there.
PIXEL = 1 / 24
POUR_INSET = PIXEL
POUR_CORNER = 3 * PIXEL
POUR_ALPHA = 0.35
VIA_GRID = 3
VIA_PITCH = 7 * PIXEL
VIA_RADIUS = 3 * PIXEL
DRILL_RADIUS = PIXEL


def draw(path: Path, size: int, colour: str) -> None:
    dpi = 100
    fig = Figure(figsize=(size / dpi, size / dpi), dpi=dpi)
    ax = fig.add_axes((0, 0, 1, 1))
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_aspect("equal")
    ax.axis("off")
    side = 1 - 2 * POUR_INSET
    ax.add_patch(
        FancyBboxPatch(
            (POUR_INSET + POUR_CORNER, POUR_INSET + POUR_CORNER),
            side - 2 * POUR_CORNER,
            side - 2 * POUR_CORNER,
            boxstyle=f"round,pad={POUR_CORNER}",
            facecolor=colour,
            edgecolor="none",
            alpha=POUR_ALPHA,
        )
    )
    first = 0.5 - VIA_PITCH * (VIA_GRID - 1) / 2
    for row in range(VIA_GRID):
        for column in range(VIA_GRID):
            centre = (first + column * VIA_PITCH, first + row * VIA_PITCH)
            ring_width = VIA_RADIUS - DRILL_RADIUS
            ax.add_patch(
                Circle(
                    centre,
                    VIA_RADIUS - ring_width / 2,
                    fill=False,
                    edgecolor=colour,
                    linewidth=ring_width * size * 72 / dpi,
                )
            )
    fig.savefig(path, dpi=dpi, transparent=True, metadata={"Software": None})


def main() -> None:
    for theme, colour in (("light", LIGHT_THEME), ("dark", DARK_THEME)):
        for size in TOOLBAR_SIZES:
            draw(PCM / "plugins" / f"icon-{theme}-{size}.png", size, colour)
    draw(PCM / "resources" / "icon.png", PACKAGE_SIZE, COPPER)


if __name__ == "__main__":
    main()
