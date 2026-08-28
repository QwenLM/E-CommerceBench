from .base_ecommerce_tool import EcommerceBaseTool, register_tool


@register_tool("market_search")
class MarketSearch(EcommerceBaseTool):

    @staticmethod
    def invoke(env, **kwargs):
        env.advance_minutes(30, reason="market_search")
        return env.market_search(
            store_type=kwargs.get("store_type"), category=kwargs.get("category")
        )

    @staticmethod
    def get_info():
        return {
            "type": "function",
            "function": {
                "name": "market_search",
                "description": (
                    "Progressive market research, disclosed in 3 levels so you can scan broadly "
                    "then drill in:\n"
                    "1. Call with NO arguments -> overview of ALL store types: each store's tier, "
                    "qualitative PROFIT-POTENTIAL rating (by tier), daily operating cost, seasonal sales index (Jan..Dec, baseline=100), advantage "
                    "axis, its qualitative STRENGTHS, CHALLENGES and OPERATING TIPS (a non-numeric "
                    "playbook of how to run it well), and the list of sub-categories it sells (names only).\n"
                    "2. Call with store_type only -> that store's strengths/challenges/operating tips "
                    "plus its sub-categories, each with a light card (reference-price range, product size).\n"
                    "3. Call with category (a sub-category name) -> the DEEP metrics for that one "
                    "sub-category: return-rate range, typical gross-margin range, typical monthly "
                    "sales range, and per-unit shipping/storage cost.\n"
                    "Use level 1 to compare store types before opening, level 2 to explore a "
                    "promising store, and level 3 to judge whether you can run a specific "
                    "sub-category well. The strengths/challenges/tips are qualitative guidance only "
                    "(no exact numbers); exact wholesale costs are still found via supplier negotiation."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "store_type": {
                            "type": "string",
                            "description": "Store type id (e.g. 'beauty'). With no category, returns the store's sub-categories.",
                        },
                        "category": {
                            "type": "string",
                            "description": "A sub-category name (e.g. 'Skincare & Beauty'). Returns that sub-category's detailed return-rate / margin / sales metrics.",
                        },
                    },
                    "required": [],
                },
            },
        }
