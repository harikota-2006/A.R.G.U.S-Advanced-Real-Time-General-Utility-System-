"""Anonymous isolated educational service. Run uvicorn public.app:app."""
import asyncio
import contextlib
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import sys
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT=Path(__file__).resolve().parents[1]
STORE=Path(os.environ.get('PUBLIC_DATA_DIR','/tmp/argus-public')).resolve()
ORIGIN=os.environ.get('PUBLIC_ORIGIN','http://localhost:8080').rstrip('/')
LIMIT=15*1024*1024
slots=asyncio.Semaphore(2)
locks={}
rates={}

def folder(sid):
    return STORE/hashlib.sha256(sid.encode()).hexdigest()[:32]

def valid(sid):
    if not sid or len(sid)!=64 or any(c not in '0123456789abcdef' for c in sid):
        return False
    path=folder(sid)/'lease.json'
    if not path.is_file():
        return False
    return json.loads(path.read_text())['expires']>time.time()

def authorize(request,write=False):
    sid=request.cookies.get('study_session')
    if not valid(sid):
        raise HTTPException(401,'Start a study session')
    if write and request.headers.get('origin')!=ORIGIN:
        raise HTTPException(403,'Origin rejected')
    return sid

def throttle(key,maximum):
    now=time.monotonic()
    recent=[t for t in rates.get(key,[]) if t>now-60]
    if len(recent)>=maximum:
        raise HTTPException(429,'Please wait before trying again')
    if len(rates)>10000:
        for k in list(rates):
            if not rates[k] or rates[k][-1]<now-60:
                rates.pop(k,None)
    rates[key]=[*recent,now]

async def worker(sid,payload):
    lock=locks.setdefault(sid,asyncio.Lock())
    async with lock, slots:
        target=folder(sid)
        if sum(p.stat().st_size for p in target.rglob('*') if p.is_file())>100*1024*1024:
            raise HTTPException(413,'Session storage quota reached')
        env={k:v for k,v in os.environ.items() if k in {'PATH','SYSTEMROOT','WINDIR','TEMP','TMP','PYTHONPATH','SSL_CERT_FILE'} or k in {'GEMINI_API_KEY','GROQ_API_KEY','TAVILY_API_KEY','ELEVENLABS_API_KEY','ELEVENLABS_VOICE_ID','GEMINI_MODEL','GEMINI_SEARCH_MODEL'}}
        env.update(ARGUS_PUBLIC_DEPLOYMENT='1',ARGUS_DATA_DIR=str(target),ARGUS_DB_PATH=str(target/'profile.db'),PYTHONUNBUFFERED='1',PYTHONDONTWRITEBYTECODE='1')
        process=await asyncio.create_subprocess_exec(sys.executable,str(ROOT/'public'/'worker.py'),
            cwd=target,env=env,stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        process.stdin.write((json.dumps(payload)+'\n').encode())
        await process.stdin.drain()
        process.stdin.close()
        try:
            async with asyncio.timeout(180):
                while line:=await process.stdout.readline():
                    event=json.loads(line)
                    # Never deliver internal filesystem paths or RPC artifacts.
                    for key in ('path','artifact','original_path','workspace'):
                        event.pop(key,None)
                    if event.get('download_path'):
                        event['download_url']=event['download_path']
                    yield event
                code=await process.wait()
                if code:
                    error=await process.stderr.read()
                    raise RuntimeError('Isolated processing failed')
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

async def cleanup():
    while True:
        STORE.mkdir(parents=True,exist_ok=True)
        for path in STORE.iterdir():
            if path.is_dir() and len(path.name)==32 and all(c in '0123456789abcdef' for c in path.name):
                lease=path/'lease.json'
                if lease.is_file() and json.loads(lease.read_text())['expires']<time.time()-180:
                    # Direct, validated children only. No caller-supplied path enters removal.
                    await asyncio.to_thread(shutil.rmtree,path)
        await asyncio.sleep(60)

def jobs(sid):
    conn=sqlite3.connect(folder(sid)/'jobs.db')
    conn.execute('CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,status TEXT,request TEXT,result TEXT)')
    return conn

async def job_loop():
    while True:
        for lease in STORE.glob('*/lease.json'):
            parent=lease.parent
            ledger=parent/'jobs.db'
            if not ledger.is_file():
                continue
            with contextlib.closing(sqlite3.connect(ledger)) as conn, conn:
                row=conn.execute("SELECT id,request FROM jobs WHERE status='queued' LIMIT 1").fetchone()
                if row:
                    conn.execute("UPDATE jobs SET status='running' WHERE id=?",(row[0],))
            if row:
                try:
                    request=json.loads(row[1])
                    sid=request.pop('_session')
                    result=[e async for e in worker(sid,request)]
                    state='done'
                except asyncio.CancelledError:
                    with contextlib.closing(sqlite3.connect(ledger)) as conn, conn:
                        conn.execute("UPDATE jobs SET status='queued' WHERE id=?",(row[0],))
                    raise
                except Exception:
                    state,result='failed',[{'type':'error','message':'Document processing failed. Retry the request.'}]
                with contextlib.closing(sqlite3.connect(ledger)) as conn, conn:
                    conn.execute('UPDATE jobs SET status=?,result=? WHERE id=?',(state,json.dumps(result),row[0]))
        await asyncio.sleep(.3)

@asynccontextmanager
async def lifespan(app):
    STORE.mkdir(parents=True,exist_ok=True)
    for ledger in STORE.glob('*/jobs.db'):
        with contextlib.closing(sqlite3.connect(ledger)) as conn, conn:
            conn.execute("UPDATE jobs SET status='queued' WHERE status='running'")
    job=asyncio.create_task(cleanup())
    processor=asyncio.create_task(job_loop())
    yield
    job.cancel()
    processor.cancel()
    await asyncio.gather(job,processor,return_exceptions=True)

app=FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None)

