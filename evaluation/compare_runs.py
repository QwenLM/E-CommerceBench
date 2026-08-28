#!/usr/bin/env python3
"""Compare analysis reports across sessions and models.

Usage:
    # Single session (backward-compatible with show_analysis.py)
    python evaluation/compare_runs.py log/<session>/

    # Multiple sessions
    python evaluation/compare_runs.py log/session_a/ log/session_b/

    # Auto-discover sessions matching model patterns under log/
    python evaluation/compare_runs.py log/ --models gpt-5 claude-opus-4-5

    # CSV output
    python evaluation/compare_runs.py log/ --models gpt-5 --csv
"""

import argparse
import csv
import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

METRIC_DEFS: List[Tuple[str, Optional[Tuple[str, ...]]]] = [
    ("final_balance", ("profitability", "final_balance")),
    ("bankrupt", ("profitability", "bankrupt")),
    ("final_day", ("profitability", "final_day")),
    ("peak_drawdown", ("profitability", "peak_drawdown")),
    ("stores / reopens", None),
    ("SE+", ("negotiation_quality", "SE+")),
    ("CSE+", ("negotiation_quality", "CSE+")),
    ("%Oracle", ("negotiation_quality", "%Oracle")),
    ("AGR+", ("negotiation_quality", "AGR+")),
    ("avg_rounds", ("negotiation_quality", "avg_rounds_to_deal")),
    ("money_saved", ("negotiation_quality", "total_money_saved_vs_initial")),
    ("se_half_lift", ("negotiation_quality", "learning_speed", "se_half_lift")),
    ("agr_half_lift", ("negotiation_quality", "learning_speed", "agr_half_lift")),
    (
        "fraud_avoid_lift",
        ("negotiation_quality", "learning_speed", "fraud_avoidance_lift"),
    ),
    ("time_to_zero_bad", ("negotiation_quality", "learning_speed", "time_to_zero_bad")),
    (
        "rounds_improvement",
        ("negotiation_quality", "learning_speed", "rounds_half_improvement"),
    ),
    ("bad_order_share", ("fraud_identification", "bad_supplier_order_share")),
    ("spend_on_bad", ("fraud_identification", "spend_on_bad_supplier")),
    ("spend_bad_share", ("fraud_identification", "spend_on_bad_supplier_share")),
    ("vip_fee_paid", ("fraud_identification", "vip_fee_paid_count")),
    ("on_time_ship", ("fulfilment_quality", "on_time_ship_rate")),
    ("return_rate", ("fulfilment_quality", "realized_return_rate")),
]


def _g(d: Any, *keys: str, default: str = "-") -> Any:
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k, {})
    return d if d not in ({}, None) else default


def _load_session(session_dir: Path) -> List[Tuple[str, Dict]]:
    runs = []
    # Support organized layout (metrics/) and legacy flat layout
    metrics_dir = session_dir / "metrics"
    search_dir = metrics_dir if metrics_dir.is_dir() else session_dir
    for p in sorted(search_dir.glob("run_*_analysis.json")):
        try:
            with open(p, encoding="utf-8") as f:
                runs.append((p.name, json.load(f)))
        except Exception as e:
            print(f"  (skip {p}: {e})", file=sys.stderr)
    return runs


def _extract_value(rep: Dict, path: Optional[Tuple[str, ...]]) -> Any:
    if path is None:
        return f"{_g(rep,'profitability','stores_opened')}/{_g(rep,'profitability','store_reopens')}"
    return _g(rep, *path)


def _mean_std(values: List[float]) -> Tuple[float, float]:
    if not values:
        return (0.0, 0.0)
    m = sum(values) / len(values)
    if len(values) < 2:
        return (m, 0.0)
    var = sum((v - m) ** 2 for v in values) / (len(values) - 1)
    return (m, math.sqrt(var))


def _has_analysis(d: Path) -> bool:
    return bool(
        list((d / "metrics").glob("run_*_analysis.json"))
        or list(d.glob("run_*_analysis.json"))
    )


def _discover_sessions(log_dir: Path, model_patterns: List[str]) -> List[Path]:
    sessions = []
    for d in sorted(log_dir.iterdir()):
        if not d.is_dir():
            continue
        name_lower = d.name.lower()
        if any(pat.lower() in name_lower for pat in model_patterns):
            if _has_analysis(d):
                sessions.append(d)
    return sessions


def render_single(runs: List[Tuple[str, Dict]]) -> None:
    if not runs:
        print("No analysis files found.")
        return
    names = [n for n, _ in runs]
    w0 = max(len(r[0]) for r in METRIC_DEFS) + 2
    wc = max(14, max(len(n) for n in names) + 2)

    header = "metric".ljust(w0) + "".join(
        n.replace("_analysis.json", "").ljust(wc) for n in names
    )
    print(header)
    print("-" * len(header))
    for label, path in METRIC_DEFS:
        cells = []
        for _, rep in runs:
            v = _extract_value(rep, path)
            cells.append(str(v).ljust(wc))
        print(label.ljust(w0) + "".join(cells))


