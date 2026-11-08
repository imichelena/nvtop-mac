#!/usr/bin/env bash
# install.sh — nvtop-mac dependency checker / installer (no sudo required)
#
#   ./install.sh               check & install everything that is missing
#   ./install.sh --check-only  audit only, change nothing
#
# Handles:
#   * macOS + Apple Silicon check
#   * Homebrew (required; only reports if missing, since its installer needs sudo)
#   * Python >= 3.10
#   * macmon (brew) — GPU / power / thermal / memory stats
#   * .venv with textual + psutil
#   * nvtop launcher on PATH
#   * inference server probes (informational): Ollama / LM Studio / vLLM / llama.cpp
set -euo pipefail

PREFIX="$(cd "$(dirname "$0")" && pwd)"
CHECK_ONLY=0
[ "${1:-}" = "--check-only" ] && CHECK_ONLY=1

GREEN='\033[32m'; YELLOW='\033[33m'; RED='\033[31m'; NC='\033[0m'
ok()   { printf "${GREEN}  [ ok ]${NC} %s\n" "$1"; }
warn() { printf "${YELLOW}  [warn]${NC} %s\n" "$1"; }
fail() { printf "${RED}  [FAIL]${NC} %s\n" "$1"; FAILS=$((FAILS+1)); }

FAILS=0
BREW_OK=0

echo "== nvtop-mac installer (check-only: $CHECK_ONLY) =="
echo "   prefix: $PREFIX"
echo

# --- 1. platform ----------------------------------------------------------
echo "-- Platform"
if [ "$(uname -s)" != "Darwin" ]; then
    fail "macOS required (detected $(uname -s))"
    exit 1
fi
ok "macOS $(sw_vers -productVersion 2>/dev/null || echo unknown)"
if [ "$(uname -m)" = "arm64" ]; then
    ok "Apple Silicon"
else
    fail "Apple Silicon required (detected $(uname -m)) — macmon is arm64-only"
fi

# --- 2. homebrew ----------------------------------------------------------
echo "-- Homebrew"
if command -v brew >/dev/null 2>&1; then
    ok "brew $(brew --version | head -1) at $(command -v brew)"
    BREW_OK=1
else
    fail "Homebrew not found — install it manually (its installer needs sudo):"
    echo '         /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
fi

# --- 3. python ------------------------------------------------------------
echo "-- Python (>= 3.10)"
PY=""
for cand in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if command -v "$cand" >/dev/null 2>&1 \
       && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
        PY="$(command -v "$cand")"
        break
    fi
done
if [ -n "$PY" ]; then
    ok "$("$PY" -V 2>&1) at $PY"
else
    if [ $CHECK_ONLY -eq 1 ]; then
        fail "no python3 >= 3.10 (fix: brew install python)"
    elif [ $BREW_OK -eq 1 ]; then
        warn "no suitable python3 — installing via Homebrew"
        brew install python
        PY="/opt/homebrew/bin/python3"
        ok "$("$PY" -V 2>&1) installed"
    else
        fail "no python3 >= 3.10 and no Homebrew to install it with"
    fi
fi

# --- 4. macmon -------------------------------------------------------------
echo "-- macmon (GPU stats)"
MACMON="$(command -v macmon 2>/dev/null || true)"
if [ -z "$MACMON" ]; then
    for c in /opt/homebrew/bin/macmon /usr/local/bin/macmon \
             /opt/homebrew/opt/macmon/bin/macmon "$HOME/.local/bin/macmon"; do
        if [ -x "$c" ]; then MACMON="$c"; break; fi
    done
fi
if [ -n "$MACMON" ]; then
    ok "macmon $("$MACMON" --version 2>/dev/null | head -1) at $MACMON"
else
    if [ $CHECK_ONLY -eq 1 ]; then
        fail "macmon not found (fix: brew install macmon)"
    elif [ $BREW_OK -eq 1 ]; then
        warn "macmon missing — installing via Homebrew"
        brew install macmon
        MACMON="$(command -v macmon 2>/dev/null || echo /opt/homebrew/bin/macmon)"
        ok "macmon $("$MACMON" --version 2>/dev/null | head -1) installed"
    else
        fail "macmon missing and no Homebrew to install it with"
    fi
fi

# --- 5. venv + python deps ---------------------------------------------------
echo "-- Python environment (textual, psutil)"
VENV="$PREFIX/.venv"
if [ ! -x "$VENV/bin/python" ]; then
    if [ $CHECK_ONLY -eq 1 ]; then
        warn "venv missing (would create $VENV)"
    elif [ -n "$PY" ]; then
        warn "creating venv at $VENV"
        "$PY" -m venv "$VENV"
        ok "venv created"
    else
        fail "venv missing and no python available to create it"
    fi
fi
if [ -x "$VENV/bin/python" ]; then
    if "$VENV/bin/python" -c 'import textual, psutil' 2>/dev/null; then
        ok "textual + psutil importable"
    elif [ $CHECK_ONLY -eq 1 ]; then
        warn "textual/psutil missing from venv (would pip install)"
    else
        warn "installing textual + psutil into venv"
        "$VENV/bin/python" -m pip install --quiet --upgrade pip
        "$VENV/bin/python" -m pip install --quiet textual psutil
        ok "textual + psutil installed"
    fi
