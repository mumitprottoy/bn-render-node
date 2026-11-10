<#
setup.ps1: one-time setup of the BowlersNetwork render node on this Windows PC.

Run in a PowerShell window opened as administrator, after the repo is cloned to D:\bn-render\app:

    powershell -NoProfile -ExecutionPolicy Bypass -File D:\bn-render\app\setup.ps1 -KeyFile <path to the key file>

Every step checks what is already done, so running it again is safe. Each step prints one PASS or FAIL line and the
script stops at the first FAIL. One step alone: -Step <name>. Steps, in order:
    admin, folders, winget, git, python, ffmpeg, blender, repo, venv, key, power, path, selfcheck, task, start

The API key is the node's only secret. It is read from -KeyFile (or, when Boss runs `-Step key` himself, from a hidden
prompt), written to D:\bn-render\.env, and never printed.
#>
param(
    [string]$Root = 'D:\bn-render',
    [string]$RepoUrl = '',
    [string]$KeyFile = '',
    [string]$Step = ''
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$TaskName = 'BN Render Node'
$CheckTaskName = 'BN Render Node check'
$BlenderUrl = 'https://download.blender.org/release/Blender5.2/blender-5.2.3-windows-x64.zip'
$BlenderZipName = 'blender-5.2.3-windows-x64.zip'
$BlenderSize = 405839452
$BlenderSha256 = '723f08a4401502561eb763cdeacf8745b31e5f3f154d287d3830d97cf75dc2fb'
$BlenderVersion = 'Blender 5.2.3'
$MinFreeGB = 60
$CheckTaskMinutes = 15

$App = Join-Path $Root 'app'
$Tools = Join-Path $Root 'tools'
$State = Join-Path $Root 'state'
$Downloads = Join-Path $Root 'downloads'
$Venv = Join-Path $Root 'venv'
$VenvPython = Join-Path $Venv 'Scripts\python.exe'
$VenvPythonW = Join-Path $Venv 'Scripts\pythonw.exe'
$EnvFile = Join-Path $Root '.env'
$UserId = "$env:USERDOMAIN\$env:USERNAME"

function Report-Pass([string]$Name, [string]$Detail) {
    Write-Host ('PASS  {0,-10} {1}' -f $Name, $Detail) -ForegroundColor Green
}

function Report-Fail([string]$Name, [string]$Detail) {
    Write-Host ('FAIL  {0,-10} {1}' -f $Name, $Detail) -ForegroundColor Red
    exit 1
}

function Report-Note([string]$Text) {
    Write-Host "      $Text"
}

function Update-SessionPath {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
}

function Invoke-Native([string]$File, [string[]]$Arguments) {
    # Runs a program and returns its exit code and output, without stopping on what it writes to stderr.
    if (-not (Get-Command $File -ErrorAction SilentlyContinue)) {
        return [pscustomobject]@{ Code = -1; Output = "$File is not installed or not on PATH." }
    }
    $global:LASTEXITCODE = 0
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & $File @Arguments 2>&1 | Out-String
        $code = $LASTEXITCODE
    } catch {
        $output = $_.Exception.Message
        $code = -1
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($null -eq $code) { $code = 0 }
    return [pscustomobject]@{ Code = $code; Output = [string]$output }
}

function Get-LastLines([string]$Text, [int]$Count = 3) {
    $lines = @($Text -split "`r?`n" | Where-Object { $_.Trim() })
    if ($lines.Count -eq 0) { return '(no output)' }
    return ($lines | Select-Object -Last $Count) -join ' | '
}

function Install-WithWinget([string]$Id, [string[]]$Extra) {
    $arguments = @('install', '--id', $Id, '-e', '--silent', '--accept-package-agreements', '--accept-source-agreements',
                   '--disable-interactivity') + $Extra
    $result = Invoke-Native 'winget' $arguments
    Update-SessionPath
    return $result
}

function Find-Python312 {
    $launcher = Invoke-Native 'py' @('-3.12', '-c', 'import sys; print(sys.executable)')
    if ($launcher.Code -eq 0) {
        $path = $launcher.Output.Trim()
        if ($path -and (Test-Path $path)) { return $path }
    }
    foreach ($candidate in @("$env:ProgramFiles\Python312\python.exe", "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe")) {
        if (Test-Path $candidate) { return $candidate }
    }
    return $null
}

function Find-FfmpegFolder {
    foreach ($packages in @("$env:LOCALAPPDATA\Microsoft\WinGet\Packages", "$env:ProgramFiles\WinGet\Packages")) {
        if (-not (Test-Path $packages)) { continue }
        $hit = Get-ChildItem -Path $packages -Filter 'ffmpeg.exe' -Recurse -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -like '*Gyan.FFmpeg*' } | Select-Object -First 1
        if ($hit -and (Test-Path (Join-Path $hit.DirectoryName 'ffprobe.exe'))) { return $hit.DirectoryName }
    }
    return $null
}

