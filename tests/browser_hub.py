"""Real Chromium + FastAPI integration for the signed-in study HUD.

Run from the project root in a development environment with both role requirements
and Playwright Chromium: python tests/browser_hub.py --screenshot progress.png
Cloud speech uses fixtures. SQLite, auth, HTTP, WebSockets, PDF parsing and WebGL run live.
"""
from __future__ import annotations

import argparse
from array import array
import asyncio
from contextlib import ExitStack
import io
import json
import math
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def require(value, message):
    if not value:
        raise AssertionError(message)


def passed(component, start):
    print(f"[PASS] {component} ({(time.perf_counter() - start) * 1000:.0f} ms)", flush=True)


def microphone_fixture(path):
    rate = 16000
    samples = array("h")
    for n in range(6 * rate):
        t = n / rate
        voiced = .25 < t < 3.2
        envelope = .72 + .27 * math.sin(2 * math.pi * 3.3 * t)
        vowel = math.sin(2*math.pi*227*t) + .38*math.sin(2*math.pi*594*t)
        samples.append(int(11000 * envelope * vowel) if voiced else 0)
    if sys.byteorder != "little":
        samples.byteswap()
    with wave.open(str(path), "wb") as stream:
        stream.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        stream.writeframes(samples.tobytes())


def study_pdf():
    from reportlab.pdfgen import canvas
    out = io.BytesIO()
    doc = canvas.Canvas(out)
    doc.setTitle("Laboratory study guide")
    for heading, text in (("Unit 1 Capacitors", "A capacitor stores energy in an electric field."),
                          ("Unit 2 Inductors", "An inductor stores energy in a magnetic field.")):
        doc.drawString(75, 750, heading)
        doc.drawString(75, 718, text)
        doc.showPage()
    doc.save()
    return out.getvalue()


async def synthetic_speech(text):
    # Silent MPEG-1 Layer III frames, 32 kbps, 44.1 kHz mono, zero side information.
    frame = bytes.fromhex("fffb10c4") + bytes(100)
    yield frame * 20


