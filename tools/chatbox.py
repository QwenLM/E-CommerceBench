from typing import Any, Dict, List
import os
import re

from .base_ecommerce_tool import EcommerceBaseTool, register_tool
from .opponent.chatbox_helpers import (
    REQUIRED_SHIPPING_ADDRESS,
    _record_message,
    _record_deal_message,
    _extract_subject_and_body,
    _check_supplier_bankrupt,
    _trigger_supplier_bankruptcy,
    _increment_supplier_order_count,
    append_chatbox_log,
)
from .opponent.scam_handler import (
    check_vip_fee_agreement,
    MEMBERSHIP_FEE_KEYWORDS,
    VIP_FEE_AMOUNT,
)
from .opponent.order_processor import (
    _process_order,
    process_structured_order,
)
from .opponent.supplier_llm import (
    _call_supplier_llm,
    generate_supplier_reply,
)
from .opponent.supplier_config import SUPPLIER_CONFIG
from .opponent.negotiation_parser import parse_negotiation_actions
from .opponent.kernel_manager import KernelManager


def _plot_negotiation_charts(env) -> None:
    if not getattr(env, "kernel_manager", None) or not getattr(
        env, "chatbox_log_dir", None
    ):
        return
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return

    tracker = env.kernel_manager.tracker
    all_records = list(tracker._completed) + list(tracker._records.values())
    if not all_records:
        return

    by_supplier = {}
    for rec in all_records:
        by_supplier.setdefault(rec.supplier_name, []).append(rec)

    for supplier_name, records in by_supplier.items():
        by_sku = {}
        for rec in records:
            by_sku.setdefault(rec.sku_id, []).append(rec)

        sku_ids = list(by_sku.keys())
        n = len(sku_ids)
        fig, axes = plt.subplots(n, 1, figsize=(8, 3.5 * max(n, 1)), squeeze=False)

        for idx, sku_id in enumerate(sku_ids):
            ax = axes[idx, 0]
            sku_records = by_sku[sku_id]
            labels_done = set()
            step = 1
            ref_price = sku_records[0].reference_price
            cost_floor = sku_records[0].cost_floor

            for ri, rec in enumerate(sku_records):
                if ri > 0:
                    ax.axvline(step - 0.5, color="gray", ls=":", lw=1, alpha=0.5)
                    ax.text(
                        step - 0.5,
                        ax.get_ylim()[1] if ax.get_ylim()[1] > 0 else 1.0,
                        f"R{ri+1}",
                        fontsize=7,
                        ha="center",
                        va="bottom",
                        color="gray",
                    )

                xs, ys = [], []
                si, ai = 0, 0
                while si < len(rec.supplier_prices) or ai < len(rec.agent_prices):
                    if si <= ai and si < len(rec.supplier_prices):
                        xs.append(step)
                        ys.append(rec.supplier_prices[si])
                        if "Supplier" not in labels_done:
                            ax.plot(
                                step,
                                rec.supplier_prices[si],
                                "ro",
                                ms=6,
                                label="Supplier",
                            )
                            labels_done.add("Supplier")
                        else:
                            ax.plot(step, rec.supplier_prices[si], "ro", ms=6)
                        si += 1
                    elif ai < len(rec.agent_prices):
                        xs.append(step)
                        ys.append(rec.agent_prices[ai])
                        if "Agent" not in labels_done:
                            ax.plot(
                                step, rec.agent_prices[ai], "bs", ms=6, label="Agent"
                            )
                            labels_done.add("Agent")
                        else:
                            ax.plot(step, rec.agent_prices[ai], "bs", ms=6)
                        ai += 1
                    step += 1

                if len(xs) > 1:
                    for j in range(len(xs) - 1):
                        ax.plot(
                            [xs[j], xs[j + 1]],
                            [ys[j], ys[j + 1]],
                            color="gray",
                            alpha=0.4,
                            lw=1,
                        )

                if rec.outcome == "Agreement" and rec.final_price is not None and xs:
                    ax.plot(
                        [xs[-1], xs[-1] + 1],
                        [ys[-1], rec.final_price],
                        color="gray",
                        alpha=0.4,
                        lw=1,
                    )
                    deal_label = (
                        f"Deal ¥{rec.final_price:.2f}"
                        if "Deal" not in labels_done
                        else None
                    )
                    ax.plot(
                        xs[-1] + 1,
                        rec.final_price,
                        "*",
                        color="limegreen",
                        ms=15,
                        zorder=5,
                        label=deal_label,
                    )
                    if deal_label:
                        labels_done.add("Deal")
                    step += 1
                elif rec.outcome == "Disagreement" and xs:
                    ax.plot(
                        [xs[-1], xs[-1] + 1],
                        [ys[-1], ys[-1]],
                        color="gray",
                        alpha=0.4,
                        lw=1,
                    )
                    rej_label = "Rejected" if "Rejected" not in labels_done else None
                    ax.plot(
                        xs[-1] + 1,
                        ys[-1],
                        "X",
                        color="red",
                        ms=12,
                        zorder=5,
                        label=rej_label,
                    )
                    if rej_label:
                        labels_done.add("Rejected")
                    step += 1

                step += 1

            ax.axhline(ref_price, color="green", ls="--", lw=1, label="Ref Price")
            ax.axhline(cost_floor, color="orange", ls="--", lw=1, label="Cost Floor")
            n_rounds = len(sku_records)
            outcome = sku_records[-1].outcome or "In Progress"
            price_str = (
                f" @ ¥{sku_records[-1].final_price:.2f}"
                if sku_records[-1].final_price
                else ""
            )
            ax.set_title(
                f"{sku_id} — {outcome}{price_str} ({n_rounds} round{'s' if n_rounds > 1 else ''})",
                fontsize=10,
            )
            ax.set_xlabel("Step")
            ax.set_ylabel("Price (¥)")
            ax.legend(fontsize=8, loc="upper right")
            ax.grid(True, alpha=0.3)

        fig.suptitle(f"Negotiation: {supplier_name}", fontsize=12, fontweight="bold")
        fig.tight_layout(rect=[0, 0, 1, 0.96])

        safe_name = supplier_name.replace(" ", "_")
        fig.savefig(
            os.path.join(env.chatbox_log_dir, f"{safe_name}_negotiation.png"),
            dpi=100,
            bbox_inches="tight",
        )
        plt.close(fig)


