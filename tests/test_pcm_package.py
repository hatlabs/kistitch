import hashlib
import importlib.util
import json
import shutil
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, TypeAlias, cast

import pytest

ROOT = Path(__file__).parent.parent

if TYPE_CHECKING:
    import pcm_package
else:
    _spec = importlib.util.spec_from_file_location(
        "pcm_package", ROOT / "scripts" / "pcm_package.py"
    )
    assert _spec is not None and _spec.loader is not None
    pcm_package = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = pcm_package
    _spec.loader.exec_module(pcm_package)

VERSION = (ROOT / "VERSION").read_text().strip()
TAG = f"v{VERSION}+3"
RELEASE = f"https://github.com/hatlabs/kistitch/releases/download/v{VERSION}%2B3"
NOW = datetime(2026, 11, 17, 12, 30, 5, tzinfo=UTC)

Json: TypeAlias = dict[str, object]


def _json(data: bytes) -> Json:
    return cast(Json, json.loads(data))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _only(items: object) -> Json:
    entries = cast(list[Json], items)
    assert len(entries) == 1
    return entries[0]


def _plugin_files() -> set[str]:
    return {
        f"plugins/{path.name}"
        for path in (ROOT / "pcm" / "plugins").iterdir()
        if path.is_file() and path.suffix != ".pyc"
    }


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> pcm_package.Built:
    # Stray bytecode in the source must not reach the package.
    source = tmp_path_factory.mktemp("source") / "pcm"
    shutil.copytree(
        ROOT / "pcm", source, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    cache = source / "plugins" / "__pycache__"
    cache.mkdir()
    (cache / "kistitch_launcher.cpython-39.pyc").write_bytes(b"\0")
    (source / "plugins" / "kistitch_launcher.pyc").write_bytes(b"\0")
    out = tmp_path_factory.mktemp("out")
    return pcm_package.build(source, VERSION, TAG, out, NOW)


def test_archive_holds_metadata_plugin_and_icon(built: pcm_package.Built) -> None:
    with zipfile.ZipFile(built.package) as archive:
        names = set(archive.namelist())
    plugins = _plugin_files()
    assert {"plugins/plugin.json", "plugins/kistitch_launcher.py"} <= plugins
    assert names == {"metadata.json", "resources/icon.png", *plugins}


def test_archive_metadata_has_the_version_without_download_fields(
    built: pcm_package.Built,
) -> None:
    with zipfile.ZipFile(built.package) as archive:
        metadata = _json(archive.read("metadata.json"))
    assert metadata["identifier"] == pcm_package.IDENTIFIER
    assert _only(metadata["versions"]) == {
        "version": VERSION,
        "status": "stable",
        "kicad_version": "10.0",
        "runtime": "ipc",
        "platforms": ["macos", "linux", "windows"],
    }


def test_identifier_matches_the_plugin_and_its_metadata() -> None:
    plugin = _json((ROOT / "pcm" / "plugins" / "plugin.json").read_bytes())
    metadata = _json((ROOT / "pcm" / "metadata.json").read_bytes())
    assert plugin["identifier"] == metadata["identifier"] == pcm_package.IDENTIFIER


def test_packages_json_describes_the_archive(built: pcm_package.Built) -> None:
    packages = _json(built.packages.read_bytes())
    package = _only(packages["packages"])
    version = _only(package["versions"])
    with zipfile.ZipFile(built.package) as archive:
        install_size = sum(info.file_size for info in archive.infolist())
        inner = _json(archive.read("metadata.json"))
    assert built.package.name == f"kistitch-pcm-{VERSION}.zip"
    assert version["download_url"] == f"{RELEASE}/{built.package.name}"
    assert version["download_sha256"] == _sha256(built.package)
    assert version["download_size"] == built.package.stat().st_size
    assert version["install_size"] == install_size
    assert {key: value for key, value in package.items() if key != "versions"} == {
        key: value for key, value in inner.items() if key != "versions"
    }


def test_resources_archive_holds_the_icon_under_the_identifier(
    built: pcm_package.Built,
) -> None:
    with zipfile.ZipFile(built.resources) as archive:
        assert archive.namelist() == [f"{pcm_package.IDENTIFIER}/icon.png"]
        icon = archive.read(f"{pcm_package.IDENTIFIER}/icon.png")
    assert icon == (ROOT / "pcm" / "resources" / "icon.png").read_bytes()


def test_repository_json_points_at_the_stable_release(
    built: pcm_package.Built,
) -> None:
    repository = _json(built.repository.read_bytes())
    stamp = {
        "update_timestamp": int(NOW.timestamp()),
        "update_time_utc": "2026-11-17 12:30:05",
    }
    assert repository["schema_version"] == 2
    assert repository["packages"] == {
        "url": f"{RELEASE}/packages.json",
        "sha256": _sha256(built.packages),
        **stamp,
    }
    assert repository["resources"] == {
        "url": f"{RELEASE}/{built.resources.name}",
        "sha256": _sha256(built.resources),
        **stamp,
    }


def test_archives_depend_only_on_their_contents(
    built: pcm_package.Built, tmp_path: Path
) -> None:
    again = pcm_package.build(ROOT / "pcm", VERSION, TAG, tmp_path, NOW)
    assert again.package.read_bytes() == built.package.read_bytes()
    assert again.resources.read_bytes() == built.resources.read_bytes()


@pytest.mark.parametrize(
    "tag",
    [
        "v0.0.1+1",  # another version
        f"v{VERSION}+1_pre",  # the pre-release
        f"v{VERSION}",  # no build number
        f"{VERSION}+1",  # no v
    ],
)
def test_main_refuses_a_tag_that_is_not_this_versions_stable_tag(
    tag: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert pcm_package.main([tag, "--out", str(tmp_path)]) != 0
    assert tag in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_main_writes_the_four_release_files(tmp_path: Path) -> None:
    assert pcm_package.main([TAG, "--out", str(tmp_path)]) == 0
    assert {path.name for path in tmp_path.iterdir()} == {
        f"kistitch-pcm-{VERSION}.zip",
        "packages.json",
        "kistitch-pcm-resources.zip",
        "repository.json",
    }
