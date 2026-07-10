"""Parser for BeamNG's "Cached Collada" (.cdae) binary shape format.

.cdae is BeamNG's compiled/binary form of a DAE model (a Torque3D-derived
TSShape), used at runtime instead of the original .dae for faster loading.
It is NOT XML like standard COLLADA -- it's a MessagePack container,
optionally Zstandard-compressed, holding a serialized TSShape.

This parser implements the documented v31 layout
(https://documentation.beamng.com/modding/file_formats/cdae/):

    int32   version            (low byte must be 31)
    uint32  headerSize
    bytes   msgpack header      {compression, bodysize, ...}
    bytes   body                (zstd-compressed if header.compression)

The decompressed body is itself MessagePack, containing, in order:
shape info, a fixed sequence of named "pack_vector" blocks (nodes,
objects, ..., details), a shape-names string table, then the mesh list.

Only what's needed for a UV template is extracted: each StandardMesh's
`verts`/`tverts` (source UV data) and `primitives`/`indices` (to build
triangles), grouped by material index via the shape's material list.
Skinned/decal/sorted/null meshes and animation data are read structurally
(to stay in sync with the stream) but not interpreted further.

Torque3D's TSDrawPrimitive (3 x int32: start, numElements, matIndex) packs
draw-type and material index into matIndex's high bits:
    TypeMask     = bits 30-31 (0=Triangles, 1=Strip, 2=Fan)
    Indexed      = bit 29
    NoMaterial   = bit 28
    MaterialMask = remaining bits (the actual material index)
"""

import struct

from core.mesh_data import MeshParseError, UVGroup, UVMesh

try:
    import msgpack
except ImportError:  # pragma: no cover - surfaced as a clear runtime error
    msgpack = None

try:
    import zstandard
except ImportError:  # pragma: no cover
    zstandard = None


CDAE_VERSION_MASK = 0xFF
CDAE_SUPPORTED_VERSION = 31

