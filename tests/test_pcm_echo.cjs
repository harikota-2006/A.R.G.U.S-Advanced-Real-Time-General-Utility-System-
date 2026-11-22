'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {performance} = require('node:perf_hooks');
const worklet = fs.readFileSync(path.join(__dirname,'../static/pcm-worklet.js'),'utf8');

function simulated({rate=16000, duration=1, delay=0, echo=0, near=()=>0, reference=true}) {
  const history = [], starts = [], pcm = [];
  let Processor, time = 0;
  const sandbox = {sampleRate:rate, AudioWorkletProcessor:class {
    constructor() {
      this.port = {postMessage(message) {
        if (message.type==='pcm') {
          const samples = new Int16Array(message.buffer);
          assert.equal(samples.length,320,'Every frame must be exactly 20 ms');
          for (const s of samples) pcm.push(s/32768);
        } else {
          history.push(message.type);
          if (message.type==='speech_start') starts.push(time);
        }
      }};
    }
  },registerProcessor(name, ctor) { assert.equal(name,'argus-pcm'); Processor=ctor; }};
  vm.runInNewContext(worklet,sandbox);
  const processor = new Processor();
  // Nonperiodic modulation distinguishes a true delay from a coincidental pitch match.
  const far = t => (t < 0 || t >= .73) ? 0 :
    (.13*Math.sin(2*Math.PI*(167*t+93*t*t)) +
     .061*Math.sin(2*Math.PI*(551*t+253*t*t)) +
     .034*Math.sin(2*Math.PI*(1235*t+144*t*t))) * (.76+.21*Math.cos(2*Math.PI*4.3*t));
  const size = Math.round(duration*rate);
  for (let start=0;start<size;start+=128) {
    const n=Math.min(128,size-start);
    const mic=new Float32Array(n), ref=new Float32Array(n), out=new Float32Array(n).fill(.7);
    for (let i=0;i<n;i++) {
      const t=(start+i)/rate;
      mic[i]=near(t)+echo*far(t-delay);
      ref[i]=far(t);
    }
    time=(start+n)/rate;
    const inputs=reference?[[mic],[ref]]:[[mic]];
    assert.equal(processor.process(inputs,[[out]]),true);
    assert.ok(out.every(v=>v===0),'Microphone output may not reach the speakers');
  }
  assert.equal(pcm.length,Math.round(duration*16000));
  return {history,starts,pcm,far};
}

function rms(pcm,start,end) {
  const rows=pcm.slice(Math.round(start*16000),Math.round(end*16000));
  return Math.sqrt(rows.reduce((sum,v)=>sum+v*v,0)/rows.length);
}
const utterance=t=>.08*Math.sin(2*Math.PI*289*t)+.03*Math.sin(2*Math.PI*(973*t+56*t*t));
const report=[];
const onset=[];
function check(name, fn) {
  const begin=performance.now();
  fn();
  report.push('[PASS] '+name+' ('+(performance.now()-begin).toFixed(1)+' ms host test time)');
}

check('Quiet capture and authentication without a reference',()=>{
  const silent=simulated({reference:false});
  assert.equal(silent.starts.length,0);
  assert.ok(silent.pcm.every(x=>x===0));
  const normal=simulated({reference:false,near:t=>t<.4?utterance(t):0});
  assert.equal(normal.starts.length,1);
  assert.ok(normal.starts[0]<=.05,'Two 20 ms windows should detect local speech');
  assert.deepEqual(normal.history,['speech_start','speech_end']);
  for(let i=0;i<6400;i++) assert.equal(normal.pcm[i],Math.round(utterance(i/16000)*32767)/32768);
});
for (const rate of [16000,44100,48000]) {
  check('Delayed speaker echo and acoustic tail at '+rate+' Hz',()=>{
    const echo=simulated({rate,delay:.0834375,echo:.57});
    assert.equal(echo.starts.length,0,'Playback should never trigger an interruption');
    const level=rms(echo.pcm,.14,.78);
    assert.ok(level<.005,'Residual echo level was '+level);
  });
}
check('Uncorrelated near speech during playback',()=>{
  const start=.28;
  const run=simulated({near:t=>t>=start&&t<.57?utterance(t):0});
  assert.equal(run.starts.length,1);
  assert.ok(run.starts[0]>=start&&run.starts[0]-start<.1);
  onset.push('distinct speech '+((run.starts[0]-start)*1000).toFixed(1)+' ms');
  for(let i=4800;i<8000;i++) assert.equal(run.pcm[i],Math.round(utterance(i/16000)*32767)/32768);
});
check('Quiet near speech survives a stronger simultaneous echo',()=>{
  const start=.28;
  const quiet=t=>t>=start&&t<.58?.024*Math.sin(2*Math.PI*397*t):0;
  const run=simulated({near:quiet,echo:.85,delay:.1251875});
  assert.equal(run.starts.length,1);
  assert.ok(run.starts[0]>=start&&run.starts[0]-start<.1,
            'Local onset relative to near voice: '+(run.starts[0]-start));
  onset.push('double-talk '+((run.starts[0]-start)*1000).toFixed(1)+' ms');
  let product=0,power=0;
  for(let i=4800;i<8900;i++) {
    const wanted=quiet(i/16000); product+=wanted*run.pcm[i]; power+=wanted*wanted;
  }
  assert.ok(product/power>.80,'Cancellation discarded the near-end utterance');
});
for (const line of report) console.log(line);
console.log('Simulated speech-onset delay: '+onset.join(', '));
