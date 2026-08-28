#!/usr/bin/env python3
"""E-Commerce Bench — Standalone benchmark runner."""

import argparse
import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent.ecommerce_agent import EcommerceBenchAgent


def fmt_metric(value, spec: str = ".4f", default: str = "N/A") -> str:
    """Format a metric that may be absent *or* explicitly null.

    A metric that is undefined for a run (e.g. FAGR- when no adversarial
    supplier was ever negotiated with) is stored as JSON null rather than
    omitted. dict.get's default only covers a missing key, not a present None,
    so formatting the result directly raised TypeError and crashed the summary
    after a fully successful episode.
    """
    if value is None:
        return default
    try:
        return format(value, spec)
    except (TypeError, ValueError):
        return str(value)


def run_single(args, run_index: int = 0) -> dict:
    agent = EcommerceBenchAgent(
        model=args.model,
        max_tokens=args.max_tokens,
        max_turns=args.max_turns,
        initial_balance=args.initial_balance,
        daily_fee=args.daily_fee,
        max_day=args.max_days,
        max_token_capacity=args.max_token_capacity,
        tokenizer_path=args.tokenizer_path,
        log_dir=args.log_dir,
        run_index=run_index,
    )

    if args.job_file:
        with open(args.job_file, "r") as f:
            job = json.loads(f.readline())
    else:
        job = None

    result = agent.run(job)

    reward_meta = result.get("reward_meta", {})
    print("\n=== Episode Complete ===")
    print(
        f'Termination: {result.get("termination_reason")} ({result.get("termination_detail", "N/A")})'
    )
    print(f'Final Day: {reward_meta.get("final_day", "N/A")}')
    print(f'Final Balance: ${reward_meta.get("final_a", "N/A")}')
    print(f'Reward: {reward_meta.get("final_score", "N/A")}')

    neg_metrics = reward_meta.get("negotiation_metrics", {})
    if neg_metrics.get("total_negotiations", 0) > 0:
        total_n = neg_metrics["total_negotiations"]
        total_d = neg_metrics.get("total_deals", 0)
        print("\n--- Negotiation Metrics ---")
        print(f"Negotiations: {total_n}, Deals: {total_d}")
        print(f'Avg Rounds to Deal: {neg_metrics.get("avg_rounds_to_deal", "N/A")}')
        print(
            f'Total Money Saved vs Initial: ${fmt_metric(neg_metrics.get("total_money_saved_vs_initial"), ".2f")}'
        )
        by_type = neg_metrics.get("by_supplier_type", {})
        for stype in ("good", "bad"):
            info = by_type.get(stype, {})
            if info.get("negotiations", 0) > 0:
                print(
                    f'  {stype}: negs={info["negotiations"]}, deals={info["deals"]}, '
                    f'avg_eff={fmt_metric(info.get("avg_efficiency"), ".3f")}'
                )
        tb = neg_metrics.get("terms_bench_metrics", {})
        if tb:
            print("\n--- Terms Bench Metrics ---")
            print(f'  SE+  (Surplus Efficiency)  ↑ : {fmt_metric(tb.get("SE+"))}')
            print(f'  AGR+ (Agreement Rate)      ↑ : {fmt_metric(tb.get("AGR+"))}')
            print(f'  CSE+ (Conditional SE)      ↑ : {fmt_metric(tb.get("CSE+"))}')
            print(f'  FAGR-(False Agreement)     ↓ : {fmt_metric(tb.get("FAGR-"))}')
            print(f'  CritViol                   ↓ : {fmt_metric(tb.get("CritViol"))}')
            print(
                f'  %Oracle                    ↑ : {fmt_metric(tb.get("%Oracle"), ".2f")}%'
            )
    return result


def main():
    parser = argparse.ArgumentParser(description="E-Commerce Bench Runner")
    parser.add_argument(
        "--model",
        type=str,
        required=True,
        help="Model key from models_config.json (e.g. gpt-5, claude-opus-4-5)",
    )
    parser.add_argument(
        "--max-tokens", type=int, default=16384, help="Max tokens per LLM call"
    )
    parser.add_argument(
        "--max-turns", type=int, default=4000, help="Max agent turns per episode"
    )
    parser.add_argument("--max-days", type=int, default=365, help="Max simulation days")
    parser.add_argument(
        "--initial-balance", type=float, default=100000.0, help="Starting bank balance"
    )
    parser.add_argument(
        "--daily-fee", type=float, default=50.0, help="Daily store operating fee"
    )
    parser.add_argument(
        "--max-token-capacity",
        type=int,
        default=128000,
        help="Context window token capacity",
    )
    parser.add_argument(
        "--tokenizer-path",
        type=str,
        default=None,
        help="HuggingFace tokenizer path (default: model name)",
    )
    parser.add_argument(
        "--log-dir", type=str, default=None, help="Log output directory"
    )
    parser.add_argument(
        "--job-file", type=str, default=None, help="Pre-built job JSONL file"
    )
    parser.add_argument("--runs", type=int, default=1, help="Number of parallel runs")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.runs > 1:
        with ThreadPoolExecutor(max_workers=args.runs) as executor:
            futures = {
                executor.submit(run_single, args, i): i for i in range(args.runs)
            }
            for future in as_completed(futures):
                idx = futures[future]
                try:
                    future.result()
                    print(f"Run {idx}/{args.runs} completed.")
                except Exception as e:
                    print(f"Run {idx} failed: {e}")
    else:
        run_single(args, 0)


if __name__ == "__main__":
    main()
