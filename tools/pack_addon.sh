#!/bin/sh
# Copy the add-on folder (addon/, package included) into dist/hisense_local, ready to copy to /addons.
set -eu
cd "$(dirname "$0")/.."
rm -rf dist/hisense_local
mkdir -p dist/hisense_local
cp -R addon/. dist/hisense_local/
# a hand install builds from the Dockerfile; with image: set, Home Assistant would pull instead
sed -i.bak '/^image:/d' dist/hisense_local/config.yaml && rm dist/hisense_local/config.yaml.bak
find dist -name '__pycache__' -type d -prune -exec rm -rf {} +
find dist -name '._*' -delete
COPYFILE_DISABLE=1 tar -C dist -czf dist/hisense_local.tar.gz hisense_local
echo "dist/hisense_local.tar.gz"
