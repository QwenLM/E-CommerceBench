from .base_ecommerce_tool import EcommerceBaseTool, register_tool


@register_tool("wait_for_next_day")
class WaitForNextDay(EcommerceBaseTool):

    @staticmethod
    def invoke(env, **kwargs):
        return env.wait_for_next_day()

    @staticmethod
    def get_info():
        return {
            "type": "function",
            "function": {
                "name": "wait_for_next_day",
                "description": """Skip to the next business day (8:00 AM). Triggers daily processing: sales, returns, deliveries, events, fee deductions. You MUST pass `current_day` (the date you are on right now) on every call.""",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "current_day": {
                            "type": "string",
                            "description": "REQUIRED. The current in-simulation date you are advancing FROM, copied verbatim from the most recent tool result's `date` / `current_time` field (format YYYY-MM-DD, e.g. 2026-03-14). Pass the real current date each time — it changes every day, so consecutive calls are never identical.",
                        },
                    },
                    "required": ["current_day"],
                },
            },
        }
