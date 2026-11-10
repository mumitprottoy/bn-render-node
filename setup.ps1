<#
setup.ps1: one-time setup of the BowlersNetwork render node on this Windows PC. No questions are asked.

Run in a PowerShell window opened as administrator, one step at a time (each step is short and safe to run again):

    powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\Public\bn-render-setup\setup.ps1 -Step <name>

Steps, in order:
    admin, folders, winget, git, python, ffmpeg, blender, repo, venv, account, key, access, power, path, selfcheck,
    task, restart

Each step prints one PASS or FAIL line. The work folder is chosen automatically: an existing D:\bn-render (or
X:\bn-render), else D: when it has 50 GB free, else the fixed drive with the most free space. The API key file
(studio-node-api-key.txt) is found on the Desktop, in Downloads or Documents, or at the top of any drive; the key is
never printed (only its first 12 characters).
#>
param(
    [string]$Root = '',
    [string]$RepoUrl = 'https://github.com/mumitprottoy/bn-render-node.git',
    [string]$KeyFile = '',
    [string]$Step = ''
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$TaskName = 'BN Render Node'
$OldCheckTaskName = 'BN Render Node check'
$NodeUser = 'BNRender'
$KeyFileName = 'studio-node-api-key.txt'
$AutologonUrl = 'https://live.sysinternals.com/Autologon64.exe'
$BlenderUrl = 'https://download.blender.org/release/Blender5.2/blender-5.2.3-windows-x64.zip'
$BlenderZipName = 'blender-5.2.3-windows-x64.zip'
$BlenderSize = 405839452
$BlenderSha256 = '723f08a4401502561eb763cdeacf8745b31e5f3f154d287d3830d97cf75dc2fb'
$BlenderVersion = 'Blender 5.2.3'
$MinFreeGB = 50
$SystemSid = '*S-1-5-18'
$AdministratorsSid = '*S-1-5-32-544'
$UsersSid = 'S-1-5-32-545'
$AuthenticatedUsersSid = '*S-1-5-11'

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

function Resolve-NodeRoot {
    if ($Root) { return $Root }
    $disks = @(Get-CimInstance -ClassName Win32_LogicalDisk -Filter 'DriveType=3')
    foreach ($disk in $disks) {
        $candidate = "$($disk.DeviceID)\bn-render"
        if (Test-Path (Join-Path $candidate 'app\.git')) { return $candidate }
    }
    $roomy = @($disks | Where-Object { $_.FreeSpace -ge ($MinFreeGB * 1GB) })
    if ($roomy | Where-Object { $_.DeviceID -eq 'D:' }) { return 'D:\bn-render' }
    # Any other fixed drive with room, the one with the most free space; the Windows drive only as the last choice.
    $pick = $roomy | Sort-Object -Property @{ Expression = { $_.DeviceID -eq $env:SystemDrive } }, @{ Expression = { $_.FreeSpace }; Descending = $true } | Select-Object -First 1
    if ($pick) { return "$($pick.DeviceID)\bn-render" }
    return $null
}

$Root = Resolve-NodeRoot
if (-not $Root) {
    Report-Fail 'folders' "No drive on this PC has $MinFreeGB GB free for the render node."
}
$App = Join-Path $Root 'app'
$Tools = Join-Path $Root 'tools'
$State = Join-Path $Root 'state'
$Downloads = Join-Path $Root 'downloads'
$Venv = Join-Path $Root 'venv'
$VenvPython = Join-Path $Venv 'Scripts\python.exe'
$VenvPythonW = Join-Path $Venv 'Scripts\pythonw.exe'
$EnvFile = Join-Path $Root '.env'
$NodeAccount = "$env:COMPUTERNAME\$NodeUser"

function Update-SessionPath {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
}

# Each step is its own process started from the same window, which keeps the PATH the window opened with. Reading the
# current PATH finds programs an earlier step installed (Git, Python).
Update-SessionPath

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

function Get-KeyPrefix([string]$Key) {
    if ($Key.Length -le 12) { return $Key }
    return $Key.Substring(0, 12)
}

function Find-KeyFile {
    # Desktop, Downloads, Documents, OneDrive\Desktop, the public Desktop, then the top of every fixed and removable
    # drive and one folder below it. The first match wins.
    $places = @(
        [Environment]::GetFolderPath('Desktop'),
        (Join-Path $env:USERPROFILE 'Downloads'),
        [Environment]::GetFolderPath('MyDocuments'),
        (Join-Path $env:USERPROFILE 'OneDrive\Desktop'),
        (Join-Path $env:PUBLIC 'Desktop')
    )
    if ($env:OneDrive) { $places += (Join-Path $env:OneDrive 'Desktop') }
    foreach ($place in $places) {
        if ($place -and (Test-Path (Join-Path $place $KeyFileName))) { return (Join-Path $place $KeyFileName) }
    }
    $drives = @(Get-CimInstance -ClassName Win32_LogicalDisk | Where-Object { $_.DriveType -in @(2, 3) } | Sort-Object DeviceID)
    foreach ($drive in $drives) {
        $top = "$($drive.DeviceID)\"
        if (Test-Path (Join-Path $top $KeyFileName)) { return (Join-Path $top $KeyFileName) }
    }
    foreach ($drive in $drives) {
        $top = "$($drive.DeviceID)\"
        foreach ($folder in @(Get-ChildItem -Path $top -Directory -Force -ErrorAction SilentlyContinue)) {
            $candidate = Join-Path $folder.FullName $KeyFileName
            if (Test-Path $candidate) { return $candidate }
        }
    }
    return $null
}

function Read-KeyFromFile([string]$Path) {
    $text = Get-Content -Raw -Path $Path
    $match = [regex]::Match($text, 'bn_[A-Za-z0-9_\-]{8,}')
    if ($match.Success) { return $match.Value }
    if ($text -and ($text.Trim() -notmatch '\s')) { return $text.Trim() }
    return $null
}

function Get-SavedKey {
    if (-not (Test-Path $EnvFile)) { return $null }
    $line = Get-Content -Path $EnvFile | Where-Object { $_ -match '^\s*BN_API_KEY\s*=\s*\S' } | Select-Object -First 1
    if (-not $line) { return $null }
    return ($line -split '=', 2)[1].Trim()
}

function Protect-EnvFile {
    # Only the node's account and administrators can read the key.
    $reset = Invoke-Native 'icacls.exe' @($EnvFile, '/reset')
    $narrow = Invoke-Native 'icacls.exe' @($EnvFile, '/inheritance:r', '/grant:r', "${NodeAccount}:(M)", "${AdministratorsSid}:(F)")
    if ($reset.Code -ne 0 -or $narrow.Code -ne 0) { Report-Fail 'key' "The key file's permissions could not be set: $(Get-LastLines ($reset.Output + $narrow.Output) 1)" }
}

function New-NodePassword {
    # 64 symbols, so every random byte maps evenly; none of them needs quoting on a command line.
    $alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789-_.=+@#'
    $random = [Security.Cryptography.RandomNumberGenerator]::Create()
    $bytes = New-Object byte[] 40
    do {
        $random.GetBytes($bytes)
        $password = -join ($bytes | ForEach-Object { $alphabet[$_ % 64] })
    } until ($password -cmatch '[A-Z]' -and $password -cmatch '[a-z]' -and $password -match '[0-9]' -and $password -match '[-_.=+@#]')
    return $password
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

function Get-FailedChecks([string]$Report) {
    if (-not (Test-Path $Report)) { return @('selfcheck') }
    $parsed = Get-Content -Raw -Path $Report | ConvertFrom-Json
    return @($parsed.checks | Where-Object { $_.status -eq 'FAIL' } | ForEach-Object { $_.name })
}

# The steps

function Step-Admin {
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Report-Fail 'admin' 'Claude Code must be started from a PowerShell window opened as administrator.'
    }
    Report-Pass 'admin' "Running as administrator ($env:USERDOMAIN\$env:USERNAME); work folder $Root."
}

function Step-Folders {
    $drive = Split-Path -Qualifier $Root
    $free = (Get-PSDrive -Name $drive.TrimEnd(':')).Free
    foreach ($folder in @($Root, $Tools, $State, $Downloads, (Join-Path $Root 'work'), (Join-Path $Root 'models'), (Join-Path $Root 'assets'))) {
        New-Item -ItemType Directory -Force -Path $folder | Out-Null
    }
    Report-Pass 'folders' ("{0} is ready ({1:N0} GB free on {2})." -f $Root, ($free / 1GB), $drive)
}

function Step-Winget {
    $result = Invoke-Native 'winget' @('--version')
    if ($result.Code -ne 0) {
        Report-Note 'Registering the Windows package manager (App Installer)...'
        try {
            Add-AppxPackage -RegisterByFamilyName -MainPackage Microsoft.DesktopAppInstaller_8wekyb3d8bbwe -ErrorAction Stop
        } catch {
            Report-Note "App Installer could not be registered: $($_.Exception.Message)"
        }
        Update-SessionPath
        $result = Invoke-Native 'winget' @('--version')
    }
    if ($result.Code -ne 0) { Report-Fail 'winget' 'winget is not available on this PC.' }
    Invoke-Native 'winget' @('source', 'update') | Out-Null
    Report-Pass 'winget' "winget $($result.Output.Trim())"
}

function Step-Git {
    Update-SessionPath
    $result = Invoke-Native 'git' @('--version')
    if ($result.Code -ne 0) {
        Report-Note 'Installing Git...'
        Install-WithWinget 'Git.Git' @('--scope', 'machine') | Out-Null
        $result = Invoke-Native 'git' @('--version')
        if ($result.Code -ne 0) { Report-Fail 'git' 'Git did not install.' }
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
    if (-not $python) {
        Install-WithWinget 'Python.Python.3.12' @() | Out-Null
        $python = Find-Python312
    }
    if (-not $python) { Report-Fail 'python' 'Python 3.12 did not install.' }
    $version = Invoke-Native $python @('--version')
    if ($version.Output -notmatch 'Python 3\.12\.') { Report-Fail 'python' "Found $($version.Output.Trim()) at $python; 3.12 is needed." }
    Report-Pass 'python' "$($version.Output.Trim()) at $python"
}

function Step-Ffmpeg {
    $target = Join-Path $Tools 'ffmpeg\bin'
    $ffmpeg = Join-Path $target 'ffmpeg.exe'
    if (Test-Path $ffmpeg) {
        $encoders = Invoke-Native $ffmpeg @('-hide_banner', '-encoders')
        if ($encoders.Output -notmatch 'libx264') { Remove-Item -Recurse -Force (Join-Path $Tools 'ffmpeg') }
    }
    if (-not (Test-Path $ffmpeg)) {
        $found = Find-FfmpegFolder
        foreach ($id in @('Gyan.FFmpeg', 'Gyan.FFmpeg.Essentials')) {
            if ($found) { break }
            Report-Note "Installing ffmpeg ($id)..."
            Install-WithWinget $id @() | Out-Null
            $found = Find-FfmpegFolder
        }
        if (-not $found) { Report-Fail 'ffmpeg' 'ffmpeg did not install.' }
        New-Item -ItemType Directory -Force -Path $target | Out-Null
        Copy-Item -Path (Join-Path $found 'ffmpeg.exe') -Destination $target -Force
        Copy-Item -Path (Join-Path $found 'ffprobe.exe') -Destination $target -Force
    }
    $encoders = Invoke-Native $ffmpeg @('-hide_banner', '-encoders')
    if ($encoders.Output -notmatch 'libx264') { Report-Fail 'ffmpeg' "The ffmpeg in $target has no libx264 encoder." }
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
        if ($download.Code -ne 0) { Report-Fail 'blender' "The download stopped: $(Get-LastLines $download.Output 2). Run this step again to continue it." }
    }
    $size = (Get-Item $zip).Length
    if ($size -ne $BlenderSize) {
        Remove-Item -Force $zip
        Report-Fail 'blender' "The download is $size bytes, not $BlenderSize. It was deleted; run this step again."
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
    if (-not $inner) { Report-Fail 'blender' 'The Blender archive was empty after extracting; run this step again.' }
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
        $pull = Invoke-Native 'git' @('-c', "safe.directory=$($App -replace '\\', '/')", '-C', $App, 'pull', '--ff-only')
        if ($pull.Code -ne 0) { Report-Fail 'repo' "git pull failed in ${App}: $(Get-LastLines $pull.Output 2)" }
    } else {
        if (Test-Path $App) { Remove-Item -Recurse -Force $App }
        $clone = Invoke-Native 'git' @('clone', '--branch', 'main', $RepoUrl, $App)
        if ($clone.Code -ne 0) { Report-Fail 'repo' "git clone failed: $(Get-LastLines $clone.Output 2)" }
    }
    if (-not (Test-Path (Join-Path $App 'bnrender\__main__.py'))) { Report-Fail 'repo' "$App has no bnrender package." }
    $head = Invoke-Native 'git' @('-c', "safe.directory=$($App -replace '\\', '/')", '-C', $App, 'log', '-1', '--format=%h %s')
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
    $requirements = Join-Path $App 'requirements.txt'
    $install = $null
    foreach ($attempt in 1..3) {
        $install = Invoke-Native $VenvPython @('-m', 'pip', 'install', '--disable-pip-version-check', '--quiet', '-r', $requirements)
        if ($install.Code -eq 0) { break }
        Invoke-Native $VenvPython @('-m', 'pip', 'install', '--disable-pip-version-check', '--quiet', '--upgrade', 'pip') | Out-Null
        Start-Sleep -Seconds 10
    }
    if ($install.Code -ne 0) { Report-Fail 'venv' "pip install failed: $(Get-LastLines $install.Output 3)" }
    # Checked by exit code: Windows PowerShell 5.1 strips double quotes inside arguments to native programs.
    $imports = Invoke-Native $VenvPython @('-c', 'import yaml, PIL, numpy, psutil')
    if ($imports.Code -ne 0) { Report-Fail 'venv' "The packages do not import: $(Get-LastLines $imports.Output 2)" }
    Report-Pass 'venv' "Python environment ready in $Venv"
}

function Step-Account {
    # Autologon from Microsoft Sysinternals stores the password as an LSA secret, not in the registry.
    $tool = Join-Path $Tools 'Autologon64.exe'
    if (-not (Test-Path $tool)) {
        $download = Invoke-Native 'curl.exe' @('-L', '--fail', '--silent', '--show-error', '--retry', '5', '-o', $tool, $AutologonUrl)
        if ($download.Code -ne 0) { Report-Fail 'account' "Autologon64.exe could not be downloaded: $(Get-LastLines $download.Output 1). Run this step again." }
    }
    $signature = Get-AuthenticodeSignature -FilePath $tool
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation') {
        Remove-Item -Force $tool
        Report-Fail 'account' "Autologon64.exe is not validly signed by Microsoft Corporation ($($signature.Status)); it was deleted. Run this step again."
    }
    $password = New-NodePassword
    $secure = ConvertTo-SecureString -String $password -AsPlainText -Force
    $existing = Get-LocalUser -Name $NodeUser -ErrorAction SilentlyContinue
    if ($existing) {
        Set-LocalUser -Name $NodeUser -Password $secure -PasswordNeverExpires $true
        Enable-LocalUser -Name $NodeUser
    } else {
        New-LocalUser -Name $NodeUser -Password $secure -FullName 'BowlersNetwork render node' `
            -Description 'Runs the BowlersNetwork render node' -PasswordNeverExpires -UserMayNotChangePassword `
            -AccountNeverExpires | Out-Null
    }
    $users = Get-LocalGroup -SID $UsersSid
    if (-not (Get-LocalGroupMember -Group $users -Member $NodeUser -ErrorAction SilentlyContinue)) {
        Add-LocalGroupMember -Group $users -Member $NodeUser
    }
    $administrators = Get-LocalGroup -SID $AdministratorsSid.TrimStart('*')
    if (Get-LocalGroupMember -Group $administrators -Member $NodeUser -ErrorAction SilentlyContinue) {
        Remove-LocalGroupMember -Group $administrators -Member $NodeUser
    }
    $autologon = Invoke-Native $tool @('/accepteula', $NodeUser, $env:COMPUTERNAME, $password)
    Remove-Variable -Name password, secure
    $winlogonKey = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
    $winlogon = Get-ItemProperty -Path $winlogonKey
    if ($winlogon.PSObject.Properties.Name -contains 'DefaultPassword') {
        Remove-ItemProperty -Path $winlogonKey -Name 'DefaultPassword'
    }
    if ($winlogon.AutoAdminLogon -ne '1' -or $winlogon.DefaultUserName -ne $NodeUser) {
        Report-Fail 'account' "Automatic sign-in for $NodeUser was not set ($(Get-LastLines $autologon.Output 1)). Run this step again."
    }
    Report-Pass 'account' "Standard account $NodeUser signs in automatically (new random password, kept only as an LSA secret)."
}

