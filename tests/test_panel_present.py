from types import MappingProxyType

import pytest
from kipy.errors import ApiError
from kipy.errors import ConnectionError as KiCadConnectionError
from panel_fakes import busy, return_result, run_result, stitch_result

from kistitch import drc
from kistitch.clear_command import Cleared
from kistitch.clearvias import RemovableVias
from kistitch.commit import CommitOutcome, CreateItemsError, UnknownNetsError
from kistitch.overlay import OverlayLayersError
from kistitch.panel.present import (
    Icon,
    Kind,
    Step,
    View,
    cancelled,
    cleared_outcome,
    confirmation,
    counted,
    failure,
    heatmap_outcome,
    heatmap_summary,
    nothing_measured,
    placed_outcome,
    removed_outcome,
    source,
)
from kistitch.panel.probe import BoardSwitchedError, NoBoardError
from kistitch.placement import BoardChangedError, SplitError
from kistitch.pourstitch import OtherClassNetsError
from kistitch.returnvia import NoGroundNetsError
from kistitch.snapshot import NoBoardOutlineError


class TestCounted:
    def test_one_is_singular(self) -> None:
        assert counted(1, "via") == "1 via"

    def test_others_are_plural(self) -> None:
        assert counted(3, "pad") == "3 pads"


class TestPadViasPlace:
    def test_placed_with_pads_left(self) -> None:
        s = placed(run_result(placed=35, unfixable=3))

        assert (s.icon, s.lines) == (
            Icon.WARNING,
            ("Placed 35 vias; 3 pads have no room (circled)",),
        )

    def test_placed_all(self) -> None:
        s = placed(run_result(placed=35))

        assert (s.icon, s.lines) == (Icon.DONE, ("Placed 35 vias",))

    def test_nothing_to_place(self) -> None:
        s = placed(run_result())

        assert (s.icon, s.lines) == (
            Icon.DONE,
            ("Nothing to place: every pad has a via",),
        )

    def test_splits_get_their_own_line(self) -> None:
        s = placed(run_result(placed=35, splits=2))

        assert s.icon is Icon.WARNING
        assert s.lines == ("Placed 35 vias", "2 possible pour splits marked (squares)")


def placed(result):
    from kistitch.panel.present import place_summary

    return place_summary(result)


class TestStitchingStatus:
    def test_heatmap_with_copper_beyond_the_distance(self) -> None:
        s = heatmap_summary(stitch_result((112.4, 0.0, 0.0)))

        assert (s.icon, s.lines) == (
            Icon.WARNING,
            ("112.4 mm² beyond 10 mm on 1 of 3 nets",),
        )

    def test_heatmap_counts_the_nets_with_copper_beyond(self) -> None:
        s = heatmap_summary(stitch_result((100.0, 12.4, 0.0)))

        assert s.lines == ("112.4 mm² beyond 10 mm on 2 of 3 nets",)

    def test_heatmap_with_every_point_covered(self) -> None:
        s = heatmap_summary(stitch_result((0.0,), distance=3.0))

        assert (s.icon, s.lines) == (
            Icon.DONE,
            ("Every point within 3 mm of a stitch",),
        )

    def test_heatmap_adds_copper_that_cannot_be_stitched(self) -> None:
        s = heatmap_summary(stitch_result((0.0,), unstitchable=4.1))

        assert s.lines == (
            "Every point within 10 mm of a stitch; 4.1 mm² can't be stitched",
        )

    def test_heatmap_at_a_custom_distance_names_it(self) -> None:
        s = heatmap_summary(stitch_result((5.0,), distance=5.0))

        assert s.lines == ("5.0 mm² beyond 5 mm on 1 of 1 nets",)

    def test_tiny_areas_keep_two_decimals(self) -> None:
        s = heatmap_summary(stitch_result((0.01,)))

        assert s.lines == ("0.01 mm² beyond 10 mm on 1 of 1 nets",)

    def test_place_with_copper_left(self) -> None:
        s = placed(stitch_result((0.8,), placement=run_result(placed=27)))

        assert (s.icon, s.lines) == (
            Icon.WARNING,
            ("Placed 27 vias; 0.8 mm² still beyond 10 mm",),
        )

    def test_place_covering_every_point(self) -> None:
        s = placed(
            stitch_result((0.0,), distance=3.0, placement=run_result(placed=27)),
        )

        assert (s.icon, s.lines) == (
            Icon.DONE,
            ("Placed 27 vias; every point within 3 mm",),
        )

    def test_place_with_splits(self) -> None:
        s = placed(stitch_result((0.0,), placement=run_result(placed=27, splits=1)))

        assert s.icon is Icon.WARNING
        assert s.lines[1] == "1 possible pour split marked (squares)"


