import kipy.board_types as kbt
import pytest
from kipy.errors import ApiError
from kipy.errors import ConnectionError as KiCadConnectionError
from kipy.proto.board.board_types_pb2 import BoardLayer as BL
from kipy.proto.board.board_types_pb2 import ZoneType
from kipy.proto.common.envelope_pb2 import ApiStatusCode
from panel_fakes import BOARD_FILE, PROJECT, FakeBoard, FakeKiCad, busy

from kistitch.panel.probe import (
    BoardId,
    GroundSource,
    NoBoardError,
    Prober,
    open_board,
)


def copper_zone(net: str, *layers: BL.ValueType) -> kbt.Zone:
    z = kbt.Zone()
    z.proto.type = ZoneType.ZT_COPPER
    z.proto.layers.extend(layers)
    z.proto.copper_settings.net.name = net
    return z


def rule_area(*layers: BL.ValueType) -> kbt.Zone:
    z = kbt.Zone()
    z.proto.type = ZoneType.ZT_RULE_AREA
    z.proto.layers.extend(layers)
    return z


def four_layer_board() -> FakeBoard:
    """GND and GND_ISO planes on both inner layers, +5V poured on both
    outer layers, +3V3 on one layer only, and a rule area."""
    return FakeBoard(
        zones=[
            copper_zone("GND", BL.BL_In1_Cu, BL.BL_In2_Cu),
            copper_zone("GND_ISO", BL.BL_In1_Cu),
            copper_zone("GND_ISO", BL.BL_In2_Cu),
            copper_zone("+5V", BL.BL_F_Cu, BL.BL_B_Cu),
            copper_zone("+3V3", BL.BL_F_Cu),
            rule_area(BL.BL_F_Cu),
        ],
        nets=["GND", "GND_ISO", "+5V", "+3V3", "SIG"],
    )


def prober(*kicads: FakeKiCad) -> Prober:
    made = iter(kicads)
    return Prober(lambda: next(made))


class TestBoardId:
    def test_identity_is_the_full_path_and_the_name_the_file_name(self) -> None:
        probe = prober(FakeKiCad(four_layer_board()))()

        assert probe.board == BoardId(f"{PROJECT}/{BOARD_FILE}", BOARD_FILE)

    def test_identity_without_a_project_path_is_the_name(self) -> None:
        board = FakeBoard(project="")

        assert prober(FakeKiCad(board))().board == BoardId(BOARD_FILE, BOARD_FILE)


class TestNets:
    def test_nets_with_pours_are_the_copper_zones_nets(self) -> None:
        nets = prober(FakeKiCad(four_layer_board()))().nets

        assert nets.zoned == ("+3V3", "+5V", "GND", "GND_ISO")

    def test_a_net_is_stitchable_with_zones_on_two_layers(self) -> None:
        nets = prober(FakeKiCad(four_layer_board()))().nets

        assert [n for n in nets.zoned if nets.stitchable(n)] == [
            "+5V",
            "GND",
            "GND_ISO",
        ]

    def test_ground_nets_are_detected_by_zones_on_two_inner_layers(self) -> None:
        nets = prober(FakeKiCad(four_layer_board()))().nets

        assert nets.detected == (("GND", "GND_ISO"), GroundSource.INNER_ZONES)

    def test_without_inner_planes_the_ground_net_is_gnd(self) -> None:
        board = FakeBoard(
            zones=[copper_zone("GND", BL.BL_F_Cu, BL.BL_B_Cu)], nets=["GND", "SIG"]
        )

        assert prober(FakeKiCad(board))().nets.detected == (
            ("GND",),
            GroundSource.FALLBACK,
        )

    def test_without_inner_planes_or_gnd_no_ground_net_is_found(self) -> None:
        board = FakeBoard(
            zones=[copper_zone("VSS", BL.BL_F_Cu, BL.BL_B_Cu)], nets=["VSS"]
        )

        assert prober(FakeKiCad(board))().nets.detected == ((), GroundSource.NONE)


class TestFailures:
    def test_no_board_open_is_no_board_error(self) -> None:
        with pytest.raises(NoBoardError):
            prober(FakeKiCad(None))()

    def test_busy_is_raised_as_it_is(self) -> None:
        with pytest.raises(ApiError) as e:
            open_board(FakeKiCad(None, busy()))  # type: ignore[arg-type]

        assert e.value.code == ApiStatusCode.AS_BUSY

    def test_a_lost_connection_is_made_again_on_the_next_probe(self) -> None:
        lost = FakeKiCad(None, KiCadConnectionError("gone"))
        probe = prober(lost, FakeKiCad(four_layer_board()))

        with pytest.raises(KiCadConnectionError):
            probe()

        assert probe().board.name == BOARD_FILE

    def test_the_connection_is_kept_between_probes(self) -> None:
        probe = prober(FakeKiCad(four_layer_board()))

        probe()

        assert probe().board.name == BOARD_FILE
