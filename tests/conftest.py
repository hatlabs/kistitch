from pathlib import Path

import pytest
from loguru import logger

from kistitch import commit, overlay
from kistitch.progress import Progress, Stage


@pytest.fixture(autouse=True)
def _no_refill_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """KiCad fakes answer at once, so waiting a real poll interval only slows tests."""
    monkeypatch.setattr(commit, "REFILL_POLL_S", 0.0)


@pytest.fixture
def log():
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(str(m)), format="{message}")
    yield lines
    logger.remove(sink)


@pytest.fixture
def png_dir(monkeypatch, tmp_path: Path) -> Path:
    """Where the overlay PNGs are written, in place of a fresh temporary directory."""
    out = tmp_path / "pngs"

    def mkdtemp(**kwargs):
        out.mkdir()
        return str(out)

    monkeypatch.setattr(overlay.tempfile, "mkdtemp", mkdtemp)
    return out


class Recorder:
    """A Progress that records its stages and stop checks in `events`, and
    stops from the first report of `stop_at` on."""

    def __init__(self, events: list | None = None, stop_at: Stage | None = None):
        self.events = [] if events is None else events
        self.stop_at = stop_at
        self.stopped = False
        self.progress = Progress(self._report, self._stop)

    def _report(self, stage, count):
        self.events.append((stage, count))
        if stage == self.stop_at:
            self.stopped = True

    def _stop(self):
        self.events.append("check")
        return self.stopped

    def stages(self) -> list:
        return [
            e for e in self.events if isinstance(e, tuple) and isinstance(e[0], Stage)
        ]


@pytest.fixture
def recorder():
    return Recorder
