#!/usr/bin/env bash
# Rewrites all git commit metadata to anonymize the repository.
# Scrubs author name, email, AND timestamps/timezones to prevent
# deanonymization via commit date patterns or UTC offset leakage.
# WARNING: This rewrites history. Only run on a fresh clone before submission.
set -euo pipefail

echo "=== PROSE-APEX Git History Scrubber ==="
echo "Rewriting all commits with anonymous metadata + neutral timestamps..."

git filter-branch -f --env-filter '
    export GIT_AUTHOR_NAME="Anonymous Reviewer"
    export GIT_AUTHOR_EMAIL="anon@doubleblind.org"
    export GIT_COMMITTER_NAME="Anonymous Reviewer"
    export GIT_COMMITTER_EMAIL="anon@doubleblind.org"
    export GIT_AUTHOR_DATE="2024-01-01T12:00:00+0000"
    export GIT_COMMITTER_DATE="2024-01-01T12:00:00+0000"
' --tag-name-filter cat -- --branches --tags

# Remove backup refs
git for-each-ref --format='%(refname)' refs/original/ | while read ref; do
    git update-ref -d "$ref"
done

# Clean reflog and GC
git reflog expire --expire=now --all
git gc --prune=now --aggressive

echo "Done. All commits now attributed to:"
echo "  Author:    Anonymous Reviewer <anon@doubleblind.org>"
echo "  Timestamp: 2024-01-01T12:00:00+0000 (UTC)"
echo ""
echo "Verify with:"
echo "  git log --format='%an <%ae> %ad' --date=iso"
