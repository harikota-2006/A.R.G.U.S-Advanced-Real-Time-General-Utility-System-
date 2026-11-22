"""Workstation-only report rendering, format verification, and collision-safe publication."""
from __future__ import annotations

from contextlib import suppress
import os
from pathlib import Path
import shutil
import tempfile
from xml.sax.saxutils import escape
from zipfile import ZipFile


def text(value, label, maximum=8000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"Invalid {label}: expected nonempty text up to {maximum} characters")
    if any(ord(ch) < 32 and ch not in "\r\n\t" for ch in value):
        raise ValueError(f"{label} contains invalid control characters")
    return value.strip()


def table_data(spec, *, max_columns=8, max_rows=80, cell_limit=600):
    if not isinstance(spec, dict):
        raise ValueError("Table requires columns and rows")
    columns, rows = spec.get("columns"), spec.get("rows")
    if not isinstance(columns, list) or not 1 <= len(columns) <= max_columns:
        raise ValueError(f"A table requires 1–{max_columns} columns")
    if not isinstance(rows, list) or not 1 <= len(rows) <= max_rows:
        raise ValueError(f"A table requires 1–{max_rows} rows")
    values = [[text(cell, "column heading", 120) for cell in columns]]
    for row in rows:
        if not isinstance(row, list) or len(row) != len(columns):
            raise ValueError("Table rows must match the number of columns")
        values.append([text(str(cell), "cell", cell_limit) for cell in row])
    return values


def diagram_data(value):
    if not isinstance(value, list) or not 2 <= len(value) <= 5:
        raise ValueError("A process diagram requires two to five labeled steps")
    return [text(step, "diagram label", 60) for step in value]


def validated_report(payload):
    title = text(payload.get("title") or payload.get("topic"), "report title", 300)
    sections = payload.get("sections")
    if not isinstance(sections, list) or not 1 <= len(sections) <= 60:
        raise ValueError("Provide 1–60 report sections")
    output = []
    for section in sections:
        if not isinstance(section, dict):
            raise ValueError("Each report section must be an object")
        paragraphs = section.get("paragraphs")
        if not isinstance(paragraphs, list) or not 1 <= len(paragraphs) <= 40:
            raise ValueError("Every section must have 1–40 paragraphs")
        output.append({
            "heading": text(section.get("heading"), "heading", 180),
            "paragraphs": [text(p, "paragraph") for p in paragraphs],
            "table": table_data(section["table"]) if section.get("table") else [],
            "diagram": diagram_data(section["diagram"]) if section.get("diagram") else [],
        })
    references = payload.get("references", [])
    if not isinstance(references, list) or len(references) > 100:
        raise ValueError("Invalid reference list")
    sources = []
    for entry in references:
        if isinstance(entry, str):
            sources.append(text(entry, "reference"))
        elif isinstance(entry, dict):
            source = text(entry.get("title"), "source title", 1000)
            sources.append(source + (" — " + text(entry["url"], "source URL", 4096) if entry.get("url") else ""))
        else:
            raise ValueError("Unsupported reference")
    return title, output, sources


def render_report(payload, path):
    title, sections, references = validated_report(payload)
    author = text(payload.get("author") or "Hari", "author", 200)
    intro = text(payload["intro"], "introduction") if payload.get("intro") else ""
    if path.suffix == ".docx":
        _docx(path, title, author, intro, sections, references)
    elif path.suffix == ".pdf":
        _pdf(path, title, author, intro, sections, references)
    else:
        raise ValueError("Reports support PDF and DOCX")


def _docx(path, title, author, intro, sections, references):
    from docx import Document
    from docx.shared import Inches, Pt, RGBColor
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    doc = Document()
    doc.core_properties.title, doc.core_properties.author = title, author
    body = doc.styles["Normal"]
    body.font.name, body.font.size = "Aptos", Pt(11)
    body.paragraph_format.space_after, body.paragraph_format.line_spacing = Pt(7), 1.18
    section = doc.sections[0]
    section.left_margin = section.right_margin = Inches(.92)
    section.header.paragraphs[0].text = title
    footer = section.footer.paragraphs[0]
    footer.add_run(author + "   •   ")
    page = OxmlElement("w:fldSimple")
    page.set(qn("w:instr"), "PAGE")
    footer._p.append(page)
    doc.add_heading(title, 0)
    doc.add_paragraph(author, "Subtitle")
    if intro:
        doc.add_paragraph(intro)
    for entry in sections:
        doc.add_heading(entry["heading"], 1)
        for paragraph in entry["paragraphs"]:
            doc.add_paragraph(paragraph)
        if entry["table"]:
            rows = entry["table"]
            grid = doc.add_table(rows=len(rows), cols=len(rows[0]))
            grid.style = "Light Shading Accent 1"
            grid.rows[0]._tr.get_or_add_trPr().append(OxmlElement("w:tblHeader"))
            for cells, values in zip(grid.rows, rows):
                for cell, value in zip(cells.cells, values):
                    cell.text = value
        if entry["diagram"]:
            steps = entry["diagram"]
            grid = doc.add_table(rows=1, cols=len(steps))
            grid.style = "Light Shading Accent 1"
            for n, (cell, label) in enumerate(zip(grid.rows[0].cells, steps), 1):
                run = cell.paragraphs[0].add_run(f"{n}\n{label}")
                run.bold = True
                run.font.color.rgb = RGBColor.from_string("175D6A")
    if references:
        doc.add_heading("Sources", 1)
        for index, source in enumerate(references, 1):
            doc.add_paragraph(f"[{index}] {source}")
    doc.save(path)


