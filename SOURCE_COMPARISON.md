# Findings from the two sources

## The golden visual system

The golden dump contains the HTML, stylesheet, visualizer and manifest both at its root and under `static/`. Each duplicate pair has matching text. The golden implementation uses **Three.js r128**, a perspective camera and 36,000 point particles sampled on a Möbius surface with radius 3.2 and width 1.2. The vertex shader uses depth, phase and audio turbulence to position and size particles. The fragment shader draws soft circular points. Golden blue is `#00D2FF` to `#0077FE`.

The master carries forward the golden stylesheet, original surface parameterization and rotation, particle shaders, brand header and mode badge, transcript ribbon, response bubble, online toast and the bottom dashboard with workstation, phone, session timer ring, audio and connection telemetry. It serves the PWA manifest from the hub and vendors the Three.js runtime locally. The current `static/index.html` integrates these elements with the new gate and Source A's controls. It differs from the archived HTML where authentication, uploads, transport, progress and downloads require live controls. `static/hud.css` adds the mode and accessibility overrides.

The golden renderer owned its own fixed-port `ws://…:8000/ws/visualizer` socket and WebM recording cycle. Source A already had authenticated WebSockets, continuous PCM input, cancellable turns, TTS playback and local interruption. The master sends state, audio levels, mode and progress into the visualizer from the current HUD client, so a second login or recording loop is unnecessary.

For the requested progress view, the master extends the golden vertex shader with a second position for each of its 36,000 particles. The new positions fill a disk aligned to the interactive chart. The original fragment shader, the Möbius surface and its idle rotation continue to drive the rest of the HUD. Closing the chart reverses the transition.

## Detective Mode provenance

Golden `server.py` recognized the Detective session action. Golden `tools_hub.py:121` posted to the laptop's `/detective-start` endpoint. The visualizer consumed `detective_on` and `detective_off` state changes, switched the mode badge, and interpolated the red shader palette. The golden HTML provided the shared telemetry and response areas. A distinct investigation/evidence desk did not appear in its markup. The master adds this panel to supply the requested cited investigation view.

The dump omits the Windows implementation and the dashboard destination. The separate original `workstation_daemon.py:48` supplies the URL and layer list. Its HTTP endpoint located Chrome and launched the URL with a designated Chrome profile. The master passes that same URL to the user's default handler through authenticated workstation RPC. The tablet receives an actionable link when the laptop cannot dispatch the launch. The current requested crimson shades are `#DC143C` and `#FF1E38`.

```text
https://osirisai.live/?layers=flights,private,jets,military,maritime,satellites,sat_comms,sat_military,sat_navigation,sat_earth,sat_science,cctv,cctv_previews,live_news,earthquakes,fires,weather,global_incidents,day_night,cables,sdk_sea,sdk_air,sdk_naval,terrain_3d,malware,cyber_attacks
```

## Why a connection could appear healthy while a launch failed

The earlier upgraded Source A snapshot already used request IDs and structured `rpc` / `rpc_result` messages on `/ws/workstation`. Its basic daemon resolved a limited alias table, PATH, registry App Paths and a few installation locations. An arbitrary file path was handled as an unknown application in the general open handler. Some shortcuts or per-user applications lay outside those search locations. Its URL path returned `launch_requested` from the browser API, while the hub could say “The workstation launch request completed” when the response had no message. The JSON transport alone did not establish which window, if any, appeared. The working tree at the start of this pass already contained an intermediate restoration with richer receipts and Start Menu handling.

The master opens explicit Windows paths and file associations, checks common document directories for bare filenames, recognizes configured aliases and Start Menu shortcuts, and uses Windows process creation for executables and shell associations for URLs and documents. It fixes blank output/workspace overrides resolving to the process working directory. Result messages distinguish accepted launches, application errors and verified document creation. Both nodes can correlate RPC IDs. A Windows launch receipt includes the dispatch method, target, timestamp and PID when available. Desktop session, file association and application installation still determine what is visible on the user's laptop.

A live Windows loopback test obtained a genuine process ID for a benign system executable. Reproducing the original failure on Hari's desktop requires its request ID, target and ACK/error. The supplied files contain no such device-specific trace. See [verification](VERIFICATION.md).

## Features inherited and changed

| Area | Source evidence and master behavior |
|---|---|
| Authentication | Source A supplied ordered gates, origin checks, retry limits, signed expiring sessions and an independent daemon bearer key. The master verifies audio for the playful phrase on the server and then verifies the existing numeric PIN. The previous camera/WebAuthn and master-password interaction has been retired. |
| Study | Source A provided local PDF extraction, rule/memory storage and grounded Q&A. The intermediate build exposed page progress. The master recognizes topics within sources, uses slides and supported text documents, gives Hari the floor during explanations, and uses cited excerpts when assessing answers. A progress request reports the numerator and denominator and drives the silver visualization. |
| Voice and actions | Existing PCM transport, noise gating, interrupt frames, reasoning/TTS providers, Android-local clock intents, ntfy and search routing stay on the tablet. Source B's duplicate voice/WebSocket clients are historical references. |
| Workstation and exports | The golden HTTP protocol relied on fixed laptop routes. Source A's outbound authenticated RPC and document payloads remain the transport. The master generates and checks editable PPTX, formatted PDF, and DOCX locally, exposes verified exports through the tablet, and gates report filing on explicit approval. |
| Research | Supplied material, phone attachments and the study corpus can form report sources. For a video, Gemini receives the supplied YouTube URL as a video input. Public captions provide the fallback. Missing usable video evidence yields an explicit failure. |