else
    if [ $CHECK_ONLY -eq 0 ]; then
        fail "venv unusable at $VENV"
    fi
fi

# --- 6. launcher -------------------------------------------------------------
echo "-- Launcher"
LAUNCHER="$PREFIX/nvtop"
if [ $CHECK_ONLY -eq 1 ]; then
    if [ -x "$LAUNCHER" ]; then
        ok "launcher present: $LAUNCHER"
    else
        warn "launcher missing (would generate it)"
    fi
else
    cat > "$LAUNCHER" <<'EOF'
#!/bin/bash
# nvtop-mac launcher (self-locating; follows symlinks)
src="$0"
while [ -L "$src" ]; do
    dir="$(cd "$(dirname "$src")" && pwd)"
    src="$(readlink "$src")"
    [ "${src#/}" != "$src" ] || src="$dir/$src"
done
DIR="$(cd "$(dirname "$src")" && pwd)"
exec "$DIR/.venv/bin/python" "$DIR/nvtop.py" "$@"
EOF
    chmod +x "$LAUNCHER"
    ok "launcher written: $LAUNCHER"
fi
LINKED=0
for bin in /opt/homebrew/bin /usr/local/bin "$HOME/.local/bin"; do
    if [ -d "$bin" ] && [ -w "$bin" ]; then
        if [ $CHECK_ONLY -eq 0 ]; then
            ln -sf "$LAUNCHER" "$bin/nvtop"
        fi
        ok "on PATH via $bin/nvtop"
        LINKED=1
        break
    fi
done
if [ $LINKED -eq 0 ]; then
    if [ $CHECK_ONLY -eq 1 ]; then
        warn "no writable bin dir found (would need a manual PATH link for $LAUNCHER)"
    else
        fail "no writable bin dir found — link $LAUNCHER into your PATH manually"
    fi
fi

# PATH: register the tool directory itself in the user's shell profile (idempotent)
# zsh reads .zprofile (login) before .zshrc (interactive); prefer a writable one.
PROFILE_CANDS=""
if [ -n "${ZSH_VERSION:-}" ] || [ "$(basename "${SHELL:-/bin/zsh}")" = "zsh" ]; then
    PROFILE_CANDS="$HOME/.zprofile $HOME/.zshrc"
elif [ -n "${BASH_VERSION:-}" ]; then
    PROFILE_CANDS="$HOME/.bash_profile $HOME/.bashrc"
else
    PROFILE_CANDS="$HOME/.profile"
fi
PROFILE=""
for c in $PROFILE_CANDS; do
    if [ -f "$c" ] && [ -w "$c" ]; then PROFILE="$c"; break; fi
done
if [ -z "$PROFILE" ]; then
    for c in $PROFILE_CANDS; do
        if [ ! -e "$c" ]; then PROFILE="$c"; break; fi
    done
fi
if [ -z "$PROFILE" ]; then
    if [ $CHECK_ONLY -eq 1 ]; then
        warn "no writable shell profile found (would need manual PATH entry for $PREFIX)"
    else
        fail "no writable shell profile found — add $PREFIX to your PATH manually"
    fi
elif grep -qF "$PREFIX" "$PROFILE" 2>/dev/null; then
    ok "tool directory already on PATH ($PROFILE)"
elif [ $CHECK_ONLY -eq 1 ]; then
    warn "would add $PREFIX to PATH in $PROFILE"
else
    printf '\nexport PATH="%s:$PATH"  # nvtop-mac\n' "$PREFIX" >> "$PROFILE"
    ok "added $PREFIX to PATH in $PROFILE"
    warn "run 'source $PROFILE' or open a new terminal to pick it up"
fi

# --- 7. inference servers (informational) -------------------------------------
echo "-- Inference servers (optional — auto-detected at runtime)"
probe() {
    if curl -s -m 1 -o /dev/null "$2" 2>/dev/null; then
        ok "$1 detected at $2"
    else
        warn "$1 not detected at $2 (optional)"
    fi
}
probe "Ollama"    "http://127.0.0.1:11434/api/version"
probe "LM Studio" "http://127.0.0.1:1234/v1/models"
probe "vLLM"      "http://127.0.0.1:8000/v1/models"
probe "llama.cpp" "http://127.0.0.1:8080/v1/models"
if [ -f "$HOME/.ollama/logs/server.log" ]; then
    ok "Ollama server log present (source of token stats)"
else
    warn "no ~/.ollama/logs/server.log (Ollama token stats unavailable)"
fi

# --- 8. smoke test --------------------------------------------------------------
echo "-- Smoke test"
if [ $CHECK_ONLY -eq 0 ] && [ -x "$VENV/bin/python" ]; then
    if OUT=$("$VENV/bin/python" "$PREFIX/nvtop.py" --snapshot --no-inference 2>&1); then
        ok "nvtop --snapshot works"
    else
        fail "nvtop --snapshot failed: $(printf '%s' "$OUT" | head -c 160)"
    fi
fi

echo
if [ $FAILS -gt 0 ]; then
    echo "Done with $FAILS problem(s) — see the [FAIL] lines above."
    exit 1
fi
echo "All good — run 'nvtop' to open the TUI."
