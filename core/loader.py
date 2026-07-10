"""Detects input file type and dispatches to the correct parser."""

import os

from core.mesh_data import MeshParseError, UVMesh
from core.parsers.cdae_parser import list_uv_channels as _list_cdae_uv_channels
from core.parsers.cdae_parser import parse_cdae
from core.parsers.dae_parser import list_uv_sets as _list_dae_uv_sets
from core.parsers.dae_parser import parse_dae
from core.parsers.obj_parser import parse_obj
from core.parsers.pim_parser import list_uv_tags as _list_pim_uv_tags
from core.parsers.pim_parser import parse_pim
from core.parsers.scs_converter import (
    ConversionFailedError,
    ConverterNotFoundError,
    convert_pmg_to_pim,
)

SUPPORTED_EXTENSIONS = {".obj", ".dae", ".cdae", ".pim", ".pmg", ".pmd"}

# Extensions where a single file might expose more than one UV channel/layer
# (paint UVs vs. a lightmap/AO unwrap, etc.). .obj isn't listed: this app's
# OBJ parser has no concept of multiple UV channels, only one global `vt`
# pool per file. .pmg/.pmd aren't listed either -- channels can only be
# discovered after ConverterPIX has produced a .pim, which load_mesh() does
# on demand; see list_uv_channels()'s docstring for how to handle that case.
MULTI_CHANNEL_EXTENSIONS = {".dae", ".cdae", ".pim"}


def list_uv_channels(path: str) -> list[tuple[str, str]]:
    """Returns [(channel_id, display_label), ...] of the UV channels/layers
    this file exposes, for the UI to offer a picker *before* parsing.
    channel_id is passed back into load_mesh(..., uv_channel=...).

    Returns [] when the format doesn't have a meaningful multi-channel
    concept here (.obj), or when channel discovery isn't possible without a
    full parse the caller hasn't done yet (.pmg/.pmd -- convert to .pim
    first, then call this on the resulting .pim path if you need channels
    for it), or on any read/parse failure (the UI should just hide the
    selector in that case; load_mesh() will raise a proper error).
    """
    ext = os.path.splitext(path)[1].lower()

    if ext == ".dae":
        return [(s, f"UV Set {s}") for s in _list_dae_uv_sets(path)]

    if ext == ".cdae":
        return _list_cdae_uv_channels(path)

    if ext == ".pim":
        return [(tag, tag) for tag in _list_pim_uv_tags(path)]

    return []


def load_mesh(path: str, base_dir: str | None = None, uv_channel: str | None = None) -> UVMesh:
    """Loads any supported mesh file and returns parsed UV data.

    uv_channel selects which UV layer/set to use for formats that can carry
    more than one (see list_uv_channels()); None uses each parser's default
    (UV set "0" for .dae, channel "1" for .cdae, the first UV-tagged stream
    for .pim). Ignored for .obj, which only ever has one UV pool.

    For .pmg/.pmd (raw SCS binaries), this transparently shells out to
    ConverterPIX to produce a .pim first.
    """
    ext = os.path.splitext(path)[1].lower()

    if ext == ".obj":
        return parse_obj(path)

    if ext == ".dae":
        return parse_dae(path, uv_set=uv_channel)

    if ext == ".cdae":
        return parse_cdae(path, uv_channel=uv_channel)

    if ext == ".pim":
        return parse_pim(path, uv_tag=uv_channel)

    if ext in (".pmg", ".pmd"):
        try:
            pim_path = convert_pmg_to_pim(path, base_dir=base_dir)
        except ConverterNotFoundError as e:
            raise MeshParseError(str(e)) from e
        except ConversionFailedError as e:
            raise MeshParseError(
                f"Automatic conversion from {ext} to .pim failed: {e}"
            ) from e
        mesh = parse_pim(pim_path, uv_tag=uv_channel)
        mesh.warnings.insert(
            0, f"Auto-converted from {os.path.basename(path)} via ConverterPIX."
        )
        return mesh

    raise MeshParseError(
        f"Unsupported file type '{ext}'. Supported: "
        f"{', '.join(sorted(SUPPORTED_EXTENSIONS))}"
    )