class TestReturnViasStatus:
    def test_heatmap_with_unserved_vias(self) -> None:
        s = heatmap_summary(return_result(unserved=14))

        assert (s.icon, s.lines) == (
            Icon.WARNING,
            ("14 of 230 vias unserved within 1.5 mm",),
        )

    def test_heatmap_with_every_via_served(self) -> None:
        s = heatmap_summary(return_result())

        assert (s.icon, s.lines) == (Icon.DONE, ("All 230 vias served within 1.5 mm",))

    def test_place_with_unfixable_and_forced(self) -> None:
        s = placed(
            return_result(
                unserved=5, placement=run_result(placed=61, unfixable=5, forced=2)
            ),
        )

        assert (s.icon, s.lines) == (
            Icon.WARNING,
            ("Placed 61 vias; 5 vias unfixable (circled); 2 forced",),
        )

    def test_place_serving_every_via(self) -> None:
        s = placed(return_result(placement=run_result(placed=61)))

        assert (s.icon, s.lines) == (Icon.DONE, ("Placed 61 vias; every via served",))


class TestSource:
    def test_place(self) -> None:
        assert source(Kind.PLACE, None) == "Place"

    def test_heatmap(self) -> None:
        assert source(Kind.HEATMAP, None) == "Heatmap"

    def test_return_vias_heatmap_names_its_view(self) -> None:
        assert source(Kind.HEATMAP, View.ALL_COPPER) == "Heatmap: all copper"
        assert source(Kind.HEATMAP, View.UNSERVED) == "Heatmap: unserved vias"


class TestOutcomes:
    def test_place_done_with_the_remaining_figure(self) -> None:
        result = stitch_result((0.8,), placement=run_result(placed=27))

        o = placed_outcome(Step.POWER, result, placed(result))

        assert (o.icon, o.headline, o.body) == (
            Icon.DONE,
            "Power stitching: placed 27 vias",
            "Placed 27 vias; 0.8 mm² still beyond 10 mm",
        )

    def test_place_nothing_to_do(self) -> None:
        result = run_result()

        o = placed_outcome(Step.PAD_VIAS, result, placed(result))

        assert (o.icon, o.headline) == (Icon.DONE, "Pad vias: nothing to place")

    def test_place_with_splits_and_no_vias_keeps_the_warning(self) -> None:
        result = run_result(splits=2)

        o = placed_outcome(Step.PAD_VIAS, result, placed(result))

        assert (o.icon, o.headline) == (
            Icon.WARNING,
            "Pad vias: no vias placed; 2 possible pour splits",
        )
        assert "squares on User.Eco2" in o.body

    def test_place_with_splits(self) -> None:
        result = run_result(placed=27, splits=2)

        o = placed_outcome(Step.PAD_VIAS, result, placed(result))

        assert (o.icon, o.headline) == (
            Icon.WARNING,
            "Pad vias: placed 27 vias; 2 possible pour splits",
        )
        assert o.body.startswith("DRC found new unconnected or isolated copper")

    def test_heatmap_placed(self) -> None:
        result = stitch_result((112.4,), enabled=("User.2", "User.3"))

        o = heatmap_outcome(Step.POWER, result, heatmap_summary(result))

        assert (o.icon, o.headline) == (
            Icon.DONE,
            "Power stitching heatmap on User.1–User.4",
        )
        assert o.body == "112.4 mm² beyond 10 mm on 1 of 1 nets\nEnabled User.2, User.3"

    def test_return_vias_all_copper_heatmap_names_its_view(self) -> None:
        result = return_result(full_map=True)

        o = heatmap_outcome(Step.RETURN, result, heatmap_summary(result))

        assert o.headline == "Return vias heatmap (all copper) on User.1–User.4"

    def test_heatmap_with_nothing_to_map(self) -> None:
        result = return_result()

        o = heatmap_outcome(Step.RETURN, result, heatmap_summary(result))

        assert (o.icon, o.headline) == (Icon.DONE, "Return vias: no heatmap needed")

    def test_heatmap_images_kicad_refused(self) -> None:
        result = stitch_result((5.0,), placed_overlays=False)

        o = heatmap_outcome(Step.POWER, result, heatmap_summary(result))

        assert (o.icon, o.headline) == (
            Icon.WARNING,
            "Power stitching heatmap not placed",
        )

    def test_cancelled_place(self) -> None:
        assert cancelled(Kind.PLACE).headline == "Cancelled — no vias committed."

    def test_cancelled_heatmap(self) -> None:
        assert cancelled(Kind.HEATMAP).headline == "Cancelled — no heatmap placed."

    def test_cancelled_count(self) -> None:
        assert cancelled(Kind.COUNT).headline == "Cancelled — no vias removed."

    def test_removed_with_refills(self) -> None:
        o = removed_outcome(
            Cleared(vias=312, commit=CommitOutcome.LANDED, refilled=True)
        )

        assert (o.icon, o.headline, o.body) == (
            Icon.DONE,
            "Removed 312 vias.",
            "Zones refilled.",
        )

    def test_removed_without_a_confirmed_refill(self) -> None:
        o = removed_outcome(Cleared(vias=1, commit=CommitOutcome.LANDED))

        assert (o.headline, o.body) == ("Removed 1 via.", "")

    def test_nothing_measured(self) -> None:
        o = nothing_measured(Step.GROUND)

        assert (o.icon, o.headline, o.body) == (
            Icon.WARNING,
            "Ground stitching: nothing measured",
            (
                "None of the chosen nets has pours on the board now. Choose the "
                "nets again."
            ),
        )

    def test_cleared(self) -> None:
        o = cleared_outcome(Cleared(vias=0, commit=CommitOutcome.LANDED))

        assert (o.icon, o.headline) == (Icon.DONE, "Cleared markers and heatmaps.")

    def test_nothing_to_clear(self) -> None:
        o = cleared_outcome(Cleared(vias=0, commit=CommitOutcome.NOTHING))

        assert (o.icon, o.headline) == (Icon.INFO, "Nothing to clear.")