async def run(screenshot, executable):
    import uvicorn
    from playwright.async_api import async_playwright, expect
    with tempfile.TemporaryDirectory(prefix="argus-browser-hub-") as temporary, ExitStack() as stack:
        scratch = Path(temporary)
        listener = stack.enter_context(socket.socket())
        listener.bind(("127.0.0.1", 0))
        origin = "http://127.0.0.1:" + str(listener.getsockname()[1])
        fixtures = {name: "" for name in ("GROQ_API_KEY", "GEMINI_API_KEY", "TAVILY_API_KEY",
                    "ELEVENLABS_API_KEY", "NTFY_RX_URL", "NTFY_TX_URL", "NTFY_TOPIC", "ARGUS_PIN_HASH")}
        stack.enter_context(patch.dict(os.environ, {**fixtures, "ARGUS_ORIGIN": origin,
            "ARGUS_DB_PATH": str(scratch / "profile.db"), "ARGUS_DATA_DIR": str(scratch / "data"),
            "ARGUS_PIN": "0619", "ARGUS_SESSION_SECRET": "browser-test-only-disposable-secret-32"}))
        stack.enter_context(patch("dotenv.load_dotenv", return_value=False))
        import server
        # The browser must post captured PCM through the real one-use challenge before PIN login.
        transcription = stack.enter_context(patch.object(server, "transcribe_pcm", AsyncMock(return_value=server.PHRASE)))
        stack.enter_context(patch.object(server, "stream_speech", synthetic_speech))
        stack.enter_context(patch.object(server.tools_hub, "_json",
                            AsyncMock(side_effect=server.tools_hub.ArgusToolError("Offline fixture"))))
        stack.enter_context(patch.object(server.tools_hub, "grounded_search", side_effect=AssertionError("Unexpected web search")))
        service = uvicorn.Server(uvicorn.Config(server.app, lifespan="off", log_level="error", access_log=False))
        serve = asyncio.create_task(service.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(15):
                while not service.started:
                    if serve.done():
                        await serve
                        raise RuntimeError("Uvicorn stopped before accepting connections")
                    await asyncio.sleep(.03)
            wav = scratch / "synthetic-microphone.wav"
            microphone_fixture(wav)
            async with async_playwright() as playwright:
                args = ["--disable-breakpad", "--disable-crash-reporter",
                        "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
                        "--use-file-for-fake-audio-capture=" + str(wav), "--autoplay-policy=no-user-gesture-required",
                        "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--disable-background-networking"]
                browser = await playwright.chromium.launch(executable_path=str(executable) if executable else None,
                                                           headless=True, args=args)
                async with browser:
                    context = await browser.new_context(viewport={"width": 1366, "height": 864},
                        device_scale_factor=1, permissions=["microphone"], service_workers="block")
                    async def local_requests(route):
                        if urlsplit(route.request.url).hostname == "127.0.0.1":
                            await route.continue_()
                        else:
                            await route.abort()
                    await context.route("**/*", local_requests)
                    # Simulate the login instruction sound only. Actual microphone AudioWorklet,
                    # VAD, STT upload, PIN form, audio playback and HUD socket are untouched.
                    await context.add_init_script("Object.defineProperty(window,'speechSynthesis',{value:null});")
                    page = await context.new_page()
                    page.set_default_timeout(25000)
                    inbound = []
                    errors = []
                    page.on("pageerror", lambda exc: errors.append(str(exc)))
                    def watch_socket(ws):
                        def record(frame):
                            if isinstance(frame, str):
                                inbound.append(json.loads(frame))
                        ws.on("framereceived", record)
                    page.on("websocket", watch_socket)
                    await page.goto(origin, wait_until="domcontentloaded")
                    start = time.perf_counter()
                    require((await context.request.get(origin + "/api/health")).status == 401,
                            "Protected route was available before login")
                    await page.locator("#phrase-button").click()
                    await expect(page.locator("#pin")).to_be_visible(timeout=45000)
                    pcm = transcription.await_args.args[0]
                    require(len(pcm) >= 3200 and len(set(pcm)) > 8, "No varying microphone waveform reached the STT fixture")
                    await page.locator("#pin").fill("0619")
                    await page.locator("#pin-stage [type=submit]").click()
                    await expect(page.locator("#connection")).to_have_text("CONNECTED")
                    require((await context.request.get(origin + "/api/health")).status == 200, "Session cookie failed")
                    await expect(page.locator("#mic-toggle")).to_have_attribute("aria-pressed", "true")
                    await page.locator("#mic-toggle").click()
                    passed("Real phrase microphone capture, server verification, PIN and protected HUD socket", start)

                    start = time.perf_counter()
                    async with page.expect_response(lambda r:r.url == origin + "/api/material") as uploaded:
                        await page.locator("#pdf-upload").set_input_files({"name":"circuits.pdf",
                                  "mimeType":"application/pdf", "buffer":study_pdf()})
                    receipt = await (await uploaded.value).json()
                    require(receipt.get("page_count") == 2 and receipt["title"] == "Laboratory study guide",
                            "Uploaded PDF title/count mismatch")
                    await expect(page.locator("html")).to_have_attribute("data-mode","study")
                    await expect(page.locator("#study-progress-text")).to_contain_text("0 covered")
                    require(any(m.get("type") == "mode" and m.get("mode") == "study" for m in inbound),
                            "Study Mode did not reach Chromium over WebSocket")
                    passed("Authenticated PDF upload, two indexed topics and study palette", start)

                    async def command(text):
                        box = page.locator("#command")
                        await box.fill(text)
                        await box.press("Enter")
                    start = time.perf_counter()
                    await command("Read page 1")
                    await expect(page.locator("#study-progress-text")).to_contain_text("1 covered")
                    await expect(page.locator("#study-progress-text")).to_contain_text("1 remaining")
                    require(server.db.study_coverage([receipt["id"]])["covered_percent"] == 50, "DB coverage differs")
                    await page.evaluate("""() => {
                        window.studyMorphSamples = [];
                        const sample = () => {
                            const value = window.ARGUS?.material.uniforms.uPieMorph.value;
                            if (value === undefined) return;
                            window.studyMorphSamples.push(value);
                            if (value < .99 && window.studyMorphSamples.length < 3000) requestAnimationFrame(sample);
                        };
                        sample();
                    }""")
                    await command("How much progress have we made?")
                    await expect(page.locator(".progress-pie")).to_be_visible()
                    sector = page.locator(".progress-pie svg [aria-label^='Remaining']")
                    await expect(sector).to_have_count(1)
                    await sector.focus()
                    await sector.press("Enter")
                    await expect(page.locator(".progress-pie .chart-detail")).to_contain_text("50%")
                    await expect(page.locator(".progress-pie li")).to_contain_text("Inductors")
                    require(any(m.get("type")=="visual" and m.get("kind")=="pie" and
                                m.get("values")==[1,1] for m in inbound), "Missing backend pie event")
                    passed("Study chat changed SQLite coverage and selected the remaining topic in the live chart", start)
                    start = time.perf_counter()
                    await page.wait_for_function("window.ARGUS?.material?.uniforms?.uPieMorph?.value >= .99")
                    graphics = await page.evaluate("""() => {
                        const g = window.ARGUS, u = g.material.uniforms;
                        return {vertices:g.points.geometry.attributes.piePosition.count,
                            fraction:u.uCoveredFraction.value, selected:u.uSelectedSector.value,
                            center:u.uPieCenter.value.x, radius:u.uPieRadius.value.x,
                            animated:window.studyMorphSamples.some(t=>t>.01 && t<.98),
                            version:g.renderer.getContext().getParameter(g.renderer.getContext().VERSION)};
                    }""")
                    require(graphics["vertices"] == 36000 and graphics["fraction"] == .5 and
                            graphics["selected"] == 1 and graphics["center"] > 0 and graphics["radius"] > 0 and
                            graphics["animated"] and "WebGL" in graphics["version"], f"Incorrect particle state: {graphics}")
                    await expect(page.locator("#state")).to_have_text("LISTENING", timeout=25000)
                    if screenshot:
                        screenshot.parent.mkdir(parents=True,exist_ok=True)
                        await page.screenshot(path=str(screenshot))
                    require(not errors, "Uncaught HUD errors: " + ", ".join(errors))
                    passed("Chromium completed the WebGL transition into the interactive progress pie", start)
                    start = time.perf_counter()
                    await command("Normal mode")
                    await expect(page.locator("html")).to_have_attribute("data-mode", "normal")
                    await expect(page.locator(".progress-pie")).to_be_hidden()
                    async with page.expect_response(lambda r:r.url == origin + "/auth/logout") as logged_out:
                        await page.locator("#lock").click()
                    require((await logged_out.value).ok, "Server rejected logout request")
                    await expect(page.locator("#lock-screen")).to_be_visible()
                    await expect(page.locator("#auth-status")).to_contain_text("Locked")
                    require((await context.request.get(origin + "/api/health")).status == 401,
                            "The former HUD session continued to authorize requests after lock")
                    passed("Exit to Normal Mode, collapse progress chart and revoke authenticated session", start)
        finally:
            service.should_exit = True
            await asyncio.wait_for(serve, 15)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--screenshot", type=Path, help="Optional PNG of the signed-in progress HUD")
    parser.add_argument("--executable", type=Path, help="Use an installed Chromium/Chrome/Edge executable")
    cli = parser.parse_args()
    try:
        asyncio.run(run(cli.screenshot, cli.executable))
    except Exception as exc:
        print(f"[FAIL] {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
