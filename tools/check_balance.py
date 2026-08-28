from .base_ecommerce_tool import EcommerceBaseTool, register_tool


@register_tool("check_balance")
class CheckBalance(EcommerceBaseTool):

    @staticmethod
    def invoke(env, **kwargs):
        env.advance_minutes(10, reason="check_balance")
        return env.get_balance()

    @staticmethod
    def get_info():
        return {
            "type": "function",
            "function": {
                "name": "check_balance",
                "description": """Check your financial status. Shows three buckets: (1) bank account — all costs are paid from here (operations/staffing cost, storage, shipping, procurement); (2) platform wallet — withdrawable sales revenue; (3) pending_settlement (escrow) — shipped sales revenue that becomes withdrawable to the wallet only after the settlement window. Also shows unshipped_sales_value and upcoming settlement dates. You must withdraw from wallet to bank to fund operations, so plan working capital around the settlement delay.""",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
        }
