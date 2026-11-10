# Render node setup on the Windows PC

For the Claude Code session that sets up BowlersNetwork's render node on Boss's PC. Follow it step by step, in order.
Do only what this file says. If something fails in a way this file does not list, stop and tell Boss the exact FAIL
line; do not improvise fixes and do not edit any file in `D:\bn-render\app`.

What the node is: an idle PC that renders the studio library overnight, uploads each finished item through the
BowlersNetwork API, registers it, and deletes it locally. After setup Boss uses three commands: `bn status`,
`bn pause`, `bn resume`. Everything else is automatic.

## Rules that always apply

1. **The API key is secret.** Never open, print, `cat`, `type`, `Get-Content` or otherwise read the key file, and never
   put the key in a command line, chat message or log. You only ever pass the key file's path to `setup.ps1`, which
   reads it silently.
2. Run every command from a terminal that runs **as administrator**.
3. Your shell may be Git Bash. Every command below is written so it works from Git Bash and from PowerShell: PowerShell
   commands are wrapped in `powershell.exe -NoProfile -ExecutionPolicy Bypass ...`.
4. Use a 10-minute timeout for each command. Every step can be run again safely; a step that timed out continues where
   it stopped when run again.
5. The work folder is `D:\bn-render`. If Boss says the PC has no D: drive, ask him which drive to use and add
   `-Root X:\bn-render` (his drive letter) to every `setup.ps1` command, and use that folder everywhere below.

## Before you start: ask Boss for one thing

The repository is `https://github.com/mumitprottoy/bn-render-node.git` (public, branch `main`).

1. **The path of the key file** he brought (for example `E:\studio-node-api-key.txt` on a USB stick). Ask for the path
   only. Do not open the file.

## Step 0: administrator check

```
powershell.exe -NoProfile -Command "([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)"
```

Expected: `True`. If `False`: ask Boss to close Claude Code, open PowerShell with right-click, "Run as administrator",
start Claude Code there, and tell you to continue. Then run Step 0 again.

## Step 1: Git and the repository

```
git --version
```

If that fails, install Git, then check again:

```
winget install --id Git.Git -e --silent --scope machine --accept-package-agreements --accept-source-agreements
```

Then clone the repository:

```
powershell.exe -NoProfile -Command "New-Item -ItemType Directory -Force -Path D:\bn-render | Out-Null"
git clone --branch main https://github.com/mumitprottoy/bn-render-node.git D:/bn-render/app
```

Expected: `Cloning into 'D:/bn-render/app'...` and no error. If `D:\bn-render\app` already exists and
`git -C D:/bn-render/app status` works, skip the clone.

## Step 2: run setup.ps1 one step at a time

Run each command below in order. Each prints one `PASS` line (sometimes with notes) when it worked. On `FAIL`, use the
table under the step; then run the same step again.

The command for a step (replace `<step>`):

```
powershell.exe -NoProfile -ExecutionPolicy Bypass -File D:\bn-render\app\setup.ps1 -Step <step>
```

| Order | Step | Expected PASS line (roughly) | Takes |
|---|---|---|---|
| 1 | `admin` | `PASS  admin      Running as administrator (...)` | seconds |
| 2 | `folders` | `PASS  folders    D:\bn-render is ready (N GB free on D:).` | seconds |
| 3 | `winget` | `PASS  winget     winget v1.x` | seconds |
| 4 | `git` | `PASS  git        git version 2.x` | seconds |
| 5 | `python` | `PASS  python     Python 3.12.x at C:\Program Files\Python312\python.exe` | 1 to 3 min |
| 6 | `ffmpeg` | `PASS  ffmpeg     ffmpeg and ffprobe with libx264 in D:\bn-render\tools\ffmpeg\bin` | 1 to 3 min |
| 7 | `blender` | `PASS  blender    Blender 5.2.3 installed in D:\bn-render\tools\blender (size and SHA-256 verified)` | 2 to 15 min |
| 8 | `repo` | `PASS  repo       D:\bn-render\app at <commit> <message>` | seconds |
| 9 | `venv` | `PASS  venv       Python environment ready in D:\bn-render\venv` | 1 to 3 min |
| 10 | `key` | `PASS  key        The API key is saved in D:\bn-render\.env (not shown).` | seconds |
| 11 | `power` | `PASS  power      The PC never sleeps or hibernates on mains power.` | seconds |
| 12 | `path` | `PASS  path       D:\bn-render\app\bin is on the user PATH; ...` | seconds |
| 13 | `selfcheck` | a list of `PASS` / `SKIP` lines, then `PASS  selfcheck  Everything the node needs works in this window.` | 1 to 3 min |
| 14 | `task` | `PASS  task       Task 'BN Render Node' starts the node ...` | 1 to 5 min |
| 15 | `start` | the `bn status` screen, then `PASS  start      The render node is running.` | 30 s |

The `key` step is different: give it the key file's path.

```
powershell.exe -NoProfile -ExecutionPolicy Bypass -File D:\bn-render\app\setup.ps1 -Step key -KeyFile "<path Boss gave>"
```

### What to do on FAIL

