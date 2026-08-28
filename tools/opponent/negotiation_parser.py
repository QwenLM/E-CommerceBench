"""Parse structured negotiation actions from chatbox message content.

The agent embeds ```negotiate JSON blocks in chatbox content to make
structured offer/accept/reject actions. This parser extracts those blocks
and returns the parsed actions plus the remaining conversational text.
"""

import re
import json
from typing import Any, Dict, List, Tuple


def _parse_json_flexible(raw: str) -> List[Dict[str, Any]]:
    """Parse JSON content that may be a single object, array, or NDJSON."""
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return data
        return [data]
    except json.JSONDecodeError:
        pass

    results = []
    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                results.append(obj)
        except json.JSONDecodeError:
            continue
    return results


def parse_negotiation_actions(content: str) -> Tuple[List[Dict[str, Any]], str]:
    """Extract ```negotiate JSON blocks from chatbox message content.

    Args:
        content: The raw chatbox message content string.

    Returns:
        A tuple of (actions, conversational_text) where:
        - actions: list of parsed action dicts, each with at minimum
          "action" and "sku_id" keys
        - conversational_text: the content with negotiate blocks stripped out
    """
    pattern = r"```negotiate\s*([\s\S]*?)```"
    matches = list(re.finditer(pattern, content))

    if not matches:
        return [], content

    actions = []
    for match in matches:
        raw_json = match.group(1).strip()
        parsed_objects = _parse_json_flexible(raw_json)

        for data in parsed_objects:
            if not isinstance(data, dict):
                continue

            action_type = str(data.get("action", "")).lower()
            if action_type not in ("offer", "accept", "reject"):
                continue

            sku_id = str(data.get("sku_id", "")).strip().lower()
            if not sku_id:
                continue

            parsed = {"action": action_type, "sku_id": sku_id}

            if action_type == "offer":
                try:
                    parsed["price"] = float(data["price"])
                except (KeyError, TypeError, ValueError):
                    continue

            if action_type == "accept":
                try:
                    parsed["price"] = float(data["price"])
                except (KeyError, TypeError, ValueError):
                    parsed["price"] = None

            if "quantity" in data:
                try:
                    parsed["quantity"] = int(data["quantity"])
                except (TypeError, ValueError):
                    parsed["quantity"] = 1
            else:
                parsed["quantity"] = 1

            if "shipping_address" in data:
                parsed["shipping_address"] = str(data["shipping_address"])

            actions.append(parsed)

    conversational_text = re.sub(pattern, "", content).strip()

    return actions, conversational_text
