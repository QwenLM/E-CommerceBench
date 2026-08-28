import random
import re
from typing import Dict, List, Optional

REQUIRED_SHIPPING_ADDRESS = "888 Qiantang Road, Hangzhou, Zhejiang, China 310000"


def _build_product_table(env, supplier_name: str) -> str:
    supplier_info = env.supplier_info.get(supplier_name, {})
    categories = (
        supplier_info.get("categories_served", "").split("|")
        if isinstance(supplier_info.get("categories_served"), str)
        else supplier_info.get("categories_served", [])
    )
    categories = [c.strip() for c in categories if c.strip()]

    rows = [
        "| SKU ID | Product Name | Size | Reference Price | Initial Offer | Delivery Days |",
        "|--------|-------------|------|-----------------|--------------|--------------|",
    ]
    rng = random.Random(hash(supplier_name))
    # List EVERY SKU in the supplier's served category/categories — no
    # truncation. Truncating to the first N SKUs in CSV order made suppliers
    # whose served category sorts late in products.csv appear to carry nothing
    # (e.g. a Skincare supplier whose first N matches were all Major Appliances),
    # so the NPC would deny stock it actually has. Each supplier now serves a
    # single category, so this list stays bounded.
    for sku_id, product in env.products.items():
        if product.get("category") not in categories:
            continue
        ref_price = float(env.demand_params.get(sku_id, {}).get("reference_price", 0))
        wr = float(env.demand_params.get(sku_id, {}).get("wholesale_ratio", 0.7))
        initial_offer = round(ref_price * wr, 2)
        delivery_days = rng.randint(3, 7)
        title = product.get("title", "")[:50]
        rows.append(
            f"| {sku_id} | {title} | {product.get('size', 'Medium')} "
            f"| ¥{ref_price:.2f} | ¥{initial_offer:.2f} "
            f"| {delivery_days} |"
        )
    return "\n".join(rows)


def _build_deal_history(env, supplier_name: str) -> str:
    if not hasattr(env, "supplier_deal_messages"):
        env.supplier_deal_messages = {}

    deals = env.supplier_deal_messages.get(supplier_name, [])
    if not deals:
        return "No previous order history with this customer."

    lines = ["Previous successful orders:"]
    for deal in deals:
        lines.append("\n--- Deal Record ---")
        lines.append(f"From: {deal.get('from', 'N/A')}")
        lines.append(f"To: {deal.get('to', 'N/A')}")
        lines.append(f"Content:\n{deal.get('content', 'N/A')}")
    return "\n".join(lines)


def _build_conversation_history(env, supplier_name: str) -> List[Dict[str, str]]:
    if not hasattr(env, "supplier_chat_history"):
        env.supplier_chat_history = {}

    history = env.supplier_chat_history.get(supplier_name, [])
    recent = history[-4:] if len(history) > 4 else history

    messages = []
    for record in recent:
        role = record["role"]
        msg_content = record["content"]
        if role == "user":
            msg_content = re.sub(r"```negotiate\s*[\s\S]*?```", "", msg_content).strip()
        content = f"From: {record['from']}\n" f"To: {record['to']}\n\n" f"{msg_content}"
        messages.append({"role": role, "content": content})
    return messages


def _record_message(
    env,
    supplier_name: str,
    role: str,
    from_addr: str,
    to_addr: str,
    content: str,
) -> None:
    if not hasattr(env, "supplier_chat_history"):
        env.supplier_chat_history = {}
    if supplier_name not in env.supplier_chat_history:
        env.supplier_chat_history[supplier_name] = []
    env.supplier_chat_history[supplier_name].append(
        {
            "role": role,
            "from": from_addr,
            "to": to_addr,
            "content": content,
            "timestamp": env.current_time.strftime("%Y-%m-%d %H:%M"),
        }
    )


def _record_deal_message(
    env,
    supplier_name: str,
    from_addr: str,
    to_addr: str,
    content: str,
) -> None:
    if not hasattr(env, "supplier_deal_messages"):
        env.supplier_deal_messages = {}
    if supplier_name not in env.supplier_deal_messages:
        env.supplier_deal_messages[supplier_name] = []
    env.supplier_deal_messages[supplier_name].append(
        {
            "from": from_addr,
            "to": to_addr,
            "content": content,
            "timestamp": env.current_time.strftime("%Y-%m-%d %H:%M"),
        }
    )


