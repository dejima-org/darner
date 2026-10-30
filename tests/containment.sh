#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Brad Anderson
#
# Run from inside a dejima session on the dejima host, when darner-web or its
# proxy runs there. A sandbox can route to exactly one host address, the
# bridge gateway, so a front door that answers there opens inward: that's
# what this checks can't happen. docs/web.md says how to launch the session.
#
#   bash containment.sh --gateway-control <port> [port ...]
#   bash containment.sh --control <host:port> [port ...]
#
# dejima's egress clamp refuses every gateway port unless the session was
# launched with EGRESS_HOST_PORTS naming it, so in an ordinary session every
# probe here fails, whatever the bind. Only a session launched with
# EGRESS_HOST_PORTS covering the checked ports tests the bind.
#
# --gateway-control is the control on the same path: a port on the gateway
# you've made answer for the check, and opened through EGRESS_HOST_PORTS.
# It answering proves the session can reach the gateway, so a port that
# then doesn't answer is down to its bind. --control, a host the session
# may reach elsewhere, only rules out a dead network or a missing tool.
#
# The ports default to darner-web's 8081 and a proxy's 443. Exits 0 if every
# port is unreachable, 1 if any answers, 2 if the control fails or is
# missing. GATEWAY overrides the address checked, for this script's own test.
set -u

usage() { echo "usage: $0 (--gateway-control <port> | --control <host:port>) [port ...]" >&2; exit 2; }
probe() { timeout 5 bash -c "cat < /dev/null > /dev/tcp/$1/$2" 2>/dev/null; }

[ $# -ge 2 ] || usage
kind=$1 control=$2
shift 2
case "$kind" in --gateway-control|--control) ;; *) usage ;; esac
ports=("$@")
[ ${#ports[@]} -gt 0 ] || ports=(8081 443)

gateway=${GATEWAY:-$(ip route | awk '/^default/ { print $3; exit }')}
[ -n "$gateway" ] || { echo "no default route, so no gateway to check" >&2; exit 2; }

if [ "$kind" = --gateway-control ]; then
    if ! probe "$gateway" "$control"; then
        echo "gateway control failed: nothing answered on $gateway:$control. Either nothing listens there, or the session wasn't launched with EGRESS_HOST_PORTS including $control" >&2
        exit 2
    fi
    echo "ok: the gateway control, $gateway:$control, answers, so the gateway is reachable on opened ports"
else
    if ! probe "${control%:*}" "${control##*:}"; then
        echo "control failed: $control didn't answer, so a refusal below would prove nothing" >&2
        exit 2
    fi
    echo "ok: the control, $control, answers"
    echo "note: without --gateway-control, a refusal below may be dejima's egress clamp rather than the bind; see docs/web.md"
fi

failed=0
for port in "${ports[@]}"; do
    if probe "$gateway" "$port"; then
        echo "BAD: $gateway:$port answers from inside the sandbox; fix the bind before going further"
        failed=1
    else
        echo "ok: $gateway:$port doesn't answer"
    fi
done
exit $failed
