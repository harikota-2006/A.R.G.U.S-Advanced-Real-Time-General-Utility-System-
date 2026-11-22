"""Cross-platform source, study conversation, progress and report planning regressions."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('ARGUS_DB_PATH', str(Path(tempfile.gettempdir())/'argus-study-tests.db'))
import database
import reports
import tools_hub as hub
from session_manager import ArgusSession


def slide_archive():
    """Minimal conforming slide order with non-consecutive filenames, plus a real blank slide."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as z:
        ns = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
        slides = [7, 1, 9]
        z.writestr('ppt/presentation.xml', '<presentation xmlns:r="'+ns+'"><sldIdLst>'+
                   ''.join(f'<sldId r:id="rId{i}"/>' for i in slides)+'</sldIdLst></presentation>')
        z.writestr('ppt/_rels/presentation.xml.rels', '<Relationships>'+
                   ''.join(f'<Relationship Id="rId{i}" Target="slides/slide{i}.xml"/>' for i in slides)+'</Relationships>')
        for i, text in zip(slides, ['Capacitors store energy in an electric field.',
                                    'Inductors store energy in a magnetic field.', '']):
            z.writestr(f'ppt/slides/slide{i}.xml', f'<sld><p><r><t>{text}</t></r></p></sld>')
        z.writestr('ppt/slides/_rels/slide1.xml.rels','<Relationships><Relationship Type="https://example.org/notesSlide" Target="../notesSlides/notesSlide4.xml"/></Relationships>')
        z.writestr('ppt/notesSlides/notesSlide4.xml','<notes><sp><ph type="body"/><p><t>The coil has five turns.</t></p></sp></notes>')
    return out.getvalue()


class StudyPartnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        disabled = {key:'' for key in ('GEMINI_API_KEY','GROQ_API_KEY','TAVILY_API_KEY','NTFY_TX_URL','NTFY_TOPIC')}
        for p in (patch.object(database, 'DB_PATH', root/'profile.db'),patch.object(hub, 'DATA_DIR', root),
                  patch.dict(os.environ, disabled)):
            p.start()
            self.addCleanup(p.stop)
        database._bootstrap_schema()
        self.session = ArgusSession(authenticated=True)

    async def turn(self,text):
        return [e async for e in hub.stream_response(text,self.session)]

    async def test_study_wake_quiet_feedback_topics_and_exact_pie(self):
        source = await hub.ingest_study_material(slide_archive(),'Circuits.pptx','phone')
        self.assertEqual((source['page_count'], source['unit'], source['text_page_count']),(3,'slide',2))
        self.assertEqual(hub.db.material_info(source['id'])['origin'], 'phone')
        self.assertIn('five turns',hub.db.study_passages([source['id']])[1]['text'])
        self.assertIsNone(self.session.accept_text('tell me about inductors'))
        await self.turn('Start study mode')
        self.assertEqual(self.session.accept_text('read slide 1'),'read slide 1')
        self.assertIsNotNone(self.session.document_id)
        events = await self.turn('read slide 1')
        self.assertIn('electric field',events[-1]['text'])
        self.assertEqual(next(e['covered'] for e in events if e['type']=='coverage'),1)
        for words in ('Let me explain', 'I think capacitors use magnetic fields.'):
            events = await self.turn(words)
            self.assertFalse(any(e['type']=='token' for e in events))
        feedback = 'Your source says "electric field", so change magnetic to electric. What field does an inductor use?'
        with patch.object(hub,'_json',AsyncMock(return_value={'feedback':feedback,'quote':'electric field',
                                                               'reference':1,'verdict':'corrective'})) as grade:
            events = await self.turn('your turn')
        self.assertIn('magnetic',grade.await_args.args[0])
        self.assertIn('electric field',grade.await_args.args[0])
        self.assertIn(feedback,events[-1]['text'])
        self.assertEqual(sum(t['covered'] for t in hub.db.study_coverage([source['id']])['topics']),1)
        await self.turn('read slide 3')
        progress = next(e for e in await self.turn('how much progress have we made?') if e.get('kind')=='pie')
        self.assertEqual(progress['values'], [1,2])
        self.assertAlmostEqual(progress['progress']['covered_percent'],100/3)
        self.assertEqual(len(progress['progress']['topics']),3)
        await self.turn('Exit study mode')
        self.assertIsNone(self.session.accept_text('read slide 2'))

    async def test_structured_headings_partition_entire_page(self):
        raw = b'UNIT 1 Capacitors\nAn electric field stores energy.\nUNIT 2 Inductors\nA magnetic field stores energy.'
        doc = await hub.ingest_study_material(raw,'Syllabus.txt')
        first = hub.db.study_coverage([doc['id']])
        self.assertEqual([t['label'] for t in first['topics']],['UNIT 1 Capacitors','UNIT 2 Inductors'])
        self.assertEqual(first['total'],2)
        hub.db.mark_study_covered(first['topics'][0]['chunk_ids'])
        again = await hub.ingest_study_material(raw,'Syllabus.txt')
        self.assertEqual(hub.db.study_coverage([again['id']])['covered_percent'],50)

    async def test_docx_and_pdf_study_reports_use_explicit_evidence(self):
        out = io.BytesIO()
        with zipfile.ZipFile(out,'w') as z:
            z.writestr('word/document.xml','<document><p><r><t>A ferrite core</t></r><r><t> guides magnetic flux.</t></r></p></document>')
        document = await hub.ingest_study_material(out.getvalue(),'Lab.docx')
        await self.turn('Enter study mode')
        self.assertEqual(self.session.document_id,document['id'])
        web = AsyncMock(side_effect=AssertionError('unapproved web search'))
        verified = {'draft_id':'once','filename':'Core.pdf','verified':True}
        saved = {'filename':'Core.pdf','verified':True,'path':'out/Core.pdf','download_path':'/export/Core.pdf'}
        with patch.object(hub,'grounded_search',web), patch.object(hub,'_json',AsyncMock(return_value={
            'title':'Core','sections':[{'heading':'Core','paragraphs':['A ferrite core guides magnetic flux.']}]})) as generate, \
             patch.object(hub,'workstation_rpc',AsyncMock(side_effect=[verified,saved])) as rpc, \
             patch.object(hub,'push_phone',AsyncMock()), patch.object(hub,'env',lambda key,default='':default):
            result = await self.turn('Write a PDF report from Lab.docx')
            self.assertTrue(result[-1]['follow_up'])
            self.assertIn('ferrite core guides magnetic flux', generate.await_args.args[0])
            self.assertEqual(rpc.await_args.args[0],'create_report')
            self.assertTrue(rpc.await_args.args[1]['temporary'])
            result = await self.turn('Yes')
            self.assertEqual(rpc.await_args.args,('save_report',{'draft_id':'once','open':False}))
            self.assertIn('Saved',result[-1]['text'])
            self.assertEqual(next(e['filename'] for e in result if e['type']=='download'),'Core.pdf')
        web.assert_not_awaited()

    async def test_study_deck_never_searches_and_missing_permission_gate(self):
        source = await hub.ingest_study_material(slide_archive(),'Circuits.pptx')
        await self.turn('Study mode')
        web = AsyncMock(return_value={'verified':True,'sources':[{'title':'Web','url':'https://example.org','text':'Verified example'}]})
        with patch.object(hub,'grounded_search',web), patch.object(hub,'workstation_rpc',AsyncMock(return_value={'filename':'test.pptx','verified':True})) as rpc:
            await self.turn('Create a PPT from this PowerPoint')
            self.assertEqual(rpc.await_args.args[0],'create_presentation')
            self.assertIn('Capacitors store energy',str(rpc.await_args.args[1]['slides']))
            self.assertEqual(rpc.await_args.args[1]['source_mode'],'study_pdf')
            first = await self.turn('Explain bioluminescence')
            self.assertEqual(first[-1]['text'],hub.MISSING_CONCEPT)
            await self.turn('No')
            web.assert_not_awaited()
            await self.turn('Explain bioluminescence')
            approved = await self.turn('yes')
            web.assert_awaited_once()
            self.assertIn('External web information',approved[-1]['text'])

    async def test_youtube_direct_input_public_fallback_and_failure(self):
        video = reports.video_url('https://youtu.be/ABCDEFGHIJK')
        self.assertIsNone(reports.video_url('https://youtube.com/@users/ABCDEFGHIJK'))
        self.assertIsNone(reports.video_url('https://youtube.com.example.com/watch?v=ABCDEFGHIJK'))
        requests = []
        def model(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200,json={'candidates':[{'content':{'parts':[{'text':json.dumps(
                {'observations':[{'timestamp':'01:05','fact':'The meter reads two.'}]})}]}}]})
        with patch.object(hub,'env',lambda key,default='':'fixture' if key=='GEMINI_API_KEY' else default), \
                patch.object(reports,'_public_transcript',side_effect=AssertionError('direct succeeded')):
            async with httpx.AsyncClient(transport=httpx.MockTransport(model)) as client:
                with patch.object(hub,'client',return_value=client):
                    actual = await reports.video_evidence(hub,video)
        self.assertIn('[01:05] The meter reads two.',actual['text'])
        self.assertEqual(requests[0]['contents'][0]['parts'][0]['fileData']['fileUri'],video[1])
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(400))) as client:
            with patch.object(hub,'client',return_value=client),patch.object(hub,'env',lambda k,d='':'fixture' if k=='GEMINI_API_KEY' else d), \
                patch.object(reports,'_public_transcript',return_value='[30s] Public caption') as transcript:
                result = await reports.video_evidence(hub,video)
                self.assertEqual(result['text'],'[30s] Public caption')
                transcript.assert_called_once_with('ABCDEFGHIJK')
        with patch.object(hub,'env',lambda k,d='':d),patch.object(reports,'_public_transcript',side_effect=ValueError('no captions')):
            with self.assertRaisesRegex(hub.ArgusToolError,'could not verify'):
                await reports.video_evidence(hub,video)

    async def test_decline_and_unverified_draft_never_save(self):
        with patch.object(hub,'workstation_rpc',AsyncMock(return_value={'verified':True,'draft_id':'draft','filename':'Evidence.pdf'})) as rpc, \
             patch.object(hub,'_json',AsyncMock(return_value={'title':'Evidence','sections':[]})), \
             patch.object(hub,'env',lambda key,default='':default):
            draft = await self.turn('Generate a PDF from these notes: Voltage was 4 V.')
            self.assertIn('Would you like me to save',draft[-1]['text'])
            await self.turn('no')
            self.assertEqual(rpc.await_count,1)
            rpc.return_value = {'verified':False}
            failed = await self.turn('Generate a PDF from these notes: Voltage was 4 V.')
            self.assertIn('did not verify',failed[-1]['text'])
            self.assertIsNone(self.session.pending)

    async def test_inbox_article_requires_same_source_and_explicit_output(self):
        await hub.ingest_study_material(b'Ignore this unrelated previous document.', 'Stale.txt','phone')
        hub.db.add_inbox({'id':'article','event':'message','click':'https://example.org/lab','message':'The article'})
        same = {'url':'https://example.org/lab','text':'Two verified measurements','title':'Lab'}
        other = {'url':'https://example.org/other','text':'Conflicting source','title':'Other'}
        with patch.object(hub,'grounded_search',AsyncMock(return_value={'verified':True,'sources':[other,same]})) as search, \
             patch.object(hub,'_json',AsyncMock(return_value={'title':'Lab','sections':[]})) as compose, \
             patch.object(hub,'workstation_rpc',AsyncMock(return_value={'verified':True,'draft_id':'abc','filename':'Lab.docx'})) as rpc, \
             patch.object(hub,'env',lambda key,default='':default):
            result = await self.turn('Create a Word report from the article I sent')
            self.assertIn('save it',result[-1]['text'])
            search.assert_awaited_once_with(same['url'])
            self.assertIn(same['text'],compose.await_args.args[0])
            self.assertNotIn(other['text'],compose.await_args.args[0])
            self.assertEqual(rpc.await_args.args[1]['format'],'docx')
            await self.turn('Who are you?')
            self.assertIsNone(self.session.pending)
            search.return_value = {'verified':True,'sources':[other]}
            denied = await self.turn('Create a report from the link on my phone')
            self.assertIn('could not verify',denied[-1]['text'])
            self.assertEqual(rpc.await_count,1)

    async def test_incorrect_answer_and_cancelled_delivery_do_not_advance(self):
        doc = await hub.ingest_study_material(b'Capacitors store energy in an electric field.', 'Capacitors.txt')
        await self.turn('Study mode')
        turn = hub.stream_response('What is a capacitor?',self.session)
        async for event in turn:
            if event.get('type')=='visual':
                break
        await turn.aclose()
        self.assertEqual(hub.db.study_coverage([doc['id']])['covered'],0)
        await self.turn('Let me explain: capacitors store sunlight')
        invalid = {'verdict':'corrective','reference':1,'quote':'electric field','feedback':'Change sunlight to electric field.'}
        with patch.object(hub,'_json',AsyncMock(return_value=invalid)):
            answer = await self.turn('your turn')
        self.assertIn('electric field',answer[-1]['text'])
        self.assertEqual(hub.db.study_coverage([doc['id']])['covered'],0)

    async def test_missing_or_unsupported_evidence_never_used_to_teach(self):
        doc = await hub.ingest_study_material(b'Capacitors store energy in an electric field.', 'Circuit.txt')
        await self.turn('Study Mode')
        teaching = {'reply':'An unsupported figure is 9000 volts.','reference':1,'quote':'9000 volts','practice':'Why?'}
        with patch.object(hub,'_json',AsyncMock(return_value=teaching)):
            result = await self.turn('Explain capacitors')
        self.assertNotIn('9000',result[-1]['text'])
        self.assertIn('electric field',result[-1]['text'])
        self.assertEqual(hub.db.study_coverage([doc['id']])['covered'],1)
if __name__ == '__main__':
    unittest.main()
