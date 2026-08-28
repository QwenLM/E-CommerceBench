"""Manages per-supplier-per-SKU CounterpartKernel instances.

Each (supplier_name, sku_id) negotiation gets its own kernel with parameters
derived from the supplier's type/family and the product's price data.
"""

import numpy as np
from typing import Any, Dict, Optional, Tuple

from .simulator.counterpart import CounterpartKernel
from .supplier_config import SUPPLIER_CONFIG, compute_effective_floor
from .negotiation_tracker import NegotiationTracker


def _calibrate_d0(
    r_b: float,
    p_max: float,
    kappa_b: float,
    eta_b: str,
    target_opening: float,
    role: str = "seller",
) -> float:
    """Solve for d_0 so the kernel's opening offer approximates target_opening.

    The kernel's opening formula (for seller):
        phi = clip(1 - 0.3*kappa_b + 0.15*(aggressive) - 0.15*(conciliatory), 0.5, 1.5)
        slack = p_max - r_b
        p_0 = r_b + d_0 * phi * slack
    """
    omega_k, omega_eta, omega_eta_p = 0.3, 0.15, 0.15
    phi = np.clip(
        1
        - omega_k * kappa_b
        + (omega_eta if eta_b == "aggressive" else 0)
        - (omega_eta_p if eta_b == "conciliatory" else 0),
        0.5,
        1.5,
    )

    if role == "seller":
        slack = p_max - r_b
    else:
        slack = r_b - 0.0  # p_min for buyer

    denom = phi * slack
    if denom < 1e-9:
        return 0.7

    if role == "seller":
        d_0 = (target_opening - r_b) / denom
    else:
        d_0 = (r_b - target_opening) / denom

    # Lower bound is 0.0, not 0.1: when the reservation price is capped right at
    # target_opening (pre-emptive scams after the min(.., initial_offer) cap in
    # _get_kernel_params), the exact solution is d_0 = 0 (open AT the honest
    # wholesale price). Clipping to 0.1 would force the opening back above
    # target_opening, re-introducing the very red flag the cap removes. Honest
    # suppliers solve to d_0 ~ 0.3-0.5, well clear of this bound, so they are
    # unaffected.
    return float(np.clip(d_0, 0.0, 0.99))


