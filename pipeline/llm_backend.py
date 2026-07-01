"""
LLM backend abstraction — one ``complete_json`` call, two ways to satisfy it.

The Phase 3 estimators need a Claude completion that returns a JSON object. There
are two ways to get one, and this module picks whichever is available:

  1. **API** — the ``anthropic`` SDK with an ``ANTHROPIC_API_KEY``. Fast, supports
     native json_schema structured output. The intended path for full-auto.
  2. **CLI** — headless ``claude -p --model <m> --output-format json``, which runs
     through the user's existing Claude Code subscription with NO api key. Slower
     (each call carries Claude Code's system prompt) and rate-limited by the
     subscription, but lets the pipeline use Sonnet with nothing to configure.

Selection (env ``PIPELINE_LLM_BACKEND`` = auto|api|cli|off; default auto):
    auto -> API if a key is set, else CLI if the ``claude`` binary exists, else off.

Both return a parsed ``dict`` (validated against ``required`` keys) or ``None`` on
any failure — callers must handle None and degrade, never crash.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from typing import Optional

# Map our full model ids to the CLI's short aliases.
_CLI_ALIAS = {
    "claude-sonnet-4-6": "sonnet",
    "claude-opus-4-8": "opus",
    "claude-haiku-4-5": "haiku",
}

CLI_TIMEOUT = int(os.environ.get("PIPELINE_LLM_CLI_TIMEOUT", "150"))

# Local model (Ollama, OpenAI/native API on localhost). The 24/7 default: free,
# unlimited, private. Runs Qwen3-30B-A3B on the GPU. Every Claude model id maps to
# the one local model — local has no model tiers, just thinking on/off per call.
LOCAL_HOST = os.environ.get("PIPELINE_LOCAL_HOST", "http://localhost:11434")
LOCAL_MODEL = os.environ.get("PIPELINE_LOCAL_MODEL", "qwen3:30b")
LOCAL_TIMEOUT = int(os.environ.get("PIPELINE_LOCAL_TIMEOUT", "300"))
# Which Claude model ids should run with local "thinking" mode on (better for the
# adversarial critic / decision roles; slower). Generation stays fast (no think).
_LOCAL_THINK_MODELS = {"claude-opus-4-8"}

_local_ok_cache: Optional[bool] = None


def _local_available() -> bool:
    global _local_ok_cache
    if _local_ok_cache is not None:
        return _local_ok_cache
    try:
        import requests
        r = requests.get(f"{LOCAL_HOST}/api/version", timeout=3)
        _local_ok_cache = r.status_code == 200
    except Exception:
        _local_ok_cache = False
    return _local_ok_cache


def backend() -> str:
    """Resolve the active backend: 'local', 'api', 'cli', or 'off'."""
    choice = os.environ.get("PIPELINE_LLM_BACKEND", "auto").lower()
    if choice == "off":
        return "off"
    if choice == "local":
        return "local" if _local_available() else "off"
    if choice == "api":
        return "api" if os.getenv("ANTHROPIC_API_KEY") else "off"
    if choice == "cli":
        return "cli" if shutil.which("claude") else "off"
    # auto: prefer the free/unlimited local model, then API, then CLI.
    if _local_available():
        return "local"
    if os.getenv("ANTHROPIC_API_KEY"):
        return "api"
    if shutil.which("claude"):
        return "cli"
    return "off"


def available() -> bool:
    return backend() != "off"


def _backend_available(name: str) -> bool:
    """Is a SPECIFIC backend usable right now? (for per-role routing overrides)."""
    if name == "local":
        return _local_available()
    if name == "api":
        return bool(os.getenv("ANTHROPIC_API_KEY"))
    if name == "cli":
        return shutil.which("claude") is not None
    return False


def describe() -> str:
    b = backend()
    return {
        "local": f"local model via Ollama ({LOCAL_MODEL} @ {LOCAL_HOST}, free/unlimited)",
        "api": "Anthropic API (ANTHROPIC_API_KEY)",
        "cli": "Claude Code CLI (claude -p, subscription auth, no key)",
        "off": "no LLM backend (start Ollama, or set ANTHROPIC_API_KEY, or install Claude Code)",
    }[b]


def _extract_json(text: str) -> Optional[dict]:
    """Pull a JSON object out of model text that may have fences / prose around it."""
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def _via_api(model: str, system: str, user: str, schema: dict, max_tokens: int) -> Optional[dict]:
    import anthropic
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=model, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"format": {"type": "json_schema", "schema": schema}},
    )
    text = next((b.text for b in resp.content if b.type == "text"), "{}")
    return json.loads(text)


def _via_cli(model: str, system: str, user: str, schema: dict, max_tokens: int) -> Optional[dict]:
    alias = _CLI_ALIAS.get(model, "sonnet")
    prompt = (
        f"{system}\n\n{user}\n\n"
        "Respond with ONLY a single JSON object (no markdown, no commentary) "
        f"matching exactly this JSON schema:\n{json.dumps(schema)}"
    )
    exe = shutil.which("claude") or "claude"
    cmd = [exe, "-p", "--model", alias, "--output-format", "json"]
    try:
        proc = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True,
            timeout=CLI_TIMEOUT, encoding="utf-8",
        )
    except (FileNotFoundError, OSError):
        # Windows npm shim is claude.cmd — needs the shell to resolve/execute.
        # Quote alias defensively (it's whitelisted today, but never interpolate
        # an unquoted value into a shell string).
        joined = f'"{exe}" -p --model "{alias}" --output-format json'
        proc = subprocess.run(
            joined, input=prompt, capture_output=True, text=True,
            timeout=CLI_TIMEOUT, encoding="utf-8", shell=True,
        )
    if proc.returncode != 0 or not proc.stdout:
        return None
    try:
        env = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    if env.get("is_error"):
        return None
    return _extract_json(env.get("result", ""))


def _via_local(model: str, system: str, user: str, schema: dict, max_tokens: int) -> Optional[dict]:
    import requests
    think = model in _LOCAL_THINK_MODELS  # critic/decision roles reason harder
    payload = {
        "model": LOCAL_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "format": schema,                       # Ollama structured outputs (JSON schema)
        "think": think,
        "options": {
            "temperature": 0.2,
            # Thinking tokens count against num_predict and can run long (Qwen often
            # emits thousands); give ample room so the JSON answer is never starved.
            "num_predict": 12000 if think else max_tokens,
        },
    }
    r = requests.post(f"{LOCAL_HOST}/api/chat", json=payload, timeout=LOCAL_TIMEOUT)
    r.raise_for_status()
    content = (r.json().get("message") or {}).get("content", "")
    return _extract_json(content)


def complete_json(
    model: str, system: str, user: str, schema: dict, *, max_tokens: int = 1024,
    required: Optional[list] = None, use_backend: Optional[str] = None,
) -> Optional[dict]:
    """Get a JSON completion. Returns None on any failure or missing required keys.

    Per-role routing: pass ``use_backend`` ("cli" / "api" / "local") to force a
    specific backend for this call (e.g. route the thesis critic to "cli" for
    Opus-via-subscription while the bulk scan stays on the auto-selected local
    model). Falls back to the auto-selected backend if the requested one isn't
    available.
    """
    b = use_backend if (use_backend and _backend_available(use_backend)) else backend()
    if b == "off":
        return None
    try:
        if b == "local":
            d = _via_local(model, system, user, schema, max_tokens)
        elif b == "api":
            d = _via_api(model, system, user, schema, max_tokens)
        else:  # cli
            d = _via_cli(model, system, user, schema, max_tokens)
    except Exception as exc:  # noqa: BLE001 — degrade, never crash the scan
        print(f"    (llm backend [{b}] error: {exc})")
        return None
    if d is None:
        return None
    if required and not all(k in d for k in required):
        return None
    return d
