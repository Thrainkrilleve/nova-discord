# Start llama-server with a model, run the Nova eval against it, then stop the server.
#   .\evals\eval-model.ps1 -Model C:\models\x.gguf -Name x [-Repeat 2] [-CpuMoe 8] [-Mtp]
# -CpuMoe N keeps the experts of the first N layers in system RAM, for MoE models bigger than VRAM.
# -Mtp enables multi-token-prediction speculative decoding for models that ship MTP heads.
param(
    [Parameter(Mandatory = $true)][string]$Model,
    [Parameter(Mandatory = $true)][string]$Name,
    [int]$Repeat = 2,
    [int]$CpuMoe = 0,
    [int]$Context = 16384,
    [string]$Config = "",
    [switch]$Mtp,
    [string]$Only = "",
    [int]$MtpDraft = 2,
    [string]$Server = "$env:USERPROFILE\.unsloth\llama.cpp\build\bin\Release\llama-server.exe",
    [string]$Python = "$env:USERPROFILE\.venvs\nova\Scripts\python.exe"
)

$root = Split-Path $PSScriptRoot -Parent
$log = Join-Path $env:TEMP "nova-eval-$Name.log"
$serverArgs = @("-m", "`"$Model`"", "--jinja", "-ngl", "99", "-c", $Context, "--host", "127.0.0.1", "--port", "8080")
if ($CpuMoe -gt 0) { $serverArgs += @("--n-cpu-moe", $CpuMoe) }
# The model's own MTP head drafts tokens; ~2 per step is where acceptance stays above ~50%
if ($Mtp) { $serverArgs += @("--spec-type", "draft-mtp", "--spec-draft-n-max", $MtpDraft) }

$proc = Start-Process -FilePath $Server -ArgumentList $serverArgs -RedirectStandardError $log -WindowStyle Hidden -PassThru
try {
    $ready = $false
    for ($i = 0; $i -lt 120; $i++) {
        if ($proc.HasExited) { break }
        try { if ((Invoke-RestMethod http://127.0.0.1:8080/health -TimeoutSec 2).status -eq "ok") { $ready = $true; break } } catch {}
        Start-Sleep 3
    }
    if (-not $ready) {
        Write-Error "llama-server did not come up; last log lines:"
        Get-Content $log -Tail 15
        exit 1
    }
    $evalArgs = @("--name", $Name, "--repeat", $Repeat)
    if ($Config) { $evalArgs += @("--config", $Config) }
    if ($Only) { $evalArgs += @("--only", $Only) }
    & $Python -X utf8 (Join-Path $root "evals\run_eval.py") @evalArgs
}
finally {
    if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force }
}
