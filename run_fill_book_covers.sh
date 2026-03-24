#!/usr/bin/env bash
# 同じディレクトリの .env に RAKUTEN_APP_ID などを置くと読み込みます（リポジトリにコミットしないこと）。
set -euo pipefail
load_env_safely() {
  local env_file="$1"
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
    if [[ "$line" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
      key="${BASH_REMATCH[1]}"
      value="${BASH_REMATCH[2]}"
      value="${value%\"}"
      value="${value#\"}"
      value="${value%\'}"
      value="${value#\'}"
      export "$key=$value"
    fi
  done < "$env_file"
}
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "${SCRIPT_DIR}/.env" ]]; then
  load_env_safely "${SCRIPT_DIR}/.env"
fi
cd "${SCRIPT_DIR}"
exec python3 fill_book_covers.py "$@"
