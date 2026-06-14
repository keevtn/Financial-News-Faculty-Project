# start.ps1 — launch all three services in separate terminal windows
#
# Usage:
#   .\start.ps1              # structured only: rss + sec + fda (default)
#   .\start.ps1 -Social      # structured + unstructured (StockTwits + Bluesky)
#   .\start.ps1 -RssOnly     # structured RSS only
#   .\start.ps1 -NoIngest    # skip ingestion (middleware + frontend only)

param(
    [switch]$RssOnly,
    [switch]$NoIngest,
    [switch]$Social
)

$root = $PSScriptRoot

# --- 1. Middleware (FastAPI) ---
Start-Process powershell -ArgumentList "-NoExit", "-Command", @"
    Set-Location '$root'
    Write-Host '=== Middleware ===' -ForegroundColor Cyan
    uvicorn middleware.api:app --reload --port 8000
"@

# --- 2. Frontend (Next.js) ---
Start-Process powershell -ArgumentList "-NoExit", "-Command", @"
    Set-Location '$root\frontend'
    Write-Host '=== Frontend ===' -ForegroundColor Green
    npm run dev
"@

# --- 3. Ingestion pipeline ---
if (-not $NoIngest) {
    # Structured sources
    $structuredArgs = if ($RssOnly) { "--rss" } else { "--rss --sec --fda" }

    # Unstructured sources (opt-in via -Social flag)
    # Social uses fast-path scoring — StockTwits human labels + LM keyword fallback.
    # These run in the same process as structured sources via the shared dispatcher.
    $socialArgs = if ($Social) { "--stocktwits --bluesky" } else { "" }

    $ingestArgs = "$structuredArgs $socialArgs".Trim()

    Start-Process powershell -ArgumentList "-NoExit", "-Command", @"
        Set-Location '$root\backend'
        Write-Host '=== Ingestion ===' -ForegroundColor Yellow
        python run_ingest.py $ingestArgs
"@
}

Write-Host ""
Write-Host "All services starting in separate windows." -ForegroundColor White
Write-Host "  Middleware -> http://localhost:8000/docs" -ForegroundColor Cyan
Write-Host "  Frontend  -> http://localhost:3000" -ForegroundColor Green
if (-not $NoIngest) {
    if ($Social) {
        Write-Host "  Ingestion -> structured (RSS, SEC, FDA) + social (StockTwits, Bluesky)" -ForegroundColor Yellow
    } else {
        Write-Host "  Ingestion -> structured only (RSS, SEC, FDA)  |  add -Social to include social feeds" -ForegroundColor Yellow
    }
}
