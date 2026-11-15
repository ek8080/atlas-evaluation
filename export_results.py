"""Export Inspect AI evaluation logs to CSV plus summary tables.

Usage:
    python export_results.py                    # logs from the most recent run in logs/
    python export_results.py logs\\file1.eval logs\\file2.eval [--out results]

Denominators (stated explicitly, nothing silently excluded):
    attempted        = every sample in the logs, including errored ones.
    attack success   = succeeded / attempted adversarial samples
                       (inconclusive and errored samples count as "not succeeded" but are
                       reported separately; a conclusive-only rate is also given).
    task completion  = completed / attempted samples of that group.
"""

from __future__ import annotations

import argparse
import csv
import json
import platform
import sys
from collections import defaultdict
from importlib import metadata as importlib_metadata
from pathlib import Path

from inspect_ai.log import list_eval_logs, read_eval_log

from scoring import SCORING_VERSION, apply_truncation, score_case

ROOT = Path(__file__).resolve().parent
CSV_FIELDS = [
    "run_id", "timestamp", "model", "model_version_reported", "served_by", "stop_reason", "test_id",
    "atlas_technique", "category",
    "configuration",
    "model_response", "attack_outcome", "legit_task_outcome", "marker_status",
    "detector_false_positive", "scoring_explanation", "error_status", "log_file",
    "scoring_version", "criteria_source",
]


def _pkg_versions() -> dict:
    out = {"python": sys.version.split()[0], "platform": platform.platform()}
    for pkg in ("inspect-ai", "openai", "anthropic", "google-genai", "python-dotenv"):
        try:
            out[pkg] = importlib_metadata.version(pkg)
        except importlib_metadata.PackageNotFoundError:
            pass
    return out


def _basename(path: str) -> str:
    """File name of a log given as a plain path or a file:// URI."""
    return Path(str(path).split("://", 1)[-1]).name


def _log_dataset(h) -> str:
    return ((h.eval.task_args or {}).get("dataset")) or "atlas_tests.json"


def latest_run_logs(log_dir: Path, model: str | None = None, dataset: str | None = None) -> list[str]:
    """Logs of the most recent run (optionally of one model and/or dataset), plus (recursively)
    the logs it was a --retry-errors of."""
    infos = list_eval_logs(str(log_dir))
    if not infos:
        raise SystemExit(f"No .eval logs found in {log_dir}")
    headers = {i.name: read_eval_log(i.name, header_only=True) for i in infos}
    by_file = {_basename(n): n for n in headers}
    candidates = [h for h in headers.values()
                  if (model is None or h.eval.model == model)
                  and (dataset is None or _log_dataset(h) == dataset)]
    if not candidates:
        raise SystemExit(f"No logs for model={model} dataset={dataset} in {log_dir}")
    latest = max(candidates, key=lambda h: h.eval.created).eval.run_id
    chosen = [n for n, h in headers.items() if h.eval.run_id == latest]
    queue = list(chosen)
    while queue:
        h = headers[queue.pop()]
        for f in (h.eval.metadata or {}).get("retry_of") or []:
            n = by_file.get(f)
            if n and n not in chosen:
                chosen.append(n)
                queue.append(n)
    return chosen


def merge_attempts(rows: list[dict]) -> tuple[list[dict], int]:
    """One row per (test_id, configuration): the latest successful attempt, else the latest attempt.

    Returns (final_rows, number of errored attempts superseded by a later successful retry).
    """
    groups: dict = defaultdict(list)
    for r in rows:
        groups[(r["test_id"], r["configuration"])].append(r)
    final, superseded = [], 0
    for attempts in groups.values():
        attempts.sort(key=lambda r: r["timestamp"])
        ok = [r for r in attempts if r["error_status"] == "ok"]
        chosen = ok[-1] if ok else attempts[-1]
        chosen["attempts"] = len(attempts)
        superseded += sum(r["error_status"] != "ok" for r in attempts) if ok else 0
        final.append(chosen)
    return final, superseded


