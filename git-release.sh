#!/usr/bin/bash
# git-release.sh
# This script creates an archived copy of the current git commit, excluding files specified in .gitattributes with the export-ignore entry.
# Usage: git-release.sh
# The script should be placed in a directory included in your command path (e.g., /usr/bin/).
# It can be run from anywhere inside the repository. The zip is named
# <vendor>-<releasename>-<version>.zip after the "name" and "version" in the committed composer.json.
# Exit status: 0 on success, 1 on failure. No zip is left behind on failure.

# Configuration
GitExportDIR="../git-exported"  # Adjust GitExportDIR to the desired export path (relative to the repository root).

# Work from the repository root, so the script also works from a subdirectory
repo_root=$(git rev-parse --show-toplevel 2>/dev/null) || {
  echo "Not inside a git repository."
  echo "Are you sure this is a git repo?"
  exit 1
}
cd "$repo_root" || exit 1

# Archive the current commit (also works on detached HEAD and on branch names containing "/")
git rev-parse --verify -q HEAD >/dev/null || { echo "The repository has no commits."; exit 1; }

# Read vendor, releasename, and version from the committed composer.json (the same commit that is archived)
composer_json=$(git show HEAD:composer.json 2>/dev/null) || {
  echo "composer.json is not committed in the current commit."
  exit 1
}
vendor=$(jq -r '.name // empty' <<<"$composer_json" | cut -d'/' -f1)
releasename=$(jq -r '.name // empty' <<<"$composer_json" | cut -d'/' -f2)
version=$(jq -r '.version // empty' <<<"$composer_json")
if [ -z "$vendor" ] || [ -z "$releasename" ] || [ -z "$version" ]; then
  echo "composer.json needs a \"name\" (vendor/name) and a \"version\"."
  exit 1
fi
filename="$vendor-$releasename-$version.zip"

mkdir -p "$GitExportDIR" || { echo "Failed to create $GitExportDIR"; exit 1; }
GitExportDIR=$(cd "$GitExportDIR" && pwd) || { echo "Failed to change directory to $GitExportDIR"; exit 1; }

# Create the git archive in a temporary file, so a failure never replaces an existing zip
tmpfile="$GitExportDIR/.$filename.partial"
trap 'rm -f "$tmpfile"' EXIT
if ! git archive --format zip -9 --prefix "$vendor/$releasename/" --output "$tmpfile" HEAD; then
  echo "git archive failed."
  exit 1
fi

# Clear the git commit hash that git archive stores as the zip archive comment
zip -q -z "$tmpfile" </dev/null || { echo "Failed to clear the zip comment."; exit 1; }

mv "$tmpfile" "$GitExportDIR/$filename" || exit 1

echo "Release $filename created successfully at $GitExportDIR/$filename"
