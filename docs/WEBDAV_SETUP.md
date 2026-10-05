# WebDAV — Mount Project Files as a Drive

This guide is for people who want to open Cordée project files directly in their OS file manager (Explorer/Finder/Files) via WebDAV. No server access is needed — only a WebDAV URL and the token from the operator.

The examples use `cordee.example` for the app and `dav.cordee.example` for an optional dedicated WebDAV host. Replace them with your own; the in-app **Files → WebDAV** panel shows the exact URLs for your install.

## URLs

| Scope | URL | Notes |
|-------|-----|-------|
| All projects | `https://dav.cordee.example/` | Every project appears as a top-level folder (`example-client/`, `internal-docs/`) |
| Single project | `https://dav.cordee.example/<slug>/` | e.g. `https://dav.cordee.example/example-client/` |
| Single folder | `https://dav.cordee.example/<slug>/Working%20Documents/` | Directly into the writable tree |

Without a dedicated host, use the app's own origin with the `/dav` prefix: `https://cordee.example/dav/<slug>/`. On the dedicated host both `https://dav.cordee.example/<slug>/` and `https://dav.cordee.example/dav/<slug>/` work (alias). Both origins use the same token.

**Slug** = lowercased hyphenated project name (e.g. `Example Client` → `example-client`). Find it in the app sidebar or via `rclone lsd cordee:`.

## Auth

WebDAV uses a shared **DAV token** (`AINGEL_DAV_TOKEN` in the server `.env`, never committed). Any username works; the **password is the token**.

Ask the operator for the token. It opens every project, so it is an operator credential. **Do not commit or share it publicly.** Example placeholder: `<DAV_TOKEN>`.

* `Authorization: Bearer <DAV_TOKEN>` or `Authorization: Basic base64(user:<DAV_TOKEN>)` both work. OS clients send Basic automatically when you enter `user = me` / `pass = <DAV_TOKEN>`.
* Requests without a valid token get `401` (with `WWW-Authenticate: Basic`). There is no loopback bypass: requests from the server itself (including AI agent tools) need the token too.

## Windows — Recommended: rclone + WinFsp (all projects as `Y:`)

Native `net use` / `Map network drive` is fragile behind proxies such as Cloudflare (`error 67`, `0x80070043`). `rclone` uses the same WebDAV that `curl` proves works (`207`) and mounts as a real drive.

**1. Install (once, as Administrator):**
```cmd
winget install --id WinFsp.WinFsp -e
winget install --id Rclone.Rclone -e
:: then restart your shell (close and reopen cmd)
```

**2. Create the remote (once, regular cmd):**
```cmd
rclone config create cordee webdav url https://dav.cordee.example/ vendor other user me pass <DAV_TOKEN> --obscure
:: single project only:
:: rclone config create cordeeone webdav url https://dav.cordee.example/example-client/ vendor other user me pass <DAV_TOKEN> --obscure
```
Config is at `%APPDATA%\rclone\rclone.conf` (encrypted pass, do not commit).

**3. Test:**
```cmd
rclone lsd cordee:
curl -u me:<DAV_TOKEN> -X PROPFIND https://dav.cordee.example/example-client/ -H "Depth: 1"
:: both should return 207 / list of projects
```

**4. Mount as `Y:` (keep this window open):**
```cmd
rclone mount cordee: Y: --vfs-cache-mode writes --links --webdav-encoding None --network-mode --log-file "%USERPROFILE%\rclone.log" --log-level INFO
```
`Y:\` appears in Explorer. `Y:\example-client\Working Documents\hello.txt` is `<projects root>/Example Client/Working Documents/hello.txt` on the server. Use quotes for spaces:
```cmd
echo hello > "Y:\example-client\Working Documents\hello.txt"
dir "Y:\example-client\Working Documents"
```

**5. Make it auto-start at logon (hidden, no window):**

`rclone.exe` is a console app: a scheduled task that launches it *directly* shows a console window all day (`--no-console` only clears the window's *text*, it does not remove the window). The fix is a tiny VBS launcher started by Task Scheduler with window style 0 (completely hidden — no window, no taskbar entry).

One-time setup (PowerShell, regular user first; the `Register` step needs an **elevated** shell):

```powershell
# 1) Hidden launcher VBS — uses the WinGet\Links shim (update-proof), NOT the
#    version-pinned package path (breaks on every winget upgrade)
$rc = (Get-Command rclone).Source
$dir = "$env:LOCALAPPDATA\Cordee"; New-Item -ItemType Directory -Path $dir -Force | Out-Null
$vbs = @"
' mount-cordee.vbs - mount Cordée projects as Y: (hidden, no console window)
Set sh = CreateObject("WScript.Shell")
logFile = sh.ExpandEnvironmentStrings("%USERPROFILE%") & "\rclone.log"
cmd = """" & "$rc" & """ mount cordee: Y: --vfs-cache-mode writes --links --webdav-encoding None --network-mode --log-file """ & logFile & """ --log-level INFO"
WScript.Quit sh.Run(cmd, 0, True)
"@
Set-Content "$dir\mount-cordee.vbs" $vbs -Encoding ASCII

