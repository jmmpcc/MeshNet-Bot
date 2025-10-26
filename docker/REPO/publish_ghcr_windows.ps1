param(
  [Parameter(Mandatory=$true)][string]$User,      # ghcr.io/<User>
  [Parameter(Mandatory=$true)][string]$Token,     # PAT with write:packages
  [string]$Tag = "latest",                        # image tag: latest or v5.7 etc.
  [string]$BrokerDockerfile = "Dockerfile",       # path to broker/bot Dockerfile
  [string]$AprsDockerfile = "Dockerfile.aprs"     # path to APRS Dockerfile
)

$ErrorActionPreference = "Stop"

Write-Host "🔐 Logging in to ghcr.io as $User ..." -ForegroundColor Cyan
$Token | docker login ghcr.io -u $User --password-stdin | Out-Null

# Build/push broker
Write-Host "🐳 Building broker image..." -ForegroundColor Cyan
docker build -f $BrokerDockerfile -t ghcr.io/$User/meshtastic-broker:$Tag .
docker push ghcr.io/$User/meshtastic-broker:$Tag

# Build/push bot (assumes multi-stage or target 'bot' if used; fallback to same Dockerfile)
Write-Host "🐳 Building bot image..." -ForegroundColor Cyan
$botBuilt = $false
try {
  docker build -f $BrokerDockerfile --target bot -t ghcr.io/$User/meshtastic-bot:$Tag .
  $botBuilt = $true
} catch {
  Write-Host "ℹ️ No multi-stage target 'bot'. Will try building with same Dockerfile..." -ForegroundColor Yellow
}
if (-not $botBuilt) {
  docker build -f $BrokerDockerfile -t ghcr.io/$User/meshtastic-bot:$Tag .
}
docker push ghcr.io/$User/meshtastic-bot:$Tag

# Build/push aprs
Write-Host "🐳 Building APRS image..." -ForegroundColor Cyan
docker build -f $AprsDockerfile -t ghcr.io/$User/meshtastic-aprs:$Tag .
docker push ghcr.io/$User/meshtastic-aprs:$Tag

Write-Host "✅ Done. Images pushed with tag '$Tag'." -ForegroundColor Green