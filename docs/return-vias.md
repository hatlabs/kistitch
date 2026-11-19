# Return vias

This page explains how `kistitch returnvia` decides which vias and through-hole pads need a ground return, how it measures the distance to one, and where it places new ground vias. For an overview and the first run, see the [README](../README.md).

A signal that changes layers through a via also changes its reference plane. Its return current has to cross between the planes too, and it does that through the nearest ground via. `returnvia` places a ground via within `--distance` (1.5 mm by default) of every via and through-hole pad on a non-ground net that has no ground return that close.

## Ground nets

The ground nets are the nets with zones on two or more inner layers. On a board where no net has that, such as a two-layer board or a stackup with a single inner ground plane, the ground net is the net named `GND`. If there is no `GND` net either, the run stops with an error asking for `--ground-nets` and places nothing. The zone refill at the start of the run has already happened by then.

`--ground-nets NET [NET ...]` replaces the detection. A net listed there that has no zones, vias or pads on the board, usually a typo, gets a warning. `pourstitch` finds its ground nets the same way and takes the same option (see [Pour stitching](pour-stitching.md)).

Vias on ground nets never need a return. Every other via, power nets included, is a signal via here.

A through-hole pad on a signal net changes layers like a via. It is measured, reported and given a return via like one, and on this page "via" covers it unless the text says otherwise. Through-hole pads on ground nets are returns. SMD pads, NPTH pads and pads without a net are not measured.

## Which vias are measured

A via end is a copper layer on which the via or pad connects: a track, pad or zone fill of its own net touches its copper there. A via or pad that connects on fewer than two layers carries no layer change, such as a connector pin routed on one layer only. It is exempt and is not measured. The run logs how many vias and pads are exempt.

Each via end has a reference plane: the nearest other copper layer in stack order with a ground zone outline at the via. When two layers are equally near, the upper one wins. When several ground zones overlap on that layer, the one with the highest zone priority wins. Two ends of a via that share a reference plane are measured once.

## How the distance is measured

The return distance on a plane runs from the via's centre to the centre of the nearest return, through that plane's ground fill. It starts with a straight hop across the via's own antipad, the void around the via in the plane, and then follows the fill. A path around a slot in the plane is measured around the slot. A through-hole pad is measured from its centre too, so the hop across its clearance hole is part of its distance.

Returns are the ground net's vias and through-hole pads, counted only where the plane's fill lies within reach of them. A return only serves a plane of its own net: a GND via does not serve a via end whose plane is GND_ISO.

A via end is served when its return distance is at most `--distance` plus 0.1 mm. The extra 0.1 mm covers the 0.1 mm raster the distances are measured on. Paths on the raster run in eight directions, which overstates a straight run by up to about 8 % between those directions. The effective straight-line limit therefore depends on direction: at the default 1.5 mm it is about 1.48–1.6 mm.

Two kinds of via end are reported but can't be served by a new via:

- an end outside every ground zone: no other copper layer has a ground zone outline at the via;
- an end whose reference plane has no ground fill within `--distance` of the via.

Set `LOGURU_LEVEL=DEBUG` to have the run list each unserved via end with its distance or the reason it can't be served.

## Isolated sections

Each via end is measured against the ground net whose zone on its reference plane contains the via. A via in a GND_ISO section gets a GND_ISO return, and a via whose ends reference GND on one plane and GND_ISO on another gets one return of each.

## How new vias are placed

The planner measures candidate spots the same way as the check. For each via and ground net with unserved ends, it tries spots on rings around the via, nearest first, out to `--distance` plus the 0.1 mm margin. It takes the first spot with full clearance that serves every one of those ends. A spot across a slot in the plane is rejected when the path around the slot is too long.

A ground pin within `--distance` of a signal pin serves it like a ground via. Pins at a wider pitch, such as a 2.54 mm header routed on two layers, get a return via beside the row where one fits, or are reported as unfixable.

A return via is placed only where the ground net's zone outlines cover at least two copper layers, and it must sit inside the fill on each of them. Its size comes from the ground net's netclass, or from `--via DIAMETER/DRILL`.

A planned run therefore passes its own `returnvia --check`, apart from:

- the vias it reports as unfixable, including those left to a nearby forced via (see below);
- the vias it forced;
- vias whose return was dropped by the DRC check in its last round (see [Markers and the DRC check](markers-and-drc.md)).

Each unfixable via gets a circle marker on User.Eco2 and a line in the log.

The planner measures each ground net's plane as it was filled before the run. A new via of one ground net cuts an antipad into the planes of the other ground nets, which can lengthen the paths through them. On a board with more than one ground net, run `returnvia --check` again after the run and the refill.

## Forced vias

`returnvia --force` adds a second pass. Every via or pad that is still without a return gets one at the spot with the most clearance to other-net copper within `--distance`, nearest first among equally clear spots. A forced via never overlaps other-net copper and keeps the hole-to-hole and same-net pad rules. Only the clearance to other-net copper is relaxed, so it may violate clearance.

Forced vias survive the DRC check despite clearance violations and get no marker. The log lists each forced via's net and position, and KiCad's own DRC finds the ones that violate clearance, so you can move them by hand.

A forced via is a placeholder. It needs the ground zone outlines at its spot but may lie outside the fill, so it serves nothing in the run: the run's log and heatmaps still count its signal via or pad as unserved. A later `returnvia --check` counts it like any other ground via once the refilled fill reaches it, clearance violation or not. Other vias of the same ground net within `--distance` of it in a straight line get no forced via of their own. They are reported as unfixable with "relies on forced via at (x, y)" and get a circle marker, because a via placed by hand in place of the forced one may serve them too. After moving a forced via, refill the zones, run `returnvia --check` to confirm its return, and run KiCad's DRC to confirm its clearance.

Via ends outside every ground zone, or with no ground fill near the via, are never forced.

## Stackup assumption

kistitch takes the plane next to a signal layer to be a ground pour. That holds on a SIG / GND / GND / SIG four-layer board. It does not hold on a SIG / GND / PWR / SIG board, where B.Cu's real reference is the power plane on In2:

- With no ground zone on In2, B.Cu ends are measured on In1 instead.
- A board-wide, low-priority GND zone under the power plane on In2 makes In2 the reference, but then B.Cu ends find no ground fill there, and every B.Cu end fails.

## Checking by hand

`returnvia --check` measures the existing vias and places nothing. Combine it with `--render` or `--overlay` to see the result as a heatmap (see [Heatmaps](heatmaps.md)).