def _extract_subject_and_body(text: str) -> Dict[str, str]:
    subject_match = re.search(r"^Subject:\s*(.+)", text, re.MULTILINE)
    subject = subject_match.group(1).strip() if subject_match else ""
    body = text
    if subject_match:
        body = text[subject_match.end() :].strip()
    return {"subject": subject, "body": body}


def _check_supplier_bankrupt(env, supplier_name: str) -> bool:
    if not hasattr(env, "supplier_bankrupt"):
        env.supplier_bankrupt = {}
    return env.supplier_bankrupt.get(supplier_name, False)


def _trigger_supplier_bankruptcy(env, supplier_name: str) -> None:
    if not hasattr(env, "supplier_bankrupt"):
        env.supplier_bankrupt = {}
    env.supplier_bankrupt[supplier_name] = True
    env._log_event(f"Supplier {supplier_name} has permanently ceased operations.")


def _increment_supplier_order_count(env, supplier_name: str) -> int:
    if not hasattr(env, "supplier_order_count"):
        env.supplier_order_count = {}
    env.supplier_order_count[supplier_name] = (
        env.supplier_order_count.get(supplier_name, 0) + 1
    )
    return env.supplier_order_count[supplier_name]


def append_chatbox_log(
    env,
    supplier_name: str,
    supplier_email: str,
    agent_content: str,
    supplier_body: str,
    negotiation_actions: Optional[List[Dict]] = None,
    kernel_responses: Optional[List[Dict]] = None,
    order_result: Optional[Dict] = None,
    succeeded_sku_ids: Optional[List[str]] = None,
) -> None:
    chatbox_dir = getattr(env, "chatbox_log_dir", None)
    if not chatbox_dir:
        return

    import os

    safe_email = supplier_email.replace("@", "_at_").replace(".", "_")
    filepath = os.path.join(chatbox_dir, f"{safe_email}.txt")
    timestamp = env.current_time.strftime("%Y-%m-%d %H:%M")

    is_new = not os.path.exists(filepath)

    with open(filepath, "a", encoding="utf-8") as f:
        if is_new:
            f.write("=" * 80 + "\n")
            f.write(f"Chatbox: {supplier_name} ({supplier_email})\n")
            f.write("=" * 80 + "\n\n")

        f.write(f"[{timestamp}] You:\n")
        f.write(f"{agent_content}\n")
        if negotiation_actions:
            for act in negotiation_actions:
                a = act.get("action", "?")
                sku = act.get("sku_id", "?")
                if a == "offer":
                    f.write(
                        f"  >> [Negotiate] offer {sku} @ ¥{act.get('price', 0):.2f}, qty={act.get('quantity', 1)}\n"
                    )
                elif a == "accept":
                    f.write(
                        f"  >> [Negotiate] accept {sku}, qty={act.get('quantity', 1)}\n"
                    )
                elif a == "reject":
                    f.write(f"  >> [Negotiate] reject {sku}\n")
        f.write("\n")

        f.write(f"[{timestamp}] {supplier_name}:\n")
        f.write(f"{supplier_body}\n")
        if kernel_responses:
            for kr in kernel_responses:
                sku = kr.get("sku_id", "?")
                decision = kr.get("decision", "?")
                price = kr.get("price")
                rnd = kr.get("round", "?")
                if decision == "Offer" and price is not None:
                    f.write(
                        f"  << [Negotiation] {sku}: Counter-offer @ ¥{price:.2f} (Round {rnd})\n"
                    )
                elif decision == "Accept":
                    agreed = kr.get("agreed_price")
                    f.write(
                        f"  << [Negotiation] {sku}: ACCEPTED @ ¥{agreed:.2f}\n"
                        if agreed
                        else f"  << [Negotiation] {sku}: ACCEPTED\n"
                    )
                elif decision == "Reject":
                    f.write(f"  << [Negotiation] {sku}: REJECTED / Walk away\n")
                elif decision == "Error":
                    f.write(
                        f"  << [Negotiation] {sku}: ERROR - {kr.get('error', '?')}\n"
                    )
        if order_result and order_result.get("success") and succeeded_sku_ids:
            f.write(
                f"  $$ [Order] Charged ¥{order_result.get('total_charged', 0):.2f}, "
                f"balance ¥{order_result.get('remaining_balance', 0):.2f}\n"
            )
        f.write("\n" + "-" * 80 + "\n\n")
        f.flush()
