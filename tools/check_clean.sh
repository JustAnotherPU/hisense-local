#!/bin/sh
# Pre-publish gate: fail if the tree or its history holds anything secret-shaped.
#   tools/check_clean.sh [extra-patterns-file]
# The optional file (keep it OUTSIDE the repo) holds one extended regex per line:
# account e-mail, LAN addresses, session ids, device ids...
# A line containing "synthetic-fixture" is ignored (for hand-made test data).
set -u
cd "$(dirname "$0")/.."

ALLOWED_MAIL='12965370\+JustAnotherPU@users\.noreply\.github\.com'
PATTERNS='[0-9a-fA-F]{32,}
[A-Za-z0-9+/]{41,}={0,2}
[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}
([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}'
if [ $# -ge 1 ]; then
    PATTERNS="$PATTERNS
$(grep -v '^[[:space:]]*$' "$1")"
fi

files=$(git ls-files -co --exclude-standard)
revs=$(git rev-list --all 2>/dev/null)
hits=$(printf '%s\n' "$PATTERNS" | while IFS= read -r re; do
    [ -n "$re" ] || continue
    # shellcheck disable=SC2086
    printf '%s\n' $files | xargs grep -nIE -e "$re" /dev/null 2>/dev/null
    # shellcheck disable=SC2086
    [ -z "$revs" ] || git grep -nIE -e "$re" $revs 2>/dev/null
    # commit messages and author/committer identities are published too
    git log --all --format='%an%n%ae%n%cn%n%ce%n%s%n%b' 2>/dev/null | grep -E -e "$re" | sed 's/^/commit log: /'
done | grep -v 'synthetic-fixture' | grep -Ev "$ALLOWED_MAIL")

if [ -n "$hits" ]; then
    printf '%s\n' "$hits" | cut -c1-160
    echo "check_clean: secret-shaped content found (see above)" >&2
    exit 1
fi
echo "clean"
