
from core.mesh_data import MeshParseError, UVGroup, UVMesh

def _fan_triangulate(indices: list[int]) -> list[tuple[int, int, int]]:
    tris = []
    for i in range(1, len(indices) - 1):
        tris.append((indices[0], indices[i], indices[i + 1]))
    return tris

def parse_obj(path: str) -> UVMesh:
    mesh = UVMesh(source_path=path, format_name="OBJ")

    all_uvs: list[tuple[float, float]] = []
    group_tris: dict[str, list[tuple[int, int, int]]] = {}
    group_order: list[str] = []

    current_group = "default"
    current_material = None
    faces_without_uv = 0
    total_faces = 0

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line_no, raw_line in enumerate(f, start=1):
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue

                parts = line.split()
                tag = parts[0]

                if tag == "vt":
                    if len(parts) < 3:
                        continue
                    try:
                        u = float(parts[1])
                        v = float(parts[2])
                    except ValueError:
                        continue
                    all_uvs.append((u, v))

                elif tag == "usemtl":
                    current_material = parts[1] if len(parts) > 1 else None
                    key = current_material or current_group
                    if key not in group_tris:
                        group_tris[key] = []
                        group_order.append(key)
                    current_group = key

                elif tag in ("o", "g"):
                    name = parts[1] if len(parts) > 1 else "default"
                    if current_material is None:
                        current_group = name
                        if current_group not in group_tris:
                            group_tris[current_group] = []
                            group_order.append(current_group)

                elif tag == "f":
                    total_faces += 1
                    uv_indices = []
                    valid = True
                    for vertex_ref in parts[1:]:
                        segs = vertex_ref.split("/")
                        if len(segs) < 2 or segs[1] == "":
                            valid = False
                            continue
                        try:
                            vt_idx = int(segs[1])
                        except ValueError:
                            valid = False
                            continue
                        if vt_idx < 0:
                            vt_idx = len(all_uvs) + vt_idx + 1
                        uv_indices.append(vt_idx - 1)

                    if not valid or len(uv_indices) < 3:
                        faces_without_uv += 1
                        continue

                    key = current_material or current_group
                    if key not in group_tris:
                        group_tris[key] = []
                        group_order.append(key)

                    group_tris[key].extend(_fan_triangulate(uv_indices))

    except OSError as e:
        raise MeshParseError(f"Could not read OBJ file: {e}") from e

    if not all_uvs:
        raise MeshParseError(
            "No texture coordinates (vt) found in this OBJ file. "
            "The mesh may not be UV-unwrapped, or UVs were stripped on export."
        )

    for key in group_order:
        tris = group_tris[key]
        if not tris:
            continue
        used_indices = sorted({idx for tri in tris for idx in tri})
        remap = {old: new for new, old in enumerate(used_indices)}
        try:
            group_uvs = [all_uvs[i] for i in used_indices]
        except IndexError:
            mesh.warnings.append(
                f"Group '{key}' referenced a UV index out of range; skipped some faces."
            )
            continue
        remapped_tris = [(remap[a], remap[b], remap[c]) for a, b, c in tris]
        mesh.groups.append(UVGroup(name=key, uvs=group_uvs, triangles=remapped_tris))

    if faces_without_uv:
        mesh.warnings.append(
            f"{faces_without_uv} of {total_faces} face(s) had no UV coordinates and were skipped."
        )

    if mesh.is_empty:
        raise MeshParseError(
            "Parsed the OBJ file but found no valid UV-mapped triangles to export."
        )

    return mesh
