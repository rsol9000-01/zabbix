#!/bin/sh
# ---------------------------------------------------------------------------
# Count the host's established TCP connections.
# Read the host's IPv4 and IPv6 tables, mounted in the agent, and count
# connections whose TCP state is 01 (ESTABLISHED), without requiring additional
# privileges or nsenter.
# ---------------------------------------------------------------------------
awk 'FNR>1 && $4=="01"' /proc/1/net/tcp /proc/1/net/tcp6 | wc -l