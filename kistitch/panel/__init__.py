"""The kistitch panel: kistitch's steps run from a window beside KiCad's
PCB editor.

`controller` holds the panel's state and decides everything the window
shows; it imports no Qt. Work that talks to KiCad runs on worker threads,
each with its own KiCad connection, since kipy's client has no lock: a
run worker runs one action, and a probe (`probe.Prober`) reads which
board KiCad shows and its nets when the window is activated or on Retry.
Every worker callback reaches the controller through a "post to the
controller's thread" callable. `present` words results and errors.
`window` draws the controller and forwards clicks to it, with the
building blocks in `widgets`. `instance` keeps one panel per KiCad
instance: a second start for the same KiCad brings the open panel forward
and exits. Only these three import Qt, which comes with the `gui` extra.
"""
