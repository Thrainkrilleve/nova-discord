# Pack everything Nova needs on the server onto a drive (Windows desktop, PowerShell 5.1).
# Stop Nova first so her memory is complete.
#
#   powershell -ExecutionPolicy Bypass -File scripts\export-for-server.ps1 -Dest E:\nova-transfer
#
# Writes:  <Dest>\var\          her memory (nova.db, web.db) and the files she wrote
#          <Dest>\models\       the model and its image projector (skip with -SkipModels)
#          <Dest>\images\       prebuilt Docker images, if they were saved to F:\nova-transfer\images
#          <Dest>\MANIFEST.txt  what's there, with checksums
param(
    [Parameter(Mandatory = $true)][string]$Dest,
    [string]$Repo = "",   # default: the repo this script is in
    [string]$ModelDir = "$env:USERPROFILE\.lmstudio\models\DavidAU\Qwen3.5-9B-The-Defiant-Fable-Uncensored-Heretic-NEO-IMATRIX-MAX-MTP-GGUF",
    [string]$ImagesFrom = "F:\nova-transfer\images",
    [string]$Python = "$env:USERPROFILE\.venvs\nova\Scripts\python.exe",
    [switch]$SkipModels
)
$ErrorActionPreference = "Stop"
if (-not $Repo) { $Repo = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path) }

$running = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like '*nova.exe*' }
if ($running) {
    Write-Host "Nova is still running (PID $($running.ProcessId -join ', ')). Stop her first, then run this again." -ForegroundColor Red
    exit 1
}

$var = Join-Path $Repo "var"
New-Item -ItemType Directory -Force -Path (Join-Path $Dest "var") | Out-Null

# SQLite's own backup: a consistent copy even with a write-ahead log beside the database
foreach ($db in @("nova.db", "web.db")) {
    $src = Join-Path $var $db
    if (Test-Path $src) {
        $dst = Join-Path $Dest "var\$db"
        if (Test-Path $dst) { Remove-Item $dst -Force }
        & $Python -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()" $src $dst
        if ($LASTEXITCODE -ne 0) { throw "backup of $db failed" }
        Write-Host "memory: $db"
    }
}
if (Test-Path (Join-Path $var "files")) {
    robocopy (Join-Path $var "files") (Join-Path $Dest "var\files") /E /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "copying var\files failed" }
    Write-Host "memory: files"
}

if (-not $SkipModels) {
    New-Item -ItemType Directory -Force -Path (Join-Path $Dest "models") | Out-Null
    foreach ($m in @("Qwen3.5-9B-The-Defiant-Fable-Uncnr-Heretic-plusIQ-TOOLS-NEO-MAX-MTP-Q8_0.gguf", "mmproj-F16.gguf")) {
        $src = Join-Path $ModelDir $m
        if (-not (Test-Path $src)) { throw "model not found: $src" }
        $dst = Join-Path $Dest "models\$m"
        if (-not (Test-Path $dst) -or (Get-Item $dst).Length -ne (Get-Item $src).Length) {
            Write-Host "models: copying $m (this takes a while)..."
            Copy-Item $src $dst -Force
        }
        Write-Host "models: $m"
    }
}

if (Test-Path $ImagesFrom) {
    robocopy $ImagesFrom (Join-Path $Dest "images") /E /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "copying images failed" }
    Write-Host "images: copied from $ImagesFrom"
}

$lines = @("Nova transfer, made $(Get-Date -Format s)", "")
Get-ChildItem $Dest -Recurse -File | Where-Object { $_.Name -ne "MANIFEST.txt" } | ForEach-Object {
    $rel = $_.FullName.Substring($Dest.TrimEnd('\').Length + 1)
    $sum = if ($_.Length -lt 200MB) { (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower() } else { "(large file, size only)" }
    $lines += "{0,14:N0}  {1}  {2}" -f $_.Length, $sum, $rel
}
$lines | Set-Content -Encoding utf8 (Join-Path $Dest "MANIFEST.txt")
Write-Host "Done: $Dest" -ForegroundColor Green
