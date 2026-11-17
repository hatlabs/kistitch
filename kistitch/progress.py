"""Progress stages of a run, and stopping it on request before its commit."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum


class Stage(StrEnum):
    REFILL = "refill"
    READ = "read"
    PLAN = "plan"
    DRC = "drc"
    COMMIT = "commit"
    MEASURE = "measure"
    DRAW = "draw"


# Progress within a stage: (k, n), the k-th of n items or rounds.
Count = tuple[int, int]
Reporter = Callable[[Stage, Count | None], None]
StopCheck = Callable[[], bool]


class Cancelled(Exception):
    """The run stopped on request before its commit."""


def _ignore(stage: Stage, count: Count | None) -> None:
    pass


def _never() -> bool:
    return False


@dataclass(frozen=True)
class Progress:
    """Where a run reports its stages and asks whether to stop.

    Every stage up to the commit is entered with `enter`, a stop point. From
    the commit on, stages are only reported: the commit, the refill after it
    and the measurement after a placement run to completion.
    """

    report: Reporter = _ignore
    stop: StopCheck = _never

    def check(self) -> None:
        if self.stop():
            raise Cancelled()

    def enter(self, stage: Stage, count: Count | None = None) -> None:
        self.check()
        self.report(stage, count)

    def without_stop(self) -> Progress:
        return dataclasses.replace(self, stop=_never)


NO_PROGRESS = Progress()