def errored_samples_in_latest_run(log_dir: Path, model: str | None = None,
                                  dataset: str | None = None, include_truncated: bool = False,
                                  only: list[str] | None = None) -> dict:
    """Sample IDs whose final merged attempt in the latest run chain is still an error.

    include_truncated: also select samples whose final attempt is inconclusive because the
    response was cut off at max_tokens. only: restrict the selection to these sample IDs.
    """
    paths = latest_run_logs(log_dir, model, dataset)
    rows, infos = [], []
    for p in paths:
        r, i = rows_from_log(p)
        rows += r
        infos.append(i)
    final, _ = merge_attempts(rows)
    newest = max(infos, key=lambda i: i["created"])
    ta = newest["task_args"] or {}
    return {
        "run_id": newest["run_id"],
        "model": newest["model"],
        "smoke": bool(ta.get("smoke", False)),
        "max_tokens": int(ta.get("max_tokens", 400)),
        "temperature": ta.get("temperature", 0.0),  # may be None (omitted for that model)
        "model_args": newest.get("model_args") or {},
        "dataset": ta.get("dataset") or "atlas_tests.json",
        "log_files": [i["log_file"] for i in infos],
        "sample_ids": sorted(
            sid for r in final
            for sid in [f"{r['test_id']}-{r['configuration']}"]
            if (r["error_status"] != "ok"
                or (include_truncated and r["stop_reason"] == "max_tokens"
                    and r["attack_outcome"] == "inconclusive"))
            and (not only or sid in only)),
    }


def rows_from_log(path: str, criteria: str = "logged") -> tuple[list[dict], dict]:
    """Rows for one log. criteria='logged' scores each sample against the case copy stored in
    the log (reproduces the run exactly); 'current' uses the dataset file as it is now, so a
    documented criteria correction applies. Every row records which was used."""
    if criteria not in ("logged", "current"):
        raise ValueError("criteria must be 'logged' or 'current'")
    log = read_eval_log(path)
    ev = log.eval
    info = {
        "log_file": _basename(path),
        "run_id": ev.run_id, "created": ev.created, "model": ev.model, "status": log.status,
        "model_args": ev.model_args or {},
        "task": ev.task, "task_args": ev.task_args,
        "generate_config": log.plan.config.model_dump(exclude_none=True) if log.plan else {},
        "inspect_packages": ev.packages,
        "dataset_sha256": (ev.metadata or {}).get("dataset", {}).get("sha256"),
        "config": (ev.metadata or {}).get("config"),
        "error": log.error.message if log.error else None,
        "criteria_source": criteria,
        "criteria_overrides": [],
    }
    rows = []
    for s in log.samples or []:
        md = s.metadata or {}
        score = next(iter((s.scores or {}).values()), None)
        sm = (score.metadata if score else None) or {}
        err = s.error.message.strip().splitlines()[0][:300] if s.error else ""
        explanation = score.explanation if score else None
        # Re-score from the raw response with the current scoring.py (version recorded per row),
        # so every model is judged by identical rules even if the scorer was fixed after a run.
        # The raw response is never altered; Inspect's own score stays in the .eval log.
        stop_reason = s.output.stop_reason if s.output is not None and s.output.choices else ""
        if not err and md.get("case") and s.output is not None:
            case = md["case"]
            if criteria == "current":
                cur = _current_cases(info["task_args"]).get(case["id"])
                if cur is not None and cur != case:
                    if case["id"] not in info["criteria_overrides"]:
                        info["criteria_overrides"].append(case["id"])
                    case = cur
            sm = apply_truncation(score_case(case, s.output.completion), stop_reason == "max_tokens")
            explanation = sm["explanation"]
        rows.append({
            "run_id": ev.run_id,
            "timestamp": ev.created,
            "model": ev.model,
            "model_version_reported": s.output.model if s.output and s.output.model else "",
            "served_by": _served_by(s),
            "stop_reason": stop_reason,
            "test_id": md.get("test_id"),
            "atlas_technique": md.get("atlas_technique") or "",
            "category": md.get("category"),
            "configuration": md.get("config"),
            "model_response": s.output.completion if s.output else "",
            # Unscored (errored) adversarial samples are recorded as inconclusive, never dropped.
            "attack_outcome": sm.get("attack_outcome") or
                              ("inconclusive" if md.get("atlas_technique") else "not_applicable"),
            "legit_task_outcome": sm.get("legit_outcome", "not_completed"),
            "marker_status": sm.get("marker_status", ""),
            "detector_false_positive": sm.get("detector_false_positive", False),
            "scoring_explanation": explanation or f"Not scored: sample error ({err})",
            "error_status": f"error: {err}" if err else "ok",
            "log_file": info["log_file"],
            "scoring_version": sm.get("scoring_version", SCORING_VERSION),
            "criteria_source": criteria,
        })
    return rows, info


_CASE_CACHE: dict[str, dict] = {}


def _current_cases(task_args: dict | None) -> dict:
    """Cases of the run's dataset file as it is now, keyed by id ({} if unavailable)."""
    name = ((task_args or {}).get("dataset")) or "atlas_tests.json"
    if name not in _CASE_CACHE:
        try:
            raw = json.loads((ROOT / name).read_text(encoding="utf-8"))
            _CASE_CACHE[name] = {c["id"]: c for c in raw.get("cases", [])}
        except (OSError, ValueError):
            _CASE_CACHE[name] = {}
    return _CASE_CACHE[name]


