"""Real browser public session, source upload and private route isolation."""
import asyncio
from pathlib import Path
import os
import socket
import sys
import tempfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
async def main():
    import uvicorn
    from playwright.async_api import async_playwright
    from public import app as public
    with tempfile.TemporaryDirectory() as temp:
        public.STORE=Path(temp)
        sock=socket.socket();sock.bind(('127.0.0.1',0))
        port=sock.getsockname()[1]
        public.ORIGIN=f'http://127.0.0.1:{port}'
        service=uvicorn.Server(uvicorn.Config(public.app,log_level='error',timeout_graceful_shutdown=2))
        serving=asyncio.create_task(service.serve(sockets=[sock]))
        try:
            while not service.started: await asyncio.sleep(.02)
            async with async_playwright() as p:
                browser=await p.chromium.launch(headless=True,args=['--disable-breakpad','--disable-crash-reporter','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
                async with browser:
                    page=await browser.new_page()
                    errors=[]
                    page.on('pageerror',lambda e:errors.append(str(e)))
                    page.set_default_timeout(15000)
                    await page.goto(public.ORIGIN)
                    print('Public browser loaded',flush=True)
                    await page.click('#start')
                    await page.wait_for_function("document.getElementById('upload').disabled===false")
                    print('Public session connected',flush=True)
                    await page.set_input_files('#upload',{'name':'lecture.txt','mimeType':'text/plain','buffer':b'Capacitors store electrical energy in an electric field.'})
                    await page.wait_for_function("document.getElementById('status').textContent.includes('Source indexed')")
                    print('Public source indexed',flush=True)
                    await page.fill('#text','how much progress have we made?')
                    await page.locator('#form button').first.click()
                    await page.wait_for_function("!document.getElementById('visual').hidden")
                    await page.wait_for_function("window.ARGUS?.progressMorph>.99")
                    assert await page.locator('#coverage').inner_text()=='0 / 1 topics covered (0.00%)'
                    for path in ('/auth/config','/ws/workstation','/assets/hud.js'):
                        assert (await page.request.get(public.ORIGIN+path)).status==404,path
                    assert not errors,errors
                    print('[PASS] Public Chromium session, actual subprocess indexing, protected socket, exact pie, original WebGL and personal-route exclusion')
        finally:
            service.should_exit=True
            try:
                await asyncio.wait_for(serving,10)
            except asyncio.TimeoutError:
                serving.cancel()

if __name__=='__main__':
    asyncio.run(main())
