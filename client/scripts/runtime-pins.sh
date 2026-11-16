# Verified upstream archives for machines without suitable host runtimes.
# Node: https://nodejs.org/dist/v24.22.3/SHASUMS256.txt
# Python: https://github.com/astral-sh/python-build-standalone/releases/tag/20261026
OSBI_NODE_VERSION=24.22.3
OSBI_PYTHON_VERSION=3.12.16
OSBI_PYTHON_BUILD=20261026

osbi_runtime_pin() {
  case "$1:$2" in
    node:linux-x64) printf '%s' 061ec3e3062115705b56b51fa9cc330ab50d34b6969387d4e4b1a50c6809acc2 ;;
    node:linux-arm64) printf '%s' 261af2af250dfd7e9f25cf151d1b2713a0adf8f10a9ad116c4947f68627affe8 ;;
    node:darwin-x64) printf '%s' 977614af0504c81045e7fd85596dbca0e5f951b96681516dadafedcca8c447f7 ;;
    node:darwin-arm64) printf '%s' ff6e17d4874b76d272212b908cc020d6e815fba5a6c7eaaa06add725840307fd ;;
    python:linux-x64) printf '%s %s' x86_64-unknown-linux-gnu ce4f6d0a6cfdb3bed6e44c2f9affd85943c76a42e25ad3a259002f31d6fb8a20 ;;
    python:linux-arm64) printf '%s %s' aarch64-unknown-linux-gnu bb4b6392670c5d3f93876996fcaa19858a4a413a0c7720144c18b0d6f1f0db2d ;;
    python:darwin-x64) printf '%s %s' x86_64-apple-darwin 37c38fda76fec1fbf310adcbafb7f0d96af13b5511bf8ea954c3aa8eec185734 ;;
    python:darwin-arm64) printf '%s %s' aarch64-apple-darwin 353d5f1b4fd2715e7053f12f068435d5bbad16b91e057006b5d01e0925709a7e ;;
    *) printf 'Unsupported runtime platform: %s\n' "$2" >&2; return 1 ;;
  esac
}
