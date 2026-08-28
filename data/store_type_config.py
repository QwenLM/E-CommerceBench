"""
Store type configuration for E-Commerce Bench.
Single source of truth for store types and economic parameters.
"""

# ============================================================
# Store Type Definitions (12 types)
# ============================================================

STORE_TYPES = {
    # `tier` == DIFFICULTY class (1 = hard, 2 = medium, 3 = easy), set from an
    # empirical difficulty audit (loss% / good% / naive% under reference play).
    # Tier drives BOTH the profit ceiling (via MARKET_CAPACITY tuning)
    # and the daily ops cost (OPS_COST_PER_DAY). Hard = high risk / high cost /
    # high reward. RECALIBRATED 2026-06-24: single-store optimal profit bands are
    # now T1(hard)~¥4.1M, T2(medium)~¥3.1M, T3(easy)~¥2.0M (T1 ≈ 2× T3), with
    # difficulty (loss-rate) T1 > T2 > T3. Tiers were REASSIGNED so the hardest,
    # thin-margin grind stores are T1 (food_beverage, daily_office, home_living,
    # auto_hardware) and the tamed high-ticket stores moved to T2 (appliance_digital,
    # shoes_bags). A per-category sub-cap (CATEGORY_CAP_FRAC_DEFAULT) removes
    # single-category "jackpot" bets so score reflects operating skill, not a
    # category lottery. Ceilings are placed via MARKET_CAPACITY + PER_STORE_DEMAND_SCALE.
    # NOTE: the per-entry `daily_rent` field below is VESTIGIAL — actual ops
    # cost comes from OPS_COST_PER_DAY (by tier). It is kept only so older
    # readers of the dict don't KeyError; generate_store_types() writes the real
    # tier-based cost to store_types.csv, not this value.
    "appliance_digital": {
        "name": "Appliance & Digital",
        "tier": 2,  # MEDIUM (reassigned): high-ticket; jackpot tamed, ceiling in the medium band
        "setup_fee": 500.0,
        "daily_rent": 50.0,
        "sales_commission_rate": 0.02,
    },
    "fashion": {
        "name": "Fashion",
        "tier": 2,  # MEDIUM: high return-rate trap, but naive still ~ok
        "setup_fee": 500.0,
        "daily_rent": 50.0,
        "sales_commission_rate": 0.02,
    },
    "shoes_bags": {
        "name": "Shoes & Bags",
        "tier": 2,  # MEDIUM (reassigned): return-management store; jackpot fixed, medium band
        "setup_fee": 500.0,
        "daily_rent": 50.0,
        "sales_commission_rate": 0.02,
    },
    "food_beverage": {
        "name": "Food & Beverage",
        "tier": 1,  # HARD (reassigned): thin margin grind, pushed to top profit band (high risk/high reward)
        "setup_fee": 500.0,
        "daily_rent": 50.0,
        "sales_commission_rate": 0.02,
    },
    "beauty": {
        "name": "Beauty",
        "tier": 3,  # EASY: 0% loss, naive 66%, good% 75%
        "setup_fee": 500.0,
        "daily_rent": 50.0,
        "sales_commission_rate": 0.02,
    },
    "sports_outdoor": {
        "name": "Sports & Outdoor",
        "tier": 3,  # EASY: 0% loss, naive 83%, good% 75%
        "setup_fee": 500.0,
        "daily_rent": 50.0,
        "sales_commission_rate": 0.02,
    },
    "mother_baby": {
        "name": "Mother & Baby",
        "tier": 2,  # MEDIUM: naive 82% but 40% of policies lose money
        "setup_fee": 500.0,
        "daily_rent": 50.0,
        "sales_commission_rate": 0.02,
    },
    "home_living": {
        "name": "Home & Living",
        "tier": 1,  # HARD: naive 37%, only 15% of policies reach >=50% best
        "setup_fee": 500.0,
        "daily_rent": 50.0,
        "sales_commission_rate": 0.02,
    },
    "daily_office": {
        "name": "Daily & Office",
        "tier": 1,  # HARD (reassigned): highest loss-rate grind, pushed to top profit band
        "setup_fee": 500.0,
        "daily_rent": 50.0,
        "sales_commission_rate": 0.02,
    },
    "pet": {
        "name": "Pet Supplies",
        "tier": 3,  # EASY: 0% loss, naive 99%, good% 88% (easiest)
        "setup_fee": 500.0,
        "daily_rent": 50.0,
        "sales_commission_rate": 0.02,
    },
    "auto_hardware": {
        "name": "Auto & Hardware",
        "tier": 1,  # HARD: 62% of policies lose money (hardest)
        "setup_fee": 500.0,
        "daily_rent": 50.0,
        "sales_commission_rate": 0.02,
    },
    "toys_entertainment": {
        "name": "Toys & Entertainment",
        "tier": 3,  # EASY: 0% loss, naive 92%, good% 79%
        "setup_fee": 500.0,
        "daily_rent": 50.0,
        "sales_commission_rate": 0.02,
    },
}