def _served_by(sample) -> str:
    """Inference host named in the last raw model response (OpenRouter returns 'provider')."""
    for ev in reversed(sample.events or []):
        if getattr(ev, "event", None) == "model" and getattr(ev, "call", None) is not None:
            resp = ev.call.response or {}
            return str(resp.get("provider") or "")
    return ""


def _rate(n: int, d: int) -> str:
    return f"{n}/{d} ({100 * n / d:.1f}%)" if d else "n/a (0 samples)"


def summarise(rows: list[dict]) -> dict:
    out: dict = {"by_config": {}, "by_category": {}}
    groups: dict = defaultdict(list)
    for r in rows:
        groups[r["configuration"]].append(r)
    for cfg, rs in sorted(groups.items()):
        adv = [r for r in rs if r["category"] != "benign_control"]
        ctl = [r for r in rs if r["category"] == "benign_control"]
        succ = sum(r["attack_outcome"] == "succeeded" for r in adv)
        fail = sum(r["attack_outcome"] == "failed" for r in adv)
        inc = sum(r["attack_outcome"] == "inconclusive" for r in adv)
        errs = sum(r["error_status"] != "ok" for r in rs)
        out["by_config"][cfg] = {
            "attempted": len(rs),
            "completed_without_error": len(rs) - errs,
            "errors": errs,
            "adversarial_attempted": len(adv),
            "attack_succeeded": succ, "attack_failed": fail, "attack_inconclusive": inc,
            "attack_success_rate": _rate(succ, len(adv)),
            "attack_success_rate_conclusive_only": _rate(succ, succ + fail),
            "adversarial_legit_completion": _rate(sum(r["legit_task_outcome"] == "completed" for r in adv), len(adv)),
            "controls_attempted": len(ctl),
            "control_legit_completion": _rate(sum(r["legit_task_outcome"] == "completed" for r in ctl), len(ctl)),
            "control_detector_false_positives": sum(bool(r["detector_false_positive"]) for r in ctl),
            "_succ": succ, "_adv": len(adv),
        }
        for r in rs:
            key = (r["category"], cfg)
            c = out["by_category"].setdefault(f"{key[0]}|{key[1]}", {"category": key[0], "config": cfg,
                                                                   "n": 0, "succeeded": 0, "failed": 0,
                                                                   "inconclusive": 0, "legit_completed": 0,
                                                                   "errors": 0})
            c["n"] += 1
            c["legit_completed"] += r["legit_task_outcome"] == "completed"
            c["errors"] += r["error_status"] != "ok"
            if r["attack_outcome"] in ("succeeded", "failed", "inconclusive"):
                c[r["attack_outcome"]] += 1
    b, m = out["by_config"].get("baseline"), out["by_config"].get("mitigated")
    if b and m and b["_adv"] and m["_adv"]:
        rb, rm = b["_succ"] / b["_adv"], m["_succ"] / m["_adv"]
        out["comparison"] = {"baseline_asr": round(rb, 4), "mitigated_asr": round(rm, 4),
                             "absolute_change_pp": round(100 * (rm - rb), 1)}
        # Paired per-case view: which test IDs changed outcome between configurations.
        by_id = defaultdict(dict)
        for r in rows:
            if r["category"] != "benign_control":
                by_id[r["test_id"]][r["configuration"]] = r["attack_outcome"]
        out["comparison"]["paired_changes"] = {tid: v for tid, v in sorted(by_id.items())
                                               if v.get("baseline") != v.get("mitigated")}
    return out


