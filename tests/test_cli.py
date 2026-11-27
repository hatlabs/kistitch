import argparse
import re
import subprocess
import sys
from importlib import metadata

import kipy
import pytest
from kipy.errors import ApiError
from kipy.proto.common.envelope_pb2 import ApiStatusCode
from loguru import logger

from kistitch import (
    cli,
    overlay,
    placement,
    pourstitch,
    pourstitch_command,
    returnvia_command,
)
from kistitch.cli import parse_via_size
from kistitch.commit import MarkerLayerError
from kistitch.plan import (
    POURSTITCH_GROUND_DISTANCE,
    POURSTITCH_POWER_DISTANCE,
    StitchClass,
)
from kistitch.pourstitch import OtherClassNetsError
from kistitch.returnpath import NoBoardOutlineError
from kistitch.returnvia import NoGroundNetsError, ReturnViaOptions
from kistitch.snapshot import ViaSize


def test_via_size_parses_diameter_and_drill():
    assert parse_via_size("0.5/0.3") == ViaSize(0.5, 0.3)


@pytest.mark.parametrize("text", ["0.5", "0.6/0.3/0.1", "a/b", "0.3/0.5", "0.5/0"])
def test_bad_via_size_is_rejected(text):
    with pytest.raises(argparse.ArgumentTypeError):
        parse_via_size(text)


SPLIT_FLAGS = [([], True), (["--allow-splits"], True), (["--no-allow-splits"], False)]


@pytest.mark.parametrize(
    "argv",
    [
        ["returnvia"],
        ["padvia", "--nets", "GND"],
        ["pourstitch", "--nets", "GND"],
    ],
)
@pytest.mark.parametrize("flag, allow", SPLIT_FLAGS)
def test_placement_commands_allow_splits_unless_told_not_to(
    monkeypatch, argv, flag, allow
):
    monkeypatch.setattr(sys, "argv", ["kistitch", *argv, *flag])
    assert cli.parse_args().allow_splits is allow


@pytest.mark.parametrize(
    "error",
    [
        NoBoardOutlineError("no outline"),
        placement.SplitError("split"),
        MarkerLayerError("Eco2"),
    ],
)
def test_padvia_failure_exits_cleanly(monkeypatch, error):
    def fail(planner, options, settings):
        raise error

    monkeypatch.setattr(cli, "run_placement", fail)
    args = argparse.Namespace(
        nets=["GND"],
        via=None,
        dry_run=False,
        kicad_cli=None,
        allow_splits=False,
        refill=True,
    )

    with pytest.raises(SystemExit) as exit_info:
        cli.execute_padvia(args)

    assert exit_info.value.code == 1


def parsed(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["kistitch", *argv])
    return cli.parse_args()


def test_cli_offers_the_via_commands_and_clear(monkeypatch, capsys):
    with pytest.raises(SystemExit):
        parsed(monkeypatch, ["--help"])

    choices = re.findall(r"\{([a-z,]+)\}", capsys.readouterr().out)[0]
    assert set(choices.split(",")) == {"returnvia", "pourstitch", "padvia", "clear"}


def test_cut_is_not_a_command(monkeypatch):
    with pytest.raises(SystemExit) as exit_info:
        parsed(monkeypatch, ["cut"])

    assert exit_info.value.code == 2


@pytest.mark.parametrize("flag, allow", SPLIT_FLAGS)
def test_allow_splits_reaches_the_run_settings(monkeypatch, flag, allow):
    seen: list[placement.RunSettings] = []
    monkeypatch.setattr(
        cli, "run_placement", lambda planner, options, settings: seen.append(settings)
    )

    cli.execute_padvia(parsed(monkeypatch, ["padvia", "--nets", "GND", *flag]))

    assert [s.allow_splits for s in seen] == [allow]


class Captured(Exception):
    pass


@pytest.mark.parametrize(
    "argv, module",
    [
        (["pourstitch", "--nets", "GND"], pourstitch_command),
        (["returnvia"], returnvia_command),
    ],
)
@pytest.mark.parametrize("flag, allow", SPLIT_FLAGS)
def test_heatmap_command_allow_splits_reaches_the_run_settings(
    monkeypatch, argv, module, flag, allow
):
    seen = capture_run_settings(monkeypatch, module)

    with pytest.raises(Captured):
        getattr(cli, f"execute_{argv[0]}")(parsed(monkeypatch, [*argv, *flag]))

    assert [s.allow_splits for s in seen] == [allow]


