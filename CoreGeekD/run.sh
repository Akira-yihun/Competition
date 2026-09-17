#!/usr/bin/env bash
# Local launcher.  The platform supplies its own entry point, so this script is
# for local runs and the lab only -- it is NOT part of the submission payload.
#
# Usage: bash run.sh <port>
set -euo pipefail
cd -- "$(dirname -- "$0")"
exec "${PYTHON:-python3}" -u main3.py "$@"
