"""Model backends behind one interface:

    call(backend, model, effort, system, user, schema, timeout) -> Reply

CLI backends (`claude_cli`, `codex_cli`) run every call as a fresh, stateless subprocess in an empty temp
directory with tools disabled, a replaced system prompt and schema-enforced output. They authenticate through
the CLI's own login. API backends read a key from the environment and refuse to run unless ARENA_ALLOW_API=1,
so that paid API use is always an explicit choice.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

API_BACKENDS = ("anthropic_api", "openai_api", "gemini_api")


class RateLimited(Exception):
    def __init__(self, retry_after: float, detail: str = ""):
        super().__init__(f"rate limited, retry after {retry_after:.0f}s: {detail[:200]}")
        self.retry_after = retry_after


class CallFailed(Exception):
    pass


@dataclasses.dataclass
class Reply:
    obj: dict | None          # parsed JSON, None if the reply was not JSON
    text: str                 # raw text of the reply
    latency_ms: int
    tokens_in: int | None = None
    tokens_out: int | None = None       # visible output tokens
    tokens_reasoning: int | None = None  # thinking tokens, when the backend reports them
    tokens_cached: int | None = None
    usage: dict | None = None           # backend's raw usage record
    meta: dict | None = None            # backend-specific extras (CLI version, flags, reported cost)


_versions: dict[str, str] = {}


def cli_version(backend: str) -> str:
    if backend not in _versions:
        exe = {"claude_cli": "claude", "codex_cli": "codex"}.get(backend)
        v = ""
        if exe and shutil.which(exe):
            try:
                v = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                v = "unknown"
        _versions[backend] = v
    return _versions[backend]


def _rate_limit_from_text(text: str) -> float | None:
    t = (text or "").lower()
    keys = ("usage limit", "rate limit", "hit your limit", "limit reached", "too many requests", " 429",
            "quota", "try again in", "resets at")
    if not any(k in t for k in keys):
        return None
    m = re.search(r"\|(\d{10})\b", text)
    if m:
        return max(30.0, int(m.group(1)) - time.time() + 30)
    m = re.search(r"try again in (?:(\d+)\s*h\w*)?\s*(?:(\d+)\s*m\w*)?\s*(?:(\d+)\s*s)?", t)
    if m and any(m.groups()):
        h, mi, s = (int(g or 0) for g in m.groups())
        return h * 3600 + mi * 60 + s + 30
    return 15 * 60.0


def _run(cmd, stdin_text, timeout, cwd, extra_env=None):
    env = {k: v for k, v in os.environ.items()
           if k not in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "CLAUDECODE",
                        "CLAUDE_CODE_ENTRYPOINT")}
    env.update(extra_env or {})
    try:
        return subprocess.run(cmd, input=stdin_text, capture_output=True, text=True, timeout=timeout,
                              cwd=cwd, env=env)
    except subprocess.TimeoutExpired:
        raise CallFailed(f"timeout after {timeout}s")


def parse_json(text: str | None) -> dict | None:
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    for candidate in (text, (re.search(r"\{.*\}", text, re.S) or [None])[0]):
        if candidate:
            try:
                obj = json.loads(candidate)
                return obj if isinstance(obj, dict) else None
            except json.JSONDecodeError:
                pass
    return None


def _flags(cmd):
    """The command line minus prompt and schema text, for the call log."""
    out, skip = [], False
    for c in cmd:
        if skip:
            skip = False
            continue
        if c in ("--system-prompt", "--json-schema"):
            out.append(c + "=<elided>")
            skip = True
            continue
        out.append(c if len(c) < 120 else c[:40] + "...")
    return " ".join(out)


def _claude_cli(model, effort, system, user, schema, timeout):
    with tempfile.TemporaryDirectory(prefix="arena-") as d:
        cmd = ["claude", "-p", "--model", model, "--output-format", "json",
               "--json-schema", json.dumps(schema), "--system-prompt", system,
               "--tools", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
               "--safe-mode", "--setting-sources", "", "--no-session-persistence", "--disable-slash-commands"]
        if effort and effort != "off":
            cmd += ["--effort", effort]
        t0 = time.time()
        p = _run(cmd, user, timeout, d, {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"})
        lat = int((time.time() - t0) * 1000)
        out = p.stdout.strip()
        try:
            res = json.loads(out.splitlines()[-1]) if out else {}
        except json.JSONDecodeError:
            res = {}
        text = res.get("result", "") if isinstance(res, dict) else ""
        if p.returncode != 0 or res.get("is_error"):
            ra = _rate_limit_from_text(f"{text}\n{p.stderr}\n{out[-2000:]}")
            if ra is not None or res.get("api_error_status") == 429:
                raise RateLimited(ra or 900.0, (text or p.stderr)[:500])
            raise CallFailed(f"claude exit {p.returncode}: {(text or p.stderr or out)[:300]}")
        obj = res.get("structured_output")
        if not isinstance(obj, dict):
            obj = parse_json(text)
        u = res.get("usage", {}) or {}
        tin = (u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
               + u.get("cache_read_input_tokens", 0))
        return Reply(obj, text or json.dumps(obj), lat, tin, u.get("output_tokens"), None,
                     u.get("cache_read_input_tokens"), u,
                     {"cli_version": cli_version("claude_cli"), "flags": _flags(cmd),
                      "models_reported": list((res.get("modelUsage") or {}).keys()),
                      "list_cost_usd": res.get("total_cost_usd"), "num_turns": res.get("num_turns")})


_CODEX_DISABLED = ("shell_tool", "unified_exec", "multi_agent", "code_mode_host", "apps", "plugins",
                   "image_generation", "hooks", "goals", "browser_use", "computer_use", "view_image",
                   "skill_search", "tool_suggest", "shell_snapshot")


def _codex_cli(model, effort, system, user, schema, timeout):
    with tempfile.TemporaryDirectory(prefix="arena-") as d:
        work = os.path.join(d, "w")
        os.mkdir(work)
        sf, inf, of = (os.path.join(d, n) for n in ("schema.json", "instructions.md", "last.txt"))
        with open(sf, "w") as f:
            json.dump(schema, f)
        with open(inf, "w") as f:
            f.write(system)
        cmd = ["codex", "exec", "-m", model, "-s", "read-only", "--skip-git-repo-check", "--ephemeral",
               "--ignore-user-config", "--ignore-rules", "--json", "--output-schema", sf, "-o", of, "-C", work,
               "-c", f"model_instructions_file={inf}", "-c", "approval_policy=never", "-c", "web_search=disabled",
               "-c", f"model_reasoning_effort={effort if effort and effort != 'off' else 'low'}"]
        for feat in _CODEX_DISABLED:
            cmd += ["--disable", feat]
        cmd.append("-")
        t0 = time.time()
        p = _run(cmd, user, timeout, work)
        lat = int((time.time() - t0) * 1000)
        events = []
        for line in p.stdout.splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        errors = [e for e in events if e.get("type") in ("error", "turn.failed")]
        err_text = " ".join(json.dumps(e) for e in errors) + " " + p.stderr[-2000:]
        tools = [e["item"]["type"] for e in events if e.get("type") == "item.completed"
                 and e.get("item", {}).get("type") not in ("agent_message", "reasoning", "error")]
        last = open(of).read().strip() if os.path.exists(of) else ""
        if p.returncode != 0 or not last:
            ra = _rate_limit_from_text(err_text)
            if ra is not None:
                raise RateLimited(ra, err_text[:500])
            raise CallFailed(f"codex exit {p.returncode}: {err_text[:300]}")
        u = next((e.get("usage", {}) for e in reversed(events) if e.get("type") == "turn.completed"), {})
        return Reply(parse_json(last), last, lat, u.get("input_tokens"), u.get("output_tokens"),
                     u.get("reasoning_output_tokens"), u.get("cached_input_tokens"), u,
                     {"cli_version": cli_version("codex_cli"), "flags": _flags(cmd), "tool_items": tools})


def _http_json(url, payload, headers, timeout):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        if e.code == 429:
            raise RateLimited(float(e.headers.get("retry-after") or 60), body[:300])
        raise CallFailed(f"HTTP {e.code}: {body[:300]}")
    except (urllib.error.URLError, TimeoutError) as e:
        raise CallFailed(str(e))


def _key(var):
    if os.environ.get("ARENA_ALLOW_API") != "1":
        raise CallFailed("API backends are disabled; set ARENA_ALLOW_API=1 to allow paid API calls")
    key = os.environ.get(var)
    if not key:
        raise CallFailed(f"{var} is not set")
    return key


MAX_OUTPUT_TOKENS = 8192


def _anthropic_api(model, effort, system, user, schema, timeout):
    key = _key("ANTHROPIC_API_KEY")
    payload = {"model": model, "max_tokens": MAX_OUTPUT_TOKENS,
               "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
               "messages": [{"role": "user", "content": user}],
               "output_config": {"format": {"type": "json_schema", "schema": schema}}}
    if effort and effort != "off":
        payload["output_config"]["effort"] = effort
    t0 = time.time()
    r = _http_json("https://api.anthropic.com/v1/messages", payload,
                   {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                   timeout)
    text = "".join(b.get("text", "") for b in r.get("content", []) if b.get("type") == "text")
    u = r.get("usage", {})
    tin = u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
    return Reply(parse_json(text), text, int((time.time() - t0) * 1000), tin, u.get("output_tokens"), None,
                 u.get("cache_read_input_tokens"), u, {})


def _openai_api(model, effort, system, user, schema, timeout):
    key = _key("OPENAI_API_KEY")
    payload = {"model": model, "instructions": system, "input": user, "store": False,
               "max_output_tokens": MAX_OUTPUT_TOKENS,
               "text": {"format": {"type": "json_schema", "name": "decision", "schema": schema, "strict": True}}}
    if effort and effort != "off":
        payload["reasoning"] = {"effort": effort}
    t0 = time.time()
    r = _http_json("https://api.openai.com/v1/responses", payload,
                   {"authorization": f"Bearer {key}", "content-type": "application/json"}, timeout)
    text = "".join(c.get("text", "") for o in r.get("output", []) if o.get("type") == "message"
                   for c in o.get("content", []))
    u = r.get("usage", {})
    return Reply(parse_json(text), text, int((time.time() - t0) * 1000), u.get("input_tokens"),
                 u.get("output_tokens"), (u.get("output_tokens_details") or {}).get("reasoning_tokens"),
                 (u.get("input_tokens_details") or {}).get("cached_tokens"), u, {})


def _gemini_api(model, effort, system, user, schema, timeout):
    key = _key("GEMINI_API_KEY")
    gen = {"responseMimeType": "application/json", "responseJsonSchema": schema,
           "maxOutputTokens": MAX_OUTPUT_TOKENS}
    if effort and effort != "off":
        gen["thinkingConfig"] = {"thinkingLevel": effort}
    payload = {"systemInstruction": {"parts": [{"text": system}]},
               "contents": [{"role": "user", "parts": [{"text": user}]}], "generationConfig": gen}
    t0 = time.time()
    r = _http_json(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent", payload,
                   {"x-goog-api-key": key, "content-type": "application/json"}, timeout)
    cands = r.get("candidates", [])[:1]
    text = "".join(p.get("text", "") for c in cands for p in c.get("content", {}).get("parts", [])
                   if not p.get("thought"))
    u = r.get("usageMetadata", {})
    meta = {"finish_reason": cands[0].get("finishReason") if cands else None}
    return Reply(parse_json(text), text, int((time.time() - t0) * 1000), u.get("promptTokenCount"),
                 u.get("candidatesTokenCount"), u.get("thoughtsTokenCount"), u.get("cachedContentTokenCount"),
                 u, meta)


BACKENDS = {"claude_cli": _claude_cli, "codex_cli": _codex_cli, "anthropic_api": _anthropic_api,
            "openai_api": _openai_api, "gemini_api": _gemini_api}


def call(backend, model, effort, system, user, schema, timeout=180) -> Reply:
    if backend not in BACKENDS:
        raise ValueError(f"unknown backend {backend!r}")
    return BACKENDS[backend](model, effort, system, user, schema, timeout)