# 2) Remove any previous autostart (Startup-folder .cmd/.vbs = double-fire racer)
Remove-Item "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\mount-cordee.*" -Force -ErrorAction SilentlyContinue

# 3) Register the task (elevated shell if overwriting an existing task —
#    Register-ScheduledTask -Force on an existing task returns Access denied)
$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$dir\mount-cordee.vbs`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName "RcloneCordee" -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force
Start-ScheduledTask -TaskName "RcloneCordee"
```

How it works: `wscript.exe` (a GUI app) runs the VBS, which starts rclone with `sh.Run(cmd, 0, True)` — window style 0 = invisible, and `True` makes the VBS wait on rclone so the task state mirrors the mount (`Running` while Y: is up) and `RestartCount 3` can revive a crashed mount. `ExecutionTimeLimit 0` stops Task Scheduler from killing the never-exiting rclone.

Remount after a drop (sleep/travel): kill any surviving rclone first, then start the task:

```powershell
Get-Process rclone -ErrorAction SilentlyContinue | Stop-Process -Force
Start-ScheduledTask RcloneCordee
```

The kill matters: after sleep rclone is usually still alive with a stale mount and the task is still `Running`, so `Start-ScheduledTask` is ignored and a second VBS double-click fails silently (hidden window). Killing rclone also makes the task exit non-zero, so Task Scheduler restarts it on its own within a minute. To remove the autostart: `Unregister-ScheduledTask RcloneCordee` + delete the VBS.

Gotchas:
- `RunLevel Limited` is correct — the mount must live in the *user* session; elevated rclone mounts a drive the user's Explorer cannot see.
- `Test-Path Y:\` from an **elevated** shell returns `False` even when Y: is mounted — drive letters are per-session. Verify from Explorer or a normal shell.
- Keep exactly **one** autostart mechanism. A leftover Startup-folder VBS + the task double-fires and one racer fails silently at every logon.

**Why not `net use`?** `net use Y: https://dav.cordee.example/...` and `\\dav.cordee.example@SSL\...` often give `System error 67` / `0x80070043` behind proxies such as Cloudflare even with correct creds. If you must try it, set once as admin:
```cmd
reg add "HKEY_LOCAL_MACHINE\SYSTEM\CurrentControlSet\Services\WebClient\Parameters" /v AuthForwardServerList /t REG_SZ /d "https://dav.cordee.example https://*.cordee.example" /f
reg add "HKEY_LOCAL_MACHINE\SYSTEM\CurrentControlSet\Services\WebClient\Parameters" /v BasicAuthLevel /t REG_DWORD /d 2 /f
net stop WebClient && net start WebClient
```
then `net use Y: \\dav.cordee.example@SSL\example-client /user:me <DAV_TOKEN>`. If it still `67`, use `rclone`/`RaiDrive`/`Cyberduck` above.

## macOS

Finder → `⌘K` → `https://dav.cordee.example/example-client/` (or `https://dav.cordee.example/` for all projects) → Connect → any username / `<DAV_TOKEN>`.

Or `rclone` same as Windows (no WinFsp needed, `brew install rclone`).

## Linux

**GNOME (Nautilus):** `gio mount davs://dav.cordee.example/example-client/` → password = token. Shows under Network.

**davfs2:**
```bash
sudo apt install davfs2
sudo mount -t davfs https://dav.cordee.example/example-client/ /mnt/cordee
# credentials in ~/.davfs2/secrets: https://dav.cordee.example/example-client/ me <DAV_TOKEN>
```

**rclone (same as Windows):**
```bash
rclone config create cordee webdav url https://dav.cordee.example/ vendor other user me pass <DAV_TOKEN> --obscure
rclone mount cordee: ~/mnt/cordee --vfs-cache-mode writes --links
```

