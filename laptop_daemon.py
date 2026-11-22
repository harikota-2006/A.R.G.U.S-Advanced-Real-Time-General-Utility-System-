"""Authenticated outbound workstation RPC client; imported safely on every platform.

Run on Windows with ARGUS_HUB_WS and ARGUS_DAEMON_KEY in .env. Document
rendering and application dependencies are deliberately isolated from the hub.
Python jobs run as the current Windows user: a workspace is not an OS sandbox.
"""
from __future__ import annotations

import ast
import asyncio
import base64
import contextlib
import hashlib
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import platform
import random
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit
from report_renderer import (diagram_data, publish_document, render_report, save_verified,
                             table_data, verify_document)

log = logging.getLogger("argus.workstation")
MAX_CODE_BYTES = 256_000
MAX_OUTPUT_BYTES = 65_536
MAX_ARTIFACT_BYTES = 12 * 1024 * 1024
MAX_RPC_MESSAGE_BYTES = 20 * 1024 * 1024
MAX_CACHED_RESULT_BYTES = 40 * 1024 * 1024
ARTIFACT_MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def validate_hub_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"ws", "wss"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("ARGUS_HUB_WS must be an explicit ws:// or wss:// URL without credentials")
    if parsed.scheme == "ws":
        host = parsed.hostname
        try:
            address = ipaddress.ip_address(host)
            private = address.is_private or address.is_loopback or address in ipaddress.ip_network("100.64.0.0/10")
        except ValueError:
            private = host == "localhost"
        if not private:
            raise ValueError("Plain ws:// is allowed only for private IPs, Tailscale IPs, or localhost; use wss:// for hostnames")
    if parsed.query or parsed.fragment:
        raise ValueError("Do not place credentials or query parameters in ARGUS_HUB_WS")
    return value


