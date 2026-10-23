#!/bin/bash
# Linting script - run before pushing to catch CI failures

set -e

echo "🔍 Running ruff check..."
python -m ruff check . --fix || {
    echo "❌ Ruff check failed. Please fix the errors above."
    exit 1
}

echo "✨ Running ruff format..."
python -m ruff format .

# Reads your private list from ~/.config/storydump/private-identifiers.txt; without one it says so and passes.
# It reads this branch's commit messages since origin/main too, as CI reads a pull request's.
echo "🔒 Checking the public record..."
commits=()
if git rev-parse --verify --quiet origin/main > /dev/null; then
    commits=(--commits origin/main..HEAD)
fi
python scripts/public_record.py "${commits[@]}" || {
    echo "❌ A file or commit message names a private identifier. Write a placeholder instead (AGENTS.md › The record)."
    exit 1
}

echo "✅ All checks passed!"
