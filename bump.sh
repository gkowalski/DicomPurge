#!/usr/bin/env bash
set -e

# 1. Ensure the user passed a valid version type
BUMP_TYPE=$1
if [[ -z "$BUMP_TYPE" || ! "$BUMP_TYPE" =~ ^(major|minor|patch)$ ]]; then
  echo "❌ Error: Please specify a valid bump type."
  echo "Usage: ./bump.sh [major|minor|patch]"
  exit 1
fi

echo "🚀 Initiating $BUMP_TYPE version bump..."

# 2. Run your exact command sequence using the provided bump type
uv run bump-my-version bump "$BUMP_TYPE" && \
uv lock && \
git add uv.lock && \
git commit --amend --no-edit && \
git tag -f "v$(grep -E '^version =' pyproject.toml | tr -d 'version = " ')"

# 3. Print the new version string as confirmation
NEW_VERSION=$(grep -E '^version =' pyproject.toml | tr -d 'version = " ')
echo "✅ Successfully bumped version to v$NEW_VERSION and updated Git tag!"

