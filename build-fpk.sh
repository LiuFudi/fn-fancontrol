#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 LiuFudi
#
# This file is part of niufan, licensed under the GNU General Public
# License version 3 or (at your option) any later version.
# See the LICENSE file for the full text.
# Build the niufan .fpk and stamp the version into the artifact name.
#
# `fnpack build` always emits "<appname>.fpk" inside the source tree and offers
# no way to override the name, which makes two builds impossible to tell apart.
# This wrapper reads the version out of package/manifest, builds, and moves the
# result to dist/<appname>-<version>.fpk.
#
# Requires `fnpack`, which ships with fnOS at /usr/local/bin/fnpack.
#
# Usage:  ./build-fpk.sh [source-dir]      (default: ./package)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="${1:-$HERE/package}"
SOURCE="$(cd "$SOURCE" && pwd)"
DIST="$HERE/dist"

MANIFEST="$SOURCE/manifest"
[ -f "$MANIFEST" ] || { echo "build-fpk: no manifest in $SOURCE" >&2; exit 1; }
command -v fnpack >/dev/null 2>&1 || {
    echo "build-fpk: fnpack not found. It ships with fnOS at /usr/local/bin/fnpack." >&2
    exit 1
}

manifest_value() {
    sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$MANIFEST" \
        | head -n1 | tr -d '\r' | sed 's/[[:space:]]*$//'
}

APPNAME="$(manifest_value appname)"
VERSION="$(manifest_value version)"
if [ -z "$APPNAME" ] || [ -z "$VERSION" ]; then
    echo "build-fpk: appname/version missing from $MANIFEST" >&2
    exit 1
fi

# The daemon reports its own version over the API; keep it in step.
CODE_VERSION="$(sed -n 's/^VERSION = "\(.*\)"/\1/p' \
    "$SOURCE/app/server/fancontrold.py" | head -n1)"
if [ -n "$CODE_VERSION" ] && [ "$CODE_VERSION" != "$VERSION" ]; then
    echo "build-fpk: WARNING: app/server/fancontrold.py VERSION=$CODE_VERSION" >&2
    echo "build-fpk:          but manifest version=$VERSION - the API will report" >&2
    echo "build-fpk:          the wrong version; update the constant." >&2
fi

# Stale bytecode would end up inside the fpk; strip it before packing.  It
# happens easily -- any `python3 -m py_compile` during development recreates it.
find "$SOURCE" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$SOURCE" -name '*.py[co]' -delete 2>/dev/null || true

# The donation QR codes ship inlined as data URIs, so regenerate the module
# rather than trusting a committed copy to still match assets/donate/.
GENERATOR="$HERE/tools/make_donate_qr.py"
if [ -f "$GENERATOR" ]; then
    python3 "$GENERATOR" "$HERE"
fi
[ -f "$SOURCE/app/ui/donate-qr.js" ] || {
    echo "build-fpk: app/ui/donate-qr.js is missing - run tools/make_donate_qr.py" >&2
    exit 1
}

mkdir -p "$DIST"
ARTIFACT="$DIST/${APPNAME}-${VERSION}.fpk"

# Drop the legacy unversioned file and any rebuild of this same version, but
# keep artifacts of other versions around so they stay distinguishable.
rm -f "$SOURCE/${APPNAME}.fpk" "$ARTIFACT"

echo "build-fpk: building $APPNAME $VERSION"
( cd "$SOURCE" && fnpack build --directory . )

BUILT="$SOURCE/${APPNAME}.fpk"
[ -f "$BUILT" ] || { echo "build-fpk: fnpack produced no $BUILT" >&2; exit 1; }
mv -f "$BUILT" "$ARTIFACT"

# Verify the name really matches what is inside the package.
INSIDE="$(tar xzf "$ARTIFACT" -O manifest \
    | sed -n 's/^[[:space:]]*version[[:space:]]*=[[:space:]]*//p' \
    | head -n1 | tr -d '\r' | sed 's/[[:space:]]*$//')"
if [ "$INSIDE" != "$VERSION" ]; then
    echo "build-fpk: manifest inside the fpk says $INSIDE, expected $VERSION" >&2
    exit 1
fi

echo "build-fpk: $ARTIFACT"
ls -la "$ARTIFACT"
