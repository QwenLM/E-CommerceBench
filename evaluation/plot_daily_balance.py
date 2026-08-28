#!/usr/bin/env python3
import argparse
import json
import os
import re
import math
import statistics
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

# -----------------------------------------------------------------------------
# In-file default parameters (edit here; command-line args override)
# -----------------------------------------------------------------------------
DEFAULT_LOG_DIR = Path(__file__).resolve().parent / "log"
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / "log"

# Supports multiple models; each model gets its own overlay figure
DEFAULT_MODELS = []

DEFAULT_TIME_PREFIX = None  # common max time prefix after run_; None = no time filter (requires csv paths)
DEFAULT_OVERLAY_NAME = None  # if empty, generated automatically from model_time_prefix
DEFAULT_OVERLAY_BALANCE = True
DEFAULT_INITIAL_BALANCE = (
    100000.0  # opening balance, used to count runs with final balance < initial balance
)
# -----------------------------------------------------------------------------


def _read_first_three_columns(csv_path: Path) -> pd.DataFrame:
    rows = []
    with csv_path.open("r", encoding="utf-8") as f:
        header = f.readline().strip()
        if not header:
            raise ValueError(f"Empty file: {csv_path}")
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split(",", 5)
            if len(parts) < 3:
                continue
            date_str = parts[0]
            bank_balance_str = parts[1]
            platform_wallet_str = parts[2]
            try:
                bank_val = float(bank_balance_str)
                wallet_val = float(platform_wallet_str)
            except (ValueError, TypeError):
                continue
            # Column 3 is the authoritative total_balance written by the run
            # (bank + wallet + escrow/pending_settlement). It is also what the
            # reward uses. Prefer it; fall back to bank+wallet only if the
            # column is missing/unparseable so escrow isn't silently dropped.
            total_val = bank_val + wallet_val
            if len(parts) >= 4:
                try:
                    total_val = float(parts[3])
                except (ValueError, TypeError):
                    pass
            rows.append(
                {
                    "date": date_str,
                    "balance": bank_val,
                    "platform_wallet": wallet_val,
                    "total_balance": total_val,
                }
            )
    df = pd.DataFrame(rows)
    if df.empty:
        raise ValueError(f"No data rows parsed from {csv_path}")
    return df


def _read_last_date_and_balance(csv_path: Path) -> tuple[str, float]:
    """Returns (last_date_str, last_total_balance). Date is normalized to YYYY-MM-DD for comparison."""
    last_non_empty = None
    with csv_path.open("r", encoding="utf-8") as f:
        header = f.readline()
        if not header:
            raise ValueError(f"Empty file: {csv_path}")
        for line in f:
            line = line.strip()
            if line:
                last_non_empty = line
    if not last_non_empty:
        raise ValueError(f"No data rows found in {csv_path}")
    parts = last_non_empty.split(",", 5)
    if len(parts) < 4:
        raise ValueError(f"Malformed last data line in {csv_path}: {last_non_empty!r}")
    date_str = parts[0].strip()
    try:
        dt = pd.to_datetime(date_str)
        date_str = dt.strftime("%Y-%m-%d")
    except Exception:
        pass
    return date_str, float(parts[3])


def _read_last_bank_balance(csv_path: Path) -> float:
    """Returns the last bank_balance (column 1) to distinguish bankruptcy from in-progress."""
    last_non_empty = None
    with csv_path.open("r", encoding="utf-8") as f:
        f.readline()
        for line in f:
            line = line.strip()
            if line:
                last_non_empty = line
    if not last_non_empty:
        return 0.0
    parts = last_non_empty.split(",", 3)
    return float(parts[1]) if len(parts) >= 2 else 0.0


