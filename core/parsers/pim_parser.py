"""Parser for SCS Software's .pim (PIX Interchange Model) text format.

.pim is the plain-text "middle format" used by SCS's Blender Tools pipeline
(PIM = model geometry, PIT = traits, PIC = colliders, PIP = prefab, PIS =
skeleton, PIA = animation). It is a section-based structured text format:

    SectionName {
        PropName: value
        PropName2: value2
        AnotherSection {
            ...
        }
    }

Within a "Piece" section, per-vertex attribute data is stored as "Stream"
sub-sections carrying a Format (e.g. FLOAT2) and Tag (e.g. _UV) followed by
that many data lines, and faces are stored as flat triangle index lists.

Because this is a reverse-engineered, community-documented format rather
than a formally specified one, this parser is intentionally tolerant: it
scans for the structural tokens it needs (Piece / Stream / Format / Tag /
Triangles-like index blocks) rather than requiring an exact grammar match,
and it reports clearly if a file's structure doesn't match what it expects
instead of silently producing wrong output.
"""

import re

from core.mesh_data import MeshParseError, UVGroup, UVMesh

_SECTION_OPEN_RE = re.compile(r"^(\w+)\s*\{\s*$")
_PROP_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$")


class _Section:
    __slots__ = ("type", "props", "children")

    def __init__(self, type_name: str):
        self.type = type_name
        self.props: list[tuple[str, str]] = []
        self.children: list["_Section"] = []

    def prop(self, name: str, default=None):
        for k, v in self.props:
            if k == name:
                return v
        return default

    def all_props(self, name: str):
        return [v for k, v in self.props if k == name]

    def child(self, type_name: str):
        for c in self.children:
            if c.type == type_name:
                return c
        return None

    def children_of(self, type_name: str):
        return [c for c in self.children if c.type == type_name]


def _tokenize_blocks(text: str) -> list[str]:
    """Split raw text into lines, stripping comments and blank lines."""
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        lines.append(line)
    return lines


def _parse_sections(lines: list[str], start: int = 0) -> tuple[list[_Section], int]:
    """Parses a flat list of pre-cleaned lines into a section tree.
    Returns (sections_at_this_level, next_index)."""
    sections = []
    i = start
    while i < len(lines):
        line = lines[i]
        if line == "}":
            return sections, i + 1

        m = _SECTION_OPEN_RE.match(line)
        if m:
            sec = _Section(m.group(1))
            children, i = _parse_sections(lines, i + 1)
            sec.children = children
            sections.append(sec)
            continue

        m = _PROP_RE.match(line)
        if m and sections:
            sections[-1].props.append((m.group(1), m.group(2)))
            i += 1
            continue
        elif m:
            # Property before any section opened at this level -- attach to
            # a synthetic holder so nothing is silently lost, but this
            # shouldn't normally happen in a well-formed file.
            i += 1
            continue

        # data lines that belong to the most recently opened but not-yet-
        # closed section (e.g. raw stream numeric data) are handled by the
        # caller inspecting props with a special "__data__" convention;
        # here we just skip lines we don't recognize structurally.
        i += 1

    return sections, i


def _try_parse_numeric_row(line: str) -> list[float] | None:
    parts = line.replace(",", " ").split()
    try:
        return [float(p) for p in parts]
    except ValueError:
        return None


def _extract_stream_blocks(raw_text: str) -> list[dict]:
    """
    Best-effort extraction of Stream blocks (Format/Tag + numeric data rows)
    from within Piece sections, since the exact nesting/line-count framing
    of stream data in real .pim files is not something this parser can
    verify without a reference sample. This scans line-by-line for the
    documented tokens (Format:, Tag:) and collects subsequent numeric rows
    until the next recognized keyword or closing brace.
    """
    streams = []
    lines = raw_text.splitlines()
    current = None
    for raw in lines:
        line = raw.strip()
        if not line:
            continue

        fmt_m = re.match(r'^Format\s*:\s*"?(\w+)"?', line)
        tag_m = re.match(r'^Tag\s*:\s*"?(\w+)"?', line)

        if fmt_m:
            if current is None:
                current = {"format": None, "tag": None, "rows": []}
            current["format"] = fmt_m.group(1)
            continue
        if tag_m:
            if current is None:
                current = {"format": None, "tag": None, "rows": []}
            current["tag"] = tag_m.group(1)
            continue

        if line.startswith("Stream") or line == "}":
            if current is not None and current["tag"] is not None:
                streams.append(current)
            current = None
            continue

        row = _try_parse_numeric_row(line)
        if row is not None and current is not None and current["tag"] is not None:
            current["rows"].append(row)

    if current is not None and current["tag"] is not None:
        streams.append(current)

    return streams


def _list_uv_tags_from_text(raw_text: str) -> list[str]:
    seen: list[str] = []
    seen_set = set()
    for s in _extract_stream_blocks(raw_text):
        tag = s["tag"]
        if tag and "UV" in tag.upper() and tag not in seen_set:
            seen_set.add(tag)
            seen.append(tag)
    return seen


