
import struct

from core.mesh_data import MeshParseError, UVGroup, UVMesh

try:
    import msgpack
except ImportError:
    msgpack = None

try:
    import zstandard
except ImportError:
    zstandard = None

CDAE_VERSION_MASK = 0xFF
CDAE_SUPPORTED_VERSION = 31

_PRIM_TYPE_MASK = 0b11 << 30
_PRIM_TYPE_TRIANGLES = 0 << 30
_PRIM_TYPE_STRIP = 1 << 30
_PRIM_TYPE_FAN = 2 << 30
_PRIM_INDEXED = 1 << 29
_PRIM_NO_MATERIAL = 1 << 28
_PRIM_MATERIAL_MASK = ~(_PRIM_TYPE_MASK | _PRIM_INDEXED | _PRIM_NO_MATERIAL) & 0xFFFFFFFF

MESH_TYPE_STANDARD = 0
MESH_TYPE_SKIN = 1
MESH_TYPE_DECAL = 2
MESH_TYPE_SORTED = 3
MESH_TYPE_NULL = 4

class _Cursor:

    def __init__(self, unpacker: "msgpack.Unpacker"):
        self.unpacker = unpacker

    def obj(self):
        try:
            return next(self.unpacker)
        except StopIteration as e:
            raise MeshParseError(
                "Reached the end of the .cdae body while still expecting more "
                "data; the file may be truncated or use a layout this parser "
                "doesn't recognize."
            ) from e

def _read_pack_vector(cursor: _Cursor) -> tuple[int, int, bytes]:
    length = cursor.obj()
    elem_size = cursor.obj()
    if not isinstance(length, int) or not isinstance(elem_size, int):
        raise MeshParseError(
            "Malformed pack_vector header in .cdae body (expected two integers)."
        )
    if length < 0 or elem_size < 0:
        raise MeshParseError("Malformed pack_vector header in .cdae body (negative size).")
    nbytes = length * elem_size
    data = cursor.obj()
    if not isinstance(data, (bytes, bytearray)):
        raise MeshParseError(
            "Malformed pack_vector payload in .cdae body (expected binary data)."
        )
    if len(data) < nbytes:
        raise MeshParseError(
            "pack_vector payload shorter than declared length in .cdae body."
        )
    return length, elem_size, bytes(data[:nbytes])

def _floats_from(data: bytes, count: int) -> list[float]:
    if count == 0:
        return []
    expected = count * 4
    if len(data) < expected:
        raise MeshParseError("Truncated float array while reading .cdae mesh data.")
    return list(struct.unpack(f"<{count}f", data[:expected]))

def _skip_pack_vector(cursor: _Cursor) -> None:
    _read_pack_vector(cursor)

def _read_shape_vectors(cursor: _Cursor) -> None:
    names = [
        "nodes", "objects", "subShapeFirstNode", "subShapeFirstObject",
        "subShapeNumNodes", "subShapeNumObjects", "defaultRotations",
        "defaultTranslations", "nodeRotations", "nodeTranslations",
        "nodeUniformScales", "nodeAlignedScales", "nodeArbitraryScaleFactors",
        "nodeArbitraryScaleRots", "groundTranslations", "groundRotations",
        "objectStates", "triggers", "details",
    ]
    for _name in names:
        _skip_pack_vector(cursor)

def _read_names_table(cursor: _Cursor) -> list[str]:
    count = cursor.obj()
    if not isinstance(count, int) or count < 0:
        raise MeshParseError("Malformed shape-names table in .cdae body.")
    names = []
    for _ in range(count):
        name = cursor.obj()
        names.append(name.decode("utf-8", errors="replace") if isinstance(name, bytes) else str(name))
    return names