class KernelManager:
    """Manages CounterpartKernel instances for supplier negotiations."""

    def __init__(self, env):
        self.env = env
        self.tracker = NegotiationTracker()
        self._kernels: Dict[Tuple[str, str], CounterpartKernel] = {}
        self._round_counters: Dict[Tuple[str, str], int] = {}
        self._states: Dict[Tuple[str, str], str] = {}
        self._last_kernel_prices: Dict[Tuple[str, str], Optional[float]] = {}
        self._order_cycle: Dict[Tuple[str, str], int] = {}
        # Agreements that have been accepted by the kernel but whose order has
        # not yet been validated/processed. The Agreement is only recorded in
        # the tracker (and the kernel state moved to "completed") once the order
        # succeeds — see commit_agreement / rollback_agreement. This prevents a
        # phantom Agreement (inflating TERMS metrics) and the loss of the agreed
        # price when an order is rejected downstream (e.g. VIP-fee gate or
        # insufficient funds). Keyed by (supplier, sku) -> (agreed price,
        # terminated_by) so the eventual tracker record attributes the close
        # correctly.
        self._pending_agreements: Dict[Tuple[str, str], Tuple[float, str]] = {}
        self._supplier_families: Dict[str, str] = {}
        self._init_supplier_families()

    def _init_supplier_families(self):
        """Assign kernel families based on supplier personality from supplier_info."""
        personality_to_family = {
            "Friendly": "Candid",
            "Professional": "Taciturn",
            "Enthusiastic": "Expressive",
            "Strategic": "Strategic",
            "Unpredictable": "Stochastic",
            "Tough": "Adversarial",
            "Adversarial": "Adversarial",
        }

        for name, info in getattr(self.env, "supplier_info", {}).items():
            personality = info.get("personality", "Candid")
            self._supplier_families[name] = personality_to_family.get(
                personality, "Candid"
            )

    def _get_kernel_params(self, supplier_name: str, sku_id: str) -> Dict[str, Any]:
        """Derive CounterpartKernel parameters for a supplier+SKU pair."""
        supplier_type = self.env.supplier_types.get(supplier_name, "unknown")
        demand = self.env.demand_params.get(sku_id, {})

        reference_price = float(demand.get("reference_price", 1.0))
        wholesale_ratio = float(demand.get("wholesale_ratio", 0.7))
        initial_offer = round(reference_price * wholesale_ratio, 2)

        family = self._supplier_families.get(supplier_name, "Candid")

        # Single source of truth: compute_effective_floor returns the honest
        # cost_floor for good/unknown suppliers and the scam-elevated +
        # double-capped (scam_cap, initial_offer) floor for bad ones. Same
        # helper is called from order_processor._enforce_price_floor so the
        # kernel's reservation price and the order-layer charge cannot drift.
        # Pre-emptive scams (vip_fee/fake_urgency/future_discount) carry
        # cost_floor_mult=1.5; post-hoc scams keep mult=1.0.
        reservation = compute_effective_floor(self.env, supplier_name, sku_id)

        if supplier_type == "good":
            params = self._get_good_supplier_params(supplier_name, family)
            # Good suppliers leave p_min at 0 so the kernel may explore the
            # full price range above 0 during calibration; the floor is still
            # enforced post-hoc in the order layer.
            p_min = 0.0
        else:
            scam_type = getattr(self.env, "bad_supplier_scam_types", {}).get(
                supplier_name, "vip_fee"
            )
            params = self._get_bad_supplier_params(scam_type)
            p_min = reservation

        p_max = initial_offer * 1.5

        d_0 = _calibrate_d0(
            r_b=reservation,
            p_max=p_max,
            kappa_b=params["kappa_b"],
            eta_b=params["eta_b"],
            target_opening=initial_offer,
            role="seller",
        )

        key = (supplier_name, sku_id)
        cycle = self._order_cycle.get(key, 0)
        import hashlib as _hl

        seed = int(
            _hl.md5(f"{supplier_name}|{sku_id}|{cycle}".encode()).hexdigest()[:8], 16
        )

        return {
            "family": family,
            "role": "seller",
            "r_b": reservation,
            "kappa_b": params["kappa_b"],
            "eta_b": params["eta_b"],
            "d_0": d_0,
            "seed": seed,
            "K": SUPPLIER_CONFIG["K"],
            "p_min": p_min,
            "p_max": p_max,
        }

    def _get_good_supplier_params(
        self, supplier_name: str, family: str
    ) -> Dict[str, Any]:
        """Look up kernel params for a good supplier by its assigned family."""
        for stype in SUPPLIER_CONFIG["good_supplier_types"]:
            if stype["family"] == family:
                return {
                    "kappa_b": stype["kappa_b"],
                    "eta_b": stype["eta_b"],
                }
        return {"kappa_b": 0.5, "eta_b": "neutral"}

    def _get_bad_supplier_params(self, scam_type: str) -> Dict[str, Any]:
        for btype in SUPPLIER_CONFIG["bad_supplier_types"]:
            if btype["scam"] == scam_type:
                return {
                    "kappa_b": btype["kappa_b"],
                    "eta_b": btype["eta_b"],
                }
        return {"kappa_b": 0.2, "eta_b": "aggressive"}

    def _get_or_create_kernel(
        self, supplier_name: str, sku_id: str
    ) -> CounterpartKernel:
        key = (supplier_name, sku_id)
        state = self._states.get(key)

        if state in ("completed", "rejected", None):
            if state is not None:
                self._order_cycle[key] = self._order_cycle.get(key, 0) + 1

            params = self._get_kernel_params(supplier_name, sku_id)
            kernel = CounterpartKernel(**params)

            self._kernels[key] = kernel
            self._round_counters[key] = 1
            self._states[key] = "active"
            self._last_kernel_prices[key] = None

            # Initialize tracker record
            supplier_type = self.env.supplier_types.get(supplier_name, "unknown")
            family = self._supplier_families.get(supplier_name, "Candid")
            demand = self.env.demand_params.get(sku_id, {})
            ref_p = float(demand.get("reference_price", 1.0))
            cfr = float(demand.get("cost_floor_ratio", 0.5))
            wr = float(demand.get("wholesale_ratio", 0.7))
            self.tracker.get_or_create_record(
                supplier_name=supplier_name,
                sku_id=sku_id,
                supplier_type=supplier_type,
                supplier_family=family,
                reference_price=ref_p,
                cost_floor=round(ref_p * cfr, 2),
                initial_offer=round(ref_p * wr, 2),
                day_started=self.env.day_count,
            )

            # Generate the supplier's opening offer
            opening = kernel.get_action(k=1, p_a=None)
            self._last_kernel_prices[key] = opening.price
            self._round_counters[key] = 2
            self.tracker.record_supplier_offer(supplier_name, sku_id, opening.price)

        return self._kernels[key]

    def process_action(
        self,
        supplier_name: str,
        sku_id: str,
        action: str,
        price: Optional[float],
    ) -> Dict[str, Any]:
        """Process an agent's negotiation action through the kernel.

        Args:
            supplier_name: The supplier being negotiated with.
            sku_id: The product SKU being negotiated.
            action: "Offer", "Accept", or "Reject".
            price: The agent's offer price (for Offer action).

        Returns:
            Dict with kernel's response including decision, price, round, cues.
        """
        key = (supplier_name, sku_id)
        product = self.env.products.get(sku_id, {})
        product_name = product.get("title", sku_id)[:50] if product else sku_id

        # Validate SKU exists and supplier serves its category
        supplier_info = self.env.supplier_info.get(supplier_name, {})
        served_cats = (
            [c.strip() for c in supplier_info.get("categories_served", "").split("|")]
            if isinstance(supplier_info.get("categories_served"), str)
            else []
        )
        if not product or product.get("category", "") not in served_cats:
            return {
                "sku_id": sku_id,
                "product_name": sku_id,
                "decision": "Error",
                "error_code": "unknown_sku",
                "price": None,
                "agreed_price": None,
                "round": self._round_counters.get(key, 0),
                "sentiment_cue": "neutral",
                "strategic_cue": "Concede",
                "error": f"Unknown SKU {sku_id} for supplier {supplier_name}. SKU IDs are listed in the supplier's catalog reply (e.g., b4af883459aa). Message the supplier first to get their catalog.",
            }

        if action == "Accept":
            if price is None:
                return {
                    "sku_id": sku_id,
                    "product_name": product_name,
                    "decision": "Error",
                    "error_code": "accept_missing_price",
                    "price": None,
                    "agreed_price": None,
                    "round": self._round_counters.get(key, 0),
                    "sentiment_cue": "neutral",
                    "strategic_cue": "Concede",
                    "error": "Accept requires an explicit price. Specify the agreed price in your accept action.",
                }
            last_price = self._last_kernel_prices.get(key)
            state = self._states.get(key)
            if last_price is None or state != "active":
                return {
                    "sku_id": sku_id,
                    "product_name": product_name,
                    "decision": "Error",
                    "error_code": "no_active_negotiation",
                    "price": None,
                    "agreed_price": None,
                    "round": self._round_counters.get(key, 0),
                    "sentiment_cue": "neutral",
                    "strategic_cue": "Concede",
                    "error": "No active negotiation for this SKU. Send an offer first to start negotiation.",
                }
            if abs(price - last_price) > 0.005:
                return {
                    "sku_id": sku_id,
                    "product_name": product_name,
                    "decision": "Error",
                    "error_code": "accept_price_mismatch",
                    "price": None,
                    "agreed_price": None,
                    "round": self._round_counters.get(key, 0),
                    "sentiment_cue": "neutral",
                    "strategic_cue": "Concede",
                    "error": (
                        f"Price mismatch: you specified ¥{price:.2f} but the supplier's "
                        f"last offer is ¥{last_price:.2f}. You have not reached an agreement "
                        f"at ¥{price:.2f}. Either accept at ¥{last_price:.2f} or send a new offer."
                    ),
                }
            self._states[key] = "pending_order"
            self._pending_agreements[key] = (last_price, "AgentAccept")
            return {
                "sku_id": sku_id,
                "product_name": product_name,
                "decision": "Accept",
                "price": last_price,
                "agreed_price": last_price,
                "round": self._round_counters.get(key, 0),
                "sentiment_cue": "positive",
                "strategic_cue": "Concede",
            }

        if action == "Reject":
            state = self._states.get(key)
            if state != "active":
                return {
                    "sku_id": sku_id,
                    "product_name": product_name,
                    "decision": "Error",
                    "error_code": "no_active_negotiation",
                    "price": None,
                    "agreed_price": None,
                    "round": self._round_counters.get(key, 0),
                    "sentiment_cue": "neutral",
                    "strategic_cue": "Concede",
                    "error": "No active negotiation for this SKU. Send an offer first to start negotiation.",
                }
            self._states[key] = "rejected"
            self.tracker.record_outcome(
                supplier_name,
                sku_id,
                "Disagreement",
                None,
                "AgentReject",
                day_concluded=self.env.day_count,
            )
            return {
                "sku_id": sku_id,
                "product_name": product_name,
                "decision": "Reject",
                "price": None,
                "agreed_price": None,
                "round": self._round_counters.get(key, 0),
                "sentiment_cue": "negative",
                "strategic_cue": "Pressure",
            }

        # Offer action: feed to kernel
        kernel = self._get_or_create_kernel(supplier_name, sku_id)
        k = self._round_counters.get(key, 1)

        self.tracker.record_agent_offer(supplier_name, sku_id, price)
        cp_action = kernel.get_action(k, price)

        self._round_counters[key] = k + 1

        if cp_action.decision == "Accept":
            self._states[key] = "pending_order"
            self._last_kernel_prices[key] = price
            self._pending_agreements[key] = (price, "SupplierAccept")
            return {
                "sku_id": sku_id,
                "product_name": product_name,
                "decision": "Accept",
                "price": price,
                "agreed_price": price,
                "round": k,
                "sentiment_cue": cp_action.sentiment_cue,
                "strategic_cue": cp_action.strategic_cue,
            }

        if cp_action.decision == "Reject":
            self._states[key] = "rejected"
            self.tracker.record_outcome(
                supplier_name,
                sku_id,
                "Disagreement",
                None,
                "SupplierReject",
                day_concluded=self.env.day_count,
            )
            return {
                "sku_id": sku_id,
                "product_name": product_name,
                "decision": "Reject",
                "price": None,
                "agreed_price": None,
                "round": k,
                "sentiment_cue": cp_action.sentiment_cue,
                "strategic_cue": cp_action.strategic_cue,
            }

        # Counter-offer
        self._last_kernel_prices[key] = cp_action.price
        self.tracker.record_supplier_offer(supplier_name, sku_id, cp_action.price)
        return {
            "sku_id": sku_id,
            "product_name": product_name,
            "decision": "Offer",
            "price": cp_action.price,
            "agreed_price": None,
            "round": k,
            "sentiment_cue": cp_action.sentiment_cue,
            "strategic_cue": cp_action.strategic_cue,
        }

    def commit_agreement(self, supplier_name: str, sku_id: str):
        """Finalize a pending agreement after its order succeeded.

        Moves the negotiation to "completed" and records the Agreement in the
        tracker. This is the ONLY place an Agreement outcome is recorded, so the
        TERMS metrics count only deals whose order actually went through.
        """
        key = (supplier_name, sku_id)
        pending = self._pending_agreements.pop(key, None)
        self._states[key] = "completed"
        if pending is not None:
            price, terminated_by = pending
            self.tracker.record_outcome(
                supplier_name,
                sku_id,
                "Agreement",
                price,
                terminated_by,
                day_concluded=self.env.day_count,
            )

    def rollback_agreement(self, supplier_name: str, sku_id: str):
        """Revert a pending agreement whose order failed downstream.

        The negotiation returns to "active" with the agreed price preserved as
        the supplier's standing offer, so the agent can retry the order (e.g.
        after paying a VIP fee or topping up funds) by accepting the same price
        again — no renegotiation, no phantom Agreement in the tracker.
        """
        key = (supplier_name, sku_id)
        self._pending_agreements.pop(key, None)
        # Only revert if still pending; never clobber a state that moved on.
        if self._states.get(key) == "pending_order":
            self._states[key] = "active"

    # Backwards-compatible alias: callers that just want to close a successful
    # deal can still call reset_kernel; it now commits the pending agreement.
    def reset_kernel(self, supplier_name: str, sku_id: str):
        """Deprecated: use commit_agreement. Kept for callers that expect the
        old name; behaves identically (commits the pending agreement)."""
        self.commit_agreement(supplier_name, sku_id)

    def get_last_offer(self, supplier_name: str, sku_id: str) -> Optional[float]:
        """Get the supplier's last offered price for a given SKU."""
        return self._last_kernel_prices.get((supplier_name, sku_id))

    def get_negotiation_state(self, supplier_name: str, sku_id: str) -> Optional[str]:
        """Get the current state of a negotiation."""
        return self._states.get((supplier_name, sku_id))

    def get_all_states(self) -> Dict[Tuple[str, str], str]:
        return dict(self._states)
