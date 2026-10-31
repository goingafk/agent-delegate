#!/usr/bin/env python3
"""Delegate a prompt to a local Ollama model (default qwen3.8:27b). Stdin is appended as context."""
import argparse, json, os, sys, urllib.request

p = argparse.ArgumentParser()
p.add_argument("prompt", nargs="?", default="")
p.add_argument("--model", default=os.environ.get("QWEN_MODEL", "qwen3.8:27b"))
p.add_argument("--host", default=os.environ.get("QWEN_HOST", "http://localhost:11434"))
p.add_argument("--system", default=None)
p.add_argument("--json", action="store_true")
p.add_argument("--think", action="store_true")
p.add_argument("--timeout", type=int, default=600)
p.add_argument("--num-ctx", type=int, default=32768)
a = p.parse_args()

ctx = "" if sys.stdin.isatty() else sys.stdin.read()
user = a.prompt + (("\n\n---\n" + ctx) if ctx else "")
if not user.strip():
    sys.exit("no prompt given")
msgs = ([{"role": "system", "content": a.system}] if a.system else []) + [{"role": "user", "content": user}]
body = {"model": a.model, "messages": msgs, "stream": False, "think": a.think,
        "options": {"num_ctx": a.num_ctx}}
if a.json:
    body["format"] = "json"
req = urllib.request.Request(a.host.rstrip("/") + "/api/chat", data=json.dumps(body).encode(),
                             headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=a.timeout) as r:
        out = json.load(r)
except Exception as e:
    sys.exit(f"qwen delegate failed: {e}")
print(out.get("message", {}).get("content", "").strip())
sec = out.get("total_duration", 0) / 1e9
print(f"\n[{a.model} | {out.get('prompt_eval_count',0)} in / {out.get('eval_count',0)} out tok | {sec:.1f}s]", file=sys.stderr)
