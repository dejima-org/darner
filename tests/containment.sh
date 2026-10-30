#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Brad Anderson
#
# Run from inside a dejima session on the dejima host, when darner-web or its
# proxy runs there. A sandbox can route to exactly one host address, the
# bridge gateway, so a front door that answers there opens inward: that's
# what this checks can't happen.
#
#   bash containment.sh <reachable host:port> [port ...]
#
# The first argument is the positive control: somewhere the session is
# allowed to reach, such as its model's API on 443. Without it, "nothing
# answered" is just as well explained by a dead network or a missing tool.
# The ports default to darner-web's 8081 and a proxy's 443.
#
# Exits 0 if every port is unreachable, 1 if any answers, 2 if the control
# fails. GATEWAY overrides the address checked, for this script's own test.
set -u

[ $# -ge 1 ] || { echo "usage: $0 <reachable host:port> [port ...]" >&2; exit 2; }
control=$1
shift
ports=("$@")
[ ${#ports[@]} -gt 0 ] || ports=(8081 443)

probe() { timeout 5 bash -c "cat < /dev/null > /dev/tcp/$1/$2" 2>/dev/null; }

gateway=${GATEWAY:-$(ip route | awk '/^default/ { print $3; exit }')}
[ -n "$gateway" ] || { echo "no default route, so no gateway to check" >&2; exit 2; }

if ! probe "${control%:*}" "${control##*:}"; then
    echo "positive control failed: $control didn't answer, so a refusal below would prove nothing" >&2
    exit 2
fi
echo "ok: the control, $control, answers"

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
