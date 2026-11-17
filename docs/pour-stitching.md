# Pour stitching

This page explains how `kistitch pourstitch` stitches a net's pours on different layers together, the two classes of nets it stitches and their distances, and how to prepare the zones. For an overview and the first run, see the [README](../README.md).

With Manhattan routing, outer-layer pours break up into narrow fingers between the tracks. A regular grid of stitching vias misses many of them, and an unstitched finger connected at one end can act as an antenna.

`pourstitch` places vias so that every point of a net's pours lies within a distance D of a stitch, measuring the distance through the copper rather than in a straight line. A finger whose tip is 2 mm from a via across a gap, but 8 mm away along the copper, counts as 8 mm. Copper where no via fits close enough stays uncovered; check the log and the heatmap (see [How the vias are placed](#how-the-vias-are-placed)).

A stitch is a via or through-hole pad of the net that reaches the net's fill on every layer where one of the net's zone outlines covers it, with at least two such layers. Vias and pads already on the board count, whichever command or person placed them.

## Two classes of nets

Each run stitches one class of nets at one distance:

- `pourstitch` stitches the ground nets at 3 mm. They are found as for return vias (see [Ground nets](return-vias.md#ground-nets)), and `--ground-nets` overrides the detection the same way. Without a ground net, the run stops with an error asking for `--ground-nets` and places nothing.
- `pourstitch --power` stitches every other net at 10 mm: power pours and pours of signal nets alike. On a board without a ground net, it logs that and stitches every net.

`--distance D` replaces the class's distance for every net in the run.

Only nets with pours on two or more copper layers are stitched. A net whose zones are all on one layer has nothing to stitch and is skipped.

`--nets GND GND_ISO` limits the run to the listed nets of its class. A listed net of the other class is skipped with a warning: `pourstitch --nets GND +3.3V` stitches only GND, and +3.3V needs a `--power` run. When every listed net is of the other class, as in `pourstitch --nets +3.3V`, the run stops with an error naming the flag to use. It commits nothing and leaves the markers in place.

The log names the class, its distance and the nets it stitches.

## Why power is sparse and ground dense

Power pours are stitched sparsely. They need vias to join their layers, to carry their DC current and to reach their extremities, and a 10 mm distance gives that. More power vias cost more than they give: each one cuts an antipad into the ground planes it passes through, and each one is a via on a non-ground net that needs its own ground return. Where a power pour carries high current, check that it has enough vias for the current and add more by hand.

Ground pours are stitched densely, so that the ground layers act as one reference and no ground finger is left to act as an antenna. At 3 mm the lattice pitch is about 4.2 mm, which is λ/20 at about 1.7 GHz in FR-4.

That is not dense enough for RF. Around an RF section, such as a 2.4 GHz module and its antenna feed, add ground vias by hand.

## Where a via can go

At each spot, a via must sit inside the net's fill on every copper layer where one of the net's zone outlines covers the spot, and there must be at least two such layers. Other nets' pours are not obstacles: they refill around the new vias, as in any KiCad layout. The planner keeps fixed clearances to everything else (see [Markers and the DRC check](markers-and-drc.md#what-the-planners-check)).

Via sizes come from each net's netclass. `--via DIAMETER/DRILL` overrides them. Where the netclass size doesn't fit, the run tries the smaller fallback size, `--fallback-via`, 0.5/0.3 mm by default.

## How the vias are placed

`pourstitch` rasterises the pours at 0.1 mm. It first places vias on a square lattice of pitch D·√2, so that the centre of each lattice cell is D from its corners. Alternate nets' lattices are offset by half a pitch, so they interleave. Then it fills the remaining gaps one at a time, each time at the uncovered point farthest from any stitch.

Copper where no via fits within D through the copper stays uncovered. The run logs the uncovered area per net and layer, but doesn't mark it on the board; the [heatmaps](heatmaps.md) show it. Return vias already on the board count as ground stitches, so a ground run after `returnvia` adds vias only where the return vias leave gaps.

## Set the zones' Remove islands to Never

Set each zone's Remove islands option to Never before running `pourstitch` or `returnvia`, as the README's [Before the first run](../README.md#before-the-first-run) says. With islands kept in the fill:

- kistitch can place vias in them;
- a spot over the main pour on one layer with only an island on the other passes the two-layer rule instead of dropping out, and stops showing grey in the `pourstitch` heatmap.

Islands kept this way appear as isolated copper in DRC until they are stitched. Stitching only lowers that count, so the pour split check doesn't block the run (see [Pour splits](markers-and-drc.md#pour-splits)).
