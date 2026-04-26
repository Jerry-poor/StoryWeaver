$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$chat = Join-Path $root 'chat'
$data = Join-Path $chat 'data'
$chapters = Join-Path $data 'chapters'
$pyc = Join-Path $chat '__pycache__\app.cpython-312.pyc'

function Remove-IfExists([string]$Path) {
  if (Test-Path $Path) {
    Remove-Item -LiteralPath $Path -Force
  }
}

function Wait-Http([string]$Url, [int]$TimeoutSec = 40) {
  $deadline = (Get-Date).AddSeconds($TimeoutSec)
  while ((Get-Date) -lt $deadline) {
    try {
      $conn = [System.Net.HttpWebRequest]::Create($Url)
      $conn.Timeout = 5000
      $resp = $conn.GetResponse()
      $resp.Close()
      return
    } catch {
      Start-Sleep -Milliseconds 500
    }
  }
  throw "Timed out waiting for $Url"
}

Write-Host 'Cleaning generated files...'
@(
  (Join-Path $data 'characters.json'),
  (Join-Path $data 'conversation_memory.json'),
  (Join-Path $data 'outline_draft.json'),
  (Join-Path $data 'outline.json'),
  (Join-Path $data 'story_brief.json'),
  (Join-Path $data 'storyline.json')
) | ForEach-Object { Remove-IfExists $_ }

if (Test-Path $chapters) {
  Get-ChildItem $chapters -Filter 'chapter_*.json' -File | ForEach-Object {
    Remove-Item -LiteralPath $_.FullName -Force
  }
}
Remove-IfExists $pyc

Write-Host 'Starting app...'
$app = Start-Process -WindowStyle Hidden -PassThru -FilePath python -ArgumentList 'app.py' -WorkingDirectory $chat

try {
  Wait-Http 'http://127.0.0.1:8787/api/state'
  $brief = '一个年轻人追查姐姐失踪与旧城秘密之间的联系。'

  Write-Host 'Generating outline...'
  $outlineReq = [System.Net.HttpWebRequest]::Create('http://127.0.0.1:8787/api/generate-outline')
  $outlineReq.Method = 'POST'
  $outlineReq.ContentType = 'application/json'
  $outlineReq.Headers.Add('Accept', 'application/json')
  $outlineBody = [System.Text.Encoding]::UTF8.GetBytes((@{ story_brief = $brief } | ConvertTo-Json -Compress))
  $outStream = $outlineReq.GetRequestStream()
  $outStream.Write($outlineBody, 0, $outlineBody.Length)
  $outStream.Close()
  $outlineResp = $outlineReq.GetResponse()
  $outlineJson = New-Object System.IO.StreamReader($outlineResp.GetResponseStream()).ReadToEnd()
  $outlineResp.Close()
  $outline = $outlineJson | ConvertFrom-Json

  Write-Host 'Confirming outline...'
  $confirmReq = [System.Net.HttpWebRequest]::Create('http://127.0.0.1:8787/api/confirm-outline')
  $confirmReq.Method = 'POST'
  $confirmReq.ContentType = 'application/json'
  $confirmReq.Headers.Add('Accept', 'application/json')
  $confirmBody = [System.Text.Encoding]::UTF8.GetBytes((@{
    outline_json = ($outline.outline_draft | ConvertTo-Json -Depth 20 -Compress)
    story_brief = $brief
  } | ConvertTo-Json -Compress))
  $outStream = $confirmReq.GetRequestStream()
  $outStream.Write($confirmBody, 0, $confirmBody.Length)
  $outStream.Close()
  $confirmResp = $confirmReq.GetResponse()
  $confirmJson = New-Object System.IO.StreamReader($confirmResp.GetResponseStream()).ReadToEnd()
  $confirmResp.Close()

  $chapterResults = @()
  for ($chapterNo = 1; $chapterNo -le 5; $chapterNo++) {
    Start-Sleep -Seconds 5
    Write-Host "Generating chapter $chapterNo..."
    $chapterReq = [System.Net.HttpWebRequest]::Create('http://127.0.0.1:8787/api/generate-chapter')
    $chapterReq.Method = 'POST'
    $chapterReq.ContentType = 'application/json'
    $chapterReq.Headers.Add('Accept', 'application/json')
    $chapterBody = [System.Text.Encoding]::UTF8.GetBytes((@{
      chapter_no = $chapterNo
      chapter_title = ''
      tone = '紧张'
      length_target = 800
      instruction = "第$chapterNo章推进剧情并增加悬念。"
    } | ConvertTo-Json -Compress))
    $outStream = $chapterReq.GetRequestStream()
    $outStream.Write($chapterBody, 0, $chapterBody.Length)
    $outStream.Close()
    $chapterResp = $chapterReq.GetResponse()
    $chapterJson = New-Object System.IO.StreamReader($chapterResp.GetResponseStream()).ReadToEnd()
    $chapterResp.Close()
    $chapterResults += [PSCustomObject]@{
      chapter_no = $chapterNo
      ok = (($chapterJson | ConvertFrom-Json).ok -eq $true)
    }
  }

  $state = Invoke-RestMethod 'http://127.0.0.1:8787/api/state'
  $chapterFiles = Get-ChildItem $chapters -Filter 'chapter_*.json' -File | Sort-Object Name
  Write-Host 'Test complete.'
  [PSCustomObject]@{
    outline_ok = $true
    confirmed_ok = $true
    chapter_count = $chapterFiles.Count
    chapter_results = $chapterResults
    outline_status = $state.outline.status
  } | ConvertTo-Json -Depth 6
}
finally {
  if ($app -and -not $app.HasExited) {
    Stop-Process -Id $app.Id -Force
  }
  Write-Host 'Cleaning generated files...'
  @(
    (Join-Path $data 'characters.json'),
    (Join-Path $data 'conversation_memory.json'),
    (Join-Path $data 'outline_draft.json'),
    (Join-Path $data 'outline.json'),
    (Join-Path $data 'story_brief.json'),
    (Join-Path $data 'storyline.json')
  ) | ForEach-Object { Remove-IfExists $_ }
  if (Test-Path $chapters) {
    Get-ChildItem $chapters -Filter 'chapter_*.json' -File | ForEach-Object {
      Remove-Item -LiteralPath $_.FullName -Force
    }
  }
  Remove-IfExists $pyc
}
