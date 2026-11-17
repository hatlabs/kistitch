"""Fakes and builders the panel's test modules share."""

from dataclasses import dataclass, field

import kipy.board_types as kbt
from kipy.board_types import Net
from kipy.errors import ApiError
from kipy.proto.common.envelope_pb2 import ApiStatusCode
from kipy.proto.common.types.base_types_pb2 import DocumentSpecifier

BOARD_FILE = "main.kicad_pcb"
PROJECT = "/work/main"


def busy() -> ApiError:
    return ApiError("busy", code=ApiStatusCode.AS_BUSY)


@dataclass
class FakeBoard:
    zones: list[kbt.Zone] = field(default_factory=list)
    nets: list[str] = field(default_factory=list)
    filename: str = BOARD_FILE
    project: str = PROJECT

    @property
    def document(self) -> DocumentSpecifier:
        d = DocumentSpecifier()
        d.board_filename = self.filename
        d.project.path = self.project
        return d

    @property
    def name(self) -> str:
        return self.filename

    def get_zones(self) -> list[kbt.Zone]:
        return self.zones

    def get_nets(self) -> list[Net]:
        out = []
        for n in self.nets:
            net = Net()
            net.name = n
            out.append(net)
        return out


@dataclass
class FakeKiCad:
    board: FakeBoard | None
    error: Exception | None = None

    def get_board(self) -> FakeBoard:
        if self.error is not None:
            raise self.error
        if self.board is None:
            raise ApiError("Expected to be able to retrieve at least one board")
        return self.board
