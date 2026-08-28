"""
Central configuration for CounterpartKernel parameterization and bad-supplier
scam behavior.

This is the source of truth for:
- Kernel family assignments per supplier type
- Kernel parameter ranges (kappa_b, eta_b, d_0)
- Scam type assignments for bad suppliers
- Negotiation and bankruptcy settings

NOTE: the supplier ROSTER (how many good/bad suppliers exist, per-category
counts) is owned by ``data/store_type_config.py`` (SUPPLIER_CONFIG: total_good,
total_bad) and materialized in ``data/suppliers.csv``. Do not duplicate those
counts here — they drifted out of sync historically.

Calibration target: 3-4 avg rounds per deal (aligned with TERMS bench coverage).
TERMS paper uses: kappa_b ~ Beta(2,2), uniform stance distribution,
equal family coverage, d_0 ~ Uniform(0.20, 0.80).
"""

SUPPLIER_CONFIG = {
    # Good suppliers: balanced across all 6 TERMS bench families.
    # kappa_b values span the Beta(2,2) distribution (mean=0.5).
    # Stances follow TERMS: uniform [conciliatory, neutral, aggressive]
    # for non-Adversarial; Adversarial skews aggressive.
    # Ordered easiest → hardest.  Enthusiastic + Friendly ≈ 50 % of good roster.
    # All kappa_b raised +0.25 vs original calibration; Unpredictable further
    # adjusted down (0.60 instead of raw 0.75) so it ranks 3rd, not 1st.
    "good_supplier_types": [
        {
            "family": "Expressive",
            "count": 5,
            "kappa_b": 0.70,
            "eta_b": "conciliatory",
            "d_0_range": [0.40, 0.70],
        },
        {
            "family": "Candid",
            "count": 5,
            "kappa_b": 0.65,
            "eta_b": "neutral",
            "d_0_range": [0.30, 0.60],
        },
        {
            "family": "Stochastic",
            "count": 3,
            "kappa_b": 0.60,
            "eta_b": "neutral",
            "d_0_range": [0.30, 0.70],
        },
        {
            "family": "Taciturn",
            "count": 3,
            "kappa_b": 0.60,
            "eta_b": "aggressive",
            "d_0_range": [0.35, 0.65],
        },
        {
            "family": "Strategic",
            "count": 2,
            "kappa_b": 0.55,
            "eta_b": "aggressive",
            "d_0_range": [0.45, 0.75],
        },
        {
            "family": "Adversarial",
            "count": 2,
            "kappa_b": 0.50,
            "eta_b": "aggressive",
            "d_0_range": [0.50, 0.80],
        },
    ],
    # Bad suppliers: extreme kernel params + LLM scam overlay.
    # Kernel handles pricing (stubborn, aggressive). Bad suppliers are
    # distinguished by their kappa_b / eta_b / d_0 (concession behaviour) AND by
    # an elevated cost floor: their reservation price is
    # reference_price * cost_floor_ratio * cost_floor_mult. The "pre-emptive"
    # scams (vip_fee / fake_urgency / future_discount) carry cost_floor_mult=1.5,
    # so transacting with them ALWAYS overpays vs an honest supplier
    # (cost_floor_mult=1.0); the "post-hoc" scams (qty_bait / quality_downgrade)
    # keep the honest floor and do their damage at delivery / return time.
    # LLM handles scam narratives.
    "bad_supplier_types": [
        {
            "scam": "vip_fee",
            "family": "Adversarial",
            "kappa_b": 0.15,
            "eta_b": "aggressive",
            "d_0_range": [0.80, 0.90],
            "cost_floor_mult": 1.5,
        },
        {
            "scam": "future_discount",
            "family": "Adversarial",
            "kappa_b": 0.20,
            "eta_b": "aggressive",
            "d_0_range": [0.75, 0.85],
            "cost_floor_mult": 1.5,
        },
        {
            "scam": "qty_bait",
            "family": "Adversarial",
            "kappa_b": 0.35,
            "eta_b": "neutral",
            "d_0_range": [0.50, 0.70],
            "cost_floor_mult": 1.0,
        },
        {
            "scam": "quality_downgrade",
            "family": "Adversarial",
            "kappa_b": 0.35,
            "eta_b": "neutral",
            "d_0_range": [0.50, 0.70],
            "cost_floor_mult": 1.0,
        },
        {
            "scam": "fake_urgency",
            "family": "Adversarial",
            "kappa_b": 0.20,
            "eta_b": "aggressive",
            "d_0_range": [0.70, 0.85],
            "cost_floor_mult": 1.5,
        },
    ],
    "K": 10,
    # vip_fee fraud: a one-time "membership" fee charged up-front. The agent
    # cannot place product orders with a vip_fee supplier until it has paid this
    # fee (enforced in order_processor._process_order). After paying, the
    # promised "exclusive low pricing" never materialises — the supplier still
    # negotiates at its elevated cost_floor_mult floor. So the fee is pure loss
    # on top of the overpay.
    "vip_fee_amount": 1000.0,
    "spam_probability": 0.5,
    # quality_downgrade fraud: units delivered by such suppliers are defective,
    # manifesting as a much higher return rate once sold. The defective return
    # rate is min(0.95, max(quality_downgrade_return_rate, base_rate * 2)), so it
    # is at least this floor AND at least double the SKU's honest return rate —
    # categories whose natural return rate is already ~0.40-0.55 still get a
    # meaningful penalty. See EcommerceEnv._effective_return_rate.
    "quality_downgrade_return_rate": 0.40,
}