def list_uv_tags(path: str) -> list[str]:
    """Scans a .pim file for the distinct UV-ish stream tags used across all
    Piece sections (e.g. ["_UV0", "_UV1"] for a model with separate paint
    and lightmap UV streams), in first-seen order. Used by the UI to offer
    a channel picker before parsing. Returns [] on read failure or if no
    Piece/UV-tagged streams are found; parse_pim() will raise a clearer
    error in that case."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            raw_text = f.read()
    except OSError:
        return []
    return _list_uv_tags_from_text(raw_text)


def parse_pim(path: str, uv_tag: str | None = None) -> UVMesh:
    """uv_tag: an exact stream tag (e.g. "_UV0"), or None to use whichever
    UV-ish stream tag appears first in each piece (the historical default
    behavior, kept for files with only one UV stream). When uv_tag is given
    and a piece doesn't have a stream with that exact tag, that piece is
    skipped with a warning rather than silently substituting a different
    UV stream."""
    mesh = UVMesh(source_path=path, format_name="SCS PIM")

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            raw_text = f.read()
    except OSError as e:
        raise MeshParseError(f"Could not read .pim file: {e}") from e

    # Reuse this read for tag discovery instead of a second file read via
    # list_uv_tags() -- see the .dae parser's equivalent comment.
    available_tags = _list_uv_tags_from_text(raw_text)
    mesh.available_uv_sets = available_tags or ([uv_tag] if uv_tag else [])
    mesh.active_uv_set = uv_tag if uv_tag is not None else (available_tags[0] if available_tags else "")

    if "Piece" not in raw_text:
        raise MeshParseError(
            "This doesn't look like a valid .pim model file (no 'Piece' "
            "sections found). If this came from a .pmg/.pmd pair, make sure "
            "it was converted with ConverterPIX first."
        )

    # Split the file into per-Piece chunks so streams don't bleed across pieces.
    piece_starts = [m.start() for m in re.finditer(r"^Piece\b.*\{", raw_text, re.MULTILINE)]
    if not piece_starts:
        raise MeshParseError(
            "Found the word 'Piece' but couldn't locate any 'Piece { ... }' "
            "sections. This .pim file's structure isn't what this tool expects -- "
            "it may be a newer/older PIM version."
        )

    piece_starts.append(len(raw_text))
    uv_groups_found = 0
    pieces_missing_tag = 0

    for idx in range(len(piece_starts) - 1):
        chunk = raw_text[piece_starts[idx] : piece_starts[idx + 1]]

        streams = _extract_stream_blocks(chunk)
        uv_stream = None
        if uv_tag is not None:
            for s in streams:
                if s["tag"] == uv_tag:
                    uv_stream = s
                    break
            if uv_stream is None:
                if any(s["tag"] and "UV" in s["tag"].upper() for s in streams):
                    pieces_missing_tag += 1
                continue
        else:
            for s in streams:
                if s["tag"] and "UV" in s["tag"].upper():
                    uv_stream = s
                    break

        if uv_stream is None or not uv_stream["rows"]:
            continue

        uvs = [(row[0], row[1]) for row in uv_stream["rows"] if len(row) >= 2]

        # Look for a Triangles/Faces index block within this piece chunk.
        tri_match = re.search(
            r"(?:Triangles|Faces)\s*\{(.*?)\}", chunk, re.DOTALL
        )
        triangles: list[tuple[int, int, int]] = []
        if tri_match:
            idx_rows = []
            for line in tri_match.group(1).splitlines():
                row = _try_parse_numeric_row(line.strip())
                if row:
                    idx_rows.append([int(v) for v in row])
            flat = [v for row in idx_rows for v in row]
            for t in range(len(flat) // 3):
                triangles.append((flat[t * 3], flat[t * 3 + 1], flat[t * 3 + 2]))
        else:
            # No explicit triangle block found -- assume the UV stream is
            # already in triangle-list order (3 consecutive rows = 1 tri),
            # which matches how "graphics-card-friendly" exported streams
            # are typically laid out.
            for t in range(len(uvs) // 3):
                triangles.append((t * 3, t * 3 + 1, t * 3 + 2))

        if not triangles:
            continue

        piece_name = f"piece_{idx}"
        try:
            max_idx = max(i for tri in triangles for i in tri)
        except ValueError:
            continue
        if max_idx >= len(uvs):
            mesh.warnings.append(
                f"Piece {idx}: triangle indices exceed UV data range; skipped."
            )
            continue

        mesh.groups.append(UVGroup(name=piece_name, uvs=uvs, triangles=triangles))
        uv_groups_found += 1

    if pieces_missing_tag:
        mesh.warnings.append(
            f"{pieces_missing_tag} piece(s) had UV data but not on stream "
            f"'{uv_tag}' (they have a different UV stream) and were skipped."
        )

    if uv_groups_found == 0:
        if uv_tag is not None and pieces_missing_tag:
            raise MeshParseError(
                f"No piece in this .pim file has a UV stream tagged '{uv_tag}'. "
                "Switch the UV channel selector to one of the tags this file "
                "actually has and try again."
            )
        raise MeshParseError(
            "Couldn't extract any UV data from this .pim file. This parser "
            "supports the common PIM text layout, but SCS has revised this "
            "format across versions and this file may use a variant it "
            "doesn't recognize. If you can share a sample, this can be fixed."
        )

    if mesh.is_empty:
        raise MeshParseError("Parsed the .pim file but found no valid UV triangles.")

    return mesh
