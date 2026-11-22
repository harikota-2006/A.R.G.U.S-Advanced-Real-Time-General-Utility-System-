"""No desktop launches or network calls: validate generated artifacts and RPC jobs."""
import asyncio
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from xml.etree import ElementTree as ET
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from laptop_daemon import Workstation, artifact_payload, execute_script, open_app_or_url, render_ieee_document, render_presentation, safe_filename, validate_hub_url


class InputValidationTests(unittest.TestCase):
    def test_paths_and_reserved_names_rejected(self):
        for name in ("../escape", "a/b", "a\\b", "C:drive", "CON", "x?y", ".hidden", "x ", "COM1.py"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                safe_filename(name, ".py")
        self.assertEqual(safe_filename("my research", ".docx"), "my research.docx")

    def test_remote_websocket_requires_tls(self):
        for url in ("ws://example.com/ws/workstation", "https://host/ws", "wss://u:p@host/ws", "ws://8.8.8.8/ws", "wss://host/ws?key=secret"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_hub_url(url)
        for url in ("ws://192.168.1.8:8000/ws/workstation", "ws://100.90.1.2:8000/ws/workstation", "wss://tablet.example/ws/workstation"):
            self.assertEqual(validate_hub_url(url), url)

    def test_artifact_limit_and_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "demo.pptx"
            path.write_bytes(b"fixture")
            payload = artifact_payload(path)
            self.assertEqual(base64.b64decode(payload["data_base64"]), b"fixture")
            self.assertEqual(payload["sha256"], hashlib.sha256(b"fixture").hexdigest())
            self.assertEqual(payload["size"], 7)
            with patch("laptop_daemon.MAX_ARTIFACT_BYTES", 6), self.assertRaisesRegex(ValueError, "transfer limit"):
                artifact_payload(path)


@unittest.skipUnless(os.name == "nt", "Windows launch resolution uses mocked Windows APIs")
class LaunchTests(unittest.TestCase):
    def test_explicit_executable_path_reports_api_ack_not_gui_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "app with spaces.exe"
            executable.write_bytes(b"fixture")
            process = MagicMock(pid=4321)
            process.poll.return_value = None
            with patch("laptop_daemon.subprocess.Popen", return_value=process) as launch, self.assertLogs("argus.workstation", "INFO") as messages:
                result = open_app_or_url('"' + str(executable) + '"')
            launch.assert_called_once()
            self.assertEqual(launch.call_args.args[0], [str(executable.resolve())])
            self.assertFalse(launch.call_args.kwargs["shell"])
            self.assertEqual(result["pid"], 4321)
            self.assertTrue(result["receipt"]["accepted"])
            self.assertFalse(result["receipt"]["completed"])
            self.assertFalse(result["receipt"]["gui_verified"])
            self.assertIn("Launch ACK", messages.output[0])

    def test_cmd_and_bat_aliases_use_windows_association_without_shell_string(self):
        with tempfile.TemporaryDirectory() as directory:
            for suffix in (".cmd", ".bat"):
                executable = Path(directory) / ("wrapper with spaces" + suffix)
                executable.write_text("@echo off", encoding="utf-8")
                with patch("laptop_daemon.os.startfile") as startfile, patch("laptop_daemon.subprocess.Popen") as popen:
                    result = open_app_or_url("my code", {"my code": str(executable)})
                startfile.assert_called_once_with(str(executable.resolve()), "open")
                popen.assert_not_called()
                self.assertEqual(result["receipt"]["method"], "ShellExecute")

    def test_code_path_wrapper_can_resolve_without_code_exe(self):
        import laptop_daemon
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "code.cmd"
            executable.write_text("@echo off", encoding="utf-8")
            with patch("laptop_daemon.shutil.which", side_effect=[None, str(executable)]):
                self.assertEqual(laptop_daemon._find_application("Code.exe"), str(executable))

    def test_url_and_document_return_truthful_acceptance(self):
        with patch("laptop_daemon.os.startfile") as startfile:
            result = open_app_or_url("https://example.com/path?q=value")
        startfile.assert_called_once_with("https://example.com/path?q=value", "open")
        self.assertEqual(result["kind"], "url")
        self.assertIsNone(result["pid"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paper.docx"
            path.write_bytes(b"fixture")
            with patch("laptop_daemon.os.startfile") as startfile:
                result = open_app_or_url(str(path))
            self.assertEqual(result["kind"], "file")
            startfile.assert_called_once_with(str(path.resolve()), "open")

    def test_missing_app_and_os_rejection_do_not_claim_success(self):
        with patch("laptop_daemon._find_application", return_value=None), patch("laptop_daemon.os.startfile") as startfile:
            with self.assertRaises(FileNotFoundError):
                open_app_or_url("Unknown App")
            startfile.assert_not_called()
        with patch("laptop_daemon.os.startfile", side_effect=OSError("No registered association")):
            with self.assertRaisesRegex(OSError, "No registered association"):
                open_app_or_url("https://example.com")

    def test_registry_app_path_quotes_and_environment_expansion(self):
        import laptop_daemon
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "Registered App.exe"
            executable.write_bytes(b"fixture")
            registry = MagicMock()
            registry.KEY_READ, registry.KEY_WOW64_64KEY, registry.KEY_WOW64_32KEY = 1, 2, 4
            registry.QueryValueEx.return_value = ('"%LOCALAPPDATA%/Registered App.exe"', 1)
            with patch.dict(os.environ, {"LOCALAPPDATA": directory}), patch.dict(sys.modules, {"winreg": registry}), patch("laptop_daemon.shutil.which", return_value=None):
                self.assertEqual(Path(laptop_daemon._find_application("Registered App.exe")), executable)


class ScriptTests(unittest.IsolatedAsyncioTestCase):
    async def test_script_diagnostics_and_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = await execute_script(root, {"filename": "task.py", "python_code": "print('verified')"})
            self.assertTrue(result["ok"])
            self.assertEqual(result["stdout"].strip(), "verified")
            result = await execute_script(root, {"filename": "task.py", "python_code": "raise ValueError('failure fixture')"})
            self.assertFalse(result["ok"])
            self.assertIn("ValueError: failure fixture", result["stderr"])
            self.assertEqual(len(list((root / "backups").glob("*.bak"))), 1)
            result = await execute_script(root, {"filename": "task.py", "python_code": "def broken(:"})
            self.assertTrue(result["syntax_error"])
            self.assertIn("raise ValueError", (root / "task.py").read_text())

    async def test_timeout_and_output_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            result = await execute_script(Path(directory), {"python_code": "import time; print('x' * 80000, flush=True); time.sleep(5)", "timeout_seconds": 1})
            self.assertTrue(result["timed_out"])
            self.assertFalse(result["ok"])
            self.assertLess(len(result["stdout"]), 66000)
            self.assertIn("truncated", result["stdout"])

    async def test_credentials_not_in_child_environment(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"GROQ_API_KEY": "test-fixture", "ARGUS_DAEMON_KEY": "test-fixture"}):
            result = await execute_script(Path(directory), {"python_code": "import os; print('GROQ_API_KEY' in os.environ, 'ARGUS_DAEMON_KEY' in os.environ)"})
            self.assertEqual(result["stdout"].strip(), "False False")

    async def test_cancel_terminates_script(self):
        with tempfile.TemporaryDirectory() as directory:
            task = asyncio.create_task(execute_script(Path(directory), {"python_code": "import time; time.sleep(60)"}))
            await asyncio.sleep(.2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)

    @unittest.skipUnless(os.name == "nt", "Windows Job Object test")
    async def test_parent_exit_also_stops_child_process(self):
        with tempfile.TemporaryDirectory() as directory:
            code = "import subprocess,sys\nsubprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\nprint('parent completed')"
            result = await asyncio.wait_for(execute_script(Path(directory), {"python_code": code}), 5)
            self.assertTrue(result["ok"])
            self.assertIn("parent completed", result["stdout"])

    async def test_unsupported_actions_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            workstation = Workstation(Path(directory), Path(directory))
            with self.assertRaises(ValueError):
                await workstation.dispatch("run_workstation_action", {"action": "shell", "params": {"command": "echo bad"}})


class DocumentTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("pptx"), "python-pptx is not installed")
    def test_presentation_widescreen_notes_and_collision_safe_save(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "ece.pptx"
            payload = {"topic": "ECE test", "slides": [{"title": "ECE test", "bullets": ["A verified research fixture"], "notes": "Opening notes"}, {"title": "Method", "bullets": ["First finding", "Second finding"], "notes": "Speaker evidence [1]"}, {"title": "Summary", "bullets": ["Key takeaway"], "notes": "Close"}], "sources": [{"title": "Python documentation", "url": "https://docs.python.org/3/"}]}
            result = render_presentation(payload, target)
            self.assertEqual(result["slides"], 3)
            with ZipFile(target) as archive:
                root = ET.fromstring(archive.read("ppt/presentation.xml"))
                ns = {"p": "http://schemas.openxmlformats.org/presentationml/2006/main"}
                size = root.find("p:sldSz", ns)
                self.assertAlmostEqual(int(size.get("cx")) / int(size.get("cy")), 16 / 9, places=3)
                notes = archive.read("ppt/notesSlides/notesSlide2.xml").decode()
                self.assertIn("Speaker evidence [1]", notes)
                self.assertIn("https://docs.python.org/3/", notes)
            before = target.read_bytes()
            second = render_presentation(payload, target)
            self.assertNotEqual(target, Path(second["path"]))
            self.assertEqual(target.read_bytes(), before)
            self.assertEqual(second["verified"]["slides"], 3)

    @unittest.skipUnless(importlib.util.find_spec("docx"), "python-docx is not installed")
    def test_ieee_true_columns_and_preserved_citations(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "ece.docx"
            result = render_ieee_document({"topic": "ECE fixture", "abstract": "This is a test abstract.", "index_terms": ["circuits", "ECE"], "sections": [{"heading": "Introduction", "paragraphs": ["Source-supported text [1]."]}, {"heading": "Methodology", "paragraphs": ["Test procedure."]}], "references": [{"title": "Python documentation", "url": "https://docs.python.org/3/"},{"title":"Circuits.pdf, unit 2","url":""}]}, target)
            self.assertEqual(result["references"], 2)
            with ZipFile(target) as archive:
                raw = archive.read("word/document.xml")
                root = ET.fromstring(raw)
                ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
                columns = root.findall(".//w:sectPr/w:cols", ns)
                self.assertEqual(columns[-1].get("{" + ns["w"] + "}num"), "2")
                self.assertNotEqual(columns[0].get("{" + ns["w"] + "}num"), "2")
                text = " ".join(root.itertext())
                self.assertIn("I. INTRODUCTION", text)
                self.assertIn("II. METHODOLOGY", text)
                self.assertIn("[1]", text)
                self.assertIn("[2] “Circuits.pdf, unit 2.”", text)
                self.assertEqual(text.count("[Online]"), 1)


class RpcTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(importlib.util.find_spec("pptx"), "python-pptx is not installed")
    async def test_document_artifact_transfer_survives_auto_open_failure(self):
        payload = {"topic": "Test", "filename": "delivery.pptx", "slides": [{"title": "Title", "bullets": ["Intro"], "notes": "Intro"}, {"title": "Evidence", "bullets": ["Source fixture"], "notes": "Evidence"}, {"title": "Summary", "bullets": ["Conclusion"], "notes": "Close"}], "sources": []}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            daemon = Workstation(root, root)
            with patch("laptop_daemon.open_app_or_url", side_effect=OSError("No PowerPoint association")) as launch:
                result = await daemon.dispatch("create_presentation", payload)
            launch.assert_called_once()
            self.assertTrue(result["generated"])
            self.assertFalse(result["opened"])
            self.assertIn("No PowerPoint association", result["open_error"])
            data = (root / "delivery.pptx").read_bytes()
            self.assertEqual(base64.b64decode(result["artifact"]["data_base64"]), data)
            self.assertEqual(result["artifact"]["sha256"], hashlib.sha256(data).hexdigest())
            with patch("laptop_daemon.open_app_or_url") as launch:
                result = await daemon.dispatch("create_presentation", {**payload, "open": False})
            launch.assert_not_called()
            self.assertFalse(result["opened"])
            self.assertFalse(result["open_requested"])

    async def test_duplicate_completed_rpc_returns_cached_response(self):
        class Socket:
            def __init__(self):
                self.sent = []
                self.inputs = [json.dumps({"type": "rpc", "id": "abc", "method": "telemetry", "params": {}})] * 2
            async def send(self, message):
                self.sent.append(json.loads(message))
            def __aiter__(self):
                return self
            async def __anext__(self):
                await asyncio.sleep(.05)
                if self.inputs:
                    return self.inputs.pop(0)
                raise StopAsyncIteration
        with tempfile.TemporaryDirectory() as directory:
            daemon = Workstation(Path(directory), Path(directory))
            socket = Socket()
            with patch("laptop_daemon.telemetry", return_value={"test": True}):
                await daemon.serve_connection(socket)
            results = [item for item in socket.sent if item.get("type") == "rpc_result"]
            self.assertEqual(len(results), 2)
            self.assertEqual(results[0], results[1])


if __name__ == "__main__":
    unittest.main()
