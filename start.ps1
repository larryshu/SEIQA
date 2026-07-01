# 一鍵啟動：Django 後台(8000) + FastAPI runtime(8001) + Streamlit 前端(8501)
# 用法：在專案資料夾打  .\start.ps1
$ErrorActionPreference = "Stop"
$root  = $PSScriptRoot
$rt    = Join-Path $root ".venv\Scripts\python.exe"        # runtime / streamlit 環境
$admin = Join-Path $root ".venv-admin\Scripts\python.exe"  # Django 後台環境

Write-Host "啟動 Django 後台 (port 8000)..." -ForegroundColor Cyan
# 後台：另開視窗
$django = Start-Process -FilePath $admin `
    -ArgumentList "admin_backend\manage.py", "runserver", "127.0.0.1:8000" `
    -WorkingDirectory $root -PassThru

Write-Host "啟動 FastAPI runtime (port 8001)..." -ForegroundColor Cyan
# runtime：另開視窗
$api = Start-Process -FilePath $rt `
    -ArgumentList "-m", "uvicorn", "app.api:app", "--reload", "--port", "8001" `
    -WorkingDirectory $root -PassThru

Start-Sleep -Seconds 3

try {
    Write-Host "啟動 Streamlit 前端 (port 8501)... 關掉它 (Ctrl+C) 會一併關閉另外兩個服務" -ForegroundColor Cyan
    # 前端：當前視窗（結束它就結束整批）
    & $rt -m streamlit run (Join-Path $root "ui\streamlit_app.py")
}
finally {
    Write-Host "關閉後端服務 (Django / FastAPI)..." -ForegroundColor Yellow
    foreach ($p in @($api, $django)) {
        if ($p -and -not $p.HasExited) {
            taskkill /PID $p.Id /T /F | Out-Null  # 連同 reload 子行程一起關
        }
    }
}
