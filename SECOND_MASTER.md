# Second master extension

The original golden dump and ARGUS-master.zip remain available. ARGUS-premium.zip contains this extension. Use the existing README migration commands with the new ZIP filename.

## Documents

PowerPoint now supports native editable category charts with embedded data and a takeaway layout. Existing tables, diagrams, citations, flowing PDF paragraphs, page numbers and verified DOCX/PPTX/PDF delivery remain. The planner requests narrative progression and evidence-backed numeric visuals. Generated files still need human review for dense layouts and academic accuracy. Slide artwork from supplied images is pending.

Public design research used Anthropic's descriptions of source-to-file workflows, editable deliverables and downloads. Sources are https://support.claude.com/en/articles/12111783-create-and-edit-files-with-claude and https://claude.com/blog/create-files . Comparable visual quality has not been established by a side-by-side benchmark.

## Voice

Authenticated /api/health now reports voice_latency with count, median_ms, p95_ms and worst_ms. The browser measures from local VAD speech_end to playback scheduling or the audio element's playing event. This excludes the silence interval before the VAD endpoint and cannot measure acoustic speaker output. A bounded 500-sample window stores no speech. Current STT remains utterance-based HTTP transcription. LLM tokens and personal TTS stream. True partial STT, semantic endpoint detection, device latency and a one-second audible response remain unverified.

## Public service on EC2

Use a separate instance and fresh provider credentials. Do not copy personal .env, profiles, skills or backups. Install Docker Engine and Compose from your Linux distribution's supported installation method. Point DNS at EC2. Allow ports 80/443, restrict SSH to your administrator address, and keep port 8080 private. Store Docker volumes on encrypted EBS.

From a clean extraction, copy public/public.env.example to public/public.env, set PUBLIC_HOST, PUBLIC_ORIGIN and provider keys, then run the following.

```sh
chmod 600 public/public.env
docker compose -f public/compose.yaml build
docker compose -f public/compose.yaml up -d
docker compose -f public/compose.yaml logs --tail 100
```

Caddy handles HTTPS. The app runs as UID 10001 with a read-only root, dropped capabilities, bounded memory/CPU/process count, and a named data volume. Each anonymous session has a 24-hour HttpOnly cookie and a separate profile/material/export directory. File, storage, request, process and queued-job limits apply. Jobs persist in SQLite and interrupted running jobs are requeued after restart. Use one application worker because rate limits and locks are process-local.

The public UI mounts educational endpoints and an explicit asset allowlist. Its worker only authorizes document RPC operations and ignores the private .env. Downloads require the owning browser session. Accounts and indefinite retention are pending. Important questions persist within the session and their list restores on reconnect. Public speech uses utterance capture and buffered synthesis and needs further refinement to reach the personal streaming experience.

## Checks

## Optional startup

On Termux, install and open Termux:Boot, copy deployment/termux-boot.sh to ~/.termux/boot/argus, and run chmod 700 ~/.termux/boot/argus. Update the directory in the script if your installed hub uses a different path. On Windows, run deployment/install-windows-startup.ps1 from PowerShell after creating the virtual environment. This registers a logon task in the desktop session. Android reboot and scheduled-task registration were not executed here.

Run python tests/benchmark_argus.py --role all --browser for the existing personal browser and combined regression suite. Run python tests/browser_public.py separately for the public browser. Both require Playwright Chromium for browser checks. Public unit tests run with python -m unittest discover -s tests -p test_public.py. See SECOND_TEST_REPORT.md for actual outcomes and deployment gaps.

Docker/EC2 startup, HTTPS issuance, real provider quotas, Android/OnePlus behavior and physical microphone latency require their respective environments. The public renderer is isolated in a bounded subprocess within the application container. It is not a separate container per tenant.
