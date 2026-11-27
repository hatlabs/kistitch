import argparse
from itertools import pairwise
from pathlib import Path

import numpy as np
import pytest
from kipy.board_types import BoardSegment
from kipy.errors import ApiError
from kipy.geometry import Vector2
from kipy.proto.board.board_types_pb2 import BoardLayer as BL
from kipy.proto.common.envelope_pb2 import ApiStatusCode
from PIL import Image
from shapely.geometry import box

from kistitch import cli, overlay, pourstitch_command, render, returnvia_command
from kistitch.commit import CreateItemsError, Marker, MarkerKind, MarkerLayerError
from kistitch.plan import Plan, PlannedUnit, UnfixableSpot
from kistitch.raster import CoverageMap, Grid
from kistitch.snapshot import NM_PER_MM, Snapshot, Via, ViaSize

D = 2.5
STYLE = pourstitch_command.STYLE
STYLE_CMAP = render.plt.get_cmap(STYLE.colormap)


def rgb(colour):
    return tuple(round(c * 255) for c in render.matplotlib.colors.to_rgb(colour))


def coverage(net, layer, dist, excluded=None):
    if excluded is None:
        excluded = np.zeros(dist.shape, bool)
    return CoverageMap(net, layer, dist, excluded)


def test_placement_maps_pixel_zero_to_raster_origin():
    origin, size, pixel = (12.3, -4.5), (200, 80), 0.1

    p = overlay.image_placement(origin, size, pixel, ppi=300)

    native = overlay.MM_PER_INCH / 300
    drawn_pixel = native * p.scale
    assert drawn_pixel == pytest.approx(pixel)
    top_left = (
        p.centre[0] - size[0] * drawn_pixel / 2,
        p.centre[1] - size[1] * drawn_pixel / 2,
    )
    assert top_left == pytest.approx(origin)


def test_overlay_ppi_needs_no_scaling():
    assert overlay.image_placement((0, 0), (1, 1), 0.1).scale == pytest.approx(1.0)


def test_layer_map_follows_stack_order():
    copper = ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")

    assert overlay.layer_map(copper, overlay.DEFAULT_OVERLAY_LAYERS) == {
        "F.Cu": "User.1",
        "In1.Cu": "User.2",
        "In2.Cu": "User.3",
        "B.Cu": "User.4",
    }
    with pytest.raises(ValueError):
        overlay.layer_map(copper, ("User.1",))


def grid_of(maps: list[CoverageMap]) -> Grid:
    height, width = maps[0].distance.shape
    return Grid(0, 0, width, height)


def maps_for(layer_nets: dict[str, list[tuple[str, np.ndarray]]]) -> list[CoverageMap]:
    return [
        coverage(net, layer, dist)
        for layer, entries in layer_nets.items()
        for net, dist in entries
    ]


def test_overlay_board_area_has_raster_size_and_transparent_background(tmp_path):
    a = np.full((30, 50), np.nan)
    a[:, :20] = 1.0
    b = np.full((30, 50), np.nan)
    b[:, 30:] = np.inf
    maps = maps_for({"F.Cu": [("GND", a), ("+3.3V", b)]})

    paths = render.write_overlays(tmp_path, grid_of(maps), maps, {}, D, STYLE)

    img = Image.open(paths["F.Cu"])
    assert img.info["dpi"] == pytest.approx((overlay.OVERLAY_PPI,) * 2, abs=0.01)
    rgba = np.asarray(img.convert("RGBA"))[:30, :50]
    assert (rgba[:, 20:30, 3] == 0).all()
    assert (rgba[:, :20, 3] == 255).all()
    assert (rgba[:, 30:, 3] == 255).all()
    assert tuple(rgba[0, 40, :3]) == (0xFF, 0x2A, 0x6D)


@pytest.mark.parametrize("style", [STYLE, returnvia_command.STYLE])
def test_overlay_keeps_the_board_pixels_and_adds_a_legend_below(tmp_path, style):
    dist = ramp(50, height=30)
    dist[:, 40:] = np.nan
    maps = [coverage("GND", "F.Cu", dist)]
    grid = grid_of(maps)

    path = render.write_overlays(tmp_path, grid, maps, {}, D, style)["F.Cu"]

    rgba = np.asarray(Image.open(path).convert("RGBA"))
    board = render.overlay_rgba(grid, maps, {}, D, style)
    assert (rgba[: grid.height, : grid.width] == board).all()
    assert (rgba[: grid.height, grid.width :, 3] == 0).all()
    strip = rgba[grid.height :]
    assert strip.shape[0] > 0
    white = (strip[..., :3] == 255).all(axis=2)
    assert white.any()
    assert (strip[..., 3] == 255).any()


def test_legend_text_is_about_the_legend_height(tmp_path: Path):
    maps = [coverage("GND", "F.Cu", np.zeros((10, 10)))]
    grid = grid_of(maps)

    path = render.write_overlays(tmp_path, grid, maps, {}, D, STYLE)["F.Cu"]

    strip = np.asarray(Image.open(path).convert("RGBA"))[grid.height :]
    # Rows of the label text: the lowest run of rows with light pixels.
    light = (strip[..., :3].min(axis=2) > LIGHT_TEXT_MIN).any(axis=1)
    rows = np.nonzero(light)[0]
    runs = np.split(rows, np.nonzero(np.diff(rows) > 1)[0] + 1)
    label = runs[-1]
    text_mm = len(label) * grid.pixel
    assert text_mm == pytest.approx(render.LEGEND_TEXT_MM, rel=LEGEND_TEXT_TOLERANCE)
    # The whole label is in the strip, inside its padding.
    columns = np.nonzero(
        (strip[label, :, :3].min(axis=2) > LIGHT_TEXT_MIN).any(axis=0)
    )[0]
    padding_px = render.LEGEND_PAD_MM / grid.pixel
    assert columns.max() < strip.shape[1] - padding_px / 2


