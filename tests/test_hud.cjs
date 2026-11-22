/* Node contract tests with mocked browser audio primitives (no devices required). */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(path.join(__dirname, '../static/hud.js'), 'utf8');
const playerSource = source.slice(source.indexOf('class SpeechPlayer {'), source.indexOf('function send(message)'));
const waitTick = () => new Promise((resolve) => setImmediate(resolve));

function makeDOM() {
  class Node {
    constructor(tagName = '#text', text = '') { this.tagName = tagName; this.children = []; this.attributes = {}; this.text = text; }
    set innerHTML(_) { throw new Error('Untrusted content must never be parsed as HTML'); }
    set textContent(value) { this.children = []; this.text = String(value); }
    get textContent() { return this.text + this.children.map((child) => child.textContent).join(''); }
    append(...nodes) { for (const node of nodes) { if (node.tagName === '#fragment') this.children.push(...node.children); else this.children.push(node); } }
    replaceChildren(...nodes) { this.text = ''; this.children = []; this.append(...nodes); }
    setAttribute(name, value) { this.attributes[name] = value; }
    createTHead() { const node = new Node('thead'); this.append(node); return node; }
    createTBody() { const node = new Node('tbody'); this.append(node); return node; }
    insertRow() { const node = new Node('tr'); this.append(node); return node; }
    insertCell() { const node = new Node('td'); this.append(node); return node; }
  }
  const document = {createElement: (tag) => new Node(tag), createTextNode: (text) => new Node('#text', text), createDocumentFragment: () => new Node('#fragment')};
  const context = vm.createContext({document, URL});
  vm.runInContext(source.slice(source.indexOf('function tableView('), source.indexOf('function renderVisual(')), context);
  return {Node, context, walk: (node) => { const visit = (entry) => [entry, ...entry.children.flatMap(visit)]; return visit(node); }};
}

function makeEnvironment(mse = false) {
  const events = [], urls = new Map();
  class Media extends EventTarget {
    static isTypeSupported() { return true; }
    constructor() { super(); this.readyState = 'closed'; }
    addSourceBuffer() {
      const buffer = new EventTarget(); buffer.updating = false;
      buffer.appendBuffer = (bytes) => { events.push(['append', bytes.byteLength]); buffer.updating = true;
        queueMicrotask(() => { buffer.updating = false; buffer.dispatchEvent(new Event('updateend')); }); };
      return buffer;
    }
    endOfStream() { this.readyState = 'ended'; if (this.element?.playing) queueMicrotask(() => this.element.dispatchEvent(new Event('ended'))); }
  }
  class Audio extends EventTarget {
    set src(value) { this.media = urls.get(value); if (this.media) this.media.element = this; }
    play() { this.playing = true; if (this.media?.readyState === 'ended') queueMicrotask(() => this.dispatchEvent(new Event('ended'))); return Promise.resolve(); }
    pause() { this.playing = false; }
    load() {}
    removeAttribute() {}
  }
  const audioContext = {
    currentTime: 1, destination: {},
    createGain() { return {connect() {}, gain: {cancelScheduledValues() {}, setValueAtTime(value) { events.push(['gain', value]); }}}; },
    createMediaElementSource() { return {connect() {}, disconnect() {}}; },
    async decodeAudioData(data) { events.push(['decode', data.byteLength]); return {}; },
    createBufferSource() { return {connect() {}, disconnect() {}, start() { events.push(['play']); queueMicrotask(() => this.onended?.()); }, stop() { events.push(['stop']); }}; }
  };
  const context = vm.createContext({window: {MediaSource: mse ? Media : undefined}, MediaSource: Media, Audio,
    URL: {createObjectURL(media) { const key = `mock:${urls.size}`; urls.set(key, media); queueMicrotask(() => { media.readyState = 'open'; media.dispatchEvent(new Event('sourceopen')); }); return key; }, revokeObjectURL(key) { urls.delete(key); }},
    Event, Blob, notice(text) { events.push(['notice', text]); },
    audioContext, events, queueMicrotask, console});
  vm.runInContext(playerSource + '\nglobalThis.SpeechPlayer = SpeechPlayer;', context);
  return {context, events, audioContext, Player: context.SpeechPlayer};
}

