#!/bin/sh
# Runs every test, offline. The pre-commit hook runs this, so a commit only goes through if it passes.
set -e
cd "$(dirname "$0")"
echo "== JavaScript: lib.js and index.html"
node --test --test-reporter=spec tests/*.test.js
echo "== Python: watcher.py"
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_*.py'
echo "All tests passed."
