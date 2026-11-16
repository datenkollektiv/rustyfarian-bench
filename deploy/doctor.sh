#!/usr/bin/env bash
# Checks the prerequisites of the bench host path in deploy/README.md and says how to fix each.
# Run through `just doctor [--deep]`. Plain runs never decrypt, so they never prompt for the
# age key; --deep also derives your public key and decrypts the secrets to check their content.
# Secret values only ever travel through pipes; nothing here prints or stores one.
set -uo pipefail

deep=false
[ "${1:-}" = "--deep" ] && deep=true
root=$(cd "$(dirname "$0")/.." && pwd)
secrets="$root/shared-secrets"
ansible_dir="$root/deploy/ansible"
just_cmd=${JUST:-just}
# the pinned `uvx … ansible-core` command from the justfile; split on purpose when used
ansible_cmd=${ANSIBLE:?run this through just doctor}
problems=0

if [ -t 1 ]; then green=$'\033[32m' red=$'\033[31m' yellow=$'\033[33m' plain=$'\033[0m'
else green='' red='' yellow='' plain=''; fi
section() { printf '\n%s\n' "$1"; }
ok() { printf '  %s✓%s %s\n' "$green" "$plain" "$1"; }
bad() { printf '  %s✗%s %s\n      → %s\n' "$red" "$plain" "$1" "$2"; problems=$((problems + 1)); }
warn() { printf '  %s!%s %s\n      → %s\n' "$yellow" "$plain" "$1" "$2"; }
skip() { printf '  - %s\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1; }
# version_ge 3.13.3 3.10: true when the first version is at least the second
version_ge() {
    awk -v a="$1" -v b="$2" 'BEGIN { split(a, x, "."); split(b, y, ".")
        for (i = 1; i <= 3; i++) { if (x[i] + 0 > y[i] + 0) exit 0; if (x[i] + 0 < y[i] + 0) exit 1 }
        exit 0 }'
}

# sops' own default key files, in the order sops looks at them
default_key_files=(
    "${XDG_CONFIG_HOME:-$HOME/.config}/sops/age/keys.txt"
    "$HOME/Library/Application Support/sops/age/keys.txt"
)

section "Tools on this machine"
for tool in uv curl ssh python3; do
    if have "$tool"; then ok "$tool"; else bad "$tool not found" "brew install $tool"; fi
done
if have sops; then
    sops_version=$(sops --version 2>/dev/null | awk 'NR == 1 { print $2 }')
    if version_ge "$sops_version" 3.10; then ok "sops $sops_version"
    else bad "sops $sops_version is older than 3.10" "brew upgrade sops (SOPS_AGE_KEY_CMD needs 3.10)"; fi
else
    bad "sops not found" "brew install sops"
fi
if have age-keygen; then ok "age-keygen (for --deep)"
else warn "age-keygen not found; only --deep needs it" "brew install age"; fi
if [ "$(uname)" = Darwin ] && [ ! -d "/Applications/Raspberry Pi Imager.app" ]; then
    warn "Raspberry Pi Imager not in /Applications" "brew install --cask raspberry-pi-imager (step 1 only)"
fi

section "Age key"
key_source=""
if [ -n "${SOPS_AGE_KEY_CMD:-}" ]; then key_source=SOPS_AGE_KEY_CMD
elif [ -n "${SOPS_AGE_KEY_FILE:-}" ]; then key_source=SOPS_AGE_KEY_FILE
elif [ -n "${SOPS_AGE_KEY:-}" ]; then key_source=SOPS_AGE_KEY
else
    for f in "${default_key_files[@]}"; do [ -f "$f" ] && { key_source=$f; break; }; done
fi
if [ -n "$key_source" ]; then ok "key source: $key_source"
else bad "no age key source for sops" \
    "add to ~/.zshenv: export SOPS_AGE_KEY_CMD=\"sh -c 'gopass show <your gopass entry> | grep ^AGE-SECRET-KEY-'\""; fi
if [ -n "${SOPS_AGE_KEY_FILE:-}" ] && [ ! -r "$SOPS_AGE_KEY_FILE" ]; then
    bad "SOPS_AGE_KEY_FILE=$SOPS_AGE_KEY_FILE is not readable" "fix the path, or unset it"
fi

recipients=""
[ -f "$root/.sops.yaml" ] && recipients=$(grep -oE 'age1[0-9a-z]{58}' "$root/.sops.yaml" | sort -u)
# every AGE-SECRET-KEY line sops could use; only ever piped into age-keygen, never shown
secret_keys() {
    [ -n "${SOPS_AGE_KEY:-}" ] && printf '%s\n' "$SOPS_AGE_KEY"
    [ -n "${SOPS_AGE_KEY_FILE:-}" ] && [ -r "$SOPS_AGE_KEY_FILE" ] && cat "$SOPS_AGE_KEY_FILE"
    [ -n "${SOPS_AGE_KEY_CMD:-}" ] && sh -c "$SOPS_AGE_KEY_CMD"
    for f in "${default_key_files[@]}"; do [ -r "$f" ] && cat "$f"; done
    return 0
}
if ! $deep; then
    skip "your public key is a recipient in .sops.yaml (run with --deep)"
