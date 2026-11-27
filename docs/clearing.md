# Clearing

This page explains what `kistitch clear` removes: markers and heatmap overlays, and with `--vias`, stitching vias. For an overview and the first run, see the [README](../README.md).

## Markers and heatmaps

`kistitch clear` removes, in one undo step:

- every shape on User.Eco2, the markers of earlier runs and hand-drawn shapes alike;
- kistitch's heatmap overlays on User.1–User.4, or on the layers given with `--overlay-layers`, recognised as in [Overlays on the board](heatmaps.md#overlays-on-the-board). Other reference images on those layers stay.

It doesn't refill the zones and doesn't change which layers are visible.

## Stitching vias

`kistitch clear --vias` also removes stitching vias, so that `pourstitch` and `returnvia` can place a fresh set after layout changes.

A via is removed when all of these hold:

- it touches a fill of its own net on at least one layer;
- no track or pad of its own net touches it;
- it is not locked.

Vias in pads, fanout vias at the end of a track and locked vias stay.

**This removes every via touching a pour of its own net that no track or pad ties down, including vias you placed by hand.** That includes fanout vias tied to a pad only through a pour, and vias that are the only link between two pours of the same net. To keep such a via, connect it with a short track or lock it.

`--nets NET [NET ...]` limits the removal to those nets. By default every net with a zone is cleared. `--nets` needs `--vias`.

Vias are picked by the fills they touch, so the fills must be current. Unless refills are off, `clear --vias` refills the zones before picking the vias, and again after removing them. The removal, together with the markers and overlays, is one undo step. A selected zone with no fill can't be judged: vias that touch only that zone stay, and the run warns about it.

With `--dry-run`, it doesn't refill: it picks the vias by the fills as they are, logs how many vias per net would go, and removes nothing.

## Restoring a board after clearing

1. `kistitch clear --vias`
2. `kistitch padvia`. It adds a via with a stub track for each SMD pad that needs one, and the stubs keep those vias through later clears (see [Pad vias](pad-vias.md)).
3. The three stitching steps: `kistitch pourstitch --power`, `kistitch returnvia` and `kistitch pourstitch` (see [Command line](cli.md#order-of-runs)).
