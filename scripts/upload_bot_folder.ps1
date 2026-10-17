<#
Upload one bot folder from this laptop to the Master repo on GitHub, on its own
branch, so Claude can read it. Secrets and logs are left behind.

How to run (PowerShell):
    powershell -ExecutionPolicy Bypass -File upload_bot_folder.ps1
or name the folder directly:
    powershell -ExecutionPolicy Bypass -File upload_bot_folder.ps1 -Folder C:\Users\jeffl\gold_put_bot

The first push opens a browser window to sign in to GitHub. That is normal.
#>
param(
    [string]$Folder,
    [string]$Repo   = "https://github.com/JL304C/Master.git",
    [string]$Branch = "laptop-upload"
)

$ErrorActionPreference = "Stop"

# Files that must never go to GitHub: keys, passwords, logs, caches.
$Exclude = @(".env", ".env.*", "*.key", "*.pem", "*.p12", "*secret*", "*token*", "*password*",
             "*.log", "*.jsonl", "*.csv", "*.db", "*.sqlite", "*.parquet", "*.pyc")
$ExcludeDirs = @("__pycache__", ".venv", "venv", "env", ".git", "logs", "cache", "node_modules")

# 1. Git installed?
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Host "Git is not installed. Install it from https://git-scm.com/download/win (all defaults are fine), then run this again." -ForegroundColor Red
    exit 1
}

# 2. Which folder?
if (-not $Folder) {
    Write-Host "Bot folders in C:\Users\$env:USERNAME :"
    Get-ChildItem "C:\Users\$env:USERNAME" -Directory | Where-Object { $_.Name -match "bot|gold|put" } |
        ForEach-Object { Write-Host "   $($_.FullName)" }
    $Folder = Read-Host "Full path of the folder to upload"
}
$Folder = $Folder.Trim('"')
if (-not (Test-Path $Folder -PathType Container)) { Write-Host "Folder not found: $Folder" -ForegroundColor Red; exit 1 }
$Name = Split-Path $Folder -Leaf

# 3. List what will be uploaded, and what is skipped.
$all = Get-ChildItem $Folder -Recurse -File | Where-Object {
    $rel = $_.FullName.Substring($Folder.Length).TrimStart('\')
    -not ($ExcludeDirs | Where-Object { $rel -split '\\' -contains $_ })
}
$keep = @(); $skip = @()
foreach ($f in $all) {
    if ($Exclude | Where-Object { $f.Name -like $_ }) { $skip += $f } else { $keep += $f }
}
Write-Host "`nWill upload ($($keep.Count) files):" -ForegroundColor Green
$keep | ForEach-Object { Write-Host "   $($_.FullName.Substring($Folder.Length))" }
Write-Host "`nSkipping ($($skip.Count) files: keys, logs, data):" -ForegroundColor Yellow
$skip | ForEach-Object { Write-Host "   $($_.FullName.Substring($Folder.Length))" }

# 4. Warn about anything that looks like a key written inside the code.
$suspect = $keep | Where-Object { $_.Length -lt 2MB } | Select-String -Pattern `
    '(api[_-]?key|secret|password|pwd|token)\s*[:=]\s*["'']?[A-Za-z0-9/+_\-]{12,}' -List
if ($suspect) {
    Write-Host "`nWARNING: these files look like they contain a key or password written in the code:" -ForegroundColor Red
    $suspect | ForEach-Object { Write-Host "   $($_.Path)  (line $($_.LineNumber))" }
    Write-Host "Move those values into the .env file before uploading, or answer N below." -ForegroundColor Red
}

$ok = Read-Host "`nUpload these files to GitHub branch '$Branch'? (Y/N)"
if ($ok -notmatch '^[Yy]') { Write-Host "Nothing uploaded."; exit 0 }

# 5. Fresh copy of the repo in a temp folder (your own files are not touched).
$work = Join-Path $env:TEMP "master_upload_$(Get-Date -Format yyyyMMddHHmmss)"
git clone --quiet $Repo $work
if ($LASTEXITCODE -ne 0) { Write-Host "Could not download the repo. Check your internet / GitHub sign-in." -ForegroundColor Red; exit 1 }
Push-Location $work
try {
    git ls-remote --exit-code --heads origin $Branch *> $null
    if ($LASTEXITCODE -eq 0) { git checkout --quiet -b $Branch "origin/$Branch" } else { git checkout --quiet -b $Branch }

    if (-not (git config user.name))  { git config user.name  $env:USERNAME }
    if (-not (git config user.email)) { git config user.email "$env:USERNAME@users.noreply.github.com" }

    $dest = Join-Path $work $Name
    foreach ($f in $keep) {
        $rel = $f.FullName.Substring($Folder.Length).TrimStart('\')
        $to = Join-Path $dest $rel
        New-Item -ItemType Directory -Force -Path (Split-Path $to) | Out-Null
        Copy-Item $f.FullName $to -Force
    }
    git add -- $Name
    git commit --quiet -m "Upload $Name from laptop"
    if ($LASTEXITCODE -ne 0) { Write-Host "No changes since the last upload." -ForegroundColor Yellow; exit 0 }
    git push --quiet -u origin $Branch
    if ($LASTEXITCODE -ne 0) { Write-Host "Push failed. If a GitHub sign-in window appeared, finish it and run again." -ForegroundColor Red; exit 1 }
    Write-Host "`nDone. '$Name' is on GitHub branch '$Branch'." -ForegroundColor Green
    Write-Host "Tell Claude: 'uploaded $Name to the $Branch branch'."
}
finally {
    Pop-Location
    Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
}
