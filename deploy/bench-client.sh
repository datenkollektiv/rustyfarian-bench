#!/usr/bin/env bash
set -euo pipefail
# bench-client.sh — talk to the provisioned bench: URL from the inventory, CA and token from bench.yaml.
# Usage: deploy/bench-client.sh check [TOKEN_NAME]
#        deploy/bench-client.sh submit REPO BOARD EXAMPLE [SUBMIT_ARGS...]
#        deploy/bench-client.sh boards [BOARDS_ARGS...]
# Run through `just pi-check` / `just pi-submit` / `just pi-boards`, which pass ANSIBLE and JUST.
# The token only ever travels through pipes and submit's environment, never argv or the terminal.

root=$(cd "$(dirname "$0")/.." && pwd)
secrets="$root/shared-secrets/bench.yaml"
just_cmd=${JUST:-just}
# the pinned `uvx … ansible-core` command from the justfile; split on purpose when used
ansible_cmd=${ANSIBLE:?run this through just pi-check, pi-submit or pi-boards}

usage() { sed -n '3,6s/^# //p' "$0" >&2; exit 2; }
[ $# -ge 1 ] || usage
command=$1
shift

# shellcheck disable=SC2086
vars=$(cd "$root/deploy/ansible" && $ansible_cmd ansible-inventory --host bench)
get() { python3 -c 'import json, sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1" <<< "$vars"; }
first_token() {
    sops --decrypt --extract '["bench_tokens"]' --output-type json "$secrets" \
        | python3 -c 'import json, sys; print(sorted(json.load(sys.stdin))[0])'
}
token() { sops --decrypt --extract "[\"bench_tokens\"][\"$1\"]" "$secrets"; }
ca=$(mktemp)   # the public CA certificate, not a secret
trap 'rm -f "$ca"' EXIT
sops --decrypt --extract '["bench_ca_cert"]' "$secrets" > "$ca"
url="https://$(get bench_fqdn):$(get bench_port)"

case "$command" in
    check)
        name=${1:-}
        if [ -z "$name" ]; then
            name=$(first_token)
        # serve's NAME and NAME_MAX (src/rustyfarian_bench/remote.py)
        elif [[ ! "$name" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$ ]]; then
            echo "a token name matches [A-Za-z0-9][A-Za-z0-9_-]*, 80 characters at most, not: $name" >&2
            exit 2
        fi
        probe="$url/jobs/00000000-000000-000000"
        code=$(curl -sS -o /dev/null -w '%{http_code}' --cacert "$ca" "$probe")
        echo "without a token: $code"
        [ "$code" = 401 ]
        code=$(token "$name" | sed 's/^/Authorization: Bearer /' \
            | curl -sS -o /dev/null -w '%{http_code}' --cacert "$ca" -H @- "$probe")
        echo "with token $name: $code"
        [ "$code" = 404 ]
        # the address Ansible uses; a first-seen host key is accepted, as the doctor does
        ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new "$(get ansible_user)@$(get ansible_host)" \
            sudo -u bench -H rustyfarian-bench detect
        ;;
    submit)
        [ $# -ge 3 ] || usage
        # decrypted first, so a failing sops stops here instead of submitting without a token
        value=$(token "$(first_token)")
        RUSTYFARIAN_BENCH_URL="$url" RUSTYFARIAN_BENCH_CA="$ca" RUSTYFARIAN_BENCH_TOKEN="$value" \
            "$just_cmd" --justfile "$root/justfile" submit "$@"
        ;;
    boards)
        value=$(token "$(first_token)")
        RUSTYFARIAN_BENCH_URL="$url" RUSTYFARIAN_BENCH_CA="$ca" RUSTYFARIAN_BENCH_TOKEN="$value" \
            "$just_cmd" --justfile "$root/justfile" boards "$@"
        ;;
    *)
        usage
        ;;
esac
