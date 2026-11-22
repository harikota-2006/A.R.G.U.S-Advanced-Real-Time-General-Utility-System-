# A.R.G.U.S. master build

Hari's Lenovo tablet runs the FastAPI hub, SQLite WAL profile, speech pipeline and golden Möbius HUD. The Windows daemon makes an outbound authenticated connection to the tablet and performs desktop launches and local document rendering. The OnePlus uses separate ntfy channels for incoming material and outgoing notifications.

## Lenovo tablet in Termux

Save **ARGUS-master.zip** to Android Downloads using Drive, email or a private transfer. The release contains a top-level `argus/` directory with both roles, including `static/` and `tests/`. Transfer the code archive only. The tablet's private configuration and profile stay on the tablet.

Run the following in Termux. The fresh sibling directory keeps your previous installation available for rollback. If `~/argus-master` already exists, choose a fresh release directory before continuing.

```sh
termux-setup-storage
pkg update
pkg install python python-pip python-numpy python-lxml python-cryptography rust clang make cmake ninja pkg-config libffi openssl unzip nano nodejs-lts
(
  set -eu
  test ! -e "$HOME/argus-master"
  tmp_release=$(mktemp -d "$HOME/.argus-release.XXXXXXXX")
  unzip -q "$HOME/storage/downloads/ARGUS-master.zip" -d "$tmp_release"
  test -f "$tmp_release/argus/server.py"
  mv "$tmp_release/argus" "$HOME/argus-master"
  rmdir "$tmp_release"
) &&
cd "$HOME/argus-master" &&
python -m venv --system-site-packages .venv &&
. .venv/bin/activate &&
python -m pip install -r requirements-termux.txt
```

Python 3.11 or newer is required. The native toolchain is for Android builds of transitive packages. The virtual environment sees Termux's prebuilt NumPy, lxml and cryptography packages. Node.js runs the JavaScript checks. Keep the PWA files together, including `manifest.json`, `static/vendor/`, icons, the PCM worklet and service worker. [Termux Python package](https://github.com/termux/termux-packages/blob/master/packages/python/build.sh) and [Termux lxml package](https://github.com/termux/termux-packages/blob/master/packages/python-lxml/build.sh).

### Existing tablet profile

Stop the old hub with Ctrl+C in its Termux session. From the newly extracted release, with the virtual environment active, run this command **before setup or the first server start**.

```sh
python migrate_profile.py ~/argus
```

The utility makes a consistent SQLite backup, checks its integrity, copies the private configuration and custom skills, and sets the material library to its existing absolute path. This preserves links to previously uploaded study materials and exports. Keep that library on the tablet. The new database defaults to `~/argus-master/argus_user_profile.db`. Existing destination profile files cause the migration to stop. The old database remains available for rollback.

If a shell startup file exports `ARGUS_DB_PATH` or `ARGUS_DATA_DIR`, unset those overrides in the new hub session after the migration so it reads the new release's `.env`. Resuming the old hub later starts from its original database. Back up the new profile first if you need subsequent conversations.

### Configure and start

For a first installation only, run `cp -n .env.example .env`. Edit the tablet's private `.env`, then run setup. For a legacy configuration kept separately, use `python setup_config.py --legacy-env /path/to/env_for_astra.txt` in place of the setup command. Setup prompts for the HUD origin and PIN as needed, reuses the configured PIN or hash, and generates missing session and daemon keys.

```sh
cd "$HOME/argus-master"
. .venv/bin/activate
nano .env
python setup_config.py
python install_hud_assets.py
python tests/benchmark_argus.py --role tablet
python server.py
```

`install_hud_assets.py` verifies the bundled files against their hashes offline. If it reports damage or missing assets, run `python install_hud_assets.py --repair` with network access to restore them.

Open `http://localhost:8000` in Chrome on the Lenovo, use the displayed microphone control to speak **“I solemnly swear that I'm up to no good.”**, and enter your configured PIN. Use the exact `ARGUS_ORIGIN`. Microphone capture requires localhost or a correctly served HTTPS origin. The server starts a single Uvicorn worker and binds to `TABLET_HOST` and `TABLET_PORT` (defaults `0.0.0.0` and `8000`). Configure at least one supported speech transcription provider for phrase login.

