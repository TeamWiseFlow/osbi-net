#!/usr/bin/env bash
# Shared installation helpers. Never modify system runtimes or shell profiles.
OSBI_RUNTIME_HELPERS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$OSBI_RUNTIME_HELPERS/runtime-pins.sh"

osbi_platform() {
  local platform arch
  case "$(uname -s)" in Linux) platform=linux ;; Darwin) platform=darwin ;; *) printf 'Use Linux or macOS for this Bash installer.\n' >&2; return 1 ;; esac
  case "$(uname -m)" in x86_64|amd64) arch=x64 ;; arm64|aarch64) arch=arm64 ;; *) printf 'Unsupported CPU architecture.\n' >&2; return 1 ;; esac
  printf '%s-%s' "$platform" "$arch"
}

osbi_node_usable() {
  [[ -x "$1" ]] && "$1" -e 'const [a,b]=process.versions.node.split(".").map(Number);process.exit(a>24||(a===24&&b>=9)?0:1)' >/dev/null 2>&1
}

osbi_python_usable() {
  [[ -x "$1" ]] && "$1" -I -c 'import sys, venv, ensurepip; sys.exit(0 if sys.version_info >= (3, 12) else 1)' >/dev/null 2>&1
}

osbi_verify_archive() {
  local actual
  if command -v sha256sum >/dev/null; then actual="$(sha256sum "$1")";
  elif command -v shasum >/dev/null; then actual="$(shasum -a 256 "$1")";
  else printf 'Install sha256sum or shasum before proceeding.\n' >&2; return 1; fi
  [[ "${actual%% *}" == "$2" ]] || { printf 'Runtime SHA-256 mismatch; refusing installation.\n' >&2; return 1; }
}

osbi_download_runtime() {
  local url="$1" destination="$2" sha="$3" name="$4"
  if [[ -n "${OSBI_RUNTIME_DOWNLOADS:-}" && -f "$OSBI_RUNTIME_DOWNLOADS/$name" ]]; then
    cp "$OSBI_RUNTIME_DOWNLOADS/$name" "$destination"
  else
    command -v curl >/dev/null || { printf 'Install curl or provide OSBI_RUNTIME_DOWNLOADS.\n' >&2; return 1; }
    if [[ "$url" == https://github.com/* && -n "${OSBI_GITHUB_PROXY:-}" ]]; then
      url="$("$OSBI_INSTALL_NODE" --input-type=module -e '
        import {pathToFileURL} from "node:url";
        const {githubDownloadUrl} = await import(pathToFileURL(process.argv[2]));
        process.stdout.write(githubDownloadUrl(process.argv[1]));
      ' "$url" "$OSBI_RUNTIME_HELPERS/../client/browser/scripts/download-network.mjs")" || return
    fi
    curl --fail --location --retry 3 --connect-timeout 20 --speed-time 60 --speed-limit 1024 --output "$destination" "$url" || return
  fi
  osbi_verify_archive "$destination" "$sha"
}

osbi_prepare_node() {
  local root="$1" platform="$2" candidate archive sha destination stage
  candidate="${OSBI_BROWSER_NODE:-$(command -v node || true)}"
  if osbi_node_usable "$candidate" && [[ -x "$(dirname "$candidate")/npm" && -x "$(dirname "$candidate")/npx" ]]; then
    OSBI_INSTALL_NODE="$candidate"
  else
    archive="node-v${OSBI_NODE_VERSION}-${platform}.tar.gz"
    destination="$root/.osbi/runtime/node-v${OSBI_NODE_VERSION}-${platform}"
    if [[ ! -e "$destination" ]]; then
      sha="$(osbi_runtime_pin node "$platform")" || return
      stage="$(mktemp -d "$root/.osbi/runtime/.node-XXXXXX")" || return
      if ! osbi_download_runtime "https://nodejs.org/dist/v${OSBI_NODE_VERSION}/$archive" "$stage/runtime.tar.gz" "$sha" "$archive" ||
         ! tar -xzf "$stage/runtime.tar.gz" -C "$stage" ||
         ! osbi_node_usable "$stage/node-v${OSBI_NODE_VERSION}-${platform}/bin/node"; then
        rm -rf "$stage"; return 1
      fi
      mv "$stage/node-v${OSBI_NODE_VERSION}-${platform}" "$destination"
      rm -rf "$stage"
    fi
    OSBI_INSTALL_NODE="$destination/bin/node"
    osbi_node_usable "$OSBI_INSTALL_NODE" || { printf 'Owned Node runtime is incomplete; inspect %s.\n' "$destination" >&2; return 1; }
  fi
  local name target
  for name in node npm npx; do
    target="$(dirname "$OSBI_INSTALL_NODE")/$name"
    if [[ "$target" != "$root/.osbi/bin/$name" ]]; then ln -sfn "$target" "$root/.osbi/bin/$name"; fi
  done
  export PATH="$root/.osbi/bin:$PATH"
}

osbi_prepare_python() {
  local root="$1" platform="$2" candidate pin target sha archive destination stage
  if [[ -e "$root/.venv" ]]; then
    osbi_python_usable "$root/.venv/bin/python" || { printf 'Existing .venv is incompatible; inspect it before retrying.\n' >&2; return 1; }
    OSBI_INSTALL_PYTHON="$root/.venv/bin/python"
    return
  fi
  candidate="$(command -v python3 || true)"
  if osbi_python_usable "$candidate"; then OSBI_INSTALL_PYTHON="$candidate"; return; fi
  pin="$(osbi_runtime_pin python "$platform")" || return
  read -r target sha <<< "$pin"
  archive="cpython-${OSBI_PYTHON_VERSION}+${OSBI_PYTHON_BUILD}-${target}-install_only_stripped.tar.gz"
  destination="$root/.osbi/runtime/cpython-${OSBI_PYTHON_VERSION}-${OSBI_PYTHON_BUILD}-${platform}"
  if [[ ! -e "$destination" ]]; then
    stage="$(mktemp -d "$root/.osbi/runtime/.python-XXXXXX")" || return
    if ! osbi_download_runtime "https://github.com/astral-sh/python-build-standalone/releases/download/${OSBI_PYTHON_BUILD}/$archive" "$stage/runtime.tar.gz" "$sha" "$archive" ||
       ! tar -xzf "$stage/runtime.tar.gz" -C "$stage" || ! osbi_python_usable "$stage/python/bin/python3"; then
      rm -rf "$stage"; return 1
    fi
    mv "$stage/python" "$destination"
    rm -rf "$stage"
  fi
  OSBI_INSTALL_PYTHON="$destination/bin/python3"
  osbi_python_usable "$OSBI_INSTALL_PYTHON" || { printf 'Owned Python runtime is incomplete; inspect %s.\n' "$destination" >&2; return 1; }
}
