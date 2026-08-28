import os
import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MAX_DAYS = 365


class EcommerceToolManager:
    """Standalone tool manager for E-Commerce Bench.

    Executes tools directly in Python, manages the Env instance,
    handles day advancement, balance tracking, and logging.
    """

    def __init__(self):
        self.env = None
        self._ecommerce_tool_map = {}
        self._output_log_fp = None
        self._balance_log_fp = None
        self._messages_log_fp = None
        self._seen_balance_dates = set()

    @classmethod
    def init(cls, job: Dict[str, Any]) -> "EcommerceToolManager":
        tool_manager = cls()

        run_index = job.get("agent_info", {}).get("run_index", 0)
        tool_manager._init_local_logging(run_index=run_index)
        for msg in job.get("messages", []):
            try:
                tool_manager._append_message_log(msg)
            except Exception:
                pass

        job.setdefault("agent_info", {})
        job["agent_info"].setdefault("context_clear_count", 0)
        job["agent_info"].setdefault("context_tokens_freed_total", 0)

        if "reward_meta" not in job:
            job["reward_meta"] = {}

        job["final_day"] = 1

        from tools import ecommerce_tool_map
        from tools.ecommerce_env import EcommerceEnv

        agent_info = job.get("agent_info", {})
        tool_manager.env = EcommerceEnv(
            initial_balance=agent_info.get("initial_balance", 100000.0),
            store_daily_rent=agent_info.get("daily_fee", 50.0),
            max_day=agent_info.get("max_day", None) or MAX_DAYS,
        )

        tool_manager._ecommerce_tool_map = ecommerce_tool_map

        if hasattr(tool_manager, "log_dir") and tool_manager.log_dir:
            chatbox_dir = os.path.join(
                tool_manager.log_dir, "chatbox", f"run_{run_index}"
            )
            os.makedirs(chatbox_dir, exist_ok=True)
            tool_manager.env.chatbox_log_dir = chatbox_dir

        # Record the Day 0 starting balance. Subsequent rows are written from the
        # daily_trigger handler, whose first trigger lands on the *next* morning
        # (e.g. 2026-01-02), so without this the start_date row would be missing.
        tool_manager._try_write_balance_from_env(
            tool_manager.env.start_time.strftime("%Y-%m-%d")
        )

        logger.info("EcommerceToolManager Env initialized.")

        return tool_manager

    # ------------------------------------------------------------------
    # Local logging helpers
    # ------------------------------------------------------------------
    def _init_local_logging(self, run_index: int = 0) -> None:
        if os.environ.get("ECOMMERCE_BENCH_LOG_DIR", ""):
            log_dir = os.path.abspath(os.environ.get("ECOMMERCE_BENCH_LOG_DIR"))
        else:
            log_dir = os.path.abspath(
                os.path.join(os.path.dirname(__file__), "..", "log")
            )
        os.makedirs(log_dir, exist_ok=True)

        self.log_dir = log_dir
        self.run_prefix = f"run_{run_index}"

        traj_dir = os.path.join(log_dir, "trajectories")
        self.metrics_dir = os.path.join(log_dir, "metrics")
        balance_dir = os.path.join(log_dir, "balance")
        for d in (traj_dir, self.metrics_dir, balance_dir):
            os.makedirs(d, exist_ok=True)

        self.local_output_log_path = os.path.join(
            traj_dir, f"{self.run_prefix}_output.log"
        )
        self.local_balance_log_path = os.path.join(
            balance_dir, f"{self.run_prefix}_daily_balance.csv"
        )
        self.local_messages_log_path = os.path.join(
            traj_dir, f"{self.run_prefix}_messages.jsonl"
        )

        self._output_log_fp = open(
            self.local_output_log_path, "a", encoding="utf-8", buffering=1
        )
        self._balance_log_fp = open(
            self.local_balance_log_path, "a", encoding="utf-8", buffering=1
        )
        self._messages_log_fp = open(
            self.local_messages_log_path, "a", encoding="utf-8", buffering=1
        )
        self._output_log_fp.write("[LOG] output log started\n")
        self._output_log_fp.flush()
        self._balance_log_fp.write(
            "date,bank_balance,platform_wallet,total_balance,open_stores,warehouse_items,storage_charged\n"
        )
        self._balance_log_fp.flush()
        self._seen_balance_dates = set()

        logger.info(f"Local output log: {self.local_output_log_path}")
        logger.info(f"Local balance log: {self.local_balance_log_path}")
        logger.info(f"Local messages log: {self.local_messages_log_path}")

    def _write_output_log(self, text: str) -> None:
        if not getattr(self, "_output_log_fp", None):
            return
        self._output_log_fp.write(text)
        if not text.endswith("\n"):
            self._output_log_fp.write("\n")
        self._output_log_fp.flush()

    def _append_message_log(self, message: Dict[str, Any]) -> None:
        if not getattr(self, "_messages_log_fp", None):
            return
        # Redact raw reasoning items (large opaque encrypted_content blobs kept
        # only for the API round-trip) so the log stays readable/small. The
        # human-readable reasoning summary lives in 'reasoning_content' and is
        # untouched.
        if isinstance(message, dict) and message.get("reasoning_items"):
            message = {
                **{k: v for k, v in message.items() if k != "reasoning_items"},
                "_reasoning_items_n": len(message["reasoning_items"]),
            }
        self._messages_log_fp.write(json.dumps(message, ensure_ascii=False) + "\n")
        self._messages_log_fp.flush()

    def _write_balance_row(self, data: Dict[str, Any]) -> None:
        if not getattr(self, "_balance_log_fp", None):
            return
        date_val = data.get("date")
        if not date_val or date_val in self._seen_balance_dates:
            return
        self._seen_balance_dates.add(date_val)
        bank = data.get("bank_balance", 0)
        wallet = data.get("platform_wallet", 0)
        total = data.get("total_balance", 0)
        open_stores = data.get("open_stores", 0)
        wh_items = data.get("warehouse_items", 0)
        storage = data.get("storage_charged", 0)
        self._balance_log_fp.write(
            f"{date_val},{bank},{wallet},{total},{open_stores},{wh_items},{storage}\n"
        )
        self._balance_log_fp.flush()

    def _try_write_balance_from_env(self, date_val: str) -> None:
        if not getattr(self, "_balance_log_fp", None):
            return
        if not date_val or date_val in self._seen_balance_dates:
            return
        env = getattr(self, "env", None)
        if env is None:
            return
        bank = round(env.bank_balance, 2)
        wallet = round(getattr(env, "platform_wallet", 0.0), 2)
        escrow = round(getattr(env, "pending_settlement", 0.0), 2)
        total = round(bank + wallet + escrow, 2)
        open_stores = (
            sum(1 for s in env.stores.values() if s.is_open)
            if hasattr(env, "stores")
            else 0
        )
        wh_items = (
            sum(lot[0] for lots in env.warehouse_lots.values() for lot in lots)
            if hasattr(env, "warehouse_lots")
            else 0
        )
        payload = {
            "date": date_val,
            "bank_balance": bank,
            "platform_wallet": wallet,
            "total_balance": total,
            "open_stores": open_stores,
            "warehouse_items": wh_items,
            "storage_charged": getattr(env, "_last_storage_charged", 0),
        }
        self._write_balance_row(payload)

    def snapshot_final_state(self, job: Dict[str, Any]) -> Optional[float]:
        if "final_state" in job:
            return job.get("final_state")
        env = getattr(self, "env", None)
        if env is None:
            return None
        # Run the deferred pipeline before snapshotting. When the episode ends
        # via env-driven termination (max_days/bankruptcy) _check_done already
        # ran it; but a max_turns_reached termination never reaches _check_done,
        # so without this the escrow would be counted at gross while the seeded
        # (but not-yet-arrived) returns owed against it were never deducted,
        # over-stating final_state and letting a buzzer fire-sale dodge returns.
        # _finalize_pipeline is idempotent, so the env-driven path is unaffected.
        if hasattr(env, "_finalize_pipeline"):
            try:
                env._finalize_pipeline()
            except Exception:
                logger.exception("final pipeline finalize failed")
        final_state_a = round(
            env.bank_balance
            + getattr(env, "platform_wallet", 0.0)
            + getattr(env, "pending_settlement", 0.0),
            2,
        )
        job["final_state"] = final_state_a
        return final_state_a

    def _log_tool_calls(
        self, tool_call_infos: List[Any], tool_responses: List[str]
    ) -> None:
        if not getattr(self, "_output_log_fp", None):
            return
        for info, resp in zip(tool_call_infos, tool_responses):
            name = info.get("tool_name")
            args = info.get("tool_args")
            self._write_output_log(
                f"[TOOL_CALL] {name} args={json.dumps(args, ensure_ascii=False)}"
            )
            self._write_output_log(f"[TOOL_RESP] {name} resp={resp}")

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------
    @staticmethod
    def _inject_into_response(resp_str: str, key: str, value: Any) -> str:
        """Attach an out-of-band notification as a STRUCTURED field inside the
        tool response, instead of string-concatenating raw text after the
        closing brace of a JSON object (which produced invalid JSON and
        misattributed day-events). If resp_str is a JSON object we add the key
        and re-serialize so the result stays parseable; otherwise we fall back
        to a clearly-delimited out-of-band block the prompt documents."""
        try:
            obj = json.loads(resp_str)
            if isinstance(obj, dict):
                obj[key] = value
                return json.dumps(obj, ensure_ascii=False)
        except (json.JSONDecodeError, TypeError):
            pass
        # Non-JSON (or non-object) response: keep it readable but separated.
        return f"{resp_str}\n<{key}>{json.dumps(value, ensure_ascii=False)}</{key}>"

    def ask_code_exec(
        self, job: Dict[str, Any], tool_call_infos: List[Any]
    ) -> List[str]:
        """Execute a sequence of tool calls and update environment state."""
        num_calls = len(tool_call_infos)
        tool_responses: List[str] = [
            json.dumps({"error": "No output from executor"}, ensure_ascii=False)
            for _ in range(num_calls)
        ]

        skip_next_turn = False
        has_critical_error = False
        clean_traceback = ""
        wait_idx = None  # index of the first wait_for_next_day call this batch (for news attribution)

        env = getattr(self, "env", None)
        if env is None:
            err = json.dumps(
                {"error": "Environment not initialized"}, ensure_ascii=False
            )
            return [err for _ in range(num_calls)]

        # F11: snapshot the day before running this batch so step 2 does not
        # double-advance. A tool that crosses 6 PM fast-forwards into the next day
        # (firing that day's trigger) on its own; if that happens, a
        # wait_for_next_day in the same batch must not advance a second time.
        day_before = getattr(env, "day_count", None)
        date_before = env.current_time.date()

        # 1. Sequentially execute tool calls
        wait_seen = False
        for idx, call in enumerate(tool_call_infos):
            t_name = call["tool_name"]
            t_args = call["tool_args"]

            if t_name == "wait_for_next_day":
                # DSC-06: a batch advances the simulation exactly ONE day no
                # matter how many wait_for_next_day calls it contains (the env is
                # advanced once below). Only the first wait gets the day-advance
                # result; any duplicates in the same batch are wasted calls and
                # are told so explicitly instead of being handed an identical
                # (misleading) "next day" payload.
                if not wait_seen:
                    wait_seen = True
                    skip_next_turn = True
                    wait_idx = idx
                    tool_responses[idx] = "PENDING_ENV_UPDATE"
                else:
                    tool_responses[idx] = json.dumps(
                        {
                            "note": "Duplicate wait_for_next_day in the same batch "
                            "was ignored — time advances only one day per "
                            "turn. Issue one wait per turn."
                        },
                        ensure_ascii=False,
                    )
                continue

            tool_cls = self._ecommerce_tool_map.get(t_name)
            if tool_cls is None:
                tool_responses[idx] = json.dumps(
                    {"error": f"Unknown tool: {t_name}"}, ensure_ascii=False
                )
                continue

            try:
                result = tool_cls.invoke(env, **t_args)
                # Inject the current simulation time into every tool response so
                # the agent always knows "what date/time is it now" without having
                # to call wait_for_next_day just to check. Time advances inside
                # each tool (advance_minutes), so reading it AFTER invoke captures
                # the post-advance moment.
                if isinstance(result, dict):
                    result["current_time"] = env.current_time.strftime("%Y-%m-%d %H:%M")
                if isinstance(result, str):
                    tool_responses[idx] = result
                else:
                    tool_responses[idx] = json.dumps(result, ensure_ascii=False)
            except (KeyError, ValueError, TypeError) as e:
                # Malformed tool arguments (a required key the LLM omitted, a
                # non-numeric quantity/price, a wrong arg type) are a LOCAL error
                # for THIS call only. They must NOT trip has_critical_error,
                # which would skip the day's env advance and fail every other
                # call queued in the same batch. Report the bad call and keep
                # going with the rest of the batch.
                logger.warning(f"Tool arg error at step {idx} ({t_name}): {e}")
                tool_responses[idx] = json.dumps(
                    {"error": f"Invalid arguments for {t_name}: {str(e)}"},
                    ensure_ascii=False,
                )
                self._write_output_log(f"arg error {t_name}: {e}")
                continue
            except Exception as e:
                logger.error(f"Tool error at step {idx}: {e}")
                has_critical_error = True
                clean_traceback = str(e)
                tool_responses[idx] = json.dumps(
                    {"error": f"Tool execution exception: {str(e)}"}, ensure_ascii=False
                )
                self._write_output_log(str(e))
                break

        # 2. Environment update
        env_res: Dict[str, Any] = {}
        if not has_critical_error:
            try:
                day_already_advanced = (
                    day_before is not None
                    and getattr(env, "day_count", day_before) != day_before
                ) or env.current_time.date() != date_before
                if (
                    skip_next_turn
                    and not day_already_advanced
                    and not getattr(env, "is_done", False)
                ):
                    env_res = env.wait_for_next_day()
                else:
                    # No wait requested, OR a batch tool already crossed into the
                    # next day (a second advance would skip a working day and
                    # double-charge the economy), OR the episode already ended
                    # (don't run the economy past finalize). (F11)
                    env_res = env.next_turn()
            except Exception as e:
                logger.error(f"Env update error: {e}")
                has_critical_error = True
                clean_traceback = str(e)
                self._write_output_log(str(e))

        # 3. Error propagation
        if has_critical_error:
            # Only overwrite the UNFILLED placeholders: the executor default
            # ('No output from executor') and the wait sentinel
            # ('PENDING_ENV_UPDATE'). Use exact equality, NOT substring `in`:
            # a legitimate successful tool response whose JSON happens to contain
            # the substring 'PENDING' or 'No output' (e.g. a shipment/order
            # status field) must not be clobbered with a generic failure.
            _default_placeholder = json.dumps(
                {"error": "No output from executor"}, ensure_ascii=False
            )
            for i in range(num_calls):
                if tool_responses[i] in ("PENDING_ENV_UPDATE", _default_placeholder):
                    tool_responses[i] = json.dumps(
                        {"error": "Tool execution failed", "detail": clean_traceback},
                        ensure_ascii=False,
                    )

        # 4. Fill wait_for_next_day placeholders
        if not has_critical_error and env_res:
            agent_visible_res = dict(env_res)
            agent_visible_res.pop("events", None)
            env_res_str = json.dumps(agent_visible_res, ensure_ascii=False)
            for i in range(num_calls):
                if tool_responses[i] == "PENDING_ENV_UPDATE":
                    tool_responses[i] = env_res_str

        # 4b. Safety net: the PENDING_ENV_UPDATE sentinel must NEVER reach the
        # agent verbatim. On the terminating day (horizon end / bankruptcy via a
        # wait) env_res can be empty/falsy after popping 'events', so step 4 is
        # skipped and the sentinel would survive. Replace any remaining sentinel
        # with a minimal, parseable status payload.
        for i in range(num_calls):
            if tool_responses[i] == "PENDING_ENV_UPDATE":
                fallback = {"current_time": env.current_time.strftime("%Y-%m-%d %H:%M")}
                if env is not None and getattr(env, "is_done", False):
                    fallback["note"] = (
                        "Simulation has ended; no further days to advance."
                    )
                    if getattr(env, "termination_reason", None):
                        fallback["termination_reason"] = env.termination_reason
                tool_responses[i] = json.dumps(fallback, ensure_ascii=False)

        # 5. Environment state sync and notification injection.
        # Drain ALL events buffered since the last tool batch. This captures
        # day-crossing triggers/terminations produced by advance_minutes inside
        # normal tools (not just wait_for_next_day), which were previously lost.
        #
        # News is collected per-day into a structured list and injected as a
        # JSON field (see _inject_into_response) rather than string-concatenated
        # after a response's closing brace. It is attributed to the
        # wait_for_next_day call that advanced time when present, else to the
        # last response — never stapled onto an arbitrary unrelated tool.
        day_notices = []  # one entry per crossed day: {date, day, news:[...]}
        try:
            events = env.drain_events()
            if events:
                logger.info(f"Draining {len(events)} buffered env event(s)")

                for event in events:
                    if event.get("type") == "daily_trigger":
                        summary = event.get("summary", {})
                        trigger_date = summary.get("date")
                        if trigger_date and trigger_date != job.get(
                            "last_trigger_date"
                        ):
                            job["last_trigger_date"] = trigger_date
                            job["final_day"] = summary.get(
                                "day", job.get("final_day", 0) + 1
                            )
                            logger.info(
                                f'Day Update: day={job["final_day"]}, date={trigger_date}'
                            )
                            self._try_write_balance_from_env(trigger_date)

                        news = summary.get("news", [])
                        news_items = [
                            {
                                "type": n.get("type", "info"),
                                "content": n.get("content", "")[:200],
                            }
                            for n in news
                            if n.get("content")
                        ]
                        if news_items:
                            day_notices.append(
                                {
                                    "date": trigger_date,
                                    "day": summary.get("day"),
                                    "news": news_items,
                                }
                            )

                    elif event.get("type") == "termination":
                        info = event.get("info", {})
                        reason = info.get("reason", "env_terminated")
                        job["termination_reason"] = reason
                        # Compute final_state through the single authoritative
                        # path (snapshot_final_state runs the idempotent
                        # _finalize_pipeline and caches job['final_state']). Do NOT
                        # recompute bank+wallet inline here — that skipped finalize
                        # and could disagree with the snapshot on the terminating day.
                        try:
                            self.snapshot_final_state(job)
                        except Exception:
                            logger.exception(
                                "snapshot_final_state on termination failed"
                            )
                        logger.info(f"Termination detected: {reason}")
        except Exception as e:
            logger.error(f"Environment notify error: {e}")

        # 5b. Attribute and inject the day notices as a structured field.
        try:
            if day_notices and tool_responses:
                target = (
                    wait_idx
                    if (wait_idx is not None and wait_idx < len(tool_responses))
                    else len(tool_responses) - 1
                )
                tool_responses[target] = self._inject_into_response(
                    tool_responses[target], "system_notifications", day_notices
                )
        except Exception as e:
            logger.error(f"News injection error: {e}")

        # 6. Balance reminder — also injected as a structured field on the same
        # attributed response, not concatenated as raw text.
        try:
            if env is not None and tool_responses:
                # Use the ACTUAL upcoming daily operations cost across all open
                # stores (tier-based 80/120/160 each, full cost per store, no
                # multi-store discount), not the stale legacy flat rent (50.0). With no open
                # stores yet, fall back to the cheapest single-store ops cost so
                # the agent is still warned before it can no longer even operate.
                if hasattr(env, "current_daily_ops_cost"):
                    daily_fee = env.current_daily_ops_cost()
                    if daily_fee <= 0:
                        tiers = list(getattr(env, "ops_cost_per_day", {}).values())
                        daily_fee = min(tiers) if tiers else 80.0
                else:
                    daily_fee = getattr(env, "store_daily_rent", 50.0)
                if env.bank_balance < daily_fee:
                    target = (
                        wait_idx
                        if (wait_idx is not None and wait_idx < len(tool_responses))
                        else len(tool_responses) - 1
                    )
                    reminder = (
                        f"Your current balance (¥{env.bank_balance:.2f}) is below the "
                        f"daily operating cost (¥{daily_fee:.2f}). If your bank account stays "
                        f"negative for 10 consecutive days, you will go bankrupt."
                    )
                    tool_responses[target] = self._inject_into_response(
                        tool_responses[target], "balance_reminder", reminder
                    )
        except Exception as e:
            logger.error(f"Balance reminder error: {e}")

        # 8. Note: time advancement past 18:00 is handled inside the env's
        # _advance_to (it auto-fast-forwards to the next 8 AM), and all
        # resulting daily_trigger/termination events are drained in step 5
        # above. No separate curfew handling is needed here.

        try:
            self._log_tool_calls(tool_call_infos, tool_responses)
        except Exception:
            pass

        return tool_responses

    # ------------------------------------------------------------------
    # Cleanup / reward
    # ------------------------------------------------------------------
    def _save_negotiation_metrics(self, negotiation_metrics: Dict[str, Any]) -> None:
        if not getattr(self, "metrics_dir", None):
            return
        metrics_path = os.path.join(
            self.metrics_dir, f"{self.run_prefix}_negotiation_metrics.json"
        )
        try:
            with open(metrics_path, "w", encoding="utf-8") as f:
                json.dump(
                    negotiation_metrics, f, indent=2, ensure_ascii=False, default=str
                )
            logger.info(f"Negotiation metrics saved: {metrics_path}")
        except Exception as e:
            logger.warning(f"Failed to save negotiation metrics: {e}")

    def _save_analysis_report(self, job: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """D2: build and persist the post-hoc capability analysis panel
        (profitability, negotiation quality, fraud identification, fulfilment)
        so test-time analysis sees more than balance + chatbox."""
        env = getattr(self, "env", None)
        if env is None or not hasattr(env, "get_analysis_report"):
            return None
        try:
            report = env.get_analysis_report()
        except Exception as e:
            logger.warning(f"Failed to build analysis report: {e}")
            return None
        report["reward"] = {
            "final_score": job.get("reward_meta", {}).get("final_score"),
            "reward_res": job.get("reward_res"),
            "termination_reason": job.get("termination_reason"),
            # The real, un-normalized termination cause (set by
            # EcommerceBenchAgent._normalize_termination_reason before cleanup),
            # e.g. the actual 'llm_error: <exception>' string. Persisting it here
            # makes a truncated/failed episode diagnosable straight from
            # analysis.json instead of only on the (unsaved) console. None if the
            # job was never normalized (defensive: never raises).
            "termination_detail": job.get("termination_detail"),
        }
        job.setdefault("reward_meta", {})["analysis"] = report
        if getattr(self, "metrics_dir", None):
            path = os.path.join(self.metrics_dir, f"{self.run_prefix}_analysis.json")
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(report, f, indent=2, ensure_ascii=False, default=str)
                logger.info(f"Analysis report saved: {path}")
            except Exception as e:
                logger.warning(f"Failed to save analysis report: {e}")
        self._log_analysis_summary(report)
        return report

    @staticmethod
    def _format_learning_line(ls) -> str:
        if not ls or not isinstance(ls, dict):
            return "[Learning] N/A (insufficient data)"
        return (
            f"[Learning] se_lift={ls.get('se_half_lift')} "
            f"agr_lift={ls.get('agr_half_lift')} "
            f"fraud_avoid={ls.get('fraud_avoidance_lift')} "
            f"zero_bad={ls.get('time_to_zero_bad')} "
            f"rounds_imp={ls.get('rounds_half_improvement')}"
        )

    def _log_analysis_summary(self, report: Dict[str, Any]) -> None:
        """Emit a compact human-readable summary to the output log."""
        try:
            p = report.get("profitability", {})
            nq = report.get("negotiation_quality", {})
            fr = report.get("fraud_identification", {})
            rm = report.get("return_management", {})
            fu = report.get("fulfilment_quality", {})
            # Termination cause: surface BOTH the canonical reason and the real
            # un-normalized detail (e.g. 'llm_error: <exception>'). The detail can
            # contain newlines (exception text), so flatten it to one physical
            # line — otherwise it would split the '\n'.join'd block below.
            rw = report.get("reward", {}) or {}
            term_detail = rw.get("termination_detail")
            term_detail_str = (
                str(term_detail).replace("\n", " ").replace("\r", " ")
                if term_detail is not None
                else "N/A"
            )
            # Per-fraud-type spend one-liner (how much money each scam took).
            bt = fr.get("spend_by_fraud_type", {}) or {}
            ftype_bits = []
            for ft, b in bt.items():
                ftype_bits.append(
                    f"{ft}:¥{b.get('spend',0)}({b.get('orders',0)}ord/{b.get('units',0)}u)"
                )
            # Good-supplier spend by personality.
            sbp = fr.get("spend_by_good_personality", {}) or {}
            pers_bits = [f"{p}:¥{v}" for p, v in sbp.items()]
            # Distinct suppliers engaged (contacted vs ordered), by type.
            se = report.get("supplier_engagement", {}) or {}
            se_con = se.get("contacted", {}) or {}
            se_ord = se.get("ordered", {}) or {}
            lines = [
                "===== RUN ANALYSIS =====",
                f"[Termination] reason={rw.get('termination_reason')} "
                f"detail={term_detail_str} score={rw.get('final_score')}",
                f"[Profit] final=¥{p.get('final_balance')} bankrupt={p.get('bankrupt')} "
                f"day={p.get('final_day')} drawdown=¥{p.get('peak_drawdown')} "
                f"stores={p.get('stores_opened')} reopens={p.get('store_reopens')}",
                f"[Negotiation] SE+={nq.get('SE+')} CSE+={nq.get('CSE+')} "
                f"%Oracle={nq.get('%Oracle')} AGR+={nq.get('AGR+')} "
                f"avg_rounds={nq.get('avg_rounds_to_deal')} saved=¥{nq.get('total_money_saved_vs_initial')}",
                self._format_learning_line(nq.get("learning_speed")),
                f"[Fraud-ID] bad_order_share={fr.get('bad_supplier_order_share')} "
                f"spend_on_bad=¥{fr.get('spend_on_bad_supplier')} "
                f"(share={fr.get('spend_on_bad_supplier_share')}) "
                f"vip_fee_paid={fr.get('vip_fee_paid_count')}x¥{fr.get('vip_fee_paid_amount')}",
                "[Spend-by-fraud] " + "  ".join(ftype_bits),
                "[Spend-by-good-personality] " + "  ".join(pers_bits),
                f"[Suppliers] contacted={se_con.get('distinct_total')}"
                f"(good={se_con.get('distinct_good')}/bad={se_con.get('distinct_bad')}) "
                f"ordered={se_ord.get('distinct_total')}"
                f"(good={se_ord.get('distinct_good')}/bad={se_ord.get('distinct_bad')}) "
                f"ordered_bad_by_fraud={se_ord.get('bad_by_fraud_type')}",
                f"[Return-Mgmt] realized={rm.get('realized_return_rate')} exp={rm.get('exp_return_rate')} "
                f"(natural={rm.get('exp_return_rate_natural')} pricing={rm.get('exp_return_rate_from_pricing')} "
                f"ship_speed={rm.get('exp_return_rate_from_ship_speed')} defective_fraud={rm.get('exp_return_rate_from_defective_fraud')}) "
                f"controllable={rm.get('controllable_return_rate')} refund_loss=¥{rm.get('refund_loss_total')}",
                f"[Fulfilment] on_time={fu.get('on_time_ship_rate')} cancelled={fu.get('orders_cancelled')} "
                f"return_rate={fu.get('realized_return_rate')} speeds={fu.get('ship_speed_counts')}",
                "========================",
            ]
            self._write_output_log("\n".join(lines))
        except Exception:
            pass

    def cleanup(self, job: Dict[str, Any]) -> None:
        termination_reason = job.get("termination_reason")

        # By the time cleanup runs, the agent loop has normalized the reason to
        # one of the two canonical terminal states (see
        # EcommerceBenchAgent._normalize_termination_reason): every episode is
        # either 'env_completed' (full horizon) or 'env_terminated' (ended
        # early). The env's native 'max_days_reached'/'bankruptcy' aliases are
        # kept here as a defensive fallback in case cleanup is ever invoked on a
        # non-normalized job.
        SCORED_REASONS = (
            "env_completed",
            "env_terminated",
            "max_days_reached",
            "bankruptcy",
        )

        if termination_reason not in SCORED_REASONS:
            job["reward_res"] = termination_reason
            job["reward_meta"] = {}
            if hasattr(self, "env") and self.env is not None:
                negotiation_metrics = self.env.get_negotiation_report()
                job["reward_meta"]["negotiation_metrics"] = negotiation_metrics
                self._save_negotiation_metrics(negotiation_metrics)
                self._save_analysis_report(job)
            self._save_session_summary()
            return

        final_a = job.get("final_state", 0.0)
        final_day = job.get("final_day", 1)
        initial_a = job.get("agent_info", {}).get("initial_balance", 100000.0)

        # Reward is simply final / initial total balance (no baseline term).
        reward = final_a / initial_a if initial_a else 0.0

        negotiation_metrics = {}
        if hasattr(self, "env") and self.env is not None:
            negotiation_metrics = self.env.get_negotiation_report()

        job["reward_meta"].update(
            {
                "final_a": final_a,
                "initial": initial_a,
                "final_day": final_day,
                "final_score": reward,
                "negotiation_metrics": negotiation_metrics,
            }
        )
        self._save_negotiation_metrics(negotiation_metrics)
        logger.info(
            f"Reward: Initial={initial_a}, "
            f"Final={final_a}, Day={final_day}, Reward={reward:.4f}"
        )
        job["reward_res"] = (
            f"[Final A: {final_a}, " f"Final Day: {final_day}, Reward: {reward:.4f}]"
        )
        if hasattr(self, "env") and self.env is not None:
            self._save_analysis_report(job)
        self._save_session_summary()

    def _save_session_summary(self) -> None:
        """Aggregate mean ± std of key metrics across all runs into summary.json."""
        metrics_dir = getattr(self, "metrics_dir", None)
        log_dir = getattr(self, "log_dir", None)
        if not metrics_dir or not log_dir:
            return
        import glob as _glob
        import math

        analysis_files = sorted(
            _glob.glob(os.path.join(metrics_dir, "run_*_analysis.json"))
        )
        if not analysis_files:
            return
        reports = []
        for af in analysis_files:
            try:
                with open(af, encoding="utf-8") as f:
                    reports.append(json.load(f))
            except Exception:
                continue
        if not reports:
            return

        def _extract(report, *keys):
            d = report
            for k in keys:
                if not isinstance(d, dict):
                    return None
                d = d.get(k)
            return d if d is not None else None

        def _agg(values):
            nums = [v for v in values if isinstance(v, (int, float))]
            if not nums:
                return None
            m = sum(nums) / len(nums)
            if len(nums) < 2:
                return {"mean": round(m, 4), "std": 0.0, "n": len(nums)}
            var = sum((x - m) ** 2 for x in nums) / (len(nums) - 1)
            return {
                "mean": round(m, 4),
                "std": round(math.sqrt(var), 4),
                "n": len(nums),
            }

        metric_paths = [
            ("final_balance", "profitability", "final_balance"),
            ("final_day", "profitability", "final_day"),
            ("SE+", "negotiation_quality", "SE+"),
            ("CSE+", "negotiation_quality", "CSE+"),
            ("%Oracle", "negotiation_quality", "%Oracle"),
            ("AGR+", "negotiation_quality", "AGR+"),
            (
                "bad_supplier_order_share",
                "fraud_identification",
                "bad_supplier_order_share",
            ),
            (
                "spend_on_bad_supplier_share",
                "fraud_identification",
                "spend_on_bad_supplier_share",
            ),
            ("on_time_ship_rate", "fulfilment_quality", "on_time_ship_rate"),
            ("se_half_lift", "negotiation_quality", "learning_speed", "se_half_lift"),
            ("agr_half_lift", "negotiation_quality", "learning_speed", "agr_half_lift"),
            (
                "fraud_avoidance_lift",
                "negotiation_quality",
                "learning_speed",
                "fraud_avoidance_lift",
            ),
        ]

        summary = {"n_runs": len(reports), "metrics": {}}
        for entry in metric_paths:
            name = entry[0]
            path = entry[1:]
            values = [_extract(r, *path) for r in reports]
            summary["metrics"][name] = _agg(values)

        bankrupt = sum(1 for r in reports if _extract(r, "profitability", "bankrupt"))
        summary["bankrupt_count"] = bankrupt

        summary_path = os.path.join(log_dir, "summary.json")
        try:
            with open(summary_path, "w", encoding="utf-8") as f:
                json.dump(summary, f, indent=2, ensure_ascii=False)
            logger.info(f"Session summary saved: {summary_path}")
        except Exception as e:
            logger.warning(f"Failed to save session summary: {e}")

    def close(self):
        for fp in (self._output_log_fp, self._balance_log_fp, self._messages_log_fp):
            if fp and not fp.closed:
                fp.close()