def _jsonl_assistant_turns_and_tool_calls(
    jsonl_path: Path,
) -> tuple[int, int, dict[str, int]]:
    """Returns (assistant_turns, total_tool_calls, tool_counts).

    tool_counts keys: 'market_search_total', 'supplier_search_total',
                      'chatbox_total', 'chatbox_failed',
                      'operate_memory_total/add/get/update/delete/list'.

    Context-truncation counts are NOT derived here: the truncation notice is
    merged into a chat message, so the count is read separately from the run's
    ``*_messages.jsonl`` ``{"_event":"context_truncation"}`` lines via
    :func:`_read_context_clear_count`.
    """
    assistant_turns = 0
    total_tool_calls = 0
    tool_counts: dict[str, int] = {
        "market_search_total": 0,
        "supplier_search_total": 0,
        "chatbox_total": 0,
        "chatbox_failed": 0,
        "operate_memory_total": 0,
        "operate_memory_add": 0,
        "operate_memory_get": 0,
        "operate_memory_update": 0,
        "operate_memory_delete": 0,
        "operate_memory_list": 0,
    }
    with jsonl_path.open("r", encoding="utf-8") as f:
        for line in f:
            raw = line.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and obj.get("role") == "assistant":
                assistant_turns += 1
                tc = obj.get("tool_calls")
                if isinstance(tc, list):
                    total_tool_calls += len(tc)
            # Count tool-specific calls
            ms_cnt = line.count('"name": "market_search"') + line.count(
                '"name":"market_search"'
            )
            tool_counts["market_search_total"] += ms_cnt
            ss_cnt = line.count('"name": "supplier_search"') + line.count(
                '"name":"supplier_search"'
            )
            tool_counts["supplier_search_total"] += ss_cnt
            cb_cnt = line.count('"name": "chatbox"') + line.count('"name":"chatbox"')
            tool_counts["chatbox_total"] += cb_cnt
            tool_counts["chatbox_failed"] += line.count("supplier_communication_failed")
            # operate_memory: count from the PARSED assistant tool_calls. Substring
            # matching "action":"x" is unreliable — that value also appears in the
            # model's own reasoning/content (models often quote the JSON), and it
            # cannot tell which tool the action belongs to, so it over-counts.
            if (
                isinstance(obj, dict)
                and obj.get("role") == "assistant"
                and isinstance(obj.get("tool_calls"), list)
            ):
                for tc in obj["tool_calls"]:
                    fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                    if fn.get("name") != "operate_memory":
                        continue
                    tool_counts["operate_memory_total"] += 1
                    raw_args = fn.get("arguments", {})
                    if isinstance(raw_args, str):
                        try:
                            raw_args = json.loads(raw_args or "{}")
                        except json.JSONDecodeError:
                            raw_args = {}
                    act = raw_args.get("action") if isinstance(raw_args, dict) else None
                    if act in ("add", "get", "update", "delete", "list"):
                        tool_counts[f"operate_memory_{act}"] += 1
    return assistant_turns, total_tool_calls, tool_counts


def _read_context_clear_count(jsonl_path: Path) -> int:
    """Count context truncations directly from a run's ``*_messages.jsonl``.

    Each truncation pass writes a durable meta-event line
    ``{"_event": "context_truncation", ...}`` into the message log (see
    EcommerceBenchAgent.run). Counting those lines makes the message log the
    single source of truth, so no sibling ``context_stats.json`` is needed.

    Returns 0 when the log is missing (older runs / runs that never overflowed
    the window) or cannot be read.
    """
    if not jsonl_path or not jsonl_path.exists():
        return 0
    count = 0
    try:
        with jsonl_path.open("r", encoding="utf-8") as f:
            for line in f:
                if '"_event"' not in line or "context_truncation" not in line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and obj.get("_event") == "context_truncation":
                    count += 1
    except OSError:
        return 0
    return count