def get_cost_floor_mult(scam_type: str) -> float:
    """Per-scam multiplier applied to the honest cost_floor for a bad supplier.

    Returns 1.0 for good suppliers / unknown scams. Single source of truth for
    BOTH price-floor enforcement sites (KernelManager reservation price and
    order_processor._enforce_price_floor), so the negotiated price and the
    directly-accepted price stay consistent.
    """
    if not scam_type:
        return 1.0
    for btype in SUPPLIER_CONFIG["bad_supplier_types"]:
        if btype["scam"] == scam_type:
            return float(btype.get("cost_floor_mult", 1.0))
    return 1.0


def compute_effective_floor(env, supplier_name: str, sku_id: str) -> float:
    """Effective price floor for a (supplier, sku) pair — single source of truth.

    Used by both `KernelManager._get_kernel_params` (kernel reservation price
    `r_b`) and `order_processor._enforce_price_floor` (post-hoc order-layer
    clamp), so the price the kernel will accept and the price the order layer
    will charge can never drift apart.  The order layer must enforce the same
    floor because an agent could bypass negotiation and directly accept/offer a
    low price — without a post-hoc clamp it would dodge the markup.

    Semantics:
    - Good / unknown suppliers: floor = `cost_floor = ref_price * cost_floor_ratio`
      (design doc §3.6).
    - Bad suppliers: floor =
        ``max(cost_floor, min(cost_floor * cost_floor_mult, scam_cap, initial_offer))``
      where ``scam_cap = ref_price * scam_cap_ratio``.  Even an agent that
      grinds a pre-emptive scammer down to this floor still overpays vs an
      honest supplier (mult > 1); an agent that walks away pays nothing.

      The min-side caps freeze the overpay magnitude against a historical
      baseline AND prevent the scammer from quoting above an honest supplier
      (red flag).  Without these caps the 1.5× floor can exceed the opening,
      forcing the kernel to open above honest price — a tell that makes the
      agent walk away, so the scam never catches anyone.  Capped, the supplier
      opens competitively yet its floor still sits above the honest cost_floor.

      The outer ``max(cost_floor, ...)`` is the inversion guard: for categories
      where ``scam_cap`` or ``initial_offer`` dipped BELOW the honest
      ``cost_floor`` (e.g. Major Appliances with scam_cap_ratio 0.50 <
      cost_floor_ratio 0.72), the min-caps would otherwise push the scam floor
      under the honest floor and make the scammer cheaper than honest
      suppliers — inverting the always-overpay trap.  The pre-emptive floor
      must never sit below ``cost_floor``.

    Returns 0.0 if the SKU has no demand params (defensive; should not happen
    in normal episodes).
    """
    demand = getattr(env, "demand_params", {}).get(sku_id, {})
    if not demand:
        return 0.0
    ref_price = float(demand.get("reference_price", 1.0))
    cfr = float(demand.get("cost_floor_ratio", 0.5))
    cost_floor = round(ref_price * cfr, 2)

    supplier_type = getattr(env, "supplier_types", {}).get(supplier_name, "unknown")
    if supplier_type != "bad":
        return cost_floor

    scam_type = getattr(env, "bad_supplier_scam_types", {}).get(supplier_name, "")
    mult = get_cost_floor_mult(scam_type)
    wr = float(demand.get("wholesale_ratio", 0.7))
    initial_offer = round(ref_price * wr, 2)
    scam_cap_ratio = float(demand.get("scam_cap_ratio", wr))
    scam_cap = round(ref_price * scam_cap_ratio, 2)
    return round(
        max(cost_floor, min(cost_floor * mult, scam_cap, initial_offer)),
        2,
    )
