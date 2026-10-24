$ErrorActionPreference = 'Stop'
$androidDir = $PSScriptRoot
$repoDir = Split-Path -Parent $androidDir
$jdk = Join-Path $repoDir '.tools/temurin-17/bin'
$output = Join-Path $androidDir 'app/build/host-text-input-tests'
New-Item -ItemType Directory -Path $output -Force | Out-Null
& (Join-Path $jdk 'javac.exe') -encoding UTF-8 -d $output `
    (Join-Path $androidDir 'app/src/main/java/io/daxqo/app/TextInputEdit.java') `
    (Join-Path $androidDir 'tests/TextInputRegression.java')
if ($LASTEXITCODE -ne 0) { throw 'TextInputEdit host regression compilation failed.' }
& (Join-Path $jdk 'java.exe') -cp $output io.daxqo.app.TextInputRegression
if ($LASTEXITCODE -ne 0) { throw 'TextInputEdit regression failed.' }
