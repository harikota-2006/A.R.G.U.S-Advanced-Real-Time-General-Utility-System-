# A.R.G.U.S.
**Advanced Real-Time General Utility System**

A.R.G.U.S. is a personal, multi-device AI assistant built around an Android-hosted hub, an authenticated Windows workstation agent, and a phone notification channel. It brings voice interaction, study-material analysis, research, device actions, and document generation into one interface.

The system is designed to keep the central conversation and personal profile on the tablet while delegating desktop-specific actions to a connected Windows laptop. A browser-based heads-up display (HUD) provides authentication, status, study controls, source evidence, and export downloads.

> **Project status:** Active development. The repository contains implemented functionality and automated tests, but live speech reliability, study feedback, hardware integration, document quality, and provider availability remain dependent on the deployment environment. See [Verification](VERIFICATION.md), [Upgrade Notes](UPGRADE_NOTES.md), and [Second Master Test Report](SECOND_TEST_REPORT.md) for what has and has not been tested.

## Contents

- [Overview](#overview)
- [Features](#features)
- [Architecture](#architecture)
- [Technology stack](#technology-stack)
- [Getting started](#getting-started)
- [Android tablet setup](#android-tablet-setup)
- [Windows laptop setup](#windows-laptop-setup)
- [Phone integration](#phone-integration)
- [Using ARGUS](#using-argus)
- [Optional startup and public deployment](#optional-startup-and-public-deployment)
- [Verification and diagnostics](#verification-and-diagnostics)
- [Security and privacy](#security-and-privacy)
- [Known limitations](#known-limitations)
- [Repository documentation](#repository-documentation)
- [Third-party notices](#third-party-notices)

## Overview

ARGUS separates its responsibilities across three cooperating components:

| Component | Responsibility |
| --- | --- |
| **Android tablet / Termux** | FastAPI hub, speech and reasoning orchestration, authenticated HUD, local profile and study-material index, session state, and workstation/notification routing |
| **Windows laptop** | Outbound authenticated daemon connection, desktop application and file launching, local document rendering, and verified export transfer |
| **Phone** | Private ntfy channels for inbound material and outbound alerts, with optional signed commands |

The personal installation uses an Android tablet as the primary host. The documented Windows workstation role is distinct from the hub: it does not need the tablet's model-provider credentials or PIN hash.

### Design principles

- **Separation of roles:** Personal context and orchestration live on the tablet; Windows-specific execution stays on Windows.
- **Explicit authorization:** Spoken phrase and PIN unlock the HUD; the workstation uses a separate authentication key; phone commands may be signed.
- **Source-aware study and reporting:** Uploaded material is indexed and cited where extractable evidence is available.
- **Verifiable artifacts:** The document workflow checks generated exports instead of treating an attempted file write as a successful deliverable.
- **Recoverable installation:** Releases can be installed beside an existing profile and migrated without deleting the previous installation.

## Features

### Voice and HUD

- Voice-first conversation with a standby wake phrase and active follow-up handling.
- Server-side transcription of the login phrase, followed by PIN authentication.
- Browser HUD with a Three.js particle-based Möbius visualization, system telemetry, mode indicators, transcript and response panels, and export cards.
- Playback interruption and conversational turn handling, including a wake-free Study Mode.
- ElevenLabs speech synthesis with network-based Edge-TTS fallback, subject to service access and availability.

### Study Mode

- Upload and index supported PDFs, presentations, and text-based study resources.
- Discuss material, explain answers aloud, request feedback, and ask questions grounded in available source excerpts.
- Track covered and remaining pages, slides, or sections, with a progress visualization.
- Ask permission before web lookup when the supplied material does not cover a question.

Progress reflects indexed and discussed material; **it is not a verified measure of subject mastery**. Scanned or image-only pages may require separate review.

### Detective Mode and research

- Switch to an investigation-oriented HUD state and access the configured [Osiris dashboard](https://osirisai.live/).
- Route the dashboard launch to a connected workstation when possible; otherwise provide a tablet-accessible link.
- Prepare source-bounded research and report drafts using supplied materials, search results, or supported video evidence paths.
- Use configured Gemini video input or available public captions for eligible YouTube material. Unsupported or inaccessible evidence must produce a failure rather than a fabricated report.

### Workstation and documents

- Open supported desktop applications, local files, directories, and URLs using the authenticated Windows daemon.
- Distinguish an accepted launch request from a visually confirmed application launch.
- Create PDF, DOCX, and editable PPTX exports on Windows, including supported tables, diagrams, and editable category charts.
- Inspect generated file content and transfer verified exports to the tablet.
- Require explicit confirmation before filing supported report drafts.
- Use configured document and workspace folders; repeated output filenames receive distinct names.

Document generation depends on the **Windows renderer and an active daemon connection**. Output layout, citations, and academic suitability still require human review.

### Phone notifications and commands

- Receive supported materials through an inbound ntfy topic and send outgoing notifications through a separate topic.
- Optionally send signed commands from Termux on the phone.
- Deliver expiring export links when the hub has a phone-reachable public or private-network address.

## Architecture

```text
                         +---------------------------+
                         |  Phone / ntfy             |
                         |  Inbound material (RX)    |
                         |  Outbound alerts (TX)     |
                         +-------------+-------------+
                                       |
                              ntfy / signed commands
                                       |
+----------------------------------------------------------------+
| Android tablet — Termux                                        |
|                                                                |
|  FastAPI hub + authenticated browser HUD                       |
|  Voice / model providers / search / session management         |
|  SQLite WAL profile + study material indexing + research      |
|  Evidence / progress / exports / notification routing          |
+-------------------------------+--------------------------------+
                                |
                    Authenticated WebSocket RPC
                                |
+-------------------------------v--------------------------------+
| Windows laptop                                                 |
|                                                                |
|  laptop_daemon.py                                              |
|  Desktop launches + file resolution + document renderer        |
|  Verified PPTX / PDF / DOCX outputs                            |
+----------------------------------------------------------------+
```

The workstation makes an **outbound** WebSocket connection to the hub at `/ws/workstation`. For use beyond one local network, configure a trusted private address such as Tailscale, or a correctly secured TLS endpoint. Do not expose an unprotected development server directly to the internet.

## Technology stack

| Area | Technologies and components |
| --- | --- |
| Hub and APIs | Python, FastAPI, Uvicorn, WebSockets |
| Android environment | Termux, Python virtual environment |
| Local persistence | SQLite with WAL mode |
| Web interface | HTML, CSS, JavaScript, Three.js, PWA assets and service worker |
| Speech | Configured Groq/Gemini transcription paths, ElevenLabs, Edge-TTS fallback |
| Reasoning and research | Configured Groq/Gemini models, Tavily search, source indexing |
| Workstation | Windows, Python, authenticated WebSocket client |
| Documents | PowerPoint, Word and PDF rendering libraries in `requirements-windows.txt` |
| Notifications | ntfy with separate inbound/outbound topics |
| Optional public service | Docker Compose, Caddy, isolated public ASGI application |

Actual providers and models depend on the local `.env` and service quotas; consult [`.env.example`](.env.example).

## Getting started

**Prerequisites**

- Android tablet with Termux and its storage access enabled.
- Windows laptop with Python **3.11 or newer** for workstation functionality.
- Network connectivity for provider-backed voice, reasoning, search, and notification features.
- Relevant provider credentials and private, distinct ntfy topics for enabled integrations.
- The appropriate project files or a prepared code archive.

The setup below retains the release layout used by the original README: an `ARGUS-master.zip` archive containing an `argus/` directory. **The GitHub repository does not itself include that ZIP file.** If installing directly from a repository checkout, use the checkout's actual directory instead of the archive extraction steps.

Never include `.env`, database profiles, user documents, or provider keys in a code archive or Git commit.

## Android tablet setup

### 1. Prepare a fresh Termux installation

Place `ARGUS-master.zip` in Android Downloads. The following commands create `~/argus-master` as a fresh installation and deliberately stop if that directory already exists.

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

Python 3.11+ is required. The native toolchain supports Android builds of transitive dependencies. The environment can use Termux's prebuilt NumPy, lxml, and cryptography packages; Node.js is used for JavaScript checks. Keep the PWA assets together, including `manifest.json`, `static/vendor/`, icons, the PCM worklet, and service worker.

For Termux packaging details, see the upstream [Python](https://github.com/termux/termux-packages/blob/master/packages/python/build.sh) and [lxml](https://github.com/termux/termux-packages/blob/master/packages/python-lxml/build.sh) build definitions.

### 2. Migrate an existing profile (optional)

If upgrading from an existing `~/argus` installation, stop the old hub with **Ctrl+C** before migration. From the *new* extracted directory, with its virtual environment active, run:

```sh
python migrate_profile.py ~/argus
```

The migration utility creates a consistent SQLite backup, checks integrity, copies private configuration and custom skills, and preserves the existing absolute material-library path. The new default database is `~/argus-master/argus_user_profile.db`. Existing destination profile files cause migration to stop instead of silently overwriting them.

If your shell exports `ARGUS_DB_PATH` or `ARGUS_DATA_DIR`, unset those overrides in the new hub session after migration so it uses the new release configuration. The previous installation remains available for rollback; back up the new profile before switching back if you need to preserve later conversations.

### 3. Configure and run the hub

On a **first installation**, create the configuration file before editing:

```sh
cd "$HOME/argus-master"
. .venv/bin/activate
cp -n .env.example .env
nano .env
python setup_config.py
python install_hud_assets.py
python tests/benchmark_argus.py --role tablet
python server.py
```

If the configuration was already migrated, **do not replace it** with `.env.example`. For a separate legacy configuration, use `python setup_config.py --legacy-env /path/to/env_for_astra.txt` in place of the normal setup command.

`setup_config.py` configures the HUD origin and PIN as needed, preserves configured PIN material, and generates missing session/workstation secrets. `install_hud_assets.py` verifies bundled assets offline; if damaged or missing, run:

```sh
python install_hud_assets.py --repair
```

The repair command requires network access.

Open `http://localhost:8000` in Chrome **on the tablet** (or use the correctly configured `ARGUS_ORIGIN`). Select the microphone control, say **“I solemnly swear that I'm up to no good.”**, and enter the configured PIN.

A supported transcription provider is needed for spoken login. Browser microphone access requires localhost or a correctly configured HTTPS secure origin. The single-worker server binds to `TABLET_HOST` and `TABLET_PORT` (defaults: `0.0.0.0` and `8000`).

### Tablet configuration reference

| Setting | Purpose |
| --- | --- |
| `ARGUS_ORIGIN`, `ARGUS_PIN_HASH` | HUD origin and PIN verification |
| `ARGUS_SESSION_SECRET`, `ARGUS_DAEMON_KEY` | Signed user sessions and independent workstation authentication |
| `GROQ_API_KEY`, `GEMINI_API_KEY` | Configured speech, reasoning, and supported image/video/search paths |
| `TAVILY_API_KEY`, `ELEVENLABS_API_KEY` | Search and primary text-to-speech |
| `NTFY_RX_URL`, `NTFY_TX_URL`, `NTFY_TOKEN` | Separate incoming/outgoing ntfy topics and optional token |
| `NTFY_ATTACHMENT_HOSTS` | Additional trusted attachment hosts |
| `NTFY_COMMAND_KEY` | Optional separate 32+ character key for signed phone commands |
| `ARGUS_PUBLIC_URL` | Phone-reachable hub origin for expiring export links |
| `DETECTIVE_URL` | Optional investigation dashboard override |
| `SESSION_TIMEOUT_SECONDS`, `FOLLOWUP_SECONDS` | Session and conversational timing |
| `TABLET_HOST`, `TABLET_PORT` | Hub network binding |
| `ARGUS_DB_PATH`, `ARGUS_DATA_DIR` | Optional local storage overrides |

Available model and voice overrides are documented in [`.env.example`](.env.example). Keep all private values out of version control.

## Windows laptop setup

Use the same archive with Python 3.11 or newer. These PowerShell commands intentionally refuse to overwrite an existing destination.

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

During setup, provide the tablet's reachable WebSocket address, for example:

```text
ws://192.168.1.42:8000/ws/workstation
```

Replace the example IP with the tablet's actual private-network or Tailscale address. Use `wss://` when using an appropriately secured TLS hostname. Set `ARGUS_HUB_WS` and provide the **same** `ARGUS_DAEMON_KEY` used by the tablet. **Do not copy the tablet's provider API keys or PIN hash to Windows.**

For a minimal workstation runtime, retain `laptop_daemon.py`, `report_renderer.py`, `requirements-windows.txt`, and the laptop's private `.env`. Keep `setup_config.py` for interactive setup. The complete extracted release is useful for diagnostics. `workstation_daemon.py` provides an alternate entry point that imports `laptop_daemon.py`.

Run the daemon in the intended **signed-in Windows desktop session**, otherwise visible application launches may not behave as expected.

### Windows output and application settings

| Setting | Description |
| --- | --- |
| `ARGUS_HUB_WS` | WebSocket address of the tablet hub |
| `ARGUS_DAEMON_KEY` | Shared workstation authentication key |
| `ARGUS_DOCUMENTS` | Output folder; defaults to `~/Documents/ARGUS/documents` |
| `ARGUS_WORKSPACE` | Working folder; defaults to `~/Documents/ARGUS/workspace` |
| `ARGUS_APP_PATHS` | JSON mapping of lowercase app names to full executable paths |

Generated files receive unique names. Verified report drafts are saved to their destination only after the required confirmation. Workstation receipts indicate command dispatch and relevant process information; they do not always prove that a visible window appeared.

## Phone integration

Use **distinct private ntfy topics**: one for messages and material sent *to* ARGUS (`NTFY_RX_URL`), and another for notifications sent *from* ARGUS (`NTFY_TX_URL`). You can also upload materials directly through the HUD.

To send an authorized command using Termux on the phone:

1. Copy `phone_commands.py` and `config.py` to the phone.
2. Install `httpx` and `python-dotenv` in the phone's Python environment.
3. Create a **phone-specific** `.env` with `NTFY_RX_URL`, `NTFY_COMMAND_KEY` (matching the hub's separate command key), and `NTFY_TOKEN` if required.
4. Run:

```sh
python -m pip install httpx python-dotenv
python phone_commands.py "study mode"
```

The hub validates command signatures, allowed commands, timestamps, replay state, and the unlocked HUD session. Ordinary notes are handled as inbox material. For downloadable notification links, `ARGUS_PUBLIC_URL` must point to a hub address the phone can reach.

## Using ARGUS

| Intent | Example |
| --- | --- |
| Wake the assistant | “Hey Argus” |
| Enter study conversation | “Study mode” |
| Take the floor | “My turn, let me explain” |
| Request source-backed feedback | “Check my answer” |
| Inspect material coverage | “How much progress have we made?” |
| Dismiss the progress display | “Close chart” |
| Leave active study conversation | “Normal mode” |
| Open the investigation dashboard | “Detective mode” |
| Dispatch a workstation action | “Open Paint on my laptop” |
| Request an export | “Create a PowerPoint presentation about this topic” |

Upload a PDF or PowerPoint before requesting source-specific study feedback. Document creation requires the Windows daemon; if the laptop is disconnected, the tablet can still run its own supported hub-side operations.

The voice and study examples describe **intended commands**, not a guarantee of accurate interpretation in every environment.

## Optional startup and public deployment

### Personal-device startup

The repository includes optional startup scripts:

- **Termux:** Install and open Termux:Boot, copy `deployment/termux-boot.sh` to `~/.termux/boot/argus`, update the installation path if necessary, and run `chmod 700 ~/.termux/boot/argus`.
- **Windows:** After creating the laptop virtual environment, run `deployment/install-windows-startup.ps1` in PowerShell to register a logon task in the desktop session.

These startup mechanisms are documented but were **not physically validated at Android reboot or Windows logon** in the published development-host report. See [Second Master](SECOND_MASTER.md).

### Separate public educational service

An **optional**, separate public-facing ASGI application is included under [`public/`](public/). It is not the same deployment as the private personal hub.

The documented deployment uses Docker Compose, Caddy HTTPS, anonymous 24-hour browser sessions, per-session source/profile/export directories, bounded job processing, and a document-only worker allowlist. Follow [Second Master](SECOND_MASTER.md) and review [Second Master Test Report](SECOND_TEST_REPORT.md) before deployment.

On a properly provisioned Linux host with Docker Engine/Compose and DNS configured, create `public/public.env` from `public/public.env.example`, configure `PUBLIC_HOST`, `PUBLIC_ORIGIN`, and relevant provider credentials, then:

```sh
chmod 600 public/public.env
docker compose -f public/compose.yaml build
docker compose -f public/compose.yaml up -d
docker compose -f public/compose.yaml logs --tail 100
```

Deployment requires its own security review. The published report did **not** validate actual Docker/EC2 startup, HTTPS issuance, provider quotas, or production restart behavior. The public renderer uses a bounded subprocess **within the application container**, not a dedicated container for every tenant.

## Verification and diagnostics

The repository provides deterministic benchmark fixtures, real local file/package tests, WebSocket checks, and optional live-device/provider probes.

### Tablet

```sh
cd "$HOME/argus-master"
. .venv/bin/activate
python tests/benchmark_argus.py --role tablet --json "$HOME/argus-tablet-results.json"
```

With the hub running in another Termux session:

```sh
python tests/benchmark_argus.py --role tablet --live-hub http://localhost:8000
```

### Windows

```powershell
.\.venv\Scripts\python.exe tests\benchmark_argus.py --role laptop --json "$HOME\argus-laptop-results.json"
.\.venv\Scripts\python.exe tests\live_workstation.py
```

The live workstation diagnostic performs actual local WebSocket interactions and verifies generated document contents. To separately observe a visible desktop launch:

```powershell
.\.venv\Scripts\python.exe tests\live_workstation.py --target paint
```

### Whole-project and browser checks

On a Windows **development environment** with both requirement sets and Playwright installed:

```powershell
py -3 -m venv .venv-test
.\.venv-test\Scripts\python.exe -m pip install -r requirements-termux.txt -r requirements-windows.txt
.\.venv-test\Scripts\python.exe -m pip install playwright
.\.venv-test\Scripts\python.exe -m playwright install chromium
.\.venv-test\Scripts\python.exe verify.py
.\.venv-test\Scripts\python.exe tests\benchmark_argus.py --role all --browser --json "$HOME\argus-full-results.json"
```

The repository's [Second Master Test Report](SECOND_TEST_REPORT.md) records **133 passed, 0 failed, 0 skipped** in its combined Windows development-host benchmark. This is a report of that execution, **not a claim that the current version has been end-to-end validated on the owner's tablet, phone, and laptop**. Mocked cloud provider interactions do not measure actual API latency, speech behavior, or quotas.

Optional service probes and physical-device checks are documented in [`VERIFICATION.md`](VERIFICATION.md).

## Security and privacy

- The personal HUD uses a transcribed passphrase gate followed by PIN authentication; the phrase is **not speaker biometrics**.
- Signed, expiring sessions and origin validation protect browser workflows.
- The Windows daemon authenticates separately, and phone commands can use a distinct signing key.
- Keep `.env`, API credentials, profiles, backups, uploaded material, and generated documents private.
- Use private networking or correctly configured HTTPS when accessing the hub remotely.
- A workstation daemon acts with the permissions of the signed-in Windows user. A workspace folder and job timeout **do not sandbox its filesystem or network access**.
- Only deploy the public service with its separate configuration; do not copy personal profiles or provider credentials into it.
- Validate sources and review generated academic or investigative documents before sharing them.

## Known limitations

- **Study:** Coverage can miss synonyms or interpret explanations incorrectly; image-only documents may have no extractable text. Progress does not establish mastery.
- **Speech:** Recognition and turn timing depend on microphones, browser audio behavior, ambient noise, network/provider latency, and interruption handling. The documented personal transcription path remains utterance-based, not true streaming partial STT.
- **Reasoning and research:** A configured provider may be unavailable or rate-limited. Search and YouTube understanding depend on external services and accessible evidence.
- **Windows actions:** A successful OS launch receipt may not confirm that the correct desktop window is visible.
- **Documents:** Generated reports, slides, citations, charts, and conference-style layouts need human review. The workstation transfer limit documented for the personal system is 12 MiB.
- **Cross-device delivery:** ntfy server acceptance does not independently prove delivery to the phone; signed export links need a reachable hub.
- **Public service:** Production deployment, long-term accounts and indefinite storage, full feature parity, and multi-worker/shared rate limits are not established by the published tests.

These limitations are part of the engineering record, not hidden behind a release label. For detailed status, see [Upgrade Notes](UPGRADE_NOTES.md) and [Second Master Test Report](SECOND_TEST_REPORT.md).

## Repository documentation

| Document | Purpose |
| --- | --- |
| [`SOURCE_COMPARISON.md`](SOURCE_COMPARISON.md) | Provenance of HUD, Detective Mode, and inherited behavior |
| [`UPGRADE_NOTES.md`](UPGRADE_NOTES.md) | Implementation changes and practical boundaries |
| [`VERIFICATION.md`](VERIFICATION.md) | Commands, physical-device checklist, and measured Windows checks |
| [`SECOND_MASTER.md`](SECOND_MASTER.md) | Premium extension, optional startup, and public-service architecture |
| [`SECOND_TEST_REPORT.md`](SECOND_TEST_REPORT.md) | Executed test results and unverified environments |
| [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) | Bundled dependencies and third-party notices |
| [`.env.example`](.env.example) | Configuration names and defaults |
| [`requirements-termux.txt`](requirements-termux.txt) | Android runtime dependencies |
| [`requirements-windows.txt`](requirements-windows.txt) | Windows workstation dependencies |

## Third-party notices

See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for dependency attribution and notices.

**License:** No top-level `LICENSE` file was present in the repository when this README was revised. Third-party notices are not a substitute for a license covering the project itself; review the intended distribution terms before adding one.
