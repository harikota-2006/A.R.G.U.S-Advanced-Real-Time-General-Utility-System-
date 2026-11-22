# Master build changelog

## Hub and authentication

- `auth.py`, `server.py`, `setup_config.py`, `.env.example` and the HUD unlock form implement the two gates. Login starts with a short-lived challenge. The hub transcribes the submitted audio, checks the expected words, then verifies the existing PIN. The actual PIN length is preserved. Origin checking, signed sessions, expiry, retry limits and the separate workstation key remain in force.
- `server.py`, `session_manager.py`, `voice_engine.py` and the PCM worklet coordinate passive wake, active follow-ups, wake-free Study Mode, playback completion, echo handling and interruption. Clock requests execute on the Lenovo. Uploaded study materials, remote alerts, and verified document completions can produce HUD and voice events.
- `tools_hub.py`, `server.py`, `phone_commands.py` and `delivery.py` connect the authenticated workstation to the tablet HUD and the private phone topics. The subscriber reconnects, signed commands require authorization, and exported downloads carry validated file metadata.

## Golden HUD and study

- `static/index.html`, `static/style.css`, `static/visualizer.js`, `static/hud.css`, `static/hud.js`, `static/sw.js` and `static/vendor/` implement the golden rendering, telemetry, study controls, evidence panel, mode colors, and downloadable export cards. The shared renderer reacts to both microphone and playback activity.
- `study_materials.py`, `database.py` and `tools_hub.py` index source units into SQLite. The tracker uses headings where available and physical pages, slides or text sections otherwise. It groups evidence by source and unit. Remaining material stays in the denominator, including pages without extractable text. The result includes totals, percentages and per-topic status for the interactive chart.
- Study turns support quiet listening, follow-up questions, feedback against source excerpts, and a one-question web permission gate for missing material. The earlier WAL profile, rule export, episodic/semantic memory, dream consolidation, provider routing and local clock integrations remain part of the tablet runtime.

## Windows documents and research

- `laptop_daemon.py` resolves apps, Windows paths, files and URLs, emits correlated running/completion messages, and distinguishes process creation or shell acceptance from GUI verification. Common user folders and configured output folders support basename lookup. Ambiguous filenames request a full path.
- `reports.py` prepares source-bounded academic or investigative reports. The video path tries the configured Gemini model with video input and falls back to available public captions. An unusable source produces an explicit error. A report draft is prepared before the user decides whether to file it.
- `report_renderer.py` and `laptop_daemon.py` render actual PDFs, presentation decks, general Word reports and IEEE-style Word documents on Windows. Source-backed tables and diagram data can produce editable Office elements. Verified files are transferred by authenticated RPC. Temporary report drafts are addressed by opaque IDs. Repeated filenames receive a numeric suffix.
- `requirements-termux.txt` includes `youtube-transcript-api` for caption retrieval. The current hub uses speech authentication and does not require the older WebAuthn package. `requirements-windows.txt` includes `reportlab` and `pypdf` for PDF creation and post-generation inspection, in addition to the existing Office renderers.

## Dependency commands

The role-specific requirements files are authoritative, including version bounds. From the project root with the appropriate virtual environment active:

```sh
# Lenovo, Termux
python -m pip install -r requirements-termux.txt
```

```powershell
# ASUS, Windows
.\.venv\Scripts\python.exe -m pip install -r requirements-windows.txt
```

Relative to the earlier upgraded role-specific requirements, the new direct Python packages can also be installed independently:

```sh
python -m pip install 'youtube-transcript-api>=1.2,<2'
```

```powershell
.\.venv\Scripts\python.exe -m pip install 'reportlab>=4,<6' 'pypdf>=5,<8'
```

The new report and study functionality introduces no extra Termux native rendering system package. PowerPoint and PDF rendering run on Windows. The tablet installation instructions include the Android compilation toolchain and ZIP/configuration utilities. The optional Chromium verification has its own development dependencies. See [verification](VERIFICATION.md).

## Limits that matter in use

- Coverage measures discussed, indexed material. It cannot establish mastery or assign an exam grade independently of the cited evidence. Image-only pages/slides may need OCR or explicit review. The source matching and the model's feedback can miss a synonym or misjudge an explanation.
- A supported source format, valid credentials and provider/model availability determine which research and voice paths can run. Several cloud services, including fallback speech and search, need a network connection. A public or captioned YouTube video can still be unavailable to the configured account or region. The spoken login phrase verifies the words. It provides no speaker identification.
- Device microphones, speaker volume, ambient speech, Android audio processing and provider latency affect turn timing. A local interrupt can stop queued playback promptly. It cannot retract sound that the speaker has already played.
- PDF/document indexing accepts bounded, text-readable material. The workstation transfer limit is 12 MiB. A larger verified document can still be saved on the laptop. A phone needs a reachable `ARGUS_PUBLIC_URL` to use a signed download link.
- Run the daemon in the intended signed-in desktop session. An execution receipt reports OS acceptance and any returned PID. The current Windows user's privileges apply to authorized code execution. A workspace directory and job timeout do not isolate filesystem or network access.
- A Word document's two-column conference style may require adjustments for a particular publication. Long reports use bounded source context. Sources and timestamps should be checked when academic accuracy matters.