function Step-Key {
    if (-not (Get-LocalUser -Name $NodeUser -ErrorAction SilentlyContinue)) { Report-Fail 'key' 'Run the account step first.' }
    $saved = Get-SavedKey
    if ($saved) {
        Protect-EnvFile
        Report-Pass 'key' "already saved (prefix $(Get-KeyPrefix $saved)...)"
        return
    }
    $source = $KeyFile
    if (-not $source) { $source = Find-KeyFile }
    if (-not $source -or -not (Test-Path $source)) {
        Report-Fail 'key' "The key file $KeyFileName was not found on the Desktop, in Downloads or Documents, or on any drive."
    }
    $key = Read-KeyFromFile $source
    if (-not $key) { Report-Fail 'key' "The key file $KeyFileName does not hold a key." }
    $lines = @()
    if (Test-Path $EnvFile) { $lines = @(Get-Content -Path $EnvFile | Where-Object { $_ -notmatch '^\s*BN_API_KEY\s*=' }) }
    $lines += "BN_API_KEY=$key"
    [IO.File]::WriteAllLines($EnvFile, [string[]]$lines, (New-Object Text.UTF8Encoding($false)))
    $prefix = Get-KeyPrefix $key
    Remove-Variable -Name key, lines
    Protect-EnvFile
    Report-Pass 'key' "saved (prefix $prefix...)"
}