## Server side (operators)

Two ways to serve WebDAV (see `SETUP.md` → WebDAV file access):

* **Integrated** — the main app (`cordee.service`, `127.0.0.1:8001`) serves `/dav/<slug>/`. Nothing extra to run.
* **Dedicated host** (optional) — `ops/examples/cordee-webdav.service` runs the standalone origin on `127.0.0.1:8002`. Point a `dav.<your-domain>` hostname at it through your reverse proxy or tunnel. Hosts whose name starts with `dav.` get alias-style paths (`/<slug>/`); set `AINGEL_DAV_HOST` to name a different host.

Both need `AINGEL_DAV_TOKEN` in `.env`; without it every request is refused. Identity headers from a forward-auth proxy are ignored unless `AINGEL_DAV_TRUST_HEADERS` is set, which is only safe behind a proxy that strips client-supplied copies.

`agent_webdav.py` enforces per-verb `agent_files._is_writable_rel` + `_safe_resolve` (writable-root `Working Documents`, forbidden `.db`/`.env`/`.git`, symlink containment). `PROPFIND` returns `207` with `DAV:` + `aingel:tags`/`aingel:note`. `GET` on collections returns HTML listing; WebDAV clients use `PROPFIND`. Alias without `/dav` (`/<slug>/`) is supported on the dedicated host for cleaner mounts, and `DavWWWRoot` prefix is stripped for Windows UNC.

**Modification times (verified with rclone v1.75.1):**
the server **reads** modtime correctly (`getlastmodified` on PROPFIND) and
**honors** `X-OC-Mtime` on PUT + `getlastmodified` in PROPPATCH
(epoch/HTTP-date/ISO, range-checked, never fails the verb) — both round-trip
byte-exact on curl probes. **Caveat:** rclone with `vendor=other` (the
`cordee:`/`cordeeone:` remotes as documented above) **never sends either
signal** (0 modtime-write attempts in the request log) — uploads land with
server-time mtime. That is stock rclone behavior for plain WebDAV ("no
modified-time support" outside ownCloud/Nextcloud/Fastmail). **Do not switch
the remote to `vendor=owncloud` to chase modtime writes** — it changes
rclone's upload-protocol expectations (chunked endpoints the server doesn't
serve) and risks breaking large PUTs. A live mount such as Y: is unaffected: reads
always reflect server state and writes land directly.

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `401` with token | Truncated token (e.g. only the first characters pasted) | Use full `AINGEL_DAV_TOKEN` |
| `403 Forbidden: Path must be under Working Documents` | Writing outside `Working Documents` | Only `Working Documents` (and its subfolders) is writable via WebDAV |
| Windows `0x80070043` / `67` after login | `AuthForwardServerList` not set or `net use https://` form | Set registry + `net stop/start WebClient` as above, or use `\\...@SSL\...` / `rclone` |
| `rclone mount: symlinks not supported without --links` | The projects root contains symlink-like entries | Add `--links` to `rclone mount` |
| `rclone` service `Queued` not `Running` | `AtLogOn` + `Interactive` principal | Check the task launches `wscript.exe` + the VBS (recipe above); the VBS's `True` wait makes the task mirror rclone |
| Empty console window after logon | Task launches `rclone.exe` directly (`--no-console` only clears the text) | Point the task at `wscript.exe` + hidden VBS (recipe above) |
| `Register-ScheduledTask ... -Force` → `Access is denied` | Existing task registered from an elevated context | Unregister (`Unregister-ScheduledTask RcloneCordee`) then register from an elevated shell |
| `Y:` not visible after `rclone mount` as admin | Admin session vs user session | Run mount from regular `cmd`, not `C:\Windows\System32` admin; verify with Explorer, not an elevated `Test-Path Y:\` |

Check server logs: `journalctl -u cordee-webdav -n 50`, `journalctl -u cordee -n 30`. Check Windows `rclone` log: `%USERPROFILE%\rclone.log`.

## Security

* Token is shared — rotate by changing `AINGEL_DAV_TOKEN` in `.env` and `sudo systemctl restart cordee cordee-webdav`; re-run `rclone config update cordee pass <NEW> --obscure` on each client.
* Writable only under `Working Documents`; reads block `.db`/`.env`/`.git`. `DavWWWRoot` stripping does not bypass this.
* Proxies may cap request time (Cloudflare: 100 s) for large `PUT`s — use in-app chunked upload for >50 MB files; WebDAV is best for docs.
