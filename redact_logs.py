"""Redact the local project path from Inspect logs before publishing.

Python error tracebacks recorded in a few logs (samples that failed with rate limits or
server errors) contain the absolute path of the project folder, which includes the local
Windows user name. This script replaces that path prefix with "<PROJECT_DIR>" and any other
"C:\\Users\\<name>" prefix with "<HOME>". Nothing else in the logs is changed: prompts,
responses, scores, metadata and timings stay identical (verified by --verify, which compares
every sample's input, output and scores with the backup).

Usage:
  python redact_logs.py                       # redact logs/ in place
  python redact_logs.py --verify BACKUP_DIR   # check redacted logs against unredacted copies
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from inspect_ai.log import EvalLog, read_eval_log, write_eval_log

ROOT = Path(__file__).resolve().parent
HOME_RE = re.compile(r"[A-Za-z]:(\\\\|\\|/)Users(\\\\|\\|/)[^\\/\"'\s]+", re.IGNORECASE)


def _project_variants() -> list[str]:
    p = str(ROOT)
    return sorted({p, p.replace("\\", "\\\\"), p.replace("\\", "/")}, key=len, reverse=True)


def redact_text(s: str) -> str:
    for v in _project_variants():
        s = re.sub(re.escape(v), "<PROJECT_DIR>", s, flags=re.IGNORECASE)
    return HOME_RE.sub("<HOME>", s)


def redact(log_dir: Path) -> int:
    changed = 0
    for p in sorted(log_dir.glob("*.eval")):
        log = read_eval_log(str(p))
        raw = log.model_dump_json()
        red = redact_text(raw)
        if red != raw:
            write_eval_log(EvalLog.model_validate_json(red), str(p))
            changed += 1
    return changed


def _core(log) -> list:
    return [(s.id, s.input if isinstance(s.input, str) else [m.text for m in s.input],
             s.output.completion if s.output else None,
             {k: (v.value, v.explanation) for k, v in (s.scores or {}).items()})
            for s in (log.samples or [])]


def verify(log_dir: Path, backup: Path) -> bool:
    ok = True
    for p in sorted(log_dir.glob("*.eval")):
        a, b = read_eval_log(str(p)), read_eval_log(str(backup / p.name))
        if _core(a) != _core(b) or a.eval.run_id != b.eval.run_id or a.eval.task_args != b.eval.task_args:
            print("MISMATCH:", p.name)
            ok = False
        if HOME_RE.search(a.model_dump_json()):
            print("STILL CONTAINS A USER PATH:", p.name)
            ok = False
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--verify", metavar="BACKUP_DIR")
    args = ap.parse_args()
    if args.verify:
        good = verify(ROOT / "logs", Path(args.verify))
        print("verified: content identical, no user paths" if good else "verification FAILED")
        return 0 if good else 1
    print(f"Redacted {redact(ROOT / 'logs')} log(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
