[CmdletBinding()]
param(
    # Path to a JSON settings template (object at top-level)
    [Parameter()]
    [string]$TemplatePath = (Join-Path $PSScriptRoot 'settings.copilot.json'),

    # Apply to these VS Code profile names (matched against profile metadata when available).
    # If profile metadata can't be read, the directory name is used.
    [Parameter()]
    [string[]]$ProfileNames = @('Default', 'Robert'),

    # Apply to all profiles found under %APPDATA%\Code\User\profiles
    [Parameter()]
    [switch]$AllProfiles,

    # Also apply to the non-profile user settings file (%APPDATA%\Code\User\settings.json)
    [Parameter()]
    [switch]$IncludeUserSettings
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Read-JsonFile {
    param([Parameter(Mandatory)] [string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return @{}
    }

    $raw = Get-Content -LiteralPath $Path -Raw -Encoding UTF8
    if ([string]::IsNullOrWhiteSpace($raw)) {
        return @{}
    }

    try {
        return ($raw | ConvertFrom-Json -AsHashtable)
    }
    catch {
        throw "Failed to parse JSON: $Path`n$($_.Exception.Message)"
    }
}

function Write-JsonFile {
    param(
        [Parameter(Mandatory)] [string]$Path,
        [Parameter(Mandatory)] [hashtable]$Object
    )

    $json = $Object | ConvertTo-Json -Depth 50
    $dir = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    Set-Content -LiteralPath $Path -Value $json -Encoding UTF8 -NoNewline
}

function Backup-File {
    param([Parameter(Mandatory)] [string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return $null
    }

    $timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $backupPath = "$Path.bak-$timestamp"
    Copy-Item -LiteralPath $Path -Destination $backupPath -Force
    return $backupPath
}

function Merge-Settings {
    param(
        [Parameter(Mandatory)] [hashtable]$Target,
        [Parameter(Mandatory)] [hashtable]$Template
    )

    foreach ($key in $Template.Keys) {
        $Target[$key] = $Template[$key]
    }

    return $Target
}

function Get-VSCodeProfileName {
    param([Parameter(Mandatory)] [string]$ProfileDir)

    $profileJson = Join-Path $ProfileDir 'profile.json'
    if (Test-Path -LiteralPath $profileJson) {
        try {
            $meta = Read-JsonFile -Path $profileJson
            if ($meta.ContainsKey('name') -and -not [string]::IsNullOrWhiteSpace([string]$meta['name'])) {
                return [string]$meta['name']
            }
        }
        catch {
            # Ignore and fall back to folder name
        }
    }

    return (Split-Path -Leaf $ProfileDir)
}

$template = Read-JsonFile -Path $TemplatePath
if ($template.Count -eq 0) {
    throw "Template is empty or missing: $TemplatePath"
}

$codeUserDir = Join-Path $env:APPDATA 'Code\User'
$profilesRoot = Join-Path $codeUserDir 'profiles'

Write-Host "VS Code user dir: $codeUserDir"
Write-Host "VS Code profiles root: $profilesRoot"
Write-Host "Template: $TemplatePath"

if ($IncludeUserSettings) {
    $userSettingsPath = Join-Path $codeUserDir 'settings.json'
    $backup = Backup-File -Path $userSettingsPath
    if ($backup) { Write-Host "Backed up user settings -> $backup" }

    $current = Read-JsonFile -Path $userSettingsPath
    $merged = Merge-Settings -Target $current -Template $template
    Write-JsonFile -Path $userSettingsPath -Object $merged
    Write-Host "Updated user settings: $userSettingsPath"
}

if (-not (Test-Path -LiteralPath $profilesRoot)) {
    Write-Warning "Profiles root not found. If you don't use profiles, rerun with -IncludeUserSettings."
    exit 0
}

$profileDirs = @(Get-ChildItem -LiteralPath $profilesRoot -Directory)
if (-not $profileDirs) {
    Write-Warning "No profile directories found under: $profilesRoot"
    exit 0
}

$targets = @()
foreach ($dir in $profileDirs) {
    $name = Get-VSCodeProfileName -ProfileDir $dir.FullName
    if ($AllProfiles -or ($ProfileNames -contains $name)) {
        $targets += [pscustomobject]@{ Name = $name; Path = $dir.FullName }
    }
}

if (-not $targets) {
    if (-not $AllProfiles -and $profileDirs.Count -eq 1) {
        $onlyDir = $profileDirs | Select-Object -First 1
        $onlyName = Get-VSCodeProfileName -ProfileDir $onlyDir.FullName
        Write-Warning "No matching profiles found for: $($ProfileNames -join ', '). Applying to the only profile folder found: $onlyName"
        $targets = @([pscustomobject]@{ Name = $onlyName; Path = $onlyDir.FullName })
    }
    else {
        $available = $profileDirs | ForEach-Object { Get-VSCodeProfileName -ProfileDir $_.FullName }
        Write-Warning "No matching profiles found. Available profile names: $($available -join ', ')"
        exit 0
    }
}

foreach ($t in $targets) {
    $settingsPath = Join-Path $t.Path 'settings.json'
    $backup = Backup-File -Path $settingsPath
    if ($backup) { Write-Host "Backed up [$($t.Name)] -> $backup" }

    $current = Read-JsonFile -Path $settingsPath
    $merged = Merge-Settings -Target $current -Template $template
    Write-JsonFile -Path $settingsPath -Object $merged

    Write-Host "Updated profile [$($t.Name)]: $settingsPath"
}

Write-Host "Done. Reload VS Code windows to apply." -ForegroundColor Green
