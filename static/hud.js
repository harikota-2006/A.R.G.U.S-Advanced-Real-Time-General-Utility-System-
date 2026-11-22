const $ = (id) => document.getElementById(id);
let socket, gateId, authConfig, microphoneStream, micNode, micSource, micSink;
let audioContext, player, reconnectTimer, authenticated = false, manualLock = false;
let currentTurn = null, binaryClause = null, answerDone = false, followUp = false;
let chart = null, mode = 'normal', reconnectAttempts = 0;
let latestCoverage = null, sessionDeadline = 0, sessionDuration = 900;
const interruptedTurns = new Set();
let lastSpeechEnd = null, timedTurn = null, timingReported = false;
function reportAudibleStart() {
  if (timingReported || lastSpeechEnd === null || timedTurn === null) return;
  timingReported = true;
  send({type:'latency',turn_id:timedTurn,speech_end_to_audio_ms:performance.now()-lastSpeechEnd});
}
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function notice(text) { $('notice').textContent = text; }
function revealConversation(transcript = false) {
  if (transcript) $('transcript-ribbon').classList.remove('hidden');
  $('response-bubble').classList.remove('hidden');
}
function syncVisualizerAudio() {
  if (audioContext) window.ARGUS?.attachAudio(audioContext, micSource || null, player?.gain || null);
}
window.addEventListener('argus-renderer-ready', () => {
  window.ARGUS.setMode(mode); window.ARGUS.setLocked(!authenticated);
  window.ARGUS.setVisualFocus(!$('visual').hidden); syncVisualizerAudio();
});
function setTelemetry(channel, label, active, percent = 0) {
  const text = $(`val-${channel}`), dot = $(`dot-${channel}`), bar = $(`bar-${channel}`);
  if (text) text.textContent = label;
  if (dot) dot.className = 'status-dot' + (active ? ' active' : '');
  if (bar) bar.style.width = `${Math.max(0, Math.min(100, Number(percent) || 0))}%`;
}
function authStatus(text) { $('auth-status').textContent = text; }
function activity(text) {
  const item = document.createElement('li'); item.textContent = text;
  $('activity').prepend(item);
  while ($('activity').children.length > 7) $('activity').lastChild.remove();
}
async function request(path, data) {
  const response = await fetch(path, {method: data === undefined ? 'GET' : 'POST',
    credentials: 'same-origin', headers: {'Content-Type': 'application/json'},
    ...(data === undefined ? {} : {body: JSON.stringify(data)})});
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(typeof payload.detail === 'string' ? payload.detail : `Request failed (${response.status})`);
    error.status = response.status; throw error;
  }
  return payload;
}
async function recordPhrase() {
  await ensureAudio();
  const stream = await navigator.mediaDevices.getUserMedia({audio: {echoCancellation:true, noiseSuppression:true, channelCount:1}});
  let source, node, mute, timer;
  try {
    await audioContext.audioWorklet.addModule('/static/pcm-worklet.js');
    source = audioContext.createMediaStreamSource(stream);
    node = new AudioWorkletNode(audioContext, 'argus-pcm');
    mute = audioContext.createGain(); mute.gain.value = 0;
    const frames = []; let voiced = false;
    authStatus('Listening. Say the complete phrase.');
    await new Promise((resolve) => {
      timer = setTimeout(resolve, 10000);
      node.port.onmessage = ({data}) => {
        if (data.type === 'pcm' && frames.length < 500) frames.push(data.buffer);
        if (data.type === 'speech_start') voiced = true;
        if (data.type === 'speech_end' && voiced && frames.length >= 100) {
          clearTimeout(timer); timer = setTimeout(resolve, 650);
        }
        if (data.type === 'speech_start' && voiced) {
          clearTimeout(timer); timer = setTimeout(resolve, Math.max(50, 10000 - frames.length * 20));
        }
      };
      source.connect(node); node.connect(mute); mute.connect(audioContext.destination);
    });
    if (!voiced || frames.length < 20) throw new Error('Speech was not detected. Try again.');
    return new Blob(frames, {type:'application/octet-stream'});
  } finally {
    clearTimeout(timer); stream.getTracks().forEach(t => t.stop());
    if (node) node.port.onmessage = null;
    source?.disconnect(); node?.disconnect(); mute?.disconnect();
  }
}
async function speakGate(text) {
  if (!window.speechSynthesis || !window.SpeechSynthesisUtterance) return;
  await new Promise((resolve) => {
    const utterance = new SpeechSynthesisUtterance(text);
    const timer = setTimeout(() => { speechSynthesis.cancel(); resolve(); }, 7000);
    utterance.onend = utterance.onerror = () => { clearTimeout(timer); resolve(); };
    speechSynthesis.speak(utterance);
  });
}
async function unlockPhrase() {
  $('phrase-button').disabled = true;
  try {
    await ensureAudio();
    const {challenge_id, phrase} = await request('/auth/phrase/start', {});
    authStatus('Listen to the phrase, then repeat it when prompted.');
    await speakGate('Please say: ' + phrase);
    await sleep(300);
    const recording = await recordPhrase();
    authStatus('Checking your phrase…');
    const response = await fetch('/auth/phrase/verify?challenge_id=' + encodeURIComponent(challenge_id),
      {method:'POST', credentials:'same-origin', body:recording});
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || 'Phrase verification failed.');
    gateId = result.gate_id;
    $('phrase-stage').hidden = true; $('pin-stage').hidden = false;
    $('gate-phrase').className = 'done'; $('gate-pin').className = 'current';
    authStatus('Enter your PIN.'); $('pin').focus(); void speakGate('Enter your PIN.');
  } catch (error) { authStatus(error.message); }
  finally { $('phrase-button').disabled = false; }
}
$('phrase-button').addEventListener('click', unlockPhrase);
for (const value of ['1','2','3','4','5','6','7','8','9','Clear','0','⌫']) {
  const button = document.createElement('button'); button.type = 'button'; button.textContent = value;
  button.addEventListener('click', () => {
    if (value === 'Clear') $('pin').value = '';
    else if (value === '⌫') $('pin').value = $('pin').value.slice(0, -1);
    else $('pin').value += value;
  });
  document.querySelector('.keypad').append(button);
}
$('pin-stage').addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    await request('/auth/pin', {gate_id: gateId, pin: $('pin').value}); $('pin').value = '';
    gateId = null; authenticated = true; manualLock = false;
    $('lock-screen').hidden = true; $('hud').hidden = false;
    window.ARGUS?.setLocked(false);
    window.speechSynthesis?.cancel(); connect();
    try { await startMic(); } catch (error) { stopMic(); notice(error.message); }
  } catch (error) { if (error.status === 401) showLock(error.message); else authStatus(error.message); }
});

