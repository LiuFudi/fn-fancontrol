# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 LiuFudi
#
# This file is part of niufan, licensed under the GNU General Public
# License version 3 or (at your option) any later version.
# See the LICENSE file for the full text.
#
# Windows build of the NiuFan .fpk, using the official
# fnpack-1.2.3-windows-amd64.  It runs exactly the same checks as
# build-fpk.sh (tools/release_checks.py), so both paths produce an
# equivalent artifact.
#
# Why this is more than a plain `fnpack build`:
#   * the Windows build of fnpack writes mode 0666 for every file and 0777 for
#     every directory, so cmd/* would not be executable after installation;
#     `release_checks.py stamp` rewrites the modes and re-stamps the manifest
#     checksum (which is md5(app.tgz)) so the two stay consistent;
#   * `fnpack build` exits 0 even when it produced no file, so the artifact is
#     checked for existence;
#   * a manifest value containing ';' is silently truncated by fnpack, so the
#     packaging tree is scanned before it is packed.
#
# This file is written in ASCII on purpose: Windows PowerShell 5.1 reads
# .ps1 files without a byte-order mark as ANSI, which mangles any non-ASCII
# text.  Keep it ASCII-only.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File build-fpk.ps1
#   powershell -ExecutionPolicy Bypass -File build-fpk.ps1 -Source .\package
[CmdletBinding()]
param(
    [string]$Source = '',
    [string]$Fnpack = ''
)

$ErrorActionPreference = 'Stop'
$env:PYTHONIOENCODING = 'utf-8'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

function Fail([string]$Message) {
    Write-Error "build-fpk: $Message" -ErrorAction Continue
    exit 1
}

function Invoke-Checked([string]$Command, [string[]]$Arguments) {
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        Fail("$Command $($Arguments -join ' ') exited with $LASTEXITCODE")
    }
}

# Return the first interpreter that really runs: on Windows the bare name
# `python3` is often the Microsoft Store stub, which exits 9009.
function Resolve-Interpreter([string[]]$Candidates, [string]$Probe) {
    foreach ($candidate in $Candidates) {
        $found = Get-Command $candidate -ErrorAction SilentlyContinue
        if (-not $found) { continue }
        & $found.Source @Probe > $null 2>&1
        if ($LASTEXITCODE -eq 0) { return $found.Source }
    }
    return $null
}

$root = $PSScriptRoot
if (-not $root) { $root = Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $Source) { $Source = Join-Path $root 'package' }
$Source = (Resolve-Path -LiteralPath $Source).Path
$dist = Join-Path $root 'dist'
$checks = Join-Path $root 'tools\release_checks.py'

if (-not (Test-Path -LiteralPath (Join-Path $Source 'manifest'))) {
    Fail("no manifest in $Source")
}
if (-not (Test-Path -LiteralPath $checks)) { Fail("$checks is missing") }

$python = Resolve-Interpreter @('python', 'python3') @('-c', 'pass')
if (-not $python) { Fail('python3 not found (tools/release_checks.py needs it)') }

if (-not $Fnpack) {
    foreach ($candidate in @('fnpack.exe', 'fnpack')) {
        $found = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($found) { $Fnpack = $found.Source; break }
    }
}
if (-not $Fnpack) {
    $dirs = @()
    if ($env:LOCALAPPDATA) { $dirs += (Join-Path $env:LOCALAPPDATA 'Programs\fnpack') }
    if (${env:ProgramFiles}) { $dirs += (Join-Path ${env:ProgramFiles} 'fnpack') }
    foreach ($dir in $dirs) {
        $guess = Join-Path $dir 'fnpack.exe'
        if (Test-Path -LiteralPath $guess) { $Fnpack = $guess; break }
    }
}
if (-not $Fnpack) {
    Fail('fnpack.exe not found (official fnpack-1.2.3-windows-amd64); pass -Fnpack <path>')
}

# 1. version cross-check: manifest, daemon constant, CHANGELOG, README badge.
Invoke-Checked $python @($checks, '--root', $root, 'versions')

# 2. leftovers would otherwise end up inside the fpk.
Get-ChildItem -LiteralPath $Source -Recurse -Force -Directory -Filter '__pycache__' |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Get-ChildItem -LiteralPath $Source -Recurse -Force -File |
    Where-Object { @('.pyc', '.pyo', '.fpk') -contains $_.Extension } |
    Remove-Item -Force -ErrorAction SilentlyContinue

# 3. the donation QR codes ship inlined, so rebuild the module from assets/.
$generator = Join-Path $root 'tools\make_donate_qr.py'
if (Test-Path -LiteralPath $generator) {
    Invoke-Checked $python @($generator, $root)
}
if (-not (Test-Path -LiteralPath (Join-Path $Source 'app\ui\donate-qr.js'))) {
    Fail('app/ui/donate-qr.js is missing - run tools/make_donate_qr.py')
}

# 4. packaging tree: required files, the identifiers that must never move, no
#    symlinks, no scaffold placeholders, no host paths, no ';' in a value.
Invoke-Checked $python @($checks, '--root', $root, 'source')

if (-not (Test-Path -LiteralPath $dist)) { New-Item -ItemType Directory -Path $dist | Out-Null }

$manifest = Get-Content -LiteralPath (Join-Path $Source 'manifest') -Encoding UTF8
$appname = (($manifest | Where-Object { $_ -match '^\s*appname\s*=' }) -split '=', 2)[1].Trim()
$version = (($manifest | Where-Object { $_ -match '^\s*version\s*=' }) -split '=', 2)[1].Trim()
if (-not $appname -or -not $version) { Fail('appname/version missing from manifest') }

$built = Join-Path $Source "$appname.fpk"
$artifact = Join-Path $dist "$appname-$version.fpk"
Remove-Item -Force -ErrorAction SilentlyContinue $built, $artifact

Write-Host "build-fpk: building $appname $version"
Push-Location $Source
try { Invoke-Checked $Fnpack @('build', '--directory', '.') } finally { Pop-Location }

# fnpack exits 0 even when it produced nothing.
if (-not (Test-Path -LiteralPath $built)) { Fail("fnpack produced no $built") }

# 5. normalise the modes and re-stamp the manifest checksum.
Invoke-Checked $python @($checks, 'stamp', $built)
Move-Item -Force -LiteralPath $built -Destination $artifact

# 6. inspect the artifact that is about to be delivered.
Invoke-Checked $python @($checks, '--root', $root, 'fpk', $artifact, '--version', $version)

$hash = (Get-FileHash -LiteralPath $artifact -Algorithm SHA256).Hash.ToLower()
$size = (Get-Item -LiteralPath $artifact).Length
Write-Host "build-fpk: $artifact"
Write-Host ("build-fpk: {0} bytes  SHA-256 {1}" -f $size, $hash)
