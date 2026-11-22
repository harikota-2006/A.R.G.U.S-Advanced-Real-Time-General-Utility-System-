# Verification

The benchmark distinguishes deterministic provider fixtures, actual local file parsing, and live Windows checks. It prints a status, duration and failure details per case, and exits nonzero on a failed assertion. Latencies from mocked providers measure local routing overhead. Device or cloud service latencies must be measured on the deployed connection.

## Tablet suite

Run from the extracted hub directory in Termux. The optional health probe needs a hub already running in a separate session.

```sh
cd "$HOME/argus-master"
. .venv/bin/activate
python tests/benchmark_argus.py --role tablet --json "$HOME/argus-tablet-results.json"
python tests/benchmark_argus.py --role tablet --live-hub http://localhost:8000
```

Tablet coverage includes ordered spoken phrase and PIN authentication, session and workstation access control, voice turns and interruption, memory and rules, search and synthesis failover, ntfy simulation, local and phone study material indexing, topic progress, PDF-only study presentation dispatch, and Detective Mode launch/state routing. Fixtures exercise failure paths without sending messages to the live phone or spending provider credits.

## Windows suite and live process check

Run from the extracted `argus` directory in PowerShell. This works with only the Windows requirements.

```powershell
.\.venv\Scripts\python.exe tests\benchmark_argus.py --role laptop --json "$HOME\argus-laptop-results.json"
.\.venv\Scripts\python.exe tests\live_workstation.py
```

The live diagnostic opens a local WebSocket listener with a disposable key, authenticates the production daemon client, sends real commands and checks the returned IDs. It launches `System32\whoami.exe` and reopens actual files in a temporary folder. Each returned export must match the saved file byte-for-byte, pass a SHA-256 check and contain the source text. PDF and DOCX drafts must stay unpublished until the explicit save request.

| Observed on the Windows verification host | Result | Elapsed |
|---|---|---:|
| Incorrect daemon bearer rejected with HTTP 401 | PASS | 13.0 ms |
| Accepted connection, protocol hello and required capabilities | PASS | 10.2 ms |
| Windows process creation and PID in the correlated WebSocket receipt | PASS | 62.0 ms |
| PPTX generated, transferred and editable slide text read back | PASS | 514.1 ms |
| PDF prepared, explicitly saved, transferred and text read back | PASS | 876.2 ms |
| DOCX prepared, explicitly saved, transferred and text read back | PASS | 601.3 ms |

To request a visible desktop launch separately, run the following in the ASUS user's signed-in session and observe Paint. The receipt accurately records dispatch acceptance.

```powershell
.\.venv\Scripts\python.exe tests\live_workstation.py --target paint
```

## Whole-project verification and real browser test

Use a Windows development environment with **both** requirement sets for these commands. Only this development environment needs the additional browser test tooling. The desktop test starts a local fixture and uses Chromium with WebGL to exercise the study progress visualization. The role-specific production installations can stay small.

```powershell
py -3 -m venv .venv-test
.\.venv-test\Scripts\python.exe -m pip install -r requirements-termux.txt -r requirements-windows.txt
.\.venv-test\Scripts\python.exe -m pip install playwright
.\.venv-test\Scripts\python.exe -m playwright install chromium
.\.venv-test\Scripts\python.exe verify.py
.\.venv-test\Scripts\python.exe tests\benchmark_argus.py --role all --browser --json "$HOME\argus-full-results.json"
```

With Node.js on PATH, `verify.py` also runs the JavaScript contract checks. The regular benchmark uses synthetic cloud responses plus real SQLite databases, document readers, Office packages and local WebSocket transport. Its PDF, DOCX and PPTX checks validate content and package structure. They do not measure typography in the user's installed Office build.

## Physical-device checks

1. On the Lenovo, allow the microphone at the configured origin, speak the phrase and use the existing PIN. Check that locking the HUD ends its protected session.
2. Start the daemon on the ASUS. In standby, say “Hey Argus, open Paint on my laptop.” Compare the HUD receipt with the application that opens. Turn off the laptop and ask for Detective Mode to exercise the tablet fallback link.
3. With the Lenovo's actual speakers, interrupt a spoken reply and continue in Study Mode without another wake phrase. Say “Normal mode,” then check that unrelated room speech leaves the assistant on standby.
4. Send a known two-topic PDF or PPTX from the OnePlus, discuss one complete topic and request progress. Check the listed source and topic status. A real device can measure movement smoothness and response timing at its own frame rate.
5. Request a sourced report, first decline to save and then explicitly approve a new draft. Open it in the installed desktop application. Subscribe the phone to TX and use its signed export link while connected to the configured hub origin.
6. Set a short timer locally on the Lenovo, then check the Clock app. Live ntfy delivery, third-party quotas, speech accuracy in Hari's room, actual Google Clock behavior, and a particular YouTube video's availability require these device/account checks.

## Optional service checks on the configured tablet

Run `python tests/live_services.py --llm` for actual time to first token and total response time. Use `python tests/live_services.py --video 'PUBLIC_YOUTUBE_URL'` to see which video evidence path succeeds on your account. Run `python tests/live_services.py --notify` to send one real notification. These probes use the tablet's provider configuration and an empty temporary profile. A successful ntfy HTTP response establishes server acceptance. Check the notification on the OnePlus to establish device delivery.

The video probe submits the URL as a video input to the configured model. Availability depends on the model and access to that public video. The probe reports whether it used model observations or public captions. [Google's video input documentation](https://ai.google.dev/gemini-api/docs/video-understanding#youtube)