async function ensureAudio() {
  audioContext ||= new AudioContext({latencyHint: 'interactive'});
  await audioContext.resume();
  player ||= new SpeechPlayer(audioContext, playbackComplete);
  syncVisualizerAudio();
}
class SpeechPlayer {
  constructor(context, onEmpty) {
    this.context = context; this.onEmpty = onEmpty;
    this.gain = context.createGain(); this.gain.connect(context.destination);
    this.queue = []; this.playing = false; this.epoch = 0;
  }
  start(meta) {
    const clause = {meta, chunks: [], ended: false, epoch: this.epoch, buffer: null, ready: null,
      media: null, sourceBuffer: null, appendQueue: [], endMedia: false, resolveEnd: null};
    clause.ready = new Promise((resolve) => { clause.resolveEnd = resolve; });
    if (window.MediaSource?.isTypeSupported('audio/mpeg')) {
      const mediaSource = new MediaSource();
      const element = new Audio(); element.preload = 'auto';
      const url = URL.createObjectURL(mediaSource); element.src = url;
      const source = this.context.createMediaElementSource(element); source.connect(this.gain);
      clause.media = {mediaSource, element, source, url};
      element.addEventListener('error', () => { clause.failed = true; });
      mediaSource.addEventListener('sourceopen', () => {
        if (clause.epoch !== this.epoch) return;
        try {
          clause.sourceBuffer = mediaSource.addSourceBuffer('audio/mpeg');
          clause.sourceBuffer.mode = 'sequence';
          clause.sourceBuffer.addEventListener('updateend', () => this.pump(clause));
          this.pump(clause);
        } catch (error) { notice(`Audio decoder: ${error.message}`); clause.media.element.dispatchEvent(new Event('error')); }
      }, {once: true});
    }
    this.queue.push(clause); this.gain.gain.setValueAtTime(1, this.context.currentTime);
    this.run(); return clause;
  }
  append(clause, bytes) {
    if (!clause || clause.epoch !== this.epoch) return;
    clause.chunks.push(bytes);
    if (clause.media) { clause.appendQueue.push(bytes); this.pump(clause); }
  }
  pump(clause) {
    const buffer = clause.sourceBuffer;
    if (!buffer || buffer.updating || clause.epoch !== this.epoch) return;
    try {
      if (clause.appendQueue.length) buffer.appendBuffer(clause.appendQueue.shift());
      else if (clause.ended && clause.media.mediaSource.readyState === 'open') clause.media.mediaSource.endOfStream();
    } catch (error) { notice(`Audio stream: ${error.message}`); clause.media.element.dispatchEvent(new Event('error')); }
  }
  end(clause) {
    if (!clause || clause.epoch !== this.epoch) return;
    clause.ended = true; clause.resolveEnd(); this.pump(clause);
    if (!clause.chunks.length && clause.media) clause.media.element.dispatchEvent(new Event('error'));
  }
  async run() {
    if (this.playing) return;
    this.playing = true; const epoch = this.epoch;
    while (this.queue.length && epoch === this.epoch) {
      const clause = this.queue[0];
      if (clause.failed || (clause.ended && !clause.chunks.length)) {
        this.release(clause); this.queue.shift(); continue;
      }
      try {
        if (clause.media) {
          await new Promise((resolve) => {
            const element = clause.media.element;
            clause.cancelPlayback = resolve;
            element.addEventListener('ended', resolve, {once: true});
            element.addEventListener('error', resolve, {once: true});
            element.addEventListener('playing', () => { if (typeof reportAudibleStart === 'function') reportAudibleStart(); }, {once:true});
            element.play().catch((error) => { notice(`Tap Enable microphone to allow audio: ${error.message}`); resolve(); });
          });
        } else {
          await clause.ready;
          if (epoch !== this.epoch) break;
          const blob = new Blob(clause.chunks, {type: 'audio/mpeg'});
          if (blob.size) {
            const buffer = await this.context.decodeAudioData(await blob.arrayBuffer());
            if (epoch !== this.epoch) break;
            await new Promise((resolve) => {
              const source = this.context.createBufferSource(); source.buffer = buffer; source.connect(this.gain);
                clause.buffer = source; clause.cancelPlayback = resolve; source.onended = resolve; source.start();
                if (typeof reportAudibleStart === 'function') reportAudibleStart();
            });
          }
        }
      } catch (error) { notice(`Audio unavailable; the answer remains on screen. ${error.message}`); }
      if (epoch !== this.epoch) break;
      this.release(clause); this.queue.shift();
    }
    if (epoch === this.epoch) { this.playing = false; this.onEmpty(); }
  }
  release(clause) {
    clause.resolveEnd?.(); clause.cancelPlayback?.();
    try { clause.buffer?.stop(); clause.buffer?.disconnect(); } catch {}
    if (clause.media) {
      clause.media.element.pause(); clause.media.element.removeAttribute('src'); clause.media.element.load();
      clause.media.source.disconnect(); URL.revokeObjectURL(clause.media.url);
    }
    clause.chunks.length = 0; clause.appendQueue.length = 0;
  }
  stop() {
    // Synchronous local mute happens before any network control message.
    this.gain.gain.cancelScheduledValues(this.context.currentTime);
    this.gain.gain.setValueAtTime(0, this.context.currentTime);
    this.epoch++;
    for (const clause of this.queue) this.release(clause);
    this.queue = []; this.playing = false;
  }
}
function send(message) { if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(message)); }
function setState(state) {
  if (mode === 'study' && state === 'STANDBY') state = 'LISTENING';
  $('state').textContent = state; document.body.dataset.state = state;
  $('listening-hint').textContent = state === 'LISTENING' ? 'Listening — go ahead' : state === 'SPEAKING' ? 'Speak to interrupt' : 'Say “Hey Argus” to begin';
  setTelemetry('audio', state === 'SPEAKING' ? 'Speaking' : microphoneStream ? 'Mic active' : 'Mic paused', Boolean(microphoneStream) || state === 'SPEAKING');
}
function interrupt() {
  player?.stop();
  if (currentTurn !== null) interruptedTurns.add(currentTurn);
  if (interruptedTurns.size > 100) interruptedTurns.delete(interruptedTurns.values().next().value);
  binaryClause = null; answerDone = false; followUp = true;
  send({type: 'interrupt'}); setState('LISTENING');
}
function playbackComplete() {
  if (!answerDone || player?.queue.length || currentTurn === null || interruptedTurns.has(currentTurn)) return;
  answerDone = false; send({type: 'playback_done', turn_id: currentTurn});
  setState(mode === 'study' || followUp ? 'LISTENING' : 'STANDBY');
}
async function startMic() {
  if (microphoneStream) return;
  if (!window.isSecureContext) throw new Error('Microphone requires HTTPS or localhost.');
  await ensureAudio();
  microphoneStream = await navigator.mediaDevices.getUserMedia({audio: {channelCount: 1, sampleRate: 16000,
    echoCancellation: true, noiseSuppression: true, autoGainControl: false}, video: false});
  await audioContext.audioWorklet.addModule('/static/pcm-worklet.js');
  micSource = audioContext.createMediaStreamSource(microphoneStream);
  micNode = new AudioWorkletNode(audioContext, 'argus-pcm', {numberOfInputs: 2, numberOfOutputs: 1, channelCount: 1});
  player.gain.connect(micNode, 0, 1);
  micSink = audioContext.createGain(); micSink.gain.value = 0;
  micSource.connect(micNode); micNode.connect(micSink); micSink.connect(audioContext.destination);
  syncVisualizerAudio(); setTelemetry('audio', 'Mic active', true);
  micNode.port.onmessage = ({data}) => {
    if (data.type === 'speech_end') lastSpeechEnd = performance.now();
    if (data.type === 'speech_start' && (player?.playing || document.body.dataset.state === 'SPEAKING')) interrupt();
    if (data.type === 'pcm' && socket?.readyState === WebSocket.OPEN) {
      if (socket.bufferedAmount < 64000) socket.send(data.buffer);
      else notice('Connection is slow; dropping old microphone frames to keep conversation current.');
    }
  };
  $('mic-toggle').setAttribute('aria-pressed', 'true'); $('mic-toggle').textContent = 'Microphone active';
  notice(mode === 'study' ? 'Listening. You can speak freely in Study Mode.' : 'Continuous microphone enabled. Say “Hey Argus” to begin.');
  send({type: 'audio_config', sample_rate: 16000, encoding: 'pcm_s16le'});
}
function stopMic() {
  microphoneStream?.getTracks().forEach((track) => track.stop()); microphoneStream = null;
  if (micNode && player) { try { player.gain.disconnect(micNode); } catch {} }
  micNode?.disconnect(); micSource?.disconnect(); micSink?.disconnect(); micNode = micSource = micSink = null;
  syncVisualizerAudio(); setTelemetry('audio', 'Mic paused', false);
  $('mic-toggle').setAttribute('aria-pressed', 'false'); $('mic-toggle').textContent = 'Enable microphone';
}
$('mic-toggle').addEventListener('click', async () => {
  if (microphoneStream) { stopMic(); notice('Microphone paused.'); return; }
  try { await startMic(); } catch (error) { stopMic(); notice(error.message); }
});
function connect() {
  clearTimeout(reconnectTimer);
  socket = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws`);
  socket.binaryType = 'arraybuffer';
  socket.onopen = () => {
    reconnectAttempts = 0; $('connection').textContent = 'CONNECTED'; $('connection').classList.add('online');
    activity('Base hub connected'); notice(microphoneStream ? 'Microphone ready.' : 'Connecting microphone…');
    if (microphoneStream) send({type:'audio_config', sample_rate:16000, encoding:'pcm_s16le'});
    setTelemetry('ws', 'LINKED', true, 100);
    $('online-toast').classList.remove('hidden'); $('online-toast').classList.add('visible');
    setTimeout(() => { $('online-toast').classList.remove('visible'); $('online-toast').classList.add('hidden'); }, 2500);
    request('/api/health').then(renderHealth).catch(() => {});
  };
  socket.onclose = async ({code}) => {
    $('connection').textContent = 'OFFLINE'; $('connection').classList.remove('online'); player?.stop();
    setTelemetry('ws', 'OFFLINE', false);
    if (manualLock || !authenticated) return;
    if (code === 1008 || code === 4401 || code === 4403) { showLock('Session expired. Verify your identity again.'); return; }
    // Browsers hide a pre-accept HTTP 403 as WebSocket 1006; check the session explicitly.
    try { await request('/api/health'); }
    catch (error) { if (error.status === 401) { showLock('Session expired. Verify your identity again.'); return; } }
    if (manualLock || !authenticated) return;
    if (++reconnectAttempts > 6) { notice('Hub unavailable. Reload when the connection returns.'); return; }
    reconnectTimer = setTimeout(connect, Math.min(30000, 1000 * 2 ** reconnectAttempts));
    notice('Connection interrupted. Reconnecting…');
  };
  socket.onerror = () => notice('Unable to reach the base hub.');
  socket.onmessage = (event) => {
    if (event.data instanceof ArrayBuffer) { player?.append(binaryClause, event.data); return; }
    let message; try { message = JSON.parse(event.data); } catch { return; }
    handleMessage(message);
  };
}
function handleMessage(message) {
  if (message.type === 'turn_timing') { timedTurn=message.turn_id; timingReported=false; return; }
  if (message.type === 'state') setState(message.state);
  else if (message.type === 'transcript') { $('transcript').textContent = message.text; $('response').textContent = ''; $('sources').replaceChildren(); revealConversation(true); if (mode === 'detective') $('case-status').textContent = message.text; }
  else if (message.type === 'token') {
    if (message.turn_id !== undefined && currentTurn !== message.turn_id) { currentTurn = message.turn_id; answerDone = false; $('response').textContent = ''; }
    if (!interruptedTurns.has(message.turn_id)) $('response').textContent += message.text || '';
    revealConversation();
  } else if (message.type === 'response') {
    if (interruptedTurns.has(message.turn_id)) return;
    currentTurn = message.turn_id ?? currentTurn;
    renderRichResponse(typeof message.text === 'string' && message.text.trim() ? message.text : message.payload || '');
    revealConversation();
    followUp = Boolean(message.follow_up); answerDone = true;
    if (message.sources) renderSources(message.sources);
    playbackComplete();
  } else if (message.type === 'audio_start') {
    currentTurn = message.turn_id; answerDone = false;
    binaryClause = interruptedTurns.has(message.turn_id) ? null : player?.start(message);
    if (binaryClause) setState('SPEAKING');
  } else if (message.type === 'audio_end') { player?.end(binaryClause); binaryClause = null; }
  else if (message.type === 'interrupt' || message.type === 'stop_audio') { player?.stop(); binaryClause = null; setState('LISTENING'); }
  else if (message.type === 'visual') message.kind === 'close' ? closeVisual() : renderVisual(message);
  else if (message.type === 'sources') {
    renderSources(message.sources || []);
    if (message.search_entry_point) {
      const frame = document.createElement('iframe');
      frame.title = 'Google Search suggestions';
      frame.sandbox = 'allow-popups allow-popups-to-escape-sandbox';
      frame.referrerPolicy = 'no-referrer';
      frame.srcdoc = message.search_entry_point;
      frame.style.cssText = 'width:100%;height:92px;border:0;border-radius:5px;background:white';
      $('sources').append(frame);
    }
  }
  else if (message.type === 'close_visual') closeVisual();
  else if (message.type === 'mode') setMode(message.mode);
  else if (message.type === 'coverage' || message.type === 'study_progress') renderCoverage(message.progress || message);
  else if (message.type === 'download') showDownload(message);
  else if (message.type === 'open_url') openBrowserURL(message.url);
  else if (message.type === 'session_timer') {
    sessionDuration = Math.max(1, Number(message.total_seconds) || sessionDuration);
    sessionDeadline = Date.now() + Math.max(0, Number(message.seconds_left) || 0) * 1000; renderSessionTimer();
  }
  else if (message.type === 'health') renderHealth(message.capabilities || message.health || message.data || {});
  else if (message.type === 'status') activity(message.message || message.text || 'Working…');
  else if (message.type === 'notification') { notice(message.text || message.message || ''); activity(message.text || message.message || 'Notification'); }
  else if (message.type === 'tool_status') activity(`${message.method || 'Workstation'}: ${message.status || message.message || 'working'}`);
  else if (message.type === 'workstation') { activity(`Workstation ${message.status || 'connected'}`); setTelemetry('workstation', message.status === 'offline' ? 'Offline' : 'Linked', message.status !== 'offline', message.status === 'offline' ? 0 : 100); }
  else if (message.type === 'generation_done') {
    if (message.announcement && !interruptedTurns.has(message.turn_id)) {
      currentTurn = message.turn_id; answerDone = true;
    }
    playbackComplete();
  }
  else if (message.type === 'lock') { manualLock = true; socket?.close(); showLock('Locked by voice command.'); }
  else if (message.type === 'error') { notice(message.message || message.detail || 'The request could not be completed.'); activity('Request needs attention'); }
  else if (message.type === 'expect_reply') { followUp = true; if (!player?.playing) setState('LISTENING'); }
}
function renderSources(sources) {
  $('sources').replaceChildren();
  $('case-sources').replaceChildren();
  for (const source of sources.slice(0, 12)) {
    try {
      const url = new URL(source.url);
      if (!['http:', 'https:'].includes(url.protocol)) continue;
      const link = document.createElement('a'); link.href = url.href; link.rel = 'noopener noreferrer'; link.target = '_blank';
      link.textContent = source.title || url.hostname; $('sources').append(link);
      if (mode === 'detective') $('case-sources').append(link.cloneNode(true));
    } catch {}
  }
}
function renderHealth(health) {
  $('health').replaceChildren();
  const entries = Array.isArray(health) ? health.map((item) => [item.name, item]) : Object.entries(health);
  for (const [name, value] of entries.slice(0, 9)) {
    const row = document.createElement('div'); row.className = 'health-item';
    const label = document.createElement('span'); label.textContent = name.replace(/_/g, ' ').toUpperCase();
    const status = document.createElement('strong'); status.textContent = typeof value === 'object' ? (value.status || (value.ok || value.healthy ? 'READY' : value.available ? 'RETRY' : 'OFFLINE')) : String(value);
    if (typeof value === 'object' && value.error) row.title = value.error;
    row.append(label, status); $('health').append(row);
  }
  if (health.laptop_daemon) setTelemetry('workstation', health.laptop_daemon.ok ? 'Online' : 'Offline', health.laptop_daemon.ok, health.laptop_daemon.ok ? 100 : 0);
  if (health.ntfy_rx || health.ntfy_tx) {
    const ready = Boolean(health.ntfy_rx?.ok || health.ntfy_tx?.ok);
    setTelemetry('phone', ready ? 'Bridge ready' : 'Bridge offline', ready, ready ? 100 : 0);
  }
}
function setMode(next) {
  if (mode === 'study' && next !== 'study') closeVisual();
  mode = ['normal', 'study', 'detective'].includes(next) ? next : 'normal';
  document.documentElement.dataset.mode = mode; document.body.dataset.mode = mode;
  document.documentElement.style.setProperty('--hud-theme', mode);
  $('mode-badge').textContent = mode === 'study' ? 'STUDY PARTNER' : mode === 'detective' ? 'DETECTIVE' : 'STANDARD';
  $('study-mode').textContent = mode === 'study' ? 'Jarvis mode' : 'Study partner';
  $('detective-mode').textContent = mode === 'detective' ? 'Jarvis mode' : 'Detective mode';
  $('study-widget').hidden = mode !== 'study'; $('detective-panel').hidden = mode !== 'detective';
  document.querySelector('meta[name="theme-color"]').content = '#000000';
  window.ARGUS?.setMode(mode);
  setState(mode === 'study' ? 'LISTENING' : 'STANDBY');
}
function closeVisual() { chart?.destroy(); chart = null; $('visual').hidden = true; document.body.classList.remove('has-visual', 'has-progress'); window.ARGUS?.setProgressPie(null); window.ARGUS?.setVisualFocus(false); }
function renderCoverage(data) {
  const topics = Array.isArray(data.topics) ? data.topics : [];
  const covered = Math.max(0, Number.isFinite(Number(data.covered)) ? Number(data.covered) : topics.filter((topic) => topic.covered).length);
  const remaining = Math.max(0, Number.isFinite(Number(data.remaining)) ? Number(data.remaining) : topics.length - covered);
  const suppliedPercent = data.covered_percent ?? data.percent;
  const percent = Math.max(0, Math.min(100, Number.isFinite(Number(suppliedPercent)) ? Number(suppliedPercent) : covered + remaining ? 100 * covered / (covered + remaining) : 0));
  latestCoverage = {...data, covered, remaining, covered_percent: percent, topics};
  $('study-document').textContent = data.filename || data.document_name || data.title || 'Your study session';
  $('study-progress').value = percent;
  $('study-progress-text').textContent = `${covered} covered · ${remaining} remaining · ${Number(percent.toFixed(2))}%`;
  $('study-topics').replaceChildren();
  for (const topic of topics) {
    const label = String(topic.title || topic.label || `Topic ${topic.id ?? ''}`);
    const button = document.createElement('button'); button.type = 'button';
    button.dataset.covered = String(Boolean(topic.covered));
    button.textContent = `${topic.covered ? '✓' : '○'} ${label}${topic.page ? ` · p.${topic.page}` : ''}`;
    button.addEventListener('click', () => submitCommand(`Explain ${label} from my study material.`));
    $('study-topics').append(button);
  }
  return latestCoverage;
}
function safeBrowserURL(value, allowExportPath = false) {
  if (typeof value !== 'string' || value.length > 8192) return null;
  try {
    const relative = value.startsWith('/') && !value.startsWith('//');
    if (relative && !allowExportPath) return null;
    const url = new URL(value, location.origin);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) return null;
    if (relative && (url.origin !== location.origin || !url.pathname.startsWith('/api/exports/'))) return null;
    if (!relative && !/^https?:\/\//i.test(value)) return null;
    return url;
  } catch { return null; }
}
function showDownload(message) {
  const url = safeBrowserURL(message.download_path || message.url || message.download_url, true);
  if (!url) { notice('The exported file did not include a valid download link.'); return; }
  const card = document.createElement('article'); card.className = 'download-card';
  const link = document.createElement('a'); link.href = url.href;
  link.textContent = `Download ${message.filename || 'study notes'}`;
  link.download = String(message.filename || 'argus-export'); link.rel = 'noopener noreferrer';
  const detail = document.createElement('p'); detail.textContent = message.size ? `Ready · ${(Number(message.size) / 1024).toFixed(1)} KB` : 'Ready to save on this device';
  card.append(link, detail); $('download-cards').prepend(card);
  while ($('download-cards').children.length > 5) $('download-cards').lastChild.remove();
  activity(`Export ready: ${message.filename || 'study notes'}`);
}
function openBrowserURL(value) {
  const url = safeBrowserURL(value);
  if (!url) { notice('The browser link was rejected because its address is invalid.'); return false; }
  const card = document.createElement('article'); card.className = 'download-card';
  const link = document.createElement('a'); link.href = url.href; link.target = '_blank'; link.rel = 'noopener noreferrer';
  link.textContent = `Open ${url.hostname}`;
  const hint = document.createElement('p'); hint.textContent = 'Tap to open if the browser blocks automatic opening.';
  card.append(link, hint); $('download-cards').prepend(card);
  while ($('download-cards').children.length > 5) $('download-cards').lastChild.remove();
  try { window.open(url.href, '_blank', 'noopener,noreferrer'); } catch {}
  notice(`Browser link ready: ${url.hostname}`); return true;
}
function renderSessionTimer() {
  if (!sessionDeadline || !authenticated) { $('val-timer').textContent = '--:--'; return; }
  const seconds = Math.max(0, Math.ceil((sessionDeadline - Date.now()) / 1000));
  $('val-timer').textContent = `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
  $('timer-arc').setAttribute('stroke-dashoffset', (213.6 * (1 - Math.min(1, seconds / sessionDuration))).toFixed(2));
}
function tableView(headers, rows) {
  const table = document.createElement('table');
  if (headers?.length) { const head = table.createTHead().insertRow(); for (const item of headers) { const cell = document.createElement('th'); cell.textContent = String(item); head.append(cell); } }
  const body = table.createTBody();
  for (const values of rows.slice(0, 200)) { const row = body.insertRow(); for (const value of values) row.insertCell().textContent = String(value ?? ''); }
  return table;
}
function highlightCode(code) {
  const element = document.createElement('code');
  const tokens = String(code).match(/(?:#[^\n]*|\/\/[^\n]*|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\b(?:def|class|return|if|else|elif|for|while|import|from|async|await|const|let|function|try|except|raise|True|False|None|true|false|null)\b)|[\s\S]/g) || [];
  for (const token of tokens) {
    if (token.length < 2) { element.append(document.createTextNode(token)); continue; }
    const span = document.createElement('span'); span.textContent = token;
    span.className = token.startsWith('#') || token.startsWith('//') ? 'syntax-comment' : /^['"]/.test(token) ? 'syntax-string' : 'syntax-keyword';
    element.append(span);
  }
  return element;
}
function appendRichInline(parent, text) {
  // Small Markdown subset: only explicitly created DOM nodes, never parsed HTML.
  const pattern = /\[([^\]\n]+)\]\(([^\s)]+)\)|`([^`\n]+)`|\*\*([^*\n]+)\*\*|__([^_\n]+)__|\*([^*\n]+)\*|_([^_\n]+)_|https?:\/\/[^\s<>]+/g;
  let offset = 0;
  for (const match of String(text).matchAll(pattern)) {
    parent.append(document.createTextNode(text.slice(offset, match.index)));
    const [raw, label, destination, code, boldStars, boldUnderscores, italicStars, italicUnderscores] = match;
    if (destination || /^https?:\/\//.test(raw)) {
      const address = destination || raw.replace(/[.,;!?]+$/, '');
      try {
        const url = new URL(address);
        if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) throw new Error('Unsupported link');
        const anchor = document.createElement('a'); anchor.href = url.href;
        anchor.target = '_blank'; anchor.rel = 'noopener noreferrer'; anchor.textContent = label || address;
        parent.append(anchor);
        if (!destination && address.length < raw.length) parent.append(document.createTextNode(raw.slice(address.length)));
      } catch { parent.append(document.createTextNode(raw)); }
    } else {
      const node = document.createElement(code !== undefined ? 'code' : boldStars !== undefined || boldUnderscores !== undefined ? 'strong' : 'em');
      node.textContent = code ?? boldStars ?? boldUnderscores ?? italicStars ?? italicUnderscores;
      parent.append(node);
    }
    offset = match.index + raw.length;
  }
  parent.append(document.createTextNode(text.slice(offset)));
}
function renderRichResponse(value, target = $('response')) {
  const lines = String(value).slice(0, 120000).replace(/\r\n?/g, '\n').split('\n');
  const fragment = document.createDocumentFragment();
  const fenceAt = (line) => /^\s{0,3}(`{3,}|~{3,})([\w+.-]*)\s*$/.exec(line);
  const listAt = (line) => /^\s*(?:([-*+])|(\d+)[.)])\s+(.+)$/.exec(line);
  const headingAt = (line) => /^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$/.exec(line);
  const cells = (line) => line.trim().replace(/^\||\|$/g, '').split('|').map((cell) => cell.trim());
  const tableAt = (index) => lines[index]?.includes('|') && lines[index + 1]?.includes('-')
    && cells(lines[index + 1]).every((cell) => /^:?-{3,}:?$/.test(cell));
  const ruleAt = (line) => /^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line);
  const startsBlock = (index) => !lines[index]?.trim() || fenceAt(lines[index]) || listAt(lines[index])
    || headingAt(lines[index]) || /^\s*>/.test(lines[index]) || ruleAt(lines[index]) || tableAt(index);
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) { index++; continue; }
    const fence = fenceAt(line);
    if (fence) {
      const body = []; index++;
      const closing = new RegExp(`^\\s{0,3}${fence[1][0]}{${fence[1].length},}\\s*$`);
      while (index < lines.length && !closing.test(lines[index])) body.push(lines[index++]);
      if (index < lines.length) index++;
      const pre = document.createElement('pre'); pre.className = 'answer-code';
      if (fence[2]) pre.setAttribute('aria-label', `${fence[2]} code`);
      pre.append(highlightCode(body.join('\n'))); fragment.append(pre); continue;
    }
    const heading = headingAt(line);
    if (heading) {
      const element = document.createElement(`h${Math.min(6, heading[1].length + 1)}`);
      appendRichInline(element, heading[2]); fragment.append(element); index++; continue;
    }
    if (ruleAt(line)) { fragment.append(document.createElement('hr')); index++; continue; }
    const list = listAt(line);
    if (list) {
      const ordered = Boolean(list[2]); const element = document.createElement(ordered ? 'ol' : 'ul');
      if (ordered && Number(list[2]) > 1) element.setAttribute('start', String(Number(list[2])));
      while (index < lines.length) {
        const item = listAt(lines[index]);
        if (!item || Boolean(item[2]) !== ordered) break;
        const entry = document.createElement('li'); appendRichInline(entry, item[3]); element.append(entry); index++;
      }
      fragment.append(element); continue;
    }
    if (tableAt(index)) {
      const headers = cells(line); index += 2; const rows = [];
      while (index < lines.length && lines[index].includes('|') && lines[index].trim()) rows.push(cells(lines[index++]));
      const wrapper = document.createElement('div'); wrapper.className = 'answer-table';
      wrapper.append(tableView(headers, rows)); fragment.append(wrapper); continue;
    }
    if (/^\s*>/.test(line)) {
      const quote = document.createElement('blockquote'); const body = [];
      while (index < lines.length && /^\s*>/.test(lines[index])) body.push(lines[index++].replace(/^\s*>\s?/, ''));
      appendRichInline(quote, body.join(' ')); fragment.append(quote); continue;
    }
    const paragraph = document.createElement('p'); const body = [line]; index++;
    while (index < lines.length && !startsBlock(index)) body.push(lines[index++]);
    appendRichInline(paragraph, body.join(' ')); fragment.append(paragraph);
  }
  target.replaceChildren(fragment);
}
function interactivePie(labels, values, title = 'Chart', onSelect = null) {
  const wrapper = document.createElement('div'); wrapper.className = 'chart-fallback';
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 250 250'); svg.setAttribute('role', 'group'); svg.setAttribute('aria-label', title);
  const legend = document.createElement('div'); legend.className = 'chart-legend';
  const detail = document.createElement('p'); detail.className = 'chart-detail'; detail.setAttribute('aria-live', 'polite');
  const colors = ['#FFFFFF','#687480','#00D2FF','#A791D9','#DC143C','#D1AD67'];
  const total = values.reduce((sum, value) => sum + Math.max(0, value), 0);
  const sectors = [], buttons = [];
  let angle = -Math.PI / 2;
  function select(index) {
    const percent = total ? values[index] / total * 100 : 0;
    detail.textContent = `${labels[index]}: ${values[index]} (${Number(percent.toFixed(2))}%)`;
    sectors.forEach((sector, i) => { sector.style.opacity = i === index ? '1' : '.45'; });
    buttons.forEach((button, i) => button.setAttribute('aria-pressed', String(i === index)));
    onSelect?.(index);
  }
  if (!total) {
    const empty = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    empty.setAttribute('cx', '125'); empty.setAttribute('cy', '125'); empty.setAttribute('r', '90'); empty.setAttribute('fill', '#26333b'); svg.append(empty);
    detail.textContent = 'No completed topics yet. Select a category for details.';
  }
  labels.forEach((label, index) => {
    const amount = Math.max(0, values[index]); const sweep = total ? amount / total * Math.PI * 2 : 0;
    const startX = 125 + 90 * Math.cos(angle), startY = 125 + 90 * Math.sin(angle);
    const endX = 125 + 90 * Math.cos(angle + sweep), endY = 125 + 90 * Math.sin(angle + sweep);
    const sector = document.createElementNS('http://www.w3.org/2000/svg', sweep >= Math.PI * 2 - 1e-8 ? 'circle' : 'path');
    if (sweep >= Math.PI * 2 - 1e-8) { sector.setAttribute('cx','125'); sector.setAttribute('cy','125'); sector.setAttribute('r','90'); }
    else sector.setAttribute('d', `M 125 125 L ${startX} ${startY} A 90 90 0 ${sweep > Math.PI ? 1 : 0} 1 ${endX} ${endY} Z`);
    sector.setAttribute('fill', colors[index % colors.length]); sector.setAttribute('role', 'button'); sector.setAttribute('tabindex', '0');
    sector.setAttribute('aria-label', `${label}: ${amount}`);
    sector.addEventListener('click', () => select(index)); sector.addEventListener('focus', () => select(index));
    sector.addEventListener('keydown', (event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); select(index); } });
    if (amount > 0 && total) svg.append(sector); sectors.push(sector);
    const button = document.createElement('button'); button.type = 'button'; button.textContent = `${label}: ${amount}`;
    button.style.borderLeft = `3px solid ${colors[index % colors.length]}`; button.setAttribute('aria-pressed', 'false');
    button.addEventListener('click', () => select(index)); legend.append(button); buttons.push(button); angle += sweep;
  });
  wrapper.append(svg, legend, detail);
  if (labels.length && total) select(0);
  return wrapper;
}
function renderVisual(message) {
  closeVisual(); const data = message.data || message;
  $('visual-title').textContent = message.title || data.title || 'Visual workspace'; $('visual-content').replaceChildren();
  const kind = message.kind || data.kind;
  if (kind === 'code') { const pre = document.createElement('pre'); pre.append(highlightCode(data.code || data.text || '')); $('visual-content').append(pre); }
  else if (kind === 'table') $('visual-content').append(tableView(data.headers || data.columns || [], data.rows || []));
  else if (['bar','pie','line'].includes(kind)) {
    const labels = (data.labels || []).slice(0, 60);
    const values = (data.values || data.datasets?.[0]?.data || []).slice(0, labels.length).map(Number);
    const valid = values.length === labels.length && values.every((value) => Number.isFinite(value) && value >= 0);
    if (!valid || !values.length) { const note = document.createElement('p'); note.textContent = 'No valid chart data is available.'; $('visual-content').append(note); }
    else if (kind === 'pie' && mode === 'study' && labels.length === 2 && /^covered/i.test(labels[0]) && /^remaining/i.test(labels[1])) {
      const topicsList = document.createElement('ul'); topicsList.className = 'sector-topics';
      const pie = interactivePie(labels, values, message.title, (index) => {
        window.ARGUS?.selectProgressSector(index);
        topicsList.replaceChildren();
        for (const topic of latestCoverage?.topics || []) {
          if (Boolean(topic.covered) !== (index === 0)) continue;
          const item = document.createElement('li'); item.textContent = topic.title || topic.label;
          topicsList.append(item);
        }
      });
      pie.classList.add('progress-pie'); pie.append(topicsList);
      $('visual-content').append(pie); document.body.classList.add('has-progress');
      const fraction = values[0] / (values[0] + values[1] || 1);
      if (values[0] + values[1] > 0) {
        const activate = () => { if (pie.isConnected) window.ARGUS.setProgressPie(pie.querySelector('svg'), fraction); };
        if (window.ARGUS) activate(); else window.addEventListener('argus-renderer-ready', activate, {once:true});
      }
    } else if (window.Chart) {
      const canvas = document.createElement('canvas'); canvas.setAttribute('role', 'img'); canvas.setAttribute('aria-label', message.title || 'Chart'); $('visual-content').append(canvas);
      try { chart = new Chart(canvas, {type: kind, data: {labels, datasets: [{label: data.label || 'Value', data: values,
        backgroundColor: ['#58bad5','#78a0c2','#9fb9c8','#81bbb0','#d2b582','#bd9ed1'], borderColor: '#58bad5', borderWidth: 1}]},
        options: {responsive: true, maintainAspectRatio: true, plugins: {legend: {display: kind === 'pie'}}, animation: {duration: 450}}}); }
      catch { canvas.remove(); $('visual-content').append(kind === 'pie' ? interactivePie(labels, values, message.title) : tableView(['Label','Value'], labels.map((label, i) => [label, values[i]]))); }
    } else $('visual-content').append(kind === 'pie' ? interactivePie(labels, values, message.title) : tableView(['Label','Value'], labels.map((label, i) => [label, values[i]])));
  } else { const text = document.createElement('p'); text.textContent = data.text || ''; $('visual-content').append(text); }
  $('visual').hidden = false; document.body.classList.add('has-visual');
  window.ARGUS?.setVisualFocus(true);
}
function submitCommand(text) {
  if (!text.trim()) return;
  if (socket?.readyState !== WebSocket.OPEN) { notice('Connect to the hub before sending a command.'); return; }
  interrupt(); $('transcript').textContent = text; $('response').textContent = ''; $('sources').replaceChildren();
  revealConversation(true); if (mode === 'detective') $('case-status').textContent = text;
  if (/^(close|hide) (the )?(chart|visual|card)/i.test(text)) closeVisual();
  send({type: 'command', text}); setState('THINKING');
}
$('command-form').addEventListener('submit', (event) => { event.preventDefault(); submitCommand($('command').value); $('command').value = ''; });
$('stop').addEventListener('click', interrupt);
$('close-visual').addEventListener('click', () => { closeVisual(); send({type: 'command', text: 'close chart'}); });
$('study-mode').addEventListener('click', () => submitCommand(mode === 'study' ? 'Jarvis mode' : 'Study Partner Mode'));
$('detective-mode').addEventListener('click', () => submitCommand(mode === 'detective' ? 'Jarvis mode' : 'Detective mode'));
$('study-progress-chart').addEventListener('click', () => {
  if (!latestCoverage) { notice('Upload study material first to track coverage.'); return; }
  renderVisual({kind: 'pie', title: 'Study coverage', labels: ['Covered', 'Remaining'], values: [latestCoverage.covered, latestCoverage.remaining]});
});
$('export-study').addEventListener('click', () => submitCommand('Export my study notes as a PDF'));
$('pdf-upload').addEventListener('change', async (event) => {
  const file = event.target.files[0]; if (!file) return;
  if (file.size > 15 * 1024 * 1024) { notice('Study material is too large. Maximum upload is 15 MB.'); return; }
  try {
    notice('Reading your study material…');
    const response = await fetch('/api/material', {method: 'POST', credentials: 'same-origin', headers: {'Content-Type': file.type || 'application/octet-stream', 'X-Filename': encodeURIComponent(file.name)}, body: file});
    const result = await response.json(); if (!response.ok) throw new Error(result.detail || 'Material upload failed');
    setMode('study'); activity(`Study material added: ${file.name}`); notice('Study Partner Mode is ready. Ask a question about your material.');
  } catch (error) { notice(error.message); }
  finally { event.target.value = ''; }
});
function showLock(message = '') {
  authenticated = false; clearTimeout(reconnectTimer); stopMic(); player?.stop(); 
  if (socket?.readyState === WebSocket.OPEN) socket.close();
  $('hud').hidden = true; $('lock-screen').hidden = false;
  window.ARGUS?.setLocked(true); closeVisual(); sessionDeadline = 0;
  $('phrase-stage').hidden = false; $('pin-stage').hidden = true;
  $('gate-phrase').className = 'current'; $('gate-pin').className = '';
  gateId = null; $('pin').value = ''; authStatus(message);
}
$('lock').addEventListener('click', async () => {
  manualLock = true; clearTimeout(reconnectTimer); socket?.close(); showLock('Locked.');
  try { await request('/auth/logout', {}); } catch (error) { authStatus(error.message); }
});
$('fullscreen').addEventListener('click', async () => {
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else if (document.documentElement.requestFullscreen) await document.documentElement.requestFullscreen({navigationUI: 'hide'});
    else notice('Full screen is unavailable in this browser. Install the PWA for a larger display.');
  } catch { notice('The browser could not enter full screen. You can continue using this window.'); }
});
document.addEventListener('fullscreenchange', () => {
  const active = Boolean(document.fullscreenElement);
  $('fullscreen').setAttribute('aria-label', active ? 'Exit full screen' : 'Enter full screen');
  $('fullscreen').setAttribute('aria-pressed', String(active));
});
function tickClock() { $('clock').textContent = new Intl.DateTimeFormat('en-IN', {timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit', hour12: false}).format(new Date()) + ' IST'; renderSessionTimer(); }
tickClock(); setInterval(tickClock, 1000);
// Local assets support offline use; a missing chart library falls back to tables.
function loadChartScript(url) {
  return new Promise((resolve, reject) => {
    const script = document.createElement('script'); script.src = url;
    script.onload = resolve; script.onerror = reject; document.head.append(script);
  });
}
loadChartScript('/static/vendor/chart.umd.min.js')
  .catch(() => loadChartScript('https://cdn.jsdelivr.net/npm/chart.js@4.4.8/dist/chart.umd.min.js'))
  .catch(() => {});
try {
  authConfig = await request('/auth/config');
  $('phrase-button').disabled = !authConfig.configured;
  if (!authConfig.configured) authStatus('Run setup_config.py on the tablet first.');
  if ('serviceWorker' in navigator && window.isSecureContext) navigator.serviceWorker.register('/sw.js').catch(() => {});
} catch (error) { authStatus(error.message); }
