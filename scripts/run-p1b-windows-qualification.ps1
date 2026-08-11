#Requires -Version 5.1
<#
.SYNOPSIS
    Produce the P1b native-Windows qualification evidence on a local Windows VM.

.DESCRIPTION
    Phase 1 of two. This runs inside native Windows PowerShell on a local Windows
    VM -- Parallels Desktop, or any other provider the checker recognizes --
    against a clean checkout of this repository at an exact commit, and leaves
    exactly two files behind:

        artifacts/p1b-windows-qualification/junit.sanitized.xml
        artifacts/p1b-windows-qualification/evidence.json

    Phase 2 is `scripts/check-p1b-windows-qualification.py verify`, which reads
    those two files offline on any platform -- macOS included -- and needs no
    network, no GitHub and no access to this guest.

    There is no GitHub Actions lane and no hosted runner anywhere in this
    qualification. Nothing here bills anything.

    What it does, in order:

      1. Refuses to run anywhere but Windows.
      2. Requires the caller to name the commit under test and a fresh 64-hex
         challenge chosen by the verifier, and refuses a checkout that is not
         exactly that commit with a clean working tree. Both are read before
         anything else runs -- see "What the commit and clean-tree checks mean"
         below.
      3. Creates a private temporary directory for pytest's raw report. That
         report quotes assertion text, tracebacks, captured output and absolute
         guest paths, so it never enters the artifact tree and the directory is
         deleted before this script returns -- on the failure path too.
      4. Uses a CPython 3.11 interpreter and installs exactly three things into
         it. `$InstallCommands` is the whole list, and those three commands are
         also the only network this script performs -- see "What this fetches"
         below.
      5. Runs the whole discovery file, unfiltered. No -k, no -m, no --deselect,
         no --exitfirst: a narrowed run could report a green summary while the
         three native Windows cases never ran, which is the exact defect this
         qualification exists to rule out.
      6. Collects the Windows and virtualization facts only CIM and the service
         database can answer, and hands them to the checker as JSON. The facts
         are generic -- the raw manufacturer, model and BIOS strings, the
         hypervisor flag, and whichever guest-tools service the guest runs -- so
         nothing here decides which hypervisor is acceptable. The checker does.
      7. Invokes the checker, whose exit code is this script's exit code. pytest's
         own exit code decides nothing: it says whether cases failed, not which
         cases ran.

    What this fetches.
    -----------------
    `pip install pytest` resolves against whatever index the guest's pip is
    configured for and downloads pytest and its dependencies unless they are
    already present. The two editable installs build from this checkout, and pip
    may still reach the index for their build backend. So this script is not
    offline, and does not claim to be. What is pinned is that `$InstallCommands`
    is the *whole* list: no pip self-upgrade, no requirements file, no toolchain
    fetch, nothing downloaded outside those three commands. Phase 2 -- the
    checker's `verify` -- is the part that needs no network at all.

    What the commit and clean-tree checks mean.
    ------------------------------------------
    Both are measured in step 2, before the installs, before pytest and before
    anything is written into the artifact tree. That ordering is the property: a
    status taken afterwards would describe this script's own output. `artifacts/`
    is not in `.gitignore` -- measured, it reports as `?? artifacts/` -- so the
    two files below make `git status` dirty the moment they are written. The
    caches pytest and pip leave behind are ignored and would not have.

    The consequence is that a previous run's artifacts make the next run refuse.
    That is the honest behaviour and not a bug to work around: delete
    `artifacts/p1b-windows-qualification` before re-running, and the tree the
    evidence describes is the tree that was tested.

.PARAMETER ExpectedCommit
    The 40-character commit the verifier asked for. HEAD must be exactly this.

.PARAMETER Challenge
    A fresh 64-character lowercase hex string the verifier chose before this run.
    The evidence records sha256(challenge), never the challenge, and binds it to
    both artifact digests -- so a record built before this challenge existed
    cannot be presented for it.

    This is local attestation. It proves these artifacts came from a run holding
    this challenge and have not moved since. It is not remote attestation and not
    a proof that the guest was real: whoever runs this owns the machine.

