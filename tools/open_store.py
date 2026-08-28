from .base_ecommerce_tool import EcommerceBaseTool, register_tool


@register_tool("open_store")
class OpenStore(EcommerceBaseTool):

    @staticmethod
    def invoke(env, **kwargs):
        env.advance_minutes(60, reason="open_store")
        return env.open_store(
            store_type=kwargs["store_type"], store_name=kwargs["store_name"]
        )

    @staticmethod
    def get_info():
        return {
            "type": "function",
            "function": {
                "name": "open_store",
                "description": """Open a new online store of a specified type.""",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "store_type": {
                            "type": "string",
                            "description": "Type of store to open (e.g. 'beauty', 'fashion', 'appliance_digital'). Use market_search to see available types.",
                        },
                        "store_name": {
                            "type": "string",
                            "description": "A name for your store.",
                        },
                    },
                    "required": ["store_type", "store_name"],
                },
            },
        }
