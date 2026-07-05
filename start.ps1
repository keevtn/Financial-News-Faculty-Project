# start.ps1 - launch all three services in separate terminal windows
#
# Usage:
#   .\start.ps1              # structured only: rss + sec + fda (default)
#   .\start.ps1 -Social      # structured + unstructured (StockTwits + Bluesky)
#   .\start.ps1 -RssOnly     # structured RSS only
#   .\start.ps1 -NoIngest    # skip ingestion (middleware + frontend only)
#   .\start.ps1 -NoBackfill  # skip the social-sentiment backfill self-heal step
#   .\start.ps1 -CatalystUniverse  # also run the 12h candidate-universe scheduler (no LLM cost)
#
# Notes:
#   * The middleware runs WITHOUT uvicorn --reload on purpose. Under OneDrive the
#     reload file-watcher is unreliable AND its multiprocessing.spawn workers get
#     orphaned on window close, leaving a stale process holding port 8000 that
#     serves old code. A single-process uvicorn launched from the venv Python is
#     predictable: edit code, close the Middleware window, re-run start.ps1.
#   * Every service is launched with the project venv's python.exe explicitly so
#     nothing ever falls back to a system Python that lacks the dependencies.

param(
    [switch]$RssOnly,
    [switch]$NoIngest,
    [switch]$Social,
    [switch]$NoBackfill,
    [switch]$CatalystUniverse
)

$root = $PSScriptRoot
$venvPy = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $venvPy)) {
    Write-Host "ERROR: venv Python not found at $venvPy" -ForegroundColor Red
    Write-Host "Create it first:  python -m venv .venv ; .\.venv\Scripts\pip install -r requirements.txt" -ForegroundColor Red
    exit 1
}

# --- 0. Pre-flight: free port 8000 from stale uvicorn / reload-orphan workers ---
# Closing a previous Middleware window can leave orphaned multiprocessing.spawn
# children (from --reload) that still hold the listen socket and serve old code.
# Kill anything bound to 8000 plus any stray uvicorn/spawn workers before launch.
Write-Host "Clearing port 8000..." -ForegroundColor DarkGray
$cleared = $false
for ($i = 0; $i -lt 6; $i++) {
    $owners = (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue).OwningProcess
    $stray = (Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe' OR Name='uvicorn.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*uvicorn*' -or $_.CommandLine -like '*spawn_main*' -or $_.CommandLine -like '*middleware.api*' }).ProcessId
    $targets = @($owners) + @($stray) | Where-Object { $_ } | Select-Object -Unique
    if (-not $targets) { $cleared = $true; break }
    foreach ($procId in $targets) { Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Milliseconds 600
}
$stillUsed = [bool](Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue)
if (-not $cleared -and $stillUsed) {
    Write-Host "WARN: port 8000 still in use - the middleware may fail to bind." -ForegroundColor Yellow
} else {
    Write-Host "Port 8000 is free." -ForegroundColor DarkGray
}

# --- 1. Middleware (FastAPI) - single process, venv Python, no --reload ---
# -CatalystUniverse turns on the 12h candidate-universe scheduler by exporting its
# env flag inside the child window (the backtick keeps $env literal until then).
$universeEnv = if ($CatalystUniverse) { "`$env:RUN_CATALYST_UNIVERSE_SCHEDULER = 'true'" } else { "" }
Start-Process powershell -ArgumentList "-NoExit", "-Command", @"
    Set-Location '$root'
    $universeEnv
    Write-Host '=== Middleware ===' -ForegroundColor Cyan
    & '$venvPy' -m uvicorn middleware.api:app --port 8000
"@

# --- 2. Frontend (Next.js) ---
Start-Process powershell -ArgumentList "-NoExit", "-Command", @"
    Set-Location '$root\frontend'
    Write-Host '=== Frontend ===' -ForegroundColor Green
    npm run dev
"@

# --- 3. Ingestion pipeline ---
# RSS/social and SEC/FDA run in SEPARATE processes so high-volume RSS cycling
# can never starve regulatory source polls or mask their failures.
if (-not $NoIngest) {
    # Window 3a: RSS + optional social (high-frequency, 60 s cycles)
    $socialArgs = if ($Social) { "--stocktwits --bluesky" } else { "" }
    # --sentiment (LM) is REQUIRED here: without it MongoHandler stores structured
    # docs with no sentiment field, and $setOnInsert makes that permanent.
    $rssArgs = ("--rss --sentiment $socialArgs").Trim()

    Start-Process powershell -ArgumentList "-NoExit", "-Command", @"
        Set-Location '$root\backend'
        Write-Host '=== Ingestion: RSS$(if ('$Social' -eq 'True') { ' + Social' } else { '' }) ===' -ForegroundColor Yellow
        & '$venvPy' run_ingest.py $rssArgs
"@

    # Window 3b: SEC + FDA (regulatory, independent — skipped with -RssOnly)
    if (-not $RssOnly) {
        Start-Process powershell -ArgumentList "-NoExit", "-Command", @"
            Set-Location '$root\backend'
            Write-Host '=== Ingestion: SEC + FDA ===' -ForegroundColor Magenta
            & '$venvPy' run_ingest.py --sec --fda --sentiment
"@
    }
}

# --- 4. Social sentiment backfill (self-heal) ---
# Repairs any social docs that predate sentiment scoring / HTML stripping. It is
# idempotent and fast: on a healthy DB it touches 0 docs. Runs in THIS window so
# its summary is visible. Skip with -NoBackfill.
if (-not $NoBackfill -and -not $NoIngest) {
    Write-Host ""
    Write-Host "Backfilling social sentiment (needs-repair only)..." -ForegroundColor Magenta
    & "$venvPy" "$root\backend\backfill_social.py"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  (backfill skipped/failed - is MongoDB running and MONGODB_URI set? Non-fatal.)" -ForegroundColor DarkYellow
    }
}

Write-Host ""
Write-Host "All services starting in separate windows." -ForegroundColor White
Write-Host "  Middleware -> http://localhost:8000/docs" -ForegroundColor Cyan
Write-Host "  Frontend  -> http://localhost:3000" -ForegroundColor Green
if ($CatalystUniverse) {
    Write-Host "  Catalyst universe scheduler -> ON (12h cadence, no LLM cost)" -ForegroundColor Cyan
}
if (-not $NoIngest) {
    if ($RssOnly) {
        Write-Host "  Ingestion -> [RSS only]$(if ($Social) { ' + Social' } else { '' })  (SEC+FDA skipped via -RssOnly)" -ForegroundColor Yellow
    } elseif ($Social) {
        Write-Host "  Ingestion -> [Window A] RSS + Social (StockTwits + Bluesky)" -ForegroundColor Yellow
        Write-Host "           -> [Window B] SEC + FDA  (independent process)" -ForegroundColor Magenta
    } else {
        Write-Host "  Ingestion -> [Window A] RSS  |  [Window B] SEC + FDA  |  add -Social for StockTwits + Bluesky" -ForegroundColor Yellow
    }
}
