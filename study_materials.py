"""Read study materials on the tablet with bounded ZIP/XML parsing."""
from __future__ import annotations
import io
import posixpath
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

DOC_REL = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'

def _xml(archive, member):
    raw = archive.read(member)
    if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise ValueError('Unsupported document XML')
    return ET.fromstring(raw)

def _paragraphs(node):
    return [''.join(t.text or '' for t in p.findall('.//{*}t')).strip() for p in node.findall('.//{*}p')]

def _text_units(value):
    units = []
    for physical in value.replace('\r\n', '\n').split('\f'):
        units.extend(physical[i:i+5000] for i in range(0, max(1, len(physical)), 5000))
    return units

def read_material(data, filename):
    suffix = Path(filename).suffix.lower()
    if suffix in {'.txt', '.md', '.vtt', '.srt'}:
        return _text_units(data.decode('utf-8-sig')), Path(filename).stem, 'section'
    if suffix not in {'.docx', '.pptx'}:
        raise ValueError('Upload PDF, PPTX, DOCX, Markdown, text, or a UTF-8 transcript.')
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = archive.infolist()
        if len(members)>5000 or sum(m.file_size for m in members)>100*1024*1024:
            raise ValueError('Expanded Office document exceeds the indexing limit.')
        title = Path(filename).stem
        if 'docProps/core.xml' in archive.namelist():
            title = _xml(archive,'docProps/core.xml').findtext('.//{*}title') or title
        if suffix == '.docx':
            return _text_units('\n'.join(_paragraphs(_xml(archive,'word/document.xml')))), title, 'section'
        presentation = _xml(archive,'ppt/presentation.xml')
        relations = {r.get('Id'):r for r in _xml(archive,'ppt/_rels/presentation.xml.rels')}
        slides = []
        for slide in presentation.findall('./{*}sldIdLst/{*}sldId'):
            rel = relations[slide.get(DOC_REL)]
            if rel.get('TargetMode') == 'External':
                raise ValueError('External presentation slides cannot be indexed.')
            target = rel.get('Target','')
            path = target.lstrip('/') if target.startswith('/') else posixpath.normpath(posixpath.join('ppt',target))
            if not re.fullmatch(r'ppt/slides/[^/]+\.xml', path):
                raise ValueError('Invalid slide relationship')
            paragraphs = _paragraphs(_xml(archive,path))
            folder, basename = posixpath.split(path)
            rel_path = f'{folder}/_rels/{basename}.rels'
            if rel_path in archive.NameToInfo:
                for relation in _xml(archive,rel_path):
                    if not relation.get('Type','').endswith('/notesSlide') or relation.get('TargetMode')=='External':
                        continue
                    target = relation.get('Target','')
                    note_path = target.lstrip('/') if target.startswith('/') else posixpath.normpath(posixpath.join(folder,target))
                    if not re.fullmatch(r'ppt/notesSlides/[^/]+\.xml',note_path):
                        raise ValueError('Invalid speaker-notes relationship')
                    for shape in _xml(archive,note_path).findall('.//{*}sp'):
                        placeholder = shape.find('.//{*}ph')
                        if placeholder is None or placeholder.get('type','body')=='body':
                            paragraphs.extend(_paragraphs(shape))
            slides.append('\n'.join(p for p in paragraphs if p))
        return slides, title, 'slide'

