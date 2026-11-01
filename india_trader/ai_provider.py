"""Bounded, read-only Codex CLI model adapter for paper research.

The subprocess receives only a whitelisted environment and a data-only prompt.
Its working directory is an isolated temporary folder, not the repository.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


class AIProviderError(RuntimeError):
    def __init__(self, category):
        self.category = category
        super().__init__(category)


ALLOWED_ENV = ("PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC",
               "USERPROFILE", "APPDATA", "LOCALAPPDATA", "HOME", "CODEX_HOME",
               "TEMP", "TMP", "LANG", "CODEX_CA_CERTIFICATE", "SSL_CERT_FILE")


def _clean_env():
    """Preserve local CLI login paths, never pass broker or API credentials."""
    return {key: os.environ[key] for key in ALLOWED_ENV if key in os.environ}


def _estimate_tokens(text):
    return max(1, math.ceil(len(text) / 4))


def _usage_from_events(output):
    latest = None
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(event, dict) or event.get("type") != "turn.completed":
            continue
        usage = event.get("usage")
        if isinstance(usage, dict) and isinstance(usage.get("input_tokens"), int) and \
                isinstance(usage.get("output_tokens"), int) and \
                usage["input_tokens"] >= 0 and usage["output_tokens"] >= 0:
            latest = (usage["input_tokens"], usage["output_tokens"])
    return latest


def _no_tool_activity(output):
    """Reject outputs from any invocation that used an agent tool."""
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(event, dict) or not str(event.get("type", "")).startswith("item."):
            continue
        item = event.get("item") or {}
        if not isinstance(item, dict) or item.get("type") not in ("agent_message", "reasoning"):
            raise AIProviderError("AI_TOOL_ACTIVITY_BLOCKED")


class CodexCLIAdapter:
    """One fresh Codex invocation per role; never resumes another role's session."""

    def __init__(self, runtime_dir="trader/state/ai-runtime", executable=None, run=None):
        self.runtime_dir = Path(runtime_dir)
        self.executable = executable or shutil.which("codex.exe") or shutil.which("codex")
        self.run = run or subprocess.run

    def _auth(self, env):
        if not self.executable:
            raise AIProviderError("CODEX_CLI_MISSING")
        try:
            result = self.run([self.executable, "login", "status"],
                              capture_output=True, text=True, timeout=10, env=env,
                              check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AIProviderError("CODEX_AUTH_UNAVAILABLE") from exc
        status = (result.stdout or "") + " " + (result.stderr or "")
        if result.returncode != 0 or re.search(r"api[ -]?key", status, re.I) or \
                not re.search(r"chatgpt", status, re.I):
            raise AIProviderError("CHATGPT_LOGIN_REQUIRED")

    def invoke(self, spec, prompt, schema, max_answer_chars=12000):
        if not self.executable:
            raise AIProviderError("CODEX_CLI_MISSING")
        if not isinstance(prompt, str) or not prompt or len(prompt) > spec["max_prompt_chars"]:
            raise AIProviderError("PROMPT_LIMIT")
        provider = spec["provider"]
        if provider not in ("codex_cli", "ollama", "lmstudio"):
            raise AIProviderError("UNSUPPORTED_PROVIDER")
        env = _clean_env()
        if provider == "codex_cli":
            self._auth(env)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="committee-", dir=self.runtime_dir) as temp:
            scratch = Path(temp)
            schema_path = scratch / "schema.json"
            answer_path = scratch / "answer.json"
            schema_path.write_text(json.dumps(schema, allow_nan=False), encoding="utf-8")
            command = [self.executable, "exec", "--sandbox", "read-only",
                       "--ephemeral", "--ignore-user-config", "--skip-git-repo-check",
                       "--json", "--cd", str(scratch),
                       "--config", 'approval_policy="never"',
                       "--output-schema", str(schema_path),
                       "--output-last-message", str(answer_path)]
            if provider != "codex_cli":
                command.extend(["--oss", "--local-provider", provider])
            if spec.get("model"):
                command.extend(["--model", spec["model"]])
            command.append("-")
            try:
                result = self.run(command, input=prompt, capture_output=True, text=True,
                                  timeout=spec["timeout_seconds"], env=env, cwd=scratch,
                                  check=False)
            except subprocess.TimeoutExpired as exc:
                raise AIProviderError("AI_TIMEOUT") from exc
            except OSError as exc:
                raise AIProviderError("AI_PROCESS_UNAVAILABLE") from exc
            if result.returncode != 0:
                diagnostic = ((result.stderr or "") + " " + (result.stdout or "")).lower()
                category = ("AI_USAGE_LIMIT" if any(word in diagnostic for word in
                           ("usage limit", "rate limit", "quota", "too many requests"))
                            else "AI_PROCESS_FAILED")
                raise AIProviderError(category)
            _no_tool_activity(result.stdout or "")
            if not answer_path.is_file() or answer_path.stat().st_size > max_answer_chars:
                raise AIProviderError("AI_OUTPUT_LIMIT_OR_MISSING")
            try:
                raw = answer_path.read_text(encoding="utf-8")
                answer = json.loads(raw)
            except (UnicodeError, ValueError) as exc:
                raise AIProviderError("AI_INVALID_JSON") from exc
            if not isinstance(answer, dict):
                raise AIProviderError("AI_INVALID_JSON")
            counted = _usage_from_events(result.stdout or "")
            input_tokens, output_tokens = (counted if counted is not None else
                                           (_estimate_tokens(prompt), _estimate_tokens(raw)))
            return {"answer": answer, "usage": {"input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "source": "CODEX_EVENT" if counted is not None else "LOCAL_ESTIMATE"}}
