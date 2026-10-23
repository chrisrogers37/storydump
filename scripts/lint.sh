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
echo "🔒 Checking the public record..."
python scripts/public_record.py || {
    echo "❌ A file names a private identifier. Write a placeholder instead (AGENTS.md › The record)."
    exit 1
}

echo "✅ All checks passed!"
