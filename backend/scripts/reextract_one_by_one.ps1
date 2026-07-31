# 逐份重抽：每份文件用「獨立的 python 程序」處理。
#
# 為什麼不一次跑完：實測單一程序處理完第一份（810H，2453 個區塊）之後，
# 下一份開始時程序就以離開碼 1 中止，且 stdout/stderr 完全沒有輸出、
# 沒有 traceback。改成一份一個程序後，即使某份仍會觸發，也只影響那一份，
# 而且能明確看出是在哪一份、哪一步發生。
#
# 用法：powershell -ExecutionPolicy Bypass -File scripts\reextract_one_by_one.ps1

$ErrorActionPreference = "Continue"
Set-Location (Join-Path $PSScriptRoot "..")
$py = ".venv\Scripts\python.exe"
$env:PYTHONIOENCODING = "utf-8"
# PaddleOCR 啟動時會往 stderr 寫一行「Checking connectivity to the model hosters…」。
# PowerShell 5.1 會把原生指令的 stderr 包成 ErrorRecord（NativeCommandError），
# 使整條管線被判定為失敗而中止 —— 這正是先前「第一份完成後下一份立刻 exit=1
# 且零輸出」的原因，並非程式崩潰。關掉那個檢查即可根除。
$env:PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK = "True"
if (-not (Test-Path "logs")) { New-Item -ItemType Directory "logs" | Out-Null }

$titles = @(
  "MIL-STD-810H",
  "MIL-DTL-901E",
  "MIL-HDBK-704-8",
  "MIL-PRF-7808L",
  "MIL-STD-209K",
  "MIL-STD-461G",
  "MIL-STD-704F",
  "MIL-STD-1320D"
)

foreach ($t in $titles) {
  Write-Output "=== $t ==="
  $log = "logs\reextract_$($t -replace '[^A-Za-z0-9\-]','_').log"
  & $py -u "scripts\reextract_rotated_docs.py" --only $t --backup-dir "backup_rotated_run" *> $log
  $rc = $LASTEXITCODE
  $done = Select-String -Path $log -Pattern "\[完成\]|\[失敗\]" -ErrorAction SilentlyContinue
  if ($done) { $done | ForEach-Object { Write-Output ("  " + $_.Line.Trim()) } }
  Write-Output "  exit=$rc"
}
Write-Output "全部結束"
