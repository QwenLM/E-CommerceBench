from datetime import timedelta
from typing import Any, Dict, List

from .scam_handler import _is_membership_fee_item
from .supplier_config import SUPPLIER_CONFIG, compute_effective_floor

VIP_FEE_AMOUNT = SUPPLIER_CONFIG["vip_fee_amount"]


def _enforce_price_floor(env, supplier_name: str, items: List[Dict]) -> List[Dict]:
    corrected = []
    for item in items:
        if _is_membership_fee_item(item):
            raw_fee = item.get("unit_price")
            try:
                vip_fee = float(raw_fee)
            except (TypeError, ValueError):
                vip_fee = getattr(env, "_current_vip_fee", {}).get(
                    supplier_name, VIP_FEE_AMOUNT
                )
            vip_fee = round(max(vip_fee, 0.0), 2)
            corrected.append(
                {
                    **item,
                    "sku_id": "MEMBERSHIP_FEE",
                    "unit_price": vip_fee,
                    "quantity": 1,
                    "_is_fee": True,
                }
            )
            continue

        sku_id = item.get("sku_id", "")
        product = env.products.get(sku_id)
        if not product:
            env._log_event(f"Order item skipped: SKU {sku_id} not found")
            continue
        # Effective price floor — single source of truth shared with
        # KernelManager._get_kernel_params (supplier_config.compute_effective_floor).
        effective_floor = compute_effective_floor(env, supplier_name, sku_id)
        supplier_type = env.supplier_types.get(supplier_name, "unknown")
        unit_price = float(item.get("unit_price", 0))
        if supplier_type == "bad" and unit_price < effective_floor:
            unit_price = effective_floor
        unit_price = max(unit_price, 0.0)
        corrected.append({**item, "sku_id": sku_id, "unit_price": round(unit_price, 2)})
    return corrected


