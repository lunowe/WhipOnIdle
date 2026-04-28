#!/usr/bin/env bash
# Build WhipOnIdle.app on macOS.
# Output: dist/WhipOnIdle.app — drag into /Applications or zip to share.
set -euo pipefail

cd "$(dirname "$0")"

# Use a venv to keep the build hermetic.
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi
# shellcheck source=/dev/null
source .venv/bin/activate

pip install --upgrade pip >/dev/null
pip install -r requirements-dev.txt

rm -rf build dist
pyinstaller whip_app.spec --clean --noconfirm

# Strip the macOS quarantine attribute on the freshly built app so we can run
# it locally without Gatekeeper complaining. (Recipients still need to right-
# click → Open the first time unless we code-sign it.)
xattr -dr com.apple.quarantine dist/WhipOnIdle.app 2>/dev/null || true

# Zip for distribution.
cd dist
rm -f WhipOnIdle-mac.zip
zip -qr WhipOnIdle-mac.zip WhipOnIdle.app
cd ..

echo
echo "Built: dist/WhipOnIdle.app"
echo "Zipped for sharing: dist/WhipOnIdle-mac.zip"