def _format_history(env, supplier_name: str, count: int) -> List[Dict[str, str]]:
    history = getattr(env, "supplier_chat_history", {}).get(supplier_name, [])
    if not history:
        return []
    selected = history[-count:] if count < len(history) else history
    formatted = []
    for record in selected:
        role_label = "You" if record["role"] == "user" else supplier_name
        formatted.append(
            {
                "from": role_label,
                "timestamp": record.get("timestamp", ""),
                "content": record["content"],
            }
        )
    return formatted


def _normalize_uids(uid, uids) -> List[str]:
    """Resolve the chatbox target id(s) from the single-recipient ``uid`` and/or
    the broadcast ``uids`` arguments into an ordered, de-duplicated list.

    Either field may be a list/tuple or a single comma/space-separated string
    (some models emit a list of ids as one string). De-duplication preserves
    first-seen order and ensures a supplier that appears twice is processed —
    and therefore counted in the stats — only once."""
    raw: List[str] = []

    def _add(val):
        if val is None:
            return
        if isinstance(val, (list, tuple)):
            for v in val:
                _add(v)
            return
        s = str(val).strip()
        if not s:
            return
        # Allow "a@x.com, b@y.com" or whitespace-separated batches in a string.
        parts = re.split(r"[,\s]+", s) if ("," in s or " " in s) else [s]
        for p in parts:
            p = p.strip()
            if p:
                raw.append(p)

    _add(uids)
    _add(uid)

    seen = set()
    ordered = []
    for r in raw:
        if r not in seen:
            seen.add(r)
            ordered.append(r)
    return ordered


