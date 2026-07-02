param(
  [Parameter(Mandatory=$true)] [string] $Query,
  [string] $Geo = "FR",
  [string] $Hl = "fr-FR",
  [string] $Timeframe = "today 12-m",
  [ValidateSet("COUNTRY", "REGION", "CITY")] [string] $RegionResolution = "REGION",
  [ValidateSet("chrome", "msedge")] [string] $BrowserChannel = "chrome",
  [string] $UserDataDir = "$env:TEMP\seo-trends-playwright-profile",
  [int] $TimeoutMs = 60000,
  [int] $KeepOpenMs = 3500,
  [int] $KeepOpenOnErrorMs = 0,
  [switch] $Headless,
  [string] $Fixture = ""
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $ScriptDir
try {
  if (-not (Test-Path "node_modules\playwright")) {
    npm install --no-audit --no-fund | Out-Host
  }

  $argsList = @("trends_runner.js")
  if ($Fixture -ne "") {
    $argsList += @("--fixture", $Fixture)
  } else {
    $argsList += @(
      "--query", $Query,
      "--geo", $Geo,
      "--hl", $Hl,
      "--timeframe", $Timeframe,
      "--region-resolution", $RegionResolution,
      "--browser-channel", $BrowserChannel,
      "--user-data-dir", $UserDataDir,
      "--timeout-ms", [string]$TimeoutMs,
      "--keep-open-ms", [string]$KeepOpenMs,
      "--keep-open-on-error-ms", [string]$KeepOpenOnErrorMs
    )
    if ($Headless) { $argsList += "--headless" }
  }

  & node @argsList
  exit $LASTEXITCODE
}
finally {
  Pop-Location
}