| Tablet settings | Use |
|---|---|
| `ARGUS_ORIGIN`, `ARGUS_PIN_HASH`, `ARGUS_SESSION_SECRET`, `ARGUS_DAEMON_KEY` | Login origin, existing PIN, signed sessions and shared workstation credential. |
| `GROQ_API_KEY`, `GEMINI_API_KEY` | Speech transcription and reasoning. Gemini also handles image/video input and search grounding. |
| `TAVILY_API_KEY`, `ELEVENLABS_API_KEY` | Primary search and speech synthesis. Edge-TTS is a network-based speech fallback. |
| `NTFY_RX_URL`, `NTFY_TX_URL`, `NTFY_TOKEN` | Distinct private topics and optional access token. Publish material to RX and subscribe the phone to TX. |
| `NTFY_ATTACHMENT_HOSTS`, `NTFY_COMMAND_KEY`, `ARGUS_PUBLIC_URL` | Additional trusted attachment hosts, optional separate signing key (32+ characters), and the hub origin reachable **from the phone** for expiring download links. |
| `DETECTIVE_URL`, `SESSION_TIMEOUT_SECONDS`, `FOLLOWUP_SECONDS` | Optional dashboard override and session/follow-up timing. The packaged example lists supported model and voice overrides. |

## ASUS laptop in PowerShell

Use the same ZIP with Python 3.11 or newer. Use an empty installation folder. The commands deliberately stop at an existing destination.

```powershell
$release = Join-Path $HOME 'ARGUS-master'
if (Test-Path -LiteralPath $release) { throw "Use a fresh ARGUS-master destination." }
Expand-Archive -LiteralPath (Join-Path $HOME 'Downloads/ARGUS-master.zip') -DestinationPath $release
Set-Location (Join-Path $release 'argus')
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-windows.txt
.\.venv\Scripts\python.exe setup_config.py --laptop
.\.venv\Scripts\python.exe tests\benchmark_argus.py --role laptop
.\.venv\Scripts\python.exe tests\live_workstation.py
.\.venv\Scripts\python.exe laptop_daemon.py
```

Enter the Lenovo's private address, for example `ws://192.168.1.42:8000/ws/workstation`, with the Lenovo's actual IP or Tailscale address. Use `wss://` when deploying through a TLS hostname. Supply the **same** `ARGUS_DAEMON_KEY` as the tablet. These and `ARGUS_HUB_WS` are the only credentials/connection settings required on Windows. Do not transfer the tablet's provider keys or PIN hash to the laptop.

For a minimal Windows runtime, keep `laptop_daemon.py`, `report_renderer.py`, `requirements-windows.txt`, and the laptop's private `.env`. Keep `setup_config.py` for interactive setup. Keep the extracted release for the diagnostic commands above. `workstation_daemon.py` is an alternate entry point that imports `laptop_daemon.py`. Start either launcher in your signed-in Windows desktop session to show apps on that desktop.

The optional Windows settings are `ARGUS_DOCUMENTS` (default `~/Documents/ARGUS/documents`), `ARGUS_WORKSPACE` (default `~/Documents/ARGUS/workspace`) and `ARGUS_APP_PATHS` (JSON mapping of lowercase app names to full executable paths). Generated documents receive unique names. A verified report draft is saved into the output folder only after confirmation.

## Phone and daily use

Publish supported files to RX or use the HUD upload picker. To send signed commands from Termux on the phone, copy `phone_commands.py` and `config.py` together, install `httpx` and `python-dotenv`, and create a phone-specific `.env` with `NTFY_RX_URL`, `NTFY_COMMAND_KEY` (same separate key as the hub), and `NTFY_TOKEN` when required by the topic. Run `python phone_commands.py "study mode"`. The hub checks the signature, command allowlist, timestamp, replay state and unlocked HUD session. Ordinary notes enter the inbox.

- Say “Hey Argus” in standby, then continue conversationally. “Study mode” starts wake-free discussion. “My turn, let me explain” gives you the floor. “Check my answer” requests source-backed feedback.
- Upload a PDF or PowerPoint, then ask “How much progress have we made?” to inspect covered and remaining topics. Say “Close chart” to dismiss the visualization. “Normal mode” returns to standby behavior.
- “Detective mode” opens the recovered Osiris dashboard through the workstation when available. If a tablet browser blocks the automatic launch, tap the dashboard link on the HUD.
- Ask for a presentation, a PDF report, or a Word report. For a video, include the full YouTube URL. The tablet gathers evidence and the laptop generates the file. The HUD download card and the optional signed phone link refer to a server-verified export.

See [source comparison](SOURCE_COMPARISON.md), [changes and limits](UPGRADE_NOTES.md), and [verification commands and results](VERIFICATION.md).