@app.middleware('http')
async def headers(request,call_next):
    response=await call_next(request)
    response.headers.update({'X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer','X-Frame-Options':'DENY','Permissions-Policy':'camera=(), microphone=(self)','Cache-Control':'no-store'})
    return response

@app.post('/api/session')
async def session(request:Request):
    if request.headers.get('origin')!=ORIGIN:
        raise HTTPException(403,'Origin rejected')
    if valid(request.cookies.get('study_session')):
        return {'ok':True,'resumed':True}
    throttle(request.client.host,3)
    STORE.mkdir(parents=True,exist_ok=True)
    if sum(1 for p in STORE.iterdir() if p.is_dir())>=200:
        raise HTTPException(503,'Session capacity reached')
    sid=secrets.token_hex(32)
    path=folder(sid)
    path.mkdir(mode=0o700)
    (path/'lease.json').write_text(json.dumps({'expires':time.time()+86400}))
    response=JSONResponse({'ok':True,'expires_in':86400})
    response.set_cookie('study_session',sid,httponly=True,secure=ORIGIN.startswith('https:'),samesite='strict',max_age=86400)
    return response

@app.post('/api/material')
async def upload(request:Request):
    sid=authorize(request,True)
    throttle(sid,10)
    name=Path(request.headers.get('x-filename','material.pdf')).name
    if Path(name).suffix.lower() not in {'.pdf','.pptx','.docx','.txt','.md'}:
        raise HTTPException(415,'Upload a PDF, PPTX, DOCX or text file')
    body=bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body)>LIMIT:
            raise HTTPException(413,'File exceeds 15 MiB')
    # Serialize publication and worker consumption under a separate upload lock.
    async with locks.setdefault(sid+'upload',asyncio.Lock()):
        (folder(sid)/'incoming').write_bytes(body)
        events=[e async for e in worker(sid,{'action':'upload','filename':name})]
    return {'events':events}

@app.get('/api/exports/{identifier}')
async def download(identifier:str,request:Request):
    sid=authorize(request)
    if len(identifier)!=32 or any(c not in '0123456789abcdef' for c in identifier):
        raise HTTPException(404)
    db=folder(sid)/'profile.db'
    if not db.is_file():
        raise HTTPException(404)
    with contextlib.closing(sqlite3.connect(db)) as conn:
        row=conn.execute('SELECT value FROM settings WHERE key=?',('export:'+identifier,)).fetchone()
    if not row:
        raise HTTPException(404)
    meta=json.loads(row[0])
    suffix={ 'application/pdf':'.pdf','application/vnd.openxmlformats-officedocument.presentationml.presentation':'.pptx','application/vnd.openxmlformats-officedocument.wordprocessingml.document':'.docx'}.get(meta['media_type'])
    if not suffix:
        raise HTTPException(404)
    return FileResponse(folder(sid)/'exports'/(identifier+suffix),filename=meta['filename'],media_type=meta['media_type'])

