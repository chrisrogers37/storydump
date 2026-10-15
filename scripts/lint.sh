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

echo "✅ All checks passed!"
