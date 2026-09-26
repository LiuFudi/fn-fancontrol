#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 LiuFudi
#
# This file is part of niufan, licensed under the GNU General Public
# License version 3 or (at your option) any later version.
# See the LICENSE file for the full text.
# Build the NiuFan .fpk and stamp the version into the artifact name.
#
# `fnpack build` always emits "<appname>.fpk" inside the source tree and offers
# no way to override the name, which makes two builds impossible to tell apart.
# This wrapper reads the version out of package/manifest, checks it against the
# other places that carry it, builds, normalises the modes inside the fpk
# (fnpack.exe on Windows writes 0666/0777 for everything) and verifies the
# result before moving it to dist/<appname>-<version>.fpk.
#
# Every check lives in tools/release_checks.py, so the Windows wrapper
# (build-fpk.ps1) enforces exactly the same rules.  Any failure exits non-zero
# and leaves no artifact behind.
#
# Requires `fnpack`, which ships with fnOS at /usr/local/bin/fnpack.
#
# Usage:  ./build-fpk.sh [source-dir]      (default: ./package)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="${1:-$HERE/package}"
SOURCE="$(cd "$SOURCE" && pwd)"
DIST="$HERE/dist"
CHECKS="$HERE/tools/release_checks.py"

MANIFEST="$SOURCE/manifest"
[ -f "$MANIFEST" ] || { echo "build-fpk: no manifest in $SOURCE" >&2; exit 1; }

PYTHON="$(command -v python3 || command -v python || echo /usr/bin/python3)"
command -v "$PYTHON" >/dev/null 2>&1 || {
    echo "build-fpk: python3 not found (tools/release_checks.py needs it)" >&2
    exit 1
}
[ -f "$CHECKS" ] || { echo "build-fpk: $CHECKS is missing" >&2; exit 1; }

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

# 1. The version is written in several places (manifest, the daemon constant,
#    the CHANGELOG, the README badge) and they must agree: a mismatch means the
#    artifact would report a version the app center never sees.  Hard failure.
"$PYTHON" "$CHECKS" --root "$HERE" versions || exit 1

# 2. Stale bytecode and leftovers would end up inside the fpk; it happens
#    easily, because any `python3 -m py_compile` recreates __pycache__.
find "$SOURCE" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$SOURCE" -name '*.py[co]' -delete 2>/dev/null || true
find "$SOURCE" -name '*.fpk' -delete 2>/dev/null || true

# 3. The donation QR codes ship inlined as data URIs, so regenerate the module
#    rather than trusting a committed copy to still match assets/donate/.
GENERATOR="$HERE/tools/make_donate_qr.py"
if [ -f "$GENERATOR" ]; then
    "$PYTHON" "$GENERATOR" "$HERE"
fi
[ -f "$SOURCE/app/ui/donate-qr.js" ] || {
    echo "build-fpk: app/ui/donate-qr.js is missing - run tools/make_donate_qr.py" >&2
    exit 1
}

# 4. Packaging tree: required files, the identifiers that must never move, no
#    symlinks, no scaffold placeholders, no host paths or credentials.
"$PYTHON" "$CHECKS" --root "$HERE" source || exit 1

mkdir -p "$DIST"
ARTIFACT="$DIST/${APPNAME}-${VERSION}.fpk"

# Drop the legacy unversioned file and any rebuild of this same version, but
# keep artifacts of other versions around so they stay distinguishable.
rm -f "$SOURCE/${APPNAME}.fpk" "$ARTIFACT"

echo "build-fpk: building $APPNAME $VERSION"
( cd "$SOURCE" && fnpack build --directory . )

# `fnpack build` reports success even when it produced nothing.
BUILT="$SOURCE/${APPNAME}.fpk"
[ -f "$BUILT" ] || { echo "build-fpk: fnpack produced no $BUILT" >&2; exit 1; }

# 5. Normalise the modes and re-stamp the manifest checksum.
"$PYTHON" "$CHECKS" stamp "$BUILT" || exit 1
mv -f "$BUILT" "$ARTIFACT"

# 6. Inspect what is actually inside the artifact: version, display name,
#    identity fields, checksum, modes, symlinks, and the shipped UI strings.
"$PYTHON" "$CHECKS" --root "$HERE" fpk "$ARTIFACT" --version "$VERSION" || exit 1

echo "build-fpk: $ARTIFACT"
sha256sum "$ARTIFACT" 2>/dev/null || true
ls -la "$ARTIFACT"