@app.websocket('/ws')
async def conversation(ws:WebSocket):
    sid=ws.cookies.get('study_session')
    if ws.headers.get('origin')!=ORIGIN or not valid(sid):
        await ws.close(code=4401)
        return
    await ws.accept()
    turn=None
    async def answer(payload):
        try:
            async for event in worker(sid,payload):
                await ws.send_json(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            await ws.send_json({'type':'error','message':'Processing failed. Please retry.'})
        finally:
            await ws.send_json({'type':'done'})
    try:
        while True:
            raw=await asyncio.wait_for(ws.receive_text(),90)
            if not valid(sid) or len(raw)>900000:
                break
            payload=json.loads(raw)
            if payload.get('type')=='interrupt':
                if turn:
                    turn.cancel()
                    await asyncio.gather(turn,return_exceptions=True)
                continue
            throttle(sid+'chat',15)
            action=payload.get('type')
            if action not in {'chat','transcribe','speech'}:
                continue
            if action=='chat' and (not isinstance(payload.get('text'),str) or len(payload['text'])>8000):
                break
            if action=='speech' and (not isinstance(payload.get('text'),str) or len(payload['text'])>1500):
                break
            if turn:
                turn.cancel()
                await asyncio.gather(turn,return_exceptions=True)
            turn=asyncio.create_task(answer({**payload,'action':action}))
    except (WebSocketDisconnect,ValueError,asyncio.TimeoutError,HTTPException):
        pass
    finally:
        if turn:
            turn.cancel()
            await asyncio.gather(turn,return_exceptions=True)
        with contextlib.suppress(Exception):
            await ws.close()

@app.get('/')
async def home():
    return FileResponse(ROOT/'public'/'index.html')

@app.get('/client.js')
async def client():
    return FileResponse(ROOT/'public'/'client.js',media_type='application/javascript')

@app.post('/api/questions')
async def save_question(request:Request):
    sid=authorize(request,True)
    throttle(sid+'questions',20)
    raw=await request.body()
    if len(raw)>5000:
        raise HTTPException(413)
    try:
        question=json.loads(raw)['question']
        if not isinstance(question,str) or not 1<=len(question.strip())<=2000:
            raise ValueError()
    except (ValueError,KeyError,TypeError):
        raise HTTPException(400,'Supply a question')
    with contextlib.closing(sqlite3.connect(folder(sid)/'questions.db')) as conn, conn:
        conn.execute('CREATE TABLE IF NOT EXISTS questions(question TEXT UNIQUE,created REAL)')
        if conn.execute('SELECT count(*) FROM questions').fetchone()[0]>=200:
            raise HTTPException(429,'Question limit reached')
        conn.execute('INSERT OR IGNORE INTO questions VALUES(?,?)',(question.strip(),time.time()))
    return {'ok':True}

@app.get('/api/questions')
async def questions(request:Request):
    sid=authorize(request)
    path=folder(sid)/'questions.db'
    if not path.is_file():
        return {'questions':[]}
    with contextlib.closing(sqlite3.connect(path)) as conn:
        rows=conn.execute('SELECT question FROM questions ORDER BY created').fetchall()
    return {'questions':[row[0] for row in rows]}

@app.post('/api/jobs')
async def create_job(request:Request):
    sid=authorize(request,True)
    throttle(sid+'jobs',3)
    raw=await request.body()
    if len(raw)>9000:
        raise HTTPException(413)
    try:
        text=json.loads(raw)['text']
        if not isinstance(text,str) or not 1<=len(text)<=8000:
            raise ValueError()
    except (ValueError,TypeError,KeyError):
        raise HTTPException(400)
    with contextlib.closing(jobs(sid)) as conn, conn:
        if conn.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]>=2:
            raise HTTPException(429,'Two jobs are already queued')
        identifier=secrets.token_hex(16)
        conn.execute('INSERT INTO jobs VALUES(?,?,?,?)',(identifier,'queued',json.dumps({'action':'chat','text':text,'_session':sid}),'[]'))
    return {'id':identifier,'status':'queued'}

@app.get('/api/jobs/{identifier}')
async def get_job(identifier:str,request:Request):
    sid=authorize(request)
    with contextlib.closing(jobs(sid)) as conn:
        row=conn.execute('SELECT status,result FROM jobs WHERE id=?',(identifier,)).fetchone()
    if not row:
        raise HTTPException(404)
    return {'status':row[0],'events':json.loads(row[1])}

# Only the explicit educational asset set is exposed.
@app.get('/assets/{name}')
async def asset(name:str):
    allowed={'visualizer.js':'visualizer.js','style.css':'style.css','pcm-worklet.js':'pcm-worklet.js','three.min.js':'vendor/three.min.js'}
    if name not in allowed:
        raise HTTPException(404)
    return FileResponse(ROOT/'static'/allowed[name])