.EXAMPLE
    # On the verifier (macOS), pick a challenge:
    #   python3 -c "import secrets; print(secrets.token_hex(32))"
    # On the guest:
    .\scripts\run-p1b-windows-qualification.ps1 `
        -ExpectedCommit 0123456789abcdef0123456789abcdef01234567 `
        -Challenge <64-hex>
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-f]{40}$')]
    [string] $ExpectedCommit,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-f]{64}$')]
    [string] $Challenge
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# --- 1. Windows only -------------------------------------------------------
# PowerShell 7 runs on macOS and Linux, so being in PowerShell is not being on
# Windows. `$IsWindows` does not exist under Windows PowerShell 5.1 and reading
# it under StrictMode would throw, so the platform is read from the runtime.
if ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
    throw "P1b Windows qualification refused: this runs on Windows, not $([System.Environment]::OSVersion.Platform)."
}

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $RepoRoot

$DiscoveryTest = 'packages/omnivia-core-client/tests/test_discovery.py'
$CheckerScript = 'scripts/check-p1b-windows-qualification.py'
$ArtifactDir = 'artifacts/p1b-windows-qualification'
$PythonSeries = '3.11'

# --- 2. Exactly the commit under test, and a clean tree --------------------
# Measured here, before anything writes into the checkout: the artifacts this
# script is about to produce would themselves make a later `git status` dirty --
# `artifacts/` is not ignored -- so a status taken afterwards would say nothing
# about what was tested. The header's "What the commit and clean-tree checks
# mean" has the consequence: clear the artifact directory before re-running.
$RevParse = & git rev-parse HEAD
if ($LASTEXITCODE -ne 0) { throw 'P1b Windows qualification refused: not a git checkout.' }
# Through Out-String rather than .Trim() on the raw result: a command that
# printed nothing yields $null, and calling a method on it would replace this
# script's own message with an unrelated one about a null-valued expression.
$HeadCommit = ($RevParse | Out-String).Trim()
if ($HeadCommit -ne $ExpectedCommit) {
    throw "P1b Windows qualification refused: HEAD is $HeadCommit, not the expected $ExpectedCommit."
}
$Porcelain = & git status --porcelain
$CheckoutStatus = if ([string]::IsNullOrWhiteSpace(($Porcelain | Out-String))) { 'clean' } else { 'dirty' }
if ($CheckoutStatus -ne 'clean') {
    throw "P1b Windows qualification refused: the working tree is not clean.`n$($Porcelain | Out-String)"
}

# --- 4. A CPython 3.11 interpreter, and exactly three installs -------------
# `py -3.11` is the Windows launcher's way of naming a series; a bare `python`
# is the fallback for a guest where the launcher is absent or the qualified
# interpreter is already the one on PATH. Whichever is found is then made to
# state its own version, because a launcher that resolved something else would
# otherwise go unnoticed.
$PythonExe = $null
$PythonArgs = @()
try {
    & py "-$PythonSeries" -c 'import sys'
    if ($LASTEXITCODE -eq 0) {
        $PythonExe = 'py'
        $PythonArgs = @("-$PythonSeries")
    }
} catch {
    # No launcher on this guest. `python` below is the other way to name it.
    $PythonExe = $null
}
if ($null -eq $PythonExe -and (Get-Command 'python' -ErrorAction SilentlyContinue)) {
    $PythonExe = 'python'
    $PythonArgs = @()
}
if ($null -eq $PythonExe) {
    throw "P1b Windows qualification refused: no CPython $PythonSeries interpreter was found."
}

$Interpreter = & $PythonExe @PythonArgs -c 'import platform; print(platform.python_implementation() + " " + platform.python_version())'
$Reported = ($Interpreter | Out-String).Trim()
if ($Reported -notmatch "^CPython $([regex]::Escape($PythonSeries))\.\d+$") {
    throw "P1b Windows qualification refused: the interpreter reports '$Reported', not CPython $PythonSeries.x."
}

