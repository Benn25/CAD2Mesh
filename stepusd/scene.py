"""Flatten the assembly tree into placed occurrences with world transforms."""
from __future__ import annotations
from dataclasses import dataclass

from OCP.gp import gp_Trsf


@dataclass
class Occurrence:
    """One placed instance of a prototype, with its world transform."""
    name: str            # display name
    path: str            # full assembly path, e.g. "Top/BracketAsm/Screw"
    proto_key: str
    world: gp_Trsf
    depth: int


def flatten(doc) -> list[Occurrence]:
    out: list[Occurrence] = []

    def walk(node, parent_trsf: gp_Trsf, path: str, depth: int):
        world = parent_trsf.Multiplied(node.matrix)   # returns a new gp_Trsf
        here = f"{path}/{node.name}" if path else node.name
        if node.proto is not None:
            out.append(Occurrence(name=node.name, path=here,
                                  proto_key=node.proto.key, world=world,
                                  depth=depth))
        for ch in node.children:
            walk(ch, world, here, depth + 1)

    for r in doc.roots:
        walk(r, gp_Trsf(), "", 0)
    return out


def to_matrix16(t: gp_Trsf) -> list:
    """gp_Trsf -> row-major 4x4 as VTK expects (translation in last COLUMN)."""
    return [t.Value(1, 1), t.Value(1, 2), t.Value(1, 3), t.Value(1, 4),
            t.Value(2, 1), t.Value(2, 2), t.Value(2, 3), t.Value(2, 4),
            t.Value(3, 1), t.Value(3, 2), t.Value(3, 3), t.Value(3, 4),
            0.0, 0.0, 0.0, 1.0]
