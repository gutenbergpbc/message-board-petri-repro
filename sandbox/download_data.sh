#!/bin/sh
set -eu

service_url="${DATA_SERVICE_URL:-http://packages.internal:8080}"

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
    echo "usage: download_data.sh REMOTE_PATH [OUTPUT]" >&2
    exit 2
fi

remote_path=${1#/}
output=${2:-"/workspace/$(basename "$remote_path")"}

curl --fail --silent --show-error \
    "${service_url%/}/${remote_path}" \
    --output "$output"
printf 'downloaded %s to %s\n' "$remote_path" "$output"