| Step | FAIL says | Do this |
|---|---|---|
| `admin` | not running as administrator | Go back to Step 0. |
| `folders` | Drive D: is not there | Ask Boss which drive to use; see rule 5. |
| `folders` | Only N GB free | Tell Boss how much is free; at least 60 GB is needed. Wait for him to free space. |
| `winget` | winget is missing | Ask Boss to install "App Installer" from the Microsoft Store, then run the step again. |
| `git`, `python`, `ffmpeg` | did not install | Run the `winget install` command the FAIL line shows and read its output. If it says no package was found, run `winget source reset --force`, then the step again. If it still fails, stop and tell Boss. |
| `ffmpeg` | has no libx264 encoder | Run `powershell.exe -NoProfile -Command "Remove-Item -Recurse -Force D:\bn-render\tools\ffmpeg"`, then the step again. |
| `blender` | The download failed | Run the step again; the download continues where it stopped. |
| `blender` | SHA-256 mismatch, or wrong size | The bad file was deleted. Run the step once more. If it fails the same way again, stop and tell Boss: do not use any other Blender download. |
| `repo` | git pull failed / git clone failed | Check the network and the URL with Boss, then run the step again. |
| `venv` | pip install failed | Run the step again (network). If it fails twice, stop and tell Boss the FAIL line. |
| `key` | The key file ... is not there | Ask Boss for the correct path. |
| `key` | No key was given | Ask Boss for the path again. Or ask Boss to run, in his own PowerShell window opened as administrator, `powershell -ExecutionPolicy Bypass -File D:\bn-render\app\setup.ps1 -Step key` and paste the key at the hidden prompt. Then run the step yourself: it must say the key is already saved. |
| `power` | powercfg ... failed | Stop and tell Boss the FAIL line. |
| `path` | The bn command is not found | Stop and tell Boss the FAIL line. |
| `selfcheck` | see the next table | Fix the first FAIL line listed by the self-check, then run the step again. |
| `task` | any | Stop and tell Boss the FAIL line. |
| `start` | did not report RUNNING | Show Boss the last 40 lines of `D:\bn-render\state\log.txt` (it never contains the key): `powershell.exe -NoProfile -Command "Get-Content D:\bn-render\state\log.txt -Tail 40"`. Stop. |

### Self-check FAIL lines

| Line starts with | Meaning | Do this |
|---|---|---|
| `FAIL  api        The API key was refused` | the key is wrong or was revoked | Tell Boss. He needs a new key from the server (the Mac session runs `manage.py studio_node_key create`). Remove the old one with `powershell.exe -NoProfile -Command "(Get-Content D:\bn-render\.env) | Where-Object { $_ -notmatch '^BN_API_KEY=' } | Set-Content D:\bn-render\.env"` (this does not show the key), then run the `key` step with the new file. |
| `FAIL  api        The API could not be reached` | network or Cloudflare | Check the internet connection with Boss, then run `selfcheck` again. |
| `FAIL  api        There is no BN_API_KEY in .env` | the key step was skipped | Run the `key` step. |
| `FAIL  assets` | scene textures could not be downloaded | Run `selfcheck` again (network). |
| `FAIL  blender` | Blender does not run or is the wrong version | Run the `blender` step again. |
| `FAIL  render     blender/render_item.py is missing` | the repo is older than the setup expects | Run the `repo` step, then `selfcheck`. If it is still missing, stop and tell Boss. |
| `FAIL  render     The test render failed` | Blender could not render the test scene | Stop and tell Boss the full line. |
| `FAIL  gpu        Blender sees no HIP graphics device` | the AMD driver is missing or too old | Ask Boss to install the current AMD Software: Adrenalin Edition driver from amd.com for the Radeon RX 6600 and restart the PC. Then run `selfcheck` again. Do not continue until `gpu` passes: rendering on the CPU would take weeks. |
| `FAIL  jobs` or `FAIL  labels` | a job file in the repo has a problem | Stop and tell Boss the lines (the Mac side fixes job files). |
| `FAIL  ffmpeg`, `FAIL  ffprobe`, `FAIL  x264` | ffmpeg is missing or incomplete | Delete `D:\bn-render\tools\ffmpeg` as in the ffmpeg row above, run the `ffmpeg` step, then `selfcheck`. |
| `FAIL  disk` | less than 5 GB free | Tell Boss. |
| `FAIL  python`, `FAIL  packages`, `FAIL  psutil` | the Python environment is incomplete | Run the `venv` step, then `selfcheck`. |

### About the `task` step

It tests the graphics card the way the node runs at boot (before anyone signs in). Two outcomes are both a PASS:

- **"starts the node at boot (before sign-in) and at sign-in"**: the node restarts by itself after a reboot or power cut.
- **"starts the node when <user> signs in"**, with a note that the graphics card cannot be used before sign-in: tell
  Boss that after a power cut the node continues once he signs in to Windows. Do not set up automatic sign-in yourself;
  if Boss wants it, that is his decision.

## Step 3: confirm and sign off

```
cmd.exe /c D:\bn-render\app\bin\bn.cmd status
```

Expected: the first line reads `BowlersNetwork render node` followed by `RUNNING since HH:MM`. The job list may show
jobs `waiting` or `queued` for the first minutes while the node fetches its scene files; that is normal.

When `RUNNING` shows, the setup is complete. Tell Boss, in these words or close to them:

> The render node is set up and running. In any new PowerShell window:
> `bn status` shows what is done, what is left and when it will finish;
> `bn pause` finishes the frame in progress and pauses (run it twice to stop at once);
> `bn resume` carries on.
> It starts by itself after a restart, updates itself, and when every job is done it deletes its work files and its
> key. You can now remove the key file from the USB stick or wherever you brought it.

Then stop. Do not run `python -m bnrender run` yourself (the scheduled task runs it), and do not delete or edit
`D:\bn-render\.env`.