def test_excluded_copper_is_grey_not_pink(tmp_path: Path):
    dist = np.full((10, 10), np.inf)
    excluded = np.zeros(dist.shape, bool)
    excluded[:, 5:] = True
    maps = [coverage("GND", "F.Cu", dist, excluded)]

    rgba = np.asarray(
        Image.open(
            render.write_overlays(tmp_path, grid_of(maps), maps, {}, D, STYLE)["F.Cu"]
        )
    )

    assert tuple(rgba[0, 0, :3]) == (0xFF, 0x2A, 0x6D)
    grey = render.matplotlib.colors.to_rgb(render.EXCLUDED_COLOUR)
    assert tuple(rgba[0, 9, :3]) == tuple(round(c * 255) for c in grey)
    assert rgba[0, 9, 3] == 255


def test_layers_differing_only_in_excluded_copper_get_own_panels():
    a = np.array([[np.inf, np.inf]])
    maps = [
        coverage("GND", "F.Cu", a),
        coverage("GND", "B.Cu", a.copy(), np.array([[False, True]])),
    ]

    assert len(render.panels(maps)) == 2


def test_one_overlay_per_copper_layer(tmp_path: Path):
    a = np.zeros((4, 4))
    maps = maps_for({"In1.Cu": [("GND", a)], "In2.Cu": [("GND", a)]})

    assert set(render.write_overlays(tmp_path, grid_of(maps), maps, {}, D, STYLE)) == {
        "In1.Cu",
        "In2.Cu",
    }


def test_identical_layers_share_a_panel():
    a = np.array([[1.0, np.nan]])
    maps = [
        coverage("GND", "In1.Cu", a),
        coverage("GND", "In2.Cu", a.copy()),
        coverage("GND", "F.Cu", np.array([[2.0, np.nan]])),
    ]

    shown = render.panels(maps)

    assert [(m.layer, twins) for m, twins in shown] == [
        ("In1.Cu", ["In2.Cu"]),
        ("F.Cu", []),
    ]


def test_render_panels_draws_markers_where_they_are(tmp_path: Path):
    snap = Snapshot(
        copper_layers=("F.Cu", "B.Cu"),
        vias=[Via("v", "GND", (0.6, 0.6), 0.6, 0.3)],
        edge_paths=[list(box(0, 0, 3, 2).exterior.coords)],  # type: ignore[list-item]
    )
    grid = Grid(0, 0, 30, 20)
    dist = np.zeros((20, 30))
    excluded = np.zeros(dist.shape, bool)
    excluded[:, 25:] = True
    maps = [coverage("GND", "F.Cu", dist, excluded)]
    plan = Plan(
        units=(PlannedUnit((1.5, 1.0), "GND", ViaSize(0.6, 0.3)),),
        unfixable=(UnfixableSpot((2.0, 1.4), "GND", "no fit", "F.Cu"),),
    )
    out = tmp_path / "heat.png"

    render.render_panels(
        out, snap, grid, maps, pourstitch_command.markers(snap, plan, maps), D, STYLE
    )

    rgb_px = np.asarray(Image.open(out).convert("RGB")).astype(int)

    def near(colour):
        return np.abs(rgb_px - rgb(colour)).sum(axis=2) < 30

    # The zero-distance copper is one solid colour; the rows and columns it
    # dominates span the board from x 0 to the excluded strip at 2.5 mm
    # and from y 0 to 2 mm. The colourbar's low end shares the colour but
    # fills too little of any row to count.
    solid = near(render.matplotlib.colors.to_hex(STYLE_CMAP(0.0)))
    row_counts = solid.sum(axis=1)
    rows = np.nonzero(row_counts > row_counts.max() / 2)[0]
    cols = np.nonzero(solid[rows].sum(axis=0) > len(rows) / 2)[0]
    px_per_mm = (rows.max() + 1 - rows.min()) / 2.0
    assert (cols.max() + 1 - cols.min()) / px_per_mm == pytest.approx(2.5, abs=0.02)

    def hit(colour, point, reach=0):
        r = round(rows.min() + point[1] * px_per_mm)
        c = round(cols.min() + point[0] * px_per_mm)
        return near(colour)[r - reach : r + reach + 1, c - reach : c + reach + 1].any()

    # A ring is a hairline and a cross's strokes are a few pixels long, so
    # allow for rounding of where they land.
    assert hit(render.MARKER_COLOUR, (0.9, 0.6), reach=1)
    assert not hit(render.MARKER_COLOUR, (0.6, 0.6))
    assert hit(render.MARKER_COLOUR, (1.5, 1.0))
    assert hit(render.UNFIXABLE_COLOUR, (2.0, 1.4), reach=2)
    assert not hit(render.UNFIXABLE_COLOUR, (1.5, 1.4), reach=2)
    assert hit(render.EXCLUDED_COLOUR, (2.75, 1.0))


def test_render_panels_paints_unreached_copper_pink(tmp_path: Path):
    # Copper no stitch reaches has an infinite distance; imshow would draw it
    # as background unless it is mapped above the colour scale.
    snap = Snapshot(
        copper_layers=("F.Cu", "B.Cu"),
        edge_paths=[list(box(0, 0, 3, 2).exterior.coords)],  # type: ignore[list-item]
    )
    grid = Grid(0, 0, 30, 20)
    maps = [coverage("GND", "F.Cu", np.full((20, 30), np.inf))]
    out = tmp_path / "heat.png"

    render.render_panels(out, snap, grid, maps, {}, D, STYLE)

    pixels = np.asarray(Image.open(out).convert("RGB")).reshape(-1, 3)
    pink = tuple(int(pourstitch_command.OVER_COLOUR[i : i + 2], 16) for i in (1, 3, 5))
    assert (np.abs(pixels.astype(int) - pink).sum(axis=1) < 10).sum() > 5000