elif [ -z "$key_source" ] || ! have age-keygen; then
    skip "your public key is a recipient in .sops.yaml (needs a key source and age-keygen)"
else
    public_keys=$(secret_keys 2>/dev/null | grep '^AGE-SECRET-KEY-' | age-keygen -y 2>/dev/null | sort -u)
    match=$(comm -12 <(printf '%s\n' "$public_keys") <(printf '%s\n' "$recipients") | head -n 1)
    if [ -z "$public_keys" ]; then
        bad "the key source yields no AGE-SECRET-KEY line" "check the gopass entry; do not use \`gopass show -o\`"
    elif [ -n "$match" ]; then ok "your key ${match:0:12}… is a recipient in .sops.yaml"
    else bad "your key ${public_keys:0:12}… is not a recipient in .sops.yaml" \
        "add it to .sops.yaml, then \`just secrets-updatekeys\` from a machine that can decrypt"; fi
fi

section "SOPS files (local only, gitignored)"
recipient_count=$(printf '%s\n' "$recipients" | grep -c age1)
if [ ! -f "$root/.sops.yaml" ]; then
    bad ".sops.yaml missing" "cp deploy/sops.example.yaml .sops.yaml, then put in your age public key"
elif [ "$recipient_count" -eq 0 ]; then
    bad ".sops.yaml has no age recipient (still the template's placeholder?)" \
        "replace it with the output of \`age-keygen -y <your key file>\`"
else ok ".sops.yaml with $recipient_count recipient(s)"; fi
if check=$("$just_cmd" --justfile "$root/justfile" secrets-check 2>&1); then ok "no plaintext in shared-secrets/"
else bad "plaintext in shared-secrets/: $(printf '%s' "$check" | grep -o 'shared-secrets/[^ ]*' | tr '\n' ' ')" \
    "just encrypt <file>"; fi
for f in bench.yaml pi.env; do
    if [ -f "$secrets/$f" ]; then ok "shared-secrets/$f"
    elif [ "$f" = bench.yaml ]; then bad "shared-secrets/bench.yaml missing" "just pi-ca-init, then just secrets-edit bench.yaml"
    else bad "shared-secrets/pi.env missing" "just secrets-edit pi.env (KEY=value lines, see deploy/README.md step 2)"; fi
done

# the rules of `load_tokens` (src/rustyfarian_bench/remote.py); prints names and problems, never a token
check_tokens='
import json, re, sys
tokens, problems, seen = json.load(sys.stdin), [], set()
if not isinstance(tokens, dict) or not tokens:
    problems.append("no tokens yet")
else:
    for name, token in tokens.items():
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", str(name)):
            problems.append(f"{name!r}: name must match [A-Za-z0-9][A-Za-z0-9_-]*")
        if not isinstance(token, str):
            problems.append(f"{name}: not a string, quote it")
            continue
        if len(token) < 32:
            problems.append(f"{name}: shorter than 32 characters")
        if not (token.isascii() and token.isprintable()) or token != token.strip():
            problems.append(f"{name}: not printable ASCII, or edge spaces")
        if token in seen:
            problems.append(f"{name}: the same token as another name")
        seen.add(token)
print("; ".join(problems) if problems else ", ".join(sorted(tokens)))
sys.exit(1 if problems else 0)
'
check_seed='
import sys
env = dict(l.split("=", 1) for l in sys.stdin.read().splitlines() if "=" in l and not l.startswith("#"))
missing = [k for k in ("PI_HOSTNAME", "PI_USER", "PI_SSH_PUBKEY") if not env.get(k)]
if env.get("PI_WIFI_SSID") and len(env.get("PI_WIFI_PSK", "")) < 8:
    missing.append("PI_WIFI_PSK (8+ characters, as PI_WIFI_SSID is set)")
print(", ".join(missing))
sys.exit(1 if missing else 0)
'
if ! $deep; then
    skip "secrets decrypt, tokens and seed values are valid (run with --deep)"
elif ! have sops || [ -z "$key_source" ]; then
    skip "secrets content (needs sops and a key source)"
else
    if [ -f "$secrets/bench.yaml" ]; then
        if result=$(sops --decrypt --extract '["bench_tokens"]' --output-type json "$secrets/bench.yaml" 2>/dev/null \
            | python3 -c "$check_tokens"); then ok "bench.yaml decrypts; tokens: $result"
        else bad "bench.yaml: ${result:-does not decrypt}" "just secrets-edit bench.yaml (tokens from \`openssl rand -hex 32\`)"; fi
    fi
    if [ -f "$secrets/pi.env" ]; then
        if result=$(sops --decrypt "$secrets/pi.env" 2>/dev/null | python3 -c "$check_seed"); then
            ok "pi.env decrypts and has the seed values"
        else bad "pi.env: missing ${result:-everything, or it does not decrypt}" "just secrets-edit pi.env"; fi
    fi
