param([switch]$SelfTest, [switch]$Preview)
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$compiler = 'C:/Windows/Microsoft.NET/Framework64/v4.0.30319/csc.exe'
if (-not (Test-Path -LiteralPath $compiler)) { throw "Missing .NET Framework compiler: $compiler" }
$sources = @(Get-ChildItem -LiteralPath $PSScriptRoot -Filter '*.cs' | Sort-Object Name | ForEach-Object { $_.FullName })
$out = Join-Path $root 'BPS-Manager.exe'
& $compiler /nologo /target:winexe /platform:anycpu /optimize+ /debug- /warn:4 /utf8output "/out:$out" /reference:System.dll /reference:System.Core.dll /reference:System.Drawing.dll /reference:System.Windows.Forms.dll /reference:System.Management.dll /reference:System.Web.Extensions.dll @sources
if ($LASTEXITCODE -ne 0) { throw 'Desktop manager compilation failed.' }
Write-Output "Built $out"
if ($SelfTest) {
    $report = Join-Path $PSScriptRoot 'selftest-results.txt'
    $process = Start-Process -FilePath $out -ArgumentList @('--self-test', ('"' + $report + '"')) -WindowStyle Hidden -Wait -PassThru
    Get-Content -LiteralPath $report
    if ($process.ExitCode -ne 0) { throw "Self-test failed ($($process.ExitCode))" }
}
if ($Preview) {
    $image = Join-Path $PSScriptRoot 'preview.png'
    $process = Start-Process -FilePath $out -ArgumentList @('--preview', ('"' + $image + '"')) -WindowStyle Hidden -Wait -PassThru
    if ($process.ExitCode -ne 0) { throw 'Preview render failed.' }
    Write-Output "Rendered $image"
}