OTHER_STYLE = render.HeatmapStyle(
    title="Other",
    colourbar_label="mm; cap {distance:g}",
    png_prefix="other",
    colormap="magma",
    over_colour=None,
    unreachable_colour="#00ff00",
)


def test_style_sets_overlay_prefix_and_colours(tmp_path: Path):
    dist = np.array([[0.0, 2 * D, np.inf, np.nan]])
    maps = [coverage("GND", "In1.Cu", dist)]

    paths = render.write_overlays(tmp_path, grid_of(maps), maps, {}, D, OTHER_STYLE)

    assert paths["In1.Cu"].name == "other-In1_Cu.png"
    rgba = np.asarray(Image.open(paths["In1.Cu"]).convert("RGBA"))
    magma = render.plt.get_cmap("magma")
    assert tuple(rgba[0, 0, :3]) == tuple(magma(0.0, bytes=True)[:3])
    # Without an over colour, values above the scale saturate at its top.
    assert tuple(rgba[0, 1, :3]) == tuple(magma(1.0, bytes=True)[:3])
    assert tuple(rgba[0, 2, :3]) == rgb("#00ff00")
    assert rgba[0, 3, 3] == 0


def test_render_panels_paints_unreached_copper_in_the_style_colour(tmp_path: Path):
    snap = Snapshot(
        copper_layers=("F.Cu", "B.Cu"),
        edge_paths=[list(box(0, 0, 3, 2).exterior.coords)],  # type: ignore[list-item]
    )
    grid = Grid(0, 0, 30, 20)
    maps = [coverage("GND", "F.Cu", np.full((20, 30), np.inf))]
    out = tmp_path / "heat.png"

    render.render_panels(out, snap, grid, maps, {}, D, OTHER_STYLE)

    pixels = np.asarray(Image.open(out).convert("RGB")).reshape(-1, 3)
    green = rgb("#00ff00")
    assert (np.abs(pixels.astype(int) - green).sum(axis=1) < 10).sum() > 5000


def test_overlay_draws_rings_only(tmp_path: Path):
    dist = np.full((40, 40), np.nan)
    dist[:, :20] = 0.0
    maps = [coverage("GND", "F.Cu", dist)]
    grid = grid_of(maps)
    marks = render.Markers(
        rings=(((2.0, 2.0), 1.0),),
        dots=(((1.0, 3.5), 0.3),),
        crosses=(("F.Cu", (3.0, 0.5)),),
    )

    paths = render.write_overlays(
        tmp_path, grid, maps, {("GND", "F.Cu"): marks}, D, STYLE
    )

    rgba = np.asarray(Image.open(paths["F.Cu"]).convert("RGBA"))
    white = (255, 255, 255, 255)
    on_copper, off_copper = grid.index((1.0, 2.0)), grid.index((3.0, 2.0))
    assert tuple(rgba[on_copper]) == white
    assert tuple(rgba[off_copper]) == white
    assert tuple(rgba[grid.index((1.5, 2.0))]) != white
    assert rgba[(*grid.index((2.5, 2.0)), 3)] == 0
    assert tuple(rgba[grid.index((1.0, 3.5))]) != white
    assert rgba[(*grid.index((3.0, 0.5)), 3)] == 0


# Antialiased white text: well above the legend's dark background.
LIGHT_TEXT_MIN = 128
LEGEND_TEXT_TOLERANCE = 0.3
SCALE = 3.0
SATURATING_STYLE = render.HeatmapStyle(
    title="Saturating",
    colourbar_label="mm",
    png_prefix="saturating",
    colormap="cividis",
    over_colour=None,
    unreachable_colour="#00ff00",
    scale=SCALE,
)


def ramp(width: int, height: int = 20, pixel: float = 0.1) -> np.ndarray:
    """Distance growing by one pixel per column from 0 at the left edge."""
    return np.tile(np.arange(width) * pixel, (height, 1))


def test_overlay_saturates_beyond_the_scale(tmp_path: Path):
    dist = ramp(100)
    maps = [coverage("GND", "In1.Cu", dist)]

    paths = render.write_overlays(
        tmp_path, grid_of(maps), maps, {}, D, SATURATING_STYLE
    )

    rgba = np.asarray(Image.open(paths["In1.Cu"]).convert("RGBA"))
    cividis = render.plt.get_cmap("cividis")
    assert tuple(rgba[10, 0, :3]) == tuple(cividis(0.0, bytes=True)[:3])
    top = tuple(cividis(1.0, bytes=True)[:3])
    beyond = np.nonzero(dist[0] > SCALE * D)[0]
    assert beyond.size and all(tuple(rgba[10, c, :3]) == top for c in beyond)
    assert tuple(rgba[10, beyond[0] - 2, :3]) != top


def test_overlay_colours_follow_the_scale(tmp_path: Path):
    dist = ramp(40)
    maps = [coverage("GND", "F.Cu", dist)]

    rgba = render.overlay_rgba(grid_of(maps), maps, {}, D, STYLE)

    norm = render.matplotlib.colors.Normalize(vmin=0, vmax=D)
    expected = STYLE_CMAP(norm(np.minimum(dist, D)), bytes=True)
    assert (rgba[dist <= D] == expected[dist <= D]).all()