function Step-Access {
    if (-not (Get-LocalUser -Name $NodeUser -ErrorAction SilentlyContinue)) { Report-Fail 'access' 'Run the account step first.' }
    # The node's account changes everything; administrators have full control; every signed-in account can read and
    # run the bn command, and can write the state folder (status, pause and resume from any account).
    # Inheritance from the drive is removed: a drive root often lets every signed-in user change everything below it,
    # which would let any account change the node's code and run it as BNRender.
    $grant = Invoke-Native 'icacls.exe' @($Root, '/inheritance:r', '/grant:r', "${SystemSid}:(OI)(CI)F", "${AdministratorsSid}:(OI)(CI)F", "${NodeAccount}:(OI)(CI)M", "${AuthenticatedUsersSid}:(OI)(CI)RX", '/Q')
    if ($grant.Code -ne 0) { Report-Fail 'access' "Permissions on $Root could not be set: $(Get-LastLines $grant.Output 1)" }
    $state = Invoke-Native 'icacls.exe' @($State, '/grant', "${AuthenticatedUsersSid}:(OI)(CI)M", '/Q')
    if ($state.Code -ne 0) { Report-Fail 'access' "Permissions on $State could not be set: $(Get-LastLines $state.Output 1)" }
    if (Test-Path $EnvFile) { Protect-EnvFile }
    # git refuses a repository owned by another account unless it is named as safe.
    $safe = $App -replace '\\', '/'
    $known = Invoke-Native 'git' @('config', '--system', '--get-all', 'safe.directory')
    if ($known.Output -notmatch [regex]::Escape($safe)) {
        Invoke-Native 'git' @('config', '--system', '--add', 'safe.directory', $safe) | Out-Null
    }
    # No privacy questions on the node account's first sign-in, so nothing covers the screen.
    $oobe = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\OOBE'
    New-Item -Path $oobe -Force | Out-Null
    Set-ItemProperty -Path $oobe -Name 'DisablePrivacyExperience' -Value 1 -Type DWord
    Report-Pass 'access' "$NodeUser can change $Root; the key is readable by $NodeUser and administrators only."
}

