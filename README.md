# UV Template Exporter

A standalone PySide6 tool for exporting blank UV wireframe templates (à la
BeamNG "Easy Mod" skin templates) from vehicle mesh files, with support for
resolutions up to **16384×16384**.

## Supported input formats

| Format | Notes |
|---|---|
| `.obj` | Standard Wavefront OBJ. Groups by `usemtl` material (falls back to `o`/`g` if no materials). |
| `.dae` | COLLADA. Supports both `<triangles>` and `<polylist>`/`<polygons>` mesh encodings, multiple UV sets (defaults to set 0), and groups by material. |
| `.cdae` | BeamNG's binary "Cached Collada" shape format (`TSShape`, MessagePack + optional Zstandard) — BeamNG's own compiled runtime form of a `.dae`. Supports multiple UV channels, groups by material. |
| `.pim` | SCS Software's text-based "PIX Interchange Model" format (the middle format used by SCS Blender Tools / ConverterPIX for ETS2/ATS). |
| `.pmg` / `.pmd` | Raw SCS binary model files. These are **auto-converted** to `.pim` via [ConverterPIX](https://github.com/mwl4/ConverterPIX) before parsing — see setup below. |

## Setup

**Windows:** double-click `run.bat`. It checks for Python, installs PySide6
automatically if missing, then launches the app.

**Linux / macOS:** run `./run.sh` (or `bash run.sh`). Same behavior as
`run.bat`.

Manual setup, if you'd rather do it yourself:

```bash
pip install PySide6 --break-system-packages
python3 main.py
```

### Optional: enabling `.cdae` support

BeamNG's `.cdae` format is a MessagePack container (optionally
Zstandard-compressed) rather than plain XML, so reading it needs two extra
packages beyond PySide6:

```bash
pip install msgpack zstandard --break-system-packages
```

`msgpack` is required for any `.cdae` file; `zstandard` is only needed if
the specific file is Zstandard-compressed, but installing both up front is
simplest. Without them, loading a `.cdae` file will fail with a clear error
telling you which package is missing rather than crashing the app.

### Optional: enabling `.pmg` / `.pmd` support

`.pmg`/`.pmd` are SCS's compiled binary formats — there's no reliable
standalone parser for them (SCS's own Blender importer for this format is
unmaintained). Instead, this tool shells out to **ConverterPIX**, a
community tool that converts SCS binary formats to the text-based `.pim`
format this tool can read directly.

1. Download a ConverterPIX build for your OS from
   https://github.com/mwl4/ConverterPIX
2. Place the executable (`converter_pix` / `converter_pix.exe`) in this
   project's `resources/` folder, or anywhere on your system `PATH`.
3. When you drop a `.pmg`/`.pmd` file onto the app, it will automatically
   invoke ConverterPIX and load the resulting `.pim`.

Note: ConverterPIX needs the model's "base" directory (the mod/game root
folder containing `vehicle/`, `material/`, etc.) to resolve referenced
material files. By default this tool uses the input file's parent folder —
if conversion fails, try pointing the input file's location at (or inside)
your extracted game base folder.

If you already have `.pim` files (e.g. exported directly from SCS Blender
Tools, or already converted), you can skip ConverterPIX entirely and just
load the `.pim` file.

## Usage

1. Drop a mesh file onto the app (or click to browse).
2. Uncheck any mesh groups/materials you don't want in the template (e.g.
   glass, collision meshes).
3. Set resolution, line width/color, and optional material color-coding or
   checker background.
4. Click **Export UV Template PNG…** and choose a save location.

The live preview renders at a fixed low resolution for responsiveness —
the actual export always renders fresh at your selected resolution.

## Performance notes

- Resolutions up to 8192×8192 render in well under a second for typical
  vehicle-complexity meshes.
- 16384×16384 uses roughly a 1GB working image buffer and can take anywhere
  from ~10 seconds to significantly longer depending on triangle count —
  antialiasing is automatically disabled above 8192px to keep this
  reasonable, since individual lines are already sub-pixel dense at that
  point.
- Rendering the low-res live preview happens synchronously on the UI
  thread, so it can briefly block the window on complex meshes (the app
  shows a small "Applying…" indicator over the preview in that case).
  Export, however, always runs on a background thread, so the UI stays
  responsive during high-res exports even though the preview doesn't.

## Architecture

```
core/
  mesh_data.py          UVMesh / UVGroup data model shared by all parsers
  loader.py              format detection + dispatch
  rasterizer.py           UV -> wireframe PNG rendering (QPainter/QImage)
  parsers/
    obj_parser.py         .obj
    dae_parser.py          .dae (COLLADA)
    cdae_parser.py          .cdae (BeamNG binary shape)
    pim_parser.py           .pim (SCS text format)
    scs_converter.py         ConverterPIX wrapper for .pmg/.pmd
gui/
  main_window.py          main window, drag-drop, worker threads
  widgets.py                color picker, group checklist
main.py                    entry point
```

## Known limitations

- The `.pim` parser is built against publicly documented structural
  concepts (Piece/Stream/Format/Tag sections) rather than a verified formal
  grammar, since SCS's format isn't officially specified and has changed
  across versions. It's intentionally defensive — it will raise a clear
  error rather than silently produce a wrong template if a file's exact
  layout doesn't match. If you hit a parse failure on a real `.pim` file,
  that's a fixable parser gap, not a dead end.
- COLLADA files with UV data addressed through unusual/nonstandard
  `<source>` indirection chains (rare, but COLLADA is a loose spec) may not
  be picked up — the parser covers the standard VERTEX→POSITION and
  TEXCOORD input patterns used by Blender/3ds Max/Maya exporters.
- The `.cdae` parser targets the documented v31 layout only (see
  [BeamNG's format docs](https://documentation.beamng.com/modding/file_formats/cdae/)).
  Standard and skinned meshes are read into UV data; decal/sorted/null
  meshes and animation data are parsed structurally (to stay in sync with
  the file) but don't contribute geometry, so a model made up only of those
  produces a clear "no usable UV data" error rather than a wrong template.
