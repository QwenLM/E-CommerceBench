"""
E-Commerce Bench simulation environment.
Manages multi-store state, sales simulation, returns, deliveries,
dual-account system, events, and supplier interactions.
"""

import csv
import json
import logging
import math
import random
from datetime import datetime, timedelta, date, time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data"


class StoreState:
    def __init__(
        self,
        store_id: str,
        store_type: str,
        store_name: str,
        opened_date: date,
        daily_rent: float,
    ):
        self.store_id = store_id
        self.store_type = store_type
        self.store_name = store_name
        self.opened_date = opened_date
        self.is_open = True
        self.daily_rent = daily_rent
        self.inventory: Dict[str, int] = {}
        self.prices: Dict[str, float] = {}
        self.cumulative_sales = 0
        self.reputation = 0.5
        self.promotion_active: Optional[str] = None
        self.promotion_discount: float = 0.0
        self.yesterday_sales: Dict[str, Dict] = {}
        self.total_revenue = 0.0
        self.total_shipping_cost = 0.0
        self.total_refunds = 0.0
        # B3 (two-way reputation): rolling reputation that can fall, driven by
        # recent service quality. `reputation` is recomputed daily from base
        # volume reputation MINUS penalties accrued in the rolling window
        # (returns, stockout-cancelled orders). These counters decay daily.
        self.recent_returns = 0.0
        self.recent_sold = 0.0
        self.recent_cancellations = 0.0
        # S2 (anti-churn): number of times this store *type* has been (re)opened
        # is tracked on the env; per-store we record whether reputation should
        # rebuild slowly after a reopen.
        self.reopened = False