# ============================================================
# Shipping & Storage Costs by Size
# ============================================================

SIZE_COSTS = {
    "Small": {"shipping": 0.5, "storage_per_day": 0.05},
    "Medium": {"shipping": 1.5, "storage_per_day": 0.15},
    "Large": {"shipping": 3.0, "storage_per_day": 0.50},
    "XLarge": {"shipping": 6.0, "storage_per_day": 1.50},
}

# ============================================================
# A1 — Human / Operations cost (replaces flat "rent")
# Each open store needs staff + ops. Charged daily per open store.
#
# REDEFINED 2026-06-19: `tier` now means DIFFICULTY (1 = hard, 2 = medium,
# 3 = easy), measured empirically by how forgiving each store is (fraction of
# policies that profit, naive-policy profit).
# Ops cost scales with difficulty: harder stores cost more to operate.
#   tier 1 (hard, demands skill):   ops ¥130/day
#   tier 2 (medium):                ops ¥100/day
#   tier 3 (easy):                  ops ¥60/day
# Capacities (MARKET_CAPACITY) are tuned per class to hit those ceilings.
# ============================================================

OPS_COST_PER_DAY = {
    # RECALIBRATED for the "smart wins in T1, fool wins in T3" property: T1 ops is
    # HIGH so a low-volume/mispriced (foolish) policy bleeds it dry (foolish<T3),
    # while a high-volume smart policy easily covers it (optimal highest). T3 ops
    # is LOW so even a careless policy stays profitable (foolish highest).
    1: 130.0,  # hard tier — high fixed burn punishes bad play
    2: 100.0,  # medium tier
    3: 60.0,  # easy tier — moderate burn
}
OPS_COST_DEFAULT = 80.0

# Per-store-type ops-cost overrides (take precedence over the tier table).
# None currently — ops cost is fully governed by the difficulty tier above.
OPS_COST_OVERRIDE = {}

# ============================================================
# E — Shipping speed tiers (manual ship_orders action)
# multiplier on base size shipping cost, and the return-rate multiplier
# applied to that order's units (faster = fewer returns; see S3).
# ============================================================

SHIP_SPEED = {
    "fast": {"cost_mult": 2.0, "return_mult": 0.75},
    "standard": {"cost_mult": 1.0, "return_mult": 1.00},
    "slow": {"cost_mult": 0.5, "return_mult": 1.30},
}
SHIP_SPEED_DEFAULT = "standard"

# E — settlement & fulfilment timing
SETTLEMENT_WINDOW_DAYS = 9  # >= max return lag (7) so refunds net in escrow
SHIP_DEADLINE_DAYS = 2  # ship within N days of sale or the order cancels

# ============================================================
# B2 — Market capacity (diminishing returns on breadth/depth)
# Per (store_type) soft cap on how much demand a single store can absorb.
# Realised demand for a store is scaled by a saturating factor of the
# store's total raw demand vs this capacity, so piling on SKUs/inventory
# yields sub-linear returns. Tuned per store-type "volume" character.
# ============================================================

MARKET_CAPACITY = {
    # daily realisable demand soft-cap per store (units/day, pre-saturation knee).
    # RECALIBRATED 2026-06-24: values are now AUTO-TUNED per store (with
    # PER_STORE_DEMAND_SCALE) to place each store's optimal in its tier band
    # (T1~¥4.1M / T2~¥3.1M / T3~¥2.0M) AFTER the per-category sub-cap. The live
    # difficulty tier is in STORE_TYPES above — the tier-group LABELS below are
    # pre-reassignment and kept only for history; do not trust them for tiering.
    # --- tier 1 (HARD) ---
    "appliance_digital": 13.959,  # was 23.5; x0.54 then x1.1
    "shoes_bags": 127.2,  # was 212.0 (x0.6)
    "home_living": 4218.18,  # was 7030.3 (x0.6)
    "auto_hardware": 19862.16,  # was 33103.6 (x0.6)
    # --- tier 2 (MEDIUM) ---
    "fashion": 224.37,  # was 415.5 (x0.54)
    "food_beverage": 658.56,  # was 1097.6 (x0.6)
    "mother_baby": 44.0154,  # was 74.1; x0.54 then x1.1
    "daily_office": 1345.8,  # was 2243.0 (x0.6)
    # --- tier 3 (EASY) ---
    "beauty": 21.78,  # was 36.3 (x0.6)
    "sports_outdoor": 41.16,  # was 68.6 (x0.6)
    "pet": 91.2,  # was 152.0 (x0.6)
    "toys_entertainment": 23.28,  # was 38.8 (x0.6)
}
MARKET_CAPACITY_DEFAULT = 50.0

