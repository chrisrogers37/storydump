#!/bin/sh
# The Ignored Build Step for this Vercel project (`ignoreCommand` in vercel.json).
#
# Vercel runs it from the project's Root Directory, landing/, before it installs
# or builds. Exit 0 cancels the deployment; exit 1 lets it build. Vercel
# documents only those two codes, so every path here ends in one of them.
#
# The rule is BUILD UNLESS SURE. The build is skipped only when the commit of
# this branch's last successful deployment is known and nothing under landing/
# changed since. Everything else builds:
#
# - VERCEL_GIT_PREVIOUS_SHA is empty on a branch's first deployment. HEAD^ is no
#   substitute: the branch's parent was never deployed as this branch.
# - The previous commit may be missing from Vercel's shallow clone, or gone from
#   the history after a rewrite. git diff then fails (128), which builds.
#
# Scope is landing/ alone because nothing the build reads lies outside it.
# The point is cost: Vercel bills a build by its build minutes. A deployment
# cancelled here stops before the install and the build, so it costs seconds of
# build time instead of minutes. It still appears as a (cancelled) deployment.

cd "$(dirname "$0")/.." || exit 1

prev="${VERCEL_GIT_PREVIOUS_SHA:-}"
if [ -n "$prev" ] && git diff --quiet "$prev" HEAD -- .; then
  echo "landing/ is unchanged since ${prev}: skipping the build"
  exit 0
fi
echo "building: no previous deployment to compare with, or landing/ changed"
exit 1
