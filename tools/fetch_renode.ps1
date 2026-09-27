<#
.SYNOPSIS
    Downloads and extracts the pinned Renode build used as the emulation backend.

.DESCRIPTION
    Renode is the CPU/bus emulator; this project only ships the platform
    description and peripheral models.  The archive is verified against the
    SHA256 captured when the stack was chosen, then extracted into
    tools\renode\ so the whole emulator stays inside this repository.
#>
[CmdletBinding()]
param(
    [string] $Version = "1.17.0",
    [switch] $Force
)

$ErrorActionPreference = "Stop"

$expectedSha256 = @{
    "1.17.0" = "18BCF145422039E8702C3E5F9864C7787561BFD5DEB7A3EE6E168ECF19E2B9DB"
}
$url = "https://github.com/renode/renode/releases/download/v$Version/renode-$Version.windows-portable.zip"

$repoRoot = Split-Path -Parent $PSScriptRoot
$toolsDir = Join-Path $repoRoot "tools"
$targetDir = Join-Path $toolsDir "renode"
$archive = Join-Path $toolsDir "renode-$Version.windows-portable.zip"

if ((Test-Path (Join-Path $targetDir "renode.exe")) -and -not $Force) {
    Write-Host "Renode $Version already present at $targetDir (use -Force to re-install)"
    exit 0
}

if (-not (Test-Path $archive)) {
    Write-Host "Downloading $url"
    Invoke-WebRequest -Uri $url -OutFile $archive -UseBasicParsing
}

$actual = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash
$expected = $expectedSha256[$Version]
if ($expected -and $actual -ne $expected) {
    throw "SHA256 mismatch for $archive`n  expected $expected`n  actual   $actual"
}

Write-Host "Extracting to $targetDir"
Add-Type -AssemblyName System.IO.Compression.FileSystem
if (Test-Path $targetDir) { Remove-Item -Recurse -Force $targetDir }
$staging = Join-Path $toolsDir "renode-extract"
if (Test-Path $staging) { Remove-Item -Recurse -Force $staging }
[System.IO.Compression.ZipFile]::ExtractToDirectory($archive, $staging)

# The archive contains a single versioned directory; hoist it into place.
$inner = Get-ChildItem -Path $staging -Directory | Select-Object -First 1
Move-Item -LiteralPath $inner.FullName -Destination $targetDir
Remove-Item -Recurse -Force $staging

Write-Host "Renode $Version ready: $(Join-Path $targetDir 'renode.exe')"