def render_comparison(
    sessions: List[Tuple[str, List[Tuple[str, Dict]]]],
    csv_output: bool = False,
) -> None:
    if not sessions:
        print("No sessions found.")
        return

    metric_labels = [label for label, _ in METRIC_DEFS]

    if csv_output:
        writer = csv.writer(sys.stdout)
        header = ["session", "n_runs"]
        for label in metric_labels:
            header.extend([f"{label}_mean", f"{label}_std"])
        writer.writerow(header)
        for session_name, runs in sessions:
            row = [session_name, len(runs)]
            for label, path in METRIC_DEFS:
                raw = [_extract_value(rep, path) for _, rep in runs]
                nums = []
                for v in raw:
                    try:
                        nums.append(float(v))
                    except (TypeError, ValueError):
                        pass
                if nums:
                    m, s = _mean_std(nums)
                    row.extend([f"{m:.4f}", f"{s:.4f}"])
                else:
                    non_num = raw[0] if raw else "-"
                    row.extend([str(non_num), ""])
            writer.writerow(row)
        return

    session_names = [name for name, _ in sessions]
    w0 = max(len(label) for label, _ in METRIC_DEFS) + 2
    wc = max(20, max(len(n) for n in session_names) + 4)

    header = "metric".ljust(w0) + "".join(n.ljust(wc) for n in session_names)
    print(header)
    print("-" * len(header))
    for label, path in METRIC_DEFS:
        cells = []
        for _, runs in sessions:
            raw = [_extract_value(rep, path) for _, rep in runs]
            nums = []
            for v in raw:
                try:
                    nums.append(float(v))
                except (TypeError, ValueError):
                    pass
            if nums:
                m, s = _mean_std(nums)
                if s > 0:
                    cells.append(f"{m:.4f} ± {s:.4f}".ljust(wc))
                else:
                    cells.append(f"{m:.4f}".ljust(wc))
            else:
                non_num = raw[0] if raw else "-"
                cells.append(str(non_num).ljust(wc))
        print(label.ljust(w0) + "".join(cells))

    print()
    print(
        "Runs per session: "
        + ", ".join(f"{name}={len(runs)}" for name, runs in sessions)
    )


def main():
    parser = argparse.ArgumentParser(
        description="Compare analysis reports across sessions and models."
    )
    parser.add_argument(
        "paths",
        nargs="+",
        help="Session directories or individual analysis JSON files",
    )
    parser.add_argument(
        "--models",
        nargs="+",
        default=None,
        help="Model name substrings to auto-discover sessions under log/",
    )
    parser.add_argument(
        "--csv",
        action="store_true",
        help="Output as CSV instead of a formatted table",
    )
    args = parser.parse_args()

    if args.models:
        all_sessions = []
        for base in args.paths:
            base_path = Path(base)
            if base_path.is_dir():
                all_sessions.extend(_discover_sessions(base_path, args.models))
        if not all_sessions:
            print(f"No sessions matching {args.models} found.", file=sys.stderr)
            return 1
        sessions = [(s.name, _load_session(s)) for s in all_sessions]
        sessions = [(name, runs) for name, runs in sessions if runs]
        render_comparison(sessions, csv_output=args.csv)
    elif len(args.paths) == 1 and Path(args.paths[0]).is_dir():
        p = Path(args.paths[0])
        sub_sessions = [
            d for d in sorted(p.iterdir()) if d.is_dir() and _has_analysis(d)
        ]
        if sub_sessions:
            sessions = [(s.name, _load_session(s)) for s in sub_sessions]
            sessions = [(n, r) for n, r in sessions if r]
            if sessions:
                render_comparison(sessions, csv_output=args.csv)
                return 0
        runs = _load_session(p)
        if runs:
            render_single(runs)
        else:
            print(f"No analysis files found in {p}", file=sys.stderr)
            return 1
    else:
        sessions = []
        for a in args.paths:
            p = Path(a)
            if p.is_dir():
                runs = _load_session(p)
                if runs:
                    sessions.append((p.name, runs))
            elif p.is_file():
                try:
                    with open(p, encoding="utf-8") as f:
                        sessions.append((p.name, [(p.name, json.load(f))]))
                except Exception as e:
                    print(f"  (skip {p}: {e})", file=sys.stderr)
        if len(sessions) == 1:
            render_single(sessions[0][1])
        elif sessions:
            render_comparison(sessions, csv_output=args.csv)
        else:
            print("No analysis files found.", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
