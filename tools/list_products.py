from .base_ecommerce_tool import EcommerceBaseTool, register_tool


@register_tool("list_products")
class ListProducts(EcommerceBaseTool):

    @staticmethod
    def invoke(env, **kwargs):
        env.advance_minutes(10, reason="list_products")
        return env.list_products(
            store_type=kwargs.get("store_type"), category=kwargs.get("category")
        )

    @staticmethod
    def get_info():
        return {
            "type": "function",
            "function": {
                "name": "list_products",
                "description": """List available products that can be purchased and sold. Filter by store type or category.""",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "store_type": {
                            "type": "string",
                            "description": "Filter by store type (e.g. 'beauty').",
                        },
                        "category": {
                            "type": "string",
                            "description": "Filter by category (e.g. 'Skincare & Beauty').",
                        },
                    },
                    "required": [],
                },
            },
        }
