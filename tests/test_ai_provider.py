"""Mocked official CLI invocation contract; no live model request."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from india_trader.ai_provider import AIProviderError, CodexCLIAdapter


SPEC = {"id": "AI4", "provider": "codex_cli", "model": None,
        "timeout_seconds": 30, "max_prompt_chars": 5000}


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.calls = []

    def _run(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if command[1:3] == ["login", "status"]:
            return subprocess.CompletedProcess(command, 0, "Logged in using ChatGPT", "")
        answer = Path(command[command.index("--output-last-message") + 1])
        answer.write_text('{"ok": true}', encoding="utf-8")
        event = {"type": "turn.completed", "usage": {"input_tokens": 12,
                                                     "output_tokens": 4}}
        return subprocess.CompletedProcess(command, 0, json.dumps(event), "")

    def test_read_only_ephemeral_schema_and_no_inherited_keys(self):
        adapter = CodexCLIAdapter(Path(self.temp.name), executable="codex.cmd", run=self._run)
        with patch.dict(os.environ, {"UPSTOX_ANALYTICS_TOKEN": "secret-upstox",
                                     "FYERS_ACCESS_TOKEN": "secret-fyers",
                                     "OPENAI_API_KEY": "secret-api",
                                     "CODEX_API_KEY": "secret-codex"}):
            result = adapter.invoke(SPEC, "paper-only test", {"type": "object"})
        self.assertEqual(result["answer"], {"ok": True})
        self.assertEqual(result["usage"]["source"], "CODEX_EVENT")
        command, options = self.calls[-1]
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        for flag in ("--ephemeral", "--ignore-user-config", "--skip-git-repo-check",
                     "--output-schema", "--output-last-message", "--json"):
            self.assertIn(flag, command)
        self.assertEqual(command[-1], "-")
        self.assertEqual(options["input"], "paper-only test")
        self.assertTrue(all(key not in options["env"] for key in
                            ("UPSTOX_ANALYTICS_TOKEN", "FYERS_ACCESS_TOKEN",
                             "OPENAI_API_KEY", "CODEX_API_KEY")))

    def test_api_key_auth_is_rejected_before_model_call(self):
        def api_auth(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, "Logged in using API key", "")
        adapter = CodexCLIAdapter(Path(self.temp.name), executable="codex.cmd", run=api_auth)
        with self.assertRaises(AIProviderError) as raised:
            adapter.invoke(SPEC, "paper-only test", {"type": "object"})
        self.assertEqual(raised.exception.category, "CHATGPT_LOGIN_REQUIRED")

    def test_timeout_and_missing_cli_fail_closed(self):
        missing = CodexCLIAdapter(Path(self.temp.name), executable="notfound", run=lambda *a, **k:
                                  (_ for _ in ()).throw(subprocess.TimeoutExpired(a[0], 1)))
        with self.assertRaises(AIProviderError) as raised:
            missing.invoke(SPEC, "test", {"type": "object"})
        self.assertEqual(raised.exception.category, "CODEX_AUTH_UNAVAILABLE")

    def test_model_tool_activity_is_rejected(self):
        def tool_run(command, **kwargs):
            if command[1:3] == ["login", "status"]:
                return subprocess.CompletedProcess(command, 0, "Logged in using ChatGPT", "")
            answer = Path(command[command.index("--output-last-message") + 1])
            answer.write_text('{"ok": true}', encoding="utf-8")
            event = {"type": "item.started", "item": {"type": "command_execution"}}
            return subprocess.CompletedProcess(command, 0, json.dumps(event), "")
        adapter = CodexCLIAdapter(Path(self.temp.name), executable="codex.cmd", run=tool_run)
        with self.assertRaises(AIProviderError) as raised:
            adapter.invoke(SPEC, "paper-only test", {"type": "object"})
        self.assertEqual(raised.exception.category, "AI_TOOL_ACTIVITY_BLOCKED")


if __name__ == "__main__":
    unittest.main()
