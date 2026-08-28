from .base_ecommerce_tool import EcommerceBaseTool, register_tool


@register_tool("supplier_search")
class SupplierSearch(EcommerceBaseTool):

    @staticmethod
    def invoke(env, **kwargs):
        env.advance_minutes(10, reason="supplier_search")
        return env.supplier_search(
            product_name=kwargs.get("product_name"),
            category=kwargs.get("category"),
            store_type=kwargs.get("store_type"),
        )

    @staticmethod
    def get_info():
        return {
            "type": "function",
            "function": {
                "name": "supplier_search",
                "description": """Search for wholesale suppliers. Filter by product name, category, or store type. Contact suppliers via chatbox to negotiate prices and place orders.""",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "product_name": {
                            "type": "string",
                            "description": "Search by product name (partial match).",
                        },
                        "category": {
                            "type": "string",
                            "description": "Filter by category.",
                        },
                        "store_type": {
                            "type": "string",
                            "description": "Filter by store type.",
                        },
                    },
                    "required": [],
                },
            },
        }