# ============================================================
# B2b — Per-store-type DEMAND scale (live calibration knob)
# Multiplies each SKU's pre-saturation base daily demand for that store type
# (on top of the global PER_STORE_SCALE). This is the lever for stores whose
# market_capacity does NOT bind at the optimum (demand-/margin-limited stores
# such as auto_hardware / food_beverage / daily_office): raising it lifts the
# profit CEILING without touching margins or return rates, so a store's
# DIFFICULTY (loss-rate, driven by margin/returns/ops) is preserved while its
# reachable profit is rescaled. 1.0 = unchanged. Read live by EcommerceEnv
# (no CSV regeneration needed). Used together with MARKET_CAPACITY (the lever
# for cap-bound stores) to place each tier's profit ceiling in its target band.
# ============================================================
PER_STORE_DEMAND_SCALE = {
    "appliance_digital": 0.6,  # manual override (was 0.75)
    "shoes_bags": 0.2,  # T2 (jackpot fixed; demand-bound)
    "home_living": 6.0,  # T1 -> ~¥4M
    "auto_hardware": 4.0,  # T1 -> ~¥4M (thin margin keeps it hardest)
    "fashion": 4.0,  # manual override (was 6.0)
    "food_beverage": 9.0,  # T1 -> ~¥4M (thin-margin grind stays hard)
    "mother_baby": 0.8,  # manual override (was 1.0)
    "daily_office": 4.0,  # T1 -> ~¥4M (highest loss-rate grind)
    "beauty": 1.5,  # T3 -> ~¥2M
    "sports_outdoor": 1.5,  # T3 -> ~¥2M
    "pet": 0.75,  # T3 -> ~¥2M
    "toys_entertainment": 1.5,  # T3 -> ~¥2M
}
PER_STORE_DEMAND_SCALE_DEFAULT = 1.0

# ============================================================
# B2c — Per-category sub-cap fraction (anti-"jackpot" guard)
# A single sub-category may realise at most this fraction of its store's
# market capacity. This stops a concentrated single-category bet from beating
# a diversified portfolio (which keeps the benchmark about operating SKILL, not
# a category lottery). Applied as a per-category saturation on top of the
# store-level one, in EcommerceEnv._process_sales_for_date. The env AUTO-RELAXES
# it for stores with few allowed categories (effective frac = max(this,
# 1.2/num_allowed_categories), capped at 1.0) so they can still fill their cap;
# single-category stores (e.g. pet) get frac>=1.0 and are unaffected.
# Read live (no CSV regeneration needed).
# ============================================================
CATEGORY_CAP_FRAC_DEFAULT = 0.35

# ============================================================
# B3 — Two-way reputation penalty weights
# reputation = base_volume_rep - penalty(recent returns, cancellations)
# ============================================================

REPUTATION_PENALTY = {
    "return_weight": 0.6,  # weight of recent return-rate on rep penalty
    "cancel_weight": 1.0,  # weight of recent stockout cancellations
    "decay": 0.85,  # daily decay of rolling counters
    "max_penalty": 0.5,  # cap on how far penalties can pull rep down
}

# ============================================================
# B4 — Warehouse aging: storage cost multiplier by stock age (days)
# Older stock costs more to hold (capital + space pressure).
# ============================================================

STORAGE_AGE_MULT = [
    (0, 1.0),
    (21, 1.4),
    (45, 2.2),
    (90, 4.0),
    (135, 6.0),
    (180, 9.0),
]

# ============================================================
# B5 — Liquidation salvage rate
# Fraction of purchase cost recovered when closing a store liquidates its
# remaining inventory. Lower = harsher penalty for over-stocking then bailing.
# ============================================================

LIQUIDATION_SALVAGE_RATE = 0.1

# ============================================================
# S2 — Re-open penalty (discourage churn, reward long-term portfolios)
# Re-opening a store TYPE that was closed: pays setup fee again AND the new
# store rebuilds reputation slowly (starts at a discount, recovers over time).
# ============================================================

REOPEN_REPUTATION = 0.5  # reopened stores start at the same initial reputation

# ============================================================
# S3 — Return rate vs pricing (price/reference ratio -> return multiplier)
# Pricing above reference -> expectation gap -> more returns.
# Pricing at/below reference -> baseline or slightly fewer.
# Piecewise-linear in r = retail/reference.
# ============================================================

RETURN_PRICE_KNEES = [
    (0.8, 0.85),
    (1.0, 1.00),
    (1.3, 1.50),
    (1.8, 2.20),
]

# ============================================================
# Per-Store Scale Factor
# Raw data monthly_sales_range is platform-wide per-SKU volume.
# PER_STORE_SCALE converts to single-store daily demand.
# ============================================================

