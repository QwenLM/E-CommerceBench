#!/usr/bin/env python3
"""Extract per-supplier chatbox conversations from messages JSONL files.

Saves the original JSON lines verbatim (preserving reasoning_content, content,
tool_calls, etc.). For each run, creates a subfolder with one JSONL file per
supplier containing:
  - assistant messages where the agent calls `chatbox` to that supplier
  - tool results carrying the supplier's reply (message == "message_sent" or
    "supplier_bankrupt"); the reply body is in the same result under
    "supplier_reply".

Note: the `chatbox` tool is synchronous — the supplier's reply is returned in
the SAME tool result as the agent's outgoing message, matched here by
tool_call_id. (Earlier versions used a separate `send_email`/`email_sent` tool
plus a distinct inbox message; that schema no longer exists.)
"""

import argparse
import json
import os
import re
import glob


def clean_for_json_parse(raw: str) -> str:
    """Strip <system_warning> tags so the inner JSON can be parsed."""
    return re.sub(
        r"<system_warning>.*?</system_warning>", "", raw, flags=re.DOTALL
    ).strip()


def extract_chatbox_conversations(lines: list[str]) -> dict[str, list[str]]:
    """Return {supplier_email: [raw_json_line, ...]} in message order.

    Keys are the supplier contact addresses (the `uid` of each chatbox
    call); values are the raw chatbox request/reply JSON lines for that supplier.
    """
    supplier_lines: dict[str, list[tuple[int, str]]] = {}  # supplier -> [(idx, line)]
    # Maps tool_call_id -> supplier contact, for matching the chatbox reply
    tcid_to_supplier: dict[str, str] = {}

    messages = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            messages.append((line, json.loads(line)))
        except json.JSONDecodeError:
            # A truncated / half-flushed line (common when a run is killed
            # mid-write) must not abort extraction of the whole file — skip it.
            continue

    for idx, (raw_line, msg) in enumerate(messages):
        role = msg.get("role", "")

        # --- Assistant calls chatbox ---
        if role == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                func = tc.get("function", {})
                if func.get("name") != "chatbox":
                    continue
                try:
                    args = json.loads(func["arguments"])
                except (json.JSONDecodeError, KeyError):
                    continue
                # New logs use `uid`; fall back to the legacy `to_email` key so
                # transcripts recorded before the rename still parse.
                supplier_uid = args.get("uid", "") or args.get("to_email", "")
                if not supplier_uid:
                    continue
                tc_id = tc.get("id", "")
                if tc_id:
                    tcid_to_supplier[tc_id] = supplier_uid
                supplier_lines.setdefault(supplier_uid, []).append((idx, raw_line))

        # --- Tool results ---
        if role == "tool":
            raw_content = msg.get("content", "")
            tc_id = msg.get("tool_call_id", "")
            cleaned = (
                clean_for_json_parse(raw_content)
                if isinstance(raw_content, str)
                else ""
            )

            try:
                data = (
                    json.loads(cleaned)
                    if cleaned
                    else (raw_content if isinstance(raw_content, dict) else None)
                )
            except json.JSONDecodeError:
                data = None

            if not isinstance(data, dict):
                continue

            # chatbox result — carries the supplier's reply in the same message.
            # Associate it with the supplier via the originating tool_call_id.
            if data.get("message") in ("message_sent", "supplier_bankrupt"):
                supplier = tcid_to_supplier.get(tc_id)
                if supplier:
                    supplier_lines.setdefault(supplier, []).append((idx, raw_line))
                continue

    # Sort by message index and return raw lines only
    result: dict[str, list[str]] = {}
    for supplier, items in supplier_lines.items():
        items.sort(key=lambda t: t[0])
        result[supplier] = [line for _, line in items]
    return result


def process_run(jsonl_path: str, output_dir: str):
    with open(jsonl_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    supplier_lines = extract_chatbox_conversations(lines)

    if not supplier_lines:
        print(f"  No chatbox conversations found in {os.path.basename(jsonl_path)}")
        return

    os.makedirs(output_dir, exist_ok=True)
    for supplier, raw_lines in sorted(supplier_lines.items()):
        safe_name = supplier.replace("/", "_").replace("\\", "_")
        out_path = os.path.join(output_dir, f"{safe_name}.jsonl")
        with open(out_path, "w", encoding="utf-8") as f:
            for line in raw_lines:
                f.write(line if line.endswith("\n") else line + "\n")
        print(f"  {safe_name}.jsonl  ({len(raw_lines)} messages)")


def main():
    parser = argparse.ArgumentParser(
        description="Extract per-supplier chatbox conversations from benchmark logs."
    )
    parser.add_argument(
        "log_dir", help="Path to the log directory (e.g. log/20260529_133115_gpt-5)"
    )
    parser.add_argument(
        "--output-dir",
        "-o",
        default=None,
        help="Output base directory. Defaults to <log_dir>/chatbox_conversations/",
    )
    args = parser.parse_args()

    log_dir = args.log_dir.rstrip("/")
    output_base = args.output_dir or os.path.join(log_dir, "chatbox")

    # Support both organized layout (trajectories/) and legacy flat layout
    jsonl_files = sorted(
        glob.glob(os.path.join(log_dir, "trajectories", "run_*_messages.jsonl"))
    )
    if not jsonl_files:
        jsonl_files = sorted(glob.glob(os.path.join(log_dir, "run_*_messages.jsonl")))
    if not jsonl_files:
        print(f"No run_*_messages.jsonl files found in {log_dir}")
        return

    print(f"Found {len(jsonl_files)} run(s) in {log_dir}")
    for jsonl_path in jsonl_files:
        basename = os.path.basename(jsonl_path)
        run_name = basename.replace("_messages.jsonl", "")
        print(f"\n[{run_name}] {basename}")
        run_output = os.path.join(output_base, run_name)
        process_run(jsonl_path, run_output)

    print(f"\nDone. Output written to {output_base}/")


if __name__ == "__main__":
    main()
