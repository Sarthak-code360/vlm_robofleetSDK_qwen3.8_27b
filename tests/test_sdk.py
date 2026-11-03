import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from robofleet import Coordinator, ModelClient, ModelError, load_json, system_prompt

EXAMPLES = Path(__file__).parent.parent / "examples/warehouse"
SITE = load_json(EXAMPLES / "site.json")
FLEET = load_json(EXAMPLES / "fleet_state.json")


class FakeServer:
    """A tiny OpenAI-compatible server that records the request and returns `reply`."""

    def __init__(self, reply: str):
        self.reply, self.requests = reply, []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                outer.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                body = json.dumps({"choices": [{"message": {"content": outer.reply}}], "usage": {"total_tokens": 1}})
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body.encode())

            def log_message(self, *args):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class ModelClientTest(unittest.TestCase):
    def test_local_server_needs_no_key_and_thinking_can_be_off(self):
        server = FakeServer('{"ok": true}')
        try:
            answer, stats = ModelClient(server.url, "qwen", thinking=False).chat_json(
                [{"role": "user", "content": "hi"}], {"type": "object"}, "t")
        finally:
            server.close()
        self.assertEqual(answer, {"ok": True})
        self.assertEqual(server.requests[0]["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(server.requests[0]["response_format"]["json_schema"]["name"], "t")

    def test_thinking_on_sends_no_template_kwargs(self):
        server = FakeServer("hello")
        try:
            text, _ = ModelClient(server.url, "qwen").chat([{"role": "user", "content": "hi"}])
        finally:
            server.close()
        self.assertEqual(text, "hello")
        self.assertNotIn("chat_template_kwargs", server.requests[0])

    def test_hosted_default_requires_key(self):
        with self.assertRaises(ModelError):
            ModelClient(api_key=None).chat([{"role": "user", "content": "hi"}])

    def test_from_env(self):
        import os
        from unittest import mock
        env = {"VLM_BASE_URL": "http://localhost:8000/v1", "VLM_MODEL": "qwen", "VLM_THINKING": "off"}
        with mock.patch.dict(os.environ, env, clear=True):
            client = ModelClient.from_env()
        self.assertEqual((client.base_url, client.model, client.thinking), ("http://localhost:8000/v1", "qwen", False))


class CoordinatorTest(unittest.TestCase):
    def test_offline_uses_rules(self):
        decision = Coordinator(SITE).plan(FLEET)
        self.assertEqual(decision.source, "rules")
        self.assertEqual(decision.plan, decision.baseline)
        self.assertTrue(decision.plan["assignments"])

    def test_model_plan_is_checked(self):
        reply = json.dumps({"assignments": [{"task_id": "T-901", "robot_id": "RBT-004", "reason": "closest"},
                                            {"task_id": "T-902", "robot_id": "RBT-001", "reason": "here"}],
                            "robot_messages": [], "summary": "s"})
        server = FakeServer(reply)
        try:
            decision = Coordinator(SITE, ModelClient(server.url, "qwen")).plan(FLEET)
        finally:
            server.close()
        self.assertEqual(decision.source, "model")
        self.assertEqual([a["task_id"] for a in decision.plan["assignments"]], ["T-902"])
        self.assertIn("battery would drop", decision.rejected[0])

    def test_model_failure_falls_back_and_logs(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "decisions.jsonl"
            unreachable = ModelClient("http://127.0.0.1:9/v1", "qwen", timeout=2)
            decision = Coordinator(SITE, unreachable, log_path=log).plan(FLEET)
            record = json.loads(log.read_text().splitlines()[0])
        self.assertEqual(decision.source, "rules")
        self.assertIn("Could not reach model", decision.error)
        self.assertEqual(record["source"], "rules")

    def test_prompt_uses_site_type(self):
        self.assertIn("robots at a hotel", system_prompt({"type": "hotel"}))
        self.assertIn("robots at a site", system_prompt({}))


if __name__ == "__main__":
    unittest.main()
