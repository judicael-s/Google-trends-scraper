param(
  [string] $Query = "cahier de vacances maths",
  [string] $Geo = "FR",
  [string] $Hl = "fr-FR",
  [string] $Timeframe = "today 12-m",
  [ValidateSet("chrome", "msedge")] [string] $BrowserChannel = "chrome",
  [string] $UserDataDir = "$env:TEMP\seo-trends-playwright-profile"
)

$ErrorActionPreference = "Stop"

$encodedQuery = [uri]::EscapeDataString($Query)
$encodedTimeframe = [uri]::EscapeDataString($Timeframe)
$url = "https://trends.google.com/trends/explore?date=$encodedTimeframe&geo=$Geo&q=$encodedQuery&hl=$Hl"

if ($BrowserChannel -eq "msedge") {
  $browser = "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
  if (-not (Test-Path $browser)) { $browser = "C:\Program Files\Microsoft\Edge\Application\msedge.exe" }
} else {
  $browser = "C:\Program Files\Google\Chrome\Application\chrome.exe"
}

if (-not (Test-Path $browser)) {
  throw "Browser executable not found: $browser"
}

Start-Process -FilePath $browser -ArgumentList @(
  "--user-data-dir=$UserDataDir",
  $url
)

Write-Output "Opened $BrowserChannel with profile: $UserDataDir"
Write-Output "URL: $url"
Write-Output "Leave this browser open, interact with Google Trends manually if needed, then rerun run-trends.ps1 later."