function Step-Power {
    $commands = @(
        @('/change', 'standby-timeout-ac', '0'),
        @('/change', 'hibernate-timeout-ac', '0'),
        @('/change', 'monitor-timeout-ac', '0'),
        @('/change', 'disk-timeout-ac', '0'),
        @('/setacvalueindex', 'SCHEME_CURRENT', 'SUB_NONE', 'CONSOLELOCK', '0'),
        @('/setdcvalueindex', 'SCHEME_CURRENT', 'SUB_NONE', 'CONSOLELOCK', '0'),
        @('/setactive', 'SCHEME_CURRENT')
    )
    foreach ($arguments in $commands) {
        $result = Invoke-Native 'powercfg.exe' $arguments
        if ($result.Code -ne 0) { Report-Fail 'power' "powercfg $($arguments -join ' ') failed: $(Get-LastLines $result.Output 1)" }
    }
    # Some PCs do not support hibernation at all; then there is nothing to switch off.
    $hibernate = Invoke-Native 'powercfg.exe' @('/hibernate', 'off')
    if ($hibernate.Code -ne 0) { Report-Note "Hibernation could not be switched off ($(Get-LastLines $hibernate.Output 1)); its timeout is never." }
    foreach ($setting in @(@('SUB_SLEEP', 'STANDBYIDLE'), @('SUB_VIDEO', 'VIDEOIDLE'))) {
        $query = Invoke-Native 'powercfg.exe' @('/query', 'SCHEME_CURRENT', $setting[0], $setting[1])
        if ($query.Output -match 'Current AC Power Setting Index:\s*(0x[0-9a-fA-F]+)') {
            if ([Convert]::ToInt32($Matches[1], 16) -ne 0) { Report-Fail 'power' "$($setting[1]) on mains power is still $($Matches[1])." }
        }
    }
    Report-Pass 'power' 'On mains power the PC never sleeps or hibernates, the screen stays on, and waking needs no sign-in.'
}