# TSDrawPrimitive.matIndex bit layout (see TSMesh::TSDrawPrimitive in Torque3D)
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
    """Small helper for walking a decoded MessagePack object stream in the
    exact fixed order the format defines, since the body isn't a single
    nested msgpack value but a flat sequence of independently-packed
    objects and binary vector blocks concatenated together."""

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
    """Reads one pack_vector block: (length int32, element_size int32, raw bin)."""
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
    """Reads (and discards) the fixed sequence of shape-level pack_vector
    blocks that precede the mesh list. We don't need node/animation data
    for a UV template, but must consume them in order to reach the meshes."""
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
    """Reads one mesh entry. Returns a dict with verts/tverts/primitives/
    indices for standard & skin meshes, or None for null/unsupported meshes
    (still fully consumed from the stream)."""
    mesh_type = cursor.obj()
    if not isinstance(mesh_type, int):
        raise MeshParseError(f"Malformed mesh type for mesh #{mesh_index} in .cdae body.")

    if mesh_type == MESH_TYPE_NULL:
        return None

    # numFrames, numMatFrames, parentMesh
    _num_frames = cursor.obj()
    _num_mat_frames = cursor.obj()
    parent_mesh = cursor.obj()
    # Box3F bounds (6 floats) + Point3F center (3 floats) + float radius,
    # all packed as individual msgpack floats.
    for _ in range(6 + 3 + 1):
        cursor.obj()

    _len_v, _sz_v, verts_raw = _read_pack_vector(cursor)
    _len_tv, _sz_tv, tverts_raw = _read_pack_vector(cursor)
    _len_tv2, _sz_tv2, tverts2_raw = _read_pack_vector(cursor)  # 2nd UV channel (lightmap/AO, if present)
    _skip_pack_vector(cursor)  # colors
    _skip_pack_vector(cursor)  # norms
    _skip_pack_vector(cursor)  # encodedNorms
    _len_p, _sz_p, primitives_raw = _read_pack_vector(cursor)
    _len_i, _sz_i, indices_raw = _read_pack_vector(cursor)
    _skip_pack_vector(cursor)  # tangents

    _verts_per_frame = cursor.obj()
    _mesh_flags = cursor.obj()

    if mesh_type == MESH_TYPE_SKIN:
        is_parent = isinstance(parent_mesh, int) and parent_mesh < 0
        if is_parent:
            _skip_pack_vector(cursor)  # initialVerts
            _skip_pack_vector(cursor)  # initialNorms
        _skip_pack_vector(cursor)  # initialTransforms
        if is_parent:
            _skip_pack_vector(cursor)  # vertexIndex
            _skip_pack_vector(cursor)  # boneIndex
            _skip_pack_vector(cursor)  # weight
            _skip_pack_vector(cursor)  # nodeIndex

    if mesh_type not in (MESH_TYPE_STANDARD, MESH_TYPE_SKIN):
        # DecalMesh (deprecated) and SortedMesh share the same base layout
        # we've just consumed; we simply don't build UV groups from them,
        # since they aren't standard renderable surface geometry.
        return None

    # verts: Point3F (12 bytes) per vertex -> element count from raw length
    vert_elem_size = 12
    num_verts = len(verts_raw) // vert_elem_size if vert_elem_size else 0

    # tverts / tverts2: Point2F (8 bytes) per vertex. tverts2 is a second,
    # optional UV channel (commonly a lightmap/AO unwrap) -- present on some
    # meshes and empty on others, so it's decoded but may come back as [].
    tvert_elem_size = 8
    num_tverts = len(tverts_raw) // tvert_elem_size if tvert_elem_size else 0
    tvert_flat = _floats_from(tverts_raw, num_tverts * 2)
    uvs = [(tvert_flat[i * 2], tvert_flat[i * 2 + 1]) for i in range(num_tverts)]

    num_tverts2 = len(tverts2_raw) // tvert_elem_size if tvert_elem_size else 0
    tvert2_flat = _floats_from(tverts2_raw, num_tverts2 * 2)
    uvs2 = [(tvert2_flat[i * 2], tvert2_flat[i * 2 + 1]) for i in range(num_tverts2)]

    # primitives: TSDrawPrimitive = 3 x int32 (start, numElements, matIndex)
    prim_stride = 12
    num_prims = len(primitives_raw) // prim_stride
    primitives = []
    for i in range(num_prims):
        start, num_elements, mat_index_raw = struct.unpack_from(
            "<3i", primitives_raw, i * prim_stride
        )
        primitives.append((start, num_elements, mat_index_raw))

    # indices: array of signed/unsigned 32-bit ints (element size tells us
    # exactly, but Torque3D's TSMesh always stores these as 32-bit).
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
    """Converts a mesh's primitive list into triangles, grouped by resolved
    material index. Strips/fans are decomposed the same way the renderer
    would (alternating winding for strips, fan pivot for fans)."""
    by_material: dict[int, list[tuple[int, int, int]]] = {}

    for start, num_elements, mat_index_raw in primitives:
        if num_elements <= 0:
            continue
        prim_type = mat_index_raw & _PRIM_TYPE_MASK
        no_material = bool(mat_index_raw & _PRIM_NO_MATERIAL)
        mat_index = -1 if no_material else (mat_index_raw & _PRIM_MATERIAL_MASK)

        seg = indices[start : start + num_elements]
        if len(seg) < num_elements:
            # Truncated primitive -- skip rather than reading garbage.
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
    """Reads and decodes a .cdae file down to its list of parsed meshes
    (each with both UV channels already extracted). Shared by parse_cdae()
    and list_uv_channels() so the latter doesn't need its own copy of the
    binary-format walk."""
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
    # header_size is declared unsigned in the spec; struct 'i' reads it
    # signed, but a legitimate header is always small and positive.
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
        # 1. Shape info: mSmallestVisibleSize, mSmallestVisibleDL, radius,
        # tubeRadius, center (3 floats), bounds (6 floats)
        for _ in range(2 + 2 + 3 + 6):
            cursor.obj()

        # 2. Fixed sequence of shape-level pack_vector blocks
        _read_shape_vectors(cursor)

        # 3. Shape names table
        _read_names_table(cursor)

        # 4. Meshes
        total_meshes = cursor.obj()
        if not isinstance(total_meshes, int) or total_meshes < 0:
            raise MeshParseError("Malformed mesh count in .cdae body.")

        parsed_meshes = []
        for mesh_index in range(total_meshes):
            parsed_meshes.append(_read_mesh(cursor, mesh_index))

        # 5. Sequences (skip -- not needed for UVs, but must stay in sync
        # in case a caller wants to extend this parser later; since we
        # stop reading once we have the mesh + material data we need, we
        # don't attempt to parse sequences/materials positions here).
    except MeshParseError:
        raise
    except Exception as e:
        raise MeshParseError(
            f"Failed to parse .cdae body: {e}. The file may use a variant of "
            "the v31 layout this parser doesn't recognize."
        ) from e

    return parsed_meshes


def list_uv_channels(path: str) -> list[tuple[str, str]]:
    """Returns the UV channels this .cdae file actually has data on, as
    [(channel_id, display_label), ...], for the UI to offer a picker.
    Channel "1" (tverts) is present on virtually every real mesh; channel
    "2" (tverts2) is only offered if at least one mesh actually has data
    there, since most BeamNG shapes don't use a second UV channel at all."""
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
    """uv_channel: "1" (tverts, the default) or "2" (tverts2, commonly a
    lightmap/AO unwrap). Meshes without data on the requested channel are
    skipped with a warning rather than silently falling back to the other
    channel, since that produced exactly the kind of "missing part with no
    explanation" bug this parameter exists to avoid."""
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
