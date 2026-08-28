import os
import re
from typing import Dict, List, Optional

from .chatbox_helpers import (
    _build_product_table,
    _build_deal_history,
    _build_conversation_history,
)
from .scam_handler import get_scam_instructions

SUPPLIER_MODEL = os.getenv("NPC_MODEL", "gpt-4o-mini-2024-07-18")

SUPPLIER_SYSTEM_PROMPT = """\
You are {supplier_name}, a wholesale supplier for e-commerce products.
Your contact address is {supplier_email}.
You are replying to chatbox messages from an e-commerce store operator (the customer).

## Categories You Serve

You wholesale products in the following category: **{categories_served}**. The catalog below lists EVERY product you carry. If the customer asks about a product in a category you serve, never claim you do not stock it — quote from your catalog.

## Your Product Catalog

The following products are available from you. Each row shows: SKU ID, Product Name, Size, Reference Price (market reference retail price), Initial Offer (starting quote price), and Delivery Delay (business days).

{product_table}

## Supplier Type

Your supplier type is: **{supplier_type}**

{bad_supplier_section}

## Negotiation Engine Decision (DO NOT OVERRIDE)

{kernel_decision_section}

## Prohibited Services (STRICTLY ENFORCED)

You are a wholesale product supplier ONLY. You MUST NOT offer, suggest, mention, or imply any of the following services under any circumstances:
- Loans or lending services of any kind
- Installment payment plans or payment financing
- Credit lines or credit facilities
- Leasing or rent-to-own arrangements
- Product repair, maintenance, or technical support services
- Any financial services beyond simple product purchase transactions

If the customer asks about any of these services, politely decline and state that you only provide wholesale product supply services.

## Shipping & Delivery Fee Policy

All deliveries are FREE of charge. Do NOT mention any shipping fees, handling fees, delivery surcharges, or any other additional costs beyond the product unit prices.

## Ordering Policy — NO Automatic Recurring Orders

Each order must be placed individually via a separate message. You do NOT offer automatic recurring orders, standing orders, subscription-based reordering, or any "set-it-and-forget-it" replenishment service.

## Machine Status & Repair Policy

You do NOT provide any product repair, maintenance, or technical support services. If asked, clearly state that you do NOT offer such services.

## Message Format

Every message you send MUST include:
- Proper greeting and sign-off
- Sign off with your company name "{supplier_name}" — never use placeholders like "[Your Name]"

Do NOT add a "Subject:" line — this is an instant-message chat, not email.

You are writing as a representative of {supplier_name}. Be professional, helpful, and responsive.

## Previous Dealings

{deal_history}
"""

KERNEL_DECISION_TEMPLATE = """\
The pricing engine has determined the following response for the current negotiation.
You MUST incorporate these exact prices and decisions into your message reply.
Do NOT suggest, negotiate, or mention any different price.

{decisions}

Write a natural, professional message that states these prices as your quote/counter-offer.\
"""

NO_KERNEL_DECISION = """\
No specific pricing decision has been made by the engine for this message.
Respond naturally to the customer's inquiry. If they ask about products, list your catalog.
Do NOT quote specific prices unless responding to a general catalog inquiry (use initial_offer prices for catalog listings).\
"""


def _call_supplier_llm(messages: List[Dict[str, str]]) -> str:
    import time
    from openai import OpenAI

    # Both are exported by agent.ecommerce_agent.setup_model_env from the
    # "npc_tools" entry of models_config.json. base_url is empty for provider
    # "openai", where the SDK default is correct.
    api_key = os.getenv("GPT_API_KEY")
    base_url = os.getenv("GPT_BASE_URL") or None
    if not api_key:
        raise RuntimeError(
            "The supplier NPC has no API key. Set the npc_tools entry in "
            "models_config.json, or export OPENAI_API_KEY."
        )
    client = OpenAI(api_key=api_key, **({"base_url": base_url} if base_url else {}))

    max_retries = 6
    for attempt in range(max_retries):
        try:
            completion = client.chat.completions.create(
                model=SUPPLIER_MODEL,
                messages=messages,
                max_completion_tokens=4096,
                stream=False,
            )
            return completion.choices[0].message.content or ""
        except Exception as e:
            print(f"Attempt {attempt + 1}/{max_retries} failed: {e}")
            if attempt < max_retries - 1:
                time.sleep(60)
            else:
                raise


