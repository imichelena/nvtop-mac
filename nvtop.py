#!/usr/bin/env python3
"""nvtop-mac — an nvtop-style GPU & inference monitor for Apple Silicon Macs.

Data sources (no sudo needed):
  * macmon   (brew install macmon) — GPU/CPU utilization, frequency, power,
               temperature, unified memory, swap, fans, chip info
  * psutil   — per-process CPU / memory
  * inference servers: Ollama, LM Studio, vLLM, llama.cpp, any OpenAI-compatible
               /v1/models endpoint (auto-detected; add more with --endpoint)
    Token stats: Ollama (server-log tail: prompt/generation tokens, tok/s),
    vLLM (Prometheus counters: totals + tok/s, KV cache, TTFT),
    llama.cpp (per-slot token counts + tok/s).

Usage:
  nvtop                          interactive TUI
  nvtop --snapshot               one-shot JSON dump (scripting)
  nvtop --interval 1             refresh period in seconds
  nvtop --endpoint http://rig:8000   monitor an extra inference endpoint
  nvtop --no-inference           skip endpoint probing

Keys: q quit · s sort by CPU · m sort by MEM · g cycle refresh interval
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone

try:
    import psutil
except ImportError:
    psutil = None

try:
    from rich.text import Text
    from textual.app import App
    from textual.binding import Binding
    from textual.containers import Container, Horizontal
    from textual.widgets import DataTable, Footer, Label, ProgressBar, Static
    _TEXTUAL = True
except ImportError:
    _TEXTUAL = False

DEFAULT_ENDPOINTS = [
    "http://127.0.0.1:11434",  # Ollama
    "http://127.0.0.1:1234",   # LM Studio
    "http://127.0.0.1:8000",   # vLLM
    "http://127.0.0.1:8080",   # llama.cpp server
]


# ----------------------------------------------------------------- utilities

def http_get(url: str, timeout: float = 1.5) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "nvtop-mac/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def fbytes(n) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def ftemp(c) -> str:
    return f"{c:.0f}C" if c is not None else "?"


def _fans_str(fans) -> str:
    if not fans:
        return "fans n/a"
    return "fans " + ", ".join(f"{f.get('name', 'fan')} {f.get('rpm', 0):.0f} rpm" for f in fans)


def _short_err(e: Exception) -> str:
    return (str(e) or e.__class__.__name__)[:80]


def find_macmon():
    """Locate the macmon binary (PATH first, then common install dirs)."""
    p = shutil.which("macmon")
    if p:
        return p
    for cand in ("/opt/homebrew/bin/macmon", "/usr/local/bin/macmon",
                 "/opt/homebrew/opt/macmon/bin/macmon",
                 os.path.expanduser("~/.local/bin/macmon")):
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def chip_name() -> str:
    """SoC name, from sysctl (macmon's pipe output lacks the soc field)."""
    try:
        out = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"],
                             capture_output=True, text=True, timeout=2).stdout.strip()
        if out:
            return out
    except Exception:
        pass
    return "Apple Silicon"


# ------------------------------------------------------------------- macmon

