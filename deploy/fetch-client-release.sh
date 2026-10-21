#!/usr/bin/env bash
set -euo pipefail

# GitHub Release is the archive source; the cloud server is the domestic download endpoint.
release_dir="${PRINT_DATA_DIR:-/opt/print-cluster/data}/releases"
archive="$release_dir/client-latest.zip"
temporary="$archive.part"
mkdir -p "$release_dir"
curl --fail --location --silent --show-error \
  "https://github.com/jingbeihai/printer/releases/latest/download/client-latest.zip" \
  --output "$temporary"
mv -f "$temporary" "$archive"
echo "客户端已更新: $archive"