def test_render_panels_draw_the_saturated_colour(tmp_path: Path):
    width_mm, height_mm = 10.0, 2.0
    snap = Snapshot(
        copper_layers=("In1.Cu",),
        edge_paths=[list(box(0, 0, width_mm, height_mm).exterior.coords)],  # type: ignore[list-item]
    )
    grid = Grid(0, 0, 100, 20)
    small_d = 1.0
    maps = [coverage("GND", "In1.Cu", ramp(100))]
    out = tmp_path / "heat.png"

    render.render_panels(out, snap, grid, maps, {}, small_d, SATURATING_STYLE)

    rgb_px = np.asarray(Image.open(out).convert("RGB")).astype(int)

    def near(colour, tol):
        return np.abs(rgb_px - rgb(colour)).sum(axis=2) < tol

    # The panel's copper runs from the scale's low end at x 0 to its
    # saturated top at x 10 mm; the middle row of the panel crosses both.
    cividis = render.plt.get_cmap("cividis")
    low = near(render.matplotlib.colors.to_hex(cividis(0.0)), 30)
    top = near(render.matplotlib.colors.to_hex(cividis(1.0)), 5)
    counts = top.sum(axis=1)
    middle = round(np.nonzero(counts > counts.max() / 2)[0].mean())
    x0 = np.nonzero(low[middle])[0].min()
    x1 = np.nonzero(top[middle])[0].max() + 1
    px_per_mm = (x1 - x0) / width_mm

    def column(x_mm):
        return round(x0 + x_mm * px_per_mm)

    assert top[middle, column(SCALE * small_d + 0.5)]
    assert top[middle, column(width_mm - 0.5)]
    assert not top[middle, column(SCALE * small_d - 0.5)]


@pytest.mark.parametrize(
    "style, top, extend",
    [
        (STYLE, 0.0, "max"),
        (SATURATING_STYLE, SCALE * D, "neither"),
        (SATURATING_STYLE, SCALE * D + 0.1, "max"),
    ],
)
def test_colourbar_extends_only_past_values_the_scale_leaves_out(style, top, extend):
    dist = np.array([[0.0, top, np.inf, np.nan]])

    assert render.colourbar_extend([coverage("GND", "F.Cu", dist)], D, style) == extend


def test_ring_centred_off_the_grid_draws_its_arc_on_the_grid(tmp_path: Path):
    dist = np.zeros((40, 40))
    maps = [coverage("GND", "F.Cu", dist)]
    grid = grid_of(maps)
    marks = render.Markers(rings=(((-0.5, 2.0), 1.0),))

    rgba = render.overlay_rgba(grid, maps, {("GND", "F.Cu"): marks}, D, STYLE)

    white = (255, 255, 255, 255)
    assert tuple(rgba[grid.index((0.45, 2.0))]) == white
    assert tuple(rgba[grid.index((0.05, 2.0))]) != white
    assert not (rgba[:, 10:] == white).all(axis=2).any()


def test_folded_panel_draws_shared_circles_once_and_every_layers_crosses():
    a = np.array([[1.0, np.nan]])
    front, back = coverage("GND", "F.Cu", a), coverage("GND", "B.Cu", a.copy())
    via = ((1.0, 1.0), 0.3)
    crosses = (("F.Cu", (2.0, 2.0)), ("B.Cu", (3.0, 3.0)))
    markers = {
        ("GND", "F.Cu"): render.Markers(rings=(via,), crosses=crosses),
        ("GND", "B.Cu"): render.Markers(rings=(via,), crosses=crosses),
    }

    ((panel, twins),) = render.panels([front, back])
    marks = render.panel_markers(markers, panel, twins)

    assert marks.rings == (via,)
    assert marks.crosses == crosses


def test_unfolded_panel_draws_only_its_layers_crosses():
    front = coverage("GND", "F.Cu", np.array([[1.0]]))
    back = coverage("GND", "B.Cu", np.array([[2.0]]))
    crosses = (("F.Cu", (2.0, 2.0)), ("B.Cu", (3.0, 3.0)))
    markers = {(m.net, m.layer): render.Markers(crosses=crosses) for m in (front, back)}

    shown = render.panels([front, back])

    assert [render.panel_markers(markers, m, t).crosses for m, t in shown] == [
        (crosses[0],),
        (crosses[1],),
    ]


def test_folded_panel_draws_crosses_in_plan_order():
    # Overlapping crosses composite their anti-aliased edges in drawing order,
    # so the panel follows the plan, which alternates between twin layers.
    snap = Snapshot(copper_layers=("F.Cu", "B.Cu"))
    spots = [(5.0, 5.0), (5.3, 5.0)]
    plan = Plan(
        unfixable=tuple(
            UnfixableSpot(p, "GND", "no fit", layer)
            for p in spots
            for layer in ("F.Cu", "B.Cu")
        )
    )
    a = np.zeros((1, 1))
    maps = [coverage("GND", "F.Cu", a), coverage("GND", "B.Cu", a.copy())]

    ((panel, twins),) = render.panels(maps)
    marks = render.panel_markers(
        pourstitch_command.markers(snap, plan, maps), panel, twins
    )

    assert marks.crosses == (
        ("F.Cu", spots[0]),
        ("B.Cu", spots[0]),
        ("F.Cu", spots[1]),
        ("B.Cu", spots[1]),
    )


def test_pourstitch_markers_follow_net():
    snap = Snapshot(
        copper_layers=("F.Cu", "B.Cu"),
        vias=[
            Via("g", "GND", (1.0, 1.0), 0.8, 0.3),
            Via("s", "SIG", (2.0, 2.0), 0.8, 0.3),
        ],
    )
    plan = Plan(
        units=(
            PlannedUnit((3.0, 3.0), "GND", ViaSize(1.0, 0.4)),
            PlannedUnit((4.0, 4.0), "VCC", ViaSize(1.0, 0.4)),
        ),
        unfixable=(
            UnfixableSpot((5.0, 5.0), "GND", "no fit", "F.Cu"),
            UnfixableSpot((6.0, 6.0), "GND", "no fit", "B.Cu"),
            UnfixableSpot((7.0, 7.0), "SIG", "no fit", "F.Cu"),
        ),
    )
    a = np.zeros((1, 1))
    maps = [coverage("GND", "F.Cu", a), coverage("GND", "B.Cu", a)]

    marks = pourstitch_command.markers(snap, plan, maps)

    front = marks[("GND", "F.Cu")]
    assert front.rings == (((1.0, 1.0), 0.4),)
    assert front.dots == (((3.0, 3.0), 0.5),)
    assert front.crosses == (("F.Cu", (5.0, 5.0)), ("B.Cu", (6.0, 6.0)))


