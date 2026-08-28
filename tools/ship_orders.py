from .base_ecommerce_tool import EcommerceBaseTool, register_tool


@register_tool("ship_orders")
class ShipOrders(EcommerceBaseTool):

    @staticmethod
    def invoke(env, **kwargs):
        env.advance_minutes(20, reason="ship_orders")
        action = kwargs.get("action", "ship")
        if action == "list":
            return env.get_pending_shipments()
        shipment_ids = kwargs.get("shipment_ids")
        speed = kwargs.get("speed", "standard")
        return env.ship_orders(shipment_ids=shipment_ids, speed=speed)

    @staticmethod
    def get_info():
        return {
            "type": "function",
            "function": {
                "name": "ship_orders",
                "description": (
                    "Fulfil customer orders. When customers buy, the order does NOT "
                    "auto-ship — it waits in a pending-shipment queue and you must ship "
                    "it. Shipping does three things: (1) charges shipping cost from your "
                    "bank NOW, (2) moves the sale's net revenue into 'pending_settlement' "
                    "(escrow) which becomes withdrawable from your wallet after the "
                    "settlement window, (3) determines how many units get returned.\n\n"
                    "Speed tiers trade cost against returns:\n"
                    "- fast: 2x shipping cost, fewest returns (best for high-return / "
                    "high-value categories like fashion)\n"
                    "- standard: normal cost and returns (default)\n"
                    "- slow: half shipping cost, but more returns\n\n"
                    "IMPORTANT: orders not shipped within the deadline are CANCELLED — "
                    "lost sale plus a reputation hit. Ship promptly.\n\n"
                    "Use action='list' to see pending shipments and their deadlines."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["ship", "list"],
                            "description": "'ship' to ship orders (default), 'list' to view the pending-shipment queue.",
                        },
                        "shipment_ids": {
                            "type": "array",
                            "description": "Optional list of shipment_id integers to ship. If omitted, ALL pending shipments are shipped.",
                            "items": {"type": "integer"},
                        },
                        "speed": {
                            "type": "string",
                            "enum": ["fast", "standard", "slow"],
                            "description": "Shipping speed (default 'standard'). Faster = costs more but fewer returns.",
                        },
                    },
                    "required": [],
                },
            },
        }
