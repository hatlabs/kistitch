# Pad vias

This page explains which SMD pads `kistitch padvia` gives a via, and how it places the via and its stub. For an overview and the first run, see the [README](../README.md).

`padvia` connects SMD pads to their net's plane or pour on another layer. Each selected pad gets a via and a short stub track from the pad to the via.

## Which pads get a via

A pad gets a via when all of these hold:

- It is an SMD pad on a single copper layer.
- Its net has zones on two or more copper layers. A net with zones on only one layer is skipped.
- Its net's fill on another copper layer is within reach of the pad, and a via of the net's size fits inside that fill. A sliver narrower than the via, such as a strip of pour left under a same-net track, doesn't count.
- The pad is not already served. A pad is served when a via or through-hole pad of its own net lies within 1 mm of it, measured copper edge to copper edge on the pad's layer, and is connected to the pad through the pad's same-net tracks, pads or pour on that layer.

`--nets GND +3.3V` limits the run to the pads of the listed nets. Use it when a net that is meant to join its plane at a single point, such as a star ground, has a zone under its pads: leave that net out of the list.

## Where the via goes

The planner looks for a spot in 48 directions around the pad. In each direction it starts where the via clears the pad and searches up to 1.2 mm farther out, taking the first spot where the via has room. This is the reach the pad selection above refers to. Of those, it keeps the one with the shortest stub, with a penalty on directions that point back towards the footprint's centre. The via therefore usually ends up on the side of the pad that faces away from the footprint, where there is room.

The via must sit inside its net's fill on every layer, other than the pad's own, where one of the net's zone outlines covers the spot. Its size comes from the net's netclass, or from `--via DIAMETER/DRILL`.

The stub starts at the pad's centre, on the pad's layer, and runs at multiples of 45°: one segment when the via lies straight or diagonally off the pad, otherwise a diagonal leg and a straight leg. Its width follows the pad's narrower side, kept between 0.2 mm and 0.4 mm.

A pad where no via fits gets a circle marker on User.Eco2. The circle encloses the pad with a margin, so copper drawn in front of the marker can't hide it (see [Markers and the DRC check](markers-and-drc.md)).

## Why the stub matters

The stub ties the via to the pad, so the via stays through later runs of `kistitch clear --vias` (see [Clearing](clearing.md) for which vias those remove).