function Test-KeyPresent {
    if (-not (Test-Path $EnvFile)) { return $false }
    return [bool](Select-String -Path $EnvFile -Pattern '^\s*BN_API_KEY\s*=\s*\S' -Quiet)
}

function Invoke-Bnrender([string[]]$Arguments) {
    $env:BN_RENDER_ROOT = $Root
    $env:PYTHONIOENCODING = 'utf-8'
    Push-Location $App
    try {
        return Invoke-Native $VenvPython (@('-m', 'bnrender') + $Arguments)
    } finally {
        Pop-Location
    }
}

# The steps

function Step-Admin {
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Report-Fail 'admin' 'PowerShell is not running as administrator. Open PowerShell with "Run as administrator" and run this again.'
    }
    Report-Pass 'admin' "Running as administrator ($UserId)."
}

function Step-Folders {
    $drive = Split-Path -Qualifier $Root
    if (-not (Test-Path "$drive\")) { Report-Fail 'folders' "Drive $drive is not there." }
    $free = (Get-PSDrive -Name $drive.TrimEnd(':')).Free
    if ($free -lt ($MinFreeGB * 1GB)) {
        Report-Fail 'folders' ("Only {0:N0} GB free on {1}; at least {2} GB is needed." -f ($free / 1GB), $drive, $MinFreeGB)
    }
    foreach ($folder in @($Root, $Tools, $State, $Downloads, (Join-Path $Root 'work'), (Join-Path $Root 'models'), (Join-Path $Root 'assets'))) {
        New-Item -ItemType Directory -Force -Path $folder | Out-Null
    }
    Report-Pass 'folders' ("{0} is ready ({1:N0} GB free on {2})." -f $Root, ($free / 1GB), $drive)
}

function Step-Winget {
    $result = Invoke-Native 'winget' @('--version')
    if ($result.Code -ne 0) {
        Report-Fail 'winget' 'winget is missing. Install "App Installer" from the Microsoft Store, then run this again.'
    }
    Report-Pass 'winget' "winget $($result.Output.Trim())"
}

function Step-Git {
    Update-SessionPath
    $result = Invoke-Native 'git' @('--version')
    if ($result.Code -ne 0) {
        Report-Note 'Installing Git...'
        Install-WithWinget 'Git.Git' @('--scope', 'machine') | Out-Null
        $result = Invoke-Native 'git' @('--version')
        if ($result.Code -ne 0) { Report-Fail 'git' 'Git did not install. Run: winget install --id Git.Git -e   then run this again.' }
    }
    Report-Pass 'git' $result.Output.Trim()
}

function Step-Python {
    $python = Find-Python312
    if (-not $python) {
        Report-Note 'Installing Python 3.12...'
        Install-WithWinget 'Python.Python.3.12' @('--scope', 'machine') | Out-Null
        $python = Find-Python312
    }
    if (-not $python) { Report-Fail 'python' 'Python 3.12 did not install. Run: winget install --id Python.Python.3.12 -e --scope machine   then run this again.' }
    $version = Invoke-Native $python @('--version')
    if ($version.Output -notmatch 'Python 3\.12\.') { Report-Fail 'python' "Found $($version.Output.Trim()) at $python; 3.12 is needed." }
    Report-Pass 'python' "$($version.Output.Trim()) at $python"
}

function Step-Ffmpeg {
    $target = Join-Path $Tools 'ffmpeg\bin'
    $ffmpeg = Join-Path $target 'ffmpeg.exe'
    if (-not (Test-Path $ffmpeg)) {
        $found = Find-FfmpegFolder
        if (-not $found) {
            Report-Note 'Installing ffmpeg (full build)...'
            Install-WithWinget 'Gyan.FFmpeg' @() | Out-Null
            $found = Find-FfmpegFolder
        }
        if (-not $found) { Report-Fail 'ffmpeg' 'ffmpeg did not install. Run: winget install --id Gyan.FFmpeg -e   then run this again.' }
        New-Item -ItemType Directory -Force -Path $target | Out-Null
        Copy-Item -Path (Join-Path $found 'ffmpeg.exe') -Destination $target -Force
        Copy-Item -Path (Join-Path $found 'ffprobe.exe') -Destination $target -Force
    }
    $encoders = Invoke-Native $ffmpeg @('-hide_banner', '-encoders')
    if ($encoders.Output -notmatch 'libx264') { Report-Fail 'ffmpeg' "The ffmpeg in $target has no libx264 encoder. Delete $target and run this again." }
    Report-Pass 'ffmpeg' "ffmpeg and ffprobe with libx264 in $target"
}

function Step-Blender {
    $blenderDir = Join-Path $Tools 'blender'
    $blenderExe = Join-Path $blenderDir 'blender.exe'
    if (Test-Path $blenderExe) {
        $current = Invoke-Native $blenderExe @('--version')
        if ($current.Output -match [regex]::Escape($BlenderVersion)) {
            Report-Pass 'blender' "$BlenderVersion is already in $blenderDir"
            return
        }
        Report-Note "Replacing the Blender in $blenderDir ($(Get-LastLines $current.Output 1))..."
        Remove-Item -Recurse -Force $blenderDir
    }
    New-Item -ItemType Directory -Force -Path $Downloads | Out-Null
    $zip = Join-Path $Downloads $BlenderZipName
    if ((Test-Path $zip) -and ((Get-Item $zip).Length -gt $BlenderSize)) { Remove-Item -Force $zip }
    if (-not (Test-Path $zip) -or ((Get-Item $zip).Length -ne $BlenderSize)) {
        Report-Note "Downloading $BlenderUrl (about 390 MB)..."
        $download = Invoke-Native 'curl.exe' @('-L', '--fail', '--silent', '--show-error', '--retry', '5', '--retry-delay', '5', '-C', '-', '-o', $zip, $BlenderUrl)
        if ($download.Code -ne 0) { Report-Fail 'blender' "The download failed: $(Get-LastLines $download.Output 2). Run this again to continue it." }
    }
    $size = (Get-Item $zip).Length
    if ($size -ne $BlenderSize) {
        Remove-Item -Force $zip
        Report-Fail 'blender' "The download is $size bytes, not $BlenderSize. It was deleted; run this again."
    }
    $hash = (Get-FileHash -Algorithm SHA256 -Path $zip).Hash.ToLower()
    if ($hash -ne $BlenderSha256) {
        Remove-Item -Force $zip
        Report-Fail 'blender' "SHA-256 mismatch: got $hash, expected $BlenderSha256. The download was deleted and must not be used."
    }
    Report-Note 'Checksum matches. Extracting...'
    $staging = Join-Path $Tools 'blender-extract'
    if (Test-Path $staging) { Remove-Item -Recurse -Force $staging }
    New-Item -ItemType Directory -Path $staging | Out-Null
    $extract = Invoke-Native 'tar.exe' @('-xf', $zip, '-C', $staging)
    if ($extract.Code -ne 0) { Expand-Archive -Path $zip -DestinationPath $staging -Force }
    $inner = Get-ChildItem -Path $staging -Directory | Select-Object -First 1
    if (-not $inner) { Report-Fail 'blender' 'The Blender archive was empty after extracting.' }
    Move-Item -Path $inner.FullName -Destination $blenderDir
    Remove-Item -Recurse -Force $staging
    $installed = Invoke-Native $blenderExe @('--version')
    if ($installed.Output -notmatch [regex]::Escape($BlenderVersion)) {
        Report-Fail 'blender' "blender.exe does not report $BlenderVersion ($(Get-LastLines $installed.Output 1))."
    }
    Remove-Item -Force $zip
    Report-Pass 'blender' "$BlenderVersion installed in $blenderDir (size and SHA-256 verified)"
}

function Step-Repo {
    if (Test-Path (Join-Path $App '.git')) {
        $pull = Invoke-Native 'git' @('-C', $App, 'pull', '--ff-only')
        if ($pull.Code -ne 0) { Report-Fail 'repo' "git pull failed in ${App}: $(Get-LastLines $pull.Output 2)" }
    } elseif ($RepoUrl) {
        if (Test-Path $App) { Report-Fail 'repo' "$App exists but is not a git clone. Move it away, then run this again." }
        $clone = Invoke-Native 'git' @('clone', '--branch', 'main', $RepoUrl, $App)
        if ($clone.Code -ne 0) { Report-Fail 'repo' "git clone failed: $(Get-LastLines $clone.Output 2)" }
    } else {
        Report-Fail 'repo' "$App is not a clone yet. Run again with -RepoUrl <the repository URL>."
    }
    if (-not (Test-Path (Join-Path $App 'bnrender\__main__.py'))) { Report-Fail 'repo' "$App has no bnrender package; is it the right repository?" }
    $head = Invoke-Native 'git' @('-C', $App, 'log', '-1', '--format=%h %s')
    Report-Pass 'repo' "$App at $($head.Output.Trim())"
}

function Step-Venv {
    if (-not (Test-Path $VenvPython)) {
        $python = Find-Python312
        if (-not $python) { Report-Fail 'venv' 'Python 3.12 is missing; run the python step first.' }
        $create = Invoke-Native $python @('-m', 'venv', $Venv)
        if ($create.Code -ne 0) { Report-Fail 'venv' "The Python environment could not be made: $(Get-LastLines $create.Output 2)" }
    }
    Report-Note 'Installing the Python packages...'
    $install = Invoke-Native $VenvPython @('-m', 'pip', 'install', '--disable-pip-version-check', '--quiet', '-r', (Join-Path $App 'requirements.txt'))
    if ($install.Code -ne 0) { Report-Fail 'venv' "pip install failed: $(Get-LastLines $install.Output 3)" }
    $imports = Invoke-Native $VenvPython @('-c', 'import yaml, PIL, numpy, psutil; print("ok")')
    if ($imports.Output.Trim() -ne 'ok') { Report-Fail 'venv' "The packages do not import: $(Get-LastLines $imports.Output 2)" }
    Report-Pass 'venv' "Python environment ready in $Venv"
}

function Step-Key {
    if (Test-KeyPresent) {
        Report-Pass 'key' "The API key is already in $EnvFile (not shown)."
        return
    }
    $key = $null
    if ($KeyFile) {
        if (-not (Test-Path $KeyFile)) { Report-Fail 'key' "The key file $KeyFile is not there." }
        $text = Get-Content -Raw -Path $KeyFile
        $match = [regex]::Match($text, 'bn_[A-Za-z0-9_\-]{8,}')
        if ($match.Success) {
            $key = $match.Value
        } elseif ($text -and ($text.Trim() -notmatch '\s')) {
            $key = $text.Trim()
        }
        Remove-Variable -Name text
    } elseif (-not [Console]::IsInputRedirected) {
        $secure = Read-Host -AsSecureString 'Paste the render node API key (nothing is shown) and press Enter'
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        try {
            $key = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer).Trim()
        } finally {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
        }
    }
    if (-not $key) {
        Report-Fail 'key' 'No key was given. Run again with -KeyFile <path to the key file>, or ask Boss to run "setup.ps1 -Step key" in his own PowerShell window and paste the key at the hidden prompt.'
    }
    $lines = @()
    if (Test-Path $EnvFile) { $lines = @(Get-Content -Path $EnvFile | Where-Object { $_ -notmatch '^\s*BN_API_KEY\s*=' }) }
    $lines += "BN_API_KEY=$key"
    [IO.File]::WriteAllLines($EnvFile, [string[]]$lines, (New-Object Text.UTF8Encoding($false)))
    Remove-Variable -Name key
    Remove-Variable -Name lines
    # Only this account, SYSTEM (S-1-5-18) and administrators (S-1-5-32-544) can read the key.
    $acl = Invoke-Native 'icacls.exe' @($EnvFile, '/inheritance:r', '/grant:r', "${UserId}:(R,W)", '*S-1-5-18:(F)', '*S-1-5-32-544:(F)')
    if ($acl.Code -ne 0) { Report-Note "The file permissions could not be narrowed: $(Get-LastLines $acl.Output 1)" }
    Report-Pass 'key' "The API key is saved in $EnvFile (not shown)."
}

function Step-Power {
    foreach ($setting in @('standby-timeout-ac', 'hibernate-timeout-ac')) {
        $change = Invoke-Native 'powercfg.exe' @('/change', $setting, '0')
        if ($change.Code -ne 0) { Report-Fail 'power' "powercfg /change $setting 0 failed: $(Get-LastLines $change.Output 1)" }
    }
    $query = Invoke-Native 'powercfg.exe' @('/query', 'SCHEME_CURRENT', 'SUB_SLEEP', 'STANDBYIDLE')
    if ($query.Output -match 'Current AC Power Setting Index:\s*(0x[0-9a-fA-F]+)') {
        if ([Convert]::ToInt32($Matches[1], 16) -ne 0) { Report-Fail 'power' "Sleep on mains power is still set ($($Matches[1]))." }
        Report-Pass 'power' 'The PC never sleeps or hibernates on mains power.'
    } else {
        Report-Pass 'power' 'Sleep and hibernate on mains power set to never (read-back skipped: Windows is not in English).'
    }
}

function Step-Path {
    $bin = Join-Path $App 'bin'
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $parts = @()
    if ($userPath) { $parts = @($userPath.Split(';') | Where-Object { $_ }) }
    if ($parts -notcontains $bin) {
        [Environment]::SetEnvironmentVariable('Path', (($parts + $bin) -join ';'), 'User')
    }
    Update-SessionPath
    $command = Get-Command 'bn' -ErrorAction SilentlyContinue
    if (-not $command -or ($command.Source -ne (Join-Path $bin 'bn.cmd'))) { Report-Fail 'path' "The bn command is not found in $bin." }
    Report-Pass 'path' "$bin is on the user PATH; new PowerShell windows know the bn command."
}

function Step-Selfcheck {
    $result = Invoke-Bnrender @('selfcheck', '--report', (Join-Path $State 'selfcheck-setup.json'))
    Write-Host $result.Output
    if ($result.Code -ne 0) { Report-Fail 'selfcheck' 'The self-check found problems (the FAIL lines above).' }
    Report-Pass 'selfcheck' 'Everything the node needs works in this window.'
}

function New-NodeSettings {
    # Task priority 3 starts the process at "above normal" (the default, 7, would be below normal).
    return New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
        -MultipleInstances IgnoreNew -Priority 3
}

function Register-NodeTask([string]$Mode) {
    $action = New-ScheduledTaskAction -Execute $VenvPythonW -Argument '-m bnrender run' -WorkingDirectory $App
    if ($Mode -eq 'boot') {
        # S4U: runs whether or not anyone is signed in, without storing a password.
        $principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType S4U -RunLevel Limited
        $triggers = @((New-ScheduledTaskTrigger -AtStartup), (New-ScheduledTaskTrigger -AtLogOn -User $UserId))
    } else {
        $principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType Interactive -RunLevel Limited
        $triggers = @(New-ScheduledTaskTrigger -AtLogOn -User $UserId)
    }
    Register-ScheduledTask -TaskName $TaskName -Action $action -Principal $principal -Trigger $triggers -Settings (New-NodeSettings) -Force | Out-Null
}

function Test-GpuBeforeSignIn {
    # Runs the self-check once in the same context as an at-boot task (S4U, no desktop session), because whether
    # Blender can use the graphics card there is not certain. Returns the gpu check's detail, or '' if it passed.
    $report = Join-Path $State 'selfcheck-task.json'
    if (Test-Path $report) { Remove-Item -Force $report }
    Unregister-ScheduledTask -TaskName $CheckTaskName -Confirm:$false -ErrorAction SilentlyContinue
    $action = New-ScheduledTaskAction -Execute $VenvPython -Argument "-m bnrender selfcheck --report `"$report`"" -WorkingDirectory $App
    $principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType S4U -RunLevel Limited
    Register-ScheduledTask -TaskName $CheckTaskName -Action $action -Principal $principal -Settings (New-NodeSettings) -Force | Out-Null
    Start-ScheduledTask -TaskName $CheckTaskName
    $deadline = (Get-Date).AddMinutes($CheckTaskMinutes)
    Start-Sleep -Seconds 5
    while ((Get-Date) -lt $deadline) {
        $task = Get-ScheduledTask -TaskName $CheckTaskName
        if ($task.State -ne 'Running') { break }
        Start-Sleep -Seconds 5
    }
    Stop-ScheduledTask -TaskName $CheckTaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $CheckTaskName -Confirm:$false -ErrorAction SilentlyContinue
    if (-not (Test-Path $report)) { return 'the check could not run before sign-in (no report was written)' }
    $parsed = Get-Content -Raw -Path $report | ConvertFrom-Json
    $gpu = @($parsed.checks | Where-Object { $_.name -eq 'gpu' }) | Select-Object -First 1
    if ($gpu -and $gpu.status -eq 'PASS') { return '' }
    if ($gpu) { return [string]$gpu.detail }
    return 'the check did not reach the graphics card test'
}

function Step-Task {
    Report-Note 'Testing the graphics card the way the node runs at boot (takes a minute or two)...'
    $problem = Test-GpuBeforeSignIn
    if (-not $problem) {
        Register-NodeTask 'boot'
        Report-Pass 'task' "Task '$TaskName' starts the node at boot (before sign-in) and at sign-in, as $UserId."
    } else {
        Register-NodeTask 'logon'
        Report-Pass 'task' "Task '$TaskName' starts the node when $UserId signs in."
        Report-Note "Before sign-in the graphics card cannot be used here ($problem)."
        Report-Note 'After a power cut the node therefore starts at the next sign-in.'
    }
}

function Step-Start {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) { Report-Fail 'start' "The task '$TaskName' is not registered; run the task step." }
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 20
    $status = Invoke-Bnrender @('status')
    Write-Host $status.Output
    if ($status.Output -notmatch 'RUNNING|PAUSED|FINISHED') {
        Report-Fail 'start' "The node did not report RUNNING. Its log is $State\log.txt"
    }
    Report-Pass 'start' 'The render node is running.'
}

$Steps = [ordered]@{
    'admin' = { Step-Admin }
    'folders' = { Step-Folders }
    'winget' = { Step-Winget }
    'git' = { Step-Git }
    'python' = { Step-Python }
    'ffmpeg' = { Step-Ffmpeg }
    'blender' = { Step-Blender }
    'repo' = { Step-Repo }
    'venv' = { Step-Venv }
    'key' = { Step-Key }
    'power' = { Step-Power }
    'path' = { Step-Path }
    'selfcheck' = { Step-Selfcheck }
    'task' = { Step-Task }
    'start' = { Step-Start }
}

if ($Step) {
    if (-not $Steps.Contains($Step)) { Report-Fail 'setup' "There is no step '$Step'. Steps: $($Steps.Keys -join ', ')" }
    & $Steps[$Step]
    exit 0
}
foreach ($name in $Steps.Keys) {
    & $Steps[$name]
}
Write-Host ''
Write-Host 'Setup complete. In a new PowerShell window: bn status'
exit 0
