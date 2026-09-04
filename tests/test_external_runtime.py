import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from agent.ecommerce_agent import EcommerceBenchAgent
from agent.ecommerce_tool_manager import EcommerceToolManager
from agent.external_runtime import ExternalAgentSession, ExternalRuntimeError
from agent.job import build_ecommerce_job
from agent.prompts import CONTEXT_WINDOW_PROMPT, SYSTEM_PROMPT, USER_PROMPT
from external_agent_server import ExternalAgentHandler
from tools import ECOMMERCE_TOOL_SCHEMAS


class ExternalAgentSessionTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.environment = patch.dict(
            os.environ,
            {"ECOMMERCE_BENCH_LOG_DIR": self.temporary_directory.name},
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.npc_setup = patch("agent.external_runtime.setup_npc_env")
        self.npc_setup.start()
        self.addCleanup(self.npc_setup.stop)

    def test_builtin_job_preserves_runtime_context_prompt(self):
        agent = EcommerceBenchAgent.__new__(EcommerceBenchAgent)
        agent.max_turns = 12
        agent.max_day = 34
        agent.initial_balance = 56789.0
        agent.daily_fee = 6.5
        agent.max_token_capacity = 1000
        agent.context_trigger = 800
        agent.context_clear_at_least = 400
        agent.context_keep_tool_use = 3
        agent.tool_schemas = ECOMMERCE_TOOL_SCHEMAS
        agent.run_index = 7

        job = agent._build_default_job()
        context_prompt = CONTEXT_WINDOW_PROMPT.format(
            max_token_capacity=1000,
            context_trigger=800,
            context_clear_at_least=400,
            context_keep=3,
        )
        user_prompt = (
            USER_PROMPT.replace("{daily_rent}", "6.5")
            .replace("{max_days}", "34")
            .replace("{max_token_capacity}", "runtime-managed")
            .replace("{initial_balance}", "56789")
        )

        self.assertEqual(
            job["messages"][0]["content"],
            SYSTEM_PROMPT + context_prompt + user_prompt,
        )
        self.assertEqual(job["agent_info"]["run_index"], 7)

    def test_external_session_uses_canonical_tool_execution(self):
        session = ExternalAgentSession(log_dir=self.temporary_directory.name)
        self.addCleanup(session.close)
        external = session.act(
            [{"id": "market-1", "name": "market_search", "arguments": {}}]
        )

        direct_job = build_ecommerce_job(
            tool_schemas=ECOMMERCE_TOOL_SCHEMAS,
            run_index=1,
        )
        direct_manager = EcommerceToolManager.init(direct_job)
        self.addCleanup(direct_manager.close)
        direct = direct_manager.ask_code_exec(
            direct_job,
            [
                {
                    "tool_call_id": "market-1",
                    "tool_name": "market_search",
                    "tool_args": {},
                }
            ],
        )

        self.assertEqual(external["tool_responses"][0]["content"], direct[0])
        self.assertEqual(session.job["traj"][-1]["role"], "tool")

    def test_terminal_action_returns_canonical_result(self):
        session = ExternalAgentSession(
            max_day=1,
            log_dir=self.temporary_directory.name,
        )
        result = session.act(
            [
                {
                    "id": "wait-1",
                    "name": "wait_for_next_day",
                    "arguments": {"current_day": "2026-01-01"},
                }
            ]
        )

        self.assertTrue(result["done"])
        self.assertEqual(result["termination_reason"], "env_completed")
        self.assertEqual(result["termination_detail"], "max_days_reached")
        self.assertIsNotNone(session.result()["final_state"])
        with self.assertRaisesRegex(ExternalRuntimeError, "already terminated"):
            session.act([{"name": "check_balance", "arguments": {}}])

    def test_rejects_invalid_tool_call_shapes(self):
        session = ExternalAgentSession(log_dir=self.temporary_directory.name)
        self.addCleanup(session.close)
        invalid_calls = (
            [],
            ["not-an-object"],
            [{"arguments": {}}],
            [{"name": "check_balance", "arguments": []}],
            [{"name": "check_balance", "arguments": "{not-json"}],
            [
                {"id": "same", "name": "check_balance"},
                {"id": "same", "name": "check_balance"},
            ],
        )
        for calls in invalid_calls:
            with self.subTest(calls=calls):
                with self.assertRaises(ExternalRuntimeError):
                    session.act(calls)

    def test_rejects_negative_daily_fee(self):
        with self.assertRaisesRegex(ExternalRuntimeError, "daily_fee"):
            ExternalAgentSession(daily_fee=-1)


class ExternalAgentHttpTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.environment = patch.dict(
            os.environ,
            {"ECOMMERCE_BENCH_LOG_DIR": self.temporary_directory.name},
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        npc_setup = patch("agent.external_runtime.setup_npc_env")
        npc_setup.start()
        self.addCleanup(npc_setup.stop)

        self.session = ExternalAgentSession(log_dir=self.temporary_directory.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ExternalAgentHandler)
        self.server.session = self.session
        self.server.access_token = "test-token"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.addCleanup(self._stop_server)

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.session.close()

    def _request(self, method, path, body=None, token="test-token"):
        data = json.dumps(body).encode() if body is not None else None
        request = Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        with urlopen(request) as response:
            return response.status, json.load(response)

    def test_serves_canonical_session_and_actions(self):
        status, descriptor = self._request("GET", "/v1/session")
        self.assertEqual(status, 200)
        self.assertEqual(descriptor["config"]["max_day"], 365)
        self.assertEqual(descriptor["tools"], ECOMMERCE_TOOL_SCHEMAS)
        status, result = self._request(
            "POST",
            "/v1/actions",
            {
                "content": "Checking available funds.",
                "reasoning_content": "Establish the starting balance.",
                "tool_calls": [
                    {"id": "balance-1", "name": "check_balance", "arguments": {}}
                ],
            },
        )
        self.assertEqual(status, 200)
        content = json.loads(result["tool_responses"][0]["content"])
        self.assertEqual(
            self.session.job["traj"][0]["content"],
            "Checking available funds.",
        )
        self.assertEqual(
            self.session.job["traj"][0]["reasoning_content"],
            "Establish the starting balance.",
        )

        status, finished = self._request("POST", "/v1/finish")
        self.assertEqual(status, 200)
        self.assertEqual(finished["termination_detail"], "external_runtime_closed")

        status, fetched = self._request("GET", "/v1/result")
        self.assertEqual(status, 200)
        self.assertEqual(fetched, finished)
        self.assertEqual(content["bank_balance"], 100000.0)

    def test_rejects_invalid_token(self):
        with self.assertRaises(HTTPError) as caught:
            self._request("GET", "/v1/session", token="wrong")
        self.assertEqual(caught.exception.code, 401)


if __name__ == "__main__":
    unittest.main()
