#!/usr/bin/env bash
set -euo pipefail
# secrets-check.sh — fail on any file in the secrets directory that is not SOPS-encrypted, or
# whose format this cannot check; needs no age key. Run through `just secrets-check`.
# Usage: deploy/secrets-check.sh <secrets-dir>

dir=${1:?usage: secrets-check.sh <secrets-dir>}
name=${dir##*/}
shopt -s nullglob dotglob
bad=0
for f in "$dir"/*; do
    n=${f##*/}
    if [ ! -f "$f" ]; then
        echo "UNEXPECTED: $name/$n is not a regular file" >&2; bad=1; continue
    fi
    # encrypted dotenv carries a sops_version= line, JSON a top-level "sops" object, YAML a sops: key
    case "$n" in
        *.env) grep -q '^sops_version=' "$f" ;;
        *.json) grep -q '"sops"[[:space:]]*:' "$f" ;;
        *.yaml | *.yml) grep -q '^sops:' "$f" ;;
        *) echo "UNSUPPORTED: $name/$n: only .env, .json, .yaml and .yml can be checked" >&2
           bad=1; continue ;;
    esac || { echo "PLAINTEXT: $name/$n is not encrypted; run 'just encrypt $n'" >&2; bad=1; }
done
exit "$bad"