@pytest.mark.parametrize(
    "module, options",
    [
        (pourstitch_command, pourstitch.Options()),
        (returnvia_command, ReturnViaOptions()),
    ],
)
def test_heatmap_command_allows_splits_by_default(monkeypatch, module, options):
    seen = capture_run_settings(monkeypatch, module)

    with pytest.raises(Captured):
        module.execute(options, overlay.Outputs())

    assert [s.allow_splits for s in seen] == [True]


def capture_run_settings(monkeypatch, module) -> list[placement.RunSettings]:
    """Record the settings `module` runs placement with, stopping the run there."""
    seen: list[placement.RunSettings] = []

    def capture(planner, options, settings, kicad, *, progress):
        seen.append(settings)
        raise Captured

    monkeypatch.setattr(module, "run_placement", capture)
    monkeypatch.setattr(
        module.kipy,
        "KiCad",
        lambda: type("K", (), {"get_board": lambda self: None})(),
    )
    monkeypatch.setattr(module, "check_outline", lambda board: None)
    return seen


def test_returnvia_takes_the_heatmap_options(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(
        returnvia_command,
        "execute",
        lambda options, outputs, **kw: seen.append((options, outputs, kw)),
    )
    argv = [
        "returnvia",
        "--check",
        "--render",
        str(tmp_path / "heat.png"),
        "--overlay",
        "--overlay-layers",
        "User.5",
        "User.6",
        "User.7",
        "User.8",
        "--distance",
        "2",
        "--full-map",
    ]

    cli.execute_returnvia(parsed(monkeypatch, argv))

    ((options, outputs, kw),) = seen
    assert options.distance == 2
    assert outputs == overlay.Outputs(
        render=tmp_path / "heat.png",
        overlay=True,
        overlay_layers=("User.5", "User.6", "User.7", "User.8"),
    )
    assert kw["check"]
    assert kw["full_map"]


@pytest.mark.parametrize("argv", [["returnvia"], ["pourstitch", "--nets", "GND"]])
def test_overlay_takes_no_directory(monkeypatch, argv):
    assert parsed(monkeypatch, [*argv, "--overlay"]).overlay is True
    with pytest.raises(SystemExit):
        parsed(monkeypatch, [*argv, "--overlay", "out"])


@pytest.mark.parametrize(
    "argv, dry_run, kicad_cli",
    [
        (["returnvia"], False, None),
        (
            ["--dry-run", "returnvia", "--kicad-cli", "/opt/kicad-cli"],
            True,
            "/opt/kicad-cli",
        ),
    ],
)
def test_returnvia_passes_dry_run_and_kicad_cli_through(
    monkeypatch, argv, dry_run, kicad_cli
):
    seen = []
    monkeypatch.setattr(
        returnvia_command, "execute", lambda options, outputs, **kw: seen.append(kw)
    )

    cli.execute_returnvia(parsed(monkeypatch, argv))

    (kw,) = seen
    assert kw["dry_run"] is dry_run
    assert kw["kicad_cli"] == kicad_cli


@pytest.mark.parametrize(
    "error",
    [
        NoBoardOutlineError("no outline"),
        NoGroundNetsError("no ground"),
        overlay.OverlayLayersError("layers"),
        placement.SplitError("split"),
        MarkerLayerError("Eco2"),
    ],
)
def test_returnvia_failure_exits_cleanly(monkeypatch, error):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(returnvia_command, "execute", fail)

    with pytest.raises(SystemExit) as exit_info:
        cli.execute_returnvia(parsed(monkeypatch, ["returnvia", "--check"]))

    assert exit_info.value.code == 1


def pourstitch_options(monkeypatch, argv):
    seen = []
    monkeypatch.setattr(
        pourstitch_command, "execute", lambda options, *a, **kw: seen.append(options)
    )
    cli.execute_pourstitch(parsed(monkeypatch, argv))
    (options,) = seen
    return options


def test_pourstitch_without_nets_leaves_net_selection_unset(monkeypatch):
    assert pourstitch_options(monkeypatch, ["pourstitch"]).nets is None


@pytest.mark.parametrize(
    ("argv", "stitch_class", "distance"),
    [
        ([], StitchClass.GROUND, POURSTITCH_GROUND_DISTANCE),
        (["--power"], StitchClass.POWER, POURSTITCH_POWER_DISTANCE),
        (["--distance", "4"], StitchClass.GROUND, 4.0),
        (["--power", "--distance", "3"], StitchClass.POWER, 3.0),
    ],
)
def test_pourstitch_distance_defaults_to_the_class_distance(
    monkeypatch, argv, stitch_class, distance
):
    options = pourstitch_options(monkeypatch, ["pourstitch", *argv])

    assert options.stitch_class is stitch_class
    assert options.max_distance == distance


def test_pourstitch_takes_ground_nets(monkeypatch):
    options = pourstitch_options(
        monkeypatch, ["pourstitch", "--ground-nets", "GND", "AGND"]
    )

    assert options.ground_nets == ("GND", "AGND")
    assert pourstitch_options(monkeypatch, ["pourstitch"]).ground_nets is None


@pytest.mark.parametrize(
    "error",
    [
        placement.SplitError("split"),
        placement.BoardChangedError("changed"),
        NoGroundNetsError("no ground"),
        OtherClassNetsError("other class"),
        NoBoardOutlineError("no outline"),
        MarkerLayerError("Eco2"),
    ],
)
def test_pourstitch_placement_failure_exits_cleanly(monkeypatch, error):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(pourstitch_command, "execute", fail)

    with pytest.raises(SystemExit) as exit_info:
        cli.execute_pourstitch(parsed(monkeypatch, ["pourstitch", "--nets", "GND"]))

    assert exit_info.value.code == 1
    assert exit_info.value.__cause__ is None
    assert exit_info.value.__suppress_context__


@pytest.fixture
def restore_default_sink():
    yield
    logger.remove()
    logger.add(sys.stderr)


@pytest.mark.usefixtures("restore_default_sink")
def test_cli_logs_info_by_default(monkeypatch, capsys):
    monkeypatch.delenv("LOGURU_LEVEL", raising=False)

    cli.configure_logging()
    logger.debug("per-via detail")
    logger.info("summary")

    err = capsys.readouterr().err
    assert "summary" in err
    assert "per-via detail" not in err


@pytest.mark.usefixtures("restore_default_sink")
def test_loguru_level_shows_debug(monkeypatch, capsys):
    monkeypatch.setenv("LOGURU_LEVEL", "DEBUG")

    cli.configure_logging()
    logger.debug("per-via detail")

    assert "per-via detail" in capsys.readouterr().err


@pytest.mark.usefixtures("restore_default_sink")
def test_main_logs_info_by_default(monkeypatch, capsys):
    monkeypatch.delenv("LOGURU_LEVEL", raising=False)
    monkeypatch.setattr(cli, "parse_args", lambda: argparse.Namespace(command=None))
    logger.remove()
    logger.add(sys.stderr)

    cli.main()
    logger.debug("per-via detail")

    assert "per-via detail" not in capsys.readouterr().err


def test_importing_the_cli_leaves_scipy_skimage_and_matplotlib_unloaded():
    # A subprocess, because this test session has already imported them.
    code = (
        "import sys, kistitch.cli; "
        "print([m for m in ('scipy', 'skimage', 'matplotlib') if m in sys.modules])"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )

    assert result.stdout.strip() == "[]"


class BusyBoard:
    def __getattr__(self, name):
        def busy(*args, **kwargs):
            raise ApiError("busy", code=ApiStatusCode.AS_BUSY)

        return busy


class BusyKiCad:
    def get_board(self):
        return BusyBoard()


@pytest.mark.usefixtures("restore_default_sink")
@pytest.mark.parametrize(
    "argv",
    [
        ["padvia"],
        ["pourstitch"],
        ["returnvia"],
        ["clear"],
    ],
)
def test_busy_kicad_exits_without_a_traceback(monkeypatch, capsys, argv):
    monkeypatch.setattr(kipy, "KiCad", BusyKiCad)
    monkeypatch.setattr(sys, "argv", ["kistitch", *argv])

    with pytest.raises(SystemExit) as exit_info:
        cli.main()

    assert exit_info.value.code == 1
    err = capsys.readouterr().err
    assert "KiCad is busy" in err
    assert "Traceback" not in err


@pytest.mark.usefixtures("restore_default_sink")
def test_other_kicad_errors_still_raise(monkeypatch):
    def refuse(*args, **kwargs):
        raise ApiError("bad request")

    monkeypatch.setattr(cli, "run_placement", refuse)
    monkeypatch.setattr(sys, "argv", ["kistitch", "padvia"])

    with pytest.raises(ApiError):
        cli.main()


# ---- --refill / --no-refill ------------------------------------------------------


@pytest.mark.parametrize(
    "flags, refill", [([], True), (["--refill"], True), (["--no-refill"], False)]
)
@pytest.mark.parametrize(
    "argv", [["padvia"], ["pourstitch"], ["returnvia"], ["clear", "--vias"]]
)
def test_refill_is_a_global_option_on_by_default(monkeypatch, flags, refill, argv):
    assert parsed(monkeypatch, [*flags, *argv]).refill is refill


def test_refill_help_names_what_it_refills_and_the_undo_steps(monkeypatch, capsys):
    with pytest.raises(SystemExit):
        parsed(monkeypatch, ["--help"])

    out = " ".join(capsys.readouterr().out.split())
    assert "--refill, --no-refill" in out
    assert "after clear --vias" in out
    assert "undo step" in out


@pytest.mark.parametrize("flags, refill", [([], True), (["--no-refill"], False)])
def test_refill_reaches_the_padvia_run_settings(monkeypatch, flags, refill):
    seen: list[placement.RunSettings] = []
    monkeypatch.setattr(
        cli, "run_placement", lambda planner, options, settings: seen.append(settings)
    )

    cli.execute_padvia(parsed(monkeypatch, [*flags, "padvia", "--nets", "GND"]))

    assert [s.refill for s in seen] == [refill]


def no_board_kicad(monkeypatch, module):
    monkeypatch.setattr(
        module.kipy,
        "KiCad",
        lambda: type("K", (), {"get_board": lambda self: None})(),
    )
    monkeypatch.setattr(module, "check_outline", lambda board: None)


HEATMAP_COMMANDS = [
    (["pourstitch", "--nets", "GND"], pourstitch_command),
    (["returnvia"], returnvia_command),
]


@pytest.mark.parametrize("argv, module", HEATMAP_COMMANDS)
@pytest.mark.parametrize("flags, refill", [([], True), (["--no-refill"], False)])
def test_refill_reaches_the_heatmap_command_run_settings(
    monkeypatch, argv, module, flags, refill
):
    seen: list[placement.RunSettings] = []

    def capture(planner, options, settings, kicad, *, progress):
        seen.append(settings)
        raise Captured

    monkeypatch.setattr(module, "run_placement", capture)
    no_board_kicad(monkeypatch, module)

    with pytest.raises(Captured):
        getattr(cli, f"execute_{argv[0]}")(parsed(monkeypatch, [*flags, *argv]))

    assert [s.refill for s in seen] == [refill]


@pytest.mark.parametrize("argv, module", HEATMAP_COMMANDS)
@pytest.mark.parametrize("flags, refill", [([], True), (["--no-refill"], False)])
def test_refill_reaches_the_check_measurement(monkeypatch, argv, module, flags, refill):
    seen: list[bool] = []

    def capture(kicad, board, refill, progress, *, dry_run):
        seen.append(refill)
        raise Captured

    monkeypatch.setattr(module, "measure_only", capture)
    no_board_kicad(monkeypatch, module)

    with pytest.raises(Captured):
        getattr(cli, f"execute_{argv[0]}")(
            parsed(monkeypatch, [*flags, *argv, "--check"])
        )

    assert seen == [refill]


def run_panel_without(module: str) -> subprocess.CompletedProcess[str]:
    """Run `kistitch-panel` with `module` failing to import."""
    code = (
        "import sys\n"
        f"sys.modules[{module!r}] = None\n"
        "from kistitch.cli import panel_main\n"
        "panel_main([])\n"
    )
    return subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )


def test_panel_without_the_gui_extra_prints_the_install_hint():
    ran = run_panel_without("PySide6")

    # Distinct from a failure (1) and a usage error (2), so that the KiCad
    # plugin's launcher can offer to add the extra.
    assert ran.returncode == cli.GUI_EXTRA_EXIT == 3
    assert "uv tool install --python 3.14 'kistitch[gui]'" in ran.stderr


def test_panel_reports_another_import_error_as_it_is():
    ran = run_panel_without("kistitch.panel.window")

    assert ran.returncode != 0
    assert "uv tool install" not in ran.stderr
    assert "ModuleNotFoundError" in ran.stderr


def test_panel_is_installed_as_kistitch_panel():
    (script,) = metadata.entry_points(group="console_scripts", name="kistitch-panel")
    assert script.value == "kistitch.cli:panel_main"


def test_kistitch_prints_its_version(monkeypatch, capsys):
    with pytest.raises(SystemExit) as raised:
        parsed(monkeypatch, ["--version"])

    assert raised.value.code == 0
    assert capsys.readouterr().out == f"kistitch {metadata.version('kistitch')}\n"


def test_panel_prints_its_version_without_pyside6():
    code = (
        "import sys\n"
        "sys.modules['PySide6'] = None\n"
        "import kistitch.cli\n"
        "try:\n"
        "    kistitch.cli.panel_main(['--version'])\n"
        "finally:\n"
        "    loaded = [m for m, v in sys.modules.items() if v is not None]\n"
        "    print(any(m.startswith(('PySide6', 'kistitch.panel')) "
        "for m in loaded), file=sys.stderr)\n"
    )
    ran = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )

    assert (ran.returncode, ran.stdout, ran.stderr) == (
        0,
        f"kistitch {metadata.version('kistitch')}\n",
        "False\n",
    )