COPPER_4 = (BL.BL_F_Cu, BL.BL_In1_Cu, BL.BL_In2_Cu, BL.BL_B_Cu)


class LayersBoard:
    """Board with an enabled-layer set that `set_enabled_layers` replaces."""

    def __init__(self, copper=COPPER_4, enabled=(), refuse=()):
        self.copper = list(copper)
        self.enabled = [*self.copper, *enabled]
        self.refuse = set(refuse)
        self.set_calls: list = []

    def get_copper_layer_count(self):
        return len(self.copper)

    def get_enabled_layers(self):
        return list(self.enabled)

    def set_enabled_layers(self, copper_layer_count, layers):
        self.set_calls.append((copper_layer_count, list(layers)))
        self.enabled = [*self.copper, *(L for L in layers if L not in self.refuse)]
        return list(self.enabled)


class ImageBoard(LayersBoard):
    def __init__(self, existing=(), enabled=(BL.BL_Eco2_User,)):
        super().__init__(enabled=enabled)
        self.calls: list = []
        self.existing = list(existing)
        self.visible = [BL.BL_F_Cu, BL.BL_User_2]
        self.visibility_calls: list = []
        self.shapes: list = []

    def get_shapes(self):
        return self.shapes

    def get_visible_layers(self):
        return list(self.visible)

    def set_visible_layers(self, layers):
        self.visibility_calls.append(list(layers))
        self.visible = list(layers)

    def get_reference_images(self):
        return self.existing

    def begin_commit(self):
        self.calls.append("begin_commit")
        return "c"

    def remove_items(self, items):
        self.calls.append(("remove_items", len(items)))
        self.removed = list(items)

    def push_commit(self, c, message=""):
        self.calls.append(("push_commit", message))

    def drop_commit(self, c):
        self.calls.append("drop_commit")

    def set_enabled_layers(self, copper_layer_count, layers):
        self.calls.append("set_enabled_layers")
        return super().set_enabled_layers(copper_layer_count, layers)


def image(tmp_path: Path) -> overlay.OverlayImage:
    path = tmp_path / "x.png"
    path.write_bytes(b"png")
    return overlay.OverlayImage(
        "F.Cu", "User.1", path, overlay.image_placement((1, 2), (10, 10), 0.1)
    )


PLACEMENT = overlay.image_placement((1, 2), (10, 10), 0.1)


class Existing:
    """A reference image as kipy reports it."""

    def __init__(self, layer, centre=PLACEMENT.centre, scale=PLACEMENT.scale, data=b""):
        self.layer = layer
        self.image_data = data
        self.position = Vector2.from_xy(
            round(centre[0] * NM_PER_MM), round(centre[1] * NM_PER_MM)
        )
        self.image_scale = scale


@pytest.mark.parametrize(
    "existing, ours",
    [
        (Existing(BL.BL_User_1), True),
        (Existing(BL.BL_User_1, (PLACEMENT.centre[0] + 0.0005, 2.5)), True),
        (Existing(BL.BL_User_1, (PLACEMENT.centre[0] + 0.01, 2.5)), False),
        (Existing(BL.BL_User_1, scale=2.0), False),
        (Existing(BL.BL_Cmts_User), False),
    ],
)
def test_only_images_at_the_overlay_placement_are_our_overlays(existing, ours):
    assert overlay.is_overlay(existing, PLACEMENT, ("User.1", "User.2")) is ours  # type: ignore[arg-type]


def png(path: Path, metadata: dict[str, str]) -> bytes:
    render.matplotlib.image.imsave(
        path, np.zeros((2, 2, 4), np.uint8), metadata=metadata
    )
    return path.read_bytes()


def test_written_overlays_carry_the_kistitch_tag(tmp_path: Path):
    maps = [coverage("GND", "F.Cu", np.zeros((3, 3)))]

    data = render.write_overlays(tmp_path, grid_of(maps), maps, {}, D, STYLE)[
        "F.Cu"
    ].read_bytes()

    assert overlay.png_text(data)["kistitch"] == "pourstitch-overlay"


# Overlays placed before the rename from breakneck carry its name.
BREAKNECK_TAG = ("breakneck", "pourstitch-overlay")


@pytest.mark.parametrize("tag", [overlay.OVERLAY_TAG, BREAKNECK_TAG])
def test_tagged_image_anywhere_on_an_overlay_layer_is_ours(tmp_path: Path, tag):
    # The outline changed since it was placed: the placement no longer matches.
    data = png(tmp_path / "t.png", dict([tag]))
    moved = Existing(BL.BL_User_1, (30, 30), 2.0, data)
    elsewhere = Existing(BL.BL_Cmts_User, data=data)

    assert overlay.is_overlay(moved, PLACEMENT, ("User.1",))  # type: ignore[arg-type]
    assert not overlay.is_overlay(elsewhere, PLACEMENT, ("User.1",))  # type: ignore[arg-type]


def test_untagged_image_counts_only_at_the_overlay_placement(tmp_path: Path):
    data = png(tmp_path / "u.png", {"Software": "something else"})
    users_own = Existing(BL.BL_User_1, (30, 30), data=data)
    old_overlay = Existing(BL.BL_User_1, data=data)

    assert not overlay.is_overlay(users_own, PLACEMENT, ("User.1",))  # type: ignore[arg-type]
    assert overlay.is_overlay(old_overlay, PLACEMENT, ("User.1",))  # type: ignore[arg-type]