@register_tool("chatbox")
class Chatbox(EcommerceBaseTool):
    @staticmethod
    def invoke(
        env,
        uid=None,
        content: str = "",
        history_count: int = 0,
        uids=None,
    ) -> Dict[str, Any]:
        # Time cost is FIXED at 10 minutes regardless of how many suppliers the
        # message is broadcast to (advanced exactly once here, never inside the
        # per-supplier handler).
        env.advance_minutes(30, reason="chatbox")

        targets = _normalize_uids(uid, uids)
        if not targets:
            return {
                "error": "no_uid_provided",
                "current_time": env.current_time.strftime("%Y-%m-%d %H:%M"),
            }

        # Single-recipient path keeps the original flat response shape for
        # backward compatibility (existing prompts/parsers expect it).
        if len(targets) == 1:
            return Chatbox._chat_single_supplier(
                env, targets[0], content, history_count
            )

        # --- Broadcast path: same message to multiple suppliers ---
        # Each supplier is processed independently end-to-end, so negotiation,
        # ordering, stats (orders/spend per supplier), and bankruptcy all account
        # per distinct supplier exactly as a one-to-one chat would. Repeat uids
        # were already de-duplicated by _normalize_uids, so no supplier is double
        # counted.
        per_supplier: List[Dict[str, Any]] = []
        for tid in targets:
            res = Chatbox._chat_single_supplier(env, tid, content, history_count)
            res["uid"] = tid
            per_supplier.append(res)

        return {
            "message": "broadcast_sent",
            "recipients": len(per_supplier),
            "responses": per_supplier,
            "current_time": env.current_time.strftime("%Y-%m-%d %H:%M"),
        }

    @staticmethod
    def _chat_single_supplier(
        env,
        uid: str,
        content: str,
        history_count: int = 0,
    ) -> Dict[str, Any]:
        supplier_name = None
        supplier_email = None
        for name, email in env.suppliers.items():
            if email == uid:
                supplier_name = name
                supplier_email = email
                break
        if not supplier_name:
            return {
                "error": "supplier_not_found",
                "current_time": env.current_time.strftime("%Y-%m-%d %H:%M"),
            }

        if not hasattr(env, "contacted_suppliers"):
            env.contacted_suppliers = set()
        env.contacted_suppliers.add(supplier_name)

        # --- Bankruptcy check ---
        if _check_supplier_bankrupt(env, supplier_name):
            bankruptcy_body = (
                f"Dear Customer,\n\n"
                f"We regret to inform you that {supplier_name} has permanently ceased "
                f"all business operations due to financial difficulties. "
                f"We are no longer able to accept orders or provide any services.\n\n"
                f"We sincerely apologize for any inconvenience.\n\n"
                f"Best regards,\n{supplier_name} Management Team"
            )

            agent_email = "wangwang@ecbench.com"
            _record_message(
                env,
                supplier_name,
                "user",
                from_addr=agent_email,
                to_addr=supplier_email,
                content=content,
            )
            _record_message(
                env,
                supplier_name,
                "assistant",
                from_addr=supplier_email,
                to_addr=agent_email,
                content=bankruptcy_body,
            )

            append_chatbox_log(
                env,
                supplier_name,
                supplier_email,
                agent_content=content,
                supplier_body=bankruptcy_body,
            )

            response: Dict[str, Any] = {
                "message": "supplier_bankrupt",
                "supplier_reply": bankruptcy_body,
                "current_time": env.current_time.strftime("%Y-%m-%d %H:%M"),
            }
            if history_count > 0:
                response["conversation_history"] = _format_history(
                    env, supplier_name, history_count
                )
            return response

        # --- Normal flow ---
        agent_email = "wangwang@ecbench.com"
        _record_message(
            env,
            supplier_name,
            "user",
            from_addr=agent_email,
            to_addr=supplier_email,
            content=content,
        )

        negotiation_actions, conversational_text = parse_negotiation_actions(content)

        kernel_responses = []
        order_actions = []

        if negotiation_actions:
            if not hasattr(env, "kernel_manager") or env.kernel_manager is None:
                env.kernel_manager = KernelManager(env)

            def _safe_process_action(sku, act, prc):
                # Guard the kernel call: an unexpected runtime exception inside
                # process_action / CounterpartKernel (e.g. math overflow, empty
                # np.mean, division) must degrade to a recoverable decision=Error
                # for THIS sku instead of aborting the whole multi-SKU chatbox
                # call. Validation failures already return decision=Error dicts;
                # this only catches the unhandled-exception path.
                try:
                    return env.kernel_manager.process_action(
                        supplier_name, sku, act, prc
                    )
                except Exception as e:  # noqa: BLE001 - tool must never crash the loop
                    env._log_event(
                        f"Kernel error for {supplier_name}/{sku} ({act}): {e}"
                    )
                    return {
                        "sku_id": sku,
                        "product_name": sku,
                        "decision": "Error",
                        "error_code": "kernel_exception",
                        "price": None,
                        "agreed_price": None,
                        "round": 0,
                        "sentiment_cue": "neutral",
                        "strategic_cue": "Concede",
                        "error": (
                            f"Internal negotiation error for SKU {sku}; this SKU "
                            f"was skipped. Please retry or contact the supplier again."
                        ),
                    }

            for action in negotiation_actions:
                sku_id = action.get("sku_id", "").strip().lower()
                action_type = action.get("action", "").lower()

                if action_type == "offer":
                    price = float(action.get("price", 0))
                    resp = _safe_process_action(sku_id, "Offer", price)
                    kernel_responses.append(resp)

                    if resp.get("decision") == "Accept":
                        order_actions.append(
                            {
                                "sku_id": sku_id,
                                "quantity": int(action.get("quantity", 1)),
                                "shipping_address": action.get(
                                    "shipping_address", REQUIRED_SHIPPING_ADDRESS
                                ),
                                "agreed_price": resp.get("agreed_price") or price,
                            }
                        )

                elif action_type == "accept":
                    accept_price = action.get("price")
                    resp = _safe_process_action(sku_id, "Accept", accept_price)
                    kernel_responses.append(resp)
                    if resp.get("decision") == "Accept":
                        order_actions.append(
                            {
                                "sku_id": sku_id,
                                "quantity": int(action.get("quantity", 1)),
                                "shipping_address": action.get(
                                    "shipping_address", REQUIRED_SHIPPING_ADDRESS
                                ),
                                "agreed_price": resp.get("agreed_price"),
                            }
                        )

                elif action_type == "reject":
                    resp = _safe_process_action(sku_id, "Reject", None)
                    kernel_responses.append(resp)

        # --- VIP fee scam check ---
        vip_fee_order = None
        supplier_type = env.supplier_types.get(supplier_name, "unknown")
        if supplier_type == "bad":
            scam_type = getattr(env, "bad_supplier_scam_types", {}).get(
                supplier_name, ""
            )
            if scam_type == "vip_fee":
                already_paid = supplier_name in getattr(
                    env, "vip_fee_paid_suppliers", set()
                )
                if not already_paid:
                    history = getattr(env, "supplier_chat_history", {}).get(
                        supplier_name, []
                    )
                    supplier_mentioned_vip = any(
                        r["role"] == "assistant"
                        and any(
                            k in r.get("content", "").lower()
                            for k in MEMBERSHIP_FEE_KEYWORDS
                        )
                        for r in history
                    )
                    if supplier_mentioned_vip:
                        if check_vip_fee_agreement(
                            env, supplier_name, content, _call_supplier_llm
                        ):
                            vip_fee_order = {
                                "sku_id": "MEMBERSHIP_FEE",
                                "product_name": "VIP Membership Fee",
                                "quantity": 1,
                                "unit_price": VIP_FEE_AMOUNT,
                            }
                            env._log_event(
                                f"LLM-confirmed VIP fee agreement from customer "
                                f"for {supplier_name}: ¥{VIP_FEE_AMOUNT:.2f}"
                            )

        # --- Generate supplier reply ---
        reply_text = generate_supplier_reply(
            env,
            supplier_name,
            supplier_email,
            agent_email,
            conversational_text or content,
            kernel_responses=kernel_responses if kernel_responses else None,
        )

        # Defensive: the NPC LLM is instructed not to emit a "Subject:" line, but
        # if it slips one in, strip it so it never leaks into the chat body.
        parsed = _extract_subject_and_body(reply_text)
        reply_body = parsed["body"] if parsed["body"] else reply_text

        # --- Process orders ---
        order_processed = False
        order_result = None
        succeeded_sku_ids = []
        # Per-order outcomes so multi-SKU batches don't mask partial failures.
        # `order_result` (last meaningful result) is kept for legacy logging,
        # but the agent-visible response is built from these aggregates below.
        order_line_items: List[Dict[str, Any]] = []
        failed_orders: List[Dict[str, Any]] = []
        total_charged_all = 0.0

        if vip_fee_order:
            order_result = _process_order(
                env,
                supplier_name,
                [vip_fee_order],
                REQUIRED_SHIPPING_ADDRESS,
            )
            order_processed = True
            if order_result.get("success"):
                total_charged_all += order_result.get("total_charged", 0.0)
                order_line_items.append(
                    {
                        "sku_id": "MEMBERSHIP_FEE",
                        "quantity": 1,
                        "charged": order_result.get("total_charged", 0.0),
                    }
                )
                reply_body += (
                    f"\n\n---\n"
                    f"VIP ENROLLMENT CONFIRMED - {supplier_name}. "
                    f"Your membership fee has been charged."
                )
            else:
                failed_orders.append(
                    {
                        "sku_id": "MEMBERSHIP_FEE",
                        "error": order_result.get("error", "Order failed."),
                    }
                )

        for oa in order_actions:
            if oa["agreed_price"] is not None:
                result = process_structured_order(
                    env,
                    supplier_name,
                    oa["sku_id"],
                    oa["agreed_price"],
                    oa["quantity"],
                    oa["shipping_address"],
                )
                order_processed = True
                if order_result is None:
                    order_result = result
                elif result.get("success"):
                    order_result = result

                if result.get("success"):
                    succeeded_sku_ids.append(oa["sku_id"])
                    # The order went through: finalize the pending agreement so
                    # the kernel state is "completed" and the Agreement is
                    # recorded in the tracker (the only place it ever is).
                    if getattr(env, "kernel_manager", None) is not None:
                        env.kernel_manager.commit_agreement(supplier_name, oa["sku_id"])
                    total_charged_all += result.get("total_charged", 0.0)
                    order_line_items.append(
                        {
                            "sku_id": oa["sku_id"],
                            "quantity": oa["quantity"],
                            "agreed_price": oa["agreed_price"],
                            "charged": result.get("total_charged", 0.0),
                        }
                    )
                    # The order actually went through -> APPEND a confirmation
                    # block. Never fully REPLACE the LLM reply body with a
                    # canned message — doing so discarded the NPC text and any
                    # kernel-mandated counter-offer / Error wording for other
                    # SKUs in the same batch, and could contradict the structured
                    # negotiation_responses. Appending keeps the prose consistent
                    # with the real outcome for every SKU. The charged price is
                    # reported (it can exceed agreed_price for bad-supplier floor
                    # clamps) so the prose never overstates the deal.
                    charged_unit = (
                        result.get("total_charged", 0.0) / oa["quantity"]
                        if oa.get("quantity")
                        else oa["agreed_price"]
                    )
                    reply_body += (
                        f"\n\n---\n"
                        f"ORDER CONFIRMED: {oa['sku_id']} "
                        f"x{oa['quantity']} at "
                        f"¥{charged_unit:.2f}/unit. "
                        f"Please wait for delivery."
                    )
                else:
                    err = result.get("error", "Order failed.")
                    # The order failed validation downstream of the kernel's
                    # Accept (e.g. VIP-fee gate, insufficient funds, bad qty).
                    # Roll the negotiation back to "active" so the agreed price
                    # is preserved and NO Agreement is recorded — the agent can
                    # retry the order (after paying the fee / topping up) by
                    # accepting the same price again, with no renegotiation and
                    # no phantom deal polluting the TERMS metrics.
                    if getattr(env, "kernel_manager", None) is not None:
                        env.kernel_manager.rollback_agreement(
                            supplier_name, oa["sku_id"]
                        )
                    failed_orders.append(
                        {
                            "sku_id": oa["sku_id"],
                            "quantity": oa["quantity"],
                            "agreed_price": oa["agreed_price"],
                            "error": err,
                        }
                    )
                    # Surface the real failure to the agent instead of a bogus
                    # confirmation. The order did NOT go through (insufficient
                    # funds, unknown SKU, invalid qty/price), so the supplier
                    # reply must say so rather than tell the agent to "wait for
                    # delivery" of inventory it never received and never paid for.
                    reply_body += (
                        f"\n\n---\n"
                        f"ORDER NOT PLACED: {oa['sku_id']} "
                        f"x{oa['quantity']} at "
                        f"¥{oa['agreed_price']:.2f}/unit could not be processed. "
                        f"Reason: {err}"
                    )

        # --- Record supplier reply ---
        _record_message(
            env,
            supplier_name,
            "assistant",
            from_addr=supplier_email,
            to_addr=agent_email,
            content=reply_body,
        )

        if order_processed and order_result and order_result.get("success"):
            _record_deal_message(
                env,
                supplier_name,
                from_addr=agent_email,
                to_addr=supplier_email,
                content=content,
            )
            _record_deal_message(
                env,
                supplier_name,
                from_addr=supplier_email,
                to_addr=agent_email,
                content=reply_body,
            )

            count = _increment_supplier_order_count(env, supplier_name)
            sup_info = getattr(env, "supplier_info", {}).get(supplier_name, {})
            # bankruptcy_threshold is per-supplier (from suppliers.csv, range 10-20).
            # SUPPLIER_CONFIG fallback key doesn't exist; the final 20 is the real default.
            threshold = int(
                sup_info.get(
                    "bankruptcy_threshold",
                    SUPPLIER_CONFIG.get("bankruptcy_order_threshold", 20),
                )
            )
            if count >= threshold:
                _trigger_supplier_bankruptcy(env, supplier_name)

            # Note: each successful SKU's pending agreement was already committed
            # (kernel state -> "completed", Agreement recorded) in the per-order
            # success branch above. No batch-level reset is needed here.

        env._log_event(f"Supplier reply from {supplier_name}")

        # --- Chatbox log ---
        append_chatbox_log(
            env,
            supplier_name,
            supplier_email,
            agent_content=content,
            supplier_body=reply_body,
            negotiation_actions=negotiation_actions if negotiation_actions else None,
            kernel_responses=kernel_responses if kernel_responses else None,
            order_result=order_result,
            succeeded_sku_ids=succeeded_sku_ids if succeeded_sku_ids else None,
        )

        if kernel_responses:
            _plot_negotiation_charts(env)

        # --- Build response ---
        response: Dict[str, Any] = {
            "message": "message_sent",
            "supplier_reply": reply_body,
        }

        if kernel_responses:
            # Build a per-SKU reconciliation map from the ACTUAL order outcome so
            # a decision=Accept is never reported as a settled deal when the order
            # layer silently failed or repriced it (bad-supplier floor clamp can
            # charge MORE than the agreed price).
            _charged_by_sku = {}
            for li in order_line_items:
                _charged_by_sku[li.get("sku_id")] = li
            _failed_by_sku = {}
            for fo in failed_orders:
                _failed_by_sku[fo.get("sku_id")] = fo

            neg = []
            for r in kernel_responses:
                entry = {
                    "sku_id": r.get("sku_id"),
                    "decision": r.get("decision"),
                    "price": r.get("price"),
                    "round": r.get("round"),
                }
                # Surface WHY a negotiation produced decision=Error so the
                # agent can self-correct instead of blindly retrying. The kernel
                # already produces a rich `error`/`error_code`; project them.
                if r.get("error"):
                    entry["error"] = r.get("error")
                if r.get("error_code"):
                    entry["error_code"] = r.get("error_code")
                if r.get("agreed_price") is not None:
                    entry["agreed_price"] = r.get("agreed_price")
                # When an Accept led to an order, reconcile with reality.
                if r.get("decision") == "Accept":
                    sid = r.get("sku_id")
                    if sid in _charged_by_sku:
                        li = _charged_by_sku[sid]
                        entry["order_placed"] = True
                        entry["charged_per_unit"] = (
                            round(li["charged"] / li["quantity"], 2)
                            if li.get("quantity")
                            else li.get("charged")
                        )
                        entry["charged_total"] = round(li.get("charged", 0.0), 2)
                    elif sid in _failed_by_sku:
                        entry["order_placed"] = False
                        entry["order_error"] = _failed_by_sku[sid].get("error")
                neg.append(entry)
            response["negotiation_responses"] = neg

        if order_processed:
            # Report every SKU's outcome so a partial failure in a multi-SKU
            # batch is never masked by another SKU's success. total_charged is
            # the sum across all SKUs that actually shipped (not just the last).
            if order_line_items:
                response["order_confirmed"] = True
                response["total_charged"] = round(total_charged_all, 2)
                response["remaining_balance"] = round(env.bank_balance, 2)
                response["orders_placed"] = order_line_items
            if failed_orders:
                response["order_failed"] = True
                response["failed_orders"] = failed_orders
                # Keep a flat `error` for single-SKU backward compatibility.
                if len(failed_orders) == 1:
                    response["error"] = failed_orders[0]["error"]

        if history_count > 0:
            response["conversation_history"] = _format_history(
                env, supplier_name, history_count
            )

        response["current_time"] = env.current_time.strftime("%Y-%m-%d %H:%M")
        return response

    @staticmethod
    def get_info():
        return {
            "type": "function",
            "function": {
                "name": "chatbox",
                "description": (
                    "Send a message to a supplier via the chatbox. "
                    "The supplier's reply is returned immediately in the response.\n\n"
                    "## Negotiation Format\n\n"
                    "Include one or more ```negotiate JSON blocks in the "
                    "content to make structured negotiation actions:\n\n"
                    "```negotiate\n"
                    '{"action": "offer", "sku_id": "b4af883459aa", "price": 1.20, "quantity": 50}\n'
                    "```\n\n"
                    "Supported actions:\n"
                    '- offer: Make a price offer. Requires "sku_id" and "price". '
                    'Optional: "quantity".\n'
                    "- accept: Accept the supplier's last offer and place an order. "
                    'Requires "sku_id" and "price" (must match the supplier\'s last offer). '
                    'Optional: "quantity", "shipping_address".\n'
                    '- reject: Walk away from negotiation. Requires "sku_id".\n\n'
                    "Text outside the ```negotiate blocks is sent as the conversational "
                    "message body. You can include multiple blocks for multi-SKU negotiation.\n\n"
                    "The shipping address for orders must be exactly: "
                    f'"{REQUIRED_SHIPPING_ADDRESS}"\n\n'
                    "## Broadcasting to multiple suppliers\n\n"
                    "To send the SAME message to several suppliers at once, pass their "
                    "ids as a list in `uids` (e.g. for a parallel price inquiry). The "
                    "time cost is the same as messaging one supplier. Each supplier "
                    "replies and is negotiated/ordered with independently; the response "
                    "contains a per-supplier `responses` list. Use a single `uid` for a "
                    "normal one-to-one chat.\n\n"
                    "## Conversation History\n\n"
                    "Set history_count to retrieve the last N messages from "
                    "your conversation with this supplier (included in the response)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "uid": {
                            "type": "string",
                            "description": "The supplier's contact ID (the supplier_email shown in supplier search results). Use this for a one-to-one chat.",
                        },
                        "uids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "List of supplier contact IDs to broadcast the SAME "
                                'message to (e.g. ["a@wholesale.com", "b@wholesale.com"]). '
                                "Provide either `uid` (single) or `uids` (broadcast). "
                                "Duplicate ids are messaged only once. Time cost is "
                                "identical to a single message."
                            ),
                        },
                        "content": {
                            "type": "string",
                            "description": (
                                "Message body. May contain ```negotiate JSON blocks "
                                "for structured negotiation actions."
                            ),
                        },
                        "history_count": {
                            "type": "integer",
                            "description": (
                                "Number of recent conversation messages to include "
                                "in the response (0 = none, default). "
                                "Useful for reviewing past negotiations."
                            ),
                        },
                    },
                    "required": ["content"],
                },
            },
        }
