from .base_ecommerce_tool import EcommerceBaseTool, register_tool


@register_tool("trace_return_sources")
class TraceReturnSources(EcommerceBaseTool):

    @staticmethod
    def invoke(env, **kwargs):
        env.advance_minutes(10, reason="trace_return_sources")
        return env.trace_return_sources(product_id=kwargs.get("product_id"))

    @staticmethod
    def get_info():
        return {
            "type": "function",
            "function": {
                "name": "trace_return_sources",
                "description": (
                    "Trace which suppliers a product's warehouse stock was "
                    "purchased from, shown next to that product's realized "
                    "sold/returned counts and its natural baseline return rate. "
                    "Use this to investigate high returns: a product whose "
                    "realized return rate runs far above its baseline, and whose "
                    "stock pool is dominated by one supplier, points to that "
                    "supplier as the likely cause (e.g. defective goods). "
                    "Note: inventory is pooled per product, so an individual "
                    "returned unit cannot be tied to one supplier lot — you must "
                    "infer the bad source from the delivery mix and return rate. "
                    "Omit product_id to scan all products that have had returns, "
                    "sorted by realized return rate (most suspect first)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "product_id": {
                            "type": "string",
                            "description": "Product (SKU) ID to trace. If omitted, returns all products with returns.",
                        },
                    },
                    "required": [],
                },
            },
        }