@pytest.mark.parametrize("data", [b"", b"not a png", overlay.PNG_SIGNATURE + b"\0\0"])
def test_png_text_of_non_png_or_truncated_data_is_empty(data):
    assert overlay.png_text(data) == {}


def test_png_size_reads_the_header(tmp_path: Path):
    render.matplotlib.image.imsave(tmp_path / "s.png", np.zeros((3, 7, 4), np.uint8))

    assert overlay.png_size((tmp_path / "s.png").read_bytes()) == (7, 3)


def test_overlay_placement_follows_the_board_outline_raster():
    bounds = (1.0, 2.0, 2.04, 3.0)  # 10.4 pixels wide rounds up to 11

    placement = overlay.board_placement(bounds)

    assert placement == overlay.image_placement((1.0, 2.0), (11, 10), 0.1)
    grid = Grid.covering(bounds)
    assert placement == overlay.image_placement(
        (grid.x0, grid.y0), (grid.width, grid.height), grid.pixel
    )


def test_place_overlays_replaces_images_in_one_commit(tmp_path, monkeypatch):
    created = []
    monkeypatch.setattr(
        overlay, "create_checked", lambda board, items: created.extend(items)
    )
    users_own = Existing(BL.BL_User_1, (30, 30))
    board = ImageBoard(
        existing=[Existing(BL.BL_User_1), Existing(BL.BL_Cmts_User), users_own]
    )

    assert overlay.place_overlays(
        board,  # type: ignore[arg-type]
        [image(tmp_path)],
        PLACEMENT,
        ("User.1", "User.2"),
        "label",
    )

    assert board.calls == [
        "begin_commit",
        ("remove_items", 1),
        ("push_commit", "label"),
    ]
    assert board.removed == [board.existing[0]]
    (ref,) = created
    assert ref.layer == BL.BL_User_1
    assert (ref.position.x, ref.position.y) == (1_500_000, 2_500_000)
    assert ref.image_scale == pytest.approx(1.0)
    assert ref.image_data == b"png"


def test_place_overlays_commits_under_the_given_label(tmp_path, monkeypatch):
    monkeypatch.setattr(overlay, "create_checked", lambda board, items: None)
    board = ImageBoard()

    overlay.place_overlays(
        board,  # type: ignore[arg-type]
        [image(tmp_path)],
        PLACEMENT,
        ("User.1",),
        "other overlay",
    )

    assert board.calls == ["begin_commit", ("push_commit", "other overlay")]


def test_refused_overlay_falls_back_to_manual_placement(tmp_path, monkeypatch):
    def refuse(board, items):
        raise CreateItemsError("no overlapping layers")

    monkeypatch.setattr(overlay, "create_checked", refuse)
    board = ImageBoard()

    assert not overlay.place_overlays(
        board,  # type: ignore[arg-type]
        [image(tmp_path)],
        PLACEMENT,
        ("User.1",),
        "label",
    )

    assert board.calls == ["begin_commit", "drop_commit"]


def test_placing_no_images_removes_our_overlays_only(monkeypatch):
    created = []
    monkeypatch.setattr(
        overlay, "create_checked", lambda board, items: created.extend(items)
    )
    ours = Existing(BL.BL_User_2)
    board = ImageBoard(
        existing=[ours, Existing(BL.BL_User_1, (30, 30)), Existing(BL.BL_Cmts_User)]
    )

    assert overlay.place_overlays(
        board,  # type: ignore[arg-type]
        [],
        PLACEMENT,
        ("User.1", "User.2"),
        "label",
    )

    assert board.calls == [
        "begin_commit",
        ("remove_items", 1),
        ("push_commit", "label"),
    ]
    assert board.removed == [ours]
    assert created == []


def test_placing_no_images_over_none_makes_no_commit():
    board = ImageBoard(existing=[Existing(BL.BL_User_1, (30, 30))])

    assert overlay.place_overlays(
        board,  # type: ignore[arg-type]
        [],
        PLACEMENT,
        ("User.1",),
        "label",
    )

    assert board.calls == []


def test_previous_overlays_are_found_at_the_board_placement(tmp_path, monkeypatch):
    # The new images' own placement plays no part: an untagged old overlay
    # counts by where the board puts overlays.
    monkeypatch.setattr(overlay, "create_checked", lambda board, items: None)
    elsewhere = overlay.image_placement((5, 5), (10, 10), 0.1)
    board = ImageBoard(existing=[Existing(BL.BL_User_1, elsewhere.centre)])

    overlay.place_overlays(
        board,  # type: ignore[arg-type]
        [image(tmp_path)],
        elsewhere,
        ("User.1",),
        "label",
    )

    assert ("remove_items", 1) in board.calls