function Step-Path {
    $bin = Join-Path $App 'bin'
    $machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $parts = @($machinePath.Split(';') | Where-Object { $_ })
    if ($parts -notcontains $bin) {
        [Environment]::SetEnvironmentVariable('Path', (($parts + $bin) -join ';'), 'Machine')
    }
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if ($userPath -and ($userPath.Split(';') -contains $bin)) {
        [Environment]::SetEnvironmentVariable('Path', (($userPath.Split(';') | Where-Object { $_ -and $_ -ne $bin }) -join ';'), 'User')
    }
    Update-SessionPath
    $command = Get-Command 'bn' -ErrorAction SilentlyContinue
    if (-not $command -or ($command.Source -ne (Join-Path $bin 'bn.cmd'))) { Report-Fail 'path' "The bn command is not found in $bin." }
    Report-Pass 'path' "$bin is on the machine PATH: bn status, bn pause and bn resume work from every account."
}

function Step-Selfcheck {
    $report = Join-Path $State 'selfcheck-setup.json'
    $result = Invoke-Bnrender @('selfcheck', '--report', $report)
    Write-Host $result.Output
    $failed = Get-FailedChecks $report
    if ($failed.Count -eq 0) {
        Report-Pass 'selfcheck' 'Everything the node needs works.'
        return
    }
    if (@($failed | Where-Object { $_ -ne 'gpu' }).Count -gt 0) {
        Report-Fail 'selfcheck' "Failed checks: $($failed -join ', ') (the FAIL lines above)."
    }
    # Only the graphics card failed. No driver is installed from here: winget's AMD package is a 2023 driver, which
    # would replace a newer one. The node never renders GPU work on the CPU, so GPU jobs wait until this passes.
    Report-Fail 'selfcheck' 'Blender cannot use the graphics card. Install the current AMD Software: Adrenalin Edition driver from amd.com, then run this step again.'
}