fi

section "Ansible"
if [ -d "$ansible_dir/.collections/ansible_collections/community/sops" ] \
    && [ -d "$ansible_dir/.collections/ansible_collections/community/crypto" ]; then ok "collections installed"
else bad "Ansible collections missing" "just deploy-collections"; fi
host="" user="" fqdn="" port="" vars=""
if [ -f "$ansible_dir/inventory.yml" ]; then
    # shellcheck disable=SC2086  # ansible_cmd is a command line on purpose
    vars=$(cd "$ansible_dir" && $ansible_cmd ansible-inventory --host bench 2>/dev/null)
fi
if [ ! -f "$ansible_dir/inventory.yml" ]; then
    bad "deploy/ansible/inventory.yml missing" \
        "cp deploy/ansible/inventory.example.yml deploy/ansible/inventory.yml and set host, user, bench_fqdn, LAN CIDR"
elif [ -z "$vars" ]; then
    bad "inventory.yml has no host named bench" "compare it with inventory.example.yml"
else
    get() { python3 -c 'import json, sys; v = json.load(sys.stdin).get(sys.argv[1], ""); print(" ".join(v) if isinstance(v, list) else v)' "$1" <<< "$vars"; }
    host=$(get ansible_host)
    user=$(get ansible_user)
    fqdn=$(get bench_fqdn)
    port=$(get bench_port)
    cidrs=$(get bench_allowed_cidrs)
    ok "inventory: $user@$host, bench $fqdn:$port"
    [ -n "$cidrs" ] || bad "bench_allowed_cidrs is empty" "set the LAN CIDR in inventory.yml, or nobody reaches the bench port"
    if [[ "$fqdn" != *.* || "$fqdn" == *.local || "$fqdn" == *.internal ]]; then
        warn "bench_fqdn $fqdn will not pass the devcontainer firewall" "use a multi-label name, not .local or .internal"
    fi
fi

section "Raspberry Pi (read-only)"
if [ -z "$host" ]; then
    skip "DNS, SSH and the bench port (needs inventory.yml)"
else
    if ip=$(python3 -c 'import socket, sys; print(socket.gethostbyname(sys.argv[1]))' "$fqdn" 2>/dev/null); then
        ok "$fqdn resolves to $ip"
    else bad "$fqdn does not resolve" "give the Pi this name and a static DHCP lease in the router"; fi
    if ssh_err=$(ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new \
        "$user@$host" 'sudo -n true' 2>&1 >/dev/null); then
        ok "ssh $user@$host works, with passwordless sudo"
    elif [[ "$ssh_err" == *"IDENTIFICATION HAS CHANGED"* ]]; then
        bad "$host has a new host key (reflashed?)" "ssh-keygen -R $host, then rerun"
    elif [[ "$ssh_err" == *"Too many authentication failures"* ]]; then
        # the Pi allows 3 tries (MaxAuthTries in the base role); the agent offered other keys first
        bad "ssh $user@$host: too many authentication failures" \
            "name the Pi's key in ~/.ssh/config: Host $host / IdentityFile <key> / IdentitiesOnly yes"
    else bad "ssh $user@$host fails: $(printf '%s' "$ssh_err" | tail -n 1)" \
        "write the card, \`just pi-seed\`, boot; PI_SSH_PUBKEY must be your key"; fi
    url="https://$fqdn:$port/jobs/00000000-000000-000000"
    # -k: a reachability probe only; --deep verifies the certificate
    code=$(curl -sk -o /dev/null -w '%{http_code}' --connect-timeout 5 "$url" 2>/dev/null)
    case "$code" in
        401) ok "the bench answers on https://$fqdn:$port (401 without a token)" ;;
        000) bad "nothing answers on https://$fqdn:$port" "just pi-provision; check bench_allowed_cidrs covers this machine" ;;
        *) warn "https://$fqdn:$port answers $code, expected 401" "journalctl -u rustyfarian-bench on the Pi" ;;
    esac
    if [ "$code" = 401 ] && $deep && have sops && [ -f "$secrets/bench.yaml" ]; then
        ca=$(mktemp)
        trap 'rm -f "$ca"' EXIT
        if sops --decrypt --extract '["bench_ca_cert"]' "$secrets/bench.yaml" > "$ca" 2>/dev/null \
            && [ "$(curl -s -o /dev/null -w '%{http_code}' --cacert "$ca" "$url")" = 401 ]; then
            ok "its certificate verifies against the bench CA"
        else bad "its certificate does not verify against the bench CA" "just pi-provision reissues it for bench_fqdn"; fi
    fi
fi

printf '\n'
if [ "$problems" -eq 0 ]; then printf '%sAll good.%s\n' "$green" "$plain"
else printf '%s%d problem(s).%s Fix the first ✗ first; later ones often follow from it.\n' "$red" "$problems" "$plain"; fi
[ "$problems" -eq 0 ]
