"""Opt-in Windows loopback diagnostic. Run: python tests/live_workstation.py

Use --target paint to additionally request a visible app on the current desktop.
Uses disposable credentials and generated documents. No personal .env is loaded.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from laptop_daemon import MAX_RPC_MESSAGE_BYTES, Workstation, workstation_connection


def require(value, message):
    if not value:
        raise AssertionError(message)


def passed(label, start):
    print(f"[PASS] {label} ({(time.perf_counter() - start) * 1000:.1f} ms)", flush=True)


async def rpc(ws, method, params):
    request_id = secrets.token_hex(8)
    await ws.send(json.dumps({"type": "rpc", "id": request_id, "method": method, "params": params}))
    running = False
    async with asyncio.timeout(45):
        while True:
            message = json.loads(await ws.recv())
            if message.get("id") != request_id:
                continue
            if message.get("type") == "rpc_status":
                running = running or message.get("status") == "running"
            elif message.get("type") == "rpc_result":
                require(running, f"{method} omitted its running acknowledgment")
                require(message.get("ok"), f"{method} failed: {message.get('error')}")
                return message["result"]


MARKER = "Calibration sequence K17"


def check_export(result, documents, extension):
    path = Path(result["path"]).resolve(strict=True)
    require(path.parent == documents.resolve() and path.suffix == "." + extension, "Output path mismatch")
    require(result["generated"] and result["verified"]["format"] == extension, "Missing verification receipt")
    artifact = result["artifact"]
    raw = path.read_bytes()
    require(raw == base64.b64decode(artifact["data_base64"], validate=True), "Received different export bytes")
    require(hashlib.sha256(raw).hexdigest() == artifact["sha256"], "Received a different checksum")
    if extension == "pdf":
        from pypdf import PdfReader
        with path.open("rb") as stream:
            content = " ".join(page.extract_text() for page in PdfReader(stream, strict=True).pages)
    elif extension == "docx":
        from docx import Document
        content = " ".join(p.text for p in Document(path).paragraphs)
    else:
        from pptx import Presentation
        slides = Presentation(path).slides
        require(len(slides) == 3, "Wrong slide count")
        content = " ".join(s.text for slide in slides for s in slide.shapes if s.has_text_frame)
    require(MARKER in content, "Source text missing from reopened export")


async def exercise(ws, documents, target):
    start = time.perf_counter()
    hello = json.loads(await asyncio.wait_for(ws.recv(), 10))
    require(hello.get("type") == "hello" and hello.get("protocol") == 1, "Expected workstation hello")
    require({"open_app_or_url", "create_presentation", "create_report", "save_report"}.issubset(
        hello["capabilities"]), "Daemon did not advertise required methods")
    passed("Authenticated workstation hello and capabilities", start)

    start = time.perf_counter()
    executable = Path(os.environ["SystemRoot"]) / "System32" / "whoami.exe"
    launch = await rpc(ws, "open_app_or_url", {"target": str(executable)})
    receipt = launch["receipt"]
    require(receipt["accepted"] and receipt["method"] == "CreateProcess" and receipt["pid"] > 0,
            "Windows did not return a process-creation receipt")
    require(Path(receipt["target"]).samefile(executable), "Receipt has the wrong executable")
    passed(f"Windows created whoami.exe and returned its PID {receipt['pid']} over WebSocket", start)

    if target:
        start = time.perf_counter()
        launch = await rpc(ws, "open_app_or_url", {"target": target})
        require(launch["receipt"]["accepted"], "Windows rejected the requested target")
        passed(f"Windows accepted requested target via {launch['receipt']['method']}", start)

    start = time.perf_counter()
    result = await rpc(ws, "create_presentation", {
        "topic": "Diagnostic workbook", "filename": "slides", "open": False,
        "slides": [{"title": "Fixture overview"}, {"title": "Record", "bullets": [MARKER]},
                   {"title": "Conclusion", "notes": "Traceable to the diagnostic fixture."}]})
    check_export(result, documents, "pptx")
    passed("PPTX saved, transferred, reopened and slides checked", start)
    for extension in ("pdf", "docx"):
        start = time.perf_counter()
        draft = await rpc(ws, "create_report", {
            "format": extension, "filename": "report", "temporary": True, "open": False,
            "title": "Diagnostic report", "sections": [{"heading": "Observation", "paragraphs": [MARKER]}]})
        require(draft["temporary"] and draft["verified"]["format"] == extension, "Missing draft verification")
        require("artifact" not in draft and not (documents / ("report." + extension)).exists(),
                "Draft published before save confirmation")
        result = await rpc(ws, "save_report", {"draft_id": draft["draft_id"], "open": False})
        check_export(result, documents, extension)
        passed(f"{extension.upper()} drafted, explicitly saved, transferred and content checked", start)


async def run(target):
    from websockets.asyncio.server import serve
    from websockets.exceptions import InvalidStatus

    secret = secrets.token_urlsafe(32)
    connected = asyncio.get_running_loop().create_future()

    def authorize(connection, request):
        if request.path != "/ws/workstation" or not secrets.compare_digest(
                request.headers.get("Authorization", ""), "Bearer " + secret):
            return connection.respond(401, "Unauthorized")
        return None

    async def hub(ws):
        connected.set_result(ws)
        await ws.wait_closed()

    with tempfile.TemporaryDirectory(prefix="argus-live-workstation-") as directory:
        root = Path(directory)
        documents = root / "documents"
        daemon = Workstation(root / "workspace", documents)
        async with serve(hub, "127.0.0.1", 0, process_request=authorize, max_size=MAX_RPC_MESSAGE_BYTES) as listener:
            port = listener.sockets[0].getsockname()[1]
            url = f"ws://127.0.0.1:{port}/ws/workstation"
            start = time.perf_counter()
            try:
                async with workstation_connection(url, "invalid-diagnostic-key"):
                    raise AssertionError("Loopback hub accepted incorrect credentials")
            except InvalidStatus as exc:
                require(exc.response.status_code == 401, "Expected HTTP 401 for an incorrect bearer key")
            passed("Loopback hub rejected incorrect bearer key", start)
            async with workstation_connection(url, secret) as client:
                serving = asyncio.create_task(daemon.serve_connection(client))
                try:
                    await exercise(await asyncio.wait_for(connected, 10), documents, target)
                finally:
                    await client.close()
                    await asyncio.wait_for(serving, 10)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", help="Additionally dispatch this app, file, or browser URL on the current desktop")
    args = parser.parse_args()
    if os.name != "nt":
        print("[SKIP] This live diagnostic needs Windows.")
        sys.exit(77)
    try:
        asyncio.run(run(args.target))
    except Exception as exc:
        print(f"[FAIL] {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
