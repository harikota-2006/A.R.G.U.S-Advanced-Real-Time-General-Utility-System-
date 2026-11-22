"""Offline Windows document generation and verified-save contract."""
from __future__ import annotations
import base64
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from laptop_daemon import Workstation
from report_renderer import save_verified


def report(fmt):
    return {"title": "Feedback in the laboratory", "author": "Hari", "format": fmt,
            "filename": "feedback." + fmt, "open": False,
            "intro": "The supplied notes distinguish the observation from its interpretation.",
            "sections": [{"heading": "Observation and interpretation", "paragraphs": ["Measured gain was ten in the supplied test."],
                          "table": {"columns": ["Condition", "Gain"], "rows": [["Fixture A", "10"]]},
                          "diagram": ["Input", "Amplifier", "Feedback"]}],
            "references": [{"title": "Lab notes, page 2"}]}


class ReportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        base = Path(self.scratch.name)
        self.library = base / "library"
        self.daemon = Workstation(base / "jobs", self.library)

    async def test_pdf_report_is_readable_and_has_cited_content(self):
        from pypdf import PdfReader
        result = await self.daemon.dispatch("create_report", report("pdf"))
        self.assertEqual(result["verified"]["pages"], 1)
        self.assertEqual(result["artifact"]["media_type"], "application/pdf")
        content = base64.b64decode(result["artifact"]["data_base64"], validate=True)
        self.assertEqual(content, Path(result["path"]).read_bytes())
        actual = PdfReader(io.BytesIO(content), strict=True).pages[0].extract_text()
        for phrase in ["Measured gain was ten", "Lab notes, page 2", "Fixture A", "Feedback"]:
            self.assertIn(phrase, actual)

    async def test_word_report_has_editable_table_and_diagram(self):
        from docx import Document
        result = await self.daemon.dispatch("create_report", report("docx"))
        document = Document(result["path"])
        self.assertEqual(document.tables[0].cell(1,1).text, "10")
        self.assertIn("Amplifier", document.tables[1].cell(0,1).text)
        self.assertIn("Lab notes, page 2", " ".join(p.text for p in document.paragraphs))

    async def test_draft_never_publishes_before_confirm_and_never_overwrites(self):
        request = {**report("docx"), "temporary": True}
        with patch("laptop_daemon.open_app_or_url", side_effect=AssertionError("unexpected GUI")):
            result = await self.daemon.dispatch("create_report", request)
            self.assertTrue(result["temporary"])
            self.assertNotIn("artifact", result)
            self.assertFalse(self.library.exists())
            self.library.mkdir()
            existing = self.library / "feedback.docx"
            existing.write_bytes(b"prior contents")
            approved = await self.daemon.dispatch("save_report", {"draft_id":result["draft_id"], "open":False})
            self.assertEqual(existing.read_bytes(), b"prior contents")
            self.assertNotEqual(Path(approved["path"]), existing)
            self.assertEqual(approved["verified"]["format"], "docx")
            with self.assertRaisesRegex(ValueError, "unknown"):
                await self.daemon.dispatch("save_report", {"draft_id":result["draft_id"]})

    async def test_invalid_document_is_not_published(self):
        with self.assertRaises(Exception):
            save_verified(lambda p:p.write_bytes(b"unreadable"), self.library / "failure.docx")
        self.assertFalse(self.library.exists())

    async def test_editable_presentation_table_and_flow_diagram(self):
        from pptx import Presentation
        result = await self.daemon.dispatch("create_presentation", {"topic":"Source review", "open":False,
            "slides":[{"title":"Source review"}, {"title":"Measurements", "table":{"columns":["Circuit","Gain"],"rows":[["A","10"]]}},
                      {"title":"Signal path","diagram":["Input","Amplifier","Feedback"]}, {"title":"Summary","bullets":["Gain was ten."]}]})
        deck = Presentation(result["path"])
        self.assertEqual(result["verified"]["slides"], 4)
        self.assertEqual(next(s.table for s in deck.slides[1].shapes if s.has_table).cell(1,1).text, "10")
        self.assertIn("Amplifier", [s.text for s in deck.slides[2].shapes if s.has_text_frame])

if __name__ == "__main__":
    unittest.main()
