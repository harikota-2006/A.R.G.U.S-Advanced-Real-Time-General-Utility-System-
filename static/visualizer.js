/**
 * Golden Möbius renderer restored from Source B visualizer.js SHA256
 * 253501b8ef87467b8743a9979cbfd3e3274f83cc74e257ca691ad035bed491ba.
 * The original geometry, rotation and lighting drive the normal-mode renderer.
 * Study progress adds a screen-aligned destination to the golden vertex shader.
 * Transport, authentication, recording and playback belong exclusively to hud.js.
 */
'use strict';
(async () => {
  async function loadThree(url) {
    return new Promise((resolve, reject) => {
      const script = document.createElement('script'); script.src = url;
      script.onload = resolve; script.onerror = reject; document.head.append(script);
    });
  }
  try {
    if (!window.THREE) {
      try { await loadThree('/static/vendor/three.min.js'); }
      catch { await loadThree('https://cdn.jsdelivr.net/npm/three@0.128.0/build/three.min.js'); }
    }
    const THREE = window.THREE;
const PARTICLE_COUNT      = 36_000;
const MOBIUS_WIDTH        = 1.2;
const MOBIUS_RADIUS       = 3.2;
const FFT_SIZE            = 512;
const COLOR_BLUE_HI  = new THREE.Color(0x00D2FF);
const COLOR_BLUE_MID = new THREE.Color(0x0077FE);
const MODE_COLORS = {
  normal: {hi: COLOR_BLUE_HI, mid: COLOR_BLUE_MID},
  study: {hi: new THREE.Color(0xFFFFFF), mid: new THREE.Color(0xE5E7EB)},
  detective: {hi: new THREE.Color(0xFF1E38), mid: new THREE.Color(0xDC143C)},
};
const VERTEX_SHADER = /* glsl */`
  attribute float turbulence;
  attribute float phase;
  attribute vec2 piePosition;
  uniform vec2 uPieCenter;
  uniform vec2 uPieRadius;
  uniform float uPieMorph;
  uniform float uCoveredFraction;
  uniform float uSelectedSector;
  uniform float uPiePointSize;

  uniform float uTime;
  uniform float uDiameterMod;
  uniform float uLockedDim;
  uniform vec3  uColorHi;
  uniform vec3  uColorMid;

  varying float vDepth;
  varying float vTurb;
  varying vec3  vColor;

  float hash(float n) { return fract(sin(n) * 43758.5453123); }
  float noise(float x) {
    float i = floor(x);
    float f = fract(x);
    float u = f * f * (3.0 - 2.0 * f);
    return mix(hash(i), hash(i + 1.0), u);
  }

  void main() {
    vDepth = (position.z + 6.0) / 12.0;
    vColor = mix(uColorMid, uColorHi, clamp(vDepth, 0.0, 1.0));
    vTurb  = turbulence;

    vec3 pos = position;
    pos *= 1.0 + uDiameterMod * 0.40;

    float wave = sin(uTime * 0.55 + phase * 6.2831853) * 0.06
               + cos(uTime * 0.31 + phase * 3.14159)   * 0.03;
    pos.y += wave;
    pos.x += cos(uTime * 0.40 + phase * 6.2831853) * 0.025;

    if (turbulence > 0.0) {
      float rLen   = length(pos.xz);
      vec3  radial = vec3(pos.x / max(rLen, 0.001), 0.0, pos.z / max(rLen, 0.001));
      float ripple = turbulence * sin(uTime * 8.0 + phase * 12.566);
      pos     += radial * ripple * 0.55;
      pos.y   += turbulence * sin(uTime * 11.0 + phase * 9.0) * 0.3;
    }

    pos.x += noise(phase * 17.3 + uTime * 0.7) * 0.012 - 0.006;
    pos.y += noise(phase * 23.1 + uTime * 0.5) * 0.012 - 0.006;
    pos.z += noise(phase * 31.7 + uTime * 0.6) * 0.012 - 0.006;

    vec4 mvPos   = modelViewMatrix * vec4(pos, 1.0);
    gl_Position  = projectionMatrix * mvPos;

    float dist   = -mvPos.z;
    float swell  = 1.0 + turbulence * 1.8;
    gl_PointSize = clamp((280.0 / dist) * swell, 0.8, 5.5);
    if (uPieMorph > 0.0) {
      vec2 target = uPieCenter + piePosition * uPieRadius;
      gl_Position.xy = mix(gl_Position.xy, target * gl_Position.w, uPieMorph);
      float remaining = step(uCoveredFraction, phase);
      float selected = 1.0 - abs(uSelectedSector - remaining);
      vec3 tint = mix(vec3(.67, .70, .74), vec3(.14, .22, .30), remaining);
      vColor = mix(vColor, tint * mix(.72, 1.0, selected), uPieMorph);
      vTurb *= 1.0 - uPieMorph;
      gl_PointSize = mix(gl_PointSize, uPiePointSize, uPieMorph);
    }
  }
`;

const FRAGMENT_SHADER = /* glsl */`
  uniform float uLockedDim;

  varying float vDepth;
  varying float vTurb;
  varying vec3  vColor;

  void main() {
    vec2  coord = gl_PointCoord - 0.5;
    float dist  = length(coord);
    if (dist > 0.5) discard;

    float alpha = 1.0 - smoothstep(0.18, 0.5, dist);
    alpha *= mix(0.72, 1.0, vDepth);
    alpha *= (1.0 - uLockedDim * 0.72);
    alpha  = clamp(alpha, 0.0, 1.0);

    vec3 col = vColor;
    col     += vTurb * vec3(0.18, 0.12, 0.06);
    col      = clamp(col, 0.0, 1.0);

    gl_FragColor = vec4(col, alpha);
  }
`;


class ArgusVisualizer {
  constructor() {
    this.isLocked = true; this.mode = 'normal'; this.focusVisual = false;
    this.pieElement = null; this.progressMorph = 0;
    this.micLevel = this.ttsLevel = 0; this._audioReady = false;
    this.micAnalyser = this.ttsAnalyser = this.micData = this.ttsData = null;
    this.micSource = this.ttsSource = null;
    this.clock = new THREE.Clock(); this.dom = {
      canvas: document.getElementById('argus-canvas'),
      barAudio: document.getElementById('bar-audio'),
    };
    this.initRenderer(); this.initScene(); this.buildMobiusParticles();
    window.addEventListener('resize', () => this.onResize(), {passive: true});
    this.dom.canvas.addEventListener('webglcontextlost', (event) => {
      event.preventDefault(); cancelAnimationFrame(this.frameId);
      this.pieElement?.closest('.progress-pie')?.classList.remove('live-particles');
    });
    this.dom.canvas.addEventListener('webglcontextrestored', () => this.animate());
    this.animate();
  }
  setMode(mode) {
    this.mode = Object.hasOwn(MODE_COLORS, mode) ? mode : 'normal';
    if (this.mode !== 'study') this.clearProgressPie();
  }
  setLocked(locked) { this.isLocked = Boolean(locked); }
  setVisualFocus(active) { this.focusVisual = Boolean(active); }
  clearProgressPie() { this.setProgressPie(null, 0); }
  setProgressPie(svg, fraction = 0) {
    this.pieElement?.closest('.progress-pie')?.classList.remove('live-particles');
    this.pieElement = svg;
    if (svg) this.material.uniforms.uCoveredFraction.value = Math.max(0, Math.min(1, fraction));
  }
  selectProgressSector(index) {
    this.material.uniforms.uSelectedSector.value = index === 1 ? 1 : 0;
  }
  updateProgressPie(dt, reducedMotion) {
    const uniforms = this.material.uniforms;
    let target = 0;
    if (this.pieElement?.isConnected) {
      const rect = this.pieElement.getBoundingClientRect();
      const canvas = this.dom.canvas.getBoundingClientRect();
      if (rect.width > 0 && rect.height > 0 && canvas.width > 0 && canvas.height > 0) {
        // The interactive SVG has a 250-square viewBox with r=90. Map its disk
        // into clip space so pointer hit targets and actual particles coincide.
        const radius = Math.min(rect.width, rect.height) * 90 / 250;
        const x = (rect.left + rect.width / 2 - canvas.left) / canvas.width;
        const y = (rect.top + rect.height / 2 - canvas.top) / canvas.height;
        uniforms.uPieCenter.value.set(2*x-1, 1-2*y);
        uniforms.uPieRadius.value.set(2*radius/canvas.width, 2*radius/canvas.height);
        uniforms.uPiePointSize.value = Math.max(1, radius * this.renderer.getPixelRatio() / 92);
        target = 1;
      }
    }
    const step = reducedMotion ? 1 : dt / .72;
    this.progressMorph += Math.sign(target - this.progressMorph) * Math.min(step, Math.abs(target - this.progressMorph));
    const t = this.progressMorph;
    uniforms.uPieMorph.value = t*t*(3-2*t);
    this.pieElement?.closest('.progress-pie')?.classList.toggle('live-particles', target === 1 && t > .05);
  }
  attachAudio(context, micSource, ttsSource) {
    if (this.micSource === micSource && this.ttsSource === ttsSource) return;
    for (const [source, analyser] of [[this.micSource, this.micAnalyser], [this.ttsSource, this.ttsAnalyser]]) {
      if (source && analyser) { try { source.disconnect(analyser); } catch {} }
      analyser?.disconnect();
    }
    this.micSource = micSource; this.ttsSource = ttsSource;
    this.micAnalyser = this.ttsAnalyser = this.micData = this.ttsData = null;
    this.micLevel = this.ttsLevel = 0;
    if (micSource) {
      this.micAnalyser = context.createAnalyser(); this.micAnalyser.fftSize = FFT_SIZE;
      this.micAnalyser.smoothingTimeConstant = 0.55; micSource.connect(this.micAnalyser);
      this.micData = new Uint8Array(this.micAnalyser.frequencyBinCount);
    }
    if (ttsSource) {
      this.ttsAnalyser = context.createAnalyser(); this.ttsAnalyser.fftSize = FFT_SIZE;
      this.ttsAnalyser.smoothingTimeConstant = 0.72; ttsSource.connect(this.ttsAnalyser);
      this.ttsData = new Uint8Array(this.ttsAnalyser.frequencyBinCount);
    }
    // Analysers are taps only. hud.js owns the single audible destination.
    this._audioReady = Boolean(micSource || ttsSource);
  }
  initRenderer() {
    this.renderer = new THREE.WebGLRenderer({
      canvas:          this.dom.canvas,
      antialias:       false,
      alpha:           false,
      powerPreference: 'high-performance',
    });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.setSize(window.innerWidth, window.innerHeight);
    this.renderer.setClearColor(0x000000, 1);
    this.renderer.sortObjects = false;
  }

  initScene() {
    this.scene     = new THREE.Scene();
    this.scene.fog = new THREE.FogExp2(0x000000, 0.055);
    const aspect   = window.innerWidth / window.innerHeight;
    this.camera    = new THREE.PerspectiveCamera(62, aspect, 0.1, 100);
    this.camera.position.set(0, 0, 9);
    this.camera.lookAt(0, 0, 0);
  }

  buildMobiusParticles() {
    const N = PARTICLE_COUNT;
    const R = MOBIUS_RADIUS;
    const W = MOBIUS_WIDTH;

    const positions  = new Float32Array(N * 3);
    const turbulence = new Float32Array(N);
    const phase      = new Float32Array(N);
    this.basePositions = new Float32Array(N * 3);

    for (let i = 0; i < N; i++) {
      const u   = ((i + Math.random()) / N) * Math.PI * 2.0;
      const rawV = Math.random();
      const v   = (Math.pow(rawV, 0.7) * 2.0 - 1.0) * W;

      const cu2 = Math.cos(u / 2), su2 = Math.sin(u / 2);
      const cu  = Math.cos(u),     su  = Math.sin(u);

      const x = (R + v * cu2) * cu;
      const y = (R + v * cu2) * su;
      const z = v * su2;
      const sc = 0.055;

      const idx = i * 3;
      positions[idx    ] = x + (Math.random() - 0.5) * sc;
      positions[idx + 1] = y + (Math.random() - 0.5) * sc;
      positions[idx + 2] = z + (Math.random() - 0.5) * sc;

      this.basePositions[idx    ] = x;
      this.basePositions[idx + 1] = y;
      this.basePositions[idx + 2] = z;

      turbulence[i] = 0.0;
      phase[i]      = i / N;
    }

    const geo = new THREE.BufferGeometry();
    const disk = new Float32Array(N * 2);
    for (let i = 0; i < N; i++) {
      const angle = 2 * Math.PI * i / N;
      const radius = Math.sqrt((i * .618033988749895) % 1);
      disk[i*2] = radius * Math.sin(angle);
      disk[i*2+1] = radius * Math.cos(angle);
    }
    geo.setAttribute('piePosition', new THREE.BufferAttribute(disk, 2));
    geo.setAttribute('position',   new THREE.BufferAttribute(positions,  3));
    geo.setAttribute('turbulence', new THREE.BufferAttribute(turbulence, 1));
    geo.setAttribute('phase',      new THREE.BufferAttribute(phase,      1));

    this.turbAttr  = geo.getAttribute('turbulence');
    this.phaseAttr = geo.getAttribute('phase');

    this.material = new THREE.ShaderMaterial({
      vertexShader:   VERTEX_SHADER,
      fragmentShader: FRAGMENT_SHADER,
      uniforms: {
        uPieCenter: {value: new THREE.Vector2()}, uPieRadius: {value: new THREE.Vector2()},
        uPieMorph: {value: 0}, uCoveredFraction: {value: 0},
        uSelectedSector: {value: 0}, uPiePointSize: {value: 1},
        uTime:        { value: 0.0 },
        uDiameterMod: { value: 0.0 },
        uLockedDim:   { value: 1.0 },
        uColorHi:     { value: COLOR_BLUE_HI.clone()  },
        uColorMid:    { value: COLOR_BLUE_MID.clone() },
      },
      transparent: true,
      depthWrite:  false,
      blending:    THREE.AdditiveBlending,
    });

    this.points = new THREE.Points(geo, this.material);
    this.points.frustumCulled = false;
    this.scene.add(this.points);
  }

  analyseAudio() {
    if (!this._audioReady) return;

    if (this.micAnalyser && this.micData) {
      this.micAnalyser.getByteFrequencyData(this.micData);
      const bins    = this.micData.length;
      const hiStart = Math.floor(bins * 0.35);
      let sumSq = 0, hiSum = 0;
      for (let i = 0; i < bins; i++) {
        const v = this.micData[i] / 255.0;
        sumSq  += v * v;
        if (i >= hiStart) hiSum += v;
      }
      const rms    = Math.sqrt(sumSq / bins);
      const hiNorm = hiSum / Math.max(1, bins - hiStart);
      this.micLevel = Math.min(1.0, rms * 2.2 + hiNorm * 1.4);
    }

    if (this.ttsAnalyser && this.ttsData) {
      this.ttsAnalyser.getByteTimeDomainData(this.ttsData);
      const bins = this.ttsData.length;
      let sumSq = 0;
      for (let i = 0; i < bins; i++) {
        const v = (this.ttsData[i] / 128.0) - 1.0;
        sumSq  += v * v;
      }
      this.ttsLevel = Math.min(1.0, Math.sqrt(sumSq / bins) * 3.8);
    }

    const combined = Math.max(this.micLevel, this.ttsLevel);
    this.dom.barAudio.style.width = (combined * 100).toFixed(1) + '%';
  }

  updateTurbulence(micLevel) {
    const arr   = this.turbAttr.array;
    const N     = PARTICLE_COUNT;
    const t     = this.clock.elapsedTime;
    const level = Math.max(0, micLevel - 0.08) * 1.1;

    for (let i = 0; i < N; i++) {
      const p         = this.phaseAttr.array[i];
      const ringPhase = ((t * 0.18 + p) % 1.0);
      const inRing    = Math.abs(p - ringPhase % 1.0) < 0.12;
      const target    = inRing ? level : level * 0.25;
      arr[i]         += (target - arr[i]) * 0.14;
    }
    this.turbAttr.needsUpdate = true;
  }

  updateLockedDim(dt) {
    const u      = this.material.uniforms.uLockedDim;
    const target = this.isLocked ? 1.0 : 0.0;
    u.value     += (target - u.value) * Math.min(1.0, dt * 2.5);
  }

  onResize() {
    const w = window.innerWidth, h = window.innerHeight;
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(w, h);
  }

  updateMobiusRotation(t) {
    this.points.rotation.y = t * 0.18;
    this.points.rotation.x = Math.sin(t * 0.11) * 0.15;
    this.points.rotation.z = Math.cos(t * 0.073) * 0.06;
  }

  updateColorTransition(dt) {
    const target = MODE_COLORS[this.mode];
    this.material.uniforms.uColorHi.value.lerp(target.hi, Math.min(1, dt * 5));
    this.material.uniforms.uColorMid.value.lerp(target.mid, Math.min(1, dt * 5));
  }
  animate() {
    this.frameId = requestAnimationFrame(() => this.animate());
    const dt = Math.min(this.clock.getDelta(), 0.05);
    if (document.hidden) return;
    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const t = reducedMotion ? 0 : this.clock.elapsedTime;
    this.analyseAudio(); this.updateTurbulence(this.micLevel);
    const uDiam = this.material.uniforms.uDiameterMod;
    uDiam.value += (this.ttsLevel - uDiam.value) * 0.18;
    this.updateColorTransition(dt); this.updateLockedDim(dt); this.updateMobiusRotation(t);
    this.material.uniforms.uTime.value = t;
    this.camera.position.x = Math.sin(t * 0.07) * 0.22;
    this.camera.position.y = Math.cos(t * 0.055) * 0.14;
    this.camera.lookAt(0, 0, 0);
    const scale = this.focusVisual ? 0.58 : 1;
    this.updateProgressPie(dt, reducedMotion);
    const offset = this.pieElement ? 2.5 : this.focusVisual && window.innerWidth > 700 ? -3.0 : 0;
    this.points.scale.setScalar(this.points.scale.x + (scale - this.points.scale.x) * Math.min(1, dt * 5));
    this.points.position.x += (offset - this.points.position.x) * Math.min(1, dt * 5);
    this.renderer.render(this.scene, this.camera);
  }
}
    window.ARGUS = new ArgusVisualizer();
    window.dispatchEvent(new Event('argus-renderer-ready'));
  } catch (error) {
    console.warn('Möbius renderer unavailable:', error);
    document.getElementById('notice').textContent = '3D rendering is unavailable. Voice and controls remain available.';
  }
})();