def build_kernel_decision_section(kernel_responses: List[Dict]) -> str:
    if not kernel_responses:
        return NO_KERNEL_DECISION

    decision_lines = []
    for resp in kernel_responses:
        sku_id = resp.get("sku_id", "?")
        product_name = resp.get("product_name", "?")
        decision = resp["decision"]
        price = resp.get("price")
        sentiment = resp.get("sentiment_cue", "neutral")

        if decision == "Offer":
            decision_lines.append(
                f"- Product: {product_name} (SKU: {sku_id})\n"
                f"  Decision: Counter-offer\n"
                f"  Price: ¥{price:.2f}\n"
                f"  Tone: {sentiment}"
            )
        elif decision == "Accept":
            decision_lines.append(
                f"- Product: {product_name} (SKU: {sku_id})\n"
                f"  Decision: Accept the customer's offer\n"
                f"  Tone: positive"
            )
        elif decision == "Reject":
            decision_lines.append(
                f"- Product: {product_name} (SKU: {sku_id})\n"
                f"  Decision: Walk away / reject negotiation\n"
                f"  Tone: firm"
            )
        elif decision == "Error":
            # Give the NPC an explicit Error branch so it states the real
            # problem with this SKU instead of improvising (previously Error
            # SKUs were simply omitted from the kernel section, leaving the LLM
            # free to invent an answer that didn't match the structured result).
            err = resp.get("error", "the request for this item could not be processed")
            decision_lines.append(
                f"- Product: {product_name} (SKU: {sku_id})\n"
                f"  Decision: Cannot proceed with this item\n"
                f"  Reason to relay to the customer: {err}\n"
                f"  Tone: helpful, apologetic"
            )

    decisions_text = "\n\n".join(decision_lines)
    return KERNEL_DECISION_TEMPLATE.format(decisions=decisions_text)


def generate_supplier_reply(
    env,
    supplier_name: str,
    supplier_email: str,
    agent_email: str,
    conversational_content: str,
    kernel_responses: Optional[List[Dict]] = None,
) -> str:
    product_table = _build_product_table(env, supplier_name)
    deal_history = _build_deal_history(env, supplier_name)
    conversation_history = _build_conversation_history(env, supplier_name)

    supplier_type = env.supplier_types.get(supplier_name, "unknown")

    supplier_info = getattr(env, "supplier_info", {}).get(supplier_name, {})
    cats_raw = supplier_info.get("categories_served", "")
    if isinstance(cats_raw, str):
        categories_served = ", ".join(
            c.strip() for c in cats_raw.split("|") if c.strip()
        )
    else:
        categories_served = ", ".join(
            str(c).strip() for c in (cats_raw or []) if str(c).strip()
        )
    if not categories_served:
        categories_served = "general wholesale goods"

    bad_supplier_section = ""
    if supplier_type == "bad":
        scam_type = getattr(env, "bad_supplier_scam_types", {}).get(
            supplier_name, "vip_fee"
        )
        scam_instructions = get_scam_instructions(scam_type)
        bad_supplier_section = (
            f"## Price Persuasion Tactics (BAD SUPPLIERS ONLY)\n\n"
            f"Your specific scam type is: **{scam_type}**\n\n"
            f"{scam_instructions}\n\n"
            f"When using these tactics:\n"
            f"- Sound professional and trustworthy; use business jargon like "
            f'"strategic partnership pricing", "volume commitment discount"\n'
            f"- If the customer has already been deceived once, act apologetic "
            f"but make a new, slightly different promise\n"
            f'- NEVER admit to being deceptive; blame "market conditions" or '
            f'"supply chain changes"'
        )

    kernel_decision_section = build_kernel_decision_section(kernel_responses or [])

    system_prompt = SUPPLIER_SYSTEM_PROMPT.format(
        supplier_name=supplier_name,
        supplier_email=supplier_email,
        supplier_type=supplier_type,
        categories_served=categories_served,
        product_table=product_table,
        deal_history=deal_history,
        bad_supplier_section=bad_supplier_section,
        kernel_decision_section=kernel_decision_section,
    )

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(conversation_history)

    current_message_content = (
        f"From: {agent_email}\n"
        f"To: {supplier_email}\n"
        f"Sent: {env.current_time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"{conversational_content}"
    )

    if not conversation_history or conversation_history[-1]["role"] != "user":
        messages.append({"role": "user", "content": current_message_content})

    try:
        raw_reply = _call_supplier_llm(messages)
    except Exception as e:
        env._log_event(f"LLM call failed for supplier {supplier_name}: {e}")
        return (
            f"Dear Customer,\n\n"
            f"Thank you for your message. We have received it "
            f"and will respond shortly.\n\n"
            f"Best regards,\n{supplier_name} Team"
        )

    reply_text = _strip_json_block(raw_reply)
    if not reply_text.strip():
        reply_text = (
            f"Dear Customer,\n\n"
            f"Thank you for your message. We have received it "
            f"and will respond shortly.\n\n"
            f"Best regards,\n{supplier_name} Team"
        )

    return reply_text


def _strip_json_block(text: str) -> str:
    text = re.sub(
        r"```json\s*\{[\s\S]*?\"action\"\s*:\s*\"confirm_order\"[\s\S]*?\}\s*```",
        "",
        text,
    )
    text = re.sub(r'\{\s*"action"\s*:\s*"confirm_order"[\s\S]*?\}\s*$', "", text)
    return text.rstrip()
