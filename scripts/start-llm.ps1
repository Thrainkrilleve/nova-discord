# Start llama-server (Unsloth Studio's ROCm build) with a GGUF model for Nova.
#   .\scripts\start-llm.ps1 -Model C:\models\some-model.Q4_K_M.gguf [-Mtp]
# -Mtp turns on multi-token-prediction speculative decoding for models that ship an MTP head
# (file names containing "MTP"); about 50% faster with identical output.
param(
    [Parameter(Mandatory = $true)][string]$Model,
    [int]$Context = 16384,
    [int]$Port = 8080,
    [switch]$Mtp,
    # Vision projector for image input, e.g. the model repo's mmproj-F16.gguf
    [string]$Mmproj = "",
    [string]$Server = "$env:USERPROFILE\.unsloth\llama.cpp\build\bin\Release\llama-server.exe"
)

if (-not (Test-Path $Server)) { Write-Error "llama-server not found at $Server"; exit 1 }
if (-not (Test-Path $Model)) { Write-Error "Model not found: $Model"; exit 1 }

# --jinja enables the model's own chat template, which tool calling needs.
# -ngl 99 puts every layer on the GPU; lower it if the model does not fit in 16 GB.
$serverArgs = @("-m", $Model, "--jinja", "-ngl", "99", "-c", $Context, "--host", "127.0.0.1", "--port", $Port)
if ($Mtp) { $serverArgs += @("--spec-type", "draft-mtp", "--spec-draft-n-max", "2") }
if ($Mmproj) { $serverArgs += @("--mmproj", $Mmproj) }
& $Server @serverArgs