def _pdf(path, title, author, intro, sections, references):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, TableStyle, Spacer
    from reportlab import rl_config
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    # ReportLab ships Bitstream Vera; embed it for portable punctuation and Latin text.
    for name, filename in (("ArgusBody", "Vera.ttf"), ("ArgusBold", "VeraBd.ttf")):
        if name not in pdfmetrics.getRegisteredFontNames():
            font = next((Path(d) / filename for d in rl_config.TTFSearchPath if (Path(d) / filename).is_file()), None)
            if font:
                pdfmetrics.registerFont(TTFont(name, str(font)))
    body = "ArgusBody" if "ArgusBody" in pdfmetrics.getRegisteredFontNames() else "Helvetica"
    bold = "ArgusBold" if "ArgusBold" in pdfmetrics.getRegisteredFontNames() else "Helvetica-Bold"
    ink, accent = colors.HexColor("#183143"), colors.HexColor("#087A85")
    pdf = SimpleDocTemplate(str(path), pagesize=A4, title=title, author=author,
                            leftMargin=21*mm, rightMargin=21*mm, topMargin=23*mm, bottomMargin=21*mm)
    def para(value, size=10, *, heading=False, color=ink):
        style = ParagraphStyle("argus", fontName=bold if heading else body, fontSize=size,
                               leading=size*1.4, textColor=color, spaceAfter=6, keepWithNext=heading)
        return Paragraph(escape(value).replace("\n", "<br/>"), style)
    story = [para(title, 23, heading=True), para(author, 11, color=accent), Spacer(1, 6)]
    if intro:
        story.append(para(intro))
    for entry in sections:
        story.extend([Spacer(1, 7), para(entry["heading"], 15, heading=True, color=accent)])
        story.extend(para(p) for p in entry["paragraphs"])
        for grid in (entry["table"], [[f"{i+1}. {label}" for i, label in enumerate(entry["diagram"])]] if entry["diagram"] else []):
            if not grid:
                continue
            cells = [[para(cell, 8.5, color=colors.white if i==0 else ink) for cell in row] for i,row in enumerate(grid)]
            table = Table(cells, colWidths=[(pdf.width-12)/len(grid[0])]*len(grid[0]), repeatRows=1)
            table.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),accent),("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,colors.HexColor("#EFF6F6")]),("VALIGN",(0,0),(-1,-1),"TOP"),("BOX",(0,0),(-1,-1),.45,colors.lightgrey)]))
            story.extend([table, Spacer(1, 8)])
    if references:
        story.append(para("Sources", 15, heading=True, color=accent))
        story.extend(para(f"[{i}] {reference}", 9) for i, reference in enumerate(references, 1))
    def furniture(canvas, document):
        canvas.saveState()
        canvas.setStrokeColor(accent)
        canvas.line(21*mm, A4[1]-16*mm, A4[0]-21*mm, A4[1]-16*mm)
        canvas.setFillColor(ink)
        canvas.setFont(body, 8)
        canvas.drawRightString(A4[0]-21*mm, 12*mm, f"{author}   •   {document.page}")
        canvas.restoreState()
    pdf.build(story, onFirstPage=furniture, onLaterPages=furniture)


def verify_document(path):
    if path.suffix == ".pdf":
        from pypdf import PdfReader
        with path.open("rb") as stream:
            reader = PdfReader(stream, strict=True)
            if not reader.pages or not any((p.extract_text() or "").strip() for p in reader.pages):
                raise ValueError("Generated PDF contains no readable text")
            return {"format":"pdf", "pages":len(reader.pages)}
    with ZipFile(path) as package:
        if package.testzip():
            raise ValueError("Generated Office package failed CRC validation")
    if path.suffix == ".pptx":
        from pptx import Presentation
        slides = Presentation(path).slides
        if not slides or not all(any(shape.has_text_frame and shape.text.strip() for shape in slide.shapes) for slide in slides):
            raise ValueError("Generated slides contain no editable text")
        return {"format":"pptx", "slides":len(slides)}
    if path.suffix == ".docx":
        from docx import Document
        paragraphs = Document(path).paragraphs
        if not any(p.text.strip() for p in paragraphs):
            raise ValueError("Generated Word document contains no readable text")
        return {"format":"docx", "paragraphs":len(paragraphs)}
    raise ValueError("Unsupported document format")


def publish_document(staged, requested):
    """Publish through an exclusive claim so concurrent/existing files are never overwritten."""
    requested.parent.mkdir(parents=True, exist_ok=True)
    for number in range(1, 10001):
        destination = requested if number == 1 else requested.with_name(f"{requested.stem} ({number}){requested.suffix}")
        try:
            with destination.open("xb") as output:
                try:
                    with staged.open("rb") as source:
                        shutil.copyfileobj(source, output)
                except BaseException:
                    output.close()
                    with suppress(FileNotFoundError):
                        destination.unlink()
                    raise
            return destination
        except FileExistsError:
            continue
    raise FileExistsError("No available numbered export name")


def save_verified(save, target):
    with tempfile.TemporaryDirectory(prefix="argus-render-") as directory:
        temp = Path(directory) / ("staged" + target.suffix)
        save(temp)
        verified = verify_document(temp)
        return publish_document(temp, target), verified
