#!/usr/bin/env bash
set -euo pipefail

# Supervisor mounts /data owned by root. Hand it to pwuser so we can run
# unprivileged for the rest of the container's life.
chown -R pwuser:pwuser /data 2>/dev/null || true
chmod 750 /data

exec gosu pwuser /run.sh