def _read_mesh(cursor: _Cursor, mesh_index: int) -> dict | None:
    mesh_type = cursor.obj()
    if not isinstance(mesh_type, int):
        raise MeshParseError(f"Malformed mesh type for mesh #{mesh_index} in .cdae body.")

    if mesh_type == MESH_TYPE_NULL:
        return None

    _num_frames = cursor.obj()
    _num_mat_frames = cursor.obj()
    parent_mesh = cursor.obj()
    for _ in range(6 + 3 + 1):
        cursor.obj()

    _len_v, _sz_v, verts_raw = _read_pack_vector(cursor)
    _len_tv, _sz_tv, tverts_raw = _read_pack_vector(cursor)
    _len_tv2, _sz_tv2, tverts2_raw = _read_pack_vector(cursor)
    _skip_pack_vector(cursor)
    _skip_pack_vector(cursor)
    _skip_pack_vector(cursor)
    _len_p, _sz_p, primitives_raw = _read_pack_vector(cursor)
    _len_i, _sz_i, indices_raw = _read_pack_vector(cursor)
    _skip_pack_vector(cursor)

    _verts_per_frame = cursor.obj()
    _mesh_flags = cursor.obj()

    if mesh_type == MESH_TYPE_SKIN:
        is_parent = isinstance(parent_mesh, int) and parent_mesh < 0
        if is_parent:
            _skip_pack_vector(cursor)
            _skip_pack_vector(cursor)
        _skip_pack_vector(cursor)
        if is_parent:
            _skip_pack_vector(cursor)
            _skip_pack_vector(cursor)
            _skip_pack_vector(cursor)
            _skip_pack_vector(cursor)

    if mesh_type not in (MESH_TYPE_STANDARD, MESH_TYPE_SKIN):
        return None

    vert_elem_size = 12
    num_verts = len(verts_raw) // vert_elem_size if vert_elem_size else 0

    tvert_elem_size = 8
    num_tverts = len(tverts_raw) // tvert_elem_size if tvert_elem_size else 0
    tvert_flat = _floats_from(tverts_raw, num_tverts * 2)
    uvs = [(tvert_flat[i * 2], tvert_flat[i * 2 + 1]) for i in range(num_tverts)]

    num_tverts2 = len(tverts2_raw) // tvert_elem_size if tvert_elem_size else 0
    tvert2_flat = _floats_from(tverts2_raw, num_tverts2 * 2)
    uvs2 = [(tvert2_flat[i * 2], tvert2_flat[i * 2 + 1]) for i in range(num_tverts2)]

    prim_stride = 12
    num_prims = len(primitives_raw) // prim_stride
    primitives = []
    for i in range(num_prims):
        start, num_elements, mat_index_raw = struct.unpack_from(
            "<3i", primitives_raw, i * prim_stride
        )
        primitives.append((start, num_elements, mat_index_raw))

    index_elem_size = 4
    num_indices = len(indices_raw) // index_elem_size if index_elem_size else 0
    indices = list(struct.unpack(f"<{num_indices}i", indices_raw[: num_indices * 4])) if num_indices else []

    return {
        "num_verts": num_verts,
        "uvs": uvs,
        "uvs2": uvs2,
        "primitives": primitives,
        "indices": indices,
    }