(async () => {
  {
    const env = makeEnvironment(); let emptied = 0;
    const player = new env.Player(env.audioContext, () => emptied++);
    const clause = player.start({turn_id: 1});
    player.append(clause, new Uint8Array([1,2,3]).buffer); player.end(clause);
    await waitTick(); await waitTick();
    assert.equal(player.queue.length, 0); assert.equal(emptied, 1);
    assert(env.events.some(([name]) => name === 'decode'));
    const silent = player.start({turn_id: 2}); player.end(silent);
    await waitTick(); assert.equal(player.queue.length, 0);
  }
  {
    const env = makeEnvironment(); let releaseDecode;
    env.audioContext.decodeAudioData = () => new Promise((resolve) => { releaseDecode = resolve; });
    const player = new env.Player(env.audioContext, () => {});
    const clause = player.start({turn_id: 3}); player.append(clause, new Uint8Array([1]).buffer); player.end(clause);
    await waitTick(); player.stop(); releaseDecode({}); await waitTick();
    assert.equal(player.queue.length, 0);
    assert(!env.events.some(([name]) => name === 'play'), 'stale decoded audio must never play after barge-in');
    assert.deepEqual(env.events.at(-1), ['gain', 0]);
  }
  {
    const env = makeEnvironment(true); let emptied = 0;
    const player = new env.Player(env.audioContext, () => emptied++);
    const first = player.start({turn_id: 4});
    player.append(first, new Uint8Array([1,2]).buffer);
    player.append(first, new Uint8Array([3,4,5]).buffer);
    const emptyQueued = player.start({turn_id: 4}); player.end(emptyQueued);
    player.end(first); await waitTick(); await waitTick();
    assert.equal(player.queue.length, 0);
    assert.equal(env.events.filter(([name]) => name === 'append').length, 2);
    assert.equal(emptied, 1);
  }
  {
    const context = vm.createContext({player: {queue: []}, answerDone: true, currentTurn: 7, mode: 'normal',
      interruptedTurns: new Set(), followUp: true, messages: [], states: []});
    vm.runInContext('function send(message){ messages.push(message); } function setState(state){states.push(state);}' +
      source.slice(source.indexOf('function playbackComplete()'), source.indexOf('async function startMic()')) +
      'playbackComplete(); playbackComplete();', context);
    assert.equal(context.messages.length, 1); assert.equal(context.messages[0].type, 'playback_done');
    assert.equal(context.messages[0].turn_id, 7); assert.equal(context.states[0], 'LISTENING');
  }
  {
    const events = [];
    const context = vm.createContext({player: {stop() {events.push('local_mute');}}, currentTurn: 7,
      interruptedTurns: new Set(), binaryClause: {}, answerDone: true, followUp: false,
      send() {events.push('network_interrupt');}, setState() {events.push('listening');}});
    vm.runInContext(source.slice(source.indexOf('function interrupt()'), source.indexOf('function playbackComplete()')) + 'interrupt();', context);
    assert.deepEqual(events, ['local_mute','network_interrupt','listening']);
  }
  {
    const {Node, context, walk} = makeDOM(); const target = new Node('div');
    const markdown = '# Overview\n\nA **strong** result and *emphasis*.\n\n- First\n- Second\n\n```html\n<img src=x onerror=alert(1)>\n```\n\n<img src=x onerror=alert(2)>\n\n| Key | Value |\n| --- | --- |\n| Mode | Study |';
    context.renderRichResponse(markdown, target);
    const tags = walk(target).map((node) => node.tagName);
    for (const tag of ['h2','strong','em','ul','li','pre','code','table']) assert(tags.includes(tag), `Expected ${tag} formatting`);
    assert(!tags.includes('img')); assert(!tags.includes('script'));
    assert(target.textContent.includes('<img src=x onerror=alert(1)>'));
    assert(target.textContent.includes('<img src=x onerror=alert(2)>'));
    const code = walk(target).find((node) => node.tagName === 'code');
    assert.equal(code.textContent, '<img src=x onerror=alert(1)>');
  }
  {
    const {Node, context, walk} = makeDOM(); const target = new Node('div');
    context.renderRichResponse('[Unsafe](javascript:alert(1)) [Data](data:text/html,test) [Safe](https://example.com/source)', target);
    const anchors = walk(target).filter((node) => node.tagName === 'a');
    assert.equal(anchors.length, 1); assert.equal(anchors[0].href, 'https://example.com/source');
    assert.equal(anchors[0].rel, 'noopener noreferrer');
    assert(target.textContent.includes('javascript:alert(1)'));
  }
  console.log('HUD contracts: 7 scenarios passed (5 audio/playback, rich DOM rendering, unsafe HTML/link isolation).');
})().catch((error) => { console.error(error); process.exitCode = 1; });
