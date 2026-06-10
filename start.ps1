# start.ps1 — launch all three services in separate terminal windows
#
# Usage:
#   .\start.ps1              # rss + sec + fda ingestion (default)
#   .\start.ps1 --rss-only   # ingestion RSS only
#   .\start.ps1 --no-ingest  # skip ingestion (middleware + frontend only)

param(
    [switch]$RssOnly,
    [switch]$NoIngest
)

$root = $PSScriptRoot

# --- 1. Middleware (FastAPI + FinBERT) ---
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
    $ingestArgs = if ($RssOnly) { "--rss" } else { "--rss --sec --fda" }
    Start-Process powershell -ArgumentList "-NoExit", "-Command", @"
        Set-Location '$root\backend'
        Write-Host '=== Ingestion ===' -ForegroundColor Yellow
        python run_ingest.py $ingestArgs
"@
}

Write-Host ""
Write-Host "All services starting in separate windows." -ForegroundColor White
Write-Host "  Middleware  -> http://localhost:8000/docs" -ForegroundColor Cyan
Write-Host "  Frontend    -> http://localhost:3000" -ForegroundColor Green
if (-not $NoIngest) {
    Write-Host "  Ingestion   -> writing to MongoDB (financial_news.news_items)" -ForegroundColor Yellow
}