# The whole install list. Order is the content of the last two entries:
# `omnivia-core` is unpublished and `omnivia-core-client` declares
# `omnivia-core>=0.1.0,<0.2.0`, so installing the client first sends pip to an
# index that does not have the dependency. Installed root-first, both resolve
# against this checkout.
#
# These three commands do reach the network: `pip install pytest` downloads
# pytest and its dependencies from the configured index unless the guest already
# has them, and an editable install may fetch its build backend. The claim here
# is not that nothing is downloaded -- it is that nothing is downloaded *outside
# this list*, deliberately not even `pip install --upgrade pip`, so the
# qualifying environment is these three commands and the interpreter that was
# already here rather than whatever a self-upgrade resolved on the day.
$InstallCommands = @(
    @('-m', 'pip', 'install', 'pytest'),
    @('-m', 'pip', 'install', '-e', '.'),
    @('-m', 'pip', 'install', '-e', 'packages/omnivia-core-client')
)
foreach ($command in $InstallCommands) {
    & $PythonExe @PythonArgs @command
    if ($LASTEXITCODE -ne 0) {
        throw "P1b Windows qualification refused: '$($command -join ' ')' failed."
    }
}

# --- 3. A private temporary directory for the raw report -------------------
# Under $env:TEMP, which is per-user on Windows, with a name no other process
# can predict, and removed in `finally` so a failing run does not leave the raw
# report on disk either.
$RawRoot = Join-Path $env:TEMP ("p1b-raw-junit-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $RawRoot -Force | Out-Null

try {
    $RawJunit = Join-Path $RawRoot 'junit.xml'
    $FactsFile = Join-Path $RawRoot 'facts.json'

    # --- 5. The whole file, unfiltered -----------------------------------
    # `--strict-markers` makes a mistyped marker an error rather than a silent
    # no-op. The exit code is deliberately not checked: the checker reads the
    # record and decides, because a pass/fail code says nothing about which
    # cases ran.
    & $PythonExe @PythonArgs -m pytest $DiscoveryTest -q -rs --strict-markers "--junitxml=$RawJunit"

    # --- 6. What only CIM and the service database can answer -------------
    # Raw and generic. These are the properties as the guest reports them, with
    # no hypervisor named anywhere in this file: Parallels writes 'Parallels
    # International GmbH' / 'Parallels Virtual Platform' / 'Parallels Software
    # International Inc.' across the three, VMware and QEMU write their own
    # strings, and which of those is acceptable is the checker's decision rather
    # than this collector's.
    $OperatingSystem = Get-CimInstance -ClassName Win32_OperatingSystem
    $ComputerSystem = Get-CimInstance -ClassName Win32_ComputerSystem
    $Bios = Get-CimInstance -ClassName Win32_BIOS

    # A guest-tools signal if there is one, and two empty strings if there is
    # not. Deliberately not a lookup of one service name: 'VMTools' is VMware's,
    # Parallels ships 'prl_tools_service' on some builds and 'prl_tools' on
    # others, a QEMU guest runs 'QEMU-GA' beside SPICE's 'vdservice' and
    # 'spice-webdavd', and a single guessed name is exactly the uncertain
    # dependency that makes a real guest look unidentified. So the whole service
    # list is scanned for anything whose service name or display name names a
    # virtualization vendor or guest-agent stack, and the *display* name is what
    # is recorded -- 'Parallels Tools Service', 'VMware Tools' and 'QEMU Guest
    # Agent' name their vendor where 'prl_tools', 'VMTools' and 'vdservice' do
    # not.
    #
    # Nothing about the verdict depends on finding one. A guest whose CIM
    # identity above already names its provider qualifies with no tools
    # installed at all.
    $GuestToolsPattern = 'parallels|vmware|virtualbox|qemu|spice|^prl_|^vm3dservice|^vmtools|^vboxservice|^vdservice'
    $ToolsService = @(
        Get-CimInstance -ClassName Win32_Service |
            Where-Object { $_.Name -match $GuestToolsPattern -or $_.DisplayName -match $GuestToolsPattern }
    ) | Select-Object -First 1
    $ToolsName = ''
    $ToolsVersion = ''
    if ($null -ne $ToolsService) {
        $ToolsName = [string] $ToolsService.DisplayName
        if ([string]::IsNullOrWhiteSpace($ToolsName)) { $ToolsName = [string] $ToolsService.Name }
        # `PathName` is a command line, not a path: the executable may be quoted
        # and may be followed by arguments, so it is unwrapped before the file is
        # asked for its version. A service whose binary cannot be resolved leaves
        # the version empty rather than failing the run.
        $ToolsPath = ([string] $ToolsService.PathName).Trim()
        if ($ToolsPath -match '^"([^"]+)"') { $ToolsPath = $Matches[1] }
        elseif ($ToolsPath -match '^(\S+\.exe)') { $ToolsPath = $Matches[1] }
        if ($ToolsPath -and (Test-Path -LiteralPath $ToolsPath -PathType Leaf)) {
            $ToolsVersion = [string] (Get-Item -LiteralPath $ToolsPath).VersionInfo.ProductVersion
        }
    }

    # Every value a string: the checker refuses a non-string rather than
    # coercing it, so an absent CIM property must arrive as '' and not as null.
    $Facts = [ordered] @{
        windows_caption        = [string] $OperatingSystem.Caption
        windows_version        = [string] $OperatingSystem.Version
        windows_build          = [string] $OperatingSystem.BuildNumber
        computer_manufacturer  = [string] $ComputerSystem.Manufacturer
        computer_model         = [string] $ComputerSystem.Model
        bios_vendor            = [string] $Bios.Manufacturer
        hypervisor_present     = ([string] $ComputerSystem.HypervisorPresent).ToLowerInvariant()
        guest_tools_service    = [string] $ToolsName
        guest_tools_version    = [string] $ToolsVersion
        powershell_edition     = [string] $PSVersionTable.PSEdition
        powershell_executable  = [string] ([System.Diagnostics.Process]::GetCurrentProcess().MainModule.FileName)
        powershell_version     = [string] $PSVersionTable.PSVersion
        checkout_status        = $CheckoutStatus
    }
    # Windows PowerShell 5.1 puts a byte-order mark on the front of this;
    # PowerShell 7 does not. The checker reads it as `utf-8-sig`, which accepts
    # either, so this line does not have to know which edition it is running on.
    $Facts | ConvertTo-Json -Depth 2 | Set-Content -LiteralPath $FactsFile -Encoding UTF8

    # --- 7. The checker decides -------------------------------------------
    # The artifact directory is emptied first, so what is left there is this
    # run's evidence and nothing carried over from an earlier one.
    if (Test-Path -LiteralPath $ArtifactDir) {
        Remove-Item -LiteralPath $ArtifactDir -Recurse -Force
    }
    New-Item -ItemType Directory -Path $ArtifactDir -Force | Out-Null

    & $PythonExe @PythonArgs $CheckerScript produce `
        "--junit=$RawJunit" `
        "--facts=$FactsFile" `
        "--sanitized-junit=$ArtifactDir/junit.sanitized.xml" `
        "--evidence=$ArtifactDir/evidence.json" `
        "--expected-commit=$ExpectedCommit" `
        "--challenge=$Challenge"
    $Verdict = $LASTEXITCODE

    # Exact relative paths, not file names. `-Recurse` walks subdirectories, so a
    # comparison on names alone would accept `nested/evidence.json` -- a file in
    # a directory this lane never creates -- as one of the two artifacts. The
    # separator is normalized so the compared strings do not depend on the
    # platform that produced them.
    $ArtifactRoot = (Get-Item -LiteralPath $ArtifactDir).FullName.TrimEnd('\', '/')
    $Written = @(
        Get-ChildItem -LiteralPath $ArtifactDir -Recurse -File |
            ForEach-Object { $_.FullName.Substring($ArtifactRoot.Length + 1).Replace('\', '/') } |
            Sort-Object
    )
    if (($Written -join ',') -ne 'evidence.json,junit.sanitized.xml') {
        throw "P1b Windows qualification refused: $ArtifactDir holds $($Written -join ', '), not exactly the two artifacts."
    }

    Write-Host "P1b evidence written to $ArtifactDir. Verify it offline with:"
    Write-Host "  python3 $CheckerScript verify --sanitized-junit $ArtifactDir/junit.sanitized.xml --evidence $ArtifactDir/evidence.json --expected-commit $ExpectedCommit --challenge <the challenge>"
    exit $Verdict
}
finally {
    Remove-Item -LiteralPath $RawRoot -Recurse -Force -ErrorAction SilentlyContinue
}
