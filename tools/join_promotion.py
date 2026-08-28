from .base_ecommerce_tool import EcommerceBaseTool, register_tool


@register_tool("join_promotion")
class JoinPromotion(EcommerceBaseTool):

    @staticmethod
    def invoke(env, **kwargs):
        env.advance_minutes(10, reason="join_promotion")
        return env.join_promotion(
            store_id=kwargs["store_id"],
            event_name=kwargs["event_name"],
            discount_rate=kwargs["discount_rate"],
        )

    @staticmethod
    def get_info():
        return {
            "type": "function",
            "function": {
                "name": "join_promotion",
                "description": """Opt a store into an upcoming or active promotional event with a specified discount rate. Higher discounts attract more customers but reduce per-unit revenue.""",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "store_id": {"type": "string", "description": "Store ID."},
                        "event_name": {
                            "type": "string",
                            "description": "Name of the promotional event (e.g. 'Midyear Mega Sale').",
                        },
                        "discount_rate": {
                            "type": "number",
                            "description": "Discount rate between 0.05 and 0.50 (e.g. 0.20 = 20% off).",
                        },
                    },
                    "required": ["store_id", "event_name", "discount_rate"],
                },
            },
        }