def test_placing_overlays_shows_their_layers_keeping_visible_ones(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(overlay, "create_checked", lambda board, items: None)
    board = ImageBoard()
    second = overlay.OverlayImage(
        "B.Cu", "User.3", image(tmp_path).path, image(tmp_path).placement
    )

    overlay.place_overlays(
        board,  # type: ignore[arg-type]
        [image(tmp_path), second],
        PLACEMENT,
        ("User.1", "User.2", "User.3"),
        "label",
    )

    assert set(board.visible) == {BL.BL_F_Cu, BL.BL_User_2, BL.BL_User_1, BL.BL_User_3}
    assert len(board.visibility_calls) == 1


def test_placing_markers_with_overlays_shows_the_marker_layer(tmp_path, monkeypatch):
    monkeypatch.setattr(overlay, "create_checked", lambda board, items: None)
    board = ImageBoard()

    overlay.place_overlays(
        board,  # type: ignore[arg-type]
        [],
        PLACEMENT,
        ("User.1",),
        "label",
        markers=[Marker((1, 1), MarkerKind.UNFIXABLE)],
    )

    assert set(board.visible) == {BL.BL_F_Cu, BL.BL_User_2, BL.BL_Eco2_User}


def created_on_enabled_layers(created: list):
    """create_checked as KiCad answers: an item on a disabled layer is refused."""

    def create(board, items):
        if any(i.layer not in board.enabled for i in items):
            raise CreateItemsError("no overlapping layers")
        created.extend(items)

    return create


def test_placing_markers_enables_the_marker_layer_before_the_commit(
    tmp_path, monkeypatch, log
):
    created: list = []
    monkeypatch.setattr(overlay, "create_checked", created_on_enabled_layers(created))
    board = ImageBoard(enabled=(BL.BL_User_1, BL.BL_F_SilkS))

    assert overlay.place_overlays(
        board,  # type: ignore[arg-type]
        [image(tmp_path)],
        PLACEMENT,
        ("User.1",),
        "label",
        markers=[Marker((1, 1), MarkerKind.UNFIXABLE)],
    )

    assert board.calls == [
        "set_enabled_layers",
        "begin_commit",
        ("push_commit", "label"),
    ]
    [(_, layers)] = board.set_calls
    assert sorted(layers) == sorted([BL.BL_User_1, BL.BL_F_SilkS, BL.BL_Eco2_User])
    assert {i.layer for i in created} == {BL.BL_User_1, BL.BL_Eco2_User}
    assert any("Enabled User.Eco2" in m and "undo" in m for m in log)


def test_marker_layer_kicad_does_not_enable_fails_before_the_overlay_commit(
    tmp_path, monkeypatch
):
    created: list = []
    monkeypatch.setattr(overlay, "create_checked", created_on_enabled_layers(created))
    board = ImageBoard(enabled=(BL.BL_User_1,))
    board.refuse = {BL.BL_Eco2_User}

    with pytest.raises(MarkerLayerError, match="Board Setup → Board Editor Layers"):
        overlay.place_overlays(
            board,  # type: ignore[arg-type]
            [image(tmp_path)],
            PLACEMENT,
            ("User.1",),
            "label",
            markers=[Marker((1, 1), MarkerKind.UNFIXABLE)],
        )

    assert "begin_commit" not in board.calls
    assert created == []


def test_overlay_layers_already_visible_leave_visibility_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(overlay, "create_checked", lambda board, items: None)
    board = ImageBoard()
    board.visible.append(BL.BL_User_1)

    overlay.place_overlays(
        board,  # type: ignore[arg-type]
        [image(tmp_path)],
        PLACEMENT,
        ("User.1",),
        "label",
    )

    assert board.visibility_calls == []


def test_only_removing_overlays_and_markers_leaves_visibility_alone(monkeypatch):
    monkeypatch.setattr(overlay, "create_checked", lambda board, items: None)
    board = ImageBoard(existing=[Existing(BL.BL_User_1)])
    board.shapes = [eco2_shape()]

    overlay.place_overlays(
        board,  # type: ignore[arg-type]
        [],
        PLACEMENT,
        ("User.1",),
        "label",
        markers=[],
    )

    assert ("remove_items", 2) in board.calls
    assert board.visibility_calls == []


def test_refused_overlays_leave_visibility_alone(tmp_path, monkeypatch):
    def refuse(board, items):
        raise CreateItemsError("no overlapping layers")

    monkeypatch.setattr(overlay, "create_checked", refuse)
    board = ImageBoard()

    overlay.place_overlays(
        board,  # type: ignore[arg-type]
        [image(tmp_path)],
        PLACEMENT,
        ("User.1",),
        "label",
    )

    assert board.visibility_calls == []


def test_failing_to_show_overlay_layers_only_warns(tmp_path, monkeypatch, log):
    monkeypatch.setattr(overlay, "create_checked", lambda board, items: None)

    class BusyBoard(ImageBoard):
        def get_visible_layers(self):
            raise ApiError("busy", code=ApiStatusCode.AS_BUSY)

    board = BusyBoard()

    assert overlay.place_overlays(
        board,  # type: ignore[arg-type]
        [image(tmp_path)],
        PLACEMENT,
        ("User.1",),
        "label",
    )

    assert ("push_commit", "label") in board.calls
    assert any("User.1" in line and "visible" in line for line in log)


def test_failing_to_set_overlay_layers_visible_only_warns(tmp_path, monkeypatch, log):
    monkeypatch.setattr(overlay, "create_checked", lambda board, items: None)

    class RefusingBoard(ImageBoard):
        def set_visible_layers(self, layers):
            raise ApiError("busy", code=ApiStatusCode.AS_BUSY)

    board = RefusingBoard()

    assert overlay.place_overlays(
        board,  # type: ignore[arg-type]
        [image(tmp_path)],
        PLACEMENT,
        ("User.1",),
        "label",
    )

    assert ("push_commit", "label") in board.calls
    assert "drop_commit" not in board.calls
    assert any("User.1" in line and "visible" in line for line in log)


class ClearBoard:
    """A board with an Edge.Cuts outline, Eco2 markers and reference images."""

    def __init__(self, outline, images):
        self.shapes = [*outline, eco2_shape()]
        self.images = images
        self.removed: list = []
        self.visibility_calls: list = []

    def get_visible_layers(self):
        return []

    def set_visible_layers(self, layers):
        self.visibility_calls.append(list(layers))

    def get_shapes(self):
        return self.shapes

    def get_reference_images(self):
        return self.images

    def begin_commit(self):
        return "c"

    def remove_items(self, items):
        self.removed += items

    def push_commit(self, c, message=""):
        pass


def segment(layer, start, end):
    seg = BoardSegment()
    seg.layer = layer
    seg.start = Vector2.from_xy(
        round(start[0] * NM_PER_MM), round(start[1] * NM_PER_MM)
    )
    seg.end = Vector2.from_xy(round(end[0] * NM_PER_MM), round(end[1] * NM_PER_MM))
    return seg


def eco2_shape():
    return segment(BL.BL_Eco2_User, (1, 1), (2, 2))


def rectangle_outline(x0, y0, x1, y1):
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    return [segment(BL.BL_Edge_Cuts, a, b) for a, b in pairwise(corners)]


def test_clear_removes_our_overlays_and_keeps_other_images(tmp_path, monkeypatch):
    bounds = (0.0, 0.0, 30.0, 20.0)
    placement = overlay.board_placement(bounds)
    ours = Existing(BL.BL_User_2, placement.centre, placement.scale)
    # Placed by breakneck before the outline changed.
    moved_breakneck = Existing(
        BL.BL_User_3, (40, 40), 2.0, png(tmp_path / "b.png", dict([BREAKNECK_TAG]))
    )
    users_own = Existing(BL.BL_User_1, (5, 5), 1.0)
    board = ClearBoard(rectangle_outline(*bounds), [ours, moved_breakneck, users_own])
    monkeypatch.setattr(
        cli.kipy, "KiCad", lambda: type("K", (), {"get_board": lambda self: board})()
    )

    args = argparse.Namespace(
        overlay_layers=list(overlay.DEFAULT_OVERLAY_LAYERS),
        dry_run=False,
        vias=False,
        nets=None,
        refill=True,
    )
    cli.execute_clear(args)

    assert ours in board.removed
    assert moved_breakneck in board.removed
    assert users_own not in board.removed
    assert any(getattr(s, "layer", None) == BL.BL_Eco2_User for s in board.removed)
    assert board.visibility_calls == []


def test_prepare_overlay_layers_enables_missing_layers_keeping_every_enabled_one(log):
    kept = (BL.BL_F_SilkS, BL.BL_Edge_Cuts, BL.BL_User_2)
    board = LayersBoard(enabled=kept)

    ready = overlay.prepare_overlay_layers(
        board,  # type: ignore[arg-type]
        overlay.Outputs(overlay=True),
        dry_run=False,
    )

    assert ready.place
    assert ready.enabled == ("User.1", "User.3", "User.4")
    [(count, layers)] = board.set_calls
    assert count == len(COPPER_4)
    assert set(kept) <= set(layers)
    assert {BL.BL_User_1, BL.BL_User_3, BL.BL_User_4} <= set(layers)
    assert not set(COPPER_4) & set(layers)
    [line] = [m for m in log if "Enabled" in m]
    assert "User.1, User.3, User.4" in line
    assert "undo" in line


def test_prepare_overlay_layers_enables_only_the_layers_the_copper_count_needs():
    board = LayersBoard(copper=(BL.BL_F_Cu, BL.BL_B_Cu), enabled=(BL.BL_F_SilkS,))

    overlay.prepare_overlay_layers(board, overlay.Outputs(overlay=True), dry_run=False)  # type: ignore[arg-type]

    [(count, layers)] = board.set_calls
    assert count == 2
    assert sorted(layers) == sorted([BL.BL_F_SilkS, BL.BL_User_1, BL.BL_User_2])


def test_prepare_overlay_layers_leaves_the_layers_alone_when_all_are_enabled():
    board = LayersBoard(
        enabled=(BL.BL_User_1, BL.BL_User_2, BL.BL_User_3, BL.BL_User_4)
    )

    ready = overlay.prepare_overlay_layers(
        board,  # type: ignore[arg-type]
        overlay.Outputs(overlay=True),
        dry_run=False,
    )

    assert ready == overlay.PreparedLayers(place=True, enabled=())
    assert board.set_calls == []


def test_prepare_overlay_layers_without_overlays_asked_places_nothing():
    # Too few overlay layers would raise if overlays were asked for.
    board = LayersBoard()
    outputs = overlay.Outputs(overlay=False, overlay_layers=("User.1",))

    ready = overlay.prepare_overlay_layers(board, outputs, dry_run=False)  # type: ignore[arg-type]

    assert ready == overlay.PreparedLayers(place=False, enabled=())
    assert board.set_calls == []


def test_prepare_overlay_layers_in_dry_run_warns_without_enabling(log):
    board = LayersBoard()

    ready = overlay.prepare_overlay_layers(
        board,  # type: ignore[arg-type]
        overlay.Outputs(overlay=True),
        dry_run=True,
    )

    assert ready == overlay.PreparedLayers(place=False, enabled=())
    assert board.set_calls == []
    assert any("User.1" in m and "PNGs only" in m for m in log)


def test_prepare_overlay_layers_fails_when_kicad_does_not_enable_a_layer():
    board = LayersBoard(refuse=(BL.BL_User_3,))

    with pytest.raises(overlay.OverlayLayersError, match="User.3") as e:
        overlay.prepare_overlay_layers(
            board,  # type: ignore[arg-type]
            overlay.Outputs(overlay=True),
            dry_run=False,
        )

    assert "User.1" not in str(e.value)


@pytest.mark.parametrize("bad", ["User3", "In3.Cu"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_prepare_overlay_layers_rejects_a_name_that_is_not_an_overlay_layer(
    bad, dry_run
):
    board = LayersBoard()
    outputs = overlay.Outputs(
        overlay=True, overlay_layers=("User.1", bad, "User.2", "User.4")
    )

    with pytest.raises(
        overlay.OverlayLayersError, match="not valid overlay layers"
    ) as e:
        overlay.prepare_overlay_layers(board, outputs, dry_run=dry_run)  # type: ignore[arg-type]

    assert str(e.value).startswith(f"{bad} are not valid overlay layers")
    assert board.set_calls == []
