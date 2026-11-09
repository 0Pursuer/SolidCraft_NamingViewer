param([Parameter(Mandatory=$true)][ValidateSet('before','split','chamfer')][string]$Stage)
$ErrorActionPreference = 'Stop'
$outputFolder = $PSScriptRoot
$requests = @(
    @{jsonrpc='2.0';id=1;method='initialize';params=@{protocolVersion='2024-11-05';capabilities=@{};clientInfo=@{name='topology-visualization';version='1.0'}}},
    @{jsonrpc='2.0';method='notifications/initialized'},
    @{jsonrpc='2.0';id=2;method='tools/call';params=@{name='inspect_design';arguments=@{measure=$true;sketches=$false}}},
    @{jsonrpc='2.0';id=3;method='tools/call';params=@{name='execute';arguments=@{command='model.faces';params=@{body='Body1'}}}},
    @{jsonrpc='2.0';id=4;method='tools/call';params=@{name='execute';arguments=@{command='model.edges';params=@{body='Body1'}}}},
    @{jsonrpc='2.0';id=5;method='tools/call';params=@{name='execute';arguments=@{command='file.export';params=@{path=(Join-Path $outputFolder "$Stage.3mf");format='3mf-tagged'}}}}
)
$requestLines = $requests | ForEach-Object {ConvertTo-Json -InputObject $_ -Depth 20 -Compress}
$scriptPath = Join-Path $outputFolder "$Stage-script.json"
$responses = $requestLines | & 'C:\Program Files\SolidCraft\solidcraft-cli.exe' mcp --headless --in $scriptPath --allow-path $outputFolder 2> (Join-Path $outputFolder "$Stage-stderr.log")
if($LASTEXITCODE -ne 0){throw "CLI failed for $Stage"}
$captured = [ordered]@{}
foreach($line in $responses){
    $response = $line | ConvertFrom-Json
    if($response.error -or $response.result.isError){throw $line}
    if($response.id -ge 2){
        $payload = $response.result.content | Where-Object {$_.type -eq 'text'} | Select-Object -First 1 -ExpandProperty text
        $captured[[string]$response.id] = ConvertFrom-Json -InputObject $payload
    }
}
if($captured.Count -ne 4){throw "Incomplete results for $Stage"}
$captured | ConvertTo-Json -Depth 40 | Set-Content -LiteralPath (Join-Path $outputFolder "$Stage-capture.json") -Encoding utf8
[ordered]@{stage=$Stage;faces=$captured['3'].faces.Count;edges=$captured['4'].edges.Count;volume=$captured['2'].bodies[0].volume_mm3;warning=($captured['2'].timeline | Where-Object {$_.warning} | Select-Object -ExpandProperty warning);export=$captured['5']} | ConvertTo-Json -Depth 5
