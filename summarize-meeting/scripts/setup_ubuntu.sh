#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage: bash scripts/setup_ubuntu.sh [--models all|diarization|ocr|none]

Prepare Summarize Meeting after copying it to Ubuntu 22.04 or WSL2.
This script never runs sudo. If OS packages are missing, it prints the
required apt command and exits.

Options:
  --models VALUE  Models to prepare (default: all)
  -h, --help      Show this help
EOF
}

models="all"
while (($# > 0)); do
  case "$1" in
    --models)
      if (($# < 2)); then
        echo "ERROR: --models requires a value" >&2
        exit 2
      fi
      models="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

case "$models" in
  all|diarization|ocr|none) ;;
  *)
    echo "ERROR: --models must be all, diarization, ocr, or none" >&2
    exit 2
    ;;
esac

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "ERROR: this setup script must be run on Linux" >&2
  exit 1
fi

architecture="$(uname -m)"
if [[ "$architecture" != "x86_64" ]]; then
  echo "ERROR: supported Ubuntu architecture is x86_64 (detected: $architecture)" >&2
  exit 1
fi

script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$script_directory/.." && pwd)"
cd "$project_root"

if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  source /etc/os-release
  if [[ "${ID:-}" != "ubuntu" || "${VERSION_ID:-}" != "22.04" ]]; then
    echo "WARN: supported target is Ubuntu 22.04 (detected: ${PRETTY_NAME:-unknown})"
  fi
fi

required_packages=(
  ca-certificates
  curl
  fontconfig
  fonts-noto-cjk
  libegl1
  libgl1
  libportaudio2
  libpulse0
)
missing_packages=()
if command -v dpkg-query >/dev/null 2>&1; then
  for package in "${required_packages[@]}"; do
    if ! dpkg-query -W -f='${db:Status-Abbrev}' "$package" 2>/dev/null \
      | grep -q '^ii '; then
      missing_packages+=("$package")
    fi
  done
fi

if ((${#missing_packages[@]} > 0)); then
  echo "ERROR: required Ubuntu packages are missing: ${missing_packages[*]}" >&2
  echo "Run the following commands, then run this script again:" >&2
  echo "  sudo apt update" >&2
  echo "  sudo apt install -y ${missing_packages[*]}" >&2
  exit 3
fi

if ! command -v uv >/dev/null 2>&1 && [[ -x "$HOME/.local/bin/uv" ]]; then
  export PATH="$HOME/.local/bin:$PATH"
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "ERROR: uv is not installed." >&2
  echo "Install it as your normal user, open a new shell, and run this script again:" >&2
  echo "  curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  exit 4
fi

export UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-$HOME/.local/share/uv/venvs/summarize-meeting}"

echo "Project: $project_root"
echo "Environment: $UV_PROJECT_ENVIRONMENT"
uv python install 3.11
uv sync --frozen

if [[ "$models" != "none" ]]; then
  uv run python scripts/setup_models.py "$models"
fi

echo
echo "Running environment diagnostics..."
if ! uv run python scripts/doctor.py; then
  echo >&2
  echo "WARN: diagnostics reported an error." >&2
  echo "Review the messages above. A GUI/display error is expected over plain SSH." >&2
fi

cat <<EOF

Ubuntu setup completed.

Start the application with:
  bash scripts/run_ubuntu.sh

To enable conversation summaries, configure data/settings.json as described in:
  docs/ubuntu-install.md
EOF