def _primitive_triangles(primitives: list[tuple[int, int, int]], indices: list[int]) -> dict[int, list[tuple[int, int, int]]]:
    by_material: dict[int, list[tuple[int, int, int]]] = {}

    for start, num_elements, mat_index_raw in primitives:
        if num_elements <= 0:
            continue
        prim_type = mat_index_raw & _PRIM_TYPE_MASK
        no_material = bool(mat_index_raw & _PRIM_NO_MATERIAL)
        mat_index = -1 if no_material else (mat_index_raw & _PRIM_MATERIAL_MASK)

        seg = indices[start : start + num_elements]
        if len(seg) < num_elements:
            continue

        tris = by_material.setdefault(mat_index, [])

        if prim_type == _PRIM_TYPE_TRIANGLES:
            for t in range(len(seg) // 3):
                a, b, c = seg[t * 3], seg[t * 3 + 1], seg[t * 3 + 2]
                tris.append((a, b, c))

        elif prim_type == _PRIM_TYPE_STRIP:
            for t in range(len(seg) - 2):
                if t % 2 == 0:
                    tris.append((seg[t], seg[t + 1], seg[t + 2]))
                else:
                    tris.append((seg[t + 1], seg[t], seg[t + 2]))

        elif prim_type == _PRIM_TYPE_FAN:
            pivot = seg[0]
            for t in range(1, len(seg) - 1):
                tris.append((pivot, seg[t], seg[t + 1]))

    return by_material

def _decode_meshes(path: str) -> list[dict | None]:
    if msgpack is None:
        raise MeshParseError(
            "Reading .cdae files requires the 'msgpack' Python package, which "
            "isn't installed. Re-run this app's install/setup script "
            "(run.sh / run.bat) to install it."
        )

    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        raise MeshParseError(f"Could not read .cdae file: {e}") from e

    if len(data) < 8:
        raise MeshParseError("This .cdae file is too small to be valid.")

    version, header_size = struct.unpack_from("<Ii", data, 0)
    if header_size < 0:
        raise MeshParseError("This .cdae file has an invalid header size.")

    if (version & CDAE_VERSION_MASK) != CDAE_SUPPORTED_VERSION:
        raise MeshParseError(
            f"Unsupported .cdae version ({version & CDAE_VERSION_MASK}). "
            f"This parser only supports v{CDAE_SUPPORTED_VERSION} "
            "(the format used by current BeamNG.drive)."
        )

    header_start = 8
    header_end = header_start + header_size
    if header_end > len(data):
        raise MeshParseError(
            "This .cdae file's header is truncated or the file is corrupted."
        )

    try:
        header_info = msgpack.unpackb(data[header_start:header_end], raw=False)
    except Exception as e:
        raise MeshParseError(f"Could not parse .cdae header (corrupt MessagePack): {e}") from e

    if not isinstance(header_info, dict):
        raise MeshParseError("This .cdae file's header isn't in the expected format.")

    compressed = bool(header_info.get("compression", False))
    body_raw = data[header_end:]

    if compressed:
        if zstandard is None:
            raise MeshParseError(
                "This .cdae file's body is Zstandard-compressed, which requires "
                "the 'zstandard' Python package that isn't installed. Re-run "
                "this app's install/setup script (run.sh / run.bat) to install it."
            )
        try:
            body = zstandard.ZstdDecompressor().decompress(
                body_raw, max_output_size=max(len(body_raw) * 20, 1 << 24)
            )
        except Exception as e:
            raise MeshParseError(f"Failed to decompress .cdae body (Zstandard): {e}") from e
    else:
        body = body_raw

    unpacker = msgpack.Unpacker(raw=True, max_buffer_size=len(body) + 16)
    unpacker.feed(body)
    cursor = _Cursor(unpacker)

    try:
        for _ in range(2 + 2 + 3 + 6):
            cursor.obj()

        _read_shape_vectors(cursor)

        _read_names_table(cursor)

        total_meshes = cursor.obj()
        if not isinstance(total_meshes, int) or total_meshes < 0:
            raise MeshParseError("Malformed mesh count in .cdae body.")

        parsed_meshes = []
        for mesh_index in range(total_meshes):
            parsed_meshes.append(_read_mesh(cursor, mesh_index))

    except MeshParseError:
        raise
    except Exception as e:
        raise MeshParseError(
            f"Failed to parse .cdae body: {e}. The file may use a variant of "
            "the v31 layout this parser doesn't recognize."
        ) from e

    return parsed_meshes

def list_uv_channels(path: str) -> list[tuple[str, str]]:
    try:
        parsed_meshes = _decode_meshes(path)
    except MeshParseError:
        return []

    has_1 = any(m is not None and m["uvs"] for m in parsed_meshes)
    has_2 = any(m is not None and m["uvs2"] for m in parsed_meshes)

    channels = []
    if has_1:
        channels.append(("1", "UV Channel 1"))
    if has_2:
        channels.append(("2", "UV Channel 2 (lightmap/AO)"))
    return channels

def parse_cdae(path: str, uv_channel: str | None = None) -> UVMesh:
    if uv_channel not in (None, "1", "2"):
        raise MeshParseError(f"Unknown .cdae UV channel '{uv_channel}' (expected '1' or '2').")
    channel = uv_channel or "1"

    mesh = UVMesh(source_path=path, format_name="BeamNG Cached COLLADA (.cdae)")
    parsed_meshes = _decode_meshes(path)

    channel_key = "uvs2" if channel == "2" else "uvs"
    other_key = "uvs" if channel == "2" else "uvs2"

    has_selected_channel = any(m is not None and m[channel_key] for m in parsed_meshes)
    has_other_channel = any(m is not None and m[other_key] for m in parsed_meshes)

    uvs1_present = has_selected_channel if channel == "1" else has_other_channel
    uvs2_present = has_selected_channel if channel == "2" else has_other_channel
    mesh.available_uv_sets = [c for c, present in (("1", uvs1_present), ("2", uvs2_present)) if present] or ["1"]
    mesh.active_uv_set = channel

    if not has_selected_channel:
        if has_other_channel:
            raise MeshParseError(
                f"This .cdae file has no data on UV channel {channel}, but does "
                f"have data on UV channel {'1' if channel == '2' else '2'}. "
                "Switch the UV channel selector and try again."
            )
        raise MeshParseError(
            "Parsed the .cdae file but found no UV-mapped mesh data on either "
            "UV channel. The shape may only contain collision/collapsed "
            "detail levels, or use a mesh type this parser doesn't extract UVs from."
        )

    skipped_other_channel_only = 0

    for mesh_index, parsed in enumerate(parsed_meshes):
        if parsed is None:
            continue
        mesh_uvs = parsed[channel_key]
        if not mesh_uvs:
            if parsed[other_key]:
                skipped_other_channel_only += 1
            continue

        by_material = _primitive_triangles(parsed["primitives"], parsed["indices"])
        if not by_material:
            mesh.warnings.append(
                f"Mesh #{mesh_index} had UV data but no usable triangle primitives; skipped."
            )
            continue

        for mat_index, tris in by_material.items():
            if not tris:
                continue
            group_name = (
                f"mesh{mesh_index}_mat{mat_index}" if mat_index >= 0 else f"mesh{mesh_index}_nomat"
            )

            used_indices = sorted({i for tri in tris for i in tri})
            remap = {old: new for new, old in enumerate(used_indices)}
            try:
                group_uvs = [mesh_uvs[i] for i in used_indices]
            except IndexError:
                mesh.warnings.append(
                    f"Group '{group_name}' referenced UV indices out of range; skipped."
                )
                continue
            remapped_tris = [(remap[a], remap[b], remap[c]) for a, b, c in tris]
            mesh.groups.append(UVGroup(name=group_name, uvs=group_uvs, triangles=remapped_tris))

    if skipped_other_channel_only:
        mesh.warnings.append(
            f"{skipped_other_channel_only} mesh(es) had no data on UV channel "
            f"{channel} (only on the other channel) and were skipped."
        )

    if mesh.is_empty:
        raise MeshParseError(
            "Parsed the .cdae file but found no valid UV-mapped triangles to export."
        )

    return mesh
