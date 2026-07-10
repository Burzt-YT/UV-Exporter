"""Shared mesh/UV data model used by every format parser."""

from dataclasses import dataclass, field


@dataclass
class UVGroup:
    """One UV-mapped triangle group (typically one material / one mesh piece)."""

    name: str
    uvs: list[tuple[float, float]]
    triangles: list[tuple[int, int, int]]

    @property
    def triangle_count(self) -> int:
        return len(self.triangles)

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """Returns (min_u, min_v, max_u, max_v) across this group's UVs."""
        if not self.uvs:
            return (0.0, 0.0, 1.0, 1.0)
        us = [u for u, _ in self.uvs]
        vs = [v for _, v in self.uvs]
        return (min(us), min(vs), max(us), max(vs))


@dataclass
class UVMesh:
    """A parsed model's full set of UV groups, plus metadata for the UI."""

    source_path: str
    format_name: str
    groups: list[UVGroup] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # UV channel/layer support. Formats with only one UV channel (OBJ, PIM)
    # simply leave these at their defaults; the UI hides the channel picker
    # when there's nothing to pick between.
    available_uv_sets: list[str] = field(default_factory=lambda: ["0"])
    active_uv_set: str = "0"

    @property
    def total_triangles(self) -> int:
        return sum(g.triangle_count for g in self.groups)

    @property
    def is_empty(self) -> bool:
        return len(self.groups) == 0 or self.total_triangles == 0


class MeshParseError(Exception):
    """Raised when a mesh file cannot be parsed into UV data."""
