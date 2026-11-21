#!/usr/bin/env bash
set -euo pipefail
# lint.sh — lint deploy/ and scripts/: yamllint, shellcheck, syntax check, ansible-lint, and the
# cloud-init seed rendered from fixture values. Run through `just deploy-lint`, which passes the pinned
# tool versions as ANSIBLE, ANSIBLE_CORE, ANSIBLE_LINT, YAMLLINT, SHELLCHECK, CHECK_JSONSCHEMA
# and CLOUD_INIT_SCHEMAS.

root=$(cd "$(dirname "$0")/.." && pwd)
# the pinned `uvx … ansible-core` command; split on purpose when used
ansible_cmd=${ANSIBLE:?run this through just deploy-lint}
cd "$root/deploy/ansible"

uvx "yamllint@$YAMLLINT" --strict -c ../.yamllint ..
# every shell script in the repo: deploy/ and scripts/
uvx --from "shellcheck-py==$SHELLCHECK" shellcheck ../*.sh ../../scripts/*.sh
for playbook in site.yml seed.yml ca-init.yml; do
    # shellcheck disable=SC2086
    $ansible_cmd ansible-playbook -i inventory.example.yml --syntax-check "$playbook"
done
# ansible-lint loads every builtin callback, and ansible-core 2.22 warns for its deprecated
# oneline and tree, which nothing here uses; the syntax checks above and the seed runs below
# still show deprecations in our own playbooks
ANSIBLE_DEPRECATION_WARNINGS=false \
    uvx --python 3.13 --from "ansible-lint==$ANSIBLE_LINT" --with "ansible-core==$ANSIBLE_CORE" \
    ansible-lint --profile moderate site.yml seed.yml ca-init.yml

bootfs="$root/tmp/deploy-lint/bootfs"
rm -rf "$bootfs" && mkdir -p "$bootfs"
echo "console=serial0,115200 root=PARTUUID=fixture rootwait" > "$bootfs/cmdline.txt"
: > "$bootfs/config.txt"
# read KEY=value raw, as `sops exec-env` does, so a value may hold spaces without quotes
(
    while IFS='=' read -r key value || [ -n "$key" ]; do
        [[ -z "$key" || "$key" == \#* ]] || export "$key=$value"
    done < tests/seed-fixture.env
    # twice: reseeding a card must replace its ds= entry, not add a second one
    for pass in first second; do
        echo "seed, $pass pass"
        # shellcheck disable=SC2086
        PI_BOOTFS="$bootfs" $ansible_cmd ansible-playbook -i localhost, seed.yml
    done
)
uvx "check-jsonschema@$CHECK_JSONSCHEMA" --default-filetype yaml \
    --schemafile "$CLOUD_INIT_SCHEMAS/schema-cloud-config-v1.json" "$bootfs/user-data"
# cloud-init's network v2 schema does not model `wifis` (netplan's), so these get a YAML check only
uvx "yamllint@$YAMLLINT" --strict -d "{extends: relaxed, rules: {line-length: disable}}" \
    "$bootfs/network-config" "$bootfs/meta-data"
# one id in both places, and the kernel command line still one line
id=$(sed -n 's/^instance-id: "\(.*\)"$/\1/p' "$bootfs/meta-data")
[ "$(cat "$bootfs/cmdline.txt")" = "console=serial0,115200 root=PARTUUID=fixture rootwait ds=nocloud;i=$id" ]
[ "$(wc -l < "$bootfs/cmdline.txt")" -eq 1 ]