PER_STORE_SCALE = 0.1

# ============================================================
# Wholesale Ratios (calibrated for tier profitability)
# Each entry is (historical_wholesale_ratio, cost_floor_ratio).
#   cost_floor_ratio = lowest negotiable price / reference_price (the floor).
#   The NEGOTIATION OPENING is no longer this first value — generate_category_params
#   DERIVES it as wholesale_ratio = 0.5 + 0.5*cost_floor_ratio so a naive
#   "accept the opening" agent scores a uniform SE+ = 0.5 in every category,
#   widening the room to demonstrate negotiation skill. The floor is unchanged.
#   The first tuple value (the historical wholesale ratio) is preserved and
#   emitted to the CSV as `scam_cap_ratio`: it ONLY caps the pre-emptive-scammer
#   reservation price, so raising the honest opening does not inflate scam
#   overpay. The derived values ship in data/category_params.csv.
# These ratios are NOT visible to agents — agents discover
# procurement costs only through supplier negotiation.
# ============================================================

WHOLESALE_RATIOS = {
    # --- Tier 1: Good stores (low wholesale = high margin) ---
    "Major Appliances": (0.50, 0.72),  # thinned so it stops dominating appliance
    "Small Appliances": (0.70, 0.55),
    "Audio & Video Electronics": (
        0.72,
        0.65,
    ),  # thinned high-ASP digital so no single category dominates appliance
    "Smartphones": (
        0.75,
        0.68,
    ),  # thinned high-ASP digital so no single category dominates appliance
    "Laptops": (
        0.78,
        0.70,
    ),  # thinned high-ASP digital so no single category dominates appliance
    "Computer Hardware & Peripherals": (0.60, 0.45),
    "3C Digital Accessories": (0.55, 0.40),
    "Storage Devices": (0.70, 0.55),
    "Networking Equipment": (0.65, 0.50),
    "Skincare & Beauty": (0.45, 0.30),
    "Beauty Devices": (0.55, 0.40),
    "Personal Care & Massage": (0.60, 0.45),
    "Children's Clothing": (0.55, 0.40),
    "Children's Shoes": (0.60, 0.45),
    "Baby Products": (0.50, 0.35),
    "Maternity Products": (0.65, 0.50),
    "Toys & Educational": (0.45, 0.30),
    "Sports & Fitness": (0.27, 0.17),
    "Sports Bags & Accessories": (0.31, 0.19),
    "Outdoor & Camping": (0.27, 0.17),
    # --- Tier 2: Neutral stores (medium wholesale) ---
    "Furniture": (0.88, 0.73),
    "Bedding": (0.85, 0.70),
    "Household Essentials": (0.89, 0.74),
    "Storage & Organization": (0.84, 0.69),
    "Lighting": (0.88, 0.73),
    "Home Building Materials": (0.89, 0.74),
    "Basic Construction Materials": (0.87, 0.72),
    "Cleaning Tools": (0.89, 0.74),
    "Kitchenware": (0.82, 0.67),
    "Collectibles & Figures": (0.40, 0.26),
    "Gaming & Accessories": (0.45, 0.31),
    # --- Tier 3: Trap stores (high wholesale = thin margin after costs) ---
    # REBALANCED 2026-06-19: fashion, food_beverage, daily_office, pet had margins so
    # thin that no capacity could lift their optimal profit to the ~¥800k target, so
    # their wholesale_ratio + cost_floor_ratio were scaled down (fatter margin) by
    # fashion x0.5, food_beverage/daily_office x0.65, pet x0.4. The negotiation spread
    # (wholesale vs floor) is preserved proportionally. Return rates are unchanged, so
    # fashion/shoes_bags remain return-management challenges — just no longer unwinnable.
    # Return trap (fashion, shoes): decent raw margin but 40%+ returns + shipping eat profit
    "Women's Fashion": (0.44, 0.39),
    "Men's Clothing": (0.46, 0.42),
    "Underwear & Loungewear": (0.425, 0.375),
    "Fashion Accessories": (0.435, 0.385),
    "Sportswear & Casual": (0.435, 0.385),
    "Women's Shoes": (
        0.88,
        0.45,
    ),  # fatter margin so 48% returns still profit — raises shoes diversified baseline
    "Men's Shoes": (
        0.88,
        0.50,
    ),  # fatter margin (paired with volume raise) so it's a viable shoes category
    "Sneakers": (0.93, 0.84),
    "Bags & Luggage": (
        0.90,
        0.60,
    ),  # fatter margin (paired with volume raise) so it's a viable shoes category
    # Thin margin trap (food, daily): wholesale too close to retail
    "Snacks & Nuts": (0.611, 0.572),
    "Groceries & Staples": (0.605, 0.566),
    "Coffee & Beverages": (0.605, 0.566),
    "Fresh Food & Meat": (0.598, 0.559),
    "Alcoholic Beverages": (0.585, 0.546),
    "Health Supplements": (0.552, 0.507),
    "Cleaning & Hygiene": (0.605, 0.566),
    "Gifts & Party Supplies": (0.585, 0.533),
    "Lifestyle Accessories": (0.585, 0.533),
    "Stationery & Office Supplies": (0.572, 0.52),
    "Office Equipment": (0.598, 0.552),
    "Medical Devices": (0.585, 0.533),
    # Low turnover / structural trap (auto, pet)
    "Auto Accessories": (0.92, 0.84),
    "Hardware & Tools": (0.93, 0.86),
    "Electrical Supplies": (0.94, 0.88),
    "Electronic Components": (0.95, 0.90),
    "E-Vehicles & Parts": (0.90, 0.83),
    "Agricultural Supplies": (0.92, 0.85),
    "Industrial & Lab Supplies": (0.92, 0.85),
    "Pet Supplies": (0.28, 0.22),
}


