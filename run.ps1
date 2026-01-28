# Start both backend and frontend servers

Write-Host "Starting backend server..." -ForegroundColor Cyan
$backend = Start-Process -PassThru -NoNewWindow powershell -ArgumentList "-Command", "cd '$PSScriptRoot\backend'; python main.py"

Write-Host "Starting frontend server..." -ForegroundColor Cyan
$frontend = Start-Process -PassThru -NoNewWindow powershell -ArgumentList "-Command", "cd '$PSScriptRoot\frontend'; npm start"

Write-Host ""
Write-Host "Backend running at http://localhost:8000" -ForegroundColor Green
Write-Host "Frontend running at http://localhost:3000" -ForegroundColor Green
Write-Host "Press Ctrl+C to stop both servers." -ForegroundColor Yellow

try {
    Wait-Process -Id $backend.Id, $frontend.Id
} finally {
    Stop-Process -Id $backend.Id -ErrorAction SilentlyContinue
    Stop-Process -Id $frontend.Id -ErrorAction SilentlyContinue
}
