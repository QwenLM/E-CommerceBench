from typing import Any, Dict

from .base_ecommerce_tool import EcommerceBaseTool, register_tool

MAX_MEMO_COUNT = 20


@register_tool("operate_memory")
class OperateMemory(EcommerceBaseTool):
    @staticmethod
    def invoke(
        env, action: str = None, title: str = None, content: str = None, **_extra
    ) -> Dict[str, Any]:
        env.advance_minutes(10, reason="operate_memory")
        memos = env.memos

        # ERR-04: 'action' was a required positional arg, so a model that omitted
        # it triggered an uncaught TypeError that aborted the whole tool batch.
        # Return a clean validation error instead, consistent with the others.
        if not action:
            return {
                "error": "'action' is required. Use 'add', 'get', 'update', 'delete', or 'list'."
            }

        if action == "add":
            if not title or not content:
                return {"error": "Both 'title' and 'content' are required for add."}
            if title in memos:
                return {
                    "error": f"Memo with title '{title}' already exists. Use 'update' to modify it."
                }
            if len(memos) >= MAX_MEMO_COUNT:
                return {
                    "error": f"Memo limit reached ({MAX_MEMO_COUNT}). Delete a memo before adding a new one."
                }
            memos[title] = content
            return {
                "success": True,
                "message": f"Memo '{title}' added.",
                "total_memos": len(memos),
            }

        elif action == "update":
            if not title or not content:
                return {"error": "Both 'title' and 'content' are required for update."}
            if title not in memos:
                return {
                    "error": f"Memo with title '{title}' not found. Use 'add' to create it."
                }
            memos[title] = content
            return {"success": True, "message": f"Memo '{title}' updated."}

        elif action == "delete":
            if not title:
                return {"error": "'title' is required for delete."}
            if title not in memos:
                return {"error": f"Memo with title '{title}' not found."}
            del memos[title]
            return {
                "success": True,
                "message": f"Memo '{title}' deleted.",
                "total_memos": len(memos),
            }

        elif action == "get":
            # Retrieve memo content (F4). With a title, return that one memo; without
            # a title, return all memos so the agent can recover everything at once.
            if not title:
                return {"memos": dict(memos), "total_memos": len(memos)}
            if title not in memos:
                return {
                    "error": f"Memo with title '{title}' not found.",
                    "titles": list(memos.keys()),
                }
            return {"title": title, "content": memos[title]}

        elif action == "list":
            # Return full memo CONTENT, not just titles: memos exist precisely to be
            # read back after context clearing (F4). Bounded by MAX_MEMO_COUNT.
            return {"memos": dict(memos), "total_memos": len(memos)}

        else:
            return {
                "error": f"Unknown action '{action}'. Use 'add', 'get', 'update', 'delete', or 'list'."
            }

    @staticmethod
    def get_info() -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "operate_memory",
                "description": (
                    "Manage your persistent memos. Memos survive context window clearing. "
                    "Use this to save important information (supplier prices, negotiation results, "
                    "product performance, scam alerts, etc.) that you want to remember long-term. "
                    f"Maximum {MAX_MEMO_COUNT} memos allowed."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["add", "get", "update", "delete", "list"],
                            "description": "add: create new memo (title must not exist); get: read back memo content (with 'title' returns that memo, without 'title' returns all memos); update: modify existing memo; delete: remove memo; list: show all memos with their content",
                        },
                        "title": {
                            "type": "string",
                            "description": "Memo title (key). Required for add/update/delete; optional for get (omit to retrieve all memos).",
                        },
                        "content": {
                            "type": "string",
                            "description": "Memo content (value). Required for add/update.",
                        },
                    },
                    "required": ["action"],
                },
            },
        }
