# Heatmaps

This page explains the heatmaps that `returnvia` and `pourstitch` draw, how to read their colours, and how to place them on the board. For an overview and the first run, see the [README](../README.md).

Both commands can show their result two ways:

- `--render PATH` writes a figure to a file. The format follows the file's extension.
- `--overlay` places one transparent image per copper layer on the board, lined up with the copper.

Both work with a placing run, where the heatmap includes the vias the run planned, even in a dry run that places none, and with `--check`, where it measures the board as it is.

## Check runs

`returnvia --check` and `pourstitch --check` measure the existing vias only. They do no planning, run no DRC and place no vias. Combine them with `--render` or `--overlay` to see the current state.

Every `--overlay` run commits its images, so it adds an undo step in KiCad. For repeated checks while you place vias by hand, use `--check --render` and keep the PNG open in an image viewer that reloads it. Its zone refill still adds an undo step, unless refills are off (see [Zone refills](cli.md#zone-refills)). Use `--overlay` for a one-off look on the board:

```bash
kistitch returnvia --check --render returnvia.png
kistitch pourstitch --power --check --render power.png
kistitch pourstitch --check --overlay
```

A `--check --overlay` run also replaces every shape on User.Eco2, hand-drawn ones included, in the same undo step as the images: `returnvia` draws a circle at each via or through-hole pad with an unserved end, and `pourstitch` only removes the old markers. See [Markers and the DRC check](markers-and-drc.md#markers) for when markers change.

## Pour stitching heatmap

`pourstitch --render PATH` writes a figure with one panel per net and layer, for the nets of the run's class. Copper is coloured by its distance to the nearest stitch, measured through the copper. In the figure, existing vias are rings, planned vias are filled dots and spots where no via fits are cyan crosses; on the board, the vias show as themselves.

Copper beyond the run's distance (the class's default or `--distance`) is pink.

Copper that no via could ever bring within that distance is grey. It is farther than the distance, through the net's copper on its layer, from every spot where a via could join the net's fill on two or more layers. Typical grey copper is:

- a local zone on one layer whose other side is another net's pour;
- the far end of such a zone, joined to the main pour by a long neck.

Other-net clearances are ignored for this, so a spot blocked only by other copper shows pink, not grey. Grey areas are left out of the uncovered area that the run logs and are reported separately.

## Return via heatmap

`returnvia --render PATH` writes one panel per copper layer on which an unserved via or pad end lies. By default each panel shows only the copper inside the rings around the unserved ends, which picks out the problem vias and pads. With nothing unserved, the figure is the board outline with a title saying so. `--overlay` then places no images and removes the previous ones.

`--full-map` shows the copper of every layer on which a via or through-hole pad that changes layers connects. That is every net's tracks, pads and pours, ground pours included, since a via placed anywhere in that copper would need its own return, and the ground vias. Signal vias show only on layers where they have a measured end.

Each pixel is coloured by the distance through its reference plane to the nearest ground return. Its reference plane is chosen as for a via or pad end there: the nearest other copper layer in stack order with a ground zone outline at that pixel (see [Return vias](return-vias.md#which-vias-are-measured)).

Around each via or through-hole pad:

- A via or pad end shows its own measured return distance over its copper, and over its net's copper in its antipad, the void around it in the reference plane; for a pad that is its clearance hole. This includes antipads that merge with others. Where antipads merge, the copper shows the worst of their distances.
- Another net's copper over the antipad stays purple: it has no return there.
- An exempt through-hole pad, connected on fewer than two layers, has no end: its copper shows its plane's field like other copper, purple over its clearance hole.

The colour scale:

- Copper within the served threshold, `--distance` plus the 0.1 mm margin, is dark grey-blue, lightening towards the threshold.
- Beyond the threshold the colour steps to yellow and runs through matplotlib's `plasma_r` to dark blue at three times the threshold, and stays dark blue farther out.
- Purple copper has no path to a return: it lies over a slot or void in its reference plane.
- Copper with no ground zone outline on any other layer has no reference plane and is left blank, such as the ground side of a two-layer board.

A pink ring of radius `--distance` marks each unserved via or pad end on its own layer. Layers with identical maps share a panel.

`returnvia` logs how many vias and pads are exempt, how many are unserved, and how many of their plane returns are unserved. Set `LOGURU_LEVEL=DEBUG` to list each unserved via or pad end with its distance or the reason it can't be served.

## Overlays on the board

`--overlay` places one transparent image per copper layer on the board as a reference image, scaled and positioned to line up with the copper. Each image carries its colour bar and label in a strip below the board outline.

The images go on User.1–User.4, one per copper layer in stack order, or on the layers given with `--overlay-layers`. A board with more copper layers than that needs `--overlay-layers` with one user layer per copper layer.

A run enables any of those layers that are disabled and logs which ones. It makes the layers that get an image visible, keeping the other visible layers. Neither change is in KiCad's undo history. When one of the overlay layers is disabled, a dry run leaves it disabled and writes the images without placing any.

The board stores the images itself. The PNGs are written to a temporary directory and removed once placed. When they can't be placed, as in a dry run with the layers disabled, they stay there and the log lists their paths and placements, for placing by hand.

Toggle a layer's visibility to show or hide its heatmap. `kistitch clear` removes them (see [Clearing](clearing.md)). Both `clear` and a new `--overlay` run replace only kistitch's own overlays, also after the board outline has changed. They recognise them by a `kistitch` text chunk in the PNG, or the `breakneck` chunk of overlays placed under kistitch's former name. An image without either chunk is taken as an overlay of an earlier build when its centre and scale match the overlay placement for the current board outline, so a reference image of your own at exactly that placement is removed too. Other reference images on those layers stay.
