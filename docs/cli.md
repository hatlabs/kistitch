# Command line

This page is the reference for the `kistitch` command: its subcommands and options, the order to run them in, zone refills and dry runs. For an overview and the first run, see the [README](../README.md). The [panel](panel.md) runs the same steps from a window.

`kistitch` works on the board open in KiCad's PCB editor, through the KiCad API. Run `kistitch -h`, or `kistitch <command> -h`, to see the options with their help text. To install it from PyPI, see [Without the plugin manager](../README.md#without-the-plugin-manager).

## Commands

| Command | What it does |
|---|---|
| `padvia` | Places a via and a stub track for every SMD pad that needs one ([Pad vias](pad-vias.md)). |
| `pourstitch` | Stitches the ground nets' pours, or with `--power` every other net's pours, so every point is within a distance of a via ([Pour stitching](pour-stitching.md)). |
| `returnvia` | Places a ground return via near every via on a non-ground net that lacks one ([Return vias](return-vias.md)). |
| `clear` | Removes the markers on User.Eco2 and kistitch's heatmap overlays, and with `--vias` stitching vias ([Clearing](clearing.md)). |

`kistitch-panel` opens the [panel](panel.md).

## Global options

These go before the command, as in `kistitch --dry-run returnvia`.

| Option | Effect |
|---|---|
| `--dry-run`, `-n` | Do everything except commit the changes ([Dry runs](#dry-runs)). |
| `--refill`, `--no-refill` | Refill the zones before measuring or placing, after committing, and before and after `clear --vias`. On by default ([Zone refills](#zone-refills)). |
| `--version` | Print the version. |

`LOGURU_LEVEL=DEBUG` in the environment turns on debug logging, such as the list of unserved via and pad ends from `returnvia`.

## Options of the placing commands

`padvia`, `pourstitch` and `returnvia` share these:

| Option | Effect |
|---|---|
| `--via DIAMETER/DRILL` | Via size in mm, such as `0.6/0.3`, instead of the netclass's size. For `returnvia`, the default is the ground net's netclass. |
| `--kicad-cli PATH` | The `kicad-cli` binary for the DRC check ([Before you run](markers-and-drc.md#before-you-run)). |
| `--allow-splits`, `--no-allow-splits` | Commit even when DRC finds new unconnected or isolated copper that no via could be blamed for, marking the spots (the default), or refuse to commit ([Pour splits](markers-and-drc.md#pour-splits)). |

`pourstitch` and `returnvia` also take these:

| Option | Effect |
|---|---|
| `--distance`, `-d` | The distance in mm: from a via or pad to its return for `returnvia` (default 1.5), through the copper to a stitch for `pourstitch` (default 3 for ground, 10 with `--power`). |
| `--ground-nets NET [NET ...]` | The ground nets. By default, the nets with zones on two or more inner layers, else `GND` ([Ground nets](return-vias.md#ground-nets)). |
| `--check` | Only measure the existing vias; place none ([Check runs](heatmaps.md#check-runs)). |
| `--render PATH` | Write the heatmap panels to PATH ([Heatmaps](heatmaps.md)). |
| `--overlay` | Place per-layer heatmaps on the board as reference images. |
| `--overlay-layers LAYER [LAYER ...]` | User layers for the overlays, one per copper layer in stack order. Default User.1 to User.4. |

Options of one command:

| Command | Option | Effect |
|---|---|---|
| `padvia` | `--nets NET [NET ...]` | Only the pads of these nets. |
| `pourstitch` | `--power` | Stitch the nets that are not ground instead of the ground nets. |
| `pourstitch` | `--nets NET [NET ...]` | Only these nets, within the run's class. |
| `pourstitch` | `--fallback-via DIAMETER/DRILL` | Smaller via size for spots the netclass size doesn't fit. Default `0.5/0.3`. |
| `returnvia` | `--force` | Place a placeholder via where none fits with full clearance ([Forced vias](return-vias.md#forced-vias)). |
| `returnvia` | `--full-map` | Map all copper, not only the copper around unserved via and pad ends. |
| `clear` | `--vias` | Also remove stitching vias ([Clearing](clearing.md)). |
| `clear` | `--nets NET [NET ...]` | Nets whose vias `--vias` removes. Default: every net with a zone. Needs `--vias`. |
| `clear` | `--overlay-layers LAYER [LAYER ...]` | User layers to remove kistitch overlays from. Default User.1 to User.4. |

## Order of runs

```bash
kistitch padvia
kistitch pourstitch --power
kistitch returnvia
kistitch pourstitch
```

The README's [Using it](../README.md#using-it) gives the reason for this order, the checks by hand between the steps and the final checks.

## Zone refills

By default kistitch refills the zones:

- before measuring or placing, so every run works from current fills;
- before picking the vias for `clear --vias`;
- after committing vias, or removing them with `clear --vias`, so the board is left filled.

Each refill adds an undo step in KiCad. Undoing a placement takes two Ctrl+Z: one for the refill after the commit and one for the commit.

`kistitch --no-refill <command>` turns all of these off. The run uses the zone fills as they are and logs that once, and refilling in KiCad (press B) before running kistitch is up to you. With stale fills, the planner and the heatmaps see the old copper. The DRC check is unaffected, since it refills its own copy of the board.

## Dry runs

`kistitch --dry-run <command>` plans, runs the DRC check, logs what it would place and writes the `--render` output, but commits no vias or markers.

Some things still happen in a dry run:

- The zone refill before planning runs, unless refills are off.
- `--overlay` still places its images. With `--check`, it also replaces every shape on User.Eco2, hand-drawn ones included, with the run's markers ([Check runs](heatmaps.md#check-runs)). Overlay layers that are disabled stay disabled, and the images are then only written to files ([Overlays on the board](heatmaps.md#overlays-on-the-board)).

The heatmaps of a dry run include the planned vias. For `returnvia` they count as returns, except forced ones, so a dry run shows which via and pad ends the run would leave unserved.

A dry run of `clear --vias` refills, then logs how many vias per net would go, and removes nothing.

## When KiCad is busy

KiCad refuses API calls while a tool or dialog is open in the PCB editor. kistitch then stops with a message saying so; close the tool or dialog and run again. If kistitch can't find the open board, see [Before the first run](../README.md#before-the-first-run).