class TestConfirmation:
    def test_names_the_count_per_net_and_on_the_button(self) -> None:
        vias = RemovableVias(
            nets=None,
            counts=MappingProxyType({"GND": 240, "GND_ISO": 20, "+3V3": 40, "+5V": 12}),
            unfilled_zones={},
            ids=frozenset(str(i) for i in range(312)),
        )

        c = confirmation(vias)

        assert c.title == "Remove stitching vias?"
        assert c.headline == "312 vias will be removed"
        assert c.counts == "GND 240 · GND_ISO 20 · +3V3 40 · +5V 12"
        assert c.button == "Remove 312 vias"
        assert c.unfilled == ()
        assert "including vias you placed by hand" in c.body

    def test_warns_of_unfilled_zones(self) -> None:
        vias = RemovableVias(
            nets=("+5V",),
            counts={"+5V": 1},
            unfilled_zones={"+5V": 2},
            ids=frozenset({"a"}),
        )

        c = confirmation(vias)

        assert c.headline == "1 via will be removed"
        assert c.unfilled == (
            (
                "2 zones of +5V have no fill, so vias touching only them stay. "
                "Refill zones (B) in KiCad first to include them."
            ),
        )


FAILED = "Return vias failed — nothing committed"


class TestFailure:
    @pytest.mark.parametrize(
        ("error", "headline", "body"),
        [
            (
                BoardChangedError("x"),
                FAILED,
                (
                    "The board changed while the run was going. Run it again; it "
                    "plans against the board as it is now."
                ),
            ),
            (
                NoGroundNetsError("x"),
                "Return vias needs ground nets",
                (
                    "No net has zones on two or more inner layers and there is no GND "
                    "net. Choose the ground nets at the top."
                ),
            ),
            (
                NoBoardOutlineError(),
                FAILED,
                "The board has no outline on Edge.Cuts. Draw one, then run again.",
            ),
            (
                drc.KiCadCliNotFound("x"),
                FAILED,
                (
                    "kicad-cli, KiCad's command-line tool, was not found next to the "
                    "running KiCad, on PATH or in the standard install locations."
                ),
            ),
        ],
    )
    def test_design_copy(self, error, headline, body) -> None:
        o = failure(error, Step.RETURN, Kind.PLACE)

        assert (o.headline, o.body) == (headline, body)
        assert not o.unexpected

    def test_overlay_layers(self) -> None:
        o = failure(OverlayLayersError("6 copper layers"), Step.POWER, Kind.HEATMAP)

        assert o.headline == "Power stitching heatmap failed — nothing placed"
        assert "--" not in o.body

    def test_heatmap_failures_say_nothing_placed(self) -> None:
        o = failure(NoBoardOutlineError(), Step.POWER, Kind.HEATMAP)

        assert o.headline == "Power stitching heatmap failed — nothing placed"

    def test_unsaved_board(self) -> None:
        o = failure(
            drc.ProjectFilesError("The board has not been saved"),
            Step.PAD_VIAS,
            Kind.PLACE,
        )

        assert o.headline == "Pad vias failed — nothing committed"
        assert o.body.startswith("DRC reads the board's project files from disk")

    @pytest.mark.parametrize(
        "error",
        [
            SplitError("--no-allow-splits"),
            OtherClassNetsError("--nets lists only power nets; run with --power"),
            drc.DrcError("kicad-cli DRC failed (2)"),
            CreateItemsError("refused"),
            UnknownNetsError("Nets not on the board: VBUS"),
            BoardSwitchedError("b.kicad_pcb"),
            ApiError("refused"),
        ],
    )
    def test_panel_wording_names_no_cli_flag(self, error) -> None:
        o = failure(error, Step.POWER, Kind.PLACE)

        assert "--" not in o.headline + o.body
        assert o.headline
        assert not o.unexpected

    def test_board_switched(self) -> None:
        o = failure(BoardSwitchedError("b"), Step.POWER, Kind.PLACE)

        assert o.headline == "Power stitching didn't run — KiCad shows another board"

    def test_busy(self) -> None:
        o = failure(busy(), Step.POWER, Kind.PLACE)

        assert (o.icon, o.headline, o.body) == (
            Icon.WARNING,
            "KiCad is busy — nothing committed",
            "A dialog or tool is open in the PCB editor. Close it, then try again.",
        )

    def test_lost_connection(self) -> None:
        o = failure(KiCadConnectionError("gone"), Step.POWER, Kind.PLACE)

        assert (o.icon, o.headline, o.body) == (
            Icon.ERROR,
            "Lost the connection to KiCad",
            (
                "If it happened while committing, check KiCad's undo history before "
                "running again."
            ),
        )

    def test_unexpected_error_opens_details_with_the_traceback(self) -> None:
        try:
            raise KeyError("boom")
        except KeyError as e:
            error = e

        o = failure(error, Step.GROUND, Kind.PLACE)

        assert o.headline == "Ground stitching failed — unexpected error"
        assert o.body == (
            "kistitch hit an error it does not expect. Please report it with the "
            "details below."
        )
        assert o.unexpected
        assert "Traceback" in o.detail
        assert "KeyError" in o.detail

    def test_clean_up_failures_name_the_action(self) -> None:
        o = failure(NoBoardOutlineError(), None, Kind.REMOVE)

        assert o.headline == "Remove vias failed — nothing committed"

    def test_a_plain_value_error_naming_gone_nets_is_unexpected(self) -> None:
        o = failure(ValueError("Nets not on the board: VBUS"), Step.POWER, Kind.PLACE)

        assert o.unexpected

    @pytest.mark.parametrize(
        ("error", "kind", "headline"),
        [
            (NoBoardError("x"), Kind.PLACE, "No board open — nothing changed"),
            (
                CreateItemsError("x"),
                Kind.PLACE,
                "Power stitching failed — KiCad refused the change",
            ),
            (
                ApiError("x"),
                Kind.PLACE,
                "Power stitching failed — KiCad refused the change",
            ),
            (SplitError("x"), Kind.PLACE, "Power stitching failed — nothing committed"),
            (
                OtherClassNetsError("x"),
                Kind.PLACE,
                "Power stitching failed — nothing committed",
            ),
            (
                drc.DrcError("x"),
                Kind.PLACE,
                "Power stitching failed — nothing committed",
            ),
            (
                drc.ProjectFilesError("x"),
                Kind.PLACE,
                "Power stitching failed — nothing committed",
            ),
            (
                UnknownNetsError("x"),
                Kind.PLACE,
                "Power stitching failed — nothing committed",
            ),
            (
                OverlayLayersError("x"),
                Kind.HEATMAP,
                "Power stitching heatmap failed — nothing placed",
            ),
        ],
    )
    def test_headlines(self, error, kind, headline) -> None:
        o = failure(error, Step.POWER, kind)

        assert o.headline == headline
        assert not o.unexpected
