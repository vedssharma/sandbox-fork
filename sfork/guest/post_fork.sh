#!/usr/bin/env bash
# sfork-guest post-fork
# Runs inside a freshly forked VM to undo the sameness a memory fork creates (PLAN.md section 4.7).
# Idempotent and best-effort: each step logs and continues on failure. Prints "ready" at the end.
set -u
SUDO=""
if [ "$(id -u)" != "0" ] && command -v sudo >/dev/null 2>&1; then SUDO="sudo -n"; fi
log() { echo "[sfork-guest] $*" >&2; }

# 1. machine-id: systemd, dbus and some telemetry key off it.
if [ -w /etc/machine-id ] || [ -n "$SUDO" ]; then
  if command -v systemd-machine-id-setup >/dev/null 2>&1; then
    $SUDO rm -f /etc/machine-id && $SUDO systemd-machine-id-setup >/dev/null 2>&1 || log "machine-id: setup failed"
  else
    cat /proc/sys/kernel/random/uuid | tr -d '-' | $SUDO tee /etc/machine-id >/dev/null || log "machine-id: write failed"
  fi
fi

# 2. Kernel RNG: mix fresh entropy so forks don't share a pool state. Writing to /dev/urandom
#    mixes without crediting entropy, which is what we want.
{ date +%s%N; cat /proc/sys/kernel/random/uuid; head -c 64 /dev/urandom; } | $SUDO tee /dev/urandom >/dev/null 2>&1 \
  || log "rng: reseed failed"

# 3. Hostname: optional, only when the orchestrator passes SFORK_HOSTNAME.
if [ -n "${SFORK_HOSTNAME:-}" ]; then
  $SUDO hostname "$SFORK_HOSTNAME" 2>/dev/null || log "hostname: failed"
fi

# 4. Clock: nudge time sync after resume, if a sync daemon is present.
if command -v chronyc >/dev/null 2>&1; then $SUDO chronyc makestep >/dev/null 2>&1 || true; fi
if command -v timedatectl >/dev/null 2>&1; then $SUDO timedatectl set-ntp true >/dev/null 2>&1 || true; fi

# 5. Project hooks: services that hold connections (e.g. app pools) can drop scripts here.
if [ -d /etc/sfork/post-fork.d ]; then
  for f in /etc/sfork/post-fork.d/*; do [ -x "$f" ] && "$f" || true; done
fi

echo ready
