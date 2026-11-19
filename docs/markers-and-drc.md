# Markers and the DRC check

This page explains what happens in a run of `returnvia`, `padvia` or `pourstitch` that places vias: the DRC check before the commit, the markers it leaves on User.Eco2, and what happens when a new via cuts another net's pour. For an overview and the first run, see the [README](../README.md).

## Before you run

The board must have been saved to disk with its project file next to it, and rule and netclass changes saved before a run, as the README's [Before the first run](../README.md#before-the-first-run) explains.

The DRC check uses `kicad-cli`. kistitch takes it from `--kicad-cli PATH`, then from the running KiCad, then from `PATH`, then from the standard install locations.

## What a placing run does

1. It refills the zones, so the planner sees current fills (see [Zone refills](cli.md#zone-refills)).
2. It plans the vias from a snapshot of the board.
3. It checks the plan with `kicad-cli pcb drc` on a temporary copy of the board with the vias added, against a DRC run on an unmodified copy. Vias implicated in new violations are dropped, and the planner plans again, keeping new vias at least 0.5 mm from each dropped one. This repeats for up to five rounds. At the fifth, the vias still implicated are dropped without another DRC run.
4. It commits the remaining vias, their stubs and any markers as one undo step, replacing the markers of earlier runs. Then it refills the zones, unless refills are off.

If the board was edited in KiCad while the run was going, nothing is committed: DRC never saw the edit. Run it again.

A dry run does the first three steps and commits nothing (see [Dry runs](cli.md#dry-runs)).

## What the planners check

The planners keep fixed clearances and don't read custom design rules:

- 0.3 mm to other-net copper, or a pad's own clearance override plus 0.1 mm where that is larger;
- 0.3 mm between drilled holes;
- 0.5 mm from the board edge;
- 0.15 mm from pads of the via's own net, so no via lands in a pad;
- outside via keepout rule areas.

Other nets' pours are not obstacles: they refill around the new vias, as in any KiCad layout. The DRC check is what enforces your board's own rules, including custom ones.

## Markers

Spots that need attention get markers on User.Eco2:

- A circle where no via fits, from `returnvia` and `padvia`. It is at least 0.7 mm across and reaches 0.3 mm past the copper it marks, so copper drawn in front can't hide it: on a pad, it encloses the pad.
- A circle at each via or through-hole pad with an unserved end, after `returnvia --check --overlay`.
- A square at a new DRC violation that couldn't be pinned on a single via, including pour splits (below).

`pourstitch` doesn't mark uncovered spots; its [heatmap](heatmaps.md) shows them as areas.

### When markers are replaced

The markers always show the latest run's findings:

- Each placing run of `returnvia`, `padvia` or `pourstitch` that doesn't stop with an error replaces the previous markers, whichever command made them. It removes every shape on User.Eco2, hand-drawn ones included, in the same undo step, even when it places nothing. After `padvia` then `pourstitch`, only `pourstitch`'s markers remain.
- A `--check --overlay` run replaces them too, in the undo step that places the overlay images. `returnvia` draws a circle at each via or through-hole pad with an unserved end, matching the heatmap's rings, and `pourstitch` only removes the old markers. A `--check --overlay` run with nothing to show, such as a `returnvia` check with every via and pad end served, still removes the old markers.

These leave the markers alone:

- a `--check` run without `--overlay`;
- a `--check --overlay` run that can't place overlays, as in a dry run with the overlay layers disabled;
- a dry run of a placing command;
- a run that stops with an error, such as a `pourstitch --nets` run that lists only nets of the other class.

`kistitch clear` removes the markers without a run (see [Clearing](clearing.md)).

### Layer visibility

A run that places markers makes User.Eco2 visible if it was hidden, keeping the other visible layers. This change is outside KiCad's undo history. A run that only removes markers, and `kistitch clear`, leave the visibility alone.

## Pour splits

Other nets' pours are not obstacles, so a single via can cut a narrow neck of another net's pour. DRC reports that as unconnected items or isolated copper and names the pour's pads or zones, not the via, so the via can't be dropped for it.

kistitch counts these violations per net, in DRC of the board as it is and with the planned vias added. When a net's count goes up, the run logs the violations and commits anyway, with a square marker at each violation, for fixing by hand. An existing violation that DRC reports at different items after the refill leaves the count unchanged, so it is logged as moved, and it gets a marker too.

`--no-allow-splits` makes a run with new violations of this kind commit nothing and exit with an error naming the net and listing the violations. `--allow-splits` is the default. A dry run lists them without failing, and says what a real run would do.

## Runs add vias

Every check counts the vias already on the board, so repeating a run adds nothing new where the board is already covered. To remove a run's vias, undo it, or run `kistitch clear --vias` (see [Clearing](clearing.md) for which vias it removes).

KiCad 10 doesn't allow creating groups through its API, so a run's vias can't be grouped for selecting later.
