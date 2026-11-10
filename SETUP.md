# Render node setup on the Windows PC

## Before you start (Boss)

1. Copy `studio-node-api-key.txt` onto the PC's Desktop, or onto a USB stick plugged into the PC.
2. Open PowerShell as administrator (right-click the Start button, then "Terminal (Admin)" or "Windows PowerShell
   (Admin)").
3. Start Claude Code in that window and say: **read and follow SETUP.md** (it is at
   `https://github.com/mumitprottoy/bn-render-node/blob/main/SETUP.md`).

Nothing else is needed. The PC sets itself up and then restarts into the render node.

## For the Claude Code session

The repository is `https://github.com/mumitprottoy/bn-render-node.git` (public, branch `main`).

What the node is: this PC renders BowlersNetwork's studio library, uploads each finished item through the
BowlersNetwork API and deletes it locally. It runs in its own Windows account, `BNRender`, which signs in by itself and
shows a full-screen status page.

### Rules

1. **Do not ask Boss anything.** When a step fails, apply the fix listed below yourself and run the step again. Message
   Boss only in the cases under "When to stop", with the sentence given there, and then stop.
2. **The API key is secret.** Never open, print, `cat`, `type` or `Get-Content` the key file or `.env`, and never put
   the key in a command, message or log. `setup.ps1` finds the key file itself and prints only its first 12 characters.
3. Your shell may be Git Bash. Every command below works from Git Bash and from PowerShell.
4. Use a 10-minute timeout for each command. Every step is safe to run again; a step that timed out continues where it
   stopped when run again.
5. Do not edit any file of the repository.

### Step 1: get the setup files

```
git --version
```

If that fails: `winget install --id Git.Git -e --silent --scope machine --accept-package-agreements --accept-source-agreements`

```
git clone --branch main https://github.com/mumitprottoy/bn-render-node.git C:/Users/Public/bn-render-setup
```

If `C:\Users\Public\bn-render-setup` already exists, run `git -C C:/Users/Public/bn-render-setup pull --ff-only`
instead.

### Step 2: run setup.ps1 one step at a time

The command for a step (replace `<step>`):

```
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Users\Public\bn-render-setup\setup.ps1 -Step <step>
```

Run the steps in this order. Each prints one `PASS` line when it worked (sometimes with notes above it).

| Order | Step | Expected PASS line (roughly) | Takes |
|---|---|---|---|
| 1 | `admin` | `PASS  admin      Running as administrator (...); work folder D:\bn-render.` | seconds |
| 2 | `folders` | `PASS  folders    D:\bn-render is ready (N GB free on D:).` | seconds |
| 3 | `winget` | `PASS  winget     winget v1.x` | seconds |
| 4 | `git` | `PASS  git        git version 2.x` | seconds |
| 5 | `python` | `PASS  python     Python 3.12.x at ...` | 1 to 3 min |
| 6 | `ffmpeg` | `PASS  ffmpeg     ffmpeg and ffprobe with libx264 in ...\tools\ffmpeg\bin` | 1 to 3 min |
| 7 | `blender` | `PASS  blender    Blender 5.2.3 installed in ...\tools\blender (size and SHA-256 verified)` | 2 to 15 min |
| 8 | `repo` | `PASS  repo       ...\app at <commit> <message>` | seconds |
| 9 | `venv` | `PASS  venv       Python environment ready in ...\venv` | 1 to 3 min |
| 10 | `account` | `PASS  account    Standard account BNRender signs in automatically (...)` | seconds |
| 11 | `key` | `PASS  key        saved (prefix bn_xxxxxxxxx...)` | seconds |
| 12 | `access` | `PASS  access     BNRender can change ...; the key is readable by BNRender and administrators only.` | under 2 min |
| 13 | `power` | `PASS  power      On mains power the PC never sleeps or hibernates, ...` | seconds |
| 14 | `path` | `PASS  path       ...\app\bin is on the machine PATH: ...` | seconds |
| 15 | `selfcheck` | a list of `PASS` / `SKIP` lines, then `PASS  selfcheck  Everything the node needs works.` | 1 to 5 min |
| 16 | `task` | `PASS  task       Task 'BN Render Node' starts the node when BNRender signs in (...)` | seconds |

The work folder (`D:\bn-render` above) is chosen by `setup.ps1` itself: D: when it has 50 GB free, otherwise the fixed
drive with the most free space. Its PASS lines show which.

### Fixes to apply yourself

Apply the fix, then run the same step again. If a step still fails after three tries, stop and tell Boss its FAIL line.

| Step | FAIL says | Fix |
|---|---|---|
| `git`, `python`, `ffmpeg` | did not install | Run `winget source reset --force`, then the step again. |
| `blender` | The download stopped | Run the step again; the download continues where it stopped. |
| `blender` | The download is N bytes | Run the step again. |
| `blender` | SHA-256 mismatch | Run the step once more. A second mismatch: see "When to stop". |
| `repo` | git pull failed / git clone failed | Wait one minute, then run the step again. |
| `venv` | pip install failed | Run the step again. |
| `account` | could not be downloaded / not validly signed / was not set | Run the step again. |
| `key` | Run the account step first | Run the `account` step, then `key`. |
| `selfcheck` | `FAIL  api        The API could not be reached` | Wait one minute, then run `selfcheck` again. |
| `selfcheck` | `FAIL  assets` | Run `selfcheck` again. |
| `selfcheck` | `FAIL  blender` | Run the `blender` step, then `selfcheck`. |
| `selfcheck` | `FAIL  render     blender/render_item.py is missing` | Run the `repo` step, then `selfcheck`. |
| `selfcheck` | `FAIL  ffmpeg`, `FAIL  ffprobe` or `FAIL  x264` | Run the `ffmpeg` step, then `selfcheck`. |
| `selfcheck` | `FAIL  python`, `FAIL  packages` or `FAIL  psutil` | Run the `venv` step, then `selfcheck`. |
| `selfcheck` | `FAIL  api        There is no BN_API_KEY in .env` | Run the `key` step, then `selfcheck`. |
| `selfcheck` | only `gpu` failed | Download the current AMD Software: Adrenalin Edition driver for the Radeon RX 6600 from amd.com (drivers.amd.com needs the header `Referer: https://www.amd.com/`), check its signature, install it with `Setup.exe -INSTALL -LOG <file>`, then run `selfcheck` again. Never use winget's AMD package: it is a 2023 driver. |
| `power`, `path`, `access`, `task` | anything | Run the step again. |

### When to stop

These are the only times you message Boss. Send the sentence, then stop.

| Situation | Tell Boss |
|---|---|
| `admin` fails | Please start Claude Code from a PowerShell window opened as administrator, then say: read and follow SETUP.md. |
| A FAIL line says no drive has 50 GB free | This PC has no drive with 50 GB free for the render node. |
| `winget` fails | This PC has no Windows package manager; please install App Installer from the Microsoft Store. |
| `key` says the key file was not found, or holds no key | I could not find studio-node-api-key.txt; please put it on the Desktop and tell me to continue. |
| `selfcheck` says `The API key was refused` | The API key in studio-node-api-key.txt was refused; a new key is needed from the server. |
| `selfcheck` still fails `gpu` after the current Adrenalin driver is installed | Blender cannot use the graphics card here; the driver needs a person's attention. |
| `blender` reports a SHA-256 mismatch twice | The Blender download failed its checksum twice, so I stopped for safety. |
| `selfcheck` fails on `jobs`, `labels` or `disk`, or any step fails three times | Setup stopped at step `<step>`: `<the FAIL line>` |

### Step 3: the restart (last)

When every step above passed, tell Boss exactly this one line:

> Setup is done: the PC restarts in 60 seconds into the BNRender account and shows the BowlersNetwork status screen.

Then run the last step:

```
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Users\Public\bn-render-setup\setup.ps1 -Step restart
```

Expected: `PASS  restart    Restart scheduled in 60 s.` The restart ends this session; you are done.

What happens next, with no one touching the PC: Windows signs in to `BNRender` by itself, the scheduled task starts the
runner, the runner checks the graphics card in that session, opens the full-screen status page in Microsoft Edge, and
starts rendering.

## For Boss, after the setup

- **The screen** shows the progress, the item being made, the time left, and "Working", "Paused by Mumit Boss",
  "Restarting in N s" or "Finished".
- **To use the PC:** press Ctrl+Alt+Del, choose "Switch user", and sign in to your own account (its password is
  unchanged). The node keeps working in its own account in the background.
- **The three commands work from any account:** press Win+R, type `powershell`, press Enter, then `bn status`,
  `bn pause` or `bn resume`. If Win+R does not open over the full-screen page, press Ctrl+Alt+Del, open Task Manager,
  choose "Run new task", and type `powershell`.
- `bn pause` finishes the frame in progress and pauses (the screen says "Paused by Mumit Boss"); run it twice to stop at
  once. `bn resume` carries on.
- **Restarts:** the PC restarts itself about every 6 hours at a clean moment, never while paused and never while someone
  else is signed in, and comes back to the render node by itself. `bn status` shows the last restart.
- If `bn status` says GPU jobs are "waiting for the graphics card", the AMD driver needs attention.
- When every job is done the screen says "Finished": the node deletes its work files and removes its key.
- The key file can now be deleted from the Desktop or the USB stick. The folder `C:\Users\Public\bn-render-setup` is
  only the setup copy and can be deleted too.
