# 一鍵啟動：Django 後台(8000) + FastAPI runtime(8001) + Streamlit 前端(8501)
# 用法：在專案資料夾打  .\start.ps1
# 就緒後自動開啟 WebSocket 前端 http://localhost:8001/demo（Streamlit 8501 仍跑著，需要再手動開）
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

# 等 FastAPI 真的就緒再開瀏覽器：--reload 首次啟動有時比固定 sleep 慢，輪詢 /health 比較可靠
$demoUrl = "http://localhost:8001/demo"
Write-Host "等待 FastAPI 就緒..." -ForegroundColor Cyan
$ready = $false
foreach ($i in 1..40) {   # 最多 20 秒
    try {
        Invoke-WebRequest "http://127.0.0.1:8001/health" -TimeoutSec 1 -UseBasicParsing | Out-Null
        $ready = $true
        break
    } catch {
        Start-Sleep -Milliseconds 500
    }
}
if ($ready) {
    Write-Host "開啟 $demoUrl" -ForegroundColor Green
    Start-Process $demoUrl
} else {
    Write-Host "FastAPI 未在 20 秒內就緒，略過自動開啟（稍後可自行開 $demoUrl）" -ForegroundColor Yellow
}

try {
    Write-Host "啟動 Streamlit 前端 (port 8501；不自動開瀏覽器，要用再手動開)..." -ForegroundColor Cyan
    Write-Host "關掉這個視窗 (Ctrl+C) 會一併關閉另外兩個服務" -ForegroundColor Cyan
    # 前端：當前視窗（結束它就結束整批）。--server.headless true ＝ 不要自己彈瀏覽器，
    # 我們上面已經開了 /demo；Streamlit 仍然跑著，8501 手動進得去。
    & $rt -m streamlit run --server.headless true (Join-Path $root "ui\streamlit_app.py")
}
finally {
    # 1) 先請 runtime 自己乾淨地收掉 Dcard 的 Chrome。
    #    不能只靠 app 的 atexit / lifespan：下面是 taskkill /F（硬殺），被硬殺的行程沒有機會收尾，
    #    而 Windows 不會連帶帶走子行程 —— 那顆 Chrome 就會變成孤兒繼續佔著記憶體。
    #    正常 quit 還能讓 Chrome 把 profile（含 cf_clearance）寫回磁碟，下次比較容易直接過盾。
    Write-Host "收掉 Dcard 瀏覽器..." -ForegroundColor Yellow
    try {
        Invoke-WebRequest "http://127.0.0.1:8001/internal/close-browser" -Method Post `
            -TimeoutSec 5 -UseBasicParsing | Out-Null
        Start-Sleep -Milliseconds 800   # 給 Chrome 幾百毫秒把自己收乾淨
    } catch {
        Write-Host "  (runtime 沒回應，略過；改由下面的孤兒清理處理)" -ForegroundColor DarkGray
    }

    Write-Host "關閉後端服務 (Django / FastAPI)..." -ForegroundColor Yellow
    foreach ($p in @($api, $django)) {
        if ($p -and -not $p.HasExited) {
            taskkill /PID $p.Id /T /F | Out-Null  # 連同 reload 子行程一起關
        }
    }

    # 2) 清掉孤兒 Chrome：--reload 每殺一次舊 worker，就會留下一顆父行程已死的 Chrome，
    #    它早已脫離上面那棵行程樹，taskkill /T 看不到它。用「指令列含我們的 profile 路徑」認人，
    #    所以只會殺到本專案開的那些，不會動到你自己在用的 Chrome。
    $profileDir = $null
    $envFile = Join-Path $root ".env"
    if (Test-Path $envFile) {
        $line = Select-String -Path $envFile -Pattern '^\s*DCARD_USER_DATA_DIR\s*=\s*(.+)$' |
                Select-Object -First 1
        if ($line) { $profileDir = $line.Matches[0].Groups[1].Value.Trim().Trim('"') }
    }
    if ($profileDir) {
        $orphans = Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" |
                   Where-Object { $_.CommandLine -and $_.CommandLine -like "*$profileDir*" }
        if ($orphans) {
            Write-Host "清理殘留的 Dcard 瀏覽器行程 ($($orphans.Count) 個)..." -ForegroundColor Yellow
            $orphans | ForEach-Object { taskkill /PID $_.ProcessId /T /F 2>$null | Out-Null }
        }
    }
}