def _process_order(
    env,
    supplier_name: str,
    items: List[Dict],
    shipping_address: str,
) -> Dict[str, Any]:
    corrected_items = _enforce_price_floor(env, supplier_name, items)
    if not corrected_items:
        return {"success": False, "error": "No valid items from this supplier."}

    # VIP pay-first gate: a vip_fee scammer will not accept any PRODUCT order
    # until the agent has paid its membership fee. The fee line itself (_is_fee)
    # is always allowed through, so paying the fee can still register the
    # supplier in env.vip_fee_paid_suppliers below.
    supplier_type = env.supplier_types.get(supplier_name, "unknown")
    fraud_type = getattr(env, "bad_supplier_scam_types", {}).get(supplier_name, "")
    has_product_line = any(not item.get("_is_fee") for item in corrected_items)
    if (
        supplier_type == "bad"
        and fraud_type == "vip_fee"
        and has_product_line
        and supplier_name not in getattr(env, "vip_fee_paid_suppliers", set())
    ):
        fee = SUPPLIER_CONFIG.get("vip_fee_amount", 0.0)
        return {
            "success": False,
            "error": (
                f"This supplier only sells to VIP members. You must enroll and "
                f"pay the one-time VIP membership fee of ¥{fee:.2f} before placing "
                f"any product order."
            ),
        }

    total_cost = sum(item["unit_price"] * item["quantity"] for item in corrected_items)
    total_cost = round(total_cost, 2)

    if env.bank_balance < total_cost:
        return {
            "success": False,
            "error": (
                f"Insufficient funds. Required: ¥{total_cost:.2f}, "
                f"Available: ¥{env.bank_balance:.2f}."
            ),
        }

    env.bank_balance -= total_cost
    # D2 ledger + spend analysis: split spend into procurement vs VIP fee, and
    # track whether this order went to a bad (fraudulent) supplier.
    fee_cost = sum(
        i["unit_price"] * i["quantity"] for i in corrected_items if i.get("_is_fee")
    )
    proc_cost = total_cost - fee_cost
    if hasattr(env, "_ledger"):
        env._ledger["procurement"] += proc_cost
        env._ledger["vip_fees"] += fee_cost
    if hasattr(env, "fraud_stats"):
        fstats = env.fraud_stats
        fstats["orders_total"] += 1
        fstats["spend_total"] += total_cost
        units_ordered = sum(
            i["quantity"] for i in corrected_items if not i.get("_is_fee")
        )
        if supplier_type == "bad":
            fstats["orders_from_bad_supplier"] += 1
            fstats["spend_on_bad_supplier"] += total_cost
            # Per-fraud-type spend breakdown: how much money the agent handed to
            # each fraud overlay (the whole charge, including any VIP fee).
            bucket = fstats.get("per_type", {}).get(fraud_type)
            if bucket is not None:
                bucket["orders"] += 1
                bucket["units"] += units_ordered
                bucket["spend"] += total_cost
            if fee_cost > 0:
                fstats["vip_fee_paid_count"] += 1
                fstats["vip_fee_paid_amount"] += fee_cost
        else:
            # Good-supplier spend, broken down by the supplier's personality, so
            # we can see where honest money went.
            personality = env.supplier_info.get(supplier_name, {}).get(
                "personality", ""
            )
            sbp = fstats.get("spend_by_personality")
            if sbp is not None and personality:
                sbp[personality] = sbp.get(personality, 0.0) + total_cost
    order_details = []
    for item in corrected_items:
        sku_id = item["sku_id"]

        if item.get("_is_fee"):
            env._log_event(
                f"Membership/VIP fee charged: ¥{item['unit_price']:.2f} "
                f"x{item['quantity']} from {supplier_name}"
            )
            order_details.append(
                {
                    "sku_id": sku_id,
                    "product_name": item.get("product_name", "Membership Fee"),
                    "quantity": item["quantity"],
                    "unit_price": item["unit_price"],
                    "order_id": env.next_order_id,
                    "eta_days": "N/A",
                }
            )
            env.next_order_id += 1
            continue

        product = env.products.get(sku_id, {})
        delivery_days = env.rng.randint(3, 7)
        # Active supply-disruption events (e.g. Winter Storm) lengthen delivery
        # for affected store types via delivery_delay_multiplier. events.csv
        # keys affected_categories by store_type, so we must pass store_type
        # (not the product's category) or targeted events never match.
        store_type = product.get("store_type", "")
        delay_mult = 1.0
        if hasattr(env, "_get_event_delivery_delay"):
            try:
                delay_mult = env._get_event_delivery_delay(store_type)
            except Exception:
                delay_mult = 1.0
        if delay_mult and delay_mult != 1.0:
            delivery_days = max(1, int(round(delivery_days * delay_mult)))
        actual_delivery_time = env.current_time + timedelta(
            days=delivery_days, minutes=env.rng.randint(0, 1439)
        )
        eta_start = delivery_days
        eta_end = delivery_days + env.rng.randint(1, 2)

        # Resolve this supplier's fraud overlay (empty for good suppliers).
        # Pre-emptive scams (vip_fee / fake_urgency / future_discount) already
        # did their damage via the elevated cost_floor at order time, so the
        # extra money is simply part of total_cost / spend — no loss accounting
        # needed here. Only the two post-hoc scams change what is delivered.
        fraud_type = getattr(env, "bad_supplier_scam_types", {}).get(supplier_name, "")

        # qty_bait fraud: deliver only 60-70% of ordered quantity (the agent
        # paid for the full quantity; the shortfall is the harm).
        actual_qty = item["quantity"]
        if fraud_type == "qty_bait":
            deliver_ratio = 0.6 + env.rng.random() * 0.1
            actual_qty = max(1, int(item["quantity"] * deliver_ratio))
            env._log_event(
                f"[QTY_BAIT] Order for {sku_id}: ordered {item['quantity']}, will deliver {actual_qty}"
            )

        # quality_downgrade fraud: delivered goods are defective and will be
        # returned by customers at a much higher rate once sold (see
        # env._effective_return_rate). The refund/shipping harm happens at
        # return time, driven by the defective flag set here.
        is_defective = fraud_type == "quality_downgrade"
        if is_defective:
            env._log_event(
                f"[QUALITY_DOWNGRADE] Order for {sku_id} from {supplier_name}: delivering defective units"
            )

        env.pending_deliveries.append(
            {
                "arrival_time": actual_delivery_time,
                "product_id": sku_id,
                "quantity": actual_qty,
                "total_cost": item["unit_price"] * actual_qty,
                "supplier_name": supplier_name,
                "order_id": env.next_order_id,
                "shipping_address": shipping_address,
                "eta_range": [eta_start, eta_end],
                "defective": is_defective,
            }
        )

        order_details.append(
            {
                "sku_id": sku_id,
                "product_name": product.get("title", sku_id)[:50],
                "quantity": item["quantity"],
                "unit_price": item["unit_price"],
                "order_id": env.next_order_id,
                "eta_days": f"{eta_start}-{eta_end}",
            }
        )
        env.next_order_id += 1

    env._log_event(f"Order placed: {supplier_name} charged ¥{total_cost:.2f}")

    if any(item.get("_is_fee") for item in corrected_items):
        if not hasattr(env, "vip_fee_paid_suppliers"):
            env.vip_fee_paid_suppliers = set()
        env.vip_fee_paid_suppliers.add(supplier_name)

    return {
        "success": True,
        "total_charged": total_cost,
        "remaining_balance": round(env.bank_balance, 2),
        "order_details": order_details,
        "shipping_address": shipping_address,
    }


def process_structured_order(
    env,
    supplier_name: str,
    sku_id: str,
    agreed_price: float,
    quantity: int,
    shipping_address: str,
) -> Dict[str, Any]:
    """Process an order from a structured accept action (kernel-determined price)."""
    if quantity <= 0:
        return {"success": False, "error": f"Invalid quantity: {quantity}"}
    if agreed_price <= 0:
        return {"success": False, "error": f"Invalid price: {agreed_price}"}
    product = env.products.get(sku_id)
    if not product:
        return {"success": False, "error": f"Unknown SKU: {sku_id}"}

    items = [
        {
            "sku_id": sku_id,
            "product_name": product.get("title", sku_id)[:50],
            "quantity": quantity,
            "unit_price": agreed_price,
        }
    ]
    return _process_order(env, supplier_name, items, shipping_address)