class EcommerceEnv:

    def __init__(
        self,
        initial_balance: float = 100000.0,
        store_daily_rent: float = 50.0,
        store_setup_fee: float = 500.0,
        sales_commission_rate: float = 0.02,
        max_stores: int = 4,
        max_day: int = 365,
        start_date: str = "2026-01-01",
        working_hours: Tuple[int, int] = (8, 18),
        per_store_scale: float = 0.1,
        unpaid_limit: int = 10,
        seed: int = 20260122,
        settlement_window: int = 9,
        ship_deadline_days: int = 2,
    ):
        self.rng = random.Random(seed)
        self.seed = seed

        self.bank_balance = initial_balance
        self.platform_wallet = 0.0
        self.initial_balance = initial_balance
        self.store_daily_rent = store_daily_rent
        self.store_setup_fee = store_setup_fee
        self.sales_commission_rate = sales_commission_rate
        self.max_stores = max_stores
        self.max_day = max_day
        self.per_store_scale = per_store_scale
        self.working_hours = working_hours
        self.unpaid_limit = unpaid_limit
        # E (cash-flow subsystem) config -------------------------------------
        # Sales revenue lands in escrow and only becomes withdrawable
        # `settlement_window` days after the order is *shipped*. Because the
        # settlement window is >= the max return-arrival lag (7d), customer
        # refunds always net against escrow before it settles — no negative
        # wallet. Orders not shipped within `ship_deadline_days` of the sale
        # are cancelled (lost sale + reputation hit).
        self.settlement_window = settlement_window
        self.ship_deadline_days = ship_deadline_days
        # Salvage fraction recovered when liquidating inventory on store
        # close. Closing a store dumps its shelf stock into the global warehouse
        # where it keeps accruing (rising) storage fees with no sell-off path —
        # making "close an underperformer" a perpetual cost rather than a clean
        # exit. Liquidation recovers this fraction of the per-unit purchase cost
        # and removes the stock so the storage bleed stops.
        self.liquidation_salvage_rate = 0.5

        self.start_time = datetime.strptime(start_date, "%Y-%m-%d").replace(
            hour=working_hours[0]
        )
        self.end_time = self.start_time + timedelta(days=max_day)
        self.current_time = self.start_time

        self.stores: Dict[str, StoreState] = {}
        self.warehouse: Dict[str, int] = {}
        self.warehouse_purchase_prices: Dict[str, float] = {}
        # B4 (warehouse aging): FIFO lots per SKU so storage fees can rise with
        # how long stock has sat unsold. Each lot is [quantity, inbound_date].
        # Consumed FIFO when stock is moved to a store (stock_store).
        self.warehouse_lots: Dict[str, List[List]] = {}
        # quality_downgrade fraud: cumulative units delivered per SKU and how
        # many of them were defective. Inventory is pooled by SKU (no lot
        # tracking), so the per-SKU defective *ratio* drives an elevated return
        # rate at sale time. See _process_sales_for_date.
        self.sku_defective_delivered: Dict[str, int] = {}
        self.sku_total_delivered: Dict[str, int] = {}
        # ATTR-01: agent-facing procurement provenance. For each SKU, how many
        # units were delivered by each supplier. Because inventory is pooled by
        # SKU, a returned unit cannot be tied to a specific supplier lot — but
        # the delivery mix tells the agent which suppliers fed the pool, so it
        # can correlate suppliers with realized return rates. The hidden
        # defective flag is NOT exposed here; only delivered quantities are.
        self.sku_supplier_delivered: Dict[str, Dict[str, int]] = {}
        # ATTR-01: cumulative realized units sold / returned per SKU (across all
        # stores, whole run). yesterday_sales resets daily, so these give the
        # agent a stable realized return rate per SKU for source attribution.
        self.sku_units_sold: Dict[str, int] = {}
        self.sku_units_returned: Dict[str, int] = {}
        self.next_store_id = 1

        self.pending_deliveries: List[Dict] = []
        self.pending_returns: List[Dict] = []
        # E: sales awaiting the agent's ship action. Each entry groups a
        # (store, sku, day) batch of sold units with a shipping deadline.
        self.pending_shipments: List[Dict] = []
        # E: escrow batches awaiting settlement. Each: {amount, settle_date,
        # store_id}. `pending_settlement` mirrors the sum for cheap display.
        self.escrow_batches: List[Dict] = []
        self.pending_settlement: float = 0.0
        # Buffer of events (daily_trigger / termination) produced by ANY time
        # advance (advance_minutes inside normal tools, or wait_for_next_day).
        # Drained once per tool batch by the tool manager so day-crossing events
        # are never lost. See ask_code_exec step 5.
        self.pending_events: List[Dict] = []
        self.next_order_id = 1

        self.active_promotions: List[Dict] = []
        self.active_events: List[Dict] = []
        self.news_feed: List[Dict] = []
        self.news_history: List[Dict] = []

        self.unpaid_streak = 0
        self.day_count = 0
        self.is_done = False
        self.termination_reason = None

        self.contacted_suppliers = set()
        self.supplier_chat_history: Dict[str, List] = {}
        self.supplier_order_count: Dict[str, int] = {}
        self.supplier_bankrupt: Dict[str, bool] = {}
        self.supplier_bankrupt_until: Dict[str, Any] = {}
        self.suppliers: Dict[str, str] = {}
        self.memos: Dict[str, str] = {}
        self.chatbox_log_dir: str = ""
        self.vip_fee_paid_suppliers: set = set()
        self.supplier_deal_messages: Dict[str, List] = {}

        # S2 (anti-churn): how many times each store TYPE has been opened.
        # Re-opening a type that was previously closed costs the setup fee
        # again AND the new store rebuilds reputation slowly.
        self.store_type_open_count: Dict[str, int] = {}

        # D2 (analysis panel): fraud / fulfilment counters for post-hoc
        # capability analysis. Updated by order_processor and the shipment path.
        #
        # `per_type` records, for each of the 5 fraud overlays, simply how much
        # money the agent handed to suppliers of that type (plus order/unit
        # counts for context). `spend_by_personality` does the same for good
        # suppliers, bucketed by the supplier's personality. There is no
        # loss/avoidance accounting any more — harm now shows up directly as the
        # elevated spend at bad suppliers (pre-emptive scams overpay via the
        # high cost floor; post-hoc scams under-deliver / cause returns).
        FRAUD_TYPES = (
            "vip_fee",
            "qty_bait",
            "quality_downgrade",
            "fake_urgency",
            "future_discount",
        )
        self.fraud_stats: Dict[str, Any] = {
            "orders_total": 0,
            "orders_from_bad_supplier": 0,
            "vip_fee_paid_count": 0,
            "vip_fee_paid_amount": 0.0,
            "spend_total": 0.0,
            "spend_on_bad_supplier": 0.0,
            # per-fraud-type spend breakdown
            "per_type": {
                ft: {
                    "orders": 0,  # order count to this type
                    "units": 0,  # units ordered from this type
                    "spend": 0.0,  # bank spend on this type (incl. any VIP fee)
                }
                for ft in FRAUD_TYPES
            },
            # good-supplier spend bucketed by personality (seeded from roster
            # after suppliers load, so all present personalities appear).
            "spend_by_personality": {},
        }
        self.fulfilment_stats: Dict[str, Any] = {
            "orders_sold": 0,  # shipment batches created
            "orders_shipped": 0,  # shipped in time
            "orders_cancelled": 0,  # missed ship deadline -> lost sale
            "units_sold": 0,
            "units_returned": 0,
            "ship_speed_counts": {"fast": 0, "standard": 0, "slow": 0},
        }
        # Return-rate MANAGEMENT panel (separate from fraud). Decomposes expected
        # returned units, measured at ship time, into:
        #   - natural   : the category's base return rate (uncontrollable floor)
        #   - price     : extra returns from pricing above reference (S3) — a
        #                 pricing-management lever the agent controls
        #   - ship_speed: change in returns from the chosen ship speed — a
        #                 fulfilment-management lever the agent controls
        #   - defective : extra returns from quality_downgrade fraud stock — this
        #                 is a FRAUD leak, surfaced here only so it can be
        #                 subtracted out of "management" quality.
        # `units_shipped` is the denominator. All are expectation-based (the
        # deterministic seeded realization is what actually returns).
        self.return_stats: Dict[str, Any] = {
            "units_shipped": 0,
            "exp_returns_total": 0.0,
            "exp_returns_natural": 0.0,
            "exp_returns_price": 0.0,
            "exp_returns_ship_speed": 0.0,
            "exp_returns_defective": 0.0,
            "refund_loss_total": 0.0,  # gross refunds paid (all causes)
            "shipping_loss_on_returns": 0.0,  # sunk shipping on returned units
        }

        self.balance_history: List[Dict] = []
        self.daily_summaries: List[Dict] = []
        self.next_shipment_id = 1
        self.next_batch_id = 1
        # Money ledger: every franc that enters/leaves bank+wallet+escrow goes
        # through one of these accumulators. Used by the conservation test:
        #   bank + wallet + escrow == initial_balance
        #     - setup - ops - storage - shipping - procurement
        #     + revenue_recognised_net - refunds_paid_gross
        self._ledger: Dict[str, float] = {
            "setup": 0.0,
            "ops": 0.0,
            "storage": 0.0,
            "shipping": 0.0,
            "procurement": 0.0,
            "revenue_net": 0.0,
            "refunds_gross": 0.0,
            "vip_fees": 0.0,
        }

        self._init_data_cache()
        self._process_events_for_date(self.current_time.date())

    # ================================================================
    # Data Loading
    # ================================================================

    def _init_data_cache(self) -> None:
        self.products = self._load_csv_dict("products.csv", "product_id")
        self.category_params = self._load_csv_dict("category_params.csv", "category")
        self.suppliers_data = self._load_csv_list("suppliers.csv")
        self.promotions_data = self._load_csv_list("promotions.csv")
        self.events_data = self._load_csv_list("events.csv")
        self.store_types_data = self._load_csv_dict("store_types.csv", "store_type_id")

        self.demand_params: Dict[str, Dict] = {}
        for pid, p in self.products.items():
            cat = p["category"]
            cp = self.category_params.get(cat, {})
            self.demand_params[pid] = {
                "base_monthly_sales": (
                    float(cp.get("monthly_sales_min", 100))
                    + float(cp.get("monthly_sales_max", 500))
                )
                / 2,
                "reference_price": float(p.get("reference_price", 50)),
                "return_rate": float(p.get("return_rate", 0.05)),
                "elasticity_type": cp.get("elasticity_type", "linear"),
                "elasticity_param": float(cp.get("elasticity_param", 2.0)),
                "wholesale_ratio": float(cp.get("wholesale_ratio", 0.7)),
                "cost_floor_ratio": float(cp.get("cost_floor_ratio", 0.5)),
                # Historical wholesale baseline, used ONLY to cap the
                # pre-emptive-scammer reservation price (see kernel_manager and
                # order_processor._enforce_price_floor). Falls back to the
                # opening ratio for older CSVs that predate this column.
                "scam_cap_ratio": float(
                    cp.get("scam_cap_ratio", cp.get("wholesale_ratio", 0.7))
                ),
            }

        self.supplier_types: Dict[str, str] = {}
        self.supplier_info: Dict[str, Dict] = {}
        self.bad_supplier_scam_types: Dict[str, str] = {}
        for s in self.suppliers_data:
            name = s["supplier_name"]
            self.supplier_types[name] = s["supplier_type"]
            self.supplier_info[name] = s
            self.suppliers[name] = s.get("supplier_email", "")
            if s["supplier_type"] == "bad" and s.get("fraud_type"):
                self.bad_supplier_scam_types[name] = s["fraud_type"]

        # Seed the per-personality spend buckets from the good-supplier roster
        # so every personality actually present starts at 0.0 (and bad
        # suppliers' "Adversarial" personality is excluded — their spend is
        # tracked per fraud type instead).
        sbp = self.fraud_stats["spend_by_personality"]
        for name, s in self.supplier_info.items():
            if self.supplier_types.get(name) == "good":
                p = s.get("personality", "")
                if p:
                    sbp.setdefault(p, 0.0)

        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "store_type_config", DATA_DIR / "store_type_config.py"
        )
        stc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(stc)
        self.seasonality = stc.SEASONALITY
        self.size_costs = stc.SIZE_COSTS
        # New economic tables (A1 / B2 / B3 / B4 / E / S2 / S3)
        self.ops_cost_per_day = getattr(stc, "OPS_COST_PER_DAY", {})
        self.ops_cost_default = getattr(stc, "OPS_COST_DEFAULT", self.store_daily_rent)
        self.ops_cost_override = getattr(stc, "OPS_COST_OVERRIDE", {})
        self.ship_speed = getattr(stc, "SHIP_SPEED", {})
        self.ship_speed_default = getattr(stc, "SHIP_SPEED_DEFAULT", "standard")
        self.market_capacity = getattr(stc, "MARKET_CAPACITY", {})
        self.market_capacity_default = getattr(stc, "MARKET_CAPACITY_DEFAULT", 25.0)
        self.per_store_demand_scale = getattr(stc, "PER_STORE_DEMAND_SCALE", {})
        self.per_store_demand_scale_default = getattr(
            stc, "PER_STORE_DEMAND_SCALE_DEFAULT", 1.0
        )
        self.category_cap_frac = getattr(stc, "CATEGORY_CAP_FRAC_DEFAULT", 1.0)
        self.store_playbook = getattr(stc, "STORE_PLAYBOOK", {})
        self.reputation_penalty = getattr(stc, "REPUTATION_PENALTY", {})
        self.storage_age_mult = getattr(stc, "STORAGE_AGE_MULT", [(0, 1.0)])
        self.return_price_knees = getattr(stc, "RETURN_PRICE_KNEES", [(1.0, 1.0)])
        # Allow config to override timing defaults unless caller set them.
        self.settlement_window = getattr(
            stc, "SETTLEMENT_WINDOW_DAYS", self.settlement_window
        )
        self.ship_deadline_days = getattr(
            stc, "SHIP_DEADLINE_DAYS", self.ship_deadline_days
        )
        self.liquidation_salvage_rate = getattr(
            stc, "LIQUIDATION_SALVAGE_RATE", self.liquidation_salvage_rate
        )

        # DSC-01 guard: category_params.csv must stay in sync with the
        # WHOLESALE_RATIOS source-of-truth in store_type_config.py. The CSV was
        # historically left stale (e.g. appliance cost_floor 0.90 vs 0.35),
        # silently turning flagship tiers into near-zero-margin traps. Fail loud
        # at init rather than corrupt the economy of every supplier+SKU.
        # NOTE: the dict tuple is (historical_wholesale_ratio, cost_floor_ratio).
        # The negotiation opening (wholesale_ratio column) is DERIVED as
        # 0.5 + 0.5*cost_floor_ratio, and the historical value is carried as the
        # scam_cap_ratio column — so the guard checks all three accordingly.
        wholesale_ratios = getattr(stc, "WHOLESALE_RATIOS", {})
        if wholesale_ratios:
            mismatches = []
            for cat, params in self.category_params.items():
                ref = wholesale_ratios.get(cat)
                if not ref:
                    continue
                old_wr, exp_cfr = ref
                exp_wr = round(0.5 + 0.5 * exp_cfr, 2)
                got_wr = float(params.get("wholesale_ratio", exp_wr))
                got_cfr = float(params.get("cost_floor_ratio", exp_cfr))
                got_scap = float(params.get("scam_cap_ratio", old_wr))
                if (
                    abs(got_wr - exp_wr) > 1e-6
                    or abs(got_cfr - exp_cfr) > 1e-6
                    or abs(got_scap - old_wr) > 1e-6
                ):
                    mismatches.append(
                        f"{cat}: csv=(wr={got_wr},cfr={got_cfr},scap={got_scap}) "
                        f"config=(wr={exp_wr},cfr={exp_cfr},scap={old_wr})"
                    )
            if mismatches:
                raise ValueError(
                    "category_params.csv is out of sync with WHOLESALE_RATIOS in "
                    "store_type_config.py ({} categories). The CSVs under data/ "
                    "must be consistent with that config. First few: {}".format(
                        len(mismatches), "; ".join(mismatches[:5])
                    )
                )

    def _load_csv_dict(self, filename: str, key_field: str) -> Dict[str, Dict]:
        path = DATA_DIR / filename
        result = {}
        with open(path, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                result[row[key_field]] = dict(row)
        return result

    def _load_csv_list(self, filename: str) -> List[Dict]:
        path = DATA_DIR / filename
        with open(path, "r", encoding="utf-8") as f:
            return [dict(row) for row in csv.DictReader(f)]

    # ================================================================
    # ================================================================
    # Store Lifecycle
    # ================================================================

    def open_store(self, store_type: str, store_name: str) -> Dict[str, Any]:
        if len([s for s in self.stores.values() if s.is_open]) >= self.max_stores:
            return {
                "success": False,
                "error": f"Maximum {self.max_stores} stores allowed.",
            }

        if store_type not in self.store_types_data:
            valid = list(self.store_types_data.keys())
            return {
                "success": False,
                "error": f"Invalid store type '{store_type}'. Valid: {valid}",
            }

        for s in self.stores.values():
            if s.is_open and s.store_type == store_type:
                return {
                    "success": False,
                    "error": f"Already have an open {store_type} store.",
                }

        if self.bank_balance < self.store_setup_fee:
            return {
                "success": False,
                "error": f"Insufficient funds. Need ¥{self.store_setup_fee}, have ¥{self.bank_balance:.2f}.",
            }

        self.bank_balance -= self.store_setup_fee
        self._ledger["setup"] += self.store_setup_fee
        store_id = f"store_{self.next_store_id:03d}"
        self.next_store_id += 1

        # A1: daily operating cost depends on store tier (premium stores need
        # bigger teams). Falls back to the legacy flat rent if tier unknown.
        st_info = self.store_types_data[store_type]
        ops_cost = self._ops_cost_for_type(store_type)

        store = StoreState(
            store_id=store_id,
            store_type=store_type,
            store_name=store_name,
            opened_date=self.current_time.date(),
            daily_rent=ops_cost,
        )
        # S2 (anti-churn): track how many times this TYPE has been opened. A
        # re-open (count > 1) is allowed but the setup fee is paid again (above)
        # and the new store must rebuild reputation from the floor via fresh
        # sales — the prior store's goodwill does NOT carry over.
        prior = self.store_type_open_count.get(store_type, 0)
        self.store_type_open_count[store_type] = prior + 1
        store.reopened = prior > 0

        self.stores[store_id] = store

        allowed_cats = st_info.get("allowed_categories", "").split("|")

        return {
            "success": True,
            "store_id": store_id,
            "store_type": store_type,
            "store_name": store_name,
            "setup_fee_charged": self.store_setup_fee,
            "daily_ops_cost": round(ops_cost, 2),
            "is_reopen": store.reopened,
            "reopen_note": (
                "This store type was opened before. The setup fee was charged "
                "again and reputation must be rebuilt from scratch — frequent "
                "open/close churn is costly."
                if store.reopened
                else ""
            ),
            "bank_balance": round(self.bank_balance, 2),
            "allowed_categories": allowed_cats,
        }

    def _ops_cost_for_type(self, store_type: str) -> float:
        """A1: per-day human/operations cost for a store of this type. A
        per-type override (if any) wins; otherwise the tier table applies."""
        if store_type in getattr(self, "ops_cost_override", {}):
            return float(self.ops_cost_override[store_type])
        st_info = self.store_types_data.get(store_type, {})
        try:
            tier = int(st_info.get("tier", 0))
        except (TypeError, ValueError):
            tier = 0
        return float(self.ops_cost_per_day.get(tier, self.ops_cost_default))

    def current_daily_ops_cost(self) -> float:
        """Total operations cost that will be charged across all open stores on
        the next daily trigger (no multi-store discount; each store pays full ops).
        This mirrors the charge computed in
        _process_daily_trigger and is the correct figure for low-balance
        warnings — the legacy flat ``store_daily_rent`` (50.0) is stale now that
        ops cost is tier-based (80/120/160 per store)."""
        open_stores = [s for s in self.stores.values() if s.is_open]
        return sum(s.daily_rent for s in open_stores)

    def close_store(self, store_id: str, liquidate: bool = False) -> Dict[str, Any]:
        store = self.stores.get(store_id)
        if not store or not store.is_open:
            return {
                "success": False,
                "error": f"Store '{store_id}' not found or already closed.",
            }

        returned_items = {}
        liquidated_items = {}
        salvage_total = 0.0
        for pid, qty in store.inventory.items():
            if qty <= 0:
                continue
            if liquidate:
                # Sell the shelf stock back at a salvage fraction of its
                # per-unit purchase cost and credit the bank, instead of moving
                # it to the warehouse where it would keep accruing storage with
                # no sell-off path. This makes closing an underperformer a clean,
                # escapable exit rather than a perpetual storage liability.
                unit_cost = self.warehouse_purchase_prices.get(pid, 0.0)
                proceeds = unit_cost * self.liquidation_salvage_rate * qty
                salvage_total += proceeds
                liquidated_items[pid] = {"quantity": qty, "salvage": round(proceeds, 2)}
                # Liquidated items physically leave the warehouse.
                self._warehouse_lots_remove(pid, qty)
            else:
                # Return allocation to the warehouse dict (available to re-list
                # on another store). Do NOT add a new lot — the items' lots were
                # never removed when listed, so they're still in warehouse_lots
                # and correctly accruing storage fees.
                self.warehouse[pid] = self.warehouse.get(pid, 0) + qty
                returned_items[pid] = qty
        store.inventory.clear()
        store.prices.clear()
        store.is_open = False

        result = {
            "success": True,
            "store_id": store_id,
            "note": (
                "Reopening this store type later will cost the setup fee again "
                "and start reputation from scratch."
            ),
        }
        if liquidate:
            self.bank_balance += salvage_total
            self._ledger["revenue_net"] += salvage_total
            result["liquidated"] = True
            result["items_liquidated"] = liquidated_items
            result["total_units_liquidated"] = sum(
                v["quantity"] for v in liquidated_items.values()
            )
            result["salvage_credited"] = round(salvage_total, 2)
            result["salvage_rate"] = self.liquidation_salvage_rate
            result["bank_balance"] = round(self.bank_balance, 2)
            result["note"] += (
                f" Inventory was liquidated at {self.liquidation_salvage_rate:.0%} of "
                f"purchase cost (¥{salvage_total:.2f} credited); it no longer incurs storage."
            )
        else:
            result["inventory_returned_to_warehouse"] = returned_items
            result["total_items_returned"] = sum(returned_items.values())
            result["note"] += (
                " Inventory was moved to your warehouse and WILL keep incurring "
                "storage fees until sold or liquidated (close with liquidate=true "
                "to sell it back at salvage value instead)."
            )
        return result

    # ================================================================
    # Inventory Management
    # ================================================================

    def _warehouse_add(self, pid: str, qty: int, inbound_date: date) -> None:
        """Add stock to the warehouse, recording a dated FIFO lot for B4 aging."""
        if qty <= 0:
            return
        self.warehouse[pid] = self.warehouse.get(pid, 0) + qty
        self.warehouse_lots.setdefault(pid, []).append([qty, inbound_date])

    def _warehouse_lots_remove(self, pid: str, qty: int) -> None:
        """Remove qty from FIFO lots only (for storage-fee tracking), without
        touching the warehouse allocation dict. Called when items physically
        leave the warehouse (sale/shipment), NOT when they are merely listed on
        a store page."""
        if qty <= 0:
            return
        remaining = qty
        lots = self.warehouse_lots.get(pid, [])
        while remaining > 0 and lots:
            lot = lots[0]
            if lot[0] <= remaining:
                remaining -= lot[0]
                lots.pop(0)
            else:
                lot[0] -= remaining
                remaining = 0

    def publish_to_store(self, store_id: str, plan: List[Dict]) -> Dict[str, Any]:
        """List products on a store's online page with pricing.

        This is an ONLINE LISTING operation — products remain physically in the
        warehouse (incurring storage fees) until they are actually sold and
        shipped. The warehouse dict is decremented for allocation tracking (so
        the same unit isn't listed twice), but warehouse_lots are NOT touched —
        lots track physical presence for storage-fee aging."""
        store = self.stores.get(store_id)
        if not store or not store.is_open:
            return {
                "success": False,
                "error": f"Store '{store_id}' not found or closed.",
            }

        st_info = self.store_types_data[store.store_type]
        allowed_cats = set(st_info.get("allowed_categories", "").split("|"))
        results = []

        for item in plan:
            pid = item.get("product_id", "")
            try:
                qty = int(item.get("quantity", 0))
                price = float(item.get("retail_price", 0))
            except (TypeError, ValueError):
                results.append(
                    {
                        "product_id": pid,
                        "success": False,
                        "error": "quantity must be an integer and retail_price a number.",
                    }
                )
                continue

            if qty <= 0:
                results.append(
                    {
                        "product_id": pid,
                        "success": False,
                        "error": f"quantity must be positive (got {qty}).",
                    }
                )
                continue
            if price <= 0:
                results.append(
                    {
                        "product_id": pid,
                        "success": False,
                        "error": f"retail_price must be positive (got {price}).",
                    }
                )
                continue

            product = self.products.get(pid)
            if not product:
                results.append(
                    {"product_id": pid, "success": False, "error": "Product not found."}
                )
                continue
            if product["category"] not in allowed_cats:
                results.append(
                    {
                        "product_id": pid,
                        "success": False,
                        "error": f"Category '{product['category']}' not allowed in {store.store_type} store.",
                    }
                )
                continue
            wh_qty = self.warehouse.get(pid, 0)
            if wh_qty < qty:
                results.append(
                    {
                        "product_id": pid,
                        "success": False,
                        "error": f"Insufficient warehouse stock. Have {wh_qty}, need {qty}.",
                    }
                )
                continue

            # Decrement the warehouse ALLOCATION dict only (not lots) — the
            # product is still physically in the warehouse until sold+shipped.
            self.warehouse[pid] = self.warehouse.get(pid, 0) - qty
            if self.warehouse[pid] < 0:
                self.warehouse[pid] = 0
            store.inventory[pid] = store.inventory.get(pid, 0) + qty
            store.prices[pid] = price
            results.append(
                {
                    "product_id": pid,
                    "success": True,
                    "quantity_stocked": qty,
                    "retail_price": price,
                }
            )

        return {"store_id": store_id, "results": results}

    def set_prices(self, store_id: str, prices: List[Dict]) -> Dict[str, Any]:
        store = self.stores.get(store_id)
        if not store or not store.is_open:
            return {
                "success": False,
                "error": f"Store '{store_id}' not found or closed.",
            }

        results = []
        for item in prices:
            pid = item.get("product_id", "")
            try:
                new_price = float(item.get("price", 0))
            except (TypeError, ValueError):
                results.append(
                    {
                        "product_id": pid,
                        "success": False,
                        "error": "price must be a number.",
                    }
                )
                continue
            # A non-positive price silently zeroes the SKU's demand (the sales
            # loop skips price<=0), so reject it instead of reporting success.
            if new_price <= 0:
                results.append(
                    {
                        "product_id": pid,
                        "success": False,
                        "error": f"price must be positive (got {new_price}).",
                    }
                )
                continue
            if pid not in store.inventory:
                results.append(
                    {
                        "product_id": pid,
                        "success": False,
                        "error": "Product not in store.",
                    }
                )
            else:
                old_price = store.prices.get(pid, 0)
                store.prices[pid] = new_price
                results.append(
                    {
                        "product_id": pid,
                        "success": True,
                        "old_price": old_price,
                        "new_price": new_price,
                    }
                )
        return {"store_id": store_id, "results": results}

    def return_to_warehouse(self, store_id: str, items: List[Dict]) -> Dict[str, Any]:
        store = self.stores.get(store_id)
        if not store or not store.is_open:
            return {
                "success": False,
                "error": f"Store '{store_id}' not found or closed.",
            }

        results = []
        for item in items:
            pid = item.get("product_id", "")
            try:
                qty = int(item.get("quantity", 0))
            except (TypeError, ValueError):
                results.append(
                    {
                        "product_id": pid,
                        "success": False,
                        "error": "quantity must be an integer.",
                    }
                )
                continue
            # Reject non-positive quantity. A negative qty slips past the
            # `current < qty` guard (e.g. 5 < -3 is False) and would then ADD
            # phantom sellable shelf stock (inventory -= negative) while pushing
            # a negative quantity into the warehouse — corrupting both books.
            if qty <= 0:
                results.append(
                    {
                        "product_id": pid,
                        "success": False,
                        "error": f"quantity must be positive (got {qty}).",
                    }
                )
                continue
            current = store.inventory.get(pid, 0)
            if current < qty:
                results.append(
                    {
                        "product_id": pid,
                        "success": False,
                        "error": f"Only {current} in store, requested {qty}.",
                    }
                )
            else:
                store.inventory[pid] -= qty
                if store.inventory[pid] == 0:
                    del store.inventory[pid]
                    if pid in store.prices:
                        del store.prices[pid]
                # Return the allocation to the warehouse dict (available to
                # re-list). Do NOT add a new warehouse_lot — the items' lots
                # were never removed when listed (publish_to_store only touches
                # the allocation dict), so adding would double-count storage.
                self.warehouse[pid] = self.warehouse.get(pid, 0) + qty
                results.append({"product_id": pid, "success": True, "returned": qty})
        return {"store_id": store_id, "results": results}

    # ================================================================
    # E — Fulfilment (manual shipping)
    # ================================================================

    def get_pending_shipments(self) -> Dict[str, Any]:
        """List sold-but-unshipped orders the agent must ship before their
        deadline (else they cancel: lost sale + reputation hit)."""
        today = self.current_time.date()
        items = []
        for s in self.pending_shipments:
            days_left = (s["deadline"] - today).days
            items.append(
                {
                    "shipment_id": s["shipment_id"],
                    "store_id": s["store_id"],
                    "product_id": s["product_id"],
                    "quantity": s["quantity"],
                    "unit_price": round(s["unit_price"], 2),
                    "revenue_net_if_shipped": round(s["revenue_net"], 2),
                    "sale_date": s["sale_date"].isoformat(),
                    "ship_deadline": s["deadline"].isoformat(),
                    "days_left": days_left,
                }
            )
        return {
            "pending_shipments": items,
            "count": len(items),
            "note": (
                "Ship with ship_orders (speed: fast/standard/slow). Faster ship "
                "costs more but reduces returns; slow is cheap but raises returns. "
                "Unshipped orders cancel after the deadline (lost sale + reputation hit)."
            ),
        }

    def ship_orders(
        self, shipment_ids: List[int] = None, speed: str = None
    ) -> Dict[str, Any]:
        """Ship pending orders. Charges shipping (size × speed multiplier) from
        the bank NOW, recognises net revenue into escrow (settles
        `settlement_window` days later), and seeds returns at the
        speed-adjusted rate. If shipment_ids is None, ships ALL pending."""
        speed = (speed or self.ship_speed_default).lower()
        spec = self.ship_speed.get(speed)
        if spec is None:
            return {
                "success": False,
                "error": f"Invalid speed '{speed}'. Choose: {list(self.ship_speed.keys())}.",
            }
        if self.is_done:
            return {
                "success": False,
                "error": "Episode has ended; no further shipping.",
            }

        if shipment_ids is not None:
            wanted = set(int(x) for x in shipment_ids)
            targets = [s for s in self.pending_shipments if s["shipment_id"] in wanted]
        else:
            targets = list(self.pending_shipments)

        if not targets:
            return {"success": False, "error": "No matching pending shipments."}

        results = []
        total_ship_cost = 0.0
        total_escrow = 0.0
        for s in targets:
            pid = s["product_id"]
            qty = s["quantity"]
            size = self.products.get(pid, {}).get("size", "Small")
            base_ship = self.size_costs.get(size, {}).get("shipping", 1.0)
            ship_cost = base_ship * spec["cost_mult"] * qty
            total_ship_cost += ship_cost

            # Charge shipping now (bank).
            self.bank_balance -= ship_cost
            self._ledger["shipping"] += ship_cost

            store = self.stores.get(s["store_id"])
            if store:
                store.total_shipping_cost += ship_cost
                store.total_revenue += s["revenue_gross"]
                store.cumulative_sales += qty

            # Recognise net revenue into a new escrow batch.
            batch_id = self.next_batch_id
            self.next_batch_id += 1
            settle_date = self.current_time.date() + timedelta(
                days=self.settlement_window
            )
            self.escrow_batches.append(
                {
                    "batch_id": batch_id,
                    "amount": s["revenue_net"],
                    "settle_date": settle_date,
                    "store_id": s["store_id"],
                }
            )
            self.pending_settlement += s["revenue_net"]
            total_escrow += s["revenue_net"]
            self._ledger["revenue_net"] += s["revenue_net"]

            # Seed returns at the speed-adjusted rate (S3), deterministic per
            # (shipment, speed). Arrive 3–7 days after shipping.
            eff_rate = min(0.95, s["base_return_rate"] * spec["return_mult"])

            # Return-rate MANAGEMENT decomposition (expectation-based), recorded
            # at ship time when the ship-speed lever is finally known. Splits the
            # expected returned units of THIS shipment into channels so the panel
            # can score pricing/ship-speed discipline separately from the
            # quality_downgrade fraud leak. Uses the components stored at sale.
            sm = spec["return_mult"]
            r_base = s.get("rr_base", s["base_return_rate"])
            r_after_defect = s.get("rr_after_defect", r_base)
            r_after_price = s.get("rr_after_price", s["base_return_rate"])
            # Apply ship-speed multiplier to each layer, clamped like eff_rate.
            f_base = min(0.95, r_base * sm)
            f_defect = min(0.95, r_after_defect * sm)
            f_price = min(0.95, r_after_price * sm)
            f_final = eff_rate
            rstat = self.return_stats
            rstat["units_shipped"] += qty
            rstat["exp_returns_total"] += qty * f_final
            rstat["exp_returns_natural"] += qty * f_base
            # defective uplift = (after-defect − base); a FRAUD-driven channel.
            rstat["exp_returns_defective"] += qty * max(0.0, f_defect - f_base)
            # price uplift = (after-price − after-defect); pricing MANAGEMENT.
            rstat["exp_returns_price"] += qty * (f_price - f_defect)
            # ship-speed effect = (final − after-price); fulfilment MANAGEMENT
            # (negative when fast shipping reduces returns).
            rstat["exp_returns_ship_speed"] += qty * (f_final - f_price)

            import hashlib as _hl

            det_seed = int(
                _hl.md5(f"ship{s['shipment_id']}{speed}".encode()).hexdigest()[:8], 16
            )
            rng_ret = random.Random(self.seed + det_seed)
            n_returned = sum(1 for _ in range(qty) if rng_ret.random() < eff_rate)
            if n_returned > 0:
                return_day = self.current_time.date() + timedelta(
                    days=rng_ret.randint(3, 7)
                )
                # Fraction of this SKU's stock that is quality_downgrade-defective,
                # so the return processor can attribute the share of refund+shipping
                # loss caused by fraud (ERR-03) vs ordinary returns.
                tot_del = self.sku_total_delivered.get(pid, 0)
                def_del = self.sku_defective_delivered.get(pid, 0)
                defect_frac = (def_del / tot_del) if tot_del > 0 else 0.0
                self.pending_returns.append(
                    {
                        "store_id": s["store_id"],
                        "product_id": pid,
                        "quantity": n_returned,
                        "refund_per_unit": s["unit_price"],
                        "arrival_date": return_day,
                        "batch_id": batch_id,
                        "ship_cost_per_unit": (ship_cost / qty) if qty else 0.0,
                        "defect_frac": defect_frac,
                    }
                )

            # Mark the day-summary line shipped.
            if store and pid in store.yesterday_sales:
                store.yesterday_sales[pid]["shipped"] = True
                store.yesterday_sales[pid]["shipping_cost"] = (
                    store.yesterday_sales[pid].get("shipping_cost", 0.0) + ship_cost
                )

            self.fulfilment_stats["orders_shipped"] += 1
            self.fulfilment_stats["ship_speed_counts"][speed] = (
                self.fulfilment_stats["ship_speed_counts"].get(speed, 0) + 1
            )
            results.append(
                {
                    "shipment_id": s["shipment_id"],
                    "product_id": pid,
                    "quantity": qty,
                    "ship_cost": round(ship_cost, 2),
                    "revenue_into_escrow": round(s["revenue_net"], 2),
                    "settles_on": settle_date.isoformat(),
                }
            )

        shipped_ids = {s["shipment_id"] for s in targets}
        self.pending_shipments = [
            s for s in self.pending_shipments if s["shipment_id"] not in shipped_ids
        ]

        return {
            "success": True,
            "speed": speed,
            "shipped_count": len(results),
            "total_shipping_cost": round(total_ship_cost, 2),
            "total_revenue_into_escrow": round(total_escrow, 2),
            "bank_balance": round(self.bank_balance, 2),
            "pending_settlement": round(self.pending_settlement, 2),
            "shipments": results,
        }

    # ================================================================
    # Balance & Wallet
    # ================================================================

    def get_balance(self) -> Dict[str, Any]:
        # E: surface the in-transit/escrow bucket and the next few settlement
        # dates so the agent can plan working capital. pending_settlement is
        # sales revenue that has been shipped but not yet settled (net of any
        # refunds that already arrived); it becomes withdrawable on its
        # settle_date.
        upcoming: Dict[str, float] = {}
        for b in self.escrow_batches:
            sd = b["settle_date"]
            key = sd.isoformat() if hasattr(sd, "isoformat") else str(sd)
            upcoming[key] = round(upcoming.get(key, 0.0) + b["amount"], 2)
        upcoming_sorted = dict(sorted(upcoming.items())[:5])
        unshipped = sum(s.get("revenue_net", 0.0) for s in self.pending_shipments)
        return {
            "bank_balance": round(self.bank_balance, 2),
            "platform_wallet": round(self.platform_wallet, 2),
            "pending_settlement": round(self.pending_settlement, 2),
            "unshipped_sales_value": round(unshipped, 2),
            "total": round(
                self.bank_balance + self.platform_wallet + self.pending_settlement, 2
            ),
            "upcoming_settlements": upcoming_sorted,
            "day": self.day_count,
            "date": self.current_time.strftime("%Y-%m-%d"),
            "note": (
                "Sales revenue first enters 'pending_settlement' (escrow) and "
                "becomes withdrawable from the wallet "
                f"{self.settlement_window} days after you SHIP the order. "
                "Refunds net against escrow before it settles. Ship orders "
                "promptly (ship_orders) or they cancel."
            ),
        }

    def withdraw(self, amount: Optional[float] = None) -> Dict[str, Any]:
        # WD-1: support sweeping the whole wallet. amount=None OR a sentinel
        # <=0 (the documented "pass 0 to withdraw everything") both withdraw the
        # entire current wallet balance, matching the tool docstring. Previously
        # only amount=None swept; a literal 0 was rejected with "Amount must be
        # positive.", contradicting the documented contract and causing agents
        # that followed the docs to loop on a spurious error.
        if amount is None or amount <= 0:
            amount = self.platform_wallet
        # WD-1: the agent only ever sees the wallet rounded to 2dp, while the
        # internal float carries sub-cent error. Comparing the displayed amount
        # against the raw float used to reject requests for the *exact displayed
        # balance* ("Wallet has ¥X, requested ¥X."). Tolerate a sub-cent overshoot
        # and clamp to the true balance so the request never spuriously fails and
        # can never withdraw more than exists.
        if amount - self.platform_wallet > 0.005:
            return {
                "success": False,
                "error": f"Wallet has ¥{self.platform_wallet:.2f}, requested ¥{amount:.2f}.",
            }
        actual = min(amount, self.platform_wallet)
        if actual <= 0:
            return {"success": False, "error": "Wallet is empty."}
        self.platform_wallet -= actual
        self.bank_balance += actual
        return {
            "success": True,
            "withdrawn": round(actual, 2),
            "bank_balance": round(self.bank_balance, 2),
            "platform_wallet": round(self.platform_wallet, 2),
        }

    def get_warehouse(self) -> Dict[str, Any]:
        items = {}
        total_value = 0.0
        for pid, qty in self.warehouse.items():
            if qty > 0:
                p = self.products.get(pid, {})
                cost = self.warehouse_purchase_prices.get(pid, 0)
                items[pid] = {
                    "quantity": qty,
                    "product": p.get("title", "")[:60],
                    "category": p.get("category", ""),
                    "purchase_price": round(cost, 2),
                    "size": p.get("size", ""),
                }
                total_value += qty * cost
        return {
            "total_items": sum(self.warehouse.get(pid, 0) for pid in self.warehouse),
            "total_value": round(total_value, 2),
            "items": items,
        }

    # ================================================================
    # Return-source attribution
    # ================================================================

    def trace_return_sources(self, product_id: str = None) -> Dict[str, Any]:
        """ATTR-01: trace which suppliers fed a SKU's (pooled) inventory, next
        to that SKU's realized sold/returned counts, so the agent can correlate
        suppliers with high returns.

        Inventory is pooled by SKU, so a single returned unit cannot be tied to
        a specific supplier lot. What this exposes is the *delivery mix* (how
        many units each supplier contributed to the pool) and the SKU's
        *realized* return rate vs its category-natural baseline. A SKU whose
        realized return rate runs far above baseline, and whose pool is
        dominated by one supplier, points at that supplier as the likely cause
        — the agent must make that inference; the underlying defect flag is
        never disclosed.
        """
        if product_id:
            pids = [product_id]
            if (
                product_id not in self.sku_supplier_delivered
                and product_id not in self.sku_units_sold
            ):
                return {
                    "error": f"No procurement/sales history for product '{product_id}'."
                }
        else:
            # All SKUs that have either been delivered or have realized returns.
            pids = sorted(
                set(self.sku_supplier_delivered.keys())
                | set(self.sku_units_returned.keys())
            )

        rows = []
        for pid in pids:
            p = self.products.get(pid, {})
            delivered = self.sku_supplier_delivered.get(pid, {})
            total_delivered = sum(delivered.values())
            sold = self.sku_units_sold.get(pid, 0)
            returned = self.sku_units_returned.get(pid, 0)
            realized_rr = (returned / sold) if sold > 0 else 0.0
            baseline_rr = self.demand_params.get(pid, {}).get(
                "return_rate", p.get("return_rate", 0.05)
            )

            sources = []
            for sup, qty in sorted(delivered.items(), key=lambda kv: -kv[1]):
                sources.append(
                    {
                        "supplier": sup,
                        "units_delivered": qty,
                        "share": (
                            round(qty / total_delivered, 3) if total_delivered else 0.0
                        ),
                    }
                )

            # Plain-language hint (no hidden flags leaked): flag SKUs whose
            # realized return rate is materially above the category-natural one.
            note = ""
            if sold >= 10:
                if realized_rr >= max(0.25, baseline_rr * 2):
                    note = (
                        "Realized return rate is far above this product's "
                        "natural baseline — inspect the suppliers feeding "
                        "this SKU's pool."
                    )
                elif realized_rr >= baseline_rr * 1.5:
                    note = (
                        "Realized return rate runs above baseline; worth "
                        "watching the source suppliers."
                    )

            rows.append(
                {
                    "product_id": pid,
                    "title": p.get("title", "")[:60],
                    "category": p.get("category", ""),
                    "total_units_delivered": total_delivered,
                    "units_sold": sold,
                    "units_returned": returned,
                    "realized_return_rate": round(realized_rr, 3),
                    "natural_baseline_return_rate": round(float(baseline_rr), 3),
                    "supplier_sources": sources,
                    "note": note,
                }
            )

        # Surface the most suspect SKUs first when scanning everything.
        rows.sort(key=lambda r: r["realized_return_rate"], reverse=True)
        return {"products": rows}

    # ================================================================
    # Store Status
    # ================================================================

    def get_store_status(self, store_id: str = None) -> Dict[str, Any]:
        if store_id:
            store = self.stores.get(store_id)
            if not store:
                return {"error": f"Store '{store_id}' not found."}
            return self._store_detail(store)
        else:
            return {
                "open_stores": len([s for s in self.stores.values() if s.is_open]),
                "max_stores": self.max_stores,
                "stores": [self._store_summary(s) for s in self.stores.values()],
            }

    def _store_summary(self, store: StoreState) -> Dict:
        return {
            "store_id": store.store_id,
            "store_type": store.store_type,
            "store_name": store.store_name,
            "is_open": store.is_open,
            "reputation": round(store.reputation, 3),
            "total_products": len(store.inventory),
            "total_inventory": sum(store.inventory.values()),
            "promotion": store.promotion_active,
        }

    def _store_detail(self, store: StoreState) -> Dict:
        products_detail = []
        # VIS-01: iterate current inventory PLUS any product that has a
        # yesterday_sales line (e.g. a return arrived for a product already
        # sold out or removed from inventory). Otherwise such returns would be
        # invisible in the per-product view even though they hit the store's
        # finances and reputation.
        pids = list(store.inventory.keys())
        for pid in store.yesterday_sales.keys():
            if pid not in store.inventory:
                pids.append(pid)
        for pid in pids:
            qty = store.inventory.get(pid, 0)
            p = self.products.get(pid, {})
            yesterday = store.yesterday_sales.get(pid, {})
            products_detail.append(
                {
                    "product_id": pid,
                    "title": p.get("title", "")[:60],
                    "category": p.get("category", ""),
                    "quantity": qty,
                    "retail_price": store.prices.get(pid, 0),
                    "yesterday_sold": yesterday.get("sold", 0),
                    "yesterday_returned": yesterday.get("returned", 0),
                }
            )

        day_revenue = sum(v.get("revenue", 0) for v in store.yesterday_sales.values())
        day_returns = sum(v.get("returned", 0) for v in store.yesterday_sales.values())
        day_refunds = sum(
            v.get("refund_amount", 0) for v in store.yesterday_sales.values()
        )
        day_shipping = sum(
            v.get("shipping_cost", 0) for v in store.yesterday_sales.values()
        )

        return {
            "store_id": store.store_id,
            "store_type": store.store_type,
            "store_name": store.store_name,
            "is_open": store.is_open,
            "opened_date": store.opened_date.isoformat(),
            "reputation": round(store.reputation, 3),
            "promotion": store.promotion_active,
            "promotion_discount": store.promotion_discount,
            "yesterday_summary": {
                "revenue": round(day_revenue, 2),
                "returns_count": day_returns,
                "refunds": round(day_refunds, 2),
                "shipping_cost": round(day_shipping, 2),
                "net": round(day_revenue - day_refunds - day_shipping, 2),
            },
            "products": products_detail,
        }

    # ================================================================
    # Promotions
    # ================================================================

    def join_promotion(
        self, store_id: str, event_name: str, discount_rate: float
    ) -> Dict[str, Any]:
        store = self.stores.get(store_id)
        if not store or not store.is_open:
            return {
                "success": False,
                "error": f"Store '{store_id}' not found or closed.",
            }
        if discount_rate < 0.05 or discount_rate > 0.50:
            return {
                "success": False,
                "error": "Discount rate must be between 0.05 and 0.50.",
            }

        promo = None
        for p in self.promotions_data:
            if p["event_name"] == event_name:
                promo = p
                break
        if not promo:
            return {"success": False, "error": f"Event '{event_name}' not found."}

        # Reject promotions whose window is neither active now nor starting soon.
        # Without this, joining an expired promotion returns a misleading success
        # (with a max_demand_multiplier that the next sales cycle silently drops
        # in _get_promo_boost), and could overwrite a genuinely active promotion.
        today = self.current_time.date()
        is_active = self._is_promo_active(promo, today)
        is_upcoming = self._promo_upcoming_within(promo, today, days=30)
        if not is_active and not is_upcoming:
            return {
                "success": False,
                "error": (
                    f"Promotion '{event_name}' is not currently active and is not "
                    f"starting within the next 30 days. You can only join an active "
                    f"or upcoming promotion."
                ),
            }

        store.promotion_active = event_name
        store.promotion_discount = discount_rate
        return {
            "success": True,
            "store_id": store_id,
            "event": event_name,
            "discount_rate": discount_rate,
            "active_now": is_active,
            "max_demand_multiplier": float(promo["max_demand_multiplier"]),
        }

    # ================================================================
    # Market Search & Supplier Search
    # ================================================================

    # S1: each store type's distinctive advantage axis, surfaced in
    # market_search so the agent can build a complementary portfolio instead of
    # only picking "low return rate". These are qualitative STORE-LEVEL hints
    # (the store's dominant categories) — individual categories within a store
    # can differ, so the wording hedges rather than making absolute claims.
    # Margins remain discoverable only via negotiation.
    STORE_ADVANTAGE_AXIS = {
        # Tier-consistent, VAGUE (no specific sub-category names). T1 = highest
        # ceiling but skill-gated (high risk/high reward); T2 = moderate/balanced;
        # T3 = modest but forgiving. Kept in sync with STORE_TYPES tiers.
        "food_beverage": "PROFIT POTENTIAL: VERY HIGH (only for skilled operators) — thin margins, a high return rate and a high daily operating cost punish careless play, but disciplined pricing + fast shipping + cost control reach the very top.",
        "home_living": "PROFIT POTENTIAL: VERY HIGH (only for skilled operators) — deep, steady demand and a big ceiling, but thin margins, high returns, bulkier goods and high operating cost make mistakes expensive.",
        "daily_office": "PROFIT POTENTIAL: VERY HIGH (only for skilled operators) — broad dependable demand and a high ceiling, but thin margins + high returns + high operating cost mean only disciplined play profits.",
        "auto_hardware": "PROFIT POTENTIAL: VERY HIGH (only for skilled operators) — the thinnest margins of all; relentless cost control compounds into top earnings, while any slack loses money.",
        "appliance_digital": "PROFIT POTENTIAL: MODERATE — a solid, balanced earner; high-ticket goods tie up capital and returns are mixed, but steady, well-managed play turns a dependable profit.",
        "fashion": "PROFIT POTENTIAL: MODERATE — decent margins offset by a high return rate; return management (accurate pricing + fast shipping) is the core skill.",
        "shoes_bags": "PROFIT POTENTIAL: MODERATE — a mixed range; a diversified, balanced basket earns steadily.",
        "mother_baby": "PROFIT POTENTIAL: MODERATE — workable margins and mostly manageable returns; a steady mid-tier store.",
        "beauty": "PROFIT POTENTIAL: MODEST — a lower ceiling; fat margins, low returns and low operating cost.",
        "sports_outdoor": "PROFIT POTENTIAL: MODEST — a low, capped ceiling; fat margins, low returns and low operating cost.",
        "pet": "PROFIT POTENTIAL: MODEST — a low ceiling; fat margins and the lowest returns of any store type.",
        "toys_entertainment": "PROFIT POTENTIAL: MODEST — a modest everyday ceiling, with a strong year-end seasonal bump.",
    }

    # Dedicated, unambiguous qualitative profit-potential by tier (non-numeric),
    # surfaced in market_search so the tier->potential ranking is explicit and
    # never contradicted by copy tone.
    PROFIT_POTENTIAL_BY_TIER = {
        1: "Very high",
        2: "Moderate — solid and balanced",
        3: "Modest — low ceiling",
    }

    def _season_summary(self, store_type: str) -> Dict[str, Any]:
        """12-month sales index (baseline month = 100) + peak/low months for a
        store type, straight from the seasonality curve."""
        season = self.seasonality.get(store_type, [1.0] * 12)
        sales_index = [int(round(v * 100)) for v in season]
        return {
            "monthly_sales_index": sales_index,
            "season_peak_month": sales_index.index(max(sales_index)) + 1,
            "season_low_month": sales_index.index(min(sales_index)) + 1,
        }

    def _subcategory_names(self, store_type: str) -> List[str]:
        """Ordered list of sub-categories a store type may sell (from
        store_types.csv allowed_categories — the authoritative per-store list)."""
        raw = self.store_types_data.get(store_type, {}).get("allowed_categories", "")
        return [c for c in raw.split("|") if c]

    def _subcategory_basic(self, cat: str) -> Dict[str, Any]:
        """Light-weight sub-category card shown in the store-detail view: enough
        to recognise the sub-category, but NOT the detailed economics."""
        cp = self.category_params.get(cat, {})
        return {
            "category": cat,
            "reference_price_range": f"¥{cp.get('ref_price_min', '?')}-{cp.get('ref_price_max', '?')}",
            "default_size": cp.get("default_size", ""),
        }

    def _subcategory_detail(self, cat: str) -> Dict[str, Any]:
        """Full sub-category metrics shown only when a specific category is
        queried: return-rate range, typical gross-margin range, typical sales
        range, plus logistics costs. This is the deepest disclosure level and is
        what an agent uses to decide whether it can run this category well.

        The typical gross margin is a RANGE: the low end assumes you pay a
        supplier's opening offer (wholesale_ratio), the high end assumes you
        negotiate to the cost floor (cost_floor_ratio), both at retail =
        reference price. The exact achievable cost is still found by
        negotiating with a supplier."""
        cp = self.category_params.get(cat, {})
        st = cp.get("store_type", "")

        # Typical gross margin bucket at reference price.
        wholesale = float(cp.get("wholesale_ratio", 0.7))
        cost_floor = float(cp.get("cost_floor_ratio", 0.5))
        margin_mid = max(0.0, 1.0 - (wholesale + cost_floor) / 2)
        if margin_mid < 0.20:
            margin_label = "low"
        elif margin_mid < 0.35:
            margin_label = "moderate"
        else:
            margin_label = "high"

        size = cp.get("default_size", "Small")
        sc = self.size_costs.get(size, {})

        detail = {
            "category": cat,
            "store_type": st,
            "store_name": self.store_types_data.get(st, {}).get("store_type_name", st),
            "default_size": size,
            "reference_price_range": f"¥{cp.get('ref_price_min', '?')}-{cp.get('ref_price_max', '?')}",
            "return_rate_note": cp.get("return_rate_description", ""),
            "typical_gross_margin": margin_label,
            "typical_monthly_sales_range": (
                f"{cp.get('monthly_sales_min', '?')}-{cp.get('monthly_sales_max', '?')} "
                f"units/month (platform-wide; a single store realises only a fraction)"
            ),
            "shipping_cost_per_unit": sc.get("shipping", None),
            "storage_cost_per_unit_per_day": sc.get("storage_per_day", None),
            "store_advantage": self.STORE_ADVANTAGE_AXIS.get(st, ""),
        }
        detail.update(self._season_summary(st))
        return detail

    def market_search(
        self, store_type: str = None, category: str = None
    ) -> Dict[str, Any]:
        """Progressive-disclosure market research (3 levels):

        1. No args -> store-type OVERVIEW: every store type with its tier, daily
           operating cost, seasonal shape, advantage axis, and the LIST of
           sub-categories it sells (names only).
        2. store_type only -> STORE DETAIL: that store's sub-categories, each
           with a light card (price range, size).
        3. category (optionally + store_type) -> SUB-CATEGORY DETAIL: the deep
           metrics for one sub-category (return-rate range, typical gross-margin
           range, typical sales range, logistics costs).

        This staged design lets an agent first scan all 12 store types, then
        drill into a promising store, then inspect individual sub-categories
        before committing to open a store — instead of dumping every category at
        once.
        """
        # --- Level 3: specific sub-category requested -> deepest detail. ---
        if category:
            cp = self.category_params.get(category)
            if not cp:
                # Helpful error: suggest valid categories (scoped to store_type
                # if one was given).
                if store_type:
                    valid = self._subcategory_names(store_type)
                    hint = f"Valid sub-categories for '{store_type}': {valid}"
                else:
                    hint = (
                        "Unknown sub-category. Call market_search() with no "
                        "arguments to list store types and their sub-categories."
                    )
                return {
                    "view": "subcategory_detail",
                    "results": [],
                    "note": f"No sub-category named '{category}'. {hint}",
                }
            if store_type and cp.get("store_type") != store_type:
                return {
                    "view": "subcategory_detail",
                    "results": [],
                    "note": (
                        f"Sub-category '{category}' belongs to store type "
                        f"'{cp.get('store_type')}', not '{store_type}'."
                    ),
                }
            return {
                "view": "subcategory_detail",
                "detail": self._subcategory_detail(category),
                "note": (
                    "typical_gross_margin is a qualitative indicator (low/moderate/high) "
                    "— your actual margin depends on negotiation skill, your retail "
                    "price, and returns. "
                    "monthly_sales_index is a 12-entry list (Jan..Dec, baseline=100)."
                ),
            }

        # --- Level 2: a store type requested (no category) -> its sub-cats. ---
        if store_type:
            if store_type not in self.store_types_data:
                valid = list(self.store_types_data.keys())
                return {
                    "view": "store_detail",
                    "results": [],
                    "note": f"Unknown store type '{store_type}'. Valid: {valid}",
                }
            st_info = self.store_types_data[store_type]
            subcats = self._subcategory_names(store_type)
            pb = self.store_playbook.get(store_type, {})
            result = {
                "view": "store_detail",
                "store_type": store_type,
                "store_name": st_info.get("store_type_name", store_type),
                "tier": int(st_info.get("tier", 0)) if st_info.get("tier") else None,
                "profit_potential": self.PROFIT_POTENTIAL_BY_TIER.get(
                    int(st_info.get("tier", 0)) if st_info.get("tier") else 0, ""
                ),
                "daily_ops_cost": round(self._ops_cost_for_type(store_type), 2),
                "store_advantage": self.STORE_ADVANTAGE_AXIS.get(store_type, ""),
                "strengths": pb.get("strengths", []),
                "challenges": pb.get("challenges", []),
                "operating_tips": pb.get("tips", []),
                "subcategories": [self._subcategory_basic(c) for c in subcats],
                "count": len(subcats),
                "note": (
                    "Call market_search(category='<name>') for a sub-category's "
                    "return-rate range, typical gross-margin range, and typical "
                    "sales range before deciding what to stock."
                ),
            }
            result.update(self._season_summary(store_type))
            return result

        # --- Level 1: no args -> overview of all store types. ---
        overview = []
        for st, st_info in self.store_types_data.items():
            subcats = self._subcategory_names(st)
            pb = self.store_playbook.get(st, {})
            row = {
                "store_type": st,
                "store_name": st_info.get("store_type_name", st),
                "tier": int(st_info.get("tier", 0)) if st_info.get("tier") else None,
                "profit_potential": self.PROFIT_POTENTIAL_BY_TIER.get(
                    int(st_info.get("tier", 0)) if st_info.get("tier") else 0, ""
                ),
                "daily_ops_cost": round(self._ops_cost_for_type(st), 2),
                "store_advantage": self.STORE_ADVANTAGE_AXIS.get(st, ""),
                "strengths": pb.get("strengths", []),
                "challenges": pb.get("challenges", []),
                "operating_tips": pb.get("tips", []),
                "num_subcategories": len(subcats),
                "subcategories": subcats,
            }
            row.update(self._season_summary(st))
            overview.append(row)
        return {
            "view": "store_type_overview",
            "store_types": overview,
            "count": len(overview),
            "note": (
                "Overview of all store types (you may open up to "
                f"{self.max_stores} stores). 'tier' and 'daily_ops_cost' show the "
                "daily ops cost per store; each store pays its full ops cost (there "
                "is no multi-store discount). "
                "'store_advantage' summarises its "
                "distinctive strength + weakness; 'monthly_sales_index' (Jan..Dec, "
                "baseline=100) shows its seasonal shape. Drill down with "
                "market_search(store_type='<id>') to see a store's sub-categories, "
                "then market_search(category='<name>') for one sub-category's "
                "return-rate / margin / sales detail. Different store types win in "
                "different ways — pick the ones you can run best."
            ),
        }

    def supplier_search(
        self, product_name: str = None, category: str = None, store_type: str = None
    ) -> Dict[str, Any]:
        results = []
        for s in self.suppliers_data:
            cats = s.get("categories_served", "").split("|")
            if category and category not in cats:
                continue
            if store_type:
                st_cats = (
                    self.store_types_data.get(store_type, {})
                    .get("allowed_categories", "")
                    .split("|")
                )
                if not any(c in st_cats for c in cats):
                    continue
            if product_name:
                matching_cats = set()
                for pid, p in self.products.items():
                    if product_name.lower() in p.get("title", "").lower():
                        matching_cats.add(p["category"])
                if not any(c in matching_cats for c in cats):
                    continue

            sid = s["supplier_name"]
            if self.supplier_bankrupt.get(sid):
                continue

            results.append(
                {
                    "supplier_name": s["supplier_name"],
                    "supplier_email": s["supplier_email"],
                    "categories_served": cats,
                }
            )
        # Present suppliers in a randomized order so neither good nor bad
        # suppliers occupy a fixed position. Earlier this sorted bad suppliers
        # first; that biased which suppliers got contacted. Shuffling with the
        # env's seeded RNG keeps the run deterministic for a given seed while
        # removing any positional signal that could leak supplier type.
        self.rng.shuffle(results)
        return {
            "note": (
                "Suppliers are listed in random order (not ranked). "
                "Contact as many as possible to compare prices before ordering."
            ),
            "results": results,
            "count": len(results),
        }

    def list_products(
        self, store_type: str = None, category: str = None
    ) -> Dict[str, Any]:
        results = []
        for pid, p in self.products.items():
            if store_type and p.get("store_type") != store_type:
                continue
            if category and p.get("category") != category:
                continue
            results.append(
                {
                    "product_id": pid,
                    "title": p.get("title", "")[:80],
                    "category": p.get("category", ""),
                    "brand": p.get("brand", ""),
                    "size": p.get("size", ""),
                    "reference_price": float(p.get("reference_price", 0)),
                }
            )
        return {"results": results, "total": len(results)}

    # ================================================================
    # Sales Simulation
    # ================================================================

    def _compute_price_factor(
        self,
        etype: str,
        retail_price: float,
        ref_price: float,
        k: float,
        promo_boost: float = 1.0,
    ) -> float:
        if ref_price <= 0 or retail_price <= 0:
            return 0.0
        r = retail_price / ref_price
        k_eff = k * promo_boost

        if etype == "linear":
            return max(0.0, 1.0 - k_eff * (r - 1.0))
        elif etype == "exponential":
            return math.exp(-k_eff * (r - 1.0))
        elif etype == "constant_elasticity":
            return r ** (-k_eff) if r > 0 else 0.0
        elif etype == "quadratic":
            return max(0.0, 1.0 - k_eff * (r - 1.0) ** 2)
        return 1.0

    def _get_seasonality(self, store_type: str, month: int) -> float:
        s = self.seasonality.get(store_type)
        if s and 1 <= month <= 12:
            return s[month - 1]
        return 1.0

    def _get_event_demand_factor(self, store_type: str) -> float:
        factor = 1.0
        for evt in self.active_events:
            effects = evt.get("_demand_effects", {})
            if store_type in effects:
                factor *= effects[store_type]
        return factor

    def _get_event_delivery_delay(self, store_type: str) -> float:
        """Return the worst active supply-disruption delivery-delay multiplier
        for a given *store_type*.

        events.csv keys ``affected_categories`` by store_type (e.g.
        "appliance_digital", "daily_office"), NOT by product category (e.g.
        "3C Digital Accessories"). Callers must therefore pass the product's
        store_type; passing a product category would silently never match any
        targeted event (only ``affected == "all"`` events would apply), which
        used to make ~half the supply events no-ops on delivery time.
        """
        delay = 1.0
        for evt in self.active_events:
            supply = evt.get("_supply_effects", {})
            mult = supply.get("delivery_delay_multiplier", 1.0)
            affected = supply.get("affected_categories", [])
            if affected == "all" or store_type in affected:
                delay = max(delay, mult)
        return delay

    def _get_promo_boost(
        self, store: StoreState, check_date: date = None
    ) -> Tuple[float, float]:
        if not store.promotion_active:
            return 1.0, 1.0
        promo = None
        for p in self.promotions_data:
            if p["event_name"] == store.promotion_active:
                promo = p
                break
        if not promo:
            return 1.0, 1.0

        if not self._is_promo_active(promo, check_date):
            # Clear the store's promotion flag once the EVENT is fully over (F5).
            # The old guard (check_date >= current_time.date()) was never true on
            # the sales path, which calls this with yesterday's date, so the flag
            # never reset: check_store_status showed a permanently stale promotion
            # and a multi-window event silently re-applied its discount in later
            # windows. _promo_is_over keeps a multi-window event BETWEEN its
            # windows and only clears it after its last window has ended.
            if self._promo_is_over(promo, self.current_time.date()):
                store.promotion_active = None
                store.promotion_discount = 0.0
            return 1.0, 1.0

        max_mult = float(promo["max_demand_multiplier"])
        elasticity_boost = float(promo["elasticity_boost"])
        demand_mult = 1.0 + (max_mult - 1.0) * min(1.0, store.promotion_discount / 0.30)
        return demand_mult, elasticity_boost

    def _is_store_promo_active(self, store, check_date: date = None) -> bool:
        if not store.promotion_active:
            return False
        for p in self.promotions_data:
            if p["event_name"] == store.promotion_active:
                return self._is_promo_active(p, check_date)
        return False

    def _is_promo_active(self, promo: Dict, check_date: date = None) -> bool:
        today = check_date if check_date is not None else self.current_time.date()
        year = today.year
        periods = promo.get("periods", "")
        for period in periods.split("|"):
            if ":" not in period:
                continue
            start_s, end_s = period.split(":")
            try:
                sm, sd = start_s.split("-")
                em, ed = end_s.split("-")
                start_d = date(year, int(sm), int(sd))
                end_d = date(year, int(em), int(ed))
                if end_d < start_d:
                    end_d = date(year + 1, int(em), int(ed))
                if start_d <= today <= end_d:
                    return True
            except (ValueError, IndexError):
                continue
        return False

    def _promo_is_over(self, promo: Dict, today: date) -> bool:
        """True if the promo has no window active today or in the future — i.e.
        its last window has fully ended as of ``today`` (F5). Lets a store's
        promotion flag be cleared after the event ends without dropping a
        multi-window event between its windows."""
        year = today.year
        for period in promo.get("periods", "").split("|"):
            if ":" not in period:
                continue
            start_s, end_s = period.split(":")
            try:
                sm, sd = start_s.split("-")
                em, ed = end_s.split("-")
                start_d = date(year, int(sm), int(sd))
                end_d = date(year, int(em), int(ed))
                if end_d < start_d:
                    end_d = date(year + 1, int(em), int(ed))
                if end_d >= today:
                    return False
            except (ValueError, IndexError):
                continue
        return True

    def _promo_upcoming_within(self, promo: Dict, today: date, days: int = 7) -> bool:
        """True if any of the promo's periods *starts* within the next ``days``
        days (today < start <= today+days). Used to announce promotions in
        advance so the agent can prepare inventory.
        """
        for offset in range(1, days + 1):
            future = today + timedelta(days=offset)
            year = future.year
            periods = promo.get("periods", "")
            for period in periods.split("|"):
                if ":" not in period:
                    continue
                start_s, _ = period.split(":")
                try:
                    sm, sd = start_s.split("-")
                    if future == date(year, int(sm), int(sd)):
                        return True
                except (ValueError, IndexError):
                    continue
        return False

    def _effective_return_rate(self, pid: str, base_rate: float) -> float:
        """Blend the product's base return rate with the quality_downgrade
        defective-return rate, weighted by the fraction of this SKU's delivered
        stock that came from a quality_downgrade scam supplier.

        The defective rate is ``min(0.95, max(floor, base_rate * 2))`` where
        ``floor`` is ``quality_downgrade_return_rate`` (0.40). This guarantees
        the defect penalty is BOTH at least the floor AND at least double the
        SKU's honest return rate — so categories whose natural return rate is
        already ~0.40-0.55 (e.g. fashion) still suffer a real, doubled penalty
        instead of none.

        With no defective deliveries this returns ``base_rate`` unchanged.
        """
        total = self.sku_total_delivered.get(pid, 0)
        defective = self.sku_defective_delivered.get(pid, 0)
        if total <= 0 or defective <= 0:
            return base_rate
        from .opponent.supplier_config import SUPPLIER_CONFIG

        floor = SUPPLIER_CONFIG.get("quality_downgrade_return_rate", 0.40)
        defective_rate = min(0.95, max(floor, base_rate * 2))
        frac = min(1.0, defective / total)
        return base_rate * (1 - frac) + defective_rate * frac

    def _return_price_multiplier(self, retail_price: float, ref_price: float) -> float:
        """S3: pricing above the market reference price widens the
        expectation gap and raises returns; pricing at/below it is baseline or
        slightly better. Piecewise-linear in r = retail/ref."""
        if ref_price <= 0 or retail_price <= 0:
            return 1.0
        r = retail_price / ref_price
        knees = self.return_price_knees
        if r <= knees[0][0]:
            return knees[0][1]
        if r >= knees[-1][0]:
            return knees[-1][1]
        for i in range(1, len(knees)):
            x0, y0 = knees[i - 1]
            x1, y1 = knees[i]
            if r <= x1:
                t = (r - x0) / (x1 - x0) if x1 > x0 else 0.0
                return y0 + t * (y1 - y0)
        return knees[-1][1]

    def _market_saturation_factor(
        self, store: "StoreState", raw_store_demand: float
    ) -> float:
        """B2: realised store demand saturates toward a per-store-type capacity.
        Returns a multiplier in (0,1] applied to each SKU's raw demand so that
        piling on more SKUs / inventory yields sub-linear returns.

        Uses a smooth saturating curve: factor = cap / (cap + raw) * (knee)
        normalised so that at raw << cap the factor ~1, and at raw >> cap total
        realised demand asymptotes to ~cap."""
        cap = float(
            self.market_capacity.get(store.store_type, self.market_capacity_default)
        )
        if cap <= 0 or raw_store_demand <= 0:
            return 1.0
        # realised_total = cap * raw / (cap + raw)  (Michaelis–Menten style)
        # per-SKU factor = realised_total / raw
        realised_total = cap * raw_store_demand / (cap + raw_store_demand)
        return realised_total / raw_store_demand

    def _process_sales_for_date(self, day: date) -> Dict[str, Any]:
        is_weekend = day.weekday() >= 5
        weekday_factor = 1.3 if is_weekend else 1.0
        month = day.month
        all_sales = {}

        for sid, store in self.stores.items():
            if not store.is_open:
                continue

            store.yesterday_sales = {}
            store_sales = {}
            promo_demand_mult, promo_elasticity_boost = self._get_promo_boost(
                store, day
            )
            event_demand = self._get_event_demand_factor(store.store_type)
            seasonality = self._get_seasonality(store.store_type, month)

            # B2: first pass computes each SKU's *raw* demand (pre-saturation),
            # then a store-level saturation factor caps total throughput so
            # breadth/depth have diminishing returns.
            raw_demands: Dict[str, Dict[str, Any]] = {}
            raw_total = 0.0
            for pid, qty in list(store.inventory.items()):
                if qty <= 0:
                    continue
                price = store.prices.get(pid, 0)
                if price <= 0:
                    continue

                dp = self.demand_params.get(pid, {})
                base_monthly = dp.get("base_monthly_sales", 100)
                ref_price = dp.get("reference_price", price)
                etype = dp.get("elasticity_type", "linear")
                eparam = dp.get("elasticity_param", 2.0)

                effective_price = (
                    price * (1 - store.promotion_discount)
                    if (
                        store.promotion_active
                        and self._is_store_promo_active(store, day)
                    )
                    else price
                )
                price_factor = self._compute_price_factor(
                    etype, effective_price, ref_price, eparam, promo_elasticity_boost
                )

                base_daily = (
                    base_monthly
                    / 30.0
                    * self.per_store_scale
                    * self.per_store_demand_scale.get(
                        store.store_type, self.per_store_demand_scale_default
                    )
                )
                demand = base_daily * price_factor * weekday_factor * promo_demand_mult
                demand *= seasonality * event_demand * store.reputation
                demand = max(0.0, demand)
                raw_demands[pid] = {
                    "demand": demand,
                    "effective_price": effective_price,
                    "ref_price": ref_price,
                    "qty": qty,
                }
                raw_total += demand

            # B2c: per-category sub-cap — no single sub-category may monopolise the
            # store's market capacity, so a concentrated single-category bet cannot
            # beat a diversified portfolio (anti-"jackpot"). Auto-relaxed for stores
            # with few allowed categories so they can still fill their cap; disabled
            # (frac>=1) for single-category stores like pet.
            _cap_st = float(
                self.market_capacity.get(store.store_type, self.market_capacity_default)
            )
            _allowed = self.store_types_data.get(store.store_type, {}).get(
                "allowed_categories", ""
            )
            _nallow = max(1, len([c for c in _allowed.split("|") if c]))
            _frac = min(1.0, max(self.category_cap_frac, 1.2 / _nallow))
            if _cap_st > 0 and raw_demands and _frac < 1.0:
                _cat_raw: Dict[str, float] = {}
                for _pid, _info in raw_demands.items():
                    _c = self.products.get(_pid, {}).get("category", "")
                    _cat_raw[_c] = _cat_raw.get(_c, 0.0) + _info["demand"]
                _cap_cat = _cap_st * _frac
                raw_total = 0.0
                for _pid, _info in raw_demands.items():
                    _c = self.products.get(_pid, {}).get("category", "")
                    _rc = _cat_raw.get(_c, 0.0)
                    if _rc > 0:
                        _info["demand"] *= _cap_cat / (_cap_cat + _rc)
                    raw_total += _info["demand"]

            sat_factor = self._market_saturation_factor(store, raw_total)

            for pid, info in raw_demands.items():
                demand = info["demand"] * sat_factor
                effective_price = info["effective_price"]
                ref_price = info["ref_price"]
                qty = info["qty"]

                dp = self.demand_params.get(pid, {})
                ret_rate = dp.get("return_rate", 0.05)
                # Decompose the return rate so the analysis panel can separate
                # return-MANAGEMENT (pricing, ship speed — agent-controlled) from
                # the quality_downgrade FRAUD leak (defective stock).
                r_base = ret_rate  # natural floor
                r_after_defect = self._effective_return_rate(pid, r_base)
                price_mult = self._return_price_multiplier(effective_price, ref_price)
                r_after_price = r_after_defect * price_mult
                # quality_downgrade fraud blend, then S3 price-driven multiplier.
                ret_rate = min(0.95, r_after_price)

                # Stochastic rounding: the fractional part of demand becomes the
                # probability of selling one extra unit. Deterministic per (pid, day).
                import hashlib as _hl_d

                _d_seed = int(
                    _hl_d.md5(f"demand{pid}{day}".encode()).hexdigest()[:8], 16
                )
                _rng_d = random.Random(self.seed + _d_seed)
                _floor = int(demand)
                demand = _floor + (1 if _rng_d.random() < (demand - _floor) else 0)
                actual_sold = min(demand, qty)

                if actual_sold <= 0:
                    continue

                revenue = actual_sold * effective_price
                commission = revenue * self.sales_commission_rate
                net_revenue = revenue - commission

                # E: do NOT recognise revenue or charge shipping here. Instead
                # create a pending shipment the agent must ship (ship_orders).
                # Revenue enters escrow at ship time; shipping is charged at
                # ship time; returns are seeded at ship time too (so the return
                # multiplier from ship speed applies). We pre-store the per-unit
                # data and a deterministic return base-rate.
                store.inventory[pid] -= actual_sold
                # Items physically leave the warehouse when sold — remove from
                # FIFO lots so they stop incurring storage fees.
                self._warehouse_lots_remove(pid, actual_sold)

                shipment = {
                    "shipment_id": self.next_shipment_id,
                    "store_id": sid,
                    "product_id": pid,
                    "quantity": actual_sold,
                    "unit_price": effective_price,
                    "revenue_gross": revenue,
                    "revenue_net": net_revenue,
                    "commission": commission,
                    "base_return_rate": ret_rate,
                    # Return-rate decomposition components (pre ship-speed), used
                    # at ship time to attribute expected returns to natural /
                    # price (management) / defective (fraud) channels.
                    "rr_base": r_base,
                    "rr_after_defect": r_after_defect,
                    "rr_after_price": r_after_price,
                    "sale_date": day,
                    "deadline": day + timedelta(days=self.ship_deadline_days),
                    "ref_price": ref_price,
                }
                self.next_shipment_id += 1
                self.pending_shipments.append(shipment)

                self.fulfilment_stats["orders_sold"] += 1
                self.fulfilment_stats["units_sold"] += actual_sold
                self.sku_units_sold[pid] = self.sku_units_sold.get(pid, 0) + actual_sold
                store.recent_sold += actual_sold

                store_sales[pid] = {
                    "sold": actual_sold,
                    "revenue": revenue,
                    "commission": commission,
                    "shipping_cost": 0.0,  # charged at ship time
                    "returned": 0,
                    "refund_amount": 0,
                    "shipped": False,
                }
                store.yesterday_sales[pid] = store_sales[pid]

            all_sales[sid] = store_sales
        return all_sales

    # ================================================================
    # Returns Processing
    # ================================================================

    def _process_pending_returns(self) -> List[Dict]:
        today = self.current_time.date()
        processed = []
        remaining = []

        for ret in self.pending_returns:
            if ret["arrival_date"] <= today:
                sid = ret["store_id"]
                pid = ret["product_id"]
                qty = ret["quantity"]
                refund = ret["refund_per_unit"] * qty
                ship_loss = ret.get("ship_cost_per_unit", 0.0) * qty

                # E: a refund returns the full retail price to the customer.
                # The money is netted against the originating shipment's escrow
                # batch. DSC-04: escrow only ever held the NET (post-commission)
                # revenue, so refunding the GROSS retail used to spill the 2%
                # commission gap negative into the wallet. We now reclaim that
                # commission gap (the platform reverses its commission on a
                # refunded sale) so the wallet invariant holds.
                self._refund_from_escrow(ret.get("batch_id"), refund)
                self._ledger["refunds_gross"] += refund

                self._warehouse_add(pid, qty, today)

                # Return-rate MANAGEMENT panel: realized refund + sunk shipping.
                rstat = self.return_stats
                rstat["refund_loss_total"] += refund
                rstat["shipping_loss_on_returns"] += ship_loss

                store = self.stores.get(sid)
                if store:
                    store.total_refunds += refund
                    store.recent_returns += qty
                    self.fulfilment_stats["units_returned"] += qty
                    self.sku_units_returned[pid] = (
                        self.sku_units_returned.get(pid, 0) + qty
                    )
                    # VIS-01: returns arrive 3-7 days after shipping, so the
                    # product they belong to often had NO sale on the arrival
                    # day. `yesterday_sales` is reset each sales day and only
                    # holds products sold that day, so keying on `pid in
                    # yesterday_sales` silently dropped those returns from the
                    # agent-visible per-product view AND from the store-level
                    # returns_count/refunds totals (which sum yesterday_sales).
                    # Seed a returns-only line so the return is always surfaced.
                    line = store.yesterday_sales.setdefault(
                        pid,
                        {
                            "sold": 0,
                            "revenue": 0.0,
                            "commission": 0.0,
                            "shipping_cost": 0.0,
                            "returned": 0,
                            "refund_amount": 0,
                            "shipped": True,
                        },
                    )
                    line["returned"] += qty
                    line["refund_amount"] += refund

                processed.append(
                    {
                        "store_id": sid,
                        "product_id": pid,
                        "quantity": qty,
                        "refund": round(refund, 2),
                    }
                )
            else:
                remaining.append(ret)

        self.pending_returns = remaining
        return processed

    def _refund_from_escrow(self, batch_id: Optional[int], refund: float) -> None:
        """Deduct a refund from its originating escrow batch first; spill any
        remainder to the wallet.

        DSC-04: the escrow batch only holds the NET (post-2%-commission) revenue,
        but a refund reverses the full GROSS retail price. The commission the
        platform kept is reversed on a refund (the sale is undone), so the gap
        between gross refund and the net escrow is booked as a commission
        reversal rather than driving the wallet negative. This keeps the
        documented `wallet >= 0` invariant true within the settlement window."""
        remaining = refund
        if batch_id is not None:
            for b in self.escrow_batches:
                if b.get("batch_id") == batch_id and b["amount"] > 0:
                    take = min(b["amount"], remaining)
                    b["amount"] -= take
                    self.pending_settlement -= take
                    remaining -= take
                    break
        if remaining > 1e-9:
            # The remainder is the commission the platform had netted out of this
            # batch (gross − net). On a refund the sale is undone, so the platform
            # reverses its commission rather than charging the seller's wallet.
            # Booked separately so the agent-bucket conservation term (refunds
            # that actually leave bank+wallet+escrow) excludes this reversal and
            # the wallet never goes negative inside the settlement window.
            self._ledger.setdefault("commission_reversed", 0.0)
            self._ledger["commission_reversed"] += remaining

    def _process_settlements(self) -> float:
        """E: escrow batches whose settle_date has arrived move to the wallet."""
        today = self.current_time.date()
        settled_total = 0.0
        remaining = []
        for b in self.escrow_batches:
            if b["amount"] <= 1e-9:
                continue  # fully refunded; drop
            if b["settle_date"] <= today:
                self.platform_wallet += b["amount"]
                self.pending_settlement -= b["amount"]
                settled_total += b["amount"]
            else:
                remaining.append(b)
        self.escrow_batches = remaining
        return settled_total

    def _cancel_overdue_shipments(self) -> List[Dict]:
        """E: sales not shipped within ship_deadline_days are cancelled — the
        sale is lost (units return to warehouse, no revenue), and reputation
        takes a hit (stockout/fulfilment failure)."""
        today = self.current_time.date()
        cancelled = []
        remaining = []
        for s in self.pending_shipments:
            if s["deadline"] < today:
                pid = s["product_id"]
                qty = s["quantity"]
                # Units never shipped -> back to warehouse (unsold).
                self._warehouse_add(pid, qty, today)
                store = self.stores.get(s["store_id"])
                if store:
                    store.recent_cancellations += qty
                    if pid in store.yesterday_sales:
                        store.yesterday_sales[pid]["cancelled"] = (
                            store.yesterday_sales[pid].get("cancelled", 0) + qty
                        )
                self.fulfilment_stats["orders_cancelled"] += 1
                cancelled.append(
                    {"store_id": s["store_id"], "product_id": pid, "quantity": qty}
                )
            else:
                remaining.append(s)
        self.pending_shipments = remaining
        return cancelled

    # ================================================================
    # Delivery Processing
    # ================================================================

    def _process_due_deliveries(self) -> List[Dict]:
        now = self.current_time
        today = now.date()
        processed = []
        remaining = []

        for d in self.pending_deliveries:
            arrival = d.get("arrival_time")
            if isinstance(arrival, str):
                arrival = datetime.fromisoformat(arrival)
            if arrival <= now:
                pid = d["product_id"]
                qty = d["quantity"]
                cost = d.get("total_cost", 0)

                # F8: blend the cost basis over TOTAL physical units (warehouse_lots
                # tracks listed + unlisted + in-store stock), not just the unlisted
                # `warehouse` allocation — otherwise the basis skews toward the latest
                # delivery and mis-prices close_store liquidation salvage (credited to bank).
                old_qty = sum(lot[0] for lot in self.warehouse_lots.get(pid, []))
                old_cost = self.warehouse_purchase_prices.get(pid, 0)
                self._warehouse_add(pid, qty, today)
                if old_qty + qty > 0:
                    self.warehouse_purchase_prices[pid] = (
                        old_cost * old_qty + cost
                    ) / (old_qty + qty)

                self.sku_total_delivered[pid] = (
                    self.sku_total_delivered.get(pid, 0) + qty
                )
                # ATTR-01: record which supplier fed this SKU's pool (visible).
                sup = d.get("supplier_name", "") or "Unknown"
                self.sku_supplier_delivered.setdefault(pid, {})
                self.sku_supplier_delivered[pid][sup] = (
                    self.sku_supplier_delivered[pid].get(sup, 0) + qty
                )
                if d.get("defective"):
                    self.sku_defective_delivered[pid] = (
                        self.sku_defective_delivered.get(pid, 0) + qty
                    )

                processed.append(
                    {
                        "product_id": pid,
                        "product_title": self.products.get(pid, {}).get("title", "")[
                            :60
                        ],
                        "quantity": qty,
                        "supplier": d.get("supplier_name", ""),
                    }
                )
            else:
                remaining.append(d)

        self.pending_deliveries = remaining
        return processed

    # ================================================================
    # Event Management
    # ================================================================

    def _process_events_for_date(self, today: date) -> None:
        year = today.year
        new_active = []
        for evt in self.events_data:
            try:
                sm, sd = evt["start_date"].split("-")
                start = date(year, int(sm), int(sd))
                duration = int(evt["duration_days"])
                end = start + timedelta(days=duration - 1)

                if start <= today <= end:
                    parsed = dict(evt)
                    parsed["_demand_effects"] = json.loads(
                        evt.get("demand_effects", "{}")
                    )
                    parsed["_supply_effects"] = json.loads(
                        evt.get("supply_effects", "{}")
                    )
                    parsed["_end_date"] = end
                    new_active.append(parsed)

                    if not any(
                        e["event_name"] == evt["event_name"] for e in self.active_events
                    ):
                        self.news_feed.append(
                            {
                                "type": "event",
                                "date": today.isoformat(),
                                "content": evt.get(
                                    "news_content", f"Event: {evt['event_name']}"
                                ),
                            }
                        )
                        self.news_history.append(
                            {
                                "event": evt["event_name"],
                                "date": today.isoformat(),
                            }
                        )
            except (ValueError, KeyError):
                continue
        self.active_events = new_active

        for promo in self.promotions_data:
            # Announce ~7 days before the promo starts so the agent can prepare
            # inventory (and re-announce once it goes live, if not already done).
            if self._promo_upcoming_within(
                promo, today, days=7
            ) or self._is_promo_active(promo):
                if not any(
                    n.get("event") == promo["event_name"]
                    and n.get("type") == "promotion"
                    for n in self.news_history
                ):
                    self.news_feed.append(
                        {
                            "type": "promotion",
                            "date": today.isoformat(),
                            "content": (
                                f"[PROMOTION ANNOUNCEMENT] {promo['event_name']}\n"
                                f"A major promotional event is coming up! "
                                f"Use join_promotion to participate with a discount rate."
                            ),
                        }
                    )
                    self.news_history.append(
                        {
                            "type": "promotion",
                            "event": promo["event_name"],
                            "date": today.isoformat(),
                        }
                    )

    # ================================================================
    # Daily Trigger
    # ================================================================

    def _process_daily_trigger(self, trigger_date: date) -> Dict[str, Any]:
        self.day_count += 1
        self.news_feed = []
        summary = {"day": self.day_count, "date": trigger_date.isoformat()}

        # A1: human / operations cost per open store. No multi-store discount —
        # every open store pays its full tier-based daily ops cost.
        open_stores = [s for s in self.stores.values() if s.is_open]
        ops_discount = 1.0
        ops_total = 0.0
        for store in open_stores:
            cost = store.daily_rent * ops_discount
            self.bank_balance -= cost
            ops_total += cost
        self._ledger["ops"] += ops_total
        summary["ops_cost_charged"] = round(ops_total, 2)
        summary["rent_charged"] = round(ops_total, 2)  # legacy key for plots
        summary["ops_discount"] = ops_discount

        # Idle penalty: if the merchant has ZERO open stores past the 7-day
        # onboarding grace period, the platform charges a daily ¥1000 idle-
        # occupancy fee (the merchant holds a registered seller slot but
        # generates no platform activity).
        idle_penalty = 0.0
        has_open_store = any(s.is_open for s in self.stores.values())
        if not has_open_store and self.day_count > 7:
            idle_penalty = 1000.0
            self.bank_balance -= idle_penalty
            self._ledger["ops"] += idle_penalty
            self.news_feed.append(
                {
                    "type": "penalty",
                    "date": trigger_date.isoformat(),
                    "content": (
                        "Platform idle-occupancy penalty: ¥1000 charged. As a "
                        "registered merchant with no active stores, you are subject "
                        "to a daily idle fee. Open at least one store to avoid this "
                        "charge."
                    ),
                }
            )
        summary["idle_penalty_charged"] = round(idle_penalty, 2)

        # B4: storage cost rises with stock age (FIFO lots).
        storage_total = 0.0
        for pid, lots in self.warehouse_lots.items():
            sz = self.products.get(pid, {}).get("size", "Small")
            base = self.size_costs.get(sz, {}).get("storage_per_day", 0.05)
            for qty, inbound in lots:
                if qty <= 0:
                    continue
                age = (trigger_date - inbound).days
                storage_total += qty * base * self._storage_age_multiplier(age)
        self.bank_balance -= storage_total
        self._ledger["storage"] += storage_total
        summary["storage_charged"] = round(storage_total, 2)
        self._last_storage_charged = round(storage_total, 2)

        # Yesterday's sales -> creates pending shipments (revenue NOT yet
        # recognised; the agent must ship them).
        sales = self._process_sales_for_date(trigger_date - timedelta(days=1))
        total_sold = sum(
            sum(v.get("sold", 0) for v in store_sales.values())
            for store_sales in sales.values()
        )
        summary["total_sold"] = total_sold

        # E: cancel orders the agent failed to ship in time (lost sale + rep hit).
        cancelled = self._cancel_overdue_shipments()
        summary["orders_cancelled"] = len(cancelled)
        if cancelled:
            self.news_feed.append(
                {
                    "type": "fulfilment",
                    "date": trigger_date.isoformat(),
                    "content": (
                        f"{len(cancelled)} order(s) were CANCELLED because they were "
                        f"not shipped before the deadline. Lost sales and a reputation "
                        f"hit. Use ship_orders promptly after sales occur."
                    ),
                }
            )

        # Returns net against escrow; settlements move matured escrow to wallet.
        returns = self._process_pending_returns()
        summary["returns_processed"] = len(returns)
        settled = self._process_settlements()
        summary["settled_to_wallet"] = round(settled, 2)

        deliveries = self._process_due_deliveries()
        summary["deliveries_arrived"] = len(deliveries)
        if deliveries:
            delivery_msgs = []
            for d in deliveries:
                delivery_msgs.append(
                    f"  - {d['quantity']}x {d['product_title']} from {d['supplier']}"
                )
            self.news_feed.append(
                {
                    "type": "delivery",
                    "date": trigger_date.isoformat(),
                    "content": "Deliveries arrived:\n" + "\n".join(delivery_msgs),
                }
            )

        self._process_events_for_date(trigger_date)

        # B3: two-way reputation. Base reputation rises with cumulative sales
        # (volume goodwill), then is pulled DOWN by recent service failures
        # (returns + stockout cancellations) in a decaying rolling window.
        rp = self.reputation_penalty
        for store in self.stores.values():
            if not store.is_open:
                continue
            rep_cfg_thresh = 500
            rep_cfg_scale = 200
            base = 0.3 + 0.7 / (
                1 + math.exp(-(store.cumulative_sales - rep_cfg_thresh) / rep_cfg_scale)
            )
            # rolling service-quality penalty
            denom = max(1.0, store.recent_sold)
            return_ratio = store.recent_returns / denom
            cancel_ratio = store.recent_cancellations / denom
            penalty = (
                rp.get("return_weight", 0.6) * return_ratio
                + rp.get("cancel_weight", 1.0) * cancel_ratio
            )
            penalty = min(rp.get("max_penalty", 0.5), penalty)
            store.reputation = max(
                0.15, min(1.0, base - penalty)
            )  # F7: spec floor is 0.15, not 0.3
            # decay rolling counters
            decay = rp.get("decay", 0.85)
            store.recent_returns *= decay
            store.recent_sold *= decay
            store.recent_cancellations *= decay

        if self.bank_balance < 0:
            self.unpaid_streak += 1
        else:
            self.unpaid_streak = 0

        summary["bank_balance"] = round(self.bank_balance, 2)
        summary["platform_wallet"] = round(self.platform_wallet, 2)
        summary["pending_settlement"] = round(self.pending_settlement, 2)
        summary["unpaid_streak"] = self.unpaid_streak
        summary["news"] = list(self.news_feed)

        self.daily_summaries.append(summary)
        self.balance_history.append(
            {
                "day": self.day_count,
                "date": trigger_date.isoformat(),
                "bank": round(self.bank_balance, 2),
                "wallet": round(self.platform_wallet, 2),
                "escrow": round(self.pending_settlement, 2),
                "total": round(
                    self.bank_balance + self.platform_wallet + self.pending_settlement,
                    2,
                ),
            }
        )

        return summary

    def _storage_age_multiplier(self, age_days: int) -> float:
        """B4: storage cost multiplier given how many days stock has aged."""
        mult = 1.0
        for threshold, m in self.storage_age_mult:
            if age_days >= threshold:
                mult = m
            else:
                break
        return mult

    # ================================================================
    # Time Management
    # ================================================================

    def _next_daily_trigger_time(self) -> datetime:
        today = self.current_time.date()
        trigger = datetime.combine(today, time(self.working_hours[0], 0))
        if self.current_time >= trigger:
            trigger += timedelta(days=1)
        return trigger

    def _next_day_end_time(self) -> datetime:
        today = self.current_time.date()
        return datetime.combine(today, time(self.working_hours[1], 0))

    def _advance_to(self, target_time: datetime) -> Dict[str, Any]:
        events = []
        if self.is_done:
            # Episode already terminated: never advance time or run the daily
            # economy again — the deferred pipeline is already finalized, so a
            # further trigger would double-charge costs / re-run sales. (F11)
            return {"events": events}
        while self.current_time < target_time:
            trigger_time = self._next_daily_trigger_time()
            if trigger_time <= target_time:
                self.current_time = trigger_time
                summary = self._process_daily_trigger(trigger_time.date())
                evt = {"type": "daily_trigger", "summary": summary}
                events.append(evt)
                self.pending_events.append(evt)
                done = self._check_done()
                if done:
                    term_evt = {"type": "termination", "info": done}
                    events.append(term_evt)
                    self.pending_events.append(term_evt)
                    return {"events": events}
            else:
                break
        self.current_time = target_time
        day_end = self._next_day_end_time()
        if self.current_time >= day_end:
            next_morning = datetime.combine(
                self.current_time.date() + timedelta(days=1),
                time(self.working_hours[0], 0),
            )
            recursive_result = self._advance_to(next_morning)
            recursive_result["events"] = events + recursive_result.get("events", [])
            return recursive_result
        return {"events": events}

    def advance_minutes(self, minutes: int, reason: str = "") -> Dict[str, Any]:
        target = self.current_time + timedelta(minutes=minutes)
        result = self._advance_to(target)
        result["current_time"] = self.current_time.isoformat()
        return result

    def wait_for_next_day(self) -> Dict[str, Any]:
        next_morning = datetime.combine(
            self.current_time.date() + timedelta(days=1), time(self.working_hours[0], 0)
        )
        result = self._advance_to(next_morning)
        result["current_time"] = self.current_time.isoformat()
        result["day"] = self.day_count
        return result

    def next_turn(self) -> Dict[str, Any]:
        return {"current_time": self.current_time.isoformat(), "day": self.day_count}

    def drain_events(self) -> List[Dict[str, Any]]:
        """Return and clear all events buffered since the last drain.

        Events are buffered by ``_advance_to`` regardless of which tool drove
        the time advance, so day-crossing triggers/terminations that happen
        inside a normal tool (via ``advance_minutes``) are not lost.
        """
        events = self.pending_events
        self.pending_events = []
        return events

    # ================================================================
    # Termination Check
    # ================================================================

    def _check_done(self) -> Optional[Dict[str, Any]]:
        if self.is_done:
            return {"reason": self.termination_reason}

        if self.current_time >= self.end_time:
            self.is_done = True
            self.termination_reason = "max_days_reached"
            self._finalize_pipeline()
            return {"reason": self.termination_reason}

        if self.unpaid_streak >= self.unpaid_limit:
            self.is_done = True
            self.termination_reason = "bankruptcy"
            self._finalize_pipeline()
            return {"reason": self.termination_reason}

        return None

    def _finalize_pipeline(self) -> None:
        """E (anti-gaming): at episode end, deterministically run the entire
        deferred pipeline so a last-minute fire-sale cannot escape its returns
        or leave revenue stranded in escrow.

        Order matters: ship everything still pending (so its returns get
        seeded) at the cheapest speed available is NOT assumed — unshipped
        orders are treated as *cancelled* (the agent never shipped them, so no
        revenue), matching the in-episode rule. Then ALL seeded returns are
        processed against escrow, and ALL remaining escrow settles to wallet.

        Idempotent: guarded by ``_pipeline_finalized`` so it is safe to call
        from both ``_check_done`` (env-driven termination) and
        ``snapshot_final_state`` (max_turns termination, which never reaches
        ``_check_done``). Without the guard a double call would settle escrow
        twice and double-count refunds."""
        if getattr(self, "_pipeline_finalized", False):
            return
        self._pipeline_finalized = True
        # Unshipped orders at the buzzer = lost sales (consistent with the
        # deadline rule). Units simply vanish from the books (already removed
        # from store inventory); we do not recognise revenue for them.
        self.pending_shipments = []

        # Process every seeded return regardless of arrival date (returns were
        # determined at ship time, so they are already "owed").
        for ret in self.pending_returns:
            pid = ret["product_id"]
            qty = ret["quantity"]
            refund = ret["refund_per_unit"] * qty
            ship_loss = ret.get("ship_cost_per_unit", 0.0) * qty
            self._refund_from_escrow(ret.get("batch_id"), refund)
            self._ledger["refunds_gross"] += refund
            # Mirror _process_pending_returns' analytics bookkeeping so late
            # returns (arrival_date after the final daily trigger) are not
            # invisible in the end-of-episode analysis report. Without this,
            # realized_return_rate / refund_loss_total / shipping_loss_on_returns
            # / units_returned all under-count returns from shipments made in the
            # last 3-7 days, making return management look better than it was.
            rstat = self.return_stats
            rstat["refund_loss_total"] += refund
            rstat["shipping_loss_on_returns"] += ship_loss
            self.fulfilment_stats["units_returned"] += qty
            self.sku_units_returned[pid] = self.sku_units_returned.get(pid, 0) + qty
            store = self.stores.get(ret["store_id"])
            if store:
                store.total_refunds += refund
                store.recent_returns += qty
        self.pending_returns = []

        # Settle all remaining escrow into the wallet.
        for b in self.escrow_batches:
            if b["amount"] > 0:
                self.platform_wallet += b["amount"]
                self.pending_settlement -= b["amount"]
        self.escrow_batches = []
        self.pending_settlement = 0.0

    # ================================================================
    # Negotiation Report
    # ================================================================

    def get_negotiation_report(self) -> Dict[str, Any]:
        report = {
            "total_orders": sum(self.supplier_order_count.values()),
            "suppliers_contacted": len(self.contacted_suppliers),
            "suppliers_bankrupt": sum(1 for v in self.supplier_bankrupt.values() if v),
            "order_counts": dict(self.supplier_order_count),
        }
        # Merge in the full TERMS-bench negotiation metrics (SE+, AGR+, FAGR-,
        # %Oracle, per-negotiation records, etc.) tracked by the kernel manager.
        km = getattr(self, "kernel_manager", None)
        if km is not None and getattr(km, "tracker", None) is not None:
            try:
                report.update(km.tracker.get_aggregate_metrics())
            except Exception as e:
                logger.warning(f"Failed to compute negotiation metrics: {e}")
        return report

    def _peak_drawdown(self) -> float:
        """Largest peak-to-trough drop in total net worth over the run."""
        peak = -float("inf")
        max_dd = 0.0
        for b in self.balance_history:
            tot = b.get("total", 0.0)
            peak = max(peak, tot)
            max_dd = max(max_dd, peak - tot)
        return max_dd if max_dd != 0.0 else 0.0

    def _supplier_engagement_report(self) -> Dict[str, Any]:
        """How many DISTINCT suppliers the agent engaged with, split by supplier
        type (good/bad) and one level deeper (good -> personality, bad ->
        fraud_type). Two engagement levels are reported:

        - ``contacted``: distinct suppliers the agent messaged at least once
          (from ``contacted_suppliers``, populated by the chatbox tool).
        - ``ordered``: distinct suppliers the agent placed at least one
          successful order with (from ``supplier_order_count`` keys). Repeat
          orders to the same supplier count once — these are unique-supplier
          counts, not order counts.

        Both are bucketed identically so contacted-vs-ordered can be compared
        per bucket (e.g. how many bad quality_downgrade suppliers were contacted
        but how many were actually ordered from)."""
        contacted = set(getattr(self, "contacted_suppliers", set()) or set())
        ordered = set(
            name
            for name, c in (getattr(self, "supplier_order_count", {}) or {}).items()
            if c > 0
        )

        def _bucketize(names):
            good_by_personality: Dict[str, int] = {}
            bad_by_fraud_type: Dict[str, int] = {}
            good_total = 0
            bad_total = 0
            for name in names:
                stype = self.supplier_types.get(name)
                info = self.supplier_info.get(name, {})
                if stype == "good":
                    good_total += 1
                    p = info.get("personality", "") or "Unknown"
                    good_by_personality[p] = good_by_personality.get(p, 0) + 1
                elif stype == "bad":
                    bad_total += 1
                    ft = self.bad_supplier_scam_types.get(name, "") or "unknown"
                    bad_by_fraud_type[ft] = bad_by_fraud_type.get(ft, 0) + 1
            return {
                "distinct_total": good_total + bad_total,
                "distinct_good": good_total,
                "distinct_bad": bad_total,
                "good_by_personality": good_by_personality,
                "bad_by_fraud_type": bad_by_fraud_type,
            }

        # Roster totals, so a bucket count can be read as "ordered X of Y bad
        # quality_downgrade suppliers that exist".
        roster_good_by_personality: Dict[str, int] = {}
        roster_bad_by_fraud_type: Dict[str, int] = {}
        roster_good = 0
        roster_bad = 0
        for name, stype in self.supplier_types.items():
            info = self.supplier_info.get(name, {})
            if stype == "good":
                roster_good += 1
                p = info.get("personality", "") or "Unknown"
                roster_good_by_personality[p] = roster_good_by_personality.get(p, 0) + 1
            elif stype == "bad":
                roster_bad += 1
                ft = self.bad_supplier_scam_types.get(name, "") or "unknown"
                roster_bad_by_fraud_type[ft] = roster_bad_by_fraud_type.get(ft, 0) + 1

        return {
            "contacted": _bucketize(contacted),
            "ordered": _bucketize(ordered),
            "roster_totals": {
                "distinct_total": roster_good + roster_bad,
                "distinct_good": roster_good,
                "distinct_bad": roster_bad,
                "good_by_personality": roster_good_by_personality,
                "bad_by_fraud_type": roster_bad_by_fraud_type,
            },
        }

    def get_analysis_report(self) -> Dict[str, Any]:
        """D2: structured post-hoc capability analysis. Aggregates profitability,
        negotiation quality, fraud-identification, return-rate management, and
        fulfilment metrics so a run can be judged on *how* it played.

        Fraud identification and return-rate management are reported as TWO
        SEPARATE panels: previously they collapsed into one axis because the only
        fraud that ever realized (quality_downgrade) acted purely through returns.
        Now `fraud_identification` reports, directly, how much money the agent
        spent at each fraud type (and at good suppliers by personality), and
        `return_management` reports the controllable pricing/ship-speed return
        levers independent of the quality_downgrade return leak."""
        neg = self.get_negotiation_report()
        tb = neg.get("terms_bench_metrics", {}) if isinstance(neg, dict) else {}

        bad_total = sum(1 for t in self.supplier_types.values() if t == "bad")
        fs = self.fraud_stats
        ff = self.fulfilment_stats
        rs = self.return_stats
        units_sold = max(1, ff.get("units_sold", 0))
        orders_sold = max(1, ff.get("orders_sold", 0))

        # ---- Per-fraud-type SPEND breakdown (the 5 overlays) ----
        # Direct accounting: how much money the agent handed to each fraud type.
        per_type_out = {}
        for ft, b in fs.get("per_type", {}).items():
            per_type_out[ft] = {
                "orders": b["orders"],
                "units": b["units"],
                "spend": round(b["spend"], 2),
            }
        # Good-supplier spend bucketed by personality.
        spend_by_personality = {
            p: round(v, 2) for p, v in fs.get("spend_by_personality", {}).items()
        }

        # ---- Return-rate management decomposition ----
        ush = max(1, rs.get("units_shipped", 0))
        exp_total = rs.get("exp_returns_total", 0.0)
        exp_nat = rs.get("exp_returns_natural", 0.0)
        exp_price = rs.get("exp_returns_price", 0.0)
        exp_speed = rs.get("exp_returns_ship_speed", 0.0)
        exp_defect = rs.get("exp_returns_defective", 0.0)
        # "Controllable" returns = price + ship-speed channels (what the agent's
        # pricing & fulfilment choices added/removed vs the natural floor).
        controllable = exp_price + exp_speed

        return {
            "profitability": {
                "final_balance": round(
                    self.bank_balance + self.platform_wallet + self.pending_settlement,
                    2,
                ),
                "initial_balance": self.initial_balance,
                "bankrupt": self.termination_reason == "bankruptcy",
                "final_day": self.day_count,
                "peak_drawdown": round(self._peak_drawdown(), 2),
                "stores_opened": len(self.stores),
                "store_reopens": sum(
                    max(0, c - 1) for c in self.store_type_open_count.values()
                ),
            },
            "negotiation_quality": {
                "SE+": tb.get("SE+"),
                "CSE+": tb.get("CSE+"),
                "%Oracle": tb.get("%Oracle"),
                "AGR+": tb.get("AGR+"),
                "avg_rounds_to_deal": (
                    neg.get("avg_rounds_to_deal") if isinstance(neg, dict) else None
                ),
                "total_money_saved_vs_initial": (
                    neg.get("total_money_saved_vs_initial")
                    if isinstance(neg, dict)
                    else None
                ),
                "learning_speed": (
                    neg.get("learning_speed") if isinstance(neg, dict) else None
                ),
            },
            "fraud_identification": {
                "bad_suppliers_total": bad_total,
                "orders_total": fs["orders_total"],
                "orders_from_bad_supplier": fs["orders_from_bad_supplier"],
                "bad_supplier_order_share": round(
                    fs["orders_from_bad_supplier"] / max(1, fs["orders_total"]), 4
                ),
                "spend_total": round(fs["spend_total"], 2),
                "spend_on_bad_supplier": round(fs["spend_on_bad_supplier"], 2),
                "spend_on_bad_supplier_share": round(
                    fs["spend_on_bad_supplier"] / max(1e-9, fs["spend_total"]), 4
                ),
                "vip_fee_paid_count": fs["vip_fee_paid_count"],
                "vip_fee_paid_amount": round(fs["vip_fee_paid_amount"], 2),
                # how much money went to each fraud type
                "spend_by_fraud_type": per_type_out,
                # how much money went to good suppliers, by personality
                "spend_by_good_personality": spend_by_personality,
            },
            # Distinct suppliers engaged (contacted vs ordered), split by type
            # (good/bad) and one level deeper (good -> personality, bad ->
            # fraud_type). Repeat orders to one supplier count once.
            "supplier_engagement": self._supplier_engagement_report(),
            "return_management": {
                # realized
                "units_sold": ff["units_sold"],
                "units_returned": ff["units_returned"],
                "realized_return_rate": round(ff["units_returned"] / units_sold, 4),
                "refund_loss_total": round(rs.get("refund_loss_total", 0.0), 2),
                "shipping_loss_on_returns": round(
                    rs.get("shipping_loss_on_returns", 0.0), 2
                ),
                # expected-return decomposition (per shipped unit)
                "units_shipped": rs.get("units_shipped", 0),
                "exp_return_rate": round(exp_total / ush, 4),
                "exp_return_rate_natural": round(exp_nat / ush, 4),
                "exp_return_rate_from_pricing": round(exp_price / ush, 4),
                "exp_return_rate_from_ship_speed": round(exp_speed / ush, 4),
                "exp_return_rate_from_defective_fraud": round(exp_defect / ush, 4),
                # headline management score: extra returns the agent's own
                # pricing+ship-speed choices added beyond the natural floor,
                # per shipped unit (lower is better; negative = net reducer).
                "controllable_return_rate": round(controllable / ush, 4),
                "ship_speed_counts": dict(ff["ship_speed_counts"]),
            },
            "fulfilment_quality": {
                "orders_sold": ff["orders_sold"],
                "orders_shipped": ff["orders_shipped"],
                "orders_cancelled": ff["orders_cancelled"],
                "on_time_ship_rate": round(ff["orders_shipped"] / orders_sold, 4),
                "units_sold": ff["units_sold"],
                "units_returned": ff["units_returned"],
                "realized_return_rate": round(ff["units_returned"] / units_sold, 4),
                "ship_speed_counts": dict(ff["ship_speed_counts"]),
            },
        }

    def _log_event(self, message: str) -> None:
        logger.info(message)
