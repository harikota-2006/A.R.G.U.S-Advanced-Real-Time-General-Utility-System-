/* Continuous 20 ms mono PCM. Optional input 1 is the synchronized playback tap. */
const FRAME_SIZE = 320;
const MAX_ECHO_DELAY = 4800; // 300 ms at the 16 kHz capture rate.
const RING_MASK = 8191;
class ArgusPCM extends AudioWorkletProcessor {
  constructor() {
    super();
    this.frame = new Int16Array(320);
    this.index = 0;
    this.phase = 0;
    this.sum = 0;
    this.referenceSum = 0;
    this.weight = 0;
    this.ratio = sampleRate / 16000;
    this.noise = 0.0025;
    this.voiced = 0;
    this.quiet = 0;
    this.active = false;
    this.reference = new Float32Array(RING_MASK + 1);
    this.lowReference = new Float32Array(RING_MASK + 1);
    this.lowMic = new Float32Array(FRAME_SIZE);
    this.referenceFilter = 0;
    this.micFilter = 0;
    this.sampleClock = 0;
    this.lastPlayback = -Infinity;
    this.delay = -1;
  }
  subtractReference(delay) {
    const start = this.sampleClock - FRAME_SIZE - delay;
    let sumX = 0, sumY = 0, xy = 0, xx = 0, yy = 0;
    for (let i = 0; i < FRAME_SIZE; i++) {
      const x = this.reference[(start + i) & RING_MASK], y = this.frame[i] / 32768;
      sumX += x; sumY += y; xx += x * x; yy += y * y; xy += x * y;
    }
    // Centered normalized correlation rejects coincident changes in loudness and DC offsets.
    xx -= sumX * sumX / FRAME_SIZE;
    yy -= sumY * sumY / FRAME_SIZE;
    xy -= sumX * sumY / FRAME_SIZE;
    if (xx < 1e-5 || yy < 1e-5 || xy * xy < 0.90 * xx * yy) return false;
    const gain = xy / xx;
    // Remove only the matched component. Independent near-end speech remains in the residual.
    for (let i = 0; i < FRAME_SIZE; i++) {
      const residual = this.frame[i] - 32768 * gain * this.reference[(start + i) & RING_MASK];
      this.frame[i] = Math.max(-32768, Math.min(32767, Math.round(residual)));
    }
    this.delay = delay;
    return true;
  }
  suppressEcho() {
    if (this.delay >= 0 && this.subtractReference(this.delay)) return;
    // A sparse low-pass correlation locates the likely delay before the full-rate check.
    // Most playback frames reuse the previous delay and skip this search entirely.
    const start = this.sampleClock - FRAME_SIZE;
    const stride = 16, count = FRAME_SIZE / stride;
    let sumY = 0, yy = 0;
    for (let i = 0; i < FRAME_SIZE; i += stride) {
      const y = this.lowMic[i]; sumY += y; yy += y * y;
    }
    yy -= sumY * sumY / count;
    if (yy < 1e-5) return;
    let best = 0, bestDelay = 0;
    const limit = Math.min(MAX_ECHO_DELAY, this.sampleClock);
    for (let lag = 0; lag <= limit; lag += 8) {
      let sumX = 0, xx = 0, xy = 0;
      for (let i = 0; i < FRAME_SIZE; i += stride) {
        const x = this.lowReference[(start - lag + i) & RING_MASK];
        sumX += x; xx += x * x; xy += x * this.lowMic[i];
      }
      xx -= sumX * sumX / count;
      xy -= sumX * sumY / count;
      if (xx <= 1e-6) continue;
      const score = xy * xy / (xx * yy);
      if (score > best) { best = score; bestDelay = lag; }
    }
    if (best < 0.55) return;
    let refined = bestDelay, highest = 0;
    // Compare nearby raw alignments before subtracting to avoid accepting an off-by-one tap.
    for (let lag = Math.max(0,bestDelay-6); lag <= Math.min(MAX_ECHO_DELAY,bestDelay+6); lag++) {
      let xx = 0, xy = 0;
      for (let i = 0; i < FRAME_SIZE; i += 2) {
        const x = this.reference[(start - lag + i) & RING_MASK];
        xx += x * x; xy += x * this.frame[i];
      }
      const score = xx > 1e-6 ? xy * xy / xx : 0;
      if (score > highest) { highest = score; refined = lag; }
    }
    this.subtractReference(refined);
  }
  emitSample(value, reference) {
    const position = this.sampleClock++ & RING_MASK;
    this.reference[position] = reference;
    this.referenceFilter += 0.18 * (reference - this.referenceFilter);
    this.lowReference[position] = this.referenceFilter;
    this.micFilter += 0.18 * (value - this.micFilter);
    this.lowMic[this.index] = this.micFilter;
    if (Math.abs(reference) > 0.001) this.lastPlayback = this.sampleClock;
    this.frame[this.index++] = Math.round(Math.max(-1, Math.min(1, value)) * 32767);
    if (this.index < 320) return;
    if (this.sampleClock - this.lastPlayback <= MAX_ECHO_DELAY + FRAME_SIZE) this.suppressEcho();
    else this.delay = -1;
    let energy = 0, crossings = 0, dc = 0;
    for (const sample of this.frame) dc += sample / 32768;
    dc /= 320;
    for (let i = 0; i < 320; i++) {
      const value = this.frame[i] / 32768 - dc;
      energy += value * value;
      if (i && (this.frame[i] < 0) !== (this.frame[i - 1] < 0)) crossings++;
    }
    const rms = Math.sqrt(energy / 320);
    const speech = rms > Math.max(0.012, this.noise * 3.2) && crossings > 2 && crossings < 135;
    if (!speech) this.noise = Math.max(0.0007, Math.min(0.035, this.noise * 0.995 + rms * 0.005));
    this.voiced = speech ? this.voiced + 1 : 0;
    this.quiet = speech ? 0 : this.quiet + 1;
    if (this.voiced >= 2 && !this.active) {
      this.active = true;
      // This message precedes the audio frame on the same port: mute locally first.
      this.port.postMessage({type: 'speech_start', rms});
    }
    if (this.quiet >= 12 && this.active) {
      this.active = false;
      this.port.postMessage({type: 'speech_end'});
    }
    const bytes = this.frame.buffer;
    this.port.postMessage({type: 'pcm', buffer: bytes}, [bytes]);
    this.frame = new Int16Array(320);
    this.index = 0;
  }
  process(inputs, outputs) {
    const input = inputs[0]?.[0];
    const left = inputs[1]?.[0], right = inputs[1]?.[1];
    // Connected to a zero-gain destination so the worklet stays alive, never echo mic.
    for (const channel of outputs[0] || []) channel.fill(0);
    if (!input) return true;
    // Fractionally weighted box integration handles 44.1k and 48k input rates.
    for (let n = 0; n < input.length; n++) {
      const value = input[n];
      const reference = right ? (left[n] + right[n]) * 0.5 : left ? left[n] : 0;
      let remaining = 1;
      while (remaining > 1e-9) {
        const take = Math.min(remaining, this.ratio - this.phase);
        this.sum += value * take;
        this.referenceSum += reference * take;
        this.weight += take;
        this.phase += take;
        remaining -= take;
        if (this.phase >= this.ratio - 1e-9) {
          this.emitSample(this.sum / this.weight, this.referenceSum / this.weight);
          this.phase = this.sum = this.referenceSum = this.weight = 0;
        }
      }
    }
    return true;
  }
}
registerProcessor('argus-pcm', ArgusPCM);
