from .base_ecommerce_tool import EcommerceBaseTool, register_tool


@register_tool("close_store")
class CloseStore(EcommerceBaseTool):

    @staticmethod
    def invoke(env, **kwargs):
        env.advance_minutes(30, reason="close_store")
        return env.close_store(
            store_id=kwargs["store_id"],
            liquidate=bool(kwargs.get("liquidate", False)),
        )

    @staticmethod
    def get_info():
        return {
            "type": "function",
            "function": {
                "name": "close_store",
                "description": """Close an open store permanently. No closing fee. By default the remaining shelf inventory returns to your warehouse, where it KEEPS accruing daily storage fees (which rise with age) until you sell it through another store. Set liquidate=true to instead sell that inventory back at its salvage value (a fraction of what you paid for it), credited to your bank account, so the closed store's stock stops costing you storage — use this to cleanly exit an underperforming store.""",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "store_id": {
                            "type": "string",
                            "description": "ID of the store to close (e.g. 'store_001').",
                        },
                        "liquidate": {
                            "type": "boolean",
                            "description": "If true, sell the store's remaining inventory at salvage value (credited to bank) instead of moving it to the warehouse. Default false.",
                        },
                    },
                    "required": ["store_id"],
                },
            },
        }
