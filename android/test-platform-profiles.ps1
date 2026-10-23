$ErrorActionPreference = 'Stop'
$androidDir = $PSScriptRoot
$repoDir = Split-Path -Parent $androidDir
$source = Join-Path $androidDir 'app/src/main/java/io/daxqo/app/platform/PlatformProfile.java'
$test = Join-Path $androidDir 'tests/PlatformProfileRegression.java'
$bundledJava = Join-Path $repoDir '.tools/temurin-17/bin'
$javac = if (Test-Path (Join-Path $bundledJava 'javac.exe')) { Join-Path $bundledJava 'javac.exe' } else { 'javac' }
$java = if (Test-Path (Join-Path $bundledJava 'java.exe')) { Join-Path $bundledJava 'java.exe' } else { 'java' }
$tempDir = Join-Path ([System.IO.Path]::GetTempPath()) ("daxqo-platform-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tempDir | Out-Null
try {
    & $javac -encoding UTF-8 -d $tempDir $source $test
    if ($LASTEXITCODE -ne 0) { throw 'Could not compile platform profile regression checks.' }
    & $java -cp $tempDir PlatformProfileRegression
    if ($LASTEXITCODE -ne 0) { throw 'Platform profile regression checks failed.' }
} finally {
    Remove-Item -LiteralPath $tempDir -Recurse -Force
}
