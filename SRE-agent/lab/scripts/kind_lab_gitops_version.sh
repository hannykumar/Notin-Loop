#!/usr/bin/env bash
set -euo pipefail

version="${1:-}"
manifest_path="${SRE_LAB_GITOPS_MANIFEST:-data/gitops_repo/clusters/sre-lab/demo-api.json}"

case "${version}" in
  bad-release|known-good) ;;
  *)
    echo "usage: $0 {bad-release|known-good}" >&2
    exit 2
    ;;
esac

if [[ ! -f "${manifest_path}" ]]; then
  echo "GitOps fixture not found: ${manifest_path}" >&2
  exit 1
fi

temporary_file="$(mktemp)"
trap 'rm -f "${temporary_file}"' EXIT
jq --arg version "${version}" '
  .spec.template.spec.containers[0].image =
    ((.spec.template.spec.containers[0].image | split("@")[0] | sub(":[^/:]+$"; "")) + ":" + $version)
' "${manifest_path}" >"${temporary_file}"
mv "${temporary_file}" "${manifest_path}"
trap - EXIT
