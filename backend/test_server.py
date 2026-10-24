"""Protocol, persistence, and HTTP boundary tests using a local fake subprocess fixture."""
import json
import base64
import os
import socket
import subprocess
import stat
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import uuid
from unittest.mock import MagicMock, Mock, patch
from http.server import ThreadingHTTPServer
from pathlib import Path

import server
import android_mcp_server as android_mcp


# This is deliberately a test-only JSON-RPC child process, not a simulated Codex install.
FAKE_APP_SERVER = r'''
import json, os, sys, time
turn_number = 0
loaded_threads = set()
for line in sys.stdin:
    m = json.loads(line)
    method, p = m.get("method"), m.get("params", {})
    result = {}
    if method == "initialize": result = {"userAgent":"phone-harness-fixture"}
    elif method == "account/read": result = {"account":{"type":"apiKey" if os.environ.get("PHONE_HARNESS_MODEL_KEY") else "chatgpt","email":"private@example.invalid"},"requiresOpenaiAuth":False}
    elif method == "model/list": result = {"data":[{"id":"fixture-model","displayName":"Fixture model","isDefault":True}]}
    elif method == "fixture/env": result = {name: bool(os.environ.get(name)) for name in ["HTTP_PROXY", "https_proxy", "ALL_PROXY", "openai_api_key", "PHONE_HARNESS_MODEL_KEY"]}
    elif method == "thread/start":
        if p.get("approvalPolicy") != "on-request" or p.get("sandbox") not in ("read-only", "workspace-write", "danger-full-access"):
            print(json.dumps({"jsonrpc":"2.0","id":m["id"],"error":{"code":-32602,"message":"invalid fixture enum"}}), flush=True)
            continue
        print(json.dumps({"jsonrpc":"2.0","method":"fixture/sandbox","params":{"method":"thread/start","sandbox":p["sandbox"]}}), flush=True)
        loaded_threads.add("thread-fixture")
        result = {"thread":{"id":"thread-fixture"}}
    elif method == "thread/resume":
        if p.get("approvalPolicy") != "on-request" or p.get("sandbox") not in ("read-only", "workspace-write", "danger-full-access"):
            print(json.dumps({"jsonrpc":"2.0","id":m["id"],"error":{"code":-32602,"message":"invalid resume enum"}}), flush=True)
            continue
        print(json.dumps({"jsonrpc":"2.0","method":"fixture/sandbox","params":{"method":"thread/resume","sandbox":p["sandbox"]}}), flush=True)
        loaded_threads.add(p.get("threadId"))
        result = {"thread":{"id":p.get("threadId")}}
    elif method == "turn/start":
        if p.get("threadId") not in loaded_threads:
            print(json.dumps({"jsonrpc":"2.0","id":m["id"],"error":{"code":-32000,"message":"thread not found; call thread/resume first"}}), flush=True)
            continue
        turn_number += 1
        turn_id = "turn-" + str(turn_number)
        for request in [
          {"jsonrpc":"2.0","id":"approval-"+str(turn_number),"method":"item/commandExecution/requestApproval","params":{"threadId":p["threadId"],"turnId":turn_id,"title":"Approve fixture command"}},
          {"jsonrpc":"2.0","id":"unsupported-"+str(turn_number),"method":"fixture/unsupported","params":{}}
        ]: print(json.dumps(request), flush=True)
        result = {"turn":{"id":turn_id}}
        print(json.dumps({"jsonrpc":"2.0","method":"fixture/turn-start","params":{"threadId":p["threadId"],"turnId":turn_id,"input":p.get("input")}}), flush=True)
        print(json.dumps({"jsonrpc":"2.0","id":m["id"],"result":result}), flush=True)
        if turn_number > 1:
          print(json.dumps({"jsonrpc":"2.0","method":"item/agentMessage/delta","params":{"threadId":p["threadId"],"turnId":"turn-1","delta":"stale answer"}}), flush=True)
        for event in [
          {"jsonrpc":"2.0","method":"item/agentMessage/delta","params":{"threadId":p["threadId"],"turnId":turn_id,"delta":"fixture answer"}},
          {"jsonrpc":"2.0","method":"item/completed","params":{"threadId":p["threadId"],"turnId":turn_id,"item":{"type":"agentMessage","text":"fixture answer"}}},
          {"jsonrpc":"2.0","method":"turn/completed","params":{"threadId":p["threadId"],"turn":{"id":turn_id,"status":"completed"}}}
        ]: print(json.dumps(event), flush=True)
        continue
    elif method == "turn/steer":
        if not p.get("expectedTurnId") or p.get("threadId") not in ("thread-fixture", "thread-terminal") or not p.get("input"):
            print(json.dumps({"jsonrpc":"2.0","id":m["id"],"error":{"code":-32602,"message":"invalid steer params"}}), flush=True)
            continue
        if p["input"][0].get("text") == "completion-race":
            print(json.dumps({"jsonrpc":"2.0","id":m["id"],"error":{"code":-32000,"message":"active turn cannot accept same-turn steering"}}), flush=True)
            # Exercise rejection-before-completion instead of relying on which
            # host thread happens to run first after the RPC reply.
            time.sleep(0.05)
            print(json.dumps({"jsonrpc":"2.0","method":"turn/completed","params":{"threadId":p["threadId"],"turn":{"id":p["expectedTurnId"],"status":"completed"}}}), flush=True)
            continue
        print(json.dumps({"jsonrpc":"2.0","method":"fixture/steered","params":{"clientUserMessageId":p.get("clientUserMessageId"),"expectedTurnId":p["expectedTurnId"]}}), flush=True)
        result = {"turn":{"id":p["expectedTurnId"]}}
    elif method == "account/login/start":
        result = {"loginId":"login-fixture","authUrl":"https://example.invalid/login"}
        print(json.dumps({"jsonrpc":"2.0","method":"account/login/completed","params":{"loginId":"login-fixture","success":True}}), flush=True)
    elif "id" in m and "error" in m and str(m["id"]).startswith("unsupported-"):
        print(json.dumps({"jsonrpc":"2.0","method":"fixture/unsupported-rejected","params":{}}), flush=True)
        continue
    elif "id" in m and m.get("result",{}).get("decision"):
        print(json.dumps({"jsonrpc":"2.0","method":"fixture/approval-decided","params":{"decision":m["result"]["decision"]}}), flush=True)
        continue
    else: result = {}
    if "id" in m: print(json.dumps({"jsonrpc":"2.0","id":m["id"],"result":result}), flush=True)
'''


def pid_running(pid):
    try: os.kill(pid, 0)
    except ProcessLookupError: return False
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().split()
        if len(fields) > 2 and fields[2] == "Z": return False
    except OSError: pass
    return True


class AppServerRequestBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.queued = Mock()
        self.engine = server.AppServer(["unused-fixture"], Path("unused-fixture-home"), lambda _: None,
                                       on_request=self.queued)
        self.engine.respond = Mock()

    def test_only_implemented_decision_requests_reach_approval_ui(self):
        for method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            with self.subTest(method=method):
                self.queued.reset_mock(); self.engine.respond.reset_mock()
                params = {"threadId": "thread-fixture", "turnId": "turn-fixture", "title": "Fixture action"}
                self.engine._handle_server_request({"id": 17, "method": method, "params": params})
                self.queued.assert_called_once_with({"id": 17, "key": "17", "method": method, "params": params})
                self.engine.respond.assert_not_called()

    def test_user_input_and_unknown_approval_like_requests_are_never_auto_accepted(self):
        for method in ("item/tool/requestUserInput", "mcpServer/elicitation/request",
                       "item/permissions/requestApproval", "fixture/requestApproval",
                       "item/commandExecution/requestApproval/extra", "item/commandexecution/requestapproval"):
            with self.subTest(method=method):
                self.queued.reset_mock(); self.engine.respond.reset_mock()
                # A tool/server name in question text is not evidence of authorization.
                params = {"threadId": "thread-fixture", "questions": [{"id": "daxqo_android",
                    "question": "Accept android_action?", "options": [{"label": "Accept"}, {"label": "Decline"}]}]}
                self.engine._handle_server_request({"id": "untrusted", "method": method, "params": params})
                self.queued.assert_not_called()
                self.engine.respond.assert_called_once_with("untrusted", error={"code": -32601, "message": "Unsupported app-server request"})

    def test_supported_request_rejects_malformed_params_and_queue_failure(self):
        for params in (None, [], ["bad"], "bad", 1):
            with self.subTest(params=params):
                self.engine.respond.reset_mock()
                self.engine._handle_server_request({"id": 5, "method": "item/fileChange/requestApproval", "params": params})
                self.engine.respond.assert_called_once_with(5, error={"code": -32602, "message": "Invalid approval request params"})
        self.queued.assert_not_called()
        self.queued.side_effect = RuntimeError("fixture failure")
        self.engine.respond.reset_mock()
        self.engine._handle_server_request({"id": 6, "method": "item/fileChange/requestApproval", "params": {}})
        self.engine.respond.assert_called_once_with(6, error={"code": -32603, "message": "Unable to queue approval request"})


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        # Match the service's canonical paths, including Windows 8.3 TEMP aliases.
        self.root = Path(self.temp.name).resolve()
        self.fake_path = self.root / "fake_app_server.py"
        self.fake_path.write_text(FAKE_APP_SERVER, encoding="utf-8")
        self.engine = server.AppServer([sys.executable, str(self.fake_path)], self.root / "codex", lambda _: None)
        self.engine.start()
        self.harness = server.Harness("codex-not-installed-for-test", self.root / "state", self.root / "workspace", "127.0.0.1", 18880, test_engine=self.engine)
        self.events = []
        self.engine.on_event = lambda event: (self.events.append(event), self.harness._engine_event(event))

    def tearDown(self):
        if self.harness.engine: self.harness.engine.close()
        if self.engine is not self.harness.engine: self.engine.close()
        self.harness.store.db.close()
        self.temp.cleanup()

    def test_submit_request_id_is_durable_and_replay_precedes_capacity_or_offline_checks(self):
        body={"prompt":"delegated task", "title":"delegation", "clientRequestId":"voice-request-001"}
        with patch.object(self.harness, "_run_task"):
            first=self.harness.start_task(body)
            second=self.harness.start_task(body)
        self.assertEqual(first["id"],second["id"])
        with self.assertRaisesRegex(ValueError,"不同任务"):
            self.harness.start_task({**body,"prompt":"different task"})
        with patch.object(self.harness, "engine", None):
            self.assertEqual(self.harness.start_task(body)["id"],first["id"])
        self.harness.store.db.close()
        self.harness.store=server.Store(self.root/"state"/"harness.sqlite3")
        self.assertEqual(self.harness.start_task(body)["id"],first["id"])
        self.assertEqual(self.harness.store.db.execute("SELECT count(*) FROM tasks").fetchone()[0],1)

    def test_new_thread_receives_owner_policy_and_phone_facts_without_replacing_user_prompt(self):
        self.harness.phone_context.instructions=Mock(return_value="\nPHONE_FACTS: installed com.autonavi.minimap")
        with patch.object(self.engine,"request",wraps=self.engine.request) as calls:
            task=self.harness.start_task({"prompt":"find a nearby park"})
            deadline=time.monotonic()+3
            while time.monotonic()<deadline and self.harness.store.row(task["id"])["status"] in ("queued","running"):
                time.sleep(.01)
        start=next(c for c in calls.call_args_list if c.args[0]=="thread/start")
        policy=start.args[1]["developerInstructions"]
        self.assertIn("PHONE_FACTS",policy)
        self.assertIn("INSTALLED",policy)
        self.assertIn("one-tap login",policy)
        turn=next(c for c in calls.call_args_list if c.args[0]=="turn/start")
        self.assertEqual(turn.args[1]["input"][0]["text"],"find a nearby park")

    def insert_running_task(self, task_id):
        stamp = server.now()
        with self.harness.store.db:
            self.harness.store.db.execute(
                "INSERT INTO tasks(id,title,prompt,status,created_at,updated_at,thread_id,turn_id) VALUES(?,?,?,?,?,?,?,?)",
                (task_id, "fixture", "fixture prompt", "running", stamp, stamp, "thread-terminal", "turn-terminal"))
        self.harness.active_task_id = task_id
        self.harness._task_events[task_id] = [{"threadId": "thread-terminal", "turnId": "turn-terminal"}]

    def make_installed_layout(self):
        app = self.root / "installed-app"
        state, workspace = app / "state", app / "workspace"
        state.mkdir(parents=True)
        workspace.mkdir()
        (app / "runtime_control.py").write_text("# fixture", encoding="utf-8")
        (state / "capability").write_text("fixture-capability-token-0123456789abcdef", encoding="ascii")
        return app, state, workspace

    def test_runtime_control_autostart_ignores_fixture_harness_paths(self):
        with patch("server.sys.platform", "linux"), patch("server.urllib.request.build_opener") as opener, \
                patch("server.subprocess.Popen") as popen:
            result = server.ensure_runtime_control(Path(server.__file__).resolve().parent,
                self.root / "state", self.root / "workspace", 18880)
        self.assertIsNone(result)
        opener.assert_not_called()
        popen.assert_not_called()

    def test_runtime_control_autostart_reuses_identified_bridge(self):
        app, state, workspace = self.make_installed_layout()
        response = MagicMock()
        response.status = 200
        response.headers = {"Server": "DAXQORuntime/0.2 Python/3"}
        response.read.return_value = b'{"state":"running","app":{"name":"DAXQO","version":"0.2.0"}}'
        response.__enter__.return_value = response
        opener = Mock()
        opener.open.return_value = response
        with patch("server.sys.platform", "linux"), patch("server.urllib.request.build_opener", return_value=opener), \
                patch("server.subprocess.Popen") as popen:
            result = server.ensure_runtime_control(app, state, workspace, 18880)
        self.assertIsNone(result)
        opener.open.assert_called_once()
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer fixture-capability-token-0123456789abcdef")
        popen.assert_not_called()

    def test_runtime_control_autostart_launches_detached_with_private_log(self):
        app, state, workspace = self.make_installed_layout()
        opener = Mock()
        opener.open.side_effect = urllib.error.URLError("not listening")
        process = Mock()
        with patch("server.sys.platform", "linux"), patch("server.urllib.request.build_opener", return_value=opener), \
                patch.dict(os.environ, {"DAXQO_STAGE":str(self.root / "test-stage")}), \
                patch("server.os.chmod") as chmod, \
                patch("server.subprocess.Popen", return_value=process) as popen:
            result = server.ensure_runtime_control(app, state, workspace, 18880)
        self.assertIs(result, process)
        command = popen.call_args.args[0]
        self.assertEqual(command, [sys.executable, str(app / "runtime_control.py"), "--app", str(app), "--stage", str(self.root / "test-stage"), "--port", "18882", "--backend-port", "18880"])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual((state / "runtime-control-launcher.log").name, "runtime-control-launcher.log")
        chmod.assert_called_once_with(state / "runtime-control-launcher.log", 0o600)

    def test_followup_resumes_saved_thread_on_a_fresh_app_server(self):
        stamp = server.now()
        task_id = "saved-thread-after-restart"
        with self.harness.store.db:
            self.harness.store.db.execute("INSERT INTO tasks(id,title,prompt,status,created_at,updated_at,thread_id,result) VALUES(?,?,?,?,?,?,?,?)",
                (task_id, "saved", "initial prompt", "completed", stamp, stamp, "thread-fixture", "prior answer"))
        self.harness.store.add_message(task_id, "user", "initial prompt", "started", client_id="initial:" + task_id)
        self.harness.store.add_message(task_id, "assistant", "prior answer", "completed")
        self.harness._loaded_threads.clear()
        _, _, delivery = self.harness.send_message(task_id, {"text":"continue existing thread", "clientMessageId":"resume-client"})
        self.assertEqual(delivery, "queued")
        deadline = time.time() + 3
        while time.time() < deadline and len(self.harness.store.messages(task_id)) < 4: time.sleep(0.01)
        row = self.harness.store.row(task_id)
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["thread_id"], "thread-fixture")
        resumed = next(i for i, e in enumerate(self.events) if e.get("method") == "fixture/sandbox" and e["params"].get("method") == "thread/resume")
        started = next(i for i, e in enumerate(self.events) if e.get("method") == "fixture/turn-start" and any(x.get("text") == "continue existing thread" for x in e["params"].get("input", [])))
        self.assertLess(resumed, started)

    def test_failed_message_requires_explicit_safe_retry_with_same_id(self):
        self.insert_running_task("retry-safe")
        self.harness.store.update("retry-safe", thread_id="thread-fixture", turn_id="turn-retry")
        self.harness._task_events["retry-safe"][0].update(threadId="thread-fixture", turnId="turn-retry")
        rejected = server.RpcError("fixture rejected input")
        rejected.rpc_rejected = True
        with patch.object(self.engine, "request", side_effect=rejected):
            with self.assertRaisesRegex(ValueError, "delivery failed"):
                self.harness.send_message("retry-safe", {"text":"retry me", "clientMessageId":"retry-client"})
        saved = self.harness.store.message_by_client("retry-safe", "retry-client")
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["retry_safe"], 1)
        self.assertEqual(self.harness.store.messages("retry-safe")[-1]["clientMessageId"], "retry-client")
        request = Mock(return_value={})
        with patch.object(self.engine, "request", request):
            _, retried, delivery = self.harness.send_message("retry-safe", {"text":"retry me", "clientMessageId":"retry-client", "retry":True})
            _, duplicate, same_delivery = self.harness.send_message("retry-safe", {"text":"retry me", "clientMessageId":"retry-client", "retry":True})
        self.assertEqual(delivery, "steered")
        self.assertEqual(same_delivery, "steered")
        self.assertEqual(retried["id"], duplicate["id"])
        request.assert_called_once()

    def test_uncertain_delivery_cannot_be_retried_with_same_client_id(self):
        self.insert_running_task("retry-uncertain")
        self.harness.store.update("retry-uncertain", thread_id="thread-fixture", turn_id="turn-timeout")
        self.harness._task_events["retry-uncertain"][0].update(threadId="thread-fixture", turnId="turn-timeout")
        def timeout_after_native_completion(*_args, **_kwargs):
            self.harness._engine_event({"method":"turn/completed", "params":{"threadId":"thread-fixture",
                "turn":{"id":"turn-timeout", "status":"completed"}}})
            raise server.RpcError("Codex request timed out: turn/steer")
        request = Mock(side_effect=timeout_after_native_completion)
        with patch.object(self.engine, "request", request):
            with self.assertRaisesRegex(ValueError, "delivery failed"):
                self.harness.send_message("retry-uncertain", {"text":"maybe delivered", "clientMessageId":"uncertain-client"})
            with self.assertRaisesRegex(ValueError, "Retry is unsafe"):
                self.harness.send_message("retry-uncertain", {"text":"maybe delivered", "clientMessageId":"uncertain-client", "retry":True})
        self.assertEqual(request.call_count, 1)
        self.assertEqual(self.harness.store.message_by_client("retry-uncertain", "uncertain-client")["status"], "failed")
        self.assertFalse(any(e.get("method") == "fixture/turn-start" and any(x.get("text") == "maybe delivered" for x in e["params"].get("input", [])) for e in self.events))

    def test_stdio_rpc_initialize_account_catalog_and_login(self):
        self.assertTrue(self.engine.connected)
        self.assertTrue(self.engine.version.startswith("Python "))
        self.assertEqual(self.engine.account, {"authenticated": True, "type": "chatgpt"})
        self.assertNotIn("email", json.dumps(self.harness.status()))
        self.assertEqual({item["id"] for item in self.harness.models()}, {"deepseek-flash", "deepseek-v4-pro"})
        self.assertEqual(self.engine.begin_login()["loginId"], "login-fixture")
        deadline = time.time() + 1
        while time.time() < deadline and self.engine.account_error: time.sleep(0.01)
        self.assertTrue(self.engine.account["authenticated"])

    def test_account_read_never_infers_login_from_arbitrary_or_false_flat_data(self):
        request = self.engine.request
        try:
            self.engine.request = lambda method, params=None, timeout=server.RPC_TIMEOUT: {"requiresOpenaiAuth": True}
            self.assertFalse(self.engine.refresh_account()["authenticated"])
            self.engine.request = lambda method, params=None, timeout=server.RPC_TIMEOUT: {"authenticated": False, "type": "chatgpt"}
            self.assertFalse(self.engine.refresh_account()["authenticated"])
        finally:
            self.engine.request = request

    def test_provider_persistence_is_private_and_config_keeps_key_outside_toml(self):
        self.harness.engine_path = sys.executable
        api_key = "fixture-secret-key"
        real_app_server = server.AppServer
        captured = {}
        def build_engine(command, *args, **kwargs):
            captured.update(kwargs)
            return real_app_server([sys.executable, str(self.fake_path)], *args, **kwargs)
        with patch("server.AppServer", side_effect=build_engine):
            response = self.harness.save_provider({"provider": "deepseek", "model": "deepseek-flash", "apiKey": api_key})
        provider_file = self.root / "state" / "private" / "provider.json"
        config_file = self.root / "state" / "codex" / "config.toml"
        stored = json.loads(provider_file.read_text(encoding="utf-8"))
        config = config_file.read_text(encoding="utf-8")
        self.assertEqual(stored["apiKey"], api_key)
        self.assertNotIn(api_key, config)
        self.assertNotIn(api_key, json.dumps(response))
        self.assertEqual(captured["extra_env"][server.API_KEY_ENV], api_key)
        self.assertIn('model_provider = "deepseek"', config)
        self.assertIn('wire_api = "responses"', config)
        self.assertIn('web_search = "disabled"', config)
        self.assertNotIn("check_for_updates", config)
        self.assertTrue(response["provider"]["configured"])
        self.assertEqual(response["status"]["account"], {"authenticated": True, "type": "apiKey"})
        if os.name != "nt": self.assertEqual(stat.S_IMODE(provider_file.stat().st_mode), 0o600)
        # Empty keys retain the existing credential and do not expose it in the public shape.
        with patch("server.AppServer", side_effect=build_engine):
            response = self.harness.save_provider({"provider": "deepseek", "model": "deepseek-flash", "apiKey": ""})
        self.assertEqual(json.loads(provider_file.read_text(encoding="utf-8"))["apiKey"], api_key)
        self.assertNotIn("apiKey", response["provider"])
        with patch("server.AppServer", side_effect=build_engine):
            response = self.harness.save_provider({"provider": "custom", "baseUrl": "https://models.example/v1", "model": "custom-model", "apiKey": ""})
        self.assertEqual(json.loads(provider_file.read_text(encoding="utf-8"))["apiKey"], "")
        self.assertFalse(response["provider"]["configured"])

    def test_provider_test_classifies_failure_and_marks_only_success_verified(self):
        self.harness.provider.update({"provider": "deepseek", "baseUrl": server.DEEPSEEK_BASE_URL,
                                      "model": "deepseek-flash", "apiKey": "test-key", "verified": False})
        self.harness.provider_saved = True
        class Success:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, size): return b'{"id":"resp_fixture","status":"completed","output":[{"type":"message","content":[{"type":"output_text","text":"OK"}]}]}'
        opener = Mock()
        opener.open.return_value = Success()
        with patch("server.urllib.request.build_opener", return_value=opener) as build:
            result = self.harness.test_provider()
        self.assertTrue(result["verified"])
        request = opener.open.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(payload["reasoning"], {"effort": "low"})
        self.assertEqual(payload["max_output_tokens"], 32)
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
        self.assertEqual(build.call_args.args[0].proxies, {})
        self.assertNotIn("test-key", json.dumps(result))
        self.harness.provider["verified"] = False
        failure = urllib.error.HTTPError("https://api.deepseek.com/responses", 401, "unauthorized", {}, None)
        failed_opener = Mock()
        failed_opener.open.side_effect = failure
        with patch("server.urllib.request.build_opener", return_value=failed_opener):
            result = self.harness.test_provider()
        self.assertFalse(result["verified"])
        self.assertIn("rejected", result["error"])
        self.assertNotIn("test-key", json.dumps(result))
        class Incomplete:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, size): return b'{"id":"resp_fixture","status":"incomplete","output":[]}'
        incomplete_opener = Mock()
        incomplete_opener.open.return_value = Incomplete()
        with patch("server.urllib.request.build_opener", return_value=incomplete_opener):
            result = self.harness.test_provider()
        self.assertFalse(result["verified"])
        self.assertIn("no completed text", result["error"])

    def test_unconfigured_provider_uses_deepseek_catalog_not_codex_gpt_catalog(self):
        provider = self.harness.public_provider()
        self.assertEqual(provider["provider"], "deepseek")
        self.assertFalse(provider["configured"])
        self.assertFalse(provider["verified"])
        models = self.harness.models()
        self.assertEqual({model["id"] for model in models}, {"deepseek-flash", "deepseek-v4-pro"})
        self.assertTrue(next(model for model in models if model["id"] == "deepseek-flash")["isDefault"])
        self.harness._write_codex_config()
        config = (self.root / "state" / "codex" / "config.toml").read_text(encoding="utf-8")
        self.assertIn('model_provider = "deepseek"', config)
        catalog = server.Path(__file__).resolve().parent / "models-deepseek.json"
        self.assertIn(json.dumps(str(catalog)), config)
        self.assertIn("[analytics]\nenabled = false", config)

    def test_engine_child_drops_vendor_proxy_and_openai_environment(self):
        with patch.dict(os.environ, {"HTTP_PROXY":"http://proxy.invalid:8080", "https_proxy":"http://proxy.invalid:8080",
                                     "ALL_PROXY":"http://proxy.invalid:8080", "openai_api_key":"vendor-secret",
                                     server.API_KEY_ENV:"stale-secret"}, clear=False):
            child = server.AppServer([sys.executable, str(self.fake_path)], self.root / "isolated", lambda _: None,
                                     extra_env={server.API_KEY_ENV:"phone-key"})
            child.start()
            try:
                env = child.request("fixture/env")
                self.assertEqual(env, {"HTTP_PROXY":False,"https_proxy":False,"ALL_PROXY":False,
                                       "openai_api_key":False,server.API_KEY_ENV:True})
            finally: child.close()

    def test_custom_provider_models_do_not_include_deepseek_catalog(self):
        self.harness.provider.update({"provider":"custom", "baseUrl":"https://models.example/v1",
                                      "model":"private-model", "apiKey":""})
        models = self.harness.models()
        self.assertEqual([model["id"] for model in models], ["private-model"])
        self.assertTrue(models[0]["isDefault"])

    @unittest.skipIf(os.name == "nt", "SIGTERM process lifecycle is exercised on the phone's Linux runtime")
    def test_sigterm_stops_http_server_and_codex_child(self):
        port_socket = socket.socket()
        port_socket.bind(("127.0.0.1", 0))
        port = port_socket.getsockname()[1]
        port_socket.close()
        child_pid_file = self.root / "child.pid"
        grandchild_pid_file = self.root / "grandchild.pid"
        heartbeat_file = self.root / "grandchild-heartbeat"
        heartbeat_script = self.root / "heartbeat.py"
        heartbeat_script.write_text("import signal,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nwith open(" + repr(str(heartbeat_file)) + ", 'a', buffering=1) as f:\n while True:\n  f.write('x\\n'); time.sleep(0.03)\n", encoding="utf-8")
        fake_codex = self.root / "fake-codex"
        fake_codex.write_text(f'''#!{sys.executable}
import json, os, subprocess, sys
if sys.argv[1:] == ["--version"]:
 print("codex-fixture 1")
 raise SystemExit(0)
grandchild = subprocess.Popen([sys.executable, {str(heartbeat_script)!r}], start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
with open({str(grandchild_pid_file)!r}, "w") as f: f.write(str(grandchild.pid))
with open({str(child_pid_file)!r}, "w") as f: f.write(str(os.getpid()))
for line in sys.stdin:
 m=json.loads(line)
 if m.get("id") is not None:
  method=m.get("method")
  result={{"userAgent":"fixture"}} if method == "initialize" else {{"account":None,"requiresOpenaiAuth":True}}
  print(json.dumps({{"jsonrpc":"2.0","id":m["id"],"result":result}}), flush=True)
''', encoding="utf-8")
        fake_codex.chmod(0o700)
        state = self.root / "sigterm-state"
        workspace = self.root / "sigterm-workspace"
        process = subprocess.Popen([sys.executable, str(Path(server.__file__).resolve()), "--host", "127.0.0.1",
                                    "--port", str(port), "--codex", str(fake_codex), "--state", str(state),
                                    "--workspace", str(workspace)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        def read_pid(path):
            try:
                value = int(path.read_text(encoding="ascii").strip())
                return value if value > 0 else None
            except (OSError, ValueError):
                return None
        def fixture_identity_matches(pid, expected_script):
            try:
                cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
                return str(expected_script.resolve()).encode() in cmdline
            except OSError:
                return False
        def cleanup_fixture(pid_file, expected_script):
            pid = read_pid(pid_file)
            if not pid or not pid_running(pid) or not fixture_identity_matches(pid, expected_script): return
            try: os.kill(pid, 15)
            except ProcessLookupError: return
            deadline = time.time() + 1.0
            while time.time() < deadline and pid_running(pid) and fixture_identity_matches(pid, expected_script):
                time.sleep(0.025)
            if pid_running(pid) and fixture_identity_matches(pid, expected_script):
                try: os.kill(pid, 9)
                except ProcessLookupError: pass
        try:
            deadline = time.time() + 8
            ready = False
            child_pid = grandchild_pid = None
            heartbeat_ready = False
            while time.time() < deadline and process.poll() is None:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=0.3) as response:
                        ready = response.status == 200
                    child_pid = read_pid(child_pid_file)
                    grandchild_pid = read_pid(grandchild_pid_file)
                    try: heartbeat_ready = heartbeat_file.is_file() and heartbeat_file.stat().st_size > 0 and bool(heartbeat_file.read_text(encoding="ascii").strip())
                    except (OSError, UnicodeDecodeError): heartbeat_ready = False
                    if ready and child_pid and grandchild_pid and heartbeat_ready: break
                except Exception: time.sleep(0.05)
                time.sleep(0.05)
            self.assertTrue(ready, "HTTP listener did not start")
            self.assertIsNotNone(child_pid, "Codex child pid was not written")
            self.assertIsNotNone(grandchild_pid, "Codex grandchild pid was not written")
            self.assertTrue(heartbeat_ready, "detached grandchild did not write a non-empty heartbeat")
            process.terminate()
            process.wait(timeout=5)
            self.assertEqual(process.returncode, 0)
            deadline = time.time() + 3
            while time.time() < deadline and (pid_running(child_pid) or pid_running(grandchild_pid)):
                time.sleep(0.025)
            self.assertFalse(pid_running(child_pid), "Codex app-server process survived backend exit")
            self.assertFalse(pid_running(grandchild_pid), "Codex grandchild survived backend exit")
            heartbeat_size = heartbeat_file.stat().st_size
            time.sleep(0.15)
            self.assertEqual(heartbeat_file.stat().st_size, heartbeat_size, "detached grandchild continued its heartbeat")
        finally:
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait(timeout=5)
            cleanup_fixture(child_pid_file, fake_codex)
            cleanup_fixture(grandchild_pid_file, heartbeat_script)
            if process.stderr: process.stderr.close()

    @unittest.skipIf(os.name == "nt", "process-group lifecycle is Linux/POSIX-only")
    def test_app_server_close_cleans_grandchild_after_leader_exits(self):
        grandchild_pid_file = self.root / "orphan.pid"
        heartbeat_file = self.root / "orphan-heartbeat"
        heartbeat_script = self.root / "orphan-heartbeat.py"
        heartbeat_script.write_text("import signal,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nwith open(" + repr(str(heartbeat_file)) + ", 'a', buffering=1) as f:\n while True:\n  f.write('x\\n'); time.sleep(0.03)\n", encoding="utf-8")
        fake = self.root / "exit-after-account.py"
        fake.write_text(f'''#!{sys.executable}
import json, os, subprocess, sys
if sys.argv[1:] == ["--version"]:
 print("fixture 1")
 raise SystemExit(0)
for line in sys.stdin:
 m=json.loads(line)
 method=m.get("method")
 if method == "initialize": result={{"userAgent":"fixture"}}
 elif method == "account/read":
  grandchild=subprocess.Popen([sys.executable,{str(heartbeat_script)!r}],start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
  open({str(grandchild_pid_file)!r},"w").write(str(grandchild.pid))
  import time; time.sleep(0.3)
  result={{"account":None,"requiresOpenaiAuth":True}}
 else: result={{}}
 if "id" in m: print(json.dumps({{"jsonrpc":"2.0","id":m["id"],"result":result}}),flush=True)
 if method == "account/read": break
''', encoding="utf-8")
        engine = server.AppServer([sys.executable, str(fake)], self.root / "exit-codex", lambda _: None)
        try:
            engine.start()
            self.assertTrue(grandchild_pid_file.exists())
            grandchild_pid = int(grandchild_pid_file.read_text())
            deadline = time.time() + 2
            while time.time() < deadline and engine.process.poll() is None: time.sleep(0.01)
            self.assertIsNotNone(engine.process.poll(), "fixture leader should exit before close")
            self.assertTrue(heartbeat_file.exists())
            engine.close()
            deadline = time.time() + 2
            while time.time() < deadline and pid_running(grandchild_pid): time.sleep(0.025)
            self.assertFalse(pid_running(grandchild_pid), "grandchild survived after its session leader exited")
            heartbeat_size = heartbeat_file.stat().st_size
            time.sleep(0.15)
            self.assertEqual(heartbeat_file.stat().st_size, heartbeat_size, "detached grandchild continued its heartbeat")
            engine.close()  # Repeated close must not signal a potentially reused PID/process group.
        finally:
            engine.close()

    def test_task_events_and_explicit_resume_use_saved_thread(self):
        self.harness.sandbox = "danger-full-access"
        task = self.harness.start_task({"prompt": "complete a fixture task", "model": "fixture-model"})
        deadline = time.time() + 3
        while time.time() < deadline:
            saved = self.harness.store.row(task["id"])
            rejected = any(e.get("method") == "fixture/unsupported-rejected" for e in self.events)
            if saved["status"] in ("completed", "failed") and saved["turn_id"] == "turn-1" and rejected: break
            time.sleep(0.02)
        saved = self.harness.store.row(task["id"])
        self.assertEqual(saved["status"], "completed", saved)
        self.assertEqual(saved["thread_id"], "thread-fixture")
        self.assertEqual(saved["turn_id"], "turn-1")
        self.assertEqual(self.harness.status()["executionScope"], "phone-linux")
        self.assertTrue(any(e.get("method") == "fixture/sandbox" and e["params"] == {"method": "thread/start", "sandbox": "danger-full-access"} for e in self.events))
        self.assertTrue(any(e["text"] == "fixture answer" for e in self.harness.store.events(task["id"], 0)))
        message_events = [e for e in self.harness.store.events(task["id"], 0) if e["type"] == "message"]
        self.assertEqual([e["text"] for e in message_events], ["fixture answer"])
        self.assertTrue(any(e.get("method") == "fixture/unsupported-rejected" for e in self.events))
        approval = next(item for item in self.harness.approvals() if item["id"] == "approval-1")
        self.assertEqual(approval["taskId"], task["id"])
        self.assertEqual(self.harness.decide_approval(approval["id"], "accept")["decision"], "accept")
        self.harness.store.update(task["id"], status="interrupted")
        resumed = self.harness.start_task({}, task["id"])
        self.assertEqual(resumed["id"], task["id"])
        deadline = time.time() + 3
        while time.time() < deadline and self.harness.store.row(task["id"])["status"] not in ("completed", "failed"):
            time.sleep(0.02)
        resumed_row = self.harness.store.row(task["id"])
        self.assertEqual(resumed_row["thread_id"], "thread-fixture")
        self.assertEqual(resumed_row["turn_id"], "turn-2")
        self.assertNotIn("stale answer", resumed_row["result"])
        self.assertTrue(any(e.get("method") == "fixture/sandbox" and e["params"] == {"method": "thread/resume", "sandbox": "danger-full-access"} for e in self.events))

    def test_turn_completion_persists_id_when_it_arrives_before_start_response(self):
        self.insert_running_task("completion-before-start-response")
        self.harness.store.update("completion-before-start-response", turn_id=None)
        self.harness._engine_event({"method": "turn/completed", "params": {
            "threadId": "thread-terminal", "turn": {"id": "turn-terminal", "status": "completed"}}})
        row = self.harness.store.row("completion-before-start-response")
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["turn_id"], "turn-terminal")

    def test_messages_steer_idempotently_and_queue_until_turn_id_exists(self):
        stamp = server.now()
        task_id = "message-task"
        with self.harness.store.db:
            self.harness.store.db.execute("INSERT INTO tasks(id,title,prompt,status,created_at,updated_at,thread_id) VALUES(?,?,?,?,?,?,?)",
                (task_id, "fixture", "initial", "running", stamp, stamp, "thread-fixture"))
        self.harness.active_task_id = task_id
        self.harness._task_events[task_id] = [{"threadId": "thread-fixture", "turnId": None}]
        task, message, delivery = self.harness.send_message(task_id, {"text":"queued while starting", "clientMessageId":"client-1"})
        self.assertEqual(delivery, "queued")
        with self.harness._task_event_lock: self.harness._task_events[task_id][0]["turnId"] = "turn-fixture"
        self.harness.store.update(task_id, turn_id="turn-fixture")
        self.harness._steer_waiting(task_id, "turn-fixture")
        self.assertEqual(self.harness.store.message_by_client(task_id, "client-1")["status"], "steered")
        _, duplicate, delivery = self.harness.send_message(task_id, {"text":"queued while starting", "clientMessageId":"client-1"})
        self.assertEqual(delivery, "steered")
        self.assertEqual(duplicate["id"], message["id"])
        self.assertEqual(duplicate["text"], "queued while starting")
        with self.assertRaisesRegex(ValueError, "different message text"):
            self.harness.send_message(task_id, {"text":"different replay body", "clientMessageId":"client-1"})
        self.assertEqual(sum(e.get("method") == "fixture/steered" for e in self.events), 1)
        with self.assertRaisesRegex(ValueError, "expectedTurnId"):
            self.harness.send_message(task_id, {"text":"wrong turn", "clientMessageId":"client-2", "expectedTurnId":"stale"})
        self.assertIsNone(self.harness.store.message_by_client(task_id, "client-2"))

    def test_ui_payload_steers_with_server_turn_and_old_turn_id_is_not_used_while_starting(self):
        stamp = server.now()
        task_id = "ui-payload-task"
        with self.harness.store.db:
            self.harness.store.db.execute("INSERT INTO tasks(id,title,prompt,status,created_at,updated_at,thread_id,turn_id) VALUES(?,?,?,?,?,?,?,?)",
                (task_id, "fixture", "initial", "running", stamp, stamp, "thread-fixture", "old-turn"))
        self.harness.active_task_id = task_id
        self.harness._task_events[task_id] = [{"threadId":"thread-fixture", "turnId":"current-turn"}]
        _, msg, delivery = self.harness.send_message(task_id, {"text":"UI payload", "clientMessageId":"ui-client-id"})
        self.assertEqual(delivery, "steered")
        steer = next(e for e in self.events if e.get("method") == "fixture/steered")
        self.assertEqual(steer["params"]["expectedTurnId"], "current-turn")
        self.assertEqual(steer["params"]["clientUserMessageId"], "ui-client-id")
        self.assertEqual(msg["status"], "steered")

        waiting_id = "turn-opening-task"
        with self.harness.store.db:
            self.harness.store.db.execute("INSERT INTO tasks(id,title,prompt,status,created_at,updated_at,thread_id,turn_id) VALUES(?,?,?,?,?,?,?,?)",
                (waiting_id, "fixture", "initial", "running", stamp, stamp, "thread-fixture", "old-turn"))
        self.harness.active_task_id = waiting_id
        self.harness._task_events[waiting_id] = [{"threadId":"thread-fixture", "turnId":None, "previousTurnId":"old-turn"}]
        _, waiting_message, queued = self.harness.send_message(waiting_id, {"text":"wait for actual turn", "clientMessageId":"opening-client"})
        self.assertEqual(queued, "queued")
        self.assertEqual(waiting_message["status"], "queued")
        self.assertFalse(any(e.get("method") == "fixture/steered" and e["params"].get("clientUserMessageId") == "opening-client" for e in self.events))

    def test_steer_completion_race_requeues_once_on_same_thread(self):
        self.insert_running_task("steer-race")
        self.harness.store.update("steer-race", thread_id="thread-fixture", turn_id="turn-race")
        self.harness._task_events["steer-race"][0].update(threadId="thread-fixture", turnId="turn-race")
        _, message, delivery = self.harness.send_message("steer-race", {"text":"completion-race", "clientMessageId":"race-client"})
        self.assertEqual(delivery, "queued")
        deadline = time.time() + 3
        while time.time() < deadline and len(self.harness.store.messages("steer-race")) < 2: time.sleep(0.01)
        deadline = time.time() + 3
        while time.time() < deadline and self.harness.store.message_by_client("steer-race", "race-client")["status"] not in ("started", "failed"): time.sleep(0.01)
        persisted = self.harness.store.message_by_client("steer-race", "race-client")
        self.assertEqual(persisted["status"], "started")
        starts = [e for e in self.events if e.get("method") == "fixture/turn-start" and e["params"]["threadId"] == "thread-fixture"]
        self.assertEqual(len([e for e in starts if any(x.get("text") == "completion-race" for x in e["params"]["input"])]), 1)

    def test_completed_task_followup_uses_original_thread_and_persists_history(self):
        task = self.harness.start_task({"prompt":"first turn"})
        deadline = time.time() + 2
        while time.time() < deadline and self.harness.store.row(task["id"])["status"] != "completed": time.sleep(0.01)
        original_thread = self.harness.store.row(task["id"])["thread_id"]
        _, message, delivery = self.harness.send_message(task["id"], {"text":"follow up", "clientMessageId":"follow-1"})
        deadline = time.time() + 2
        while time.time() < deadline and len(self.harness.store.messages(task["id"])) < 4: time.sleep(0.01)
        row = self.harness.store.row(task["id"])
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["thread_id"], original_thread)
        history = self.harness.store.messages(task["id"])
        self.assertEqual([m["role"] for m in history], ["user", "assistant", "user", "assistant"])
        self.assertEqual(history[2]["text"], "follow up")
        self.assertEqual(history[2]["status"], "started")

    def test_reconnect_replaces_engine_only_without_active_task(self):
        self.harness.engine_path = sys.executable
        self.insert_running_task("reconnect-blocked")
        with self.assertRaisesRegex(ValueError, "active"):
            self.harness.reconnect()
        self.harness.store.update("reconnect-blocked", status="interrupted")
        self.harness.active_task_id = None
        old_engine = self.engine
        replacement = server.AppServer([sys.executable, str(self.fake_path)], self.root / "state" / "codex", lambda _: None)
        http = ThreadingHTTPServer(("127.0.0.1", 0), server.create_handler(self.harness))
        self.harness.port = http.server_port
        thread = threading.Thread(target=http.serve_forever, daemon=True); thread.start()
        base = f"http://127.0.0.1:{http.server_port}"
        request = urllib.request.Request(base + "/api/engine/reconnect", data=b"{}", method="POST", headers={
            "Authorization": "Bearer " + self.harness.capability, "Origin": base,
            "Host": f"127.0.0.1:{http.server_port}", "Content-Type": "application/json"})
        try:
            with patch("server.AppServer", return_value=replacement):
                with urllib.request.urlopen(request) as response: status = json.loads(response.read())["status"]
        finally:
            http.shutdown(); http.server_close()
        self.assertIsNotNone(old_engine.process.poll())
        self.assertTrue(self.harness.engine.connected)
        self.assertTrue(status["engine"]["connected"])

    def test_restart_marks_running_task_interrupted(self):
        h = self.harness
        task_id = "durable-task"
        stamp = server.now()
        with h.store.db:
            h.store.db.execute("INSERT INTO tasks(id,title,prompt,status,created_at,updated_at) VALUES(?,?,?,?,?,?)", (task_id, "title", "prompt", "running", stamp, stamp))
        reopened = server.Store(self.root / "state" / "harness.sqlite3")
        self.assertEqual(reopened.row(task_id)["status"], "interrupted")
        reopened.db.close()

    def test_missing_turn_status_and_nested_error_are_not_success(self):
        self.insert_running_task("missing-status")
        self.harness._engine_event({"method": "turn/completed", "params": {
            "threadId": "thread-terminal", "turn": {"id": "turn-terminal"}}})
        row = self.harness.store.row("missing-status")
        self.assertEqual(row["status"], "interrupted")
        self.assertFalse(self.harness._task_events)
        self.insert_running_task("nested-error")
        self.harness._engine_event({"method": "turn/completed", "params": {
            "threadId": "thread-terminal", "turn": {"id": "turn-terminal", "status": "failed", "error": {"message": "fixture failure"}}}})
        row = self.harness.store.row("nested-error")
        self.assertEqual(row["status"], "failed")
        self.assertIn("fixture failure", row["error"])

    def test_engine_eof_interrupts_active_task_and_clears_active_id(self):
        self.insert_running_task("engine-eof")
        self.engine.process.kill()
        deadline = time.time() + 2
        while time.time() < deadline and self.harness.store.row("engine-eof")["status"] == "running":
            time.sleep(0.01)
        self.assertEqual(self.harness.store.row("engine-eof")["status"], "interrupted")
        self.assertIsNone(self.harness.active_task_id)

    def test_http_requires_capability_and_same_origin(self):
        http = ThreadingHTTPServer(("127.0.0.1", 0), server.create_handler(self.harness))
        self.harness.port = http.server_port
        thread = threading.Thread(target=http.serve_forever, daemon=True); thread.start()
        base = f"http://127.0.0.1:{http.server_port}"
        try:
            req = urllib.request.Request(base + "/api/status", headers={"Host": f"127.0.0.1:{http.server_port}"})
            with self.assertRaises(urllib.error.HTTPError) as denied: urllib.request.urlopen(req)
            self.assertEqual(denied.exception.code, 401)
            req = urllib.request.Request(base + "/api/status", headers={"Authorization": "Bearer " + self.harness.capability,
                "Origin": "http://evil.invalid", "Host": f"127.0.0.1:{http.server_port}"})
            with self.assertRaises(urllib.error.HTTPError) as cross: urllib.request.urlopen(req)
            self.assertEqual(cross.exception.code, 403)
            req = urllib.request.Request(base + "/api/status", headers={"Authorization": "Bearer " + self.harness.capability,
                "Origin": base, "Host": f"127.0.0.1:{http.server_port}"})
            with urllib.request.urlopen(req) as response:
                result = json.loads(response.read())
                self.assertTrue(result["engine"]["connected"])
                self.assertEqual(result["app"], {"name":"DAXQO", "version":"0.6.0"})
        finally:
            http.shutdown(); http.server_close()

    def test_assistant_task_result_http_paginates_with_strict_authenticated_query(self):
        task_id = str(uuid.uuid4())
        stamp = server.now()
        result_text = "0123456789" + "x" * (9 * 1024 * 1024)
        with self.harness.store.db:
            self.harness.store.db.execute(
                "INSERT INTO tasks(id,title,prompt,status,created_at,updated_at,result,error) VALUES(?,?,?,?,?,?,?,?)",
                (task_id, "private title", "PRIVATE_PROMPT", "completed", stamp, stamp, result_text, "apiKey=PRIVATE_KEY"))
        for index in range(7):
            self.harness.store.progress(task_id, "phase", {"phase": f"phase-{index}", "summary": "PRIVATE_PROGRESS"})

        http = ThreadingHTTPServer(("127.0.0.1", 0), server.create_handler(self.harness))
        self.harness.port = http.server_port
        thread = threading.Thread(target=http.serve_forever, daemon=True); thread.start()
        base = f"http://127.0.0.1:{http.server_port}"
        headers = {"Authorization": "Bearer " + self.harness.capability,
                   "Host": f"127.0.0.1:{http.server_port}"}

        def get(query, *, auth=True):
            request_headers = dict(headers) if auth else {"Host": headers["Host"]}
            request = urllib.request.Request(base + f"/api/assistant/tasks/{task_id}" + query, headers=request_headers)
            return urllib.request.urlopen(request, timeout=3)

        try:
            with get("?offset=0") as response:
                default_page = json.loads(response.read())
            self.assertEqual(len(default_page["result"]["text"]), 2000)
            self.assertEqual(default_page["resultNextOffset"], 2000)
            with get("?offset=8&limit=8") as response:
                page = json.loads(response.read())
            self.assertEqual(page["result"], {"text": "89xxxxxx", "offset": 8, "totalLength": len(result_text),
                                               "truncated": True, "nextOffset": 16})
            self.assertEqual(page["resultNextOffset"], 16)
            self.assertEqual(page["activity"]["phase"], "phase-6")
            self.assertEqual(page["progress"][-1]["phase"], "phase-6")
            public_text = json.dumps(page)
            for private in ("PRIVATE_PROMPT", "PRIVATE_PROGRESS", "PRIVATE_KEY", "events", "liveMessage", "agents", "context"):
                self.assertNotIn(private, public_text)

            # A result larger than the former response cap remains addressable by page.
            large_offset = 9 * 1024 * 1024
            with get(f"?offset={large_offset}&limit=8000") as response:
                final = json.loads(response.read())
            self.assertEqual(len(final["result"]["text"]), 10)
            self.assertFalse(final["result"]["truncated"])
            self.assertIsNone(final["resultNextOffset"])

            for query in ("?limit=0", "?limit=8001", "?offset=-1", "?offset=x", "?limit=1&limit=2",
                          "?offset=0&limit=1&other=x", "?offset=1&", "?offset=0&limit="):
                with self.subTest(query=query):
                    with self.assertRaises(urllib.error.HTTPError) as raised:
                        get(query)
                    self.assertEqual(raised.exception.code, 400)
            with self.assertRaises(urllib.error.HTTPError) as raised:
                get("?limit=1", auth=False)
            self.assertEqual(raised.exception.code, 401)
        finally:
            http.shutdown(); http.server_close()

    def test_two_independent_tasks_respect_limit_and_retain_active_ids(self):
        self.harness.services.settings["maxConcurrentTasks"] = 2
        with patch.object(self.harness, "_run_task"):
            one = self.harness.start_task({"prompt": "one"})
            two = self.harness.start_task({"prompt": "two"})
            with self.assertRaisesRegex(ValueError, "并发任务数已达上限"):
                self.harness.start_task({"prompt": "three"})
        self.assertCountEqual(self.harness.status()["activeTaskIds"], [one["id"], two["id"]])
        self.harness.store.update(one["id"], status="completed")
        self.harness._sync_active_task_id()
        status = self.harness.status()
        self.assertEqual(status["activeTaskIds"], [two["id"]])
        self.assertEqual(status["activeTaskId"], two["id"])
        with self.assertRaisesRegex(ValueError, "provider"):
            self.harness.save_provider({"provider": "deepseek", "model": "deepseek-flash"})

    def test_http_concurrency_limit_returns_409_with_friendly_message(self):
        self.harness.services.settings["maxConcurrentTasks"] = 1
        with patch.object(self.harness, "_run_task"):
            self.harness.start_task({"prompt": "occupy capacity"})
        http = ThreadingHTTPServer(("127.0.0.1", 0), server.create_handler(self.harness))
        self.harness.port = http.server_port
        thread = threading.Thread(target=http.serve_forever, daemon=True); thread.start()
        base = f"http://127.0.0.1:{http.server_port}"
        request = urllib.request.Request(base + "/api/tasks", data=json.dumps({"prompt": "one too many"}).encode(),
            headers={"Authorization": "Bearer " + self.harness.capability, "Origin": base,
                     "Host": f"127.0.0.1:{http.server_port}", "Content-Type": "application/json"}, method="POST")
        try:
            with self.assertRaises(urllib.error.HTTPError) as response:
                urllib.request.urlopen(request, timeout=2)
            self.assertEqual(response.exception.code, 409)
            payload = json.loads(response.exception.read())
            self.assertIn("并发任务数已达上限", payload["error"])
            self.assertIn("等待", payload["error"])
        finally:
            http.shutdown(); http.server_close()

    def test_completed_agent_message_flushes_full_live_message(self):
        self.insert_running_task("live-flush")
        self.harness._engine_event({"method": "item/agentMessage/delta", "params": {
            "threadId": "thread-terminal", "turnId": "turn-terminal", "delta": "进度"}})
        full_message = "进度：正在检索资料，并整理结果。"
        self.harness._engine_event({"method": "item/completed", "params": {
            "threadId": "thread-terminal", "turnId": "turn-terminal",
            "item": {"type": "agentMessage", "text": full_message}}})
        self.assertEqual(self.harness.task_detail("live-flush")["liveMessage"], full_message)

    def test_completed_task_elapsed_uses_terminal_time_and_stays_fixed(self):
        self.insert_running_task("elapsed-fixed")
        started = server.dt.datetime.now(server.dt.timezone.utc) - server.dt.timedelta(seconds=90)
        finished = started + server.dt.timedelta(seconds=35)
        with self.harness.store.lock, self.harness.store.db:
            self.harness.store.db.execute("UPDATE tasks SET status='completed',updated_at=? WHERE id=?",
                (finished.isoformat().replace("+00:00", "Z"), "elapsed-fixed"))
            self.harness.store.db.execute("INSERT INTO progress(task_id,kind,payload,created_at) VALUES(?,?,?,?)",
                ("elapsed-fixed", "phase", json.dumps({"phase": "running"}), started.isoformat().replace("+00:00", "Z")))
        first = self.harness.task_detail("elapsed-fixed")["activity"]["elapsedSeconds"]
        time.sleep(0.05)
        second = self.harness.task_detail("elapsed-fixed")["activity"]["elapsedSeconds"]
        self.assertEqual(first, 35)
        self.assertEqual(second, first)

    def test_structured_progress_tracks_public_events_and_filters_credentials(self):
        self.insert_running_task("observable")
        self.harness.provider["apiKey"] = "fixture-secret-value"
        with patch("server.time.monotonic", side_effect=[10.0, 12.0]):
            self.harness._engine_event({"method": "item/agentMessage/delta", "params": {
                "threadId": "thread-terminal", "turnId": "turn-terminal", "delta": "Working with apiKey=fixture-"}})
            self.harness._engine_event({"method": "item/agentMessage/delta", "params": {
                "threadId": "thread-terminal", "turnId": "turn-terminal", "delta": "secret-value; checking workspace"}})
        self.harness._engine_event({"method": "item/started", "params": {
            "threadId": "thread-terminal", "turnId": "turn-terminal", "item": {"type": "commandExecution", "command": "git status --porcelain"}}})
        self.harness._engine_event({"method": "item/completed", "params": {
            "threadId": "thread-terminal", "turnId": "turn-terminal", "item": {"type": "commandExecution", "status": "completed", "exitCode": 0}}})
        self.harness._engine_event({"method": "item/started", "params": {
            "threadId": "thread-terminal", "turnId": "turn-terminal", "item": {"type": "contextCompaction", "id": "compact-1"}}})
        self.harness._engine_event({"method": "item/completed", "params": {
            "threadId": "thread-terminal", "turnId": "turn-terminal", "item": {"type": "contextCompaction", "id": "compact-1", "status": "completed"}}})
        self.harness._engine_event({"method": "item/completed", "params": {
            "threadId": "thread-terminal", "turnId": "turn-terminal", "item": {"type": "collabAgentToolCall", "status": "completed", "id": "call-1", "receiverThreadIds": ["child-thread"], "agentsStates": {"child-thread": {"status": "completed"}}, "tool": "spawnAgent"}}})
        self.harness._engine_event({"method": "thread/tokenUsage/updated", "params": {
            "threadId": "thread-terminal", "turnId": "turn-terminal", "tokenUsage": {
                "modelContextWindow": 65536,
                "last": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15, "cachedInputTokens": 1, "reasoningOutputTokens": 2},
                "total": {"inputTokens": 100, "outputTokens": 50, "totalTokens": 150, "cachedInputTokens": 4, "reasoningOutputTokens": 10}}}})
        self.harness._engine_event({"method": "thread/tokenUsage/updated", "params": {
            "threadId": "child-thread", "tokenUsage": {"modelContextWindow": 32768,
                "last": {"inputTokens": 7, "outputTokens": 3, "totalTokens": 10, "cachedInputTokens": 0, "reasoningOutputTokens": 1},
                "total": {"inputTokens": 20, "outputTokens": 10, "totalTokens": 30, "cachedInputTokens": 0, "reasoningOutputTokens": 3}}}})
        detail = self.harness.task_detail("observable")
        self.assertNotIn("fixture-secret-value", json.dumps(detail))
        self.assertIn("Working with apiKey=[redacted]; checking workspace", detail["liveMessage"])
        self.assertEqual(detail["context"]["windowTokens"], 65536)
        self.assertEqual(detail["context"]["contextWindow"], 65536)
        self.assertEqual(detail["context"]["last"]["totalTokens"], 15)
        self.assertEqual(detail["context"]["total"]["totalTokens"], 150)
        self.assertEqual(detail["context"]["compactions"], 1)
        self.assertEqual(detail["context"]["compactionAttempts"], 1)
        self.assertEqual(len(detail["agents"]), 1)
        self.assertEqual(detail["agents"][0]["id"], "child-thread")
        self.assertEqual(detail["agents"][0]["status"], "completed")
        self.assertEqual(detail["agents"][0]["context"]["total"]["totalTokens"], 30)
        events = self.harness.store.progress_rows("observable")
        self.assertIn("git", json.dumps(events))
        self.assertIn("exitCode", json.dumps(events))
        self.assertNotIn("fixture-secret-value", json.dumps(events))
        self.assertEqual(detail["progress"], events)

    def test_compaction_started_is_not_counted_as_completed_and_legacy_is_deduplicated(self):
        self.insert_running_task("compaction-count")
        started = {"threadId": "thread-terminal", "turnId": "turn-terminal",
                   "item": {"type": "contextCompaction", "id": "compact-one"}}
        self.harness._engine_event({"method": "item/started", "params": started})
        detail = self.harness.task_detail("compaction-count")
        self.assertEqual(detail["context"]["compactions"], 0)
        self.assertEqual(detail["context"]["compactionAttempts"], 1)
        self.harness._engine_event({"method": "item/completed", "params": {**started,
            "item": {"type": "contextCompaction", "id": "compact-one", "status": "completed"}}})
        self.harness._engine_event({"method": "thread/compacted", "params": {"threadId": "thread-terminal"}})
        detail = self.harness.task_detail("compaction-count")
        self.assertEqual(detail["context"]["compactions"], 1)
        compact_events = [e for e in detail["progress"] if e.get("kind") == "compact"]
        self.assertEqual([e.get("itemId") for e in compact_events if not e.get("legacy")], ["compact-one", "compact-one"])
        self.assertEqual(sum(1 for e in compact_events if e.get("legacy")), 1)
        self.insert_running_task("legacy-compaction-only")
        self.harness._task_events["legacy-compaction-only"][0]["threadId"] = "thread-legacy"
        self.harness._engine_event({"method": "thread/compacted", "params": {"threadId": "thread-legacy"}})
        self.assertEqual(self.harness.task_detail("legacy-compaction-only")["context"]["compactions"], 1)

    def test_codex_config_sets_compaction_context_output_and_agent_limits(self):
        self.harness.services.settings["maxSubagents"] = 3
        self.harness.services.settings["contextWindow"] = 16384
        self.harness.services.settings["autoCompactTokens"] = 8000
        self.harness._write_codex_config()
        config = (self.harness.codex_home / "config.toml").read_text(encoding="utf-8")
        self.assertIn("model_auto_compact_token_limit = 8000", config)
        self.assertIn("model_context_window = 16384", config)
        self.assertIn("tool_output_token_limit = 2000", config)
        self.assertIn("max_concurrent_threads_per_session = 3", config)
        self.assertIn("max_threads = 3", config)
        self.assertIn("multi_agent = true", config)
        self.assertIn("multi_agent_v2 = false", config)
        self.assertIn("max_depth = 1", config)
        self.assertIn("at most 3 subagents", config)
        # Multiline instructions must remain one correctly escaped TOML string.
        raw_prompt = next(line.split(" = ", 1)[1] for line in config.splitlines()
                          if line.startswith("developer_instructions = "))
        self.assertEqual(json.loads(raw_prompt), server.agent_instructions(3))
        self.assertIn("\n\n", json.loads(raw_prompt))
        catalog = json.loads((server.Path(__file__).resolve().parent / "models-deepseek.json").read_text(encoding="utf-8"))
        for model in catalog["models"]:
            self.assertEqual(model["multi_agent_version"], "v1")
            self.assertFalse(model["supports_search_tool"])

    def test_android_mcp_preapproval_is_limited_to_fixed_adapter_and_four_tools(self):
        self.harness._write_codex_config()
        config = (self.harness.codex_home / "config.toml").read_text(encoding="utf-8")
        self.assertIn('approval_policy = "on-request"', config)
        self.assertNotIn('approval_policy = "never"', config)
        block = config.split("[mcp_servers.daxqo_android]\n", 1)[1].split("\n[", 1)[0]
        self.assertIn("command = " + json.dumps(sys.executable), block)
        self.assertIn("args = " + json.dumps([str(Path(server.__file__).resolve().parent / "android_mcp_server.py"),
            "--state-dir", str(self.harness.state), "--port", "18883"]), block)
        self.assertIn('enabled_tools = ["android_status", "android_apps", "android_observe", "android_action"]', block)
        self.assertGreaterEqual(int(next(line.split("=", 1)[1] for line in block.splitlines()
            if line.startswith("tool_timeout_sec"))), 2 * android_mcp.HTTP_TIMEOUT)
        self.assertIn('default_tools_approval_mode = "prompt"', block)
        for tool in ("android_status", "android_apps", "android_observe", "android_action"):
            self.assertIn("[mcp_servers.daxqo_android.tools." + tool + ']\napproval_mode = "approve"', config)
        self.assertEqual(config.count('approval_mode = "approve"'), 4)
        self.assertNotIn(self.harness.capability, config)

    def test_android_mcp_progress_exposes_bounded_error_without_tool_arguments(self):
        self.insert_running_task("android-progress")
        sensitive = "secret-image-bytes capability=fixture-secret"
        error = {"ok": False, "error": {"code": "timeout", "message": "读取界面超时"}}
        item = {"type": "mcpToolCall", "server": "daxqo_android", "tool": "android_observe",
            "status": "failed", "arguments": {"text": "never expose", "apiKey": sensitive},
            "result": {"content": [{"type": "text", "text": json.dumps(error)},
                {"type": "image", "mimeType": "image/png", "data": sensitive}]}}
        self.harness._engine_event({"method": "item/completed", "params": {
            "threadId": "thread-terminal", "turnId": "turn-terminal", "item": item}})
        event = self.harness.store.progress_rows("android-progress")[-1]
        self.assertEqual(event["summary"], "读取手机界面")
        self.assertEqual(event["errorCode"], "timeout")
        self.assertIn("读取界面超时", event["error"])
        public = json.dumps(event)
        for private in ("never expose", "fixture-secret", "secret-image-bytes", "image/png"):
            self.assertNotIn(private, public)

    def test_non_android_mcp_progress_does_not_expose_android_error_details(self):
        self.insert_running_task("other-mcp-progress")
        item = {"type": "mcpToolCall", "server": "other_server", "tool": "lookup", "status": "failed",
            "arguments": {"apiKey": "fixture-secret"},
            "result": {"content": [{"type": "text", "text": json.dumps({"ok": False,
                "error": {"code": "timeout", "message": "private Android detail"}})}]}}
        self.harness._engine_event({"method": "item/completed", "params": {
            "threadId": "thread-terminal", "turnId": "turn-terminal", "item": item}})
        event = self.harness.store.progress_rows("other-mcp-progress")[-1]
        self.assertEqual(event["summary"], "外部工具")
        self.assertNotIn("errorCode", event)
        self.assertNotIn("error", event)
        public = json.dumps(event)
        self.assertNotIn("fixture-secret", public)
        self.assertNotIn("private Android detail", public)

    def test_initial_attachment_message_separates_display_text_from_native_path(self):
        body = {"prompt": ""}
        prepared = {"prompt": "本地路径：C:\\workspace\\imports\\photo.png",
                    "_displayText": "", "_attachments": [{"id": "upload-1", "name": "photo.png",
                    "path": "imports/photo.png", "size": 12, "mime": "image/png", "isImage": True, "visionSupported": False}]}
        with patch.object(self.harness.services, "prepare_body", return_value=prepared), patch.object(self.harness, "_run_task"):
            task = self.harness.start_task(body)
        self.assertEqual(task["title"], "附件：photo.png")
        message = self.harness.store.messages(task["id"])[0]
        self.assertEqual(message["text"], "")
        self.assertEqual(message["attachments"][0]["name"], "photo.png")
        with self.harness.store.lock:
            native = self.harness.store.db.execute("SELECT text FROM messages WHERE task_id=?", (task["id"],)).fetchone()[0]
        self.assertIn("C:\\workspace\\imports\\photo.png", native)

    def test_child_completion_and_thread_level_usage_do_not_finish_parent_turn(self):
        self.insert_running_task("parent-child")
        self.harness._task_buffers["parent-child"] = "Parent draft"
        self.harness._engine_event({"method": "thread/tokenUsage/updated", "params": {
            "threadId": "thread-terminal", "tokenUsage": {"modelContextWindow": 65536,
                "last": {"inputTokens": 4, "outputTokens": 2, "totalTokens": 6},
                "total": {"inputTokens": 4, "outputTokens": 2, "totalTokens": 6}}}})
        self.harness._engine_event({"method": "item/started", "params": {
            "threadId": "thread-terminal", "turnId": "turn-terminal", "item": {
                "type": "collabAgentToolCall", "id": "call-child", "tool": "spawnAgent",
                "status": "inProgress", "receiverThreadIds": ["child-thread"],
                "agentsStates": {"child-thread": {"status": "running"}}}}})
        self.harness._engine_event({"method": "item/agentMessage/delta", "params": {
            "threadId": "child-thread", "turnId": "child-turn", "delta": "child answer"}})
        self.harness._engine_request({"key": "approval-child", "id": "rpc-approval-child",
            "method": "item/commandExecution/requestApproval", "params": {
                "threadId": "child-thread", "turnId": "child-turn", "title": "Approve child command"}})
        self.assertEqual(self.harness.approvals()[0]["taskId"], "parent-child")
        self.harness._engine_event({"method": "item/completed", "params": {
            "threadId": "child-thread", "turnId": "child-turn", "item": {"type": "agentMessage", "text": "child answer"}}})
        self.harness._engine_event({"method": "turn/completed", "params": {
            "threadId": "child-thread", "turnId": "child-turn", "turn": {"id": "child-turn", "status": "completed"}}})
        row = self.harness.store.row("parent-child")
        self.assertEqual(row["status"], "running")
        self.assertIsNone(row["result"])
        self.assertEqual(self.harness._task_buffers["parent-child"], "Parent draft")
        self.harness._engine_event({"method": "turn/completed", "params": {
            "threadId": "thread-terminal", "turn": {"status": "completed"}}})
        self.assertEqual(self.harness.store.row("parent-child")["status"], "running")
        self.harness._task_buffers["parent-child"] = "Parent final answer"
        self.harness._engine_event({"method": "turn/completed", "params": {
            "threadId": "thread-terminal", "turn": {"id": "turn-terminal", "status": "completed"}}})
        row = self.harness.store.row("parent-child")
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["result"], "Parent final answer")
        self.assertEqual([m["text"] for m in self.harness.store.messages("parent-child") if m["role"] == "assistant"], ["Parent final answer"])
        detail = self.harness.task_detail("parent-child")
        self.assertEqual(detail["context"]["contextWindow"], 65536)
        self.assertEqual(detail["agents"][0]["id"], "child-thread")
        self.assertEqual(detail["agents"][0]["status"], "completed")

    def test_collab_agent_result_and_explicit_receiver_name_are_public_and_redacted(self):
        self.insert_running_task("agent-result-shape")
        self.harness.provider["apiKey"] = "fixture-secret-value"
        child_id = "agent-uuid-1234"
        self.harness._engine_event({"method": "item/completed", "params": {
            "threadId": "thread-terminal", "turnId": "turn-terminal",
            "item": {"type": "collabAgentToolCall", "status": "completed", "id": "call-real-shape",
                "receiverThreadIds": [child_id],
                "receiverAgents": [{"threadId": child_id, "nickname": "检索员", "role": "research"}],
                "agentsStates": {child_id: {"status": "completed", "message": "结论 42/LACOL; apiKey=fixture-secret-value"}}}}})
        detail = self.harness.task_detail("agent-result-shape")
        agent = next(item for item in detail["agents"] if item["id"] == child_id)
        self.assertEqual(agent["name"], "检索员")
        self.assertEqual(agent["status"], "completed")
        self.assertEqual(agent["summary"], "结论 42/LACOL; apiKey=[redacted]")
        event = next(item for item in detail["progress"] if item["kind"] == "agent")
        self.assertEqual(event["agentsStates"][child_id]["summary"], agent["summary"])
        self.assertNotIn("fixture-secret-value", json.dumps(detail))

    def test_messages_http_routes_return_durable_idempotent_conversation(self):
        self.insert_running_task("http-messages")
        self.harness.store.update("http-messages", thread_id="thread-fixture", turn_id="turn-fixture")
        self.harness._task_events["http-messages"][0].update(threadId="thread-fixture", turnId="turn-fixture")
        http = ThreadingHTTPServer(("127.0.0.1", 0), server.create_handler(self.harness))
        self.harness.port = http.server_port
        thread = threading.Thread(target=http.serve_forever, daemon=True); thread.start()
        base = f"http://127.0.0.1:{http.server_port}"
        headers = {"Authorization":"Bearer " + self.harness.capability, "Origin":base,
                   "Host":f"127.0.0.1:{http.server_port}", "Content-Type":"application/json"}
        try:
            request = urllib.request.Request(base + "/api/tasks/http-messages/messages",
                data=json.dumps({"text":"steer over HTTP", "clientMessageId":"http-client"}).encode(),
                headers=headers, method="POST")
            with urllib.request.urlopen(request, timeout=2) as response:
                posted = json.loads(response.read())
            self.assertEqual(posted["delivery"], "steered")
            request = urllib.request.Request(base + "/api/tasks/http-messages/messages", headers=headers)
            with urllib.request.urlopen(request, timeout=2) as response:
                history = json.loads(response.read())["messages"]
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["text"], "steer over HTTP")
            self.assertEqual(history[0]["status"], "steered")

            rejected = server.RpcError("fixture rejected before delivery")
            rejected.rpc_rejected = True
            retry_request = Mock(side_effect=rejected)
            with patch.object(self.engine, "request", retry_request):
                request = urllib.request.Request(base + "/api/tasks/http-messages/messages",
                    data=json.dumps({"text":"safe retry", "clientMessageId":"http-retry", "retry":False}).encode(),
                    headers=headers, method="POST")
                with self.assertRaises(urllib.error.HTTPError) as failed:
                    urllib.request.urlopen(request, timeout=2)
                self.assertEqual(failed.exception.code, 400)
            self.assertEqual(self.harness.store.message_by_client("http-messages", "http-retry")["retry_safe"], 1)

            successful_retry = Mock(return_value={})
            with patch.object(self.engine, "request", successful_retry):
                request = urllib.request.Request(base + "/api/tasks/http-messages/messages",
                    data=json.dumps({"text":"safe retry", "clientMessageId":"http-retry", "retry":True}).encode(),
                    headers=headers, method="POST")
                with urllib.request.urlopen(request, timeout=2) as response:
                    retried = json.loads(response.read())
            self.assertEqual(retried["delivery"], "steered")
            successful_retry.assert_called_once()
        finally:
            http.shutdown(); http.server_close()

    def test_workspace_file_apis_bound_paths_hash_text_and_download(self):
        workspace = self.harness.workspace
        (workspace / "smoke-test").mkdir()
        text = "验收结果\n完成。"
        text_path = workspace / "smoke-test" / "report.md"
        text_path.write_bytes(text.encode("utf-8"))
        binary = b"fixture\x00download"
        (workspace / "result.bin").write_bytes(binary)
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6e1QAAAAASUVORK5CYII=")
        (workspace / "chart.png").write_bytes(png)
        (workspace / "active.svg").write_text('<svg onload="alert(1)"></svg>', encoding="utf-8")
        (workspace / "fake.png").write_text('<html>not an image</html>', encoding="utf-8")
        private = workspace / "state" / "private"
        private.mkdir(parents=True)
        (private / "provider.json").write_text('{"apiKey":"fixture-secret"}', encoding="utf-8")
        self.harness.provider_dir = private
        http = ThreadingHTTPServer(("127.0.0.1", 0), server.create_handler(self.harness))
        self.harness.port = http.server_port
        thread = threading.Thread(target=http.serve_forever, daemon=True); thread.start()
        base = f"http://127.0.0.1:{http.server_port}"
        headers = {"Authorization": "Bearer " + self.harness.capability, "Origin": base,
                   "Host": f"127.0.0.1:{http.server_port}"}
        def get(path): return urllib.request.urlopen(urllib.request.Request(base + path, headers=headers), timeout=2)
        try:
            with get("/api/files") as response:
                listing = json.loads(response.read())
            self.assertEqual(listing["path"], "")
            self.assertTrue(any(item["path"] == "smoke-test" and item["isDirectory"] for item in listing["files"]))
            self.assertFalse(any("provider.json" in item["path"] for item in listing["files"]))
            self.assertTrue(next(item for item in listing["files"] if item["path"] == "chart.png")["isImage"])
            query = urllib.parse.urlencode({"path": "chart.png"})
            with get("/api/files/preview?" + query) as response:
                self.assertEqual(response.headers["Content-Type"], "image/png")
                self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(response.read(), png)
            with self.assertRaises(urllib.error.HTTPError) as denied:
                urllib.request.urlopen(base + "/api/files/preview?" + query, timeout=2)
            self.assertEqual(denied.exception.code, 401)
            for name in ("active.svg", "fake.png", "state/private/provider.json", "../outside.png"):
                with self.assertRaises(urllib.error.HTTPError) as denied:
                    get("/api/files/preview?" + urllib.parse.urlencode({"path": name}))
                self.assertIn(denied.exception.code, (400, 404))
            query = urllib.parse.urlencode({"path":"smoke-test/report.md"})
            with get("/api/files/text?" + query) as response:
                document = json.loads(response.read())
            self.assertEqual(document["text"], text)
            self.assertEqual(document["sha256"], server.hashlib.sha256(text.encode("utf-8")).hexdigest())
            query = urllib.parse.urlencode({"path":"result.bin"})
            with get("/api/files/download?" + query) as response:
                self.assertEqual(response.read(), binary)
                self.assertIn("attachment;", response.headers["Content-Disposition"])
                self.assertIn("filename*=UTF-8''result.bin", response.headers["Content-Disposition"])
            for route in ("/api/files?" + urllib.parse.urlencode({"path":"../state/private"}),
                          "/api/files/text?" + urllib.parse.urlencode({"path":"state/private/provider.json"}),
                          "/api/files/text?" + urllib.parse.urlencode({"path":"missing.md"})):
                with self.assertRaises(urllib.error.HTTPError) as denied: get(route)
                self.assertIn(denied.exception.code, (400, 404))
                self.assertNotIn(b"fixture-secret", denied.exception.read())
        finally:
            http.shutdown(); http.server_close()

    def test_workspace_symlink_escape_is_rejected(self):
        outside = self.root / "outside-secret.txt"
        outside.write_text("secret", encoding="utf-8")
        link = self.harness.workspace / "escape.txt"
        try: link.symlink_to(outside)
        except (OSError, NotImplementedError): self.skipTest("symlinks are not available on this host")
        with self.assertRaises(ValueError): self.harness.read_workspace_text("escape.txt")


class AndroidOwnerConsentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.h = server.Harness("not-installed", root/"state", root/"workspace", "127.0.0.1", 18880,
                                test_engine=Mock(connected=True, account={"authenticated": True}))
        stamp = server.now()
        with self.h.store.db:
            for ident in ("owner", "other"):
                self.h.store.db.execute("INSERT INTO tasks(id,title,prompt,status,created_at,updated_at,thread_id,turn_id) VALUES(?,?,?,?,?,?,?,?)",
                    (ident, "Install requested app", "owner request", "running", stamp, stamp, "thread-"+ident, "turn-1"))
        self.a = {"id":"746bdd02-1c84-4f1a-9ed4-cb880402f4ec", "status":"pending", "action":"tap",
                  "packageName":"com.android.packageinstaller", "summary":"确认安装所请求的应用", "expiresAt":int((time.time()+600)*1000)}
        self.snapshot = {"policyMode":"confirm", "controlEnabled":True, "approvals":[self.a]}
        self.bridge = Mock(); self.bridge.status.side_effect = lambda: self.snapshot
        def decide(method, path, body):
            self.a["status"] = "approved" if body["decision"] == "accept" else "declined"
            return dict(self.a)
        self.bridge.request.side_effect = decide
        self.p = patch.object(server, "AndroidBridge", return_value=self.bridge); self.p.start()
        self.h._record_android_tool("owner", self.item({"code":"approval_required", "approval":self.a}))

    @staticmethod
    def item(error):
        return {"result":{"content":[{"type":"text", "text":json.dumps({"ok":False,"error":error})}]}}

    def tearDown(self):
        self.h.close_android_permissions(); self.p.stop(); self.h.store.db.close(); self.temp.cleanup()

    def test_cross_task_cannot_claim_or_approve_the_existing_request(self):
        self.h._record_android_tool("other", self.item({"code":"approval_required", "approval":self.a}))
        self.assertEqual(self.h.android_permission_approvals("other"), [])
        with self.assertRaises(ValueError): self.h.decide_android_permission(self.a["id"], "accept", task_id="other")
        self.bridge.request.assert_not_called()
        # A shared physical operation cannot be claimed by a second task, nor
        # may that task claim completion while still waiting for consent.
        self.h._task_events["other"] = [{"threadId":"thread-other", "turnId":"turn-1"}]
        with patch.object(server.threading, "Thread"):
            self.h._engine_event({"method":"turn/completed", "params":{"threadId":"thread-other", "turnId":"turn-1", "status":"completed"}})
        self.assertEqual(self.h.store.row("other")["status"], "waiting_approval")

    def test_accept_continues_original_task_once_and_replay_adds_no_grant(self):
        self.h.store.update("owner", status="waiting_approval"); self.h.send_message = Mock()
        self.h.decide_android_permission(self.a["id"], "accept", task_id="owner")
        self.h.decide_android_permission(self.a["id"], "accept", task_id="owner")
        self.h._resume_android_approved()
        self.bridge.request.assert_called_once(); self.h.send_message.assert_called_once()
        self.assertEqual(self.h.send_message.call_args.args[0], "owner")

    def test_decline_stops_without_resuming(self):
        self.h.store.update("owner", status="waiting_approval"); self.h.send_message = Mock()
        self.h.decide_android_permission(self.a["id"], "decline")
        self.assertEqual(self.h.store.row("owner")["status"], "stopped"); self.h.send_message.assert_not_called()

    def test_native_restart_invalidates_pending_and_never_restores_consent(self):
        self.snapshot["approvals"] = []
        with self.assertRaises(ValueError): self.h.decide_android_permission(self.a["id"], "accept")
        self.bridge.request.assert_not_called()
        self.assertEqual(self.h.android_permission_approvals()[0]["status"], "stale")

    def test_stop_revokes_pending_and_prevents_later_accept(self):
        self.h.store.update("owner", status="waiting_approval"); self.h.stop("owner")
        self.assertEqual(self.a["status"], "declined")
        with self.assertRaises(ValueError): self.h.decide_android_permission(self.a["id"], "accept")

    def test_model_turn_end_preserves_waiting_instead_of_completed(self):
        self.h._task_events["owner"] = [{"threadId":"thread-owner", "turnId":"turn-1"}]
        with patch.object(server.threading, "Thread"):
            self.h._engine_event({"method":"turn/completed", "params":{"threadId":"thread-owner", "turnId":"turn-1", "status":"completed"}})
        self.assertEqual(self.h.store.row("owner")["status"], "waiting_approval")


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux process identity checks")
class ProcDiscoveryTests(unittest.TestCase):
    def test_kernel_without_task_children_uses_verified_uid_snapshot(self):
        task = MagicMock()
        task.joinpath.return_value.read_text.side_effect = FileNotFoundError()
        task.exists.return_value = True  # Android kernel lacks the children file.
        own = Mock(name="own")
        own.name = "101"
        own.stat.return_value.st_uid = 1000
        other = Mock(name="other")
        other.name = "102"
        other.stat.return_value.st_uid = 2000
        identities = {100: (100, 1000, 1, 10), 101: (101, 1000, 100, 11)}
        with patch.object(server, "Path") as paths, patch.object(server.os, "scandir") as scan, \
                patch.object(server, "_linux_process_identity", side_effect=identities.get) as identify:
            paths.return_value.iterdir.return_value = [task]
            scan.return_value.__enter__.return_value = iter([own, other])
            self.assertEqual(server._linux_descendant_identities(100), [(101, 1000, 11)])
            self.assertNotIn(unittest.mock.call(102), identify.call_args_list)


if __name__ == "__main__": unittest.main()
