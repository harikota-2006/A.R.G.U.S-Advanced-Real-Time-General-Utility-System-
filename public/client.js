'use strict';
const $=id=>document.getElementById(id);
let socket,ctx,node,stream,current=[],voice=[],player,coverage,mode='study',chunks=[],silence=0,inSpeech=false,turn=0;
const status=text=>{$('status').textContent=text;};
function send(payload){if(socket?.readyState===1)socket.send(JSON.stringify(payload));}
function stop(){turn++;player?.pause();if(player?.src)URL.revokeObjectURL(player.src);voice=[];send({type:'interrupt'});}
function connect(){
 socket=new WebSocket(location.origin.replace('http','ws')+'/ws');
 socket.onopen=async()=>{const r=await fetch('/api/questions');if(r.ok){$('questions').replaceChildren();for(const question of (await r.json()).questions){const li=document.createElement('li');li.textContent=question;$('questions').append(li);}}status('Ready. Upload your material or ask a question.');$('upload').disabled=$('mic').disabled=false;};
 socket.onclose=()=>{status('Connection ended. Start or reconnect your session.');$('start').disabled=false;};
 socket.onmessage=({data})=>receive(JSON.parse(data));
}
async function receive(e){
 if(e.type==='token'){$('reply').textContent+=e.text;current.push(e.text);}
 if(e.type==='response'){$('reply').textContent=e.text;current=[e.text];}
 if(e.type==='error')status(e.message);
 if(e.type==='mode'){mode=e.mode;$('mode').textContent=mode.toUpperCase();window.ARGUS?.setMode(mode);}
 if(e.type==='coverage'||e.type==='study_progress')coverage=e;
 if(e.type==='visual'&&e.kind==='pie')showProgress();
 if(e.type==='download'){
  const path=e.download_path||e.download_url;
  if(path?.startsWith('/api/exports/')){const a=document.createElement('a');a.href=path;a.textContent='Download '+e.filename;a.download=e.filename;$('downloads').append(a);}
 }
 if(e.type==='transcript'&&e.text){$('text').value=e.text;submit(e.text);}
 if(e.type==='audio')voice.push(Uint8Array.from(atob(e.data),c=>c.charCodeAt(0)));
 if(e.type==='done'){
  if(voice.length){player=new Audio(URL.createObjectURL(new Blob(voice,{type:'audio/mpeg'})));voice=[];player.onended=()=>URL.revokeObjectURL(player.src);await player.play().catch(()=>status('Tap to allow audio playback.'));}
  else if(current.length){const text=current.join('');current=[];send({type:'speech',text:text.slice(0,1500)});}
 }
}
async function submit(text){
 stop();$('reply').textContent='';current=[];
 if(/\b(create|generate|make|prepare|convert)\b/i.test(text)&&/\b(presentation|pptx|powerpoint|slides?|pdf|docx|report)\b/i.test(text)){
  const response=await fetch('/api/jobs',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({text})});
  if(!response.ok){status('The job could not be queued. Please retry later.');return;}
  const {id}=await response.json();status('Document queued. You can keep this page open while it processes.');
  let attempts=0;
  while(attempts++<240){
   await new Promise(r=>setTimeout(r,1000));
   const r=await fetch('/api/jobs/'+id);if(!r.ok){status('Reconnect this session to check your document.');return;}
   const job=await r.json();
   if(['done','failed'].includes(job.status)){for(const event of job.events)await receive(event);await receive({type:'done'});status(job.status==='done'?'Processing complete.':'Generation failed. Check your sources and try again.');return;}
  }
  status('The document is still processing. Its job remains in this session.');
 } else send({type:'chat',text});
}
$('form').onsubmit=e=>{e.preventDefault();if($('text').value.trim())submit($('text').value);};
$('stop').onclick=stop;
$('start').onclick=async()=>{try{const r=await fetch('/api/session',{method:'POST'});if(!r.ok)throw Error('Cannot start session');$('start').disabled=true;await ctx?.resume();connect();}catch(e){status(e.message);}};
$('upload').onchange=async()=>{
 const f=$('upload').files[0];if(!f)return;
 status('Reading and indexing your source…');
 const r=await fetch('/api/material',{method:'POST',headers:{'x-filename':f.name},body:f});
 if(!r.ok){status('Upload failed. Check the file format, size and quota.');return;}
 for(const event of (await r.json()).events)receive(event);
 status('Source indexed. What shall we study first?');
};
function showProgress(){
 if(!coverage)return;
 $('visual').hidden=false;
 const total=coverage.total, fraction=total?coverage.covered/total:0;
 $('coverage').textContent=total?coverage.covered+' / '+total+' topics covered ('+(100*fraction).toFixed(2)+'%)':'Upload material to track progress.';
 const svg=$('visual').querySelector('svg');svg.replaceChildren();
 ['Covered','Remaining'].forEach((label,i)=>{
  const a=-Math.PI/2+(i?fraction*2*Math.PI:0),sweep=(i?1-fraction:fraction)*2*Math.PI;
  if(!sweep)return;
  const n=document.createElementNS('http://www.w3.org/2000/svg',sweep>6.28318?'circle':'path');
  if(sweep>6.28318){n.setAttribute('cx',125);n.setAttribute('cy',125);n.setAttribute('r',90);}
  else n.setAttribute('d',`M125 125 L${125+90*Math.cos(a)} ${125+90*Math.sin(a)} A90 90 0 ${sweep>Math.PI?1:0} 1 ${125+90*Math.cos(a+sweep)} ${125+90*Math.sin(a+sweep)} Z`);
  n.setAttribute('fill',i?'#687480':'#fff');n.setAttribute('tabindex',0);n.setAttribute('role','button');n.setAttribute('aria-label',label);
  const select=()=>{window.ARGUS?.selectProgressSector(i);$('topics').textContent=coverage.topics.filter(t=>Boolean(t.covered)===(i===0)).map(t=>t.title).join(' · ');};
  n.onclick=select;n.onkeydown=e=>{if(e.key==='Enter'||e.key===' ')select();};svg.append(n);
 });
 window.ARGUS?.setProgressPie(svg,fraction);
}
$('close').onclick=()=>{$('visual').hidden=true;window.ARGUS?.clearProgressPie();};
$('mic').onclick=async()=>{
 if(stream){stream.getTracks().forEach(t=>t.stop());node.disconnect();stream=null;$('mic').textContent='Enable microphone';return;}
 ctx??=new AudioContext();await ctx.resume();await ctx.audioWorklet.addModule('/assets/pcm-worklet.js');
 stream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true}});
 node=new AudioWorkletNode(ctx,'argus-pcm');const sink=ctx.createGain();sink.gain.value=0;ctx.createMediaStreamSource(stream).connect(node);node.connect(sink).connect(ctx.destination);
 node.port.onmessage=({data})=>{
  if(data.type==='speech_start'){stop();inSpeech=true;silence=0;}
  if(data.type==='speech_end')inSpeech=false;
  if(data.type==='pcm'){
   if(inSpeech||chunks.length){chunks.push(new Uint8Array(data.buffer));silence=inSpeech?0:silence+1;}
   if(chunks.length&&(silence>30||chunks.length>=700)){
    const size=chunks.reduce((n,c)=>n+c.length,0),pcm=new Uint8Array(size);let p=0;for(const c of chunks){pcm.set(c,p);p+=c.length;}chunks=[];silence=0;
    let text='';for(const b of pcm)text+=String.fromCharCode(b);send({type:'transcribe',pcm:btoa(text)});
   }
  }
 };$('mic').textContent='Pause microphone';
};
$('save-question').onclick=async()=>{
 const question=$('text').value.trim();if(!question)return;
 const r=await fetch('/api/questions',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({question})});
 if(r.ok){const li=document.createElement('li');li.textContent=question;$('questions').append(li);}
};
window.addEventListener('argus-renderer-ready',()=>{window.ARGUS.setMode('study');window.ARGUS.setLocked(false);});
