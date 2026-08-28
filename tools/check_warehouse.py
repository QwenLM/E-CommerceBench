from .base_ecommerce_tool import EcommerceBaseTool, register_tool


@register_tool("check_warehouse")
class CheckWarehouse(EcommerceBaseTool):

    @staticmethod
    def invoke(env, **kwargs):
        env.advance_minutes(10, reason="check_warehouse")
        return env.get_warehouse()

    @staticmethod
    def get_info():
        return {
            "type": "function",
            "function": {
                "name": "check_warehouse",
                "description": """Check current warehouse inventory. Shows all products in storage with quantities and purchase prices.""",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
        }
