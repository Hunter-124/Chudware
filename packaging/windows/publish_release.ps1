param(
  [Parameter(Mandatory=$true)][string]$Artifact,
  [Parameter(Mandatory=$true)][ValidateSet('default', 'msi')][string]$Format,
  [Parameter(Mandatory=$true)][string]$Signature,
  [Parameter(Mandatory=$true)][string]$StableSignature,
  [Parameter(Mandatory=$true)][string]$PayloadDir,
  [string]$EvidenceDir = ''
)

$ErrorActionPreference = 'Stop'
$base = $env:CHUDWARE_GATEWAY_PUBLIC_BASE
$retry = $env:CHUDWARE_GATEWAY_CURL_RETRY -split ' '
$name = & python Webservice\gateway\artifact_names.py --version $env:RELEASE_VERSION `
  --platform windows --arch x86_64 --format $Format
if ($LASTEXITCODE -ne 0) { throw "canonical artifact naming failed for $Format" }
$name = ($name -join '').Trim()

function Send-StagedFile([string]$Local, [string]$Remote) {
  if (-not (Test-Path -LiteralPath $Local -PathType Leaf)) {
    throw "missing staged upload: $Local"
  }
  $expected = (Get-FileHash -LiteralPath $Local -Algorithm SHA256).Hash.ToLower()
  $size = (Get-Item -LiteralPath $Local).Length
  $response = ''
  if ($size -gt 67108864) {
    $chunkFile = Join-Path $env:RUNNER_TEMP ("upload-$Format.chunk")
    $chunkSize = 67108864
    $stream = [IO.File]::OpenRead($Local)
    $buffer = New-Object byte[] $chunkSize
    $offset = 0
    try {
      while ($offset -lt $size) {
        $read = $stream.Read($buffer, 0, $chunkSize)
        if ($read -le 0) { break }
        $out = [IO.File]::Open($chunkFile, 'Create', 'Write')
        try { $out.Write($buffer, 0, $read) } finally { $out.Close() }
        $query = "offset=$offset"
        if (($offset + $read) -ge $size) { $query = "offset=$offset&complete=1" }
        $response = & curl.exe --config $env:GATEWAY_CURLRC --fail-with-body -sS `
          --max-time 3600 @retry --upload-file $chunkFile `
          "$base/api/releases/ci/upload/${Remote}?$query"
        if ($LASTEXITCODE -ne 0) {
          throw "chunk upload failed for $Remote at offset $offset : $response"
        }
        $offset += $read
      }
    } finally {
      $stream.Close()
      Remove-Item -Force -ErrorAction SilentlyContinue $chunkFile
    }
  } else {
    $response = & curl.exe --config $env:GATEWAY_CURLRC --fail-with-body -sS `
      --max-time 3600 @retry --upload-file $Local `
      "$base/api/releases/ci/upload/$Remote"
    if ($LASTEXITCODE -ne 0) { throw "upload failed for $Remote : $response" }
  }
  $reported = ''
  try { $reported = (($response -join '') | ConvertFrom-Json).sha256 } catch { $reported = '' }
  if ($reported -ne $expected) {
    throw "gateway stored a different digest for $Remote : expected $expected, got '$reported'"
  }
  Write-Host "uploaded $Remote sha256=$reported"
}

# Stage immediately before each publish. A gateway 422 deletes the staged
# artifact, so rerunning this helper safely re-uploads exactly one format.
$uploads = @(
  @{ Local = $Artifact; Remote = $name },
  @{ Local = (Join-Path $PayloadDir 'payload-manifest.v1.json'); Remote = "$name.payload-manifest.v1.json" },
  @{ Local = (Join-Path $PayloadDir 'payload-manifest.v1.sig'); Remote = "$name.payload-manifest.v1.sig" }
)
if ($EvidenceDir) {
  foreach ($doc in @('sbom.cdx.json', 'provenance.slsa.json', 'platform-test.v1.json')) {
    $uploads += @{ Local = (Join-Path $EvidenceDir $doc); Remote = "$name.$doc" }
    $uploads += @{ Local = (Join-Path $EvidenceDir "$doc.sig"); Remote = "$name.$doc.sig" }
  }
}
foreach ($item in $uploads) { Send-StagedFile $item.Local $item.Remote }

$body = Join-Path $env:RUNNER_TEMP ("publish-$Format.json")
$bodyArgs = @(
  'packaging\windows\ci_publish_body.py',
  '--artifact', $Artifact,
  '--format', $Format,
  '--signature', $Signature,
  '--stable-signature', $StableSignature,
  '--output', $body
)
if ($EvidenceDir) { $bodyArgs += @('--evidence-dir', $EvidenceDir) }
& python @bodyArgs
if ($LASTEXITCODE -ne 0) { throw "building ci/publish body failed for $Format" }
$response = & curl.exe --config $env:GATEWAY_CURLRC --fail-with-body -sS `
  --max-time 300 @retry -H 'Content-Type: application/json' `
  --data-binary "@$body" "$base/api/releases/ci/publish"
if ($LASTEXITCODE -ne 0) { throw "ci/publish rejected ${name}: $response" }
Write-Host ($response -join '')
Write-Output $name