def summary_markdown(summary: dict, infos: list[dict]) -> str:
    L = ["# Results summary", ""]
    i0 = infos[0]
    L += [f"- Run ID: `{i0['run_id']}`", f"- Model: `{i0['model']}`", f"- Run date: {i0['created']}",
          f"- Generation config: `{json.dumps(i0['generate_config'])}`",
          f"- Dataset sha256: `{i0['dataset_sha256']}`",
          f"- Log files: {', '.join('`'+i['log_file']+'`' for i in infos)}", ""]
    L += ["## By configuration", "",
          "| Metric | " + " | ".join(summary["by_config"]) + " |",
          "|---|" + "---|" * len(summary["by_config"])]
    keys = ["attempted", "completed_without_error", "errors", "adversarial_attempted", "attack_succeeded",
            "attack_failed", "attack_inconclusive", "attack_success_rate", "attack_success_rate_conclusive_only",
            "adversarial_legit_completion", "controls_attempted", "control_legit_completion",
            "control_detector_false_positives"]
    for k in keys:
        L.append(f"| {k} | " + " | ".join(str(v[k]) for v in summary["by_config"].values()) + " |")
    L += ["", "Attack success rate denominator = all attempted adversarial samples in that configuration "
          "(errored and inconclusive samples included as not-succeeded).", "",
          f"Errored attempts later replaced by a successful `--retry-errors` attempt: "
          f"{summary.get('errored_attempts_superseded_by_retry', 0)} (all attempts are in `all_attempts.csv`).", "",
          "## By category", "", "| Category | Config | n | Attack succeeded | Failed | Inconclusive | Legit completed | Errors |",
          "|---|---|---|---|---|---|---|---|"]
    for c in summary["by_category"].values():
        L.append(f"| {c['category']} | {c['config']} | {c['n']} | {c['succeeded']} | {c['failed']} | "
                 f"{c['inconclusive']} | {c['legit_completed']} | {c['errors']} |")
    if "comparison" in summary:
        cmp_ = summary["comparison"]
        L += ["", "## Baseline vs mitigated", "",
              f"Attack success rate: baseline {100*cmp_['baseline_asr']:.1f}% -> mitigated "
              f"{100*cmp_['mitigated_asr']:.1f}% (change {cmp_['absolute_change_pp']:+.1f} percentage points).", "",
              "Per-case outcome changes: " + (", ".join(f"{k}: {v.get('baseline')} -> {v.get('mitigated')}"
                                                       for k, v in cmp_["paired_changes"].items()) or "none")]
    return "\n".join(L) + "\n"


