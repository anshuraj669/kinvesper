# Sourced by the scripts: exports KEY=VALUE lines from .env, without overriding variables already set
# (so `LLM_PROVIDER=none make dev` still wins). Comments and blank lines are skipped.
load_env() {
  local file="${1:-.env}" line key value
  [[ -f "$file" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || continue
    key="${BASH_REMATCH[1]}" value="${BASH_REMATCH[2]}"
    [[ -n "${!key+set}" ]] && continue
    value="${value%%[[:space:]]#*}"                  # a trailing " # comment"
    value="${value#"${value%%[![:space:]]*}"}"       # surrounding spaces
    value="${value%"${value##*[![:space:]]}"}"
    [[ "$value" =~ ^\"(.*)\"$ || "$value" =~ ^\'(.*)\'$ ]] && value="${BASH_REMATCH[1]}"
    export "$key=$value"
  done < "$file"
}
