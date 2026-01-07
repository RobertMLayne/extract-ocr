<#
.SYNOPSIS
  Resets VS Code workspace caches for Python/Pylance (and optionally Jupyter).

.DESCRIPTION
  If you get a persistent Pylance notification like "… file(s) and … cells to analyze",
  it can be caused by stuck/corrupted workspace storage for the Python/Pylance extensions.

  This script locates the VS Code workspaceStorage entry for a given folder and removes
  the extension-specific cache folders inside it.

  By default this is a DRY RUN. Pass -Apply to actually delete anything.

.EXAMPLE
  # Dry run (recommended first)
  ./vscode/global/Reset-PythonPylanceWorkspaceState.ps1 -WorkspacePath "C:\Dev\Projects\extract-ocr"

.EXAMPLE
  # Apply deletion
  ./vscode/global/Reset-PythonPylanceWorkspaceState.ps1 -WorkspacePath "C:\Dev\Projects\extract-ocr" -Apply

.EXAMPLE
  # Apply + also clear Jupyter workspace cache
  ./vscode/global/Reset-PythonPylanceWorkspaceState.ps1 -WorkspacePath "C:\Dev\Projects\extract-ocr" -Apply -IncludeJupyter
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string] $WorkspacePath = (Get-Location).Path,

    [Parameter(Mandatory = $false)]
    [switch] $Apply,

    [Parameter(Mandatory = $false)]
    [switch] $IncludeJupyter,

    [Parameter(Mandatory = $false)]
    [switch] $ClearGlobalStorage
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-VSCodeUserRoots {
    $roots = @()

    $appData = $env:APPDATA
    if (-not $appData) {
        return , $roots
    }

    foreach ($product in @('Code', 'Code - Insiders', 'VSCodium')) {
        $candidate = Join-Path $appData $product
        $userRoot = Join-Path $candidate 'User'
        if (Test-Path -LiteralPath $userRoot) {
            $roots += $userRoot
        }
    }

    return , $roots
}

function Get-PathForCompareKey([string] $path) {
    try {
        $full = [System.IO.Path]::GetFullPath($path)
        return $full.TrimEnd('\').ToLowerInvariant()
    }
    catch {
        return $path.TrimEnd('\').ToLowerInvariant()
    }
}

function Read-WorkspaceJson([string] $workspaceJsonPath) {
    try {
        $raw = Get-Content -LiteralPath $workspaceJsonPath -Raw
        if (-not $raw) {
            return $null
        }
        return $raw | ConvertFrom-Json
    }
    catch {
        return $null
    }
}

function Get-WorkspaceStorageEntryForFolder([string] $userRoot, [string] $workspacePath) {
    $wsStorage = Join-Path $userRoot 'workspaceStorage'
    if (-not (Test-Path -LiteralPath $wsStorage)) {
        return $null
    }

    $workspacePathNorm = Get-PathForCompareKey $workspacePath

    # Each folder contains workspace.json with a "folder" URI. We match by suffix.
    foreach ($entryDir in Get-ChildItem -LiteralPath $wsStorage -Directory -ErrorAction SilentlyContinue) {
        $workspaceJson = Join-Path $entryDir.FullName 'workspace.json'
        if (-not (Test-Path -LiteralPath $workspaceJson)) {
            continue
        }

        $obj = Read-WorkspaceJson $workspaceJson
        if (-not $obj) {
            continue
        }

        $folderUri = $obj.folder
        if (-not $folderUri) {
            continue
        }

        # Typical format: "file:///c%3A/Dev/Projects/extract-ocr"
        if ($folderUri -isnot [string]) {
            continue
        }

        $decoded = [System.Uri]::UnescapeDataString($folderUri)
        $decodedNorm = $decoded.ToLowerInvariant()

        # Compare using a conservative suffix match to avoid URI parsing edge cases.
        $workspacePathNorm2 = ($workspacePathNorm -replace '\\', '/')
        if ($decodedNorm.EndsWith($workspacePathNorm2)) {
            return $entryDir.FullName
        }

        # Also check raw folder URI contains the encoded path
        $encodedPath = ($workspacePathNorm2 -replace ':', '%3a')
        if ($folderUri.ToLowerInvariant().EndsWith($encodedPath)) {
            return $entryDir.FullName
        }
    }

    return $null
}

function Remove-ExtensionWorkspaceCaches([string] $workspaceStorageEntry) {
    $targets = @(
        'ms-python.python',
        'ms-python.vscode-pylance'
    )
    if ($IncludeJupyter) {
        $targets += 'ms-toolsai.jupyter'
    }

    foreach ($name in $targets) {
        $dir = Join-Path $workspaceStorageEntry $name
        if (Test-Path -LiteralPath $dir) {
            if ($Apply) {
                Write-Host "Deleting workspace cache: $dir"
                Remove-Item -LiteralPath $dir -Recurse -Force
            }
            else {
                Write-Host "[DRY RUN] Would delete workspace cache: $dir"
            }
        }
        else {
            Write-Host "Not found (ok): $dir"
        }
    }
}

function Remove-GlobalStorageCaches([string] $userRoot) {
    $globalStorage = Join-Path $userRoot 'globalStorage'
    if (-not (Test-Path -LiteralPath $globalStorage)) {
        return
    }

    $targets = @(
        'ms-python.vscode-pylance'
    )

    foreach ($name in $targets) {
        $dir = Join-Path $globalStorage $name
        if (Test-Path -LiteralPath $dir) {
            if ($Apply) {
                Write-Host "Deleting global cache: $dir"
                Remove-Item -LiteralPath $dir -Recurse -Force
            }
            else {
                Write-Host "[DRY RUN] Would delete global cache: $dir"
            }
        }
        else {
            Write-Host "Not found (ok): $dir"
        }
    }
}

$roots = @(Get-VSCodeUserRoots)
if ($roots.Count -eq 0) {
    throw "Could not find any VS Code user data roots under %APPDATA%."
}

Write-Host "WorkspacePath: $WorkspacePath"
Write-Host "Mode: $([string]::Join('', @($(if ($Apply) { 'APPLY' } else { 'DRY RUN' }))))"

$foundAny = $false
foreach ($userRoot in $roots) {
    Write-Host "---"
    Write-Host "Searching: $userRoot"

    $entry = Get-WorkspaceStorageEntryForFolder -userRoot $userRoot -workspacePath $WorkspacePath
    if (-not $entry) {
        Write-Host "No matching workspaceStorage entry found."
        continue
    }

    $foundAny = $true
    Write-Host "Matched workspaceStorage entry: $entry"

    Remove-ExtensionWorkspaceCaches -workspaceStorageEntry $entry

    if ($ClearGlobalStorage) {
        Remove-GlobalStorageCaches -userRoot $userRoot
    }

    Write-Host "Done."
}

if (-not $foundAny) {
    Write-Host "No workspace storage entry found for that folder in any VS Code user root." 
    Write-Host "If you use a different VS Code build/profile location, pass the workspace path of the exact folder opened in VS Code." 
}

Write-Host "Next: Run 'Python: Restart Language Server' or 'Developer: Reload Window'."