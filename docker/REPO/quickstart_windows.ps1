param(
  [string]$ComposeFile = "docker-compose.sample.yml"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path ".env")) {
  if (Test-Path ".env-example.txt") {
    Copy-Item ".env-example.txt" ".env"
    Write-Host "📝 Created .env from .env-example.txt" -ForegroundColor Cyan
  } else {
    Write-Host "⚠️ .env-example.txt not found. Create .env manually." -ForegroundColor Yellow
  }
}

Write-Host "🔧 Opening .env in Notepad. Rellena tus datos y guarda el archivo..." -ForegroundColor Cyan
Start-Process notepad ".env"
Read-Host "Pulsa ENTER cuando hayas guardado .env"

# Ensure Docker Desktop is running
Write-Host "🐳 Checking Docker..." -ForegroundColor Cyan
docker version | Out-Null

Write-Host "🚀 Starting containers with '$ComposeFile'..." -ForegroundColor Cyan
docker compose -f $ComposeFile up -d

Write-Host "📜 Following logs (Ctrl+C para salir)..." -ForegroundColor Cyan
docker compose -f $ComposeFile logs -f