/*
 * J.A.R.V.I.S. reactor orb — Stark-style holographic HUD core (Three.js, vendored).
 *
 *   const orb = initOrb(canvas);
 *   orb.setAnalyser(node);          // drive flares with TTS amplitude (SPEAKING)
 *   orb.setMicAnalyser(node);       // drive ripple + gold tint with mic level (LISTENING)
 *   orb.setState("idle"|"listening"|"thinking"|"speaking");
 *   orb.getLevel();                 // smoothed 0..1, for HUD telemetry
 *
 * Look: a glowing cyan icosahedron core with GLSL vertex displacement + a fresnel
 * rim, a halo of additive particles forming a sphere, three concentric gyroscope
 * rings with tick marks rotating on different axes, and an additive-bloom feel
 * achieved with layered transparent glow. Cyan core (#2fe6ff) with gold (#ffcf6a)
 * accents that bleed in while LISTENING. Runs on requestAnimationFrame at 60fps.
 */
(function (global) {
  function initOrb(canvas) {
    const THREE = global.THREE;
    if (!THREE) {
      console.error("[orb] THREE not loaded");
      return {
        setAnalyser() {}, setMicAnalyser() {}, setState() {},
        getLevel() { return 0; }, dispose() {},
      };
    }

    // Palette ----------------------------------------------------------------
    const CYAN = new THREE.Color(0x2fe6ff);
    const GOLD = new THREE.Color(0xffcf6a);
    const DEEP = new THREE.Color(0x0a4f6b);

    // Renderer ---------------------------------------------------------------
    const renderer = new THREE.WebGLRenderer({
      canvas,
      antialias: true,
      alpha: true,
      powerPreference: "high-performance",
    });
    renderer.setPixelRatio(Math.min(global.devicePixelRatio || 1, 2));
    renderer.setClearColor(0x000000, 0);

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(38, 1, 0.1, 100);
    camera.position.z = 10.6;

    // Root group (subtle parallax sway for a "living hologram" feel).
    const root = new THREE.Group();
    scene.add(root);

    // ---------------------------------------------------------------------- //
    // Core — shader-displaced icosahedron with fresnel rim glow.
    // ---------------------------------------------------------------------- //
    const coreGeo = new THREE.IcosahedronGeometry(1.0, 48);

    const coreUniforms = {
      uTime:    { value: 0 },
      uLevel:   { value: 0 },     // amplitude 0..1
      uGold:    { value: 0 },     // 0 = cyan, 1 = gold (listening tint)
      uColorA:  { value: CYAN.clone() },
      uColorB:  { value: GOLD.clone() },
      uColorDeep: { value: DEEP.clone() },
    };

    const coreVert = `
      uniform float uTime;
      uniform float uLevel;
      varying float vNoise;
      varying vec3  vNormalW;
      varying vec3  vViewDir;

      // classic 3D simplex-ish value noise (cheap, smooth enough for displacement)
      vec3 hash3(vec3 p){
        p = vec3(dot(p,vec3(127.1,311.7,74.7)),
                 dot(p,vec3(269.5,183.3,246.1)),
                 dot(p,vec3(113.5,271.9,124.6)));
        return -1.0 + 2.0*fract(sin(p)*43758.5453123);
      }
      float noise(vec3 p){
        vec3 i = floor(p); vec3 f = fract(p);
        vec3 u = f*f*(3.0-2.0*f);
        return mix(mix(mix(dot(hash3(i+vec3(0,0,0)),f-vec3(0,0,0)),
                           dot(hash3(i+vec3(1,0,0)),f-vec3(1,0,0)),u.x),
                       mix(dot(hash3(i+vec3(0,1,0)),f-vec3(0,1,0)),
                           dot(hash3(i+vec3(1,1,0)),f-vec3(1,1,0)),u.x),u.y),
                   mix(mix(dot(hash3(i+vec3(0,0,1)),f-vec3(0,0,1)),
                           dot(hash3(i+vec3(1,0,1)),f-vec3(1,0,1)),u.x),
                       mix(dot(hash3(i+vec3(0,1,1)),f-vec3(0,1,1)),
                           dot(hash3(i+vec3(1,1,1)),f-vec3(1,1,1)),u.x),u.y),u.z);
      }
      float fbm(vec3 p){
        float v = 0.0, a = 0.5;
        for(int i=0;i<4;i++){ v += a*noise(p); p *= 2.02; a *= 0.5; }
        return v;
      }

      void main(){
        float t = uTime;
        // breathing + audio-reactive displacement — kept very tight so the
        // core silhouette stays a clean SPHERE; the noise is fine surface
        // shimmer for internal shading, not big lumps that billow out.
        float amp = 0.010 + uLevel * 0.10;
        float n = fbm(normal * 3.2 + vec3(t*0.35, t*0.22, -t*0.28));
        vNoise = n;
        float disp = n * amp + sin(t*1.3)*0.008;
        vec3 pos = position + normal * disp;

        vec4 worldPos = modelMatrix * vec4(pos, 1.0);
        vNormalW = normalize(mat3(modelMatrix) * normal);
        vViewDir = normalize(cameraPosition - worldPos.xyz);

        gl_Position = projectionMatrix * viewMatrix * worldPos;
      }
    `;

    const coreFrag = `
      uniform float uLevel;
      uniform float uGold;
      uniform vec3  uColorA;   // cyan
      uniform vec3  uColorB;   // gold
      uniform vec3  uColorDeep;
      varying float vNoise;
      varying vec3  vNormalW;
      varying vec3  vViewDir;

      void main(){
        float ndv = max(dot(vNormalW, vViewDir), 0.0);
        // crisp, bright, fairly thin fresnel rim — the classic hologram edge.
        float fres = pow(1.0 - ndv, 3.4);
        // smooth inner glow that peaks dead-center and fades to the silhouette,
        // so the body reads as one coherent luminous sphere (not patchy smoke).
        float center = pow(ndv, 2.2);
        vec3 base = mix(uColorA, uColorB, uGold);
        // gentle turbulence shimmer for life — color only, kept subtle so the
        // interior looks like churning plasma, not patchy fog.
        float shimmer = vNoise * 0.5 + 0.5;
        vec3 col  = base * (0.65 + 0.30 * shimmer);
        col += base * fres * (2.6 + uLevel * 1.8);           // hot crisp rim
        col += base * center * (0.45 + uLevel * 0.5);         // inner glow
        // alpha: crisp rim dominates; smooth low inner veil; no patchy noise.
        float bodyAlpha = center * (0.13 + 0.07 * shimmer + uLevel * 0.18);
        float alpha = clamp(fres * 1.35 + bodyAlpha, 0.0, 1.0);
        gl_FragColor = vec4(col, alpha);
      }
    `;

    const coreMat = new THREE.ShaderMaterial({
      uniforms: coreUniforms,
      vertexShader: coreVert,
      fragmentShader: coreFrag,
      transparent: true,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
    });
    const core = new THREE.Mesh(coreGeo, coreMat);
    root.add(core);

    // Inner solid nucleus so the additive core reads as a glowing ball, not a
    // hollow shell — small, dark-cyan, gives the bloom something to sit on.
    const nucleus = new THREE.Mesh(
      new THREE.IcosahedronGeometry(0.55, 6),
      new THREE.MeshBasicMaterial({ color: 0x05202c, transparent: true, opacity: 0.95 })
    );
    root.add(nucleus);

    // Wireframe shell — faint tech lattice over the core.
    const wireMat = new THREE.MeshBasicMaterial({
      color: 0x2fe6ff, wireframe: true, transparent: true, opacity: 0.07,
      blending: THREE.AdditiveBlending, depthWrite: false,
    });
    const wire = new THREE.Mesh(new THREE.IcosahedronGeometry(1.18, 3), wireMat);
    root.add(wire);

    // ---------------------------------------------------------------------- //
    // Halo particles — points distributed on a sphere, additive, twinkle + swell.
    // ---------------------------------------------------------------------- //
    const COUNT = 260;
    const pGeo = new THREE.BufferGeometry();
    const pPos = new Float32Array(COUNT * 3);
    const pRnd = new Float32Array(COUNT);       // per-particle random phase
    const pRad = new Float32Array(COUNT);       // base radius
    for (let i = 0; i < COUNT; i++) {
      // even-ish sphere distribution (golden spiral)
      const y = 1 - (i / (COUNT - 1)) * 2;
      const r = Math.sqrt(1 - y * y);
      const phi = i * 2.399963229728653; // golden angle
      const radius = 1.9 + Math.random() * 1.35;
      pPos[i * 3]     = Math.cos(phi) * r * radius;
      pPos[i * 3 + 1] = y * radius;
      pPos[i * 3 + 2] = Math.sin(phi) * r * radius;
      pRnd[i] = Math.random();
      pRad[i] = radius;
    }
    pGeo.setAttribute("position", new THREE.BufferAttribute(pPos, 3));
    pGeo.setAttribute("aRnd", new THREE.BufferAttribute(pRnd, 1));
    pGeo.setAttribute("aRad", new THREE.BufferAttribute(pRad, 1));

    const pUniforms = {
      uTime:   { value: 0 },
      uLevel:  { value: 0 },
      uGold:   { value: 0 },
      uPix:    { value: renderer.getPixelRatio() },
      uColorA: { value: CYAN.clone() },
      uColorB: { value: GOLD.clone() },
    };
    const pMat = new THREE.ShaderMaterial({
      uniforms: pUniforms,
      transparent: true,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
      vertexShader: `
        uniform float uTime; uniform float uLevel; uniform float uPix;
        attribute float aRnd; attribute float aRad;
        varying float vTwinkle;
        void main(){
          float t = uTime + aRnd * 6.2831;
          vTwinkle = 0.45 + 0.55 * sin(t * 2.0);
          // particles breathe outward with amplitude
          float swell = 1.0 + uLevel * 0.22 * (0.5 + aRnd);
          vec3 p = position * swell;
          vec4 mv = modelViewMatrix * vec4(p, 1.0);
          float size = (0.8 + aRnd * 1.3) * (1.0 + uLevel * 1.6) * uPix;
          gl_PointSize = size * (300.0 / -mv.z);
          gl_Position = projectionMatrix * mv;
        }
      `,
      fragmentShader: `
        uniform float uLevel; uniform float uGold;
        uniform vec3 uColorA; uniform vec3 uColorB;
        varying float vTwinkle;
        void main(){
          vec2 d = gl_PointCoord - vec2(0.5);
          float r = length(d);
          if (r > 0.5) discard;
          float glow = smoothstep(0.5, 0.0, r);
          vec3 col = mix(uColorA, uColorB, uGold);
          float a = glow * vTwinkle * (0.22 + uLevel * 0.4);
          gl_FragColor = vec4(col * (1.0 + uLevel * 0.6), a);
        }
      `,
    });
    const halo = new THREE.Points(pGeo, pMat);
    root.add(halo);

    // ---------------------------------------------------------------------- //
    // Gyroscope rings — concentric reactor arcs on different axes, with ticks.
    // ---------------------------------------------------------------------- //
    function makeRing(radius, tube, color, opacity) {
      const g = new THREE.Group();
      const ring = new THREE.Mesh(
        new THREE.TorusGeometry(radius, tube, 8, 180),
        new THREE.MeshBasicMaterial({
          color, transparent: true, opacity,
          blending: THREE.AdditiveBlending, depthWrite: false,
        })
      );
      g.add(ring);

      // tick marks around the ring (short radial bars)
      const ticks = 48;
      const tickMat = new THREE.MeshBasicMaterial({
        color, transparent: true, opacity: opacity * 1.4,
        blending: THREE.AdditiveBlending, depthWrite: false,
      });
      const tickGeo = new THREE.BoxGeometry(0.012, 0.07, 0.012);
      for (let i = 0; i < ticks; i++) {
        const a = (i / ticks) * Math.PI * 2;
        const big = i % 6 === 0;
        const m = new THREE.Mesh(tickGeo, tickMat);
        m.position.set(Math.cos(a) * radius, Math.sin(a) * radius, 0);
        m.rotation.z = a;
        m.scale.y = big ? 2.2 : 1.0;
        g.add(m);
      }
      // a small bright "node" marker on the ring
      const node = new THREE.Mesh(
        new THREE.SphereGeometry(0.045, 12, 12),
        new THREE.MeshBasicMaterial({
          color, transparent: true, opacity: 0.95,
          blending: THREE.AdditiveBlending, depthWrite: false,
        })
      );
      node.position.set(radius, 0, 0);
      g.add(node);
      return g;
    }

    const ring1 = makeRing(1.85, 0.009, 0x2fe6ff, 0.60);
    ring1.rotation.x = Math.PI * 0.5;
    root.add(ring1);

    const ring2 = makeRing(2.25, 0.008, 0x7ce7ff, 0.46);
    ring2.rotation.y = Math.PI * 0.32;
    ring2.rotation.x = Math.PI * 0.18;
    root.add(ring2);

    const ring3 = makeRing(2.7, 0.007, 0xffcf6a, 0.38);
    ring3.rotation.x = Math.PI * 0.62;
    ring3.rotation.z = Math.PI * 0.2;
    root.add(ring3);

    // A broken arc (gyroscope sweep) — a short bright segment that orbits.
    const arc = new THREE.Mesh(
      new THREE.TorusGeometry(2.05, 0.016, 8, 64, Math.PI * 0.55),
      new THREE.MeshBasicMaterial({
        color: 0x2fe6ff, transparent: true, opacity: 0.85,
        blending: THREE.AdditiveBlending, depthWrite: false,
      })
    );
    arc.rotation.x = Math.PI * 0.5;
    root.add(arc);

    // ---------------------------------------------------------------------- //
    // Resize
    // ---------------------------------------------------------------------- //
    function resize() {
      const w = canvas.clientWidth || global.innerWidth;
      const h = canvas.clientHeight || global.innerHeight;
      renderer.setSize(w, h, false);
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
      pUniforms.uPix.value = renderer.getPixelRatio();
    }
    global.addEventListener("resize", resize);
    resize();

    // ---------------------------------------------------------------------- //
    // Audio analysers — voice (TTS) and mic.
    // ---------------------------------------------------------------------- //
    let voiceAnalyser = null, voiceData = null;
    let micAnalyser = null, micData = null;

    function setAnalyser(node) {
      voiceAnalyser = node || null;
      voiceData = node ? new Uint8Array(node.frequencyBinCount) : null;
    }
    function setMicAnalyser(node) {
      micAnalyser = node || null;
      micData = node ? new Uint8Array(node.frequencyBinCount) : null;
    }

    function readLevel(an, data) {
      if (!an || !data) return 0;
      an.getByteFrequencyData(data);
      let sum = 0;
      for (let i = 0; i < data.length; i++) sum += data[i];
      return sum / (data.length * 255);
    }

    // ---------------------------------------------------------------------- //
    // State machine — drives idle/listening/thinking/speaking behaviour.
    // ---------------------------------------------------------------------- //
    let state = "idle";
    let goldTarget = 0;      // gold tint target (listening = 1)
    function setState(s) {
      state = s || "idle";
      goldTarget = state === "listening" ? 1 : 0;
    }

    // ---------------------------------------------------------------------- //
    // Animation loop
    // ---------------------------------------------------------------------- //
    const clock = new THREE.Clock();
    let raf = 0;
    let smoothLevel = 0;
    let smoothGold = 0;

    function frame() {
      const t = clock.getElapsedTime();

      // amplitude source depends on state
      const voiceLvl = readLevel(voiceAnalyser, voiceData);
      const micLvl = readLevel(micAnalyser, micData);
      const breathing = 0.5 + 0.5 * Math.sin(t * 1.25);

      let target;
      if (state === "speaking") {
        target = Math.max(0.05 * breathing, voiceLvl * 1.15);
      } else if (state === "listening") {
        target = Math.max(0.06 * breathing, micLvl * 1.4);
      } else if (state === "thinking") {
        // jittery searching pulse
        target = 0.10 + 0.10 * (0.5 + 0.5 * Math.sin(t * 6.0)) + 0.04 * Math.random();
      } else {
        target = 0.07 + 0.06 * breathing; // idle — gentle living pulse
      }
      smoothLevel += (target - smoothLevel) * 0.16;
      smoothGold += (goldTarget - smoothGold) * 0.08;

      // push uniforms
      coreUniforms.uTime.value = t;
      coreUniforms.uLevel.value = smoothLevel;
      coreUniforms.uGold.value = smoothGold;
      pUniforms.uTime.value = t;
      pUniforms.uLevel.value = smoothLevel;
      pUniforms.uGold.value = smoothGold;

      // core scale flare
      const s = 1 + smoothLevel * 0.35;
      core.scale.setScalar(s);
      nucleus.scale.setScalar(0.9 + smoothLevel * 0.25);
      wire.scale.setScalar(s * 1.0 + smoothLevel * 0.05);

      // rotations — every layer on its own axis/speed (gyroscope)
      const spin = 1 + smoothLevel * 2.5;
      core.rotation.y += 0.0016 * spin;
      core.rotation.x += 0.0009;
      wire.rotation.y -= 0.0013;
      wire.rotation.x += 0.0007;
      halo.rotation.y += 0.0007;
      halo.rotation.x += 0.0003;

      ring1.rotation.z += 0.0042 * spin;
      ring2.rotation.z -= 0.0030 * spin;
      ring2.rotation.x += 0.0014;
      ring3.rotation.z += 0.0019 * spin;
      ring3.rotation.y -= 0.0011;
      arc.rotation.z += 0.018 * spin;

      // subtle parallax sway so the whole rig feels alive on camera
      root.rotation.y = Math.sin(t * 0.18) * 0.10;
      root.rotation.x = Math.cos(t * 0.15) * 0.06;

      renderer.render(scene, camera);
      raf = global.requestAnimationFrame(frame);
    }
    raf = global.requestAnimationFrame(frame);

    function dispose() {
      global.cancelAnimationFrame(raf);
      global.removeEventListener("resize", resize);
      renderer.dispose();
    }

    return {
      setAnalyser,
      setMicAnalyser,
      setState,
      getLevel() { return smoothLevel; },
      dispose,
    };
  }

  global.initOrb = initOrb;
})(window);
