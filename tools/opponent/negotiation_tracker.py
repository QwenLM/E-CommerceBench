"""Tracks negotiation metrics across all supplier interactions.

Records per-supplier, per-SKU negotiation state and computes aggregate metrics
matching the Terms Bench evaluation framework (violations, surplus efficiency,
concession analysis).
"""

from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field


@dataclass
class NegotiationRecord:
    supplier_name: str
    sku_id: str
    supplier_type: str
    supplier_family: str
    reference_price: float
    cost_floor: float
    initial_offer: float
    agent_prices: List[float] = field(default_factory=list)
    supplier_prices: List[float] = field(default_factory=list)
    # Total turns exchanged by EITHER party: supplier opening offer, every
    # agent offer, every supplier counter-offer, and the terminal accept/reject.
    # e.g. supplier offer -> agent offer -> supplier counter -> agent accept = 4.
    rounds: int = 0
    outcome: Optional[str] = None  # "Agreement", "Disagreement", "Timeout"
    final_price: Optional[float] = None
    terminated_by: Optional[str] = None
    critical_violations: List[str] = field(default_factory=list)
    secondary_violations: List[str] = field(default_factory=list)
    day_started: Optional[int] = None
    day_concluded: Optional[int] = None


class NegotiationTracker:
    """Tracks and aggregates negotiation metrics."""

    def __init__(self):
        self._records: Dict[Tuple[str, str], NegotiationRecord] = {}
        self._completed: List[NegotiationRecord] = []

    def get_or_create_record(
        self,
        supplier_name: str,
        sku_id: str,
        supplier_type: str,
        supplier_family: str,
        reference_price: float,
        cost_floor: float,
        initial_offer: float,
        day_started: Optional[int] = None,
    ) -> NegotiationRecord:
        key = (supplier_name, sku_id)
        if key not in self._records:
            self._records[key] = NegotiationRecord(
                supplier_name=supplier_name,
                sku_id=sku_id,
                supplier_type=supplier_type,
                supplier_family=supplier_family,
                reference_price=reference_price,
                cost_floor=cost_floor,
                initial_offer=initial_offer,
                day_started=day_started,
            )
        return self._records[key]

    def record_agent_offer(self, supplier_name: str, sku_id: str, price: float):
        key = (supplier_name, sku_id)
        record = self._records.get(key)
        if record is None:
            return
        record.agent_prices.append(price)
        record.rounds += 1
        self._audit_agent_offer(record, price)

    def record_supplier_offer(self, supplier_name: str, sku_id: str, price: float):
        key = (supplier_name, sku_id)
        record = self._records.get(key)
        if record is None:
            return
        record.supplier_prices.append(price)
        # Supplier turn: opening quote or a counter-offer.
        record.rounds += 1

    def record_outcome(
        self,
        supplier_name: str,
        sku_id: str,
        outcome: str,
        final_price: Optional[float],
        terminated_by: str,
        day_concluded: Optional[int] = None,
    ):
        key = (supplier_name, sku_id)
        record = self._records.get(key)
        if record is None:
            return
        record.outcome = outcome
        record.final_price = final_price
        record.terminated_by = terminated_by
        record.day_concluded = day_concluded
        # Terminal turn: the accept/reject by whichever party closed it.
        record.rounds += 1

        self._completed.append(record)
        del self._records[key]

    def _audit_agent_offer(self, record: NegotiationRecord, price: float):
        """Check for violations in the agent's offer (adapted from NegotiationEnv)."""
        # BoundViol: price out of bounds
        # The agent is always the buyer here, so reference_price is the upper bound
        if price > record.reference_price * 1.5:
            record.critical_violations.append("BoundViol")

        # ResViol: offering above reservation price (reference_price for buyer)
        if price > record.reference_price:
            record.critical_violations.append("ResViol")

        # MonoViol: buyer's offers must weakly increase (monotonicity)
        if len(record.agent_prices) >= 2:
            if price < record.agent_prices[-2]:
                record.secondary_violations.append("MonoViol")

    def _compute_record_metrics(self, record: NegotiationRecord) -> Dict[str, Any]:
        """Compute metrics for a single completed negotiation."""
        utility = 0.0
        surplus_efficiency = 0.0

        if record.outcome == "Agreement" and record.final_price is not None:
            utility = record.reference_price - record.final_price
            delta = record.reference_price - record.cost_floor
            if delta > 0:
                surplus_efficiency = utility / delta

        def avg_change(prices):
            if len(prices) < 2:
                return 0.0
            changes = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
            return sum(changes) / len(changes)

        money_saved = 0.0
        if record.outcome == "Agreement" and record.final_price is not None:
            money_saved = record.initial_offer - record.final_price

        return {
            "supplier_name": record.supplier_name,
            "sku_id": record.sku_id,
            "supplier_type": record.supplier_type,
            "supplier_family": record.supplier_family,
            "rounds": record.rounds,
            "outcome": record.outcome,
            "final_price": record.final_price,
            "reference_price": record.reference_price,
            "cost_floor": record.cost_floor,
            "initial_offer": record.initial_offer,
            "agent_utility": round(utility, 4),
            "surplus_efficiency": round(surplus_efficiency, 4),
            "money_saved_vs_initial": round(money_saved, 4),
            "agent_concession_rate": round(avg_change(record.agent_prices), 4),
            "supplier_concession_rate": round(avg_change(record.supplier_prices), 4),
            "critical_violations": list(record.critical_violations),
            "secondary_violations": list(record.secondary_violations),
            "terminated_by": record.terminated_by,
            "day_started": record.day_started,
            "day_concluded": record.day_concluded,
        }

    def _compute_learning_metrics(
        self, per_negotiation: List[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """Compute learning-speed sub-metrics by splitting negotiations into
        early/late halves based on ``day_concluded``.

        Returns None if fewer than 4 good-supplier negotiations have temporal
        data (not enough signal to measure improvement).
        """
        timed = [
            n
            for n in per_negotiation
            if n.get("day_concluded") is not None
            and n["outcome"] in ("Agreement", "Disagreement")
        ]
        if not timed:
            return None

        timed.sort(key=lambda n: n["day_concluded"])
        mid = len(timed) // 2

        good_timed = [n for n in timed if n["supplier_type"] == "good"]
        if len(good_timed) < 4:
            return None

        good_timed.sort(key=lambda n: n["day_concluded"])
        g_mid = len(good_timed) // 2
        g_early, g_late = good_timed[:g_mid], good_timed[g_mid:]

        def _se_values(records):
            return [
                r["surplus_efficiency"] if r["outcome"] == "Agreement" else 0.0
                for r in records
            ]

        def _mean(vals):
            return sum(vals) / len(vals) if vals else 0.0

        def _agr(records):
            deals = [r for r in records if r["outcome"] == "Agreement"]
            return len(deals) / len(records) if records else 0.0

        se_early = _se_values(g_early)
        se_late = _se_values(g_late)
        se_half_lift = round(_mean(se_late) - _mean(se_early), 4)

        agr_half_lift = round(_agr(g_late) - _agr(g_early), 4)

        # OLS slope of SE on day_concluded (good suppliers only)
        se_slope = None
        if len(good_timed) >= 4:
            days = [float(n["day_concluded"]) for n in good_timed]
            ses = _se_values(good_timed)
            mean_d = _mean(days)
            mean_s = _mean(ses)
            num = sum((d - mean_d) * (s - mean_s) for d, s in zip(days, ses))
            den = sum((d - mean_d) ** 2 for d in days)
            se_slope = round(num / den, 6) if den > 0 else 0.0

        # Fraud avoidance: bad-deal rate early vs late (all suppliers)
        early_all, late_all = timed[:mid], timed[mid:]

        def _bad_deal_rate(records):
            bad = [
                r
                for r in records
                if r["supplier_type"] == "bad" and r["outcome"] == "Agreement"
            ]
            return len(bad) / len(records) if records else 0.0

        fa_early = _bad_deal_rate(early_all)
        fa_late = _bad_deal_rate(late_all)
        fraud_avoidance_lift = round(fa_early - fa_late, 4)

        # Time to zero bad: first day after which no more bad deals occur
        bad_deal_days = [
            n["day_concluded"]
            for n in timed
            if n["supplier_type"] == "bad" and n["outcome"] == "Agreement"
        ]
        time_to_zero_bad = max(bad_deal_days) if bad_deal_days else 0

        # Rounds improvement: avg rounds early - avg rounds late (good deals)
        good_deals_timed = [n for n in good_timed if n["outcome"] == "Agreement"]
        if len(good_deals_timed) >= 4:
            gd_mid = len(good_deals_timed) // 2
            gd_early = good_deals_timed[:gd_mid]
            gd_late = good_deals_timed[gd_mid:]
            rounds_half_improvement = round(
                _mean([r["rounds"] for r in gd_early])
                - _mean([r["rounds"] for r in gd_late]),
                2,
            )
        else:
            rounds_half_improvement = None

        # Per-family breakdown (families with >= 4 negotiations)
        from collections import defaultdict

        by_fam: Dict[str, List[Dict]] = defaultdict(list)
        for n in good_timed:
            by_fam[n.get("supplier_family", "unknown")].append(n)

        by_family_learning = {}
        for fam, records in by_fam.items():
            if len(records) < 4:
                continue
            records.sort(key=lambda r: r["day_concluded"])
            fm = len(records) // 2
            f_early, f_late = records[:fm], records[fm:]
            by_family_learning[fam] = {
                "se_half_lift": round(
                    _mean(_se_values(f_late)) - _mean(_se_values(f_early)), 4
                ),
                "agr_half_lift": round(_agr(f_late) - _agr(f_early), 4),
                "count": len(records),
            }

        return {
            "se_half_lift": se_half_lift,
            "se_slope_per_day": se_slope,
            "agr_half_lift": agr_half_lift,
            "fraud_avoidance_lift": fraud_avoidance_lift,
            "time_to_zero_bad": time_to_zero_bad,
            "rounds_half_improvement": rounds_half_improvement,
            "by_family_learning": by_family_learning if by_family_learning else None,
            "sample_size": {
                "good_negotiations": len(good_timed),
                "good_deals": len(
                    [n for n in good_timed if n["outcome"] == "Agreement"]
                ),
                "bad_deals": len(bad_deal_days),
                "total_timed": len(timed),
            },
        }

    def _compute_anchoring_metrics(
        self,
        per_negotiation: List[Dict],
        n_perm: int = 400,
        seed: int = 20260101,
        eps: float = 0.01,
    ) -> Optional[Dict[str, Any]]:
        """Price-anchoring discipline on repeat sourcing.

        For every (supplier, SKU) pair the agent buys from more than once, each
        agreed price is normalised into that pair's bargaining range,

            pi = (price - cost_floor) / (reference_price - cost_floor),

        the deals are ordered by conclusion day, and every re-order after the
        first is scored on two questions:

          AnchorRegret (lower better)  mean of max(0, pi_t - min_{u<t} pi_u):
              how much the agent overpaid relative to the best price it had
              already obtained from that very supplier for that very SKU.
          NewLowRate (higher better)   fraction of re-orders that beat the
              running minimum by more than `eps`, i.e. genuine further descent.

        Both are reported against a within-pair permutation null that keeps each
        pair's price multiset intact and reshuffles only the order in which the
        agent obtained those prices. The z-scores therefore isolate the temporal
        signal from the agent's raw price dispersion: a model whose prices are
        tightly clustered scores a low absolute regret for free, but only a
        model that actually remembers and holds its best-known price beats its
        own null. A pair of length n contributes exactly n - 1 events under
        every permutation, so the null leaves the event set fixed and the two
        distributions are directly comparable.

        Only honest suppliers are used: the floor of a pre-deal fraudulent
        counterpart is deliberately inflated, so descent toward it is not a
        comparable quantity (Sec. fraud design).
        """
        import random
        from collections import defaultdict

        pairs: Dict[Tuple[str, str], List[Tuple[int, float]]] = defaultdict(list)
        for n in per_negotiation:
            if n["outcome"] != "Agreement" or n["supplier_type"] != "good":
                continue
            if n.get("day_concluded") is None:
                continue
            span = n["reference_price"] - n["cost_floor"]
            if span <= 0:
                continue
            pi = (n["final_price"] - n["cost_floor"]) / span
            pairs[(n["supplier_name"], n["sku_id"])].append(
                (n["day_concluded"], max(0.0, min(1.0, pi)))
            )

        # Sorted by day only; ties keep the order the negotiations concluded in.
        seqs = [
            [pi for _, pi in sorted(v, key=lambda x: x[0])]
            for v in pairs.values()
            if len(v) >= 2
        ]
        if not seqs:
            return None

        def _score(sequences):
            regret_sum, lows, events = 0.0, 0, 0
            for seq in sequences:
                running = seq[0]
                for pi in seq[1:]:
                    events += 1
                    regret_sum += max(0.0, pi - running)
                    if pi < running - eps:
                        lows += 1
                    running = min(running, pi)
            if not events:
                return None, None, 0
            return regret_sum / events, lows / events, events

        regret, new_low, events = _score(seqs)
        if not events:
            return None

        rng = random.Random(seed)
        null_regret, null_low = [], []
        for _ in range(n_perm):
            shuffled = []
            for seq in seqs:
                perm = seq[:]
                rng.shuffle(perm)
                shuffled.append(perm)
            r, l, _ = _score(shuffled)
            null_regret.append(r)
            null_low.append(l)

        def _z(observed, null, higher_is_better):
            mean = sum(null) / len(null)
            var = sum((x - mean) ** 2 for x in null) / len(null)
            sd = var**0.5
            if sd <= 0:
                return mean, None
            delta = (observed - mean) if higher_is_better else (mean - observed)
            return mean, round(delta / sd, 3)

        regret_null, regret_z = _z(regret, null_regret, higher_is_better=False)
        low_null, low_z = _z(new_low, null_low, higher_is_better=True)

        return {
            # Headline effect size: regret as a fraction of the shuffled-order
            # regret. Below 1.0 means the order in which the agent actually
            # obtained its prices cost it less than a random order would have.
            # Ratios are formed per episode against that episode's own null, so
            # averaging them across episodes stays sign-consistent with the
            # combined z (a ratio of cross-episode means does not).
            "anchor_regret_ratio": (
                round(regret / regret_null, 4) if regret_null > 0 else None
            ),
            "new_low_rate_ratio": (
                round(new_low / low_null, 4) if low_null > 0 else None
            ),
            # Positive z = better than the agent's own shuffled ordering.
            "anchor_regret": round(regret, 4),
            "anchor_regret_null": round(regret_null, 4),
            "anchor_regret_z": regret_z,
            "new_low_rate": round(new_low, 4),
            "new_low_rate_null": round(low_null, 4),
            "new_low_rate_z": low_z,
            "sample_size": {"repeat_pairs": len(seqs), "reorders": events},
        }

    def get_aggregate_metrics(self) -> Dict[str, Any]:
        """Compute aggregate metrics across all *concluded* negotiations.

        Only negotiations that reached a terminal outcome ('Agreement' or
        'Disagreement') are aggregated. Still-active records (outcome=None,
        e.g. a counter-offer round that was never accepted/rejected before the
        episode ended) must NOT be counted: doing so inflated every denominator
        (total_negotiations, good/bad_negotiations, CritViol) while contributing
        0 to deals/SE/utility, systematically depressing AGR+/SE+/%Oracle and
        diluting CritViol whenever the episode ended with open negotiations.
        """
        all_records = list(self._completed)
        # Include any still-active records so they can be reported as a count,
        # but they are excluded from the aggregated metrics below.
        active_records = list(self._records.values())
        all_records.extend(active_records)

        if not all_records:
            return {"total_negotiations": 0}

        per_negotiation_all = [self._compute_record_metrics(r) for r in all_records]
        # Metrics are computed only over concluded negotiations.
        per_negotiation = [
            n
            for n in per_negotiation_all
            if n["outcome"] in ("Agreement", "Disagreement")
        ]

        if not per_negotiation:
            return {
                "total_negotiations": 0,
                "active_unconcluded": len(active_records),
            }

        deals = [n for n in per_negotiation if n["outcome"] == "Agreement"]
        rejections = [n for n in per_negotiation if n["outcome"] == "Disagreement"]

        total_crit = sum(len(n["critical_violations"]) for n in per_negotiation)
        total_sec = sum(len(n["secondary_violations"]) for n in per_negotiation)

        avg_efficiency = 0.0
        avg_rounds = 0.0
        total_saved = 0.0
        if deals:
            avg_efficiency = sum(d["surplus_efficiency"] for d in deals) / len(deals)
            avg_rounds = sum(d["rounds"] for d in deals) / len(deals)
            total_saved = sum(d["money_saved_vs_initial"] for d in deals)

        good_deals = [d for d in deals if d["supplier_type"] == "good"]
        bad_deals = [d for d in deals if d["supplier_type"] == "bad"]

        good_negotiations = [n for n in per_negotiation if n["supplier_type"] == "good"]
        bad_negotiations = [n for n in per_negotiation if n["supplier_type"] == "bad"]

        # Family distribution metrics
        family_stats = {}
        for n in per_negotiation:
            fam = n.get("supplier_family", "unknown")
            if fam not in family_stats:
                family_stats[fam] = {"count": 0, "deals": 0, "avg_efficiency": []}
            family_stats[fam]["count"] += 1
            if n["outcome"] == "Agreement":
                family_stats[fam]["deals"] += 1
                family_stats[fam]["avg_efficiency"].append(n["surplus_efficiency"])

        for fam, stats in family_stats.items():
            effs = stats.pop("avg_efficiency")
            stats["avg_efficiency"] = round(sum(effs) / len(effs), 4) if effs else 0.0

        # Terms Bench diagnostic axes (arXiv:2605.13909v1)
        # When there are NO good-supplier negotiations, these axes are
        # UNDEFINED, not zero. Returning 0.0 conflated "no good-supplier sample"
        # with "negotiated but captured 0 surplus", which dragged cross-run
        # averages down on runs that only dealt with bad suppliers. Return None
        # (-> null in JSON) so downstream aggregation can skip / weight properly.
        has_good = bool(good_negotiations)

        # AGR+ ↑: agreement rate on good (feasible) suppliers
        agr_plus = (
            len(good_deals) / len(good_negotiations) if good_negotiations else None
        )

        # SE+ ↑: surplus efficiency across ALL good-supplier negotiations
        se_values = []
        abs_se_values = []
        for n in good_negotiations:
            if n["outcome"] == "Agreement" and n["surplus_efficiency"]:
                se_values.append(n["surplus_efficiency"])
                abs_se_values.append(n["agent_utility"])
            else:
                se_values.append(0.0)
                abs_se_values.append(0.0)
        se_plus = sum(se_values) / len(se_values) if se_values else None
        abs_se_plus = sum(abs_se_values) / len(abs_se_values) if abs_se_values else None

        # CSE+ ↑: surplus efficiency only for good-supplier agreements
        cse_values = [d["surplus_efficiency"] for d in good_deals]
        abs_cse_values = [d["agent_utility"] for d in good_deals]
        cse_plus = sum(cse_values) / len(cse_values) if cse_values else None
        abs_cse_plus = (
            sum(abs_cse_values) / len(abs_cse_values) if abs_cse_values else None
        )

        # FAGR- ↓: false agreement rate on bad suppliers (None if no bad negs)
        fagr_minus = (
            len(bad_deals) / len(bad_negotiations) if bad_negotiations else None
        )

        # CritViol ↓: fraction of negotiations with critical violations
        crit_episodes = sum(1 for n in per_negotiation if n["critical_violations"])
        crit_viol = crit_episodes / len(per_negotiation)

        # %Oracle ↑: agent utility as % of oracle utility (good suppliers)
        agent_utils = []
        oracle_utils = []
        for n in good_negotiations:
            delta = n["reference_price"] - n["cost_floor"]
            oracle_utils.append(max(0, delta))
            if n["outcome"] == "Agreement":
                agent_utils.append(n["agent_utility"])
            else:
                agent_utils.append(0.0)
        mean_oracle = sum(oracle_utils) / len(oracle_utils) if oracle_utils else 0.0
        pct_oracle = (
            100.0 * (sum(agent_utils) / len(agent_utils)) / mean_oracle
            if (has_good and mean_oracle > 0)
            else None
        )

        # Null-safe rounding — metrics with no sample are None, not 0.
        def _r(v, nd):
            return round(v, nd) if v is not None else None

        return {
            "total_negotiations": len(per_negotiation),
            "active_unconcluded": len(active_records),
            "total_deals": len(deals),
            "total_rejections": len(rejections),
            "avg_surplus_efficiency": round(avg_efficiency, 4),
            "avg_rounds_to_deal": round(avg_rounds, 2),
            "total_money_saved_vs_initial": round(total_saved, 2),
            "total_violations": {
                "critical": total_crit,
                "secondary": total_sec,
            },
            "terms_bench_metrics": {
                "AGR+": _r(agr_plus, 4),
                "SE+": _r(se_plus, 4),
                "CSE+": _r(cse_plus, 4),
                "AbsSE+": _r(abs_se_plus, 4),
                "AbsCSE+": _r(abs_cse_plus, 4),
                "FAGR-": _r(fagr_minus, 4),
                "CritViol": round(crit_viol, 4),
                "%Oracle": _r(pct_oracle, 2),
            },
            "by_supplier_type": {
                "good": {
                    "negotiations": len(good_negotiations),
                    "deals": len(good_deals),
                    "avg_efficiency": round(
                        sum(d["surplus_efficiency"] for d in good_deals)
                        / max(1, len(good_deals)),
                        4,
                    ),
                },
                "bad": {
                    "negotiations": len(bad_negotiations),
                    "deals": len(bad_deals),
                    "avg_efficiency": round(
                        sum(d["surplus_efficiency"] for d in bad_deals)
                        / max(1, len(bad_deals)),
                        4,
                    ),
                },
            },
            "by_family": family_stats,
            "learning_speed": self._compute_learning_metrics(per_negotiation),
            "anchoring": self._compute_anchoring_metrics(per_negotiation),
            "per_negotiation": per_negotiation,
        }
