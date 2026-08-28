from .base_ecommerce_tool import EcommerceBaseTool, register_tool


@register_tool("withdraw")
class Withdraw(EcommerceBaseTool):

    @staticmethod
    def invoke(env, **kwargs):
        env.advance_minutes(10, reason="withdraw")
        return env.withdraw(amount=kwargs.get("amount"))

    @staticmethod
    def get_info():
        return {
            "type": "function",
            "function": {
                "name": "withdraw",
                "description": """Transfer money from your platform wallet to your bank account. All costs (rent, procurement, storage) are paid from bank account, so you must withdraw regularly. Omit 'amount' (or pass 0) to withdraw the ENTIRE wallet balance. Requesting slightly more than the displayed balance is clamped to the available amount rather than rejected.""",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "amount": {
                            "type": "number",
                            "description": "Amount to withdraw from platform wallet to bank account (¥). Omit to withdraw the entire wallet balance.",
                        },
                    },
                    "required": [],
                },
            },
        }
