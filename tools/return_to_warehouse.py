from .base_ecommerce_tool import EcommerceBaseTool, register_tool


@register_tool("return_to_warehouse")
class ReturnToWarehouse(EcommerceBaseTool):

    @staticmethod
    def invoke(env, **kwargs):
        env.advance_minutes(15, reason="return_to_warehouse")
        return env.return_to_warehouse(
            store_id=kwargs["store_id"], items=kwargs["items"]
        )

    @staticmethod
    def get_info():
        return {
            "type": "function",
            "function": {
                "name": "return_to_warehouse",
                "description": """Move products from a store shelf back to the central warehouse.""",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "store_id": {"type": "string", "description": "Store ID."},
                        "items": {
                            "type": "array",
                            "description": "List: [{'product_id': '...', 'quantity': N}, ...]",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "product_id": {"type": "string"},
                                    "quantity": {"type": "integer"},
                                },
                                "required": ["product_id", "quantity"],
                            },
                        },
                    },
                    "required": ["store_id", "items"],
                },
            },
        }