# ============================================================
# Seasonality Multipliers (monthly, per store type)
# ============================================================

SEASONALITY = {
    #                    Jan  Feb  Mar  Apr  May  Jun  Jul  Aug  Sep  Oct  Nov  Dec
    "appliance_digital": [1.0, 0.9, 1.0, 1.0, 1.0, 1.3, 0.9, 1.0, 1.0, 1.0, 1.4, 1.1],
    "fashion": [0.75, 0.7, 1.0, 0.95, 0.85, 1.0, 0.8, 0.8, 0.95, 0.85, 1.55, 0.85],
    "shoes_bags": [0.9, 0.8, 1.1, 1.1, 1.0, 1.2, 0.9, 0.9, 1.0, 1.0, 1.4, 1.0],
    "food_beverage": [1.3, 1.6, 1.0, 0.9, 0.9, 0.8, 1.1, 1.1, 1.3, 0.9, 0.6, 1.5],
    "beauty": [1.0, 1.0, 1.3, 1.0, 1.0, 1.2, 1.0, 1.0, 1.0, 1.0, 1.3, 1.0],
    "sports_outdoor": [0.8, 0.8, 1.0, 1.2, 1.3, 1.2, 1.3, 1.2, 1.0, 1.0, 1.0, 0.8],
    "mother_baby": [1.0, 1.0, 1.0, 1.0, 1.1, 1.2, 1.0, 1.0, 1.1, 1.0, 1.2, 1.0],
    "home_living": [1.0, 0.9, 1.1, 1.1, 1.0, 1.2, 1.0, 1.0, 1.0, 1.0, 1.3, 1.0],
    "daily_office": [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.1, 1.2, 1.0, 1.0, 1.0],
    "pet": [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.1, 1.0],
    "auto_hardware": [1.0, 0.9, 1.1, 1.1, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.1, 1.0],
    "toys_entertainment": [1.2, 1.0, 1.0, 1.0, 1.1, 1.2, 1.1, 1.0, 1.0, 1.0, 1.7, 1.3],
}

# ============================================================
# Supplier Configuration
# ============================================================

SUPPLIER_PERSONALITIES = [
    "Friendly",
    "Professional",
    "Enthusiastic",
    "Strategic",
    "Unpredictable",
    "Tough",
]
# Enthusiastic + Friendly ≈ 50 % of good suppliers (25 % each);
# remaining four share the other 50 % equally (12.5 % each).
SUPPLIER_PERSONALITY_WEIGHTS = [0.25, 0.125, 0.25, 0.125, 0.125, 0.125]
FRAUD_TYPES = [
    "vip_fee",
    "future_discount",
    "qty_bait",
    "quality_downgrade",
    "fake_urgency",
]

SUPPLIER_CONFIG = {
    # Each supplier serves EXACTLY ONE category (no cross-category reuse). Totals
    # are a 4x scale-up of the historical 144-supplier roster (106 good / 38 bad),
    # preserving the good:bad ratio: 424 good + 152 bad = 576 total. Suppliers are
    # distributed as evenly as possible across the 60 categories, so every
    # category gets >=7 good and >=2 bad — fraud avoidance is testable everywhere.
    "total_good": 424,
    "total_bad": 152,
    "good_urgency_range": (0.1, 0.9),
    "bad_urgency_range": (0.05, 0.2),
    "bankruptcy_threshold_range": (10, 20),
}

# ============================================================
# Reputation Parameters
# ============================================================

REPUTATION_CONFIG = {
    "initial": 0.3,
    "max": 1.0,
    "sigmoid_threshold": 500,  # cumulative sales where reputation reaches ~0.6
    "sigmoid_scale": 200,
}

