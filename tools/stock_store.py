from .base_ecommerce_tool import EcommerceBaseTool, register_tool


@register_tool("publish_to_store")
class PublishToStore(EcommerceBaseTool):

    @staticmethod
    def invoke(env, **kwargs):
        env.advance_minutes(20, reason="publish_to_store")
        return env.publish_to_store(store_id=kwargs["store_id"], plan=kwargs["plan"])

    @staticmethod
    def get_info():
        return {
            "type": "function",
            "function": {
                "name": "publish_to_store",
                "description": """Publish (list) products on a store's online page with pricing. Products must be in the warehouse and their category must be allowed for the store type. Products remain physically in the warehouse (incurring storage fees) until they are sold and shipped.""",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "store_id": {
                            "type": "string",
                            "description": "Target store ID.",
                        },
                        "plan": {
                            "type": "array",
                            "description": "List of items: [{'product_id': '...', 'quantity': N, 'retail_price': P}, ...]",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "product_id": {"type": "string"},
                                    "quantity": {"type": "integer"},
                                    "retail_price": {"type": "number"},
                                },
                                "required": ["product_id", "quantity", "retail_price"],
                            },
                        },
                    },
                    "required": ["store_id", "plan"],
                },
            },
        }
