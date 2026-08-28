from .base_ecommerce_tool import EcommerceBaseTool, register_tool


@register_tool("check_store_status")
class CheckStoreStatus(EcommerceBaseTool):

    @staticmethod
    def invoke(env, **kwargs):
        env.advance_minutes(10, reason="check_store_status")
        return env.get_store_status(store_id=kwargs.get("store_id"))

    @staticmethod
    def get_info():
        return {
            "type": "function",
            "function": {
                "name": "check_store_status",
                "description": """Check the status of a specific store (detailed financials) or all stores (summary). Shows yesterday's revenue, returns, shipping costs, and per-product sales data.""",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "store_id": {
                            "type": "string",
                            "description": "Store ID to check. If omitted, returns summary of all stores.",
                        },
                    },
                    "required": [],
                },
            },
        }