# ============================================================
# Store playbook (qualitative, NON-numeric) for market_search.
# Agent-facing strengths / challenges / operating tips per store type.
# Auto-generated from the simulation profile; NO numbers (hint layer,
# not the answer key). Surfaced by market_search levels 1 (overview) & 2.
# ============================================================
STORE_PLAYBOOK = {
    "food_beverage": {
        "strengths": [
            "High reward ceiling: this is a high-risk, high-reward line where skill is richly rewarded. Disciplined pricing and tight cost control can push your earnings to the very top of any store type.",
            "Brisk, steady demand and a small ticket size mean little capital is locked up per unit, so cash turns over quickly and settled revenue can be recycled into restocks fast.",
            "Fast turnover lets you read the market and adjust prices quickly, rewarding an operator who pays attention and iterates.",
        ],
        "challenges": [
            "Margins are thin, leaving almost no cushion. One careless price or a slow shipment can erase the entire profit on a sale.",
            "Return rates run high here, and every return costs you a refund plus the shipping you already spent. On thin margins those losses compound fast and quietly.",
            "A heavy fixed daily operating cost grinds against you every day no matter how you sell. Idle or slow days bleed money, so carelessness is punished hard.",
        ],
        "tips": [
            "Price accurately and ship fast. These are your two strongest levers against this line's high return rate and thin margins, keeping refunds and lost shipping from eating your profit.",
            "Lean into lower-return, fatter-margin lines and price them with confidence, but don't over-price elastic goods or demand will collapse.",
            "Keep inventory lean and let settled cash fund your next restock, and keep volume high enough to carry the fixed daily cost rather than letting slow days bleed you out.",
        ],
    },
    "daily_office": {
        "strengths": [
            "Demand is broad and dependable, with a large, steady stream of buyers — the earning ceiling here is genuinely high, and a tightly run operation can scale fast.",
            "Demand stays fairly even across the calendar, so you face little seasonal whiplash and can plan stock and cash flow with confidence.",
        ],
        "challenges": [
            "Margins are thin: there's almost no cushion, so even small pricing mistakes wipe out profit even when sales look strong.",
            "Returns run high, and each one refunds the sale and burns the shipping you already paid — raw volume will not save a sloppy operation.",
            "A heavy daily operating cost bleeds whether or not you sell, so idle stock and slow days hurt badly; this is a high-reward floor only for the disciplined, and carelessness is punished hard.",
        ],
        "tips": [
            "Price with discipline and accuracy — these goods are margin-sensitive, so don't over-price demand-elastic lines or you'll kill the volume that pays the bills.",
            "Treat returns as the main enemy: ship fast and price honestly so refunds and lost shipping don't swallow your margin, and lean toward lower-return, fatter-margin lines where you can.",
            "Keep inventory lean and let settled cash fund your restocks so the high daily operating cost can't drain you, and spread your bets across lines rather than over-committing to one.",
        ],
    },
    "home_living": {
        "strengths": [
            "Big ceiling. Demand here runs deep and steady, so a careful operator who sources well and prices sharply can build real volume and serious profit.",
            "Skill compounds. Tight pricing, lean stock, and disciplined sourcing pay off more here than in gentler markets - mastery is genuinely rewarded.",
            "Hidden bright spots. Within the noise there are lower-return, fatter-margin lines; finding and leaning into them is where the outsized money is made.",
        ],
        "challenges": [
            "No cushion. Margins are thin, so a mispriced or slow-moving line gives back its profit fast - there's little room for error.",
            "Returns bite hard. Return rates run high, and every return is a compound loss: the refund plus the shipping you already sank to send and receive the goods.",
            "Heavy to carry. Bulkier goods tie up more cash and cost more to store and ship, while a high daily operating cost punishes you whenever volume runs thin.",
        ],
        "tips": [
            "Hunt for the low-return, fat-margin lines and price them up where demand holds firm; let those carry the store while you trim or drop the chronic refund magnets.",
            "On anything with a high return rate, accurate pricing and faster shipping are your defense - they keep refunds and sunk shipping from quietly eating your margin.",
            "Keep inventory lean and fund restocks from settled cash rather than parking capital in bulky slow movers; spread across several lines so one bad bet can't sink you.",
        ],
    },
    "auto_hardware": {
        "strengths": [
            "Top-tier profit ceiling for the disciplined: margins are the thinnest of all, so relentless cost control is the whole game — master it and the slim per-sale profit compounds into some of the highest earnings available; let it slip and you lose money.",
            "The ceiling is high for the disciplined: with relentless cost control, even thin per-sale profit compounds into strong results.",
        ],
        "challenges": [
            "Margins are the thinnest on the board, leaving no cushion — overpaying a supplier, shipping too fast, or letting stock sit idle can flip a profitable line straight into a loss.",
            "Bulkier, heavier goods can carry real storage and shipping weight that quietly eats your already-slim margin.",
        ],
        "tips": [
            "Negotiate procurement hard and trim every recurring cost — here, cost discipline is the entire game.",
            "Keep inventory lean and let settled cash fund your next restock, so capital and storage never sit idle.",
            "Price with discipline: there's no markup to give away, so don't undercut yourself or chase volume at a loss.",
        ],
    },
    "appliance_digital": {
        "strengths": [
            "A solid, balanced mid-tier earner: high-ticket goods bring real revenue per sale, so steady, well-managed play turns a dependable profit without chasing volume.",
            "Healthy margins are on offer for the right lines, and a well-run catalogue is a dependable earner.",
            "A balanced category overall: skill in pricing and cash handling is rewarded without punishing every mistake.",
        ],
        "challenges": [
            "Big-ticket goods tie up a lot of capital per unit, so a few unsold items can drain your cash fast.",
            "Bulkier stock costs more to store and ship, and those costs stack up against your daily operating overhead.",
            "Return rates are mixed and uneven across lines; each return refunds the sale and burns the shipping you already paid, erasing the profit.",
        ],
        "tips": [
            "Lean into low-return, fat-margin lines and price them up; treat high-return goods cautiously and price them with precision.",
            "Ship fast and keep pricing accurate so refunds and lost shipping don't quietly eat your margin.",
            "Keep inventory lean and let settled cash fund your restocks; don't sink all your capital into a single high-ticket bet — spread it and watch your balance.",
        ],
    },
    "fashion": {
        "strengths": [
            "Margins are decent — priced well, your good lines leave enough room to take the occasional refund on the chin and still come out ahead.",
            "Demand is broad and responsive, so there's steady volume to work with when you stock and price sensibly.",
            "Ticket sizes are moderate, so capital isn't locked up too heavily per order — you can keep stock turning.",
        ],
        "challenges": [
            "Returns run high here, and this is what makes or breaks you: every refund hands back the revenue and the shipping you already paid, so sloppy pricing quietly bleeds the margin dry.",
            "Shipping on returned goods is a sunk loss you never get back — slow fulfilment and mispriced lines only widen that leak.",
            "It's balanced, not forgiving: the margin is there, but careless return management will erase it just as fast as discipline earns it.",
        ],
        "tips": [
            "Treat return management as the core skill — price accurately and ship fast so fewer orders bounce back and refunds stop eating into your margin.",
            "Lean into your lowest-return, fattest-margin lines and price them up; go lighter and more cautious on volatile, high-return stock, and don't over-price the elastic items.",
            "Keep inventory lean and let settled cash fund your restocks, so a wave of refunds never catches you short on capital.",
        ],
    },
    "shoes_bags": {
        "strengths": [
            "The range is broad and mixed: some lines carry fat margins and low returns, others are thinner and more return-prone, so a diversified basket smooths the bumps and earns steadily.",
            "Demand here is fairly dependable when you spread your bets, rewarding patient, balanced operating rather than one big swing.",
            "The better lines tolerate being priced up, leaving room to lift margin where buyers aren't price-sensitive.",
        ],
        "challenges": [
            "A meaningful slice of the range returns at a high rate, and every return is a compound hit: you refund the sale and eat the shipping you already paid, so careless picks quietly bleed profit.",
            "Some goods are bulkier, pushing up storage and shipping and tying up cash in stock that sits.",
            "Elastic lines punish over-pricing, and seasonal swings can leave you over- or under-stocked if you don't read the calendar.",
        ],
        "tips": [
            "Lean into the low-return, fat-margin lines and price them up; let them carry the basket while the riskier lines stay a minority.",
            "On goods with high return rates, ship fast and price accurately so refunds and lost shipping don't swallow the margin.",
            "Keep inventory lean and let settled cash fund your restocks; diversify across lines instead of betting the warehouse on one.",
        ],
    },
    "mother_baby": {
        "strengths": [
            "Most lines carry healthy, workable margins, so disciplined pricing turns a steady profit without needing heroics.",
            "Returns are mostly manageable across the mix, so refunds and lost shipping rarely blow a hole in your month.",
            "This is a diversified store: spreading across many kinds of goods smooths demand and keeps any single weak line from sinking you.",
        ],
        "challenges": [
            "The return profile is mixed — some goods come back more often, and each return costs the refund plus the shipping you already sunk, quietly eroding margin if ignored.",
            "Running a broad assortment ties up cash and shelf space at once; without inventory and cash discipline you can be profitable yet short on liquidity.",
            "Demand shifts with the season and some goods are price-sensitive, so over-pricing elastic lines or mis-timing stock leaves you with slow inventory while the daily operating cost keeps ticking.",
        ],
        "tips": [
            "Lean into your low-return, fat-margin lines and price them up — that is where this store quietly makes its money.",
            "On higher-return goods, ship fast and price accurately so refunds and return shipping don't eat your margin; don't over-price the elastic ones.",
            "Keep inventory lean, let settled cash fund restocks, and rebalance the assortment toward whatever is selling clean and returning little.",
        ],
    },
    "beauty": {
        "strengths": [
            "Fat margins meet low return rates, so refunds claw back a smaller share of revenue than in most store types.",
            "Light overhead and easy-to-store goods mean daily operating cost is low and little cash sits trapped in inventory.",
            "Low returns and fat margins keep per-sale costs down relative to other store types.",
        ],
        "challenges": [
            "The ceiling is modest — expect steady gains, not outsized wins from any single line.",
            "Low barriers invite crowding and demand per line is finite, so simply piling on stock won't make it all clear; some demand also swings with the calendar.",
        ],
        "tips": [
            "Lean into the low-return, fat-margin nature and price up with confidence — you have room to mark up without scaring buyers off.",
            "Keep inventory lean and let settled cash fund your restocks; spread across several lines to push past the modest per-line ceiling.",
            "Even with low returns, keep shipping reasonably quick and pricing accurate so the occasional refund never swallows a whole margin.",
        ],
    },
    "sports_outdoor": {
        "strengths": [
            "Margins are fat and returns are low, so the refunds-plus-lost-shipping drain that hits other store types takes a smaller share here.",
            "Daily operating cost is light, so the break-even floor is low.",
            "Demand is steady, so a single bad pricing or stocking call has a limited effect.",
        ],
        "challenges": [
            "The ceiling is modest, so even strong play won't compound into outsized returns — expect steady profit, not a runaway.",
            "Some goods are bulkier, so storage and shipping costs scale with how much you hold and how fast you ship. Overstocking or always paying for the fastest shipping quietly trims an otherwise healthy edge.",
            "Demand shifts with the seasons. Ignoring those swings leaves capital tied up in inventory through the slow stretches and risks stockouts in the busy ones.",
        ],
        "tips": [
            "Lean into the low-return, fat-margin lines and price them up. The low return rate lets you hold firmer prices without refunds clawing the gains back.",
            "Keep inventory lean and let settled wallet cash fund your restocks. Don't tie up capital chasing volume the modest ceiling won't reward.",
            "Mind seasonality and the bulkier items: time your stocking to demand and keep shipping speed sensible so storage and freight don't nibble away the margin.",
        ],
    },
    "pet": {
        "strengths": [
            "Fat margins paired with the lowest return rates of any store type, so refunds claw back a smaller share of revenue than elsewhere.",
            "Low daily operating cost, fat margins, and low returns keep both fixed and variable costs modest.",
            "Steady, predictable demand without violent swings: stock and cash are easy to plan around.",
        ],
        "challenges": [
            "Modest ceiling: limited headroom. You will not get rich quickly here, and upside flattens as the market saturates.",
            "Low costs are not a free pass. Complacency still bleeds money through daily operating cost, sluggish shipping, and capital parked in slow-moving stock.",
            "Underpricing quietly wastes the best feature you have. With margins this fat and returns this low, timid prices leave the easiest money on the table.",
        ],
        "tips": [
            "Lean into the low-return, fat-margin lines and price them up with confidence. The low return rate means firm pricing is less likely to trigger refunds, so capture the margin on offer.",
            "Keep inventory lean and let settled cash fund your restocks, so capital is not tied up; ship promptly to keep your already-low returns from creeping up.",
            "Treat this as a stable cash engine, not a moonshot. Once demand flattens, put surplus to work by diversifying into other store types rather than over-investing past the point of diminishing returns.",
        ],
    },
    "toys_entertainment": {
        "strengths": [
            "Fat margins leave some room on pricing and sourcing decisions.",
            "Returns are low, so refunds and lost shipping take a smaller share of revenue.",
            "Low daily operating cost and modest ticket size keep little capital tied up; cash stays available.",
        ],
        "challenges": [
            "The ceiling is modest — even played well, the upside is steady rather than spectacular.",
            "Demand leans hard on a year-end seasonal peak while the rest of the calendar runs quieter, so timing stock to that swing is what separates a strong run from an average one.",
            "Low-pressure conditions can breed carelessness — over-buying ahead of the peak can leave you sitting on inventory once demand fades.",
        ],
        "tips": [
            "Lean into the fat margins and price up, especially heading into the seasonal peak — these goods tolerate firm pricing.",
            "Build inventory ahead of the year-end surge but keep it lean enough that leftovers don't pile up afterward; let settled cash fund your restocks.",
            "Keep shipping reasonably quick and pricing accurate so the few returns you do see stay cheap and protect the margin.",
        ],
    },
}