function New-NodeSettings {
    # Task priority 3 starts the runner at "above normal" (the default, 7, would be below normal).
    return New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
        -MultipleInstances IgnoreNew -Priority 3
}

function Step-Task {
    if (-not (Get-LocalUser -Name $NodeUser -ErrorAction SilentlyContinue)) { Report-Fail 'task' 'Run the account step first.' }
    Unregister-ScheduledTask -TaskName $OldCheckTaskName -Confirm:$false -ErrorAction SilentlyContinue
    # Interactive: runs in the node account's own desktop session (the graphics card and the screen work there), and
    # Task Scheduler stores no password.
    $action = New-ScheduledTaskAction -Execute $VenvPythonW -Argument '-m bnrender run' -WorkingDirectory $App
    $principal = New-ScheduledTaskPrincipal -UserId $NodeAccount -LogonType Interactive -RunLevel Limited
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $NodeAccount
    Register-ScheduledTask -TaskName $TaskName -Action $action -Principal $principal -Trigger $trigger -Settings (New-NodeSettings) -Force | Out-Null
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task -or $task.Principal.UserId -notlike "*$NodeUser" -or $task.Principal.LogonType -ne 'Interactive') {
        Report-Fail 'task' "The task '$TaskName' was not registered as expected."
    }
    Report-Pass 'task' "Task '$TaskName' starts the node when $NodeUser signs in (automatically after every start of the PC)."
}

function Step-Restart {
    Write-Host 'The PC restarts in 60 s and signs in to the BNRender account, which shows the BowlersNetwork status screen.'
    $result = Invoke-Native 'shutdown.exe' @('/r', '/t', '60', '/c', 'BowlersNetwork render node: starting')
    if ($result.Code -ne 0) { Report-Fail 'restart' "The restart could not be scheduled: $(Get-LastLines $result.Output 1)" }
    Report-Pass 'restart' 'Restart scheduled in 60 s.'
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
    'account' = { Step-Account }
    'key' = { Step-Key }
    'access' = { Step-Access }
    'power' = { Step-Power }
    'path' = { Step-Path }
    'selfcheck' = { Step-Selfcheck }
    'task' = { Step-Task }
    'restart' = { Step-Restart }
}

if ($Step) {
    if (-not $Steps.Contains($Step)) { Report-Fail 'setup' "There is no step '$Step'. Steps: $($Steps.Keys -join ', ')" }
    & $Steps[$Step]
    exit 0
}
foreach ($name in $Steps.Keys) {
    & $Steps[$name]
}
exit 0
