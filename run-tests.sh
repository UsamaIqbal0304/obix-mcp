#!/bin/sh
# Every test, no arguments, no network, no station. Exits non-zero on the first
# suite that fails so a CI step or a reviewer gets one answer.
#
# The suites start a real HTTP server on an ephemeral loopback port — that is
# the point of them — so a sandbox with no loopback will fail here for reasons
# that have nothing to do with the code.
set -e
cd "$(dirname "$0")"
python3 -m unittest discover -s tests -t . -p 'test_*.py' "$@"
