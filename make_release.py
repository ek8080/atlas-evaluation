"""Build a self-contained, credential-free release bundle for independent verification.

Creates dist/atlas_evaluation_release.zip containing the code, datasets, freeze record, unit
tests, documentation, raw Inspect logs (logs/) and all exported results (results/), so that a
reviewer can re-run the tests, re-score every logged response and check every reported number.

Safety: the bundle is scanned before it is written. Every text file and every file inside each
.eval log (which are zip archives) is checked for
  * the actual API key values in your local .env (compared in memory, never printed), and
  * generic API-key patterns (OpenAI, Anthropic, OpenRouter, Google).
If anything matches, no bundle is written and the offending file paths are listed.

Usage:
  python make_release.py            # scan + build dist/atlas_evaluation_release.zip
  python make_release.py --check    # scan only
"""

from __future__ import annotations

import argparse
import io
import re
import sys
import zipfile
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "dist" / "atlas_evaluation_release.zip"

INCLUDE_FILES = [
    "README.md", "report.md", "SUMMARY.md", "requirements.txt", ".env.example", ".gitignore",
    "atlas_eval.py", "scoring.py", "export_results.py", "guardrail_eval.py", "make_release.py",
    "redact_logs.py", "LICENSE", "LICENSE-DATA.md",
    "atlas_tests.json", "atlas_tests_hard.json", "atlas_tests_holdout.json", "HOLDOUT_FREEZE.json",
    "model_routes.json",
]
INCLUDE_DIRS = ["tests", "logs", "results"]
EXCLUDE_PARTS = {"mock", "__pycache__", ".pytest_cache"}

KEY_PATTERNS = [
    re.compile(rb"sk-(?:proj-|ant-|or-v1-)?[A-Za-z0-9_\-]{20,}"),  # OpenAI / Anthropic / OpenRouter
    re.compile(rb"AIza[0-9A-Za-z_\-]{30,}"),                          # Google API keys
]


def _files() -> list[Path]:
    out = [ROOT / f for f in INCLUDE_FILES if (ROOT / f).exists()]
    for d in INCLUDE_DIRS:
        for p in sorted((ROOT / d).rglob("*")):
            if p.is_file() and not (EXCLUDE_PARTS & set(p.relative_to(ROOT).parts)):
                out.append(p)
    return out


def _secrets() -> list[bytes]:
    vals = dotenv_values(ROOT / ".env") if (ROOT / ".env").exists() else {}
    return [v.encode() for k, v in vals.items() if v and "KEY" in k.upper() and len(v) >= 12]


def _blobs(path: Path):
    """Yield (label, bytes) for a file. For Inspect .eval logs (compressed archives), also yield
    the fully decoded log (samples, model events, raw requests/responses, attachments)."""
    data = path.read_bytes()
    yield str(path.relative_to(ROOT)), data
    if path.suffix == ".eval":
        from inspect_ai.log import read_eval_log
        yield f"{path.relative_to(ROOT)}::decoded", read_eval_log(str(path)).model_dump_json().encode("utf-8")
    elif zipfile.is_zipfile(io.BytesIO(data)):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for name in z.namelist():
                yield f"{path.relative_to(ROOT)}::{name}", z.read(name)


def scan(files: list[Path]) -> list[str]:
    secrets, hits = _secrets(), []
    for f in files:
        for label, blob in _blobs(f):
            if any(s in blob for s in secrets):
                hits.append(f"{label}  (matches a key from your .env)")
            elif any(p.search(blob) for p in KEY_PATTERNS):
                hits.append(f"{label}  (matches a generic API-key pattern)")
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="scan only; do not write the bundle")
    args = ap.parse_args()
    files = _files()
    hits = scan(files)
    print(f"Scanned {len(files)} files (including the fully decoded content of each .eval log) "
          f"against {len(_secrets())} local key value(s) and generic key patterns.")
    if hits:
        print("Possible credentials found; NO bundle written:")
        for h in hits:
            print("  " + h)
        return 1
    print("No credentials found.")
    if args.check:
        return 0
    OUT.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, Path("atlas_evaluation") / f.relative_to(ROOT))
    print(f"Wrote {OUT} ({OUT.stat().st_size / 1e6:.1f} MB, {len(files)} files).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