class Macmon:
    """Long-running `macmon pipe` subprocess; latest sample on demand."""

    def __init__(self, interval_ms: int = 1000):
        self._exe = find_macmon()
        self.available = bool(self._exe)
        self._latest = None
        self._lock = threading.Lock()
        self._proc = None
        if self.available:
            self._proc = subprocess.Popen(
                [self._exe, "pipe", "-i", str(max(250, interval_ms))],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                sample = json.loads(line)
            except json.JSONDecodeError:
                continue
            with self._lock:
                self._latest = sample

    def latest(self):
        with self._lock:
            return self._latest

    def close(self):
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass


def macmon_once(timeout: float = 3.0):
    """One macmon sample (for --snapshot)."""
    exe = find_macmon()
    if not exe:
        return None
    p = subprocess.Popen([exe, "pipe", "-i", "1000"],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    sample = None
    try:
        start = time.time()
        while time.time() - start < timeout:
            line = p.stdout.readline()
            if not line:
                break
            line = line.strip()
            if line:
                try:
                    sample = json.loads(line)
                except json.JSONDecodeError:
                    pass
            if sample and time.time() - start > 1.0:
                break
        return sample
    finally:
        p.terminate()
        try:
            p.wait(timeout=2)
        except Exception:
            pass


# ---------------------------------------------------------------- processes

class ProcTracker:
    """Keeps psutil.Process handles so cpu_percent() has a baseline."""

    def __init__(self):
        self._procs = {}
        if psutil:
            self._prime()

    def _prime(self):
        for p in psutil.process_iter(["pid", "name"]):
            self._procs[p.pid] = p
        for p in self._procs.values():
            try:
                p.cpu_percent(None)
            except Exception:
                pass

    def sample(self):
        out, dead = [], []
        for pid, p in list(self._procs.items()):
            try:
                out.append({
                    "pid": pid,
                    "name": p.name(),
                    "cpu": p.cpu_percent(None),
                    "mem": p.memory_info().rss,
                })
            except Exception:
                dead.append(pid)
        for pid in dead:
            self._procs.pop(pid, None)
        for p in psutil.process_iter(["pid", "name"]):
            if p.pid not in self._procs:
                self._procs[p.pid] = p
                try:
                    p.cpu_percent(None)
                except Exception:
                    pass
        return out


# --------------------------------------------------------------- ollama log

class OllamaLogTracker:
    """Tails the Ollama server log for per-request token statistics.

    The Ollama HTTP API exposes no token counters, but the server log
    records enough to reconstruct them:
      * prefix-cache line        -> prompt tokens  (total=N)
      * speculative decode stats -> generated tokens
        (accepted draft tokens + one bonus token per iteration)
    """

    def __init__(self, log_dir=None, window: float = 60.0, mode: str = "session"):
        self.log_dir = log_dir or os.path.expanduser("~/.ollama/logs")
        self.window = window
        # mode: "session" = count only activity after tracker started (TUI)
        #       "tail"    = parse whole current log file (one-shot snapshots)
        self.mode = mode
        self.path = os.path.join(self.log_dir, "server.log")
        self.available = os.path.isfile(self.path)
        self._fh = None
        self._offset = 0
        self._inited = False
        self._events = []   # (ts, kind, tokens)
        self._reqs = []     # (tokens, duration_s, rate)
        self.session_gen = 0
        self.session_prompt = 0
        self.session_requests = 0
        self._last_prompt_ts = None
        self._last_prefill_done = None

    def _open(self):
        try:
            self._fh = open(self.path, "r", encoding="utf-8", errors="replace")
            if not self._inited:
                if self.mode == "session":
                    self._fh.seek(0, 2)  # count only new activity
                self._inited = True
            else:
                self._fh.seek(self._offset)
            self._offset = self._fh.tell()
        except OSError:
            self.available = False
            self._fh = None

    def poll(self):
        if not self.available:
            return
        if self._fh is None:
            self._open()
            if self._fh is None:
                return
        try:
            if os.path.getsize(self.path) < self._offset:  # rotated / truncated
                self._offset = 0
                self._fh.seek(0, 2)
                self._offset = self._fh.tell()
                return
            for line in self._fh:
                self._parse(line)
            self._offset = self._fh.tell()
        except OSError:
            pass

    def _parse(self, line):
        m = re.match(r'^time=([^\s]+)', line)
        ts = None
        if m:
            try:
                ts = datetime.fromisoformat(m.group(1)).timestamp()
            except ValueError:
                return
        msg = re.search(r'msg="([^"]+)"', line)
        msg = msg.group(1) if msg else ""
        if msg in ("cache miss", "cache hit"):
            t = re.search(r'\btotal=(\d+)', line)
            if t:
                self._last_prompt_ts = ts
                self.session_prompt += int(t.group(1))
                self._events.append((ts, "prompt", int(t.group(1))))
        elif msg == "Prompt processing progress":
            # final tick is sampled (e.g. 18/19) — any progress line marks
            # prefill completion to within a second
            self._last_prefill_done = ts
        elif msg == "speculative decode stats":
            it = re.search(r'\biterations=(\d+)', line)
            ac = re.search(r'\baccepted=(\d+)', line)
            if it and ac:
                n = int(ac.group(1)) + int(it.group(1))
                self.session_gen += n
                self.session_requests += 1
                anchor = self._last_prefill_done or self._last_prompt_ts
                dur = (ts - anchor) if anchor else None
                rate = (n / dur) if dur and dur > 0.05 else None
                self._reqs.append((n, dur, rate))
                self._reqs = self._reqs[-20:]
                self._events.append((ts, "gen", n))

    def stats(self):
        now = time.time()
        while self._events and now - self._events[0][0] > self.window:
            self._events.pop(0)
        gen_win = sum(t for _, k, t in self._events if k == "gen")
        rated = [(n, d) for n, d, r in self._reqs if d and r]
        avg_rate = (sum(n for n, _ in rated) / sum(d for _, d in rated)) if rated else None
        last = self._reqs[-1] if self._reqs else None
        return {
            "tracking": self.available,
            "session_gen_tokens": self.session_gen,
            "session_prompt_tokens": self.session_prompt,
            "session_requests": self.session_requests,
            "window_gen_tokens": gen_win,
            "window_seconds": self.window,
            "window_rate": gen_win / self.window,
            "avg_e2e_rate": round(avg_rate, 1) if avg_rate is not None else None,
            "last_request": (
                {"tokens": last[0],
                 "duration_s": round(last[1], 2) if last[1] else None,
                 "rate_tps": round(last[2], 1) if last[2] else None}
                if last else None),
        }


# ---------------------------------------------------------------- inference

class Endpoint:
    """One inference server: classify its API, poll it, track counters."""

    def __init__(self, base: str, explicit: bool = False, ollama_tracker=None):
        self.base = base.rstrip("/")
        self.explicit = explicit
        self.kind = None
        self._state = {}
        self._down_since = None
        self._last = None
        self.ollama_tracker = ollama_tracker

    def poll(self):
        now = time.time()
        if self.kind is None:
            self.kind = self._classify()
            if self.kind is None:
                if self.explicit:
                    return {"kind": "unknown", "base": self.base, "status": "offline",
                            "detail": "no recognized inference API"}
                return None
        if self._down_since and now - self._down_since < 10:
            return self._last  # back off for 10s after a failure
        try:
            result = self._poll_kind()
            self._down_since = None
        except Exception as e:
            self._down_since = now
            result = {"kind": self.kind, "base": self.base, "status": "offline",
                      "detail": _short_err(e)}
        self._last = result
        return result

    def _classify(self):
        probes = [
            ("ollama",   "/api/tags"),
            ("lmstudio", "/api/v0/models"),
            ("llamacpp", "/slots"),
            ("vllm",     "/metrics"),
            ("openai",   "/v1/models"),
        ]
        for kind, path in probes:
            try:
                body = http_get(self.base + path, timeout=0.8)
            except Exception:
                continue
            if kind == "vllm" and "vllm" not in body.lower():
                continue
            return kind
        return None

    def _poll_kind(self):
        return {
            "ollama": self._poll_ollama,
            "vllm": self._poll_vllm,
            "lmstudio": self._poll_lmstudio,
            "llamacpp": self._poll_llamacpp,
            "openai": self._poll_openai,
        }[self.kind]()

    # -- ollama
    def _poll_ollama(self):
        loaded = []
        try:
            for m in json.loads(http_get(self.base + "/api/ps", 1.0)).get("models", []):
                d = m.get("details") or {}
                loaded.append({
                    "name": m.get("name", "?"),
                    "vram": m.get("size_vram"),
                    "quant": d.get("quantization_level"),
                    "expires_at": m.get("expires_at"),
                })
        except Exception:
            pass
        avail = 0
        names = []
        try:
            tags = json.loads(http_get(self.base + "/api/tags", 1.0)).get("models", [])
            avail = len(tags)
            names = [t.get("name") for t in tags][:8]
        except Exception:
            pass
        tokens = None
        if self.ollama_tracker:
            self.ollama_tracker.poll()
            tokens = self.ollama_tracker.stats()
        return {"kind": "ollama", "base": self.base, "status": "up",
                "loaded": loaded, "available": avail, "available_names": names,
                "tokens": tokens}

    # -- vllm
    def _poll_vllm(self):
        body = http_get(self.base + "/metrics", timeout=2.0)
        m = {}
        for line in body.splitlines():
            if not line or line.startswith("#"):
                continue
            parts = line.rsplit(" ", 1)
            if len(parts) != 2:
                continue
            try:
                m[parts[0].split("{")[0]] = float(parts[1])
            except ValueError:
                pass

        def rate(key, counter):
            prev = self._state.get(key)
            r = None
            if prev is not None and counter >= prev[0]:
                dt = time.time() - prev[1]
                if dt > 0:
                    r = (counter - prev[0]) / dt
            self._state[key] = (counter, time.time())
            return r

        kv = m.get("vllm:kv_cache_usage_perc")
        if kv is not None and kv <= 1.5:
            kv *= 100
        ttft_sum = m.get("vllm:time_to_first_token_seconds_sum")
        ttft_cnt = m.get("vllm:time_to_first_token_seconds_count")
        result = {
            "kind": "vllm", "base": self.base, "status": "up",
            "running": m.get("vllm:num_requests_running", 0),
            "waiting": m.get("vllm:num_requests_waiting", 0),
            "prompt_tps": rate("prompt", m.get("vllm:prompt_tokens_total", 0)),
            "gen_tps": rate("gen", m.get("vllm:generation_tokens_total", 0)),
            "gen_total": m.get("vllm:generation_tokens_total", 0),
            "prompt_total": m.get("vllm:prompt_tokens_total", 0),
            "kv_cache_pct": kv,
            "ttft_ms": (ttft_sum / ttft_cnt * 1000) if ttft_sum and ttft_cnt else None,
        }
        try:
            result["models"] = [x.get("id") for x in json.loads(
                http_get(self.base + "/v1/models", 0.8)).get("data", [])][:6]
        except Exception:
            pass
        return result

    # -- lm studio
    def _poll_lmstudio(self):
        loaded = []
        try:
            for m in json.loads(http_get(self.base + "/api/v0/loaded", 1.0)).get("models", []):
                loaded.append({"name": m.get("id", "?"), "tasks": m.get("task_count", 0)})
        except Exception:
            pass
        ntasks, nmodels = 0, 0
        try:
            ntasks = len(json.loads(http_get(self.base + "/api/v0/tasks", 1.0)).get("tasks", []))
        except Exception:
            pass
        try:
            nmodels = len(json.loads(http_get(self.base + "/api/v0/models", 1.0)).get("models", []))
        except Exception:
            pass
        return {"kind": "lmstudio", "base": self.base, "status": "up",
                "loaded": loaded, "active_tasks": ntasks, "available": nmodels}

    # -- llama.cpp server
    def _poll_llamacpp(self):
        slots = []
        now = time.time()
        try:
            for s in json.loads(http_get(self.base + "/slots", 1.0)).get("slots", []):
                slot = {"id": s.get("id"), "state": s.get("state")}
                if s.get("state") == "processing":
                    if s.get("n_prompt_ms"):
                        slot["prompt_tps"] = s.get("n_prompt_tokens", 0) / (s["n_prompt_ms"] / 1000)
                    if s.get("n_tokens") is not None:
                        slot["gen_tokens"] = s["n_tokens"]
                        key = f"slot{s.get('id')}"
                        prev = self._state.get(key)
                        if prev and s["n_tokens"] >= prev[0]:
                            dt = now - prev[1]
                            if dt > 0:
                                slot["gen_tps"] = (s["n_tokens"] - prev[0]) / dt
                        self._state[key] = (s["n_tokens"], now)
                slots.append(slot)
        except Exception:
            pass
        return {"kind": "llamacpp", "base": self.base, "status": "up", "slots": slots}

    # -- generic openai-compatible
    def _poll_openai(self):
        models = []
        try:
            models = [x.get("id") for x in json.loads(
                http_get(self.base + "/v1/models", 1.0)).get("data", [])][:8]
        except Exception:
            pass
        return {"kind": "openai", "base": self.base, "status": "up", "models": models}


def infer_lines(info: dict):
    """Display lines for one endpoint: list of (style, text)."""
    kind = (info.get("kind") or "?").upper()
    head = f"*{kind}*  {info.get('base', '')}"
    lines = []
    if info.get("status") != "up":
        return [("dim", f"- {kind}  {info.get('base', '')}  —  offline ({info.get('detail', '')})")]
    lines.append(("green", head))
    if kind == "OLLAMA":
        loaded = info.get("loaded", [])
        if loaded:
            for m in loaded:
                exp = m.get("expires_at")
                if exp:
                    try:
                        dt = datetime.fromisoformat(exp) - datetime.now(timezone.utc)
                        secs = int(dt.total_seconds())
                        exp_s = "expiring now" if secs <= 0 else f"expires in {secs // 60}m{secs % 60:02d}s"
                    except Exception:
                        exp_s = ""
                else:
                    exp_s = "pinned"
                bits = [f"loaded {m.get('name', '?')}"]
                if m.get("vram"):
                    bits.append(f"{fbytes(m['vram'])} VRAM")
                if m.get("quant"):
                    bits.append(str(m["quant"]))
                if exp_s:
                    bits.append(exp_s)
                lines.append(("", "    " + "  ·  ".join(bits)))
        else:
            names = ", ".join(info.get("available_names", [])[:4])
            lines.append(("dim", f"    no model loaded  ·  {info.get('available', 0)} available ({names})"))
        t = info.get("tokens")
        if t:
            if not t.get("tracking"):
                lines.append(("dim", "    token stats: n/a (no ~/.ollama/logs/server.log)"))
            elif not t["session_requests"] and not t["window_gen_tokens"]:
                lines.append(("dim", "    tokens: waiting for a request this session"))
            else:
                bits = [f"{t['session_gen_tokens']} gen tok",
                        f"{t['session_prompt_tokens']} prompt tok",
                        f"{t['session_requests']} req (session)"]
                if t.get("avg_e2e_rate"):
                    bits.append(f"avg {t['avg_e2e_rate']:.1f} tok/s")
                if t["window_gen_tokens"]:
                    bits.append(f"last {t['window_seconds']:.0f}s: {t['window_gen_tokens']} tok"
                                f" ({t['window_rate']:.1f}/s)")
                lr = t.get("last_request")
                if lr and lr.get("rate_tps"):
                    bits.append(f"last req: {lr['tokens']} tok @ {lr['rate_tps']} tok/s")
                lines.append(("", "    " + "  ·  ".join(bits)))
    elif kind == "VLLM":
        bits = [f"running {info.get('running', 0)}", f"waiting {info.get('waiting', 0)}"]
        g, p = info.get("gen_tps"), info.get("prompt_tps")
        bits.append(f"gen {g:.1f} tok/s" if g is not None else "gen — tok/s")
        bits.append(f"prompt {p:.1f} tok/s" if p is not None else "prompt — tok/s")
        lines.append(("", "    " + "  ·  ".join(bits)))
        bits = [f"total {info.get('gen_total', 0):.0f} gen · {info.get('prompt_total', 0):.0f} prompt tok"]
        if info.get("kv_cache_pct") is not None:
            bits.append(f"KV cache {info['kv_cache_pct']:.0f}%")
        if info.get("ttft_ms"):
            bits.append(f"TTFT {info['ttft_ms']:.0f} ms avg")
        if info.get("models"):
            bits.append("models: " + ", ".join(info["models"][:4]))
        lines.append(("", "    " + "  ·  ".join(bits)))
    elif kind == "LMSTUDIO":
        loaded = info.get("loaded", [])
        if loaded:
            for m in loaded:
                bits = [f"loaded {m.get('name', '?')}"]
                if m.get("tasks"):
                    bits.append(f"{m['tasks']} task(s)")
                lines.append(("", "    " + "  ·  ".join(bits)))
        else:
            lines.append(("dim", f"    no model loaded  ·  {info.get('available', 0)} available  ·  {info.get('active_tasks', 0)} active task(s)"))
    elif kind == "LLAMACPP":
        slots = info.get("slots", [])
        if not slots:
            lines.append(("dim", "    no slots"))
        for s in slots:
            bits = [f"slot {s.get('id')}", str(s.get("state", "?"))]
            if s.get("prompt_tps"):
                bits.append(f"prompt {s['prompt_tps']:.0f} tok/s")
            if s.get("gen_tokens") is not None:
                bits.append(f"{s['gen_tokens']} tok")
            if s.get("gen_tps") is not None:
                bits.append(f"gen {s['gen_tps']:.1f} tok/s")
            lines.append(("", "    " + "  ·  ".join(bits)))
    else:  # OPENAI
        models = info.get("models", [])
        lines.append(("dim", "    models: " + (", ".join(models[:6]) or "none listed")))
    return lines


# ------------------------------------------------------------------ snapshot

def run_snapshot(args):
    sample = macmon_once()
    procs = []
    if psutil:
        tracker = ProcTracker()
        time.sleep(0.6)
        procs = tracker.sample()
    ollama_log = OllamaLogTracker(mode="tail")
    time.sleep(0.3)
    ollama_log.poll()
    infer = []
    for base in args.endpoints:
        ep = Endpoint(base, explicit=base not in DEFAULT_ENDPOINTS,
                      ollama_tracker=ollama_log)
        r = ep.poll()
        if r:
            infer.append(r)
    s = sample or {}
    out = {
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "chip": (s.get("soc") or {}).get("chip_name") or chip_name(),
        "gpu": {
            "util_pct": round(s.get("gpu_active_ratio", 0) * 100, 1),
            "freq_mhz": s.get("gpu_freq_mhz"),
            "power_w": round(s.get("gpu_power", 0), 2),
            "ane_power_w": round(s.get("ane_power", 0), 2),
            "temp_c": (s.get("temp") or {}).get("gpu_temp_avg"),
        },
        "cpu": {
            "util_pct": round(s.get("cpu_usage_pct", 0) * 100, 1),
            "power_w": round(s.get("cpu_power", 0), 2),
            "temp_c": (s.get("temp") or {}).get("cpu_temp_avg"),
            "e_freq_mhz": s.get("ecpu_freq_mhz"),
            "p_freq_mhz": s.get("pcpu_freq_mhz"),
        },
        "memory": s.get("memory"),
        "fans": s.get("fans"),
        "inference": infer,
        "processes_top": sorted(procs, key=lambda p: p["cpu"], reverse=True)[:15],
        "sources": {"macmon": sample is not None, "psutil": psutil is not None},
    }
    print(json.dumps(out, indent=2, default=str))


# ---------------------------------------------------------------------- TUI

if _TEXTUAL:

    class Nvtop(App):
        TITLE = "nvtop-mac"
        CSS = """
        #topbar {
            height: 1; width: 100%;
            background: $primary; color: $text;
            padding: 0 1;
        }
        #gpu-box, #infer-box, #proc-box {
            border: round $primary;
            margin: 0 1;
            padding: 0 1;
        }
        #infer-box { max-height: 12; overflow: hidden; }
        #proc-box { height: 1fr; }
        .row { height: 1; align: left middle; }
        .row .lbl { width: 5; }
        .row .val { width: 48; content-align: right middle; }
        .row ProgressBar { width: 1fr; margin: 0 1 0 0; }
        """
        BINDINGS = [
            Binding("q", "quit", "Quit"),
            Binding("s", "sort_cpu", "Sort CPU"),
            Binding("m", "sort_mem", "Sort MEM"),
            Binding("g", "cycle_interval", "Interval"),
        ]

        def __init__(self, interval: float, endpoints):
            super().__init__()
            self.interval = interval
            self._intervals = [0.5, 1, 2, 5, 10]
            self._sort = "cpu"
            self._macmon = Macmon(int(interval * 1000))
            self._procs = ProcTracker()
            self._ollama_log = OllamaLogTracker()
            self._eps = [Endpoint(b, explicit=b not in DEFAULT_ENDPOINTS,
                                  ollama_tracker=self._ollama_log) for b in endpoints]
            self._ps_cpu = psutil.cpu_percent(None) if psutil else None

        def compose(self):
            yield Static("", id="topbar")
            yield Container(
                self._row("gpu", "GPU"),
                self._row("cpu", "CPU"),
                self._row("ram", "RAM"),
                self._row("swap", "SWAP"),
                id="gpu-box",
            )
            yield Container(
                Static("", id="infer"),
                id="infer-box",
            )
            yield Container(
                DataTable(id="procs", zebra_stripes=True, show_cursor=False),
                id="proc-box",
            )
            yield Footer()

        @staticmethod
        def _row(prefix, label):
            return Horizontal(
                Label(label, classes="lbl"),
                ProgressBar(total=100, show_percentage=False, id=f"{prefix}-bar"),
                Label("", classes="val", id=f"{prefix}-val"),
                classes="row",
            )

        async def on_mount(self):
            table = self.query_one("#procs", DataTable)
            table.add_column("PID")
            table.add_column("NAME")
            table.add_column("CPU%")
            table.add_column("MEM")
            self.query_one("#proc-box", Container).border_subtitle = "per-process GPU mem not exposed by macOS"
            self.query_one("#infer-box", Container).border_title = "INFERENCE"
            self.query_one("#gpu-box", Container).border_title = "GPU"
            asyncio.create_task(self._refresh_loop())

        def on_unmount(self):
            self._macmon.close()

        async def _refresh_loop(self):
            while True:
                t0 = time.monotonic()
                data = await asyncio.to_thread(self._collect)
                self._paint(data)
                await asyncio.sleep(max(0.2, self.interval - (time.monotonic() - t0)))

        def _collect(self):
            sample = self._macmon.latest() or {}
            procs = self._procs.sample() if psutil else []
            infer = [r for ep in self._eps if (r := ep.poll())]
            if psutil and self._ps_cpu is not None:
                self._ps_cpu = psutil.cpu_percent(None)
            return {"macmon": sample, "procs": procs, "infer": infer}

        def _paint(self, d):
            s = d["macmon"]
            chip = (s.get("soc") or {}).get("chip_name") or chip_name()
            up = int(time.time() - psutil.boot_time()) if psutil else 0
            srcs = []
            if s:
                srcs.append("macmon")
            if psutil:
                srcs.append("psutil")
            if d["infer"]:
                srcs.append("inference")
            self.query_one("#topbar", Static).update(
                f"nvtop-mac  ·  {chip}  ·  up {up // 86400}d{(up % 86400) // 3600:02d}h"
                f"  ·  refresh {self.interval:g}s  ·  sources: {', '.join(srcs) or 'none'}")
            self.query_one("#gpu-box", Container).border_title = f"GPU — {chip}"

            if s:
                self._set_row("gpu", s.get("gpu_active_ratio", 0) * 100,
                              f"{s.get('gpu_freq_mhz', 0):.0f} MHz · {s.get('gpu_power', 0):.1f} W"
                              f" · {ftemp((s.get('temp') or {}).get('gpu_temp_avg'))}"
                              f" · ANE {s.get('ane_power', 0):.1f} W")
                self._set_row("cpu", s.get("cpu_usage_pct", 0) * 100,
                              f"{s.get('cpu_power', 0):.1f} W · {ftemp((s.get('temp') or {}).get('cpu_temp_avg'))}"
                              f" · E {s.get('ecpu_freq_mhz') or 0:.0f} / P {s.get('pcpu_freq_mhz') or 0:.0f} MHz")
                mem = s.get("memory") or {}
                ram_t = mem.get("ram_total") or 1
                swap_t = mem.get("swap_total") or 1
                self._set_row("ram", (mem.get("ram_usage") or 0) / ram_t * 100,
                              f"{fbytes(mem.get('ram_usage'))} / {fbytes(ram_t)}"
                              f" · pressure {mem.get('pressure_percent', '?')}%")
                self._set_row("swap", (mem.get("swap_usage") or 0) / swap_t * 100,
                              f"{fbytes(mem.get('swap_usage'))} / {fbytes(swap_t)}"
                              f" · {_fans_str(s.get('fans'))}")
            elif psutil:
                vm = psutil.virtual_memory()
                sw = psutil.swap_memory()
                self._set_row("gpu", None, "n/a — install: brew install macmon")
                self._set_row("cpu", self._ps_cpu or 0, f"{psutil.cpu_count(logical=True)} logical cores")
                self._set_row("ram", vm.percent, f"{fbytes(vm.used)} / {fbytes(vm.total)}")
                self._set_row("swap", sw.percent, f"{fbytes(sw.used)} / {fbytes(sw.total)}")
            else:
                for k in ("gpu", "cpu", "ram", "swap"):
                    self._set_row(k, None, "n/a")

            text = Text()
            if not d["infer"]:
                text.append("  no inference endpoints detected (ollama 11434 · lm studio 1234 · vllm 8000 · llama.cpp 8080)",
                            style="dim")
                text.append("\n")
            for info in d["infer"]:
                for style, line in infer_lines(info):
                    text.append(line, style=style)
                    text.append("\n")
            self.query_one("#infer", Static).update(text)

            table = self.query_one("#procs", DataTable)
            table.clear()
            key = (lambda p: p["cpu"]) if self._sort == "cpu" else (lambda p: p["mem"])
            for p in sorted(d["procs"], key=key, reverse=True)[:15]:
                table.add_row(str(p["pid"]), p["name"][:40], f"{p['cpu']:.1f}", fbytes(p["mem"]))

        def _set_row(self, prefix, pct, valtext):
            if pct is None:
                self.query_one(f"#{prefix}-bar", ProgressBar).update(progress=0)
            else:
                pct = max(0.0, min(100.0, float(pct)))
                self.query_one(f"#{prefix}-bar", ProgressBar).update(progress=pct)
            val = self.query_one(f"#{prefix}-val", Label)
            if pct is not None and pct >= 90:
                val.update(Text(valtext, style="bold red"))
            else:
                val.update(valtext)

        def action_sort_cpu(self):
            self._sort = "cpu"

        def action_sort_mem(self):
            self._sort = "mem"

        def action_cycle_interval(self):
            i = self._intervals.index(self.interval) + 1 if self.interval in self._intervals else 2
            self.interval = self._intervals[i % len(self._intervals)]


def run_tui(args):
    if not _TEXTUAL:
        sys.exit("textual is required for the TUI (pip install textual); "
                 "--snapshot works without it")
    Nvtop(args.interval, args.endpoints).run()


# --------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(
        description="nvtop-mac — GPU & inference monitor for Apple Silicon",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage:")[1] if "Usage:" in __doc__ else None)
    ap.add_argument("--snapshot", action="store_true", help="print one JSON sample and exit")
    ap.add_argument("--interval", type=float, default=2.0, help="refresh interval in seconds (default 2)")
    ap.add_argument("--endpoint", action="append", default=None, metavar="URL",
                    help="extra inference endpoint (repeatable); defaults probe Ollama/LM Studio/vLLM/llama.cpp")
    ap.add_argument("--no-inference", action="store_true", help="skip inference endpoint probing")
    args = ap.parse_args()

    endpoints = list(args.endpoint or [])
    if not args.no_inference:
        for b in DEFAULT_ENDPOINTS:
            if b not in endpoints:
                endpoints.append(b)
    args.endpoints = endpoints

    if args.snapshot:
        run_snapshot(args)
        return
    run_tui(args)


if __name__ == "__main__":
    main()