def safe_filename(value: str, suffix: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 150:
        raise ValueError("A filename of 1–150 characters is required")
    if any(c in value for c in '/\\:<>"|?*') or any(ord(c) < 32 for c in value) or value != value.strip(" ."):
        raise ValueError("Filename must be a plain name without path separators or Windows metacharacters")
    if value.split(".", 1)[0].upper() in _RESERVED_NAMES or value.startswith("."):
        raise ValueError("Reserved filename")
    if not value.lower().endswith(suffix):
        value += suffix
    return value


def _inside(root: Path, name: str, suffix: str) -> Path:
    candidate = (root / safe_filename(name, suffix)).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise ValueError("Path escapes the configured workspace")
    return candidate


def _backup(path: Path) -> None:
    if path.exists():
        backup_dir = path.parent / "backups"
        backup_dir.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        shutil.copy2(path, backup_dir / f"{path.name}.{stamp}.bak")


def _atomic_save(save: Any, target: Path) -> None:
    """Render first, then replace; a failed renderer never destroys an old file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".argus-", suffix=target.suffix, dir=target.parent)
    os.close(fd)
    try:
        save(temporary)
        _backup(target)
        os.replace(temporary, target)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def _file_digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def artifact_payload(path: Path) -> dict[str, Any]:
    """Transfer a generated Office file over the authenticated workstation channel."""
    media_type = ARTIFACT_MEDIA_TYPES.get(path.suffix.lower())
    if not media_type:
        raise ValueError("Only generated PPTX, PDF and DOCX files are exportable")
    if path.stat().st_size > MAX_ARTIFACT_BYTES:
        raise ValueError("Generated file exceeds the 12 MiB transfer limit; it remains saved on the laptop")
    with path.open("rb") as stream:
        data = stream.read(MAX_ARTIFACT_BYTES + 1)
    if not data or len(data) > MAX_ARTIFACT_BYTES:
        raise ValueError("Generated export is empty or exceeds the 12 MiB transfer limit")
    return {"filename": path.name, "media_type": media_type,
            "data_base64": base64.b64encode(data).decode("ascii"),
            "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


def _text(value: Any, label: str, maximum: int = 10_000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{label} must be nonempty text of at most {maximum} characters")
    return value.strip()


def _web_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("An explicit HTTP(S) URL without embedded credentials is required")
    if any(ord(c) < 32 for c in value):
        raise ValueError("URL contains control characters")
    return value


def _sources(raw: Any, *, local_references: bool = False) -> list[dict[str, str]]:
    if not isinstance(raw, list) or len(raw) > 100:
        raise ValueError("sources/references must be a list with at most 100 entries")
    output = []
    for source in raw:
        if not isinstance(source, dict):
            raise ValueError("Each source must be an object")
        url = source.get("url")
        entry = {"title": _text(source.get("title"), "source title", 1000),
                 "url": "" if local_references and not url else _web_url(_text(url, "source URL", 4096))}
        for field in ("authors", "year"):
            if source.get(field):
                entry[field] = str(source[field])[:500]
        output.append(entry)
    return output


def render_presentation(payload: dict[str, Any], target: Path) -> dict[str, Any]:
    """Render exactly the researched slides supplied by the hub, including notes."""
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.enum.text import PP_ALIGN
    from pptx.util import Inches, Pt

    topic = _text(payload.get("topic"), "topic", 500)
    slides = payload.get("slides")
    if not isinstance(slides, list) or not 3 <= len(slides) <= 40:
        raise ValueError("slides must contain 3–40 researched slides, including title and summary")
    if payload.get("num_slides") is not None and int(payload["num_slides"]) != len(slides):
        raise ValueError("num_slides does not match the supplied slide payload")
    sources = _sources(payload.get("sources", []))
    deck = Presentation()
    deck.slide_width, deck.slide_height = Inches(13.333), Inches(7.5)
    deck.core_properties.title, deck.core_properties.author = topic, "Hari • A.R.G.U.S."
    palette = {"background": "081522", "text": "EAF4FB", "accent": "5BE0E5", "muted": "9DAFC1"}

    def textbox(slide: Any, x: float, y: float, w: float, h: float, text: str, size: int, color: str, bold: bool = False) -> Any:
        frame = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h)).text_frame
        frame.word_wrap = True
        frame.margin_left = frame.margin_right = 0
        p = frame.paragraphs[0]
        p.text = text
        p.font.name, p.font.size, p.font.bold = "Aptos", Pt(size), bold
        p.font.color.rgb = RGBColor.from_string(palette[color])
        return frame

    for index, item in enumerate(slides):
        if not isinstance(item, dict):
            raise ValueError("Each slide must be an object")
        title = _text(item.get("title"), "slide title", 180)
        bullets = item.get("bullets", [])
        if not isinstance(bullets, list) or len(bullets) > 8:
            raise ValueError("Each slide supports at most eight concise bullet points")
        full_bullets = [_text(b, "bullet", 4000) for b in bullets]
        # Keep the editable canvas readable while retaining complete material in notes.
        bullets = [b if len(b) <= 180 else b[:177].rsplit(" ", 1)[0] + "…" for b in full_bullets]
        notes = str(item.get("notes", ""))[:20_000]
        if bullets != full_bullets or item.get("table") or item.get("diagram"):
            notes += "\n\n" + "\n".join(full_bullets)
        slide = deck.slides.add_slide(deck.slide_layouts[6])
        slide.background.fill.solid()
        slide.background.fill.fore_color.rgb = RGBColor.from_string(palette["background"])
        textbox(slide, .65, .35, 11.8, .3, "A.R.G.U.S.  /  RESEARCH BRIEF", 11, "accent", True)
        if index == 0:
            textbox(slide, .7, 1.4, 11.8, 2.1, title, 32 if len(title) > 120 else 36 if len(title) > 90 else 44, "text", True)
            textbox(slide, .72, 3.8, 11.7, 2.3, "\n".join(bullets), 21, "muted")
        elif item.get("chart"):
            from pptx.chart.data import CategoryChartData
            from pptx.enum.chart import XL_CHART_TYPE
            spec = item["chart"]
            labels, values = spec.get("labels"), spec.get("values")
            if not isinstance(labels,list) or not 1 <= len(labels) <= 10 or not isinstance(values,list) or len(values)!=len(labels):
                raise ValueError("A slide chart requires matching labels and values, up to ten categories")
            if any(type(v) not in (int,float) or not math.isfinite(v) for v in values):
                raise ValueError("Chart values must be finite numbers from cited evidence")
            data = CategoryChartData()
            data.categories = [_text(label,"chart label",60) for label in labels]
            data.add_series(_text(spec.get("label","Value"),"series label",60), values)
            textbox(slide,.7,.95,11.9,1.15,title,30,"text",True)
            chart = slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(.85), Inches(2.3),
                Inches(11.6), Inches(3.7), data).chart
            chart.has_legend = False
            chart.chart_style = 10
            if bullets:
                textbox(slide,.8,6.1,11.6,.65,bullets[0],14,"muted")
        elif item.get("takeaway"):
            textbox(slide,.7,.95,11.9,1.15,title,30,"text",True)
            textbox(slide,.95,2.5,11.2,2.0,_text(item["takeaway"],"takeaway",240),32,"accent",True)
            if bullets:
                textbox(slide,.95,4.8,10.8,1.4,"\n".join(bullets[:3]),19,"muted")
        elif item.get("table") or item.get("diagram"):
            if item.get("table") and item.get("diagram"):
                raise ValueError("Choose either a table or a diagram for a slide")
            textbox(slide, .7, .95, 11.9, 1.15, title, 30, "text", True)
            if item.get("table"):
                grid = table_data(item["table"], max_columns=4, max_rows=5, cell_limit=84)
                table = slide.shapes.add_table(len(grid), len(grid[0]), Inches(.75), Inches(2.45),
                    Inches(11.8), Inches(min(3.6, len(grid)*.65))).table
                for r, row in enumerate(grid):
                    for c, label in enumerate(row):
                        cell = table.cell(r,c)
                        cell.text = label
                        cell.fill.solid()
                        cell.fill.fore_color.rgb = RGBColor.from_string("1C5E68" if r == 0 else ("173443" if r%2 else "102B38"))
                        para = cell.text_frame.paragraphs[0]
                        para.font.name, para.font.size, para.font.bold = "Aptos", Pt(14), r==0
                        para.font.color.rgb = RGBColor.from_string("F1FAFC")
            else:
                from pptx.enum.shapes import MSO_SHAPE
                steps = diagram_data(item["diagram"])
                width = 11.7 / len(steps)
                for n, step in enumerate(steps):
                    box = slide.shapes.add_shape(MSO_SHAPE.CHEVRON, Inches(.75+n*width), Inches(3),
                        Inches(width-.05), Inches(1.6))
                    box.fill.solid()
                    box.fill.fore_color.rgb = RGBColor.from_string("195B6B")
                    box.line.color.rgb = RGBColor.from_string("54BFC3")
                    box.text_frame.word_wrap = True
                    box.text_frame.paragraphs[0].text = step
                    box.text_frame.paragraphs[0].font.size = Pt(14)
                    box.text_frame.paragraphs[0].font.color.rgb = RGBColor.from_string("F1FAFC")
            if bullets:
                textbox(slide, .8, 6.1, 11.6, .75, bullets[0], 14, "muted")
        else:
            textbox(slide, .7, .95, 11.9, 1.15, title, 26 if len(title) > 90 else 34, "text", True)
            two_columns = len(bullets) > 4 or sum(map(len, bullets)) > 700
            columns = [bullets] if not two_columns else [bullets[: (len(bullets) + 1) // 2], bullets[(len(bullets) + 1) // 2 :]]
            for column, items in enumerate(columns):
                width = 11.8 if not two_columns else 5.7
                font_size = 23
                while font_size > 16:
                    chars_per_line = max(1, width * 72 / (font_size * .55))
                    estimated_height = sum(max(1, math.ceil((len(b) + 3) / chars_per_line)) * font_size * 1.1 + 14 for b in items)
                    if estimated_height <= 290:
                        break
                    font_size -= 1
                if sum(max(1, math.ceil((len(b) + 3) / (width * 72 / (font_size * .55)))) * font_size * 1.1 + 14 for b in items) > 300:
                    raise ValueError("Slide text cannot fit legibly; use shorter bullets or additional slides")
                frame = textbox(slide, .8 + column * 6.1, 2.3, width, 4.25, "", font_size, "text")
                for number, bullet in enumerate(items):
                    p = frame.paragraphs[0] if number == 0 else frame.add_paragraph()
                    p.text = "•  " + bullet
                    p.font.name, p.font.size = "Aptos", Pt(font_size)
                    p.font.color.rgb = RGBColor.from_string(palette["text"])
                    p.space_after, p.line_spacing = Pt(14), 1.1
        footer = textbox(slide, .7, 7.0, 11.9, .3, f"{topic[:100]}    |    {index + 1:02d} / {len(slides):02d}", 10, "muted")
        footer.paragraphs[0].alignment = PP_ALIGN.RIGHT
        references = "\n".join(f"[{n}] {s['title']} — {s['url']}" for n, s in enumerate(sources, 1))
        slide.notes_slide.notes_text_frame.text = notes + ("\n\nVerified source links supplied by hub:\n" + references if references else "")
    target, verified = save_verified(deck.save, target)
    return {"path": str(target), "slides": len(slides), "source_count": len(sources), "bytes": target.stat().st_size,
            "verified": verified}


def _roman(number: int) -> str:
    result = ""
    for value, numeral in ((50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")):
        count, number = divmod(number, value)
        result += numeral * count
    return result


def render_ieee_document(payload: dict[str, Any], target: Path) -> dict[str, Any]:
    """IEEE conference-style layout, not a guarantee of venue-specific compliance."""
    from docx import Document
    from docx.enum.section import WD_SECTION_START
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Inches, Pt

    title = _text(payload.get("title") or payload.get("topic"), "title", 500)
    abstract = _text(payload.get("abstract"), "abstract", 6000)
    sections = payload.get("sections")
    if not isinstance(sections, list) or not 1 <= len(sections) <= 30:
        raise ValueError("sections must contain 1–30 researched sections")
    terms = payload.get("index_terms", [])
    if not isinstance(terms, list) or len(terms) > 20:
        raise ValueError("index_terms must be a list with at most 20 terms")
    terms = [_text(term, "index term", 100) for term in terms]
    references = _sources(payload.get("references", []), local_references=True)
    doc = Document()
    doc.core_properties.title, doc.core_properties.author = title, str(payload.get("author") or "Hari")[:500]
    section = doc.sections[0]
    section.page_width, section.page_height = Inches(8.5), Inches(11)
    section.top_margin, section.bottom_margin = Inches(.75), Inches(1)
    section.left_margin = section.right_margin = Inches(.625)
    normal = doc.styles["Normal"]
    normal.font.name, normal.font.size = "Times New Roman", Pt(10)
    normal.paragraph_format.space_after = Pt(0)
    normal.paragraph_format.line_spacing = 1.0
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_after = Pt(10)
    run = p.add_run(title)
    run.font.name, run.font.size = "Times New Roman", Pt(24)
    p = doc.add_paragraph(str(payload.get("author") or "Hari"))
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_after = Pt(12)
    for run in p.runs:
        run.font.size = Pt(11)
    body = doc.add_section(WD_SECTION_START.CONTINUOUS)
    cols = body._sectPr.find(qn("w:cols"))
    if cols is None:
        cols = OxmlElement("w:cols")
        body._sectPr.append(cols)
    cols.set(qn("w:num"), "2")
    cols.set(qn("w:space"), "360")
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    p.add_run("Abstract—").bold = True
    p.add_run(abstract).bold = True
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(8)
    p.add_run("Index Terms—").bold = True
    p.add_run(", ".join(terms)).italic = True
    for index, entry in enumerate(sections, 1):
        if not isinstance(entry, dict):
            raise ValueError("Each section must be an object")
        heading = _text(entry.get("heading"), "section heading", 200)
        heading = re.sub(r"^[IVXLCDM]+\.\s*", "", heading, flags=re.I)
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.space_before, p.paragraph_format.space_after = Pt(8), Pt(4)
        p.paragraph_format.keep_with_next = True
        p.add_run(f"{_roman(index)}. {heading.upper()}")
        paragraphs = entry.get("paragraphs")
        if not isinstance(paragraphs, list) or not 1 <= len(paragraphs) <= 40:
            raise ValueError("Each section requires 1–40 paragraphs")
        for paragraph in paragraphs:
            p = doc.add_paragraph(_text(paragraph, "paragraph", 15_000))
            p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            p.paragraph_format.first_line_indent = Inches(.14)
            p.paragraph_format.widow_control = True
    if references:
        p = doc.add_paragraph("REFERENCES")
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.space_before = Pt(8)
        p.paragraph_format.keep_with_next = True
        for number, ref in enumerate(references, 1):
            author = ref.get("authors", "")
            date = ref.get("year", "")
            citation = f"[{number}] " + (author + ", " if author else "") + f'“{ref["title"]}.”'
            if date:
                citation += " " + date + "."
            if ref["url"]:
                citation += f' [Online]. Available: {ref["url"]}'
            p = doc.add_paragraph(citation)
            p.paragraph_format.left_indent, p.paragraph_format.first_line_indent = Inches(.18), Inches(-.18)
            for run in p.runs:
                run.font.size = Pt(8)
    target, verified = save_verified(doc.save, target)
    return {"path": str(target), "sections": len(sections), "references": len(references), "bytes": target.stat().st_size,
            "verified": verified, "layout": "IEEE conference-style, two columns"}


APP_ALIASES = {
    "paint": "mspaint.exe", "mspaint": "mspaint.exe", "notepad": "notepad.exe",
    "calculator": "calc.exe", "calc": "calc.exe", "explorer": "explorer.exe",
    "vs code": "Code.exe", "vscode": "Code.exe", "code": "Code.exe", "visual studio code": "Code.exe",
    "chrome": "chrome.exe", "edge": "msedge.exe", "browser": "msedge.exe",
    "terminal": "wt.exe", "powershell": "powershell.exe", "cmd": "cmd.exe",
    "word": "WINWORD.EXE", "excel": "EXCEL.EXE", "powerpoint": "POWERPNT.EXE",
    "arduino": "Arduino IDE.exe", "arduino ide": "Arduino IDE.exe",
    "microsoft paint": "mspaint.exe", "google chrome": "chrome.exe", "microsoft edge": "msedge.exe",
    "file explorer": "explorer.exe", "windows explorer": "explorer.exe", "windows terminal": "wt.exe",
    "command prompt": "cmd.exe", "windows powershell": "powershell.exe", "snipping tool": "SnippingTool.exe",
    "microsoft word": "WINWORD.EXE", "microsoft excel": "EXCEL.EXE", "microsoft powerpoint": "POWERPNT.EXE",
}

_PATH_VARIABLES = {"LOCALAPPDATA", "APPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
                   "USERPROFILE", "SYSTEMROOT", "WINDIR", "ONEDRIVE"}


def _expand_target_path(value: str) -> Path:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
    if any(ord(char) < 32 for char in value) or '"' in value:
        raise ValueError("A single file path without command arguments is required")
    def variable(match):
        name = match[1].upper()
        if name not in _PATH_VARIABLES:
            raise ValueError("Unsupported path environment variable")
        return next((v for k, v in os.environ.items() if k.upper() == name), match[0])
    return Path(re.sub(r"%([^%]+)%", variable, value)).expanduser()


def _find_shortcut(query: str) -> str | None:
    wanted = re.sub(r"[\s_-]", "", Path(query).stem.casefold())
    candidates = [("APPDATA", "Microsoft/Windows/Start Menu/Programs"),
                  ("PROGRAMDATA", "Microsoft/Windows/Start Menu/Programs"),
                  ("USERPROFILE", "Desktop"), ("ONEDRIVE", "Desktop")]
    inspected = 0
    for variable, relative in candidates:
        if not os.getenv(variable):
            continue
        base = Path(os.environ[variable]) / relative
        if not base.is_dir():
            continue
        for root, dirs, files in os.walk(base):
            dirs[:] = [name for name in dirs if not name.startswith(".")]
            for filename in files:
                inspected += 1
                if inspected > 5000:
                    return None
                name = Path(filename)
                if name.suffix.lower() in {".lnk", ".url"} and re.sub(r"[\s_-]", "", name.stem.casefold()) == wanted:
                    return str(Path(root) / name)
    return None


def _find_application(executable: str) -> str | None:
    found = shutil.which(executable) or shutil.which(Path(executable).stem)
    if found:
        return found
    if os.name == "nt":
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for flags in (winreg.KEY_READ, winreg.KEY_READ | winreg.KEY_WOW64_64KEY, winreg.KEY_READ | winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(hive, "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\" + executable, 0, flags) as key:
                        value = _expand_target_path(str(winreg.QueryValueEx(key, "")[0]))
                        if value.is_file():
                            return str(value)
                except (OSError, ValueError):
                    continue
        candidates = {
            "code.exe": [("LOCALAPPDATA", "Programs/Microsoft VS Code/Code.exe"), ("ProgramFiles", "Microsoft VS Code/Code.exe")],
            "chrome.exe": [("ProgramFiles", "Google/Chrome/Application/chrome.exe"), ("LOCALAPPDATA", "Google/Chrome/Application/chrome.exe")],
            "msedge.exe": [("ProgramFiles(x86)", "Microsoft/Edge/Application/msedge.exe"), ("ProgramFiles", "Microsoft/Edge/Application/msedge.exe")],
            "wt.exe": [("LOCALAPPDATA", "Microsoft/WindowsApps/wt.exe")],
            "mspaint.exe": [("SYSTEMROOT", "System32/mspaint.exe"), ("LOCALAPPDATA", "Microsoft/WindowsApps/mspaint.exe")],
        }
        for variable, relative in candidates.get(executable.lower(), []):
            if os.getenv(variable):
                candidate = Path(os.environ[variable]) / relative
                if candidate.is_file():
                    return str(candidate)
    return _find_shortcut(executable)


def _receipt(target: str, kind: str, method: str, pid: int | None = None) -> dict[str, Any]:
    receipt = {"id": secrets.token_hex(12), "accepted": True, "completed": False, "gui_verified": False,
               "method": method, "pid": pid, "target": target,
               "timestamp": datetime.now(timezone.utc).isoformat()}
    # Do not put query-string secrets in the daemon log.
    logged_target = urlsplit(target)._replace(query="", fragment="").geturl() if kind == "url" else target
    log.info("Launch ACK id=%s method=%s pid=%s accepted=true gui_verified=false target=%s",
             receipt["id"], method, pid, logged_target)
    return {"ok": True, "status": "launch_accepted", "kind": kind, "target": target,
            "pid": pid, "receipt": receipt,
            "message": "Windows accepted the launch request. The application window has not been verified."}


def _launch_path(path: Path, kind: str = "file") -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"The requested file does not exist: {path}")
    absolute = str(path.resolve())
    if path.is_file() and path.suffix.lower() == ".exe":
        process = subprocess.Popen([absolute], shell=False, cwd=str(path.parent.resolve()),
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        status = process.poll()
        if isinstance(status, int) and status != 0:
            raise RuntimeError(f"The application process exited immediately with code {status}")
        return _receipt(absolute, kind, "CreateProcess", process.pid)
    # Windows resolves shortcuts, .cmd/.bat wrappers, folders and document
    # associations. Passing one path avoids constructing a cmd command string.
    os.startfile(absolute, "open")
    return _receipt(absolute, kind, "ShellExecute")


_DOCUMENT_TYPES = {".pdf", ".docx", ".pptx", ".xlsx", ".txt", ".md", ".png", ".jpg", ".jpeg", ".csv"}


def _resolve_named_file(name: str) -> Path | None:
    """Resolve a bare document basename in common user or OneDrive locations."""
    path = Path(name)
    if path.name != name or path.suffix.casefold() not in _DOCUMENT_TYPES:
        return None
    roots = [Path(os.getenv("ARGUS_DOCUMENTS") or Path.home() / "Documents" / "ARGUS" / "documents")]
    for base in [Path.home(), *(Path(os.environ[k]) for k in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial") if os.getenv(k))]:
        roots.extend([base / "Desktop", base / "Documents", base / "Downloads"])
    found = {candidate.resolve() for root in roots if (candidate := root / name).is_file()}
    if len(found) > 1:
        raise ValueError("Several files share that name. Specify the full path.")
    return next(iter(found), None)


def open_app_or_url(target: str, app_paths: dict[str, str] | None = None) -> dict[str, Any]:
    if os.name != "nt":
        raise RuntimeError("Application control is available only on the Windows workstation")
    target = _text(target, "target", 4096)
    if target.lower().startswith(("https://", "http://")):
        url = _web_url(target)
        os.startfile(url, "open")
        return _receipt(url, "url", "ShellExecute")
    supplied_path = _expand_target_path(target)
    if supplied_path.exists():
        return _launch_path(supplied_path, "application" if supplied_path.suffix.lower() in {".exe", ".cmd", ".bat", ".lnk"} else "file")
    if supplied_path.is_absolute() or any(separator in target for separator in ("/", "\\")):
        raise FileNotFoundError(f"The requested file does not exist: {supplied_path}")
    named = _resolve_named_file(target)
    if named:
        return _launch_path(named, "file")
    key = re.sub(r"\s+", " ", target.casefold()).strip()
    custom = (app_paths or {}).get(key)
    executable = custom or APP_ALIASES.get(key)
    if not executable:
        # Permit an exact PATH executable or an exact Start Menu shortcut name;
        # never concatenate arbitrary arguments or guess a website domain.
        executable = target if re.fullmatch(r"[\w .-]{1,120}", target) else None
    path = str(_expand_target_path(executable).resolve()) if custom else _find_application(executable) if executable else None
    if not path or not Path(path).is_file() or Path(path).suffix.lower() not in {".exe", ".lnk", ".url", ".cmd", ".bat"}:
        raise FileNotFoundError(f"Application not found through PATH, App Paths, install locations or Start Menu: {target}. Configure ARGUS_APP_PATHS with its exact path.")
    return _launch_path(Path(path), "application")


def telemetry() -> dict[str, Any]:
    result: dict[str, Any] = {"platform": platform.system(), "node_id": "asus", "timestamp": time.time()}
    try:
        import psutil
        result["cpu_percent"], result["memory_percent"] = psutil.cpu_percent(), psutil.virtual_memory().percent
        battery = psutil.sensors_battery()
        if battery is not None:
            result["battery"] = {"percent": battery.percent, "plugged": battery.power_plugged, "seconds_left": battery.secsleft}
    except ImportError:
        result["sensors_available"] = False
    return result


async def _capture(reader: asyncio.StreamReader) -> str:
    output = bytearray()
    truncated = False
    while block := await reader.read(8192):
        room = MAX_OUTPUT_BYTES - len(output)
        output.extend(block[:room])
        truncated |= len(block) > room
    return output.decode("utf-8", errors="replace") + ("\n[Output truncated at 64 KiB]" if truncated else "")


class _WindowsJob:
    """Ensure script descendants die when a job ends, including parent early exit.

    A Job Object controls process lifetime, not file/network permissions. It is
    not a sandbox and does not make arbitrary Python untrusted-code-safe.
    """
    def __init__(self, process_id: int):
        import ctypes
        from ctypes import wintypes

        class BasicLimit(ctypes.Structure):
            _fields_ = [("ProcessTime", ctypes.c_longlong), ("JobTime", ctypes.c_longlong), ("Flags", wintypes.DWORD), ("MinWorkingSet", ctypes.c_size_t), ("MaxWorkingSet", ctypes.c_size_t), ("ActiveProcesses", wintypes.DWORD), ("Affinity", ctypes.c_size_t), ("Priority", wintypes.DWORD), ("Scheduling", wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(field, ctypes.c_ulonglong) for field in ("ReadOperations", "WriteOperations", "OtherOperations", "ReadBytes", "WriteBytes", "OtherBytes")]

        class ExtendedLimit(ctypes.Structure):
            _fields_ = [("Basic", BasicLimit), ("Io", IoCounters), ("ProcessMemory", ctypes.c_size_t), ("JobMemory", ctypes.c_size_t), ("PeakProcessMemory", ctypes.c_size_t), ("PeakJobMemory", ctypes.c_size_t)]

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.SetInformationJobObject.restype = wintypes.BOOL
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = ExtendedLimit()
        info.Basic.Flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        process_handle = None
        try:
            if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            process_handle = self.kernel.OpenProcess(0x0100 | 0x0001, False, process_id)
            if not process_handle or not self.kernel.AssignProcessToJobObject(self.handle, process_handle):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.close()
            raise
        finally:
            if process_handle:
                self.kernel.CloseHandle(process_handle)

    def close(self) -> None:
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


async def _kill_process_tree(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    if os.name == "nt":
        killer = await asyncio.create_subprocess_exec("taskkill", "/PID", str(process.pid), "/T", "/F", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            await asyncio.wait_for(killer.wait(), 5)
        except asyncio.TimeoutError:
            killer.kill()
            await killer.wait()
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
    await process.wait()


async def execute_script(workspace: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Compile and run a requested script with bounded output, time and teardown."""
    workspace.mkdir(parents=True, exist_ok=True)
    target = _inside(workspace, str(payload.get("filename") or "argus_task.py"), ".py")
    code = payload.get("python_code", payload.get("code"))
    if not isinstance(code, str) or not code.strip() or len(code.encode("utf-8")) > MAX_CODE_BYTES:
        raise ValueError("python_code must be nonempty and at most 256 KB")
    timeout = max(1.0, min(float(payload.get("timeout_seconds", 30)), 120.0))
    started = time.monotonic()
    try:
        ast.parse(code, filename=str(target))
        compile(code, str(target), "exec")
    except (SyntaxError, ValueError) as exc:
        return {"ok": False, "syntax_error": True, "stdout": "", "stderr": f"{type(exc).__name__}: {exc}", "exit_code": None, "path": str(target), "duration_seconds": time.monotonic() - started, "timed_out": False}
    _atomic_save(lambda filename: Path(filename).write_text(code, encoding="utf-8"), target)
    # Preserve tools' ordinary runtime environment while withholding the hub secrets.
    sensitive = re.compile(r"(?:KEY|TOKEN|PASSWORD|SECRET|PASSKEY|NTFY|ARGUS_PIN|GATE[12])", re.I)
    env = {k: v for k, v in os.environ.items() if not sensitive.search(k)}
    env["PYTHONIOENCODING"] = "utf-8"
    options: dict[str, Any] = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    # The bootstrap waits for a pipe byte before user code starts. This closes
    # the race where a fast script could spawn descendants before Job assignment.
    bootstrap = "import runpy,sys; sys.stdin.buffer.read(1); sys.argv=[sys.argv[1]]; runpy.run_path(sys.argv[0],run_name='__main__')"
    process = await asyncio.create_subprocess_exec(sys.executable, "-I", "-u", "-c", bootstrap, str(target), cwd=workspace, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **options)
    job = None
    try:
        if os.name == "nt":
            job = _WindowsJob(process.pid)
    except OSError:
        await _kill_process_tree(process)
        raise RuntimeError("Windows could not isolate the job's process lifetime; script execution stopped")
    stdout_task, stderr_task = asyncio.create_task(_capture(process.stdout)), asyncio.create_task(_capture(process.stderr))
    timed_out = False
    try:
        process.stdin.write(b"1")
        await process.stdin.drain()
        process.stdin.close()
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        timed_out = True
        if job:
            job.close()
        await _kill_process_tree(process)
    except asyncio.CancelledError:
        if job:
            job.close()
        await asyncio.shield(_kill_process_tree(process))
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        raise
    finally:
        if job:
            job.close()
    stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
    if timed_out:
        stderr += f"\nExecution stopped after {timeout:g} seconds."
    return {"ok": process.returncode == 0 and not timed_out, "stdout": stdout, "stderr": stderr, "exit_code": process.returncode, "path": str(target), "duration_seconds": round(time.monotonic() - started, 3), "timed_out": timed_out}


class Workstation:
    METHODS = ("create_presentation", "create_ieee_document", "create_report", "save_report",
               "open_app_or_url", "execute_script", "run_workstation_action", "telemetry")

    def __init__(self, workspace: Path, documents: Path, app_paths: dict[str, str] | None = None):
        self.workspace, self.documents = workspace.resolve(), documents.resolve()
        self.app_paths = app_paths or {}
        self._jobs: dict[str, asyncio.Task] = {}
        self._completed: OrderedDict[str, tuple[str, dict[str, Any]]] = OrderedDict()
        self._cache_sizes: dict[str, int] = {}
        self._file_locks: dict[str, asyncio.Lock] = {}
        self._semaphore = asyncio.Semaphore(2)
        self._send_lock = asyncio.Lock()
        self._drafts: dict[str, tuple[Path, Path, str, float]] = {}
        self._draft_lock = asyncio.Lock()

    def _expire_drafts(self):
        now = time.time()
        for identifier, (path, _, _, expires) in tuple(self._drafts.items()):
            if expires <= now:
                path.unlink(missing_ok=True)
                del self._drafts[identifier]

    async def _finish_document(self, path, result, auto_open):
        result.update({"generated": True, "opened": False, "open_requested": auto_open, "gui_verified": False})
        try:
            result["artifact"] = await asyncio.to_thread(artifact_payload, path)
        except (OSError, ValueError) as exc:
            result["export_error"] = str(exc)[:400]
            result["warning"] = "File saved on the laptop; transfer failed: " + result["export_error"]
        if auto_open:
            try:
                launch = await asyncio.to_thread(open_app_or_url, str(path), self.app_paths)
                result["launch_receipt"] = launch["receipt"]
                result["opened"] = bool(launch["receipt"]["accepted"])
            except (OSError, RuntimeError, ValueError) as exc:
                result["open_error"] = str(exc)[:400]
                result["warning"] = (result.get("warning", "") + " Windows could not open the file: " + result["open_error"]).strip()
        result["message"] = f"Saved and verified {path.name}."
        if result["opened"]:
            result["message"] += " Windows accepted the open request."
        if result.get("warning"):
            result["message"] += " " + result["warning"]
        return result

    async def _document_rpc(self, method, params):
        auto_open = params.get("open", params.get("auto_open", method != "save_report"))
        if not isinstance(auto_open, bool):
            raise ValueError("open/auto_open must be a boolean")
        if method == "save_report":
            async with self._draft_lock:
                self._expire_drafts()
                identifier = params.get("draft_id")
                if not isinstance(identifier, str) or identifier not in self._drafts:
                    raise ValueError("That report draft expired, was already saved, or is unknown")
                staged, requested, digest, _ = self._drafts[identifier]
                if not secrets.compare_digest(await asyncio.to_thread(_file_digest, staged), digest):
                    raise ValueError("Report draft changed; regenerate the report")
                verified = await asyncio.to_thread(verify_document, staged)
                path = await asyncio.to_thread(publish_document, staged, requested)
                staged.unlink()
                del self._drafts[identifier]
                result = {"verified": verified, "path": str(path), "bytes": path.stat().st_size}
        elif method == "create_report":
            if params.get("format") not in {"docx", "pdf"}:
                raise ValueError("Report format must be docx or pdf")
            suffix = "." + params["format"]
            requested = _inside(self.documents, params.get("filename") or "argus-report", suffix)
            temporary = params.get("temporary", False)
            if not isinstance(temporary, bool):
                raise ValueError("temporary must be a boolean")
            def make(path):
                render_report(params, path)
            if temporary:
                async with self._draft_lock:
                    self._expire_drafts()
                    if len(self._drafts) >= 8:
                        raise ValueError("Eight report drafts await saving or expiry")
                    identifier = secrets.token_urlsafe(24)
                    directory = Path(tempfile.gettempdir()) / "argus-report-drafts"
                    directory.mkdir(mode=0o700, exist_ok=True)
                    staged, verified = await asyncio.to_thread(save_verified, make, directory / (identifier + suffix))
                    expires = time.time() + 3600
                    self._drafts[identifier] = (staged, requested, await asyncio.to_thread(_file_digest, staged), expires)
                    return {"temporary": True, "verified": verified, "draft_id": identifier, "filename": requested.name,
                            "expires_at": expires, "message": "The report draft is ready. Would you like me to save it to your laptop?"}
            path, verified = await asyncio.to_thread(save_verified, make, requested)
            result = {"verified": verified, "path": str(path), "bytes": path.stat().st_size}
        else:
            suffix = ".pptx" if method == "create_presentation" else ".docx"
            requested = _inside(self.documents, str(params.get("filename") or "argus_research"), suffix)
            render = render_presentation if suffix == ".pptx" else render_ieee_document
            result = await asyncio.to_thread(render, params, requested)
            path = Path(result["path"])
        return await self._finish_document(path, result, auto_open)

    async def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "telemetry":
            return await asyncio.to_thread(telemetry)
        if method == "open_app_or_url":
            return await asyncio.to_thread(open_app_or_url, params.get("target", ""), self.app_paths)
        if method == "execute_script":
            return await execute_script(self.workspace, params)
        if method in {"create_presentation", "create_ieee_document", "create_report", "save_report"}:
            return await self._document_rpc(method, params)
        if method == "run_workstation_action":
            action, nested = params.get("action"), params.get("params", {})
            if not isinstance(nested, dict):
                raise ValueError("params must be an object")
            aliases = {"run_script": "execute_script", "execute_script": "execute_script", "open_app": "open_app_or_url", "open_app_or_url": "open_app_or_url", "telemetry": "telemetry"}
            if action in aliases:
                return await self.dispatch(aliases[action], nested)
            if action == "open_file":
                name = _text(nested.get("filename"), "filename", 150)
                suffix = Path(name).suffix.lower()
                if suffix not in {".pptx", ".docx", ".pdf", ".txt", ".png", ".jpg"}:
                    raise ValueError("Only generated document/media files can be opened")
                path = _inside(self.documents, name, suffix)
                if not path.is_file():
                    raise FileNotFoundError(name)
                result = await asyncio.to_thread(open_app_or_url, str(path), self.app_paths)
                return {**result, "path": str(path)}
            raise ValueError("Unsupported workstation action")
        raise ValueError("Unsupported RPC method")

    async def _send(self, websocket: Any, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False)
        if len(encoded.encode("utf-8")) > MAX_RPC_MESSAGE_BYTES:
            raise ValueError("RPC response exceeds the 20 MiB frame limit")
        async with self._send_lock:
            await websocket.send(encoded)

    async def _rpc(self, websocket: Any, request: dict[str, Any], fingerprint: str) -> None:
        request_id = request["id"]
        log.info("RPC received id=%s method=%s", request_id, request["method"])
        try:
            async with self._semaphore:
                await self._send(websocket, {"type": "rpc_status", "id": request_id, "status": "running"})
                result = await self.dispatch(request["method"], request.get("params", {}))
                response = {"type": "rpc_result", "id": request_id, "ok": True, "result": result}
                log.info("RPC ACK id=%s method=%s ok=true", request_id, request["method"])
        except asyncio.CancelledError:
            response = {"type": "rpc_result", "id": request_id, "ok": False, "error": "Command cancelled"}
        except Exception as exc:
            log.warning("RPC rejected id=%s method=%s error_type=%s", request_id, request.get("method"), type(exc).__name__)
            response = {"type": "rpc_result", "id": request_id, "ok": False, "error": str(exc)[:2000], "error_type": type(exc).__name__}
        finally:
            self._jobs.pop(request_id, None)
        self._completed[request_id] = (fingerprint, response)
        self._cache_sizes[request_id] = len(json.dumps(response, ensure_ascii=False).encode("utf-8"))
        while len(self._completed) > 256 or sum(self._cache_sizes.values()) > MAX_CACHED_RESULT_BYTES:
            evicted, _ = self._completed.popitem(last=False)
            self._cache_sizes.pop(evicted, None)
        with contextlib.suppress(Exception):
            await self._send(websocket, response)

    async def _heartbeat(self, websocket: Any) -> None:
        while True:
            await self._send(websocket, {"type": "telemetry", **(await asyncio.to_thread(telemetry))})
            await asyncio.sleep(15)

    async def serve_connection(self, websocket: Any) -> None:
        await self._send(websocket, {"type": "hello", "protocol": 1, "node_id": "asus", "capabilities": list(self.METHODS),
                                     "features": {"artifact_transfer": True, "artifact_limit_bytes": MAX_ARTIFACT_BYTES, "launch_receipts": True}})
        heartbeat = asyncio.create_task(self._heartbeat(websocket))
        try:
            async for raw in websocket:
                request = None
                try:
                    request = json.loads(raw)
                    if not isinstance(request, dict):
                        continue
                    request_id = request.get("id", "")
                    if request.get("type") == "cancel":
                        if request_id in self._jobs:
                            self._jobs[request_id].cancel()
                        continue
                    if request.get("type") in {"ping", "heartbeat"}:
                        await self._send(websocket, {"type": "pong", "timestamp": time.time()})
                        continue
                    if request.get("type") != "rpc":
                        continue
                    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
                        raise ValueError("Invalid RPC ID")
                    if request.get("method") not in self.METHODS or not isinstance(request.get("params", {}), dict):
                        raise ValueError("Unsupported method or malformed params")
                    fingerprint = hashlib.sha256(json.dumps({"method": request["method"], "params": request.get("params", {})}, sort_keys=True).encode()).hexdigest()
                    if request_id in self._completed:
                        old_fingerprint, response = self._completed[request_id]
                        if old_fingerprint != fingerprint:
                            raise ValueError("An RPC ID cannot be reused with different parameters")
                        await self._send(websocket, response)
                    elif request_id in self._jobs:
                        await self._send(websocket, {"type": "rpc_status", "id": request_id, "status": "running"})
                    elif len(self._jobs) >= 8:
                        await self._send(websocket, {"type": "rpc_result", "id": request_id, "ok": False, "error": "Workstation queue is full"})
                    else:
                        self._jobs[request_id] = asyncio.create_task(self._rpc(websocket, request, fingerprint))
                except (ValueError, TypeError) as exc:
                    await self._send(websocket, {"type": "rpc_result", "id": request.get("id", "") if isinstance(request, dict) else "", "ok": False, "error": str(exc)[:500]})
        finally:
            heartbeat.cancel()
            jobs = list(self._jobs.values())
            for job in jobs:
                job.cancel()
            await asyncio.gather(heartbeat, *jobs, return_exceptions=True)


def workstation_connection(url: str, token: str):
    """Connection shared by the daemon and its loopback diagnostic."""
    from websockets.asyncio.client import connect
    return connect(validate_hub_url(url), additional_headers={"Authorization": f"Bearer {token}"},
                   proxy=None, open_timeout=12, ping_interval=20, ping_timeout=20,
                   close_timeout=5, max_size=MAX_RPC_MESSAGE_BYTES, max_queue=8)


async def run_daemon() -> None:
    if os.name != "nt":
        raise RuntimeError("Run laptop_daemon.py on the Windows workstation")
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).with_name(".env"))
    except ImportError:
        pass
    url = validate_hub_url(os.getenv("ARGUS_HUB_WS", ""))
    token = os.getenv("ARGUS_DAEMON_KEY") or os.getenv("ARGUS_WORKSTATION_TOKEN", "")
    if len(token) < 32:
        raise ValueError("Configure a shared ARGUS_DAEMON_KEY of at least 32 characters on both nodes")
    workspace = Path(os.getenv("ARGUS_WORKSPACE") or Path.home() / "Documents" / "ARGUS" / "workspace")
    documents = Path(os.getenv("ARGUS_DOCUMENTS") or Path.home() / "Documents" / "ARGUS" / "documents")
    app_paths = json.loads(os.getenv("ARGUS_APP_PATHS") or "{}")
    if not isinstance(app_paths, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in app_paths.items()):
        raise ValueError("ARGUS_APP_PATHS must be a JSON mapping of app aliases to executable paths")
    daemon = Workstation(workspace, documents, {k.lower(): v for k, v in app_paths.items()})
    delay = 1.0

    while True:
        try:
            async with workstation_connection(url, token) as websocket:
                log.info("Connected to tablet hub")
                delay = 1.0
                await daemon.serve_connection(websocket)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Hub connection unavailable (%s); reconnecting", type(exc).__name__)
        await asyncio.sleep(delay + random.random())
        delay = min(delay * 2, 30)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(run_daemon())
    except KeyboardInterrupt:
        log.info("Workstation stopped")
    except (ValueError, RuntimeError) as exc:
        log.error("Configuration error: %s", exc)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
