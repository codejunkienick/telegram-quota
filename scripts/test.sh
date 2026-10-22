#!/bin/sh
# Runs the unit tests from the tree root; the exit status is unittest's.
set -eu
cd "$(dirname "$0")/.."
exec python3 -B -m unittest discover -s .