def _compute_balance_variance_stats(last_balances: list[float]) -> dict[str, float]:
    """Compute variance-related statistics for final balances across runs.

    Returns dict with: mean, variance, std, cv (coefficient of variation),
    min, max, range, median, iqr, and a stability_score in [0, 1]
    (1 = perfectly stable, 0 = highly variable relative to initial balance).
    """
    n = len(last_balances)
    if n == 0:
        return {}
    mean = statistics.fmean(last_balances)
    if n == 1:
        return {
            "mean": mean,
            "variance": 0.0,
            "std": 0.0,
            "cv": 0.0,
            "min": mean,
            "max": mean,
            "range": 0.0,
            "median": mean,
            "iqr": 0.0,
            "stability_score": 1.0,
        }

    var = statistics.variance(last_balances)
    std = math.sqrt(var)
    sorted_b = sorted(last_balances)
    median = statistics.median(sorted_b)
    q1 = statistics.median(sorted_b[: n // 2])
    q3 = statistics.median(sorted_b[(n + 1) // 2 :])
    iqr = q3 - q1
    # CV: coefficient of variation (use abs(mean) to handle negative means)
    cv = std / abs(mean) if abs(mean) > 1e-9 else float("inf")
    # Stability score: 1/(1 + cv), bounded [0, 1]; higher = more consistent
    stability_score = 1.0 / (1.0 + cv)

    return {
        "mean": mean,
        "variance": var,
        "std": std,
        "cv": cv,
        "min": sorted_b[0],
        "max": sorted_b[-1],
        "range": sorted_b[-1] - sorted_b[0],
        "median": median,
        "iqr": iqr,
        "stability_score": stability_score,
    }


def _select_runs_from_log_dir(
    log_dir: Path, model: str, time_prefix: str, temperature_filter: str | None = None
) -> list[tuple[Path, Path]]:
    """
    Returns list of (daily_balance_csv, messages_jsonl) pairs.

    New directory structure: log/{YYYYMMDD_HHMMSS}_{model}/run_{idx}_daily_balance.csv
    Selection: session directory timestamp starts with time_prefix AND model part matches.
    """
    dir_pat = re.compile(r"^(?P<ts>\d{8}_\d{6})_(?P<model_block>.+)$")
    pairs: list[tuple[Path, Path]] = []
    for session_dir in sorted(log_dir.iterdir()):
        if not session_dir.is_dir():
            continue
        m = dir_pat.match(session_dir.name)
        if not m:
            continue
        ts = m.group("ts")
        block = m.group("model_block")
        if not ts.startswith(time_prefix):
            continue
        if block != model and not (
            block.startswith(model + "_thinking")
            or block.startswith(model + "-thinking")
        ):
            continue
        if temperature_filter is not None:
            if f"_temp_{temperature_filter}" not in block:
                continue
        # Support organized layout (balance/ + trajectories/) and legacy flat layout
        balance_dir = session_dir / "balance"
        traj_dir = session_dir / "trajectories"
        csv_search = balance_dir if balance_dir.is_dir() else session_dir
        jsonl_search = traj_dir if traj_dir.is_dir() else session_dir
        for csv_path in sorted(csv_search.glob("run_*_daily_balance.csv")):
            jsonl_name = csv_path.name.replace("_daily_balance.csv", "_messages.jsonl")
            jsonl_path = jsonl_search / jsonl_name
            if not jsonl_path.exists():
                jsonl_path = session_dir / jsonl_name
            if not jsonl_path.exists():
                continue
            pairs.append((csv_path, jsonl_path))
    return pairs


def plot_csv(csv_path: Path, output_dir: Path) -> Path:
    df = _read_first_three_columns(csv_path)

    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date")
    # Use the authoritative total_balance (bank + wallet + escrow) so the curve
    # matches the CSV column and the scored final balance.
    df["balance_plus_platform_wallet"] = df["total_balance"]

    fig, ax = plt.subplots(figsize=(10, 6))
    ax.plot(df["date"], df["balance"], label="bank_balance")
    ax.plot(df["date"], df["platform_wallet"], label="platform_wallet")
    ax.plot(df["date"], df["balance_plus_platform_wallet"], label="total_balance")

    ax.set_title(csv_path.stem)
    ax.set_xlabel("date")
    ax.set_ylabel("value")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.08), ncol=3, frameon=True)
    ax.grid(True, linestyle="--", alpha=0.4)
    fig.autofmt_xdate()

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{csv_path.stem}.pdf"
    output_png_path = output_dir / f"{csv_path.stem}.png"
    fig.tight_layout()
    fig.savefig(output_path, format="pdf", bbox_inches="tight")
    fig.savefig(output_png_path, format="png", dpi=300, bbox_inches="tight")
    plt.close(fig)
    return output_path


def plot_balance_overlay(
    csv_paths: list[Path],
    output_dir: Path,
    output_name: str,
    stats_text: str | None = None,
) -> tuple[Path, Path]:
    series_list = []
    for csv_path in csv_paths:
        df = _read_first_three_columns(csv_path)
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date")
        df["source"] = csv_path.stem
        # total_balance column = bank + wallet + escrow (matches scoring).
        df["total"] = df["total_balance"]
        series_list.append(df[["date", "total", "source"]])

    if not series_list:
        raise ValueError("No CSVs provided for overlay plot")

    combined = pd.concat(series_list, ignore_index=True)

    fig, ax = plt.subplots(figsize=(20, 7))
    for source, grp in combined.groupby("source", sort=False):
        ax.plot(grp["date"], grp["total"], label=source, alpha=0.7)

    # Daily average across runs. A run that went bankrupt early has no rows for
    # later dates; those missing runs count as 0 (not excluded), so we divide the
    # daily sum by the total run count rather than by however many have data.
    num_runs = len(series_list)
    avg = combined.groupby("date", as_index=False)["total"].sum().sort_values("date")
    avg["total"] = avg["total"] / num_runs
    last_avg_val = float(avg["total"].iloc[-1]) if not avg.empty else float("nan")
    ax.fill_between(
        avg["date"],
        avg["total"],
        0,
        color="#6E6E73",
        alpha=0.12,
        linewidth=0,
        zorder=1,
    )
    ax.plot(
        avg["date"],
        avg["total"],
        label="daily average",
        color="#6E6E73",
        linewidth=2.0,
        alpha=0.9,
        solid_capstyle="round",
        solid_joinstyle="round",
        zorder=10,
    )

    ax.set_title(output_name)
    ax.set_xlabel("date")
    ax.set_ylabel("total_balance (bank + wallet + escrow)")
    ax.legend(
        fontsize=8,
        ncol=2,
        loc="upper center",
        bbox_to_anchor=(0.5, -0.08),
        frameon=True,
    )
    ax.grid(True, linestyle="--", alpha=0.4)
    fig.autofmt_xdate()

    if stats_text:
        avg_line = f"final-day avg balance: {last_avg_val:.2f}"
        stats_text = f"{avg_line}\n{stats_text}"
        ax.text(
            0.01,
            0.99,
            stats_text,
            transform=ax.transAxes,
            va="top",
            ha="left",
            fontsize=10,
            bbox=dict(facecolor="white", alpha=0.85, edgecolor="#999999"),
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{output_name}.pdf"
    output_png_path = output_dir / f"{output_name}.png"
    fig.tight_layout()
    # fig.savefig(output_path, format="pdf", bbox_inches="tight")
    fig.savefig(output_png_path, format="png", dpi=300, bbox_inches="tight")
    plt.close(fig)
    return output_path, output_png_path


def plot_compare_models(
    model_data: dict[str, list[Path]],
    output_dir: Path,
) -> Path:
    """Plot mean ± std balance curves for multiple models on one chart."""
    import numpy as np

    fig, ax = plt.subplots(figsize=(20, 7))
    colors = plt.cm.tab10.colors

    for idx, (model_name, csv_paths) in enumerate(model_data.items()):
        all_series = {}
        for csv_path in csv_paths:
            df = _read_first_three_columns(csv_path)
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date")
            for _, row in df.iterrows():
                d = row["date"]
                all_series.setdefault(d, []).append(row["total_balance"])

        dates = sorted(all_series.keys())
        means = [np.mean(all_series[d]) for d in dates]
        stds = [np.std(all_series[d]) for d in dates]
        color = colors[idx % len(colors)]

        ax.plot(dates, means, label=model_name, color=color, linewidth=2)
        ax.fill_between(
            dates,
            [m - s for m, s in zip(means, stds)],
            [m + s for m, s in zip(means, stds)],
            color=color,
            alpha=0.15,
        )

    ax.set_title("Cross-model balance comparison (mean ± std)")
    ax.set_xlabel("date")
    ax.set_ylabel("total_balance")
    ax.legend(fontsize=9)
    ax.grid(True, linestyle="--", alpha=0.4)
    fig.autofmt_xdate()

    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / "compare_models.png"
    fig.tight_layout()
    fig.savefig(out_path, format="png", dpi=300, bbox_inches="tight")
    plt.close(fig)
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Plot balance from e-commerce bench logs."
    )
    parser.add_argument(
        "csvs",
        nargs="*",
        type=Path,
        default=[],
        help="CSV paths to plot",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=DEFAULT_LOG_DIR,
        help="Directory containing run_* logs (csv/jsonl)",
    )
    parser.add_argument(
        "--model",
        nargs="*",
        type=str,
        default=DEFAULT_MODELS,
        help="Model name(s) as embedded in filenames; each model gets one overlay plot",
    )
    parser.add_argument(
        "--time-prefix",
        type=str,
        default=DEFAULT_TIME_PREFIX,
        help="Common max time prefix after 'run_' (e.g. 20260301_00)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory to save plots",
    )
    parser.add_argument(
        "--overlay-balance",
        action="store_true",
        default=DEFAULT_OVERLAY_BALANCE,
        help="Plot balance lines from all CSVs on one figure with an average line",
    )
    parser.add_argument(
        "--overlay-name",
        type=str,
        default=DEFAULT_OVERLAY_NAME,
        help="Output filename stem for the overlay plot",
    )
    parser.add_argument(
        "--initial-balance",
        type=float,
        default=DEFAULT_INITIAL_BALANCE,
        help=f"Initial balance to compare final balances against (default: {DEFAULT_INITIAL_BALANCE:g})",
    )
    parser.add_argument(
        "--temperature-filter",
        type=str,
        default=None,
        help="Only include runs with this temperature value in their filename (e.g. '1' matches _temp_1)",
    )
    parser.add_argument(
        "--num-episodes",
        type=int,
        default=None,
        help="Max number of runs to plot; if more are found, keep the latest N (by timestamp). "
        "Also reads from NUM_EPISODES env var if not specified on CLI.",
    )
    parser.add_argument(
        "--compare-models",
        nargs="+",
        type=str,
        default=None,
        help="Plot mean ± std balance for multiple models on one chart. "
        "Requires --time-prefix to select runs from --log-dir.",
    )
    args = parser.parse_args()

    # Resolve num_episodes: CLI > env > unlimited
    if args.num_episodes is None:
        env_ep = os.environ.get("NUM_EPISODES")
        args.num_episodes = int(env_ep) if env_ep else None

    # Ensure args.model is always a list (the argparse default is already a list when --model is not passed)
    models: list[str] = list(args.model) if args.model else []

    # --compare-models: cross-model overlay plot
    if args.compare_models:
        if not args.time_prefix:
            raise SystemExit("--compare-models requires --time-prefix to select runs.")
        model_data = {}
        for m in args.compare_models:
            pairs = _select_runs_from_log_dir(
                args.log_dir, m, args.time_prefix, args.temperature_filter
            )
            if pairs:
                if args.num_episodes and len(pairs) > args.num_episodes:
                    pairs = pairs[-args.num_episodes :]
                model_data[m] = [c for c, _ in pairs]
            else:
                print(f"Warning: no runs for {m!r}, skipping.")
        if model_data:
            out = plot_compare_models(model_data, args.output_dir)
            print(f"Saved cross-model comparison: {out}")
        return

    FINAL_DAY = ["2027-01-01", "2026-12-31"]

    def _process_one_model(
        model: str, csvs: list[Path], jsonls: list[Path], output_name: str
    ) -> None:
        stats_lines: list[str] = []
        last_balances: list[float] = []
        last_dates: list[str] = []
        assistant_turns_list: list[int] = []
        tool_calls_list: list[int] = []
        trunc_counts: list[int] = []
        tool_counts_list: list[dict[str, int]] = []

        _default_tool_counts = {
            "market_search_total": 0,
            "supplier_search_total": 0,
            "chatbox_total": 0,
            "chatbox_failed": 0,
            "operate_memory_total": 0,
            "operate_memory_add": 0,
            "operate_memory_get": 0,
            "operate_memory_update": 0,
            "operate_memory_delete": 0,
            "operate_memory_list": 0,
        }

        for idx, csv_path in enumerate(csvs):
            last_date_str, last_balance = _read_last_date_and_balance(csv_path)
            last_balances.append(last_balance)
            last_dates.append(last_date_str)
            # Truncation count is parsed from the sibling messages.jsonl, which
            # records a {"_event":"context_truncation"} line per truncation pass.
            # Runs without a messages.jsonl simply report 0.
            run_jsonl = jsonls[idx] if idx < len(jsonls) else None
            trunc_counts.append(
                _read_context_clear_count(run_jsonl) if run_jsonl else 0
            )
            if idx < len(jsonls) and jsonls[idx]:
                assistant_turns, total_tool_calls, tool_counts = (
                    _jsonl_assistant_turns_and_tool_calls(jsonls[idx])
                )
                assistant_turns_list.append(assistant_turns)
                tool_calls_list.append(total_tool_calls)
                tool_counts_list.append(tool_counts)
            else:
                assistant_turns_list.append(0)
                tool_calls_list.append(0)
                tool_counts_list.append(dict(_default_tool_counts))

        stats_lines.append(f"runs: {len(csvs)}")
        stats_lines.append(
            f"last total_balance (bank+wallet): avg={statistics.fmean(last_balances):.2f}"
        )
        if assistant_turns_list:
            stats_lines.append(
                f"assistant turns: avg={statistics.fmean(assistant_turns_list):.2f}"
            )
        if tool_calls_list:
            stats_lines.append(
                f"tool calls: avg={statistics.fmean(tool_calls_list):.2f}"
            )
        if trunc_counts:
            stats_lines.append(
                f"context truncations: avg={statistics.fmean(trunc_counts):.2f}"
            )

        # Tool stats
        if tool_counts_list:
            ms_totals = [t["market_search_total"] for t in tool_counts_list]
            ss_totals = [t["supplier_search_total"] for t in tool_counts_list]
            cb_totals = [t["chatbox_total"] for t in tool_counts_list]
            cb_fails = [t["chatbox_failed"] for t in tool_counts_list]
            if any(v > 0 for v in ms_totals):
                stats_lines.append(
                    f"market_search: avg_total={statistics.fmean(ms_totals):.2f}"
                )
            if any(v > 0 for v in ss_totals):
                stats_lines.append(
                    f"supplier_search: avg_total={statistics.fmean(ss_totals):.2f}"
                )
            if any(v > 0 for v in cb_totals):
                stats_lines.append(
                    f"chatbox: avg_total={statistics.fmean(cb_totals):.2f}, "
                    f"avg_failed={statistics.fmean(cb_fails):.2f}"
                )
            om_totals = [t["operate_memory_total"] for t in tool_counts_list]
            if any(v > 0 for v in om_totals):
                om_adds = [t["operate_memory_add"] for t in tool_counts_list]
                om_gets = [t["operate_memory_get"] for t in tool_counts_list]
                om_updates = [t["operate_memory_update"] for t in tool_counts_list]
                om_deletes = [t["operate_memory_delete"] for t in tool_counts_list]
                om_lists = [t["operate_memory_list"] for t in tool_counts_list]
                stats_lines.append(
                    f"operate_memory: avg_total={statistics.fmean(om_totals):.2f} "
                    f"(add={statistics.fmean(om_adds):.1f}, get={statistics.fmean(om_gets):.1f}, "
                    f"update={statistics.fmean(om_updates):.1f}, "
                    f"delete={statistics.fmean(om_deletes):.1f}, list={statistics.fmean(om_lists):.1f})"
                )

        # Variance analysis for final balances
        var_stats = _compute_balance_variance_stats(last_balances)
        if var_stats:
            stats_lines.append(
                f"balance variance: std={var_stats['std']:.2f}, "
                f"cv={var_stats['cv']:.4f}, "
                f"stability={var_stats['stability_score']:.4f}"
            )

        reached_final_idxs = [i for i in range(len(csvs)) if last_dates[i] in FINAL_DAY]
        reached_final_count = len(reached_final_idxs)
        not_final = [i for i in range(len(csvs)) if last_dates[i] not in FINAL_DAY]
        bankrupt_count = 0
        in_progress_count = 0
        for i in not_final:
            bank_bal = _read_last_bank_balance(csvs[i])
            if bank_bal < 0:
                bankrupt_count += 1
            else:
                in_progress_count += 1
        below_initial_idxs = [
            i for i in range(len(csvs)) if last_balances[i] < args.initial_balance
        ]
        below_initial_count = len(below_initial_idxs)

        if model:
            print(f"\n--- Model: {model} ---")
        status_parts = [
            f"{len(csvs)} total",
            f"{reached_final_count} reached final day",
        ]
        if bankrupt_count:
            status_parts.append(f"{bankrupt_count} bankrupt")
        if in_progress_count:
            status_parts.append(f"{in_progress_count} in progress")
        status_parts.append(f"{below_initial_count} below initial balance")
        print(f"Runs: {', '.join(status_parts)}")
        if var_stats:
            print(
                f"Balance: avg={statistics.fmean(last_balances):.2f}, "
                f"std={var_stats['std']:.2f}, "
                f"range=[{var_stats['min']:.2f}, {var_stats['max']:.2f}]"
            )

        stats_text = "\n".join(stats_lines) if stats_lines else None
        if args.overlay_balance:
            pdf_path, png_path = plot_balance_overlay(
                csvs, args.output_dir, output_name, stats_text=stats_text
            )
            print(f"Saved: {png_path}")
            print(f"Saved: {pdf_path}")
        else:
            for csv_path in csvs:
                output_path = plot_csv(csv_path, args.output_dir)
                print(f"Saved: {output_path}")
        if stats_lines:
            print("\n".join(stats_lines))

    # Multi-model mode: select runs per model via log_dir + time_prefix, one figure each
    if models and args.time_prefix and not args.csvs:
        for model in models:
            selected_pairs = _select_runs_from_log_dir(
                args.log_dir, model, args.time_prefix, args.temperature_filter
            )
            if not selected_pairs:
                print(
                    f"Warning: No matching runs for model={model!r}, time_prefix={args.time_prefix!r}, skip."
                )
                continue
            if args.num_episodes and len(selected_pairs) > args.num_episodes:
                print(
                    f"Found {len(selected_pairs)} runs, keeping latest {args.num_episodes}."
                )
                selected_pairs = selected_pairs[-args.num_episodes :]
            csvs = [c for c, _ in selected_pairs]
            jsonls = [j for _, j in selected_pairs]
            output_name = args.overlay_name or f"{model}_{args.time_prefix}"
            _process_one_model(model, csvs, jsonls, output_name)
        return

    # Single-batch mode: explicit csv paths, or no model/time_prefix given
    csvs = list(args.csvs)
    jsonls: list[Path] = []
    output_name = args.overlay_name or "daily_balance"

    if not csvs:
        if models and not args.time_prefix:
            raise SystemExit(
                "When using --model, --time-prefix is required to select runs from log-dir."
            )
        raise SystemExit(
            "No CSV paths provided and no model+time_prefix to select from log-dir."
        )

    for csv_path in csvs:
        jsonl_name = csv_path.name.replace("_daily_balance.csv", "_messages.jsonl")
        jsonl_path = csv_path.with_name(jsonl_name)
        if not jsonl_path.exists():
            traj_candidate = csv_path.parent.parent / "trajectories" / jsonl_name
            if traj_candidate.exists():
                jsonl_path = traj_candidate
        jsonls.append(jsonl_path if jsonl_path.exists() else None)

    _process_one_model("", csvs, jsonls, output_name)


if __name__ == "__main__":
    main()
