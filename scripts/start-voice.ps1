# Start Nova's voice services (Windows). Leave the window open; Ctrl+C stops both.
#   speech to text: whisper.cpp server on :8003 (CPU; the ROCm build here lacks gfx1201 kernels)
#   text to speech: pocket-tts server on :8002 (CPU, ~1 s per sentence)
# Then in nova.toml:  [voice]  whisper_url = "http://127.0.0.1:8003"  tts_url = "http://127.0.0.1:8002"
param(
    [string]$Whisper = "$env:USERPROFILE\.unsloth\whisper.cpp\build\bin\Release\whisper-server.exe",
    [string]$WhisperModel = "$env:USERPROFILE\.nova\models\whisper\ggml-small-q5_1.bin",
    [string]$PocketTts = "$env:USERPROFILE\.venvs\nllb\Scripts\pocket-tts.exe"
)

foreach ($p in $Whisper, $WhisperModel, $PocketTts) {
    if (-not (Test-Path $p)) { Write-Error "Missing: $p"; exit 1 }
}
$stt = Start-Process -FilePath $Whisper -ArgumentList @("-m", "`"$WhisperModel`"", "-l", "auto", "-ng", "-t", "8",
    "--host", "127.0.0.1", "--port", "8003") -NoNewWindow -PassThru
$tts = Start-Process -FilePath $PocketTts -ArgumentList @("serve", "--host", "127.0.0.1", "--port", "8002") -NoNewWindow -PassThru
Write-Host "whisper (pid $($stt.Id)) on :8003, pocket-tts (pid $($tts.Id)) on :8002. Ctrl+C to stop."
try { Wait-Process -Id $stt.Id, $tts.Id }
finally {
    foreach ($p in $stt, $tts) { if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force } }
}