def export(log_paths: list[str], out_dir: Path, criteria: str | None = None) -> dict:
    """Export logs to out_dir. criteria: 'logged' (default for a new folder) or 'current'.
    If None, reuse the choice recorded in out_dir/summary.json, so a later re-export (for
    example after --retry-errors) never silently switches criteria."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if criteria is None:
        try:
            criteria = json.loads((out_dir / "summary.json").read_text(encoding="utf-8")).get(
                "scoring", {}).get("criteria_source", "logged")
        except (OSError, ValueError):
            criteria = "logged"
    rows, infos = [], []
    for p in log_paths:
        r, i = rows_from_log(p, criteria)
        rows += r
        infos.append(i)
    # Every attempt (including errored ones later retried) is kept for audit.
    with open(out_dir / "all_attempts.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: (r["configuration"] or "", r["test_id"] or "", r["timestamp"])))
    rows, superseded = merge_attempts(rows)
    rows.sort(key=lambda r: (r["configuration"] or "", r["test_id"] or ""))
    with open(out_dir / "results.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS + ["attempts"])
        w.writeheader()
        w.writerows(rows)
    summary = summarise(rows)
    summary["errored_attempts_superseded_by_retry"] = superseded
    for v in summary["by_config"].values():
        v.pop("_succ"), v.pop("_adv")
    overrides = sorted({c for i in infos for c in i["criteria_overrides"]})
    scoring = {"scoring_version": SCORING_VERSION, "criteria_source": criteria,
               "criteria_overridden_cases": overrides}
    run_info = {"logs": infos, "environment": _pkg_versions(), "scoring": scoring, "summary": summary}
    (out_dir / "summary.json").write_text(json.dumps(run_info, indent=2, default=str), encoding="utf-8")
    note = (f"\n## Scoring\n\nScoring rules version {SCORING_VERSION}; task criteria taken from the "
            + ("case copies stored in the logs (exact reproduction of each run)." if criteria == "logged" else
               "current dataset file. Cases whose criteria differ from the logged copy: "
               + (", ".join(overrides) or "none") + ".") + "\n")
    (out_dir / "summary.md").write_text(summary_markdown(summary, infos) + note, encoding="utf-8")
    print(f"Exported {len(rows)} rows from {len(infos)} log(s) to {out_dir / 'results.csv'}")
    print(f"Summary: {out_dir / 'summary.md'}")
    return run_info


def compare_models(results_root: Path) -> list[dict]:
    """Cross-model table from every results/<model>/summary.json (mock excluded).

    Writes results/model_comparison.md and .json. Only fully completed runs (no errored final
    samples) are marked complete; rates are never pooled across models.
    """
    results_root = Path(results_root)
    entries = []
    for sj in sorted(results_root.glob("*/summary.json")):
        if sj.parent.name == "mock":
            continue
        d = json.loads(sj.read_text(encoding="utf-8"))
        s, logs = d["summary"], d["logs"]
        if any((lg.get("task_args") or {}).get("smoke") for lg in logs):
            print(f"Skipping {sj.parent.name} in comparison: smoke-test run only.")
            continue
        bc = s["by_config"]
        b, m = bc.get("baseline", {}), bc.get("mitigated", {})
        changes = s.get("comparison", {}).get("paired_changes", {})
        gen = (logs[0].get("generate_config") if logs else {}) or {}
        # Complete = both configurations present, no errored final samples, and exactly the
        # expected number of samples (one per dataset case) in each configuration.
        expected = len(_current_cases((logs[0].get("task_args") if logs else None)))
        complete = (set(bc) == {"baseline", "mitigated"} and expected > 0
                    and all(c.get("errors", 0) == 0 and c.get("attempted") == expected for c in bc.values()))
        entries.append({
            "model": logs[0]["model"] if logs else sj.parent.name,
            "results_dir": sj.parent.name,
            "temperature": gen.get("temperature", "omitted"),
            "max_tokens": gen.get("max_tokens"),
            "route": ((logs[0].get("model_args") or {}).get("provider") or {}).get("only") if logs else None,
            "run_ids": sorted({lg["run_id"] for lg in logs}),
            "complete": complete,
            "expected_per_config": expected,
            "attempted": {k: v.get("attempted") for k, v in bc.items()},
            "scoring": d.get("scoring", {}),
            "baseline_asr": b.get("attack_success_rate"),
            "mitigated_asr": m.get("attack_success_rate"),
            "baseline_legit_adv": b.get("adversarial_legit_completion"),
            "mitigated_legit_adv": m.get("adversarial_legit_completion"),
            "baseline_controls": b.get("control_legit_completion"),
            "mitigated_controls": m.get("control_legit_completion"),
            "paired_changes": changes,
        })
    L = ["# Cross-model comparison", "",
         "Each row is a separate run; rates are per model and never pooled. Same cases, prompts and",
         "scorer for every model. Generation settings differ where a model requires it (see columns).",
         "Complete = both configurations present, no errored samples, and one sample per dataset case.", "",
         "| Model | Host | Complete | Temp | Max tokens | Baseline ASR | Mitigated ASR | Task done (attacks) B / M | Controls B / M | Per-case changes |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    for e in entries:
        ch = ", ".join(f"{k}: {v.get('baseline')}->{v.get('mitigated')}" for k, v in e["paired_changes"].items()) or "none"
        host = ", ".join(e["route"]) if e.get("route") else "vendor API"
        L.append(f"| `{e['model']}` | {host} | {'yes' if e['complete'] else 'NO'} | {e['temperature']} | {e['max_tokens']} | "
                 f"{e['baseline_asr']} | {e['mitigated_asr']} | {e['baseline_legit_adv']} / {e['mitigated_legit_adv']} | "
                 f"{e['baseline_controls']} / {e['mitigated_controls']} | {ch} |")
    (results_root / "model_comparison.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    (results_root / "model_comparison.json").write_text(json.dumps(entries, indent=2, default=str), encoding="utf-8")
    print(f"Cross-model comparison ({len(entries)} model(s)): {results_root / 'model_comparison.md'}")
    return entries


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("logs", nargs="*", help="log files (default: most recent run in logs/)")
    ap.add_argument("--out", help="output dir (default: results/<provider>_<model>/ of that run)")
    ap.add_argument("--compare", action="store_true", help="only rebuild the model_comparison.md tables")
    ap.add_argument("--criteria", choices=["logged", "current"], default=None,
                    help="task criteria for re-scoring: 'logged' = case copies stored in the logs "
                         "(exact reproduction); 'current' = the dataset file now (applies documented "
                         "corrections). Default: whatever the output folder used before, else 'logged'.")
    args = ap.parse_args()
    if args.compare:
        compare_models(ROOT / "results")
        for sub in sorted(p for p in (ROOT / "results").iterdir()
                          if p.is_dir() and any(p.glob("*/summary.json"))):
            compare_models(sub)
        return
    logs = args.logs or latest_run_logs(ROOT / "logs")
    if args.out:
        out = Path(args.out)
    else:
        from atlas_eval import results_dir_for
        h = read_eval_log(logs[0], header_only=True)
        out = results_dir_for(h.eval.model, _log_dataset(h))
    export(logs, out, args.criteria)
    if out.name != "mock" and (out.parent == ROOT / "results" or out.parent.parent == ROOT / "results"):
        compare_models(out.parent)


if __name__ == "__main__":
    main()
