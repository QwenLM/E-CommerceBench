from .base_ecommerce_tool import EcommerceBaseTool, register_tool


@register_tool("set_prices")
class SetPrices(EcommerceBaseTool):

    @staticmethod
    def invoke(env, **kwargs):
        env.advance_minutes(10, reason="set_prices")
        return env.set_prices(store_id=kwargs["store_id"], prices=kwargs["prices"])

    @staticmethod
    def get_info():
        return {
            "type": "function",
            "function": {
                "name": "set_prices",
                "description": """Update retail prices for products already on a store's shelf.""",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "store_id": {"type": "string", "description": "Store ID."},
                        "prices": {
                            "type": "array",
                            "description": "List of price updates: [{'product_id': '...', 'price': P}, ...]",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "product_id": {"type": "string"},
                                    "price": {"type": "number"},
                                },
                                "required": ["product_id", "price"],
                            },
                        },
                    },
                    "required": ["store_id", "prices"],
                },
            },
        }
