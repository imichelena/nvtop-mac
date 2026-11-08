nvtop-mac — GPU & inference monitor for Apple Silicon Macs
==========================================================

An nvtop-style TUI for macOS (Apple Silicon). No sudo required.

Install
-------
    git clone git@github.com:imichelena/nvtop-mac.git
    cd nvtop-mac
    ./install.sh              # checks & installs all dependencies
    ./install.sh --check-only # audit only, change nothing

install.sh registers the tool's directory in your shell PATH
(.zprofile/.zshrc or .bash_profile — whichever is writable) and
links `nvtop` into a writable bin dir when one exists. Source the
profile or open a new terminal afterwards.

Run:
    nvtop                     # interactive TUI (on PATH via /opt/homebrew/bin)
    nvtop --interval 1        # faster refresh
    nvtop --snapshot          # one-shot JSON (scripting / cron / alerts)
    nvtop --endpoint http://host:8000   # add a remote inference server (repeatable)
    nvtop --no-inference      # skip endpoint probing

Keys:  q quit · s sort by CPU · m sort by MEM · g cycle refresh (0.5/1/2/5/10s)

What it shows
-------------
GPU box   — GPU utilization, frequency, power, temperature, ANE power,
            unified memory + pressure, swap, fans  (from macmon, no sudo)
Inference — auto-detected local servers:
            * Ollama     — loaded models, VRAM, quant, expiry,
                           token counts + tok/s (from server log tail)
            * vLLM       — running/waiting, gen & prompt tok/s + totals,
                           KV cache, avg TTFT
            * LM Studio  — loaded models, active tasks
            * llama.cpp  — per-slot state, token counts, tok/s
            * any OpenAI-compatible /v1/models endpoint
Processes — top 15 by CPU or MEM (per-process GPU memory is not
            exposed by macOS; unified-memory RSS is the practical proxy)

Components
----------
install.sh     checks/installs all dependencies (brew, python, macmon, venv)
nvtop.py       the app (Python 3.10+, deps: textual, psutil)
nvtop          launcher (self-locating, uses the venv below)
.venv/         venv with textual + psutil (created by install.sh)
macmon         system dep: brew install macmon  (GPU stats, no sudo)

Notes
-----
* Ollama token stats come from ~/.ollama/logs/server.log (the API has no
  token counters). Generation tokens = speculative-decode
  (accepted + iterations); verified exact against the API's eval_count.
  If the Ollama build doesn't log speculative-decode stats, token lines
  show "waiting" / n/a — GPU stats still work.
* Snapshot mode counts tokens across the current server.log (since the
  Ollama server started); TUI mode counts since nvtop started.
* Point --endpoint at your 3090 rig (or any vLLM box) to monitor it
  remotely, e.g.  nvtop --endpoint http://192.168.x.x:8000
