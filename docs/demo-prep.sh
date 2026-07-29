#!/usr/bin/env bash
# Build the throwaway repo the demo tapes record against.
#
#   bash docs/demo-prep.sh bare   # un-bootstrapped — for demo-install.tape
#   bash docs/demo-prep.sh init   # already `nightly init`ed — for demo.tape
#
# The demo must never be recorded against a real checkout: `nightly init`
# and `nightly start` both write state. It also has to start from the same
# place every time, or the GIF drifts between renders for reasons that have
# nothing to do with the CLI changing.
set -euo pipefail

MODE="${1:-init}"
DEMO_DIR="${DEMO_DIR:-/private/tmp/nightly-demo-render}"

case "$MODE" in
bare | init) ;;
*)
	echo "usage: $0 [bare|init]" >&2
	exit 2
	;;
esac

rm -rf "$DEMO_DIR"
mkdir -p "$DEMO_DIR"
cd "$DEMO_DIR"

git init -q -b main .
git config user.email dev@example.com
git config user.name dev
git config commit.gpgsign false

# A plausible-looking project, so the demo is a tool acting on a repo
# rather than a tool acting on an empty directory.
printf '# acme-api\n\nInternal HTTP client.\n' >README.md
mkdir -p src
cat >src/auth.py <<'PY'
import httpx


def login(user: str, password: str) -> str:
    """Exchange credentials for a bearer token."""
    resp = httpx.post("/oauth/token", json={"user": user, "password": password})
    return resp.json()["access_token"]
PY

git add -A
git commit -qm "initial commit"

if [ "$MODE" = init ]; then
	# The loop demo starts from an already-bootstrapped repo: `nightly init`
	# gets its own GIF, and replaying it here would push the part worth
	# watching — the cascade explaining its pick — off the bottom of frame.
	nightly init >/dev/null 2>&1
fi

echo "demo repo ready at $DEMO_DIR ($MODE)"
