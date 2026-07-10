"""Wrapper around the ConverterPIX command-line tool.

ConverterPIX (https://github.com/mwl4/ConverterPIX) converts SCS's binary
game formats (.pmg/.pmd, collectively "pmx") into the text-based "pix"
middle formats (.pim etc.) that this app's pim_parser can read.

This module does NOT bundle the ConverterPIX binary (it's a compiled,
platform-specific executable under its own license). Instead it looks for
a user-provided copy in a few conventional locations and calls it. If it
can't be found, the caller should show clear instructions instead of
failing silently.
"""

import os
import shutil
import subprocess
import tempfile

CONVERTER_EXE_NAMES = ["converter_pix", "converter_pix.exe", "ConverterPIX", "ConverterPIX.exe"]


class ConverterNotFoundError(Exception):
    pass


class ConversionFailedError(Exception):
    pass


def find_converter_pix(extra_search_dirs: list[str] | None = None) -> str | None:
    """Search common locations for a ConverterPIX executable."""
    search_dirs = list(extra_search_dirs or [])

    app_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    search_dirs.append(os.path.join(app_dir, "resources"))
    search_dirs.append(os.path.join(app_dir, "resources", "converter_pix"))

    # Also check if it's just on PATH
    for name in CONVERTER_EXE_NAMES:
        found = shutil.which(name)
        if found:
            return found

    for d in search_dirs:
        if not os.path.isdir(d):
            continue
        for name in CONVERTER_EXE_NAMES:
            candidate = os.path.join(d, name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate

    return None


def convert_pmg_to_pim(
    pmg_path: str,
    base_dir: str | None = None,
    converter_path: str | None = None,
    timeout_sec: int = 60,
) -> str:
    """
    Converts a .pmg (or .pmd) file to .pim using ConverterPIX, returning the
    path to the resulting .pim file.

    ConverterPIX operates on a "base" directory structure (mirroring the
    game's mod/vehicle folder layout) rather than single loose files, since
    a model references materials/textures by relative path. If base_dir is
    not given, we use the file's parent directory as a best-effort base.
    """
    converter = converter_path or find_converter_pix()
    if converter is None:
        raise ConverterNotFoundError(
            "ConverterPIX executable not found. Place a copy of converter_pix "
            "(from https://github.com/mwl4/ConverterPIX) in the app's "
            "resources/ folder, or add it to your system PATH, then try again."
        )

    if not os.path.isfile(pmg_path):
        raise ConversionFailedError(f"Input file does not exist: {pmg_path}")

    base = base_dir or os.path.dirname(os.path.abspath(pmg_path))
    model_rel = os.path.splitext(os.path.basename(pmg_path))[0]

    out_dir = tempfile.mkdtemp(prefix="uvtemplate_convpix_")

    cmd = [
        converter,
        "-b", base,
        "-e", out_dir,
        "-m", "/" + model_rel,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except FileNotFoundError as e:
        raise ConverterNotFoundError(f"Could not execute ConverterPIX: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise ConversionFailedError(
            f"ConverterPIX timed out after {timeout_sec}s converting {pmg_path}."
        ) from e

    if result.returncode != 0:
        raise ConversionFailedError(
            f"ConverterPIX failed (exit code {result.returncode}):\n"
            f"{result.stdout}\n{result.stderr}"
        )

    pim_path = None
    for root, _dirs, files in os.walk(out_dir):
        for fname in files:
            if fname.lower().endswith(".pim"):
                pim_path = os.path.join(root, fname)
                break
        if pim_path:
            break

    if pim_path is None:
        raise ConversionFailedError(
            "ConverterPIX ran without error but produced no .pim file. "
            "The base directory may be missing referenced files "
            f"(tried base path: {base})."
        )

    return pim_path
