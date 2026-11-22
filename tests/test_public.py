"""Public tenant boundaries, real source ingestion and rendering extension regressions."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import httpx

from latency_metrics import LatencyWindow

class MetricsTests(unittest.TestCase):
    def test_percentiles_bounded_and_invalid(self):
        w=LatencyWindow(capacity=100)
        for n in range(101): w.record(n)
        self.assertEqual(w.summary(),{'count':100,'median_ms':50.5,'p95_ms':95.0,'worst_ms':100.0})
        with self.assertRaises(ValueError): w.record(float('nan'))

class PublicTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from public import app
        self.module=app
        self.scratch=tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.change=patch.object(app,'STORE',Path(self.scratch.name))
        self.change.start()
        self.addCleanup(self.change.stop)
        app.rates.clear()
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app),base_url=app.ORIGIN,headers={'origin':app.ORIGIN})
        self.addAsyncCleanup(self.client.aclose)

    async def test_public_personal_routes_and_assets_are_absent(self):
        for route in ('/auth/config','/auth/phrase/start','/ws/workstation','/assets/hud.js','/assets/../server.py'):
            response=await self.client.get(route)
            self.assertEqual(response.status_code,404,route)
        self.assertEqual((await self.client.post('/api/material',content=b'x')).status_code,401)

    async def test_origin_quota_and_questions(self):
        self.assertEqual((await self.client.post('/api/session',headers={'origin':'https://evil.test'})).status_code,403)
        self.assertEqual((await self.client.post('/api/session')).status_code,200)
        self.assertTrue((await self.client.post('/api/questions',json={'question':'Explain capacitors'})).json()['ok'])
        self.assertEqual((await self.client.post('/api/material',headers={'x-filename':'run.exe'},content=b'bad')).status_code,415)
        for _ in range(2):
            self.client.cookies.clear()
            await self.client.post('/api/session')
        self.client.cookies.clear()
        self.assertEqual((await self.client.post('/api/session')).status_code,429)

    async def test_real_worker_source_isolation_and_pie(self):
        await self.client.post('/api/session')
        sid=self.client.cookies['study_session']
        response=await self.client.post('/api/material',headers={'x-filename':'circuits.txt'},content=b'Capacitors store energy in an electric field.')
        self.assertEqual(response.status_code,200,response.text)
        self.assertTrue(any(e['type']=='coverage' and e['total']>=1 for e in response.json()['events']))
        queued=await self.client.post('/api/jobs',json={'text':'how much progress have we made?'})
        identifier=queued.json()['id']
        runner=asyncio.create_task(self.module.job_loop())
        try:
            for _ in range(100):
                state=(await self.client.get('/api/jobs/'+identifier)).json()
                if state['status']=='done': break
                await asyncio.sleep(.05)
            self.assertEqual(state['status'],'done')
        finally:
            runner.cancel()
            await asyncio.gather(runner,return_exceptions=True)
        events=[e async for e in self.module.worker(sid,{'action':'chat','text':'how much progress have we made?'})]
        self.assertTrue(any(e.get('kind')=='pie' for e in events))
        denied=[e async for e in self.module.worker(sid,{'action':'chat','text':'detective mode'})]
        self.assertIn('study',denied[0]['text'])
        self.client.cookies.clear()
        await self.client.post('/api/session')
        other=self.client.cookies['study_session']
        self.assertNotEqual(self.module.folder(sid),self.module.folder(other))
        self.assertFalse((self.module.folder(other)/'profile.db').exists())
        self.assertEqual((await self.client.get('/api/exports/'+'a'*32)).status_code,404)

class ChartTests(unittest.TestCase):
    def test_editable_chart_values_and_varied_layout(self):
        from laptop_daemon import render_presentation
        from pptx import Presentation
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'chart.pptx'
            render_presentation({'topic':'Evidence','slides':[{'title':'Evidence','bullets':['Test data']},
                {'title':'Measured gains','chart':{'labels':['A','B'],'values':[2,5]}},
                {'title':'Synthesis','takeaway':'The second fixture had higher gain.'}]},target)
            deck=Presentation(target)
            charts=[s.chart for slide in deck.slides for s in slide.shapes if s.has_chart]
            self.assertEqual(list(charts[0].series[0].values),[2.,5.])
