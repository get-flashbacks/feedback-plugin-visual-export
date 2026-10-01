// SPDX-License-Identifier: AGPL-3.0-or-later
(function visualExportPlugin() {
  'use strict';
  const PLUGIN_ID = 'visual_export';
  if (window.__visualExportSetup) return;
  window.__visualExportSetup = true;

  let settings = { width: 1920, height: 1080, fps: 30, bitrate_mbps: 10, include_chrome: false };
  let cancelled = false;
  let exporting = false;
  let modal = null;
  // Encoder queue watermarks (issue #9, phase 1). Rendering stays ahead of the
  // encoder by at most ENCODE_HIGH_WATER frames, then production pauses until
  // the encoder has drained back to ENCODE_LOW_WATER — enough overlap to keep
  // rendering and encoding concurrent, without unbounded queue growth.
  const ENCODE_HIGH_WATER = 12;
  const ENCODE_LOW_WATER = 6;
  // Phase 2: how often dynamic (lyrics/Staff View) roots get a defensive full
  // re-describe even if no mutation was observed. Cheap insurance against host
  // mutations that raise no MutationRecord (CSS transitions, Web Animations);
  // the per-frame cost is a handful of getBoundingClientRect() calls instead
  // of the querySelectorAll + getComputedStyle storm this replaced.
  const DYNAMIC_REFRESH_FRAMES = 30;
  // Precomposed layers are rebuilt while a referenced image/SVG is still
  // decoding, so late-loading art isn't missing from the whole export. Bounded
  // so a permanently broken asset can't rebuild forever.
  const MAX_LAYER_BUILDS = 60;

  fetch(`/api/plugins/${PLUGIN_ID}/settings`).then(r => r.ok ? r.json() : null)
    .then(v => { if (v) settings = Object.assign(settings, v); }).catch(() => {});

  function visible(el) {
    if (!el || !el.isConnected) return false;
    const s = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' && Number(s.opacity) > 0 && r.width > 0 && r.height > 0;
  }

  function mountButton() {
    if (document.getElementById('visual-export-button')) return;
    const slot = window.feedBack?.ui?.playerControlSlot?.();
    if (!slot) return;
    const btn = document.createElement('button');
    btn.id = 'visual-export-button';
    btn.type = 'button';
    btn.className = 'v3-pop-btn visual-export-button';
    btn.textContent = 'Export video';
    btn.title = 'Render the configured player view to MP4';
    btn.addEventListener('click', openDialog);
    slot.appendChild(btn);
    // The 1s poll below exists only to catch the slot not being ready yet at
    // load time (a v3 chrome mount race) -- once mounted once, every future
    // remount need is already covered by the screen:changed listener (the
    // slot is otherwise a stable, always-reachable container per its own
    // contract). Stop polling instead of ticking forever for the rest of
    // the page's life once that first successful mount happens.
    stopMountPoll();
  }

  function openDialog() {
    if (modal) { modal.remove(); modal = null; }
    modal = document.createElement('div');
    modal.className = 'visual-export-modal';
    modal.innerHTML = `
      <div class="visual-export-dialog" role="dialog" aria-modal="true" aria-labelledby="ve-title">
        <div class="visual-export-head"><div><h2 id="ve-title">Export player video</h2>
          <p>Uses the current view, arrangements, split layout, lyrics, and camera settings.</p></div>
          <button type="button" data-ve-close aria-label="Close">×</button></div>
        <div class="visual-export-grid">
          <label>Resolution<select data-ve-resolution>
            <option value="1280x720">1280 × 720</option><option value="1920x1080">1920 × 1080</option>
            <option value="2560x1440">2560 × 1440</option><option value="3840x2160">3840 × 2160</option>
          </select></label>
          <label>Frame rate<select data-ve-fps><option>24</option><option>30</option><option>60</option></select></label>
          <label>Video bitrate<input data-ve-bitrate type="number" min="2" max="80" step="1"><span>Mbps</span></label>
        </div>
        <div class="visual-export-note">Keep this tab open while frames render. Playback stays paused and resumes at the same position afterward.</div>
        <div class="visual-export-progress" hidden><div data-ve-progress-bar></div></div>
        <div class="visual-export-status" data-ve-status>Ready.</div>
        <div class="visual-export-actions"><button type="button" data-ve-cancel hidden>Cancel</button>
          <button type="button" data-ve-start>Render MP4</button></div>
      </div>`;
    document.body.appendChild(modal);
    modal.querySelector('[data-ve-resolution]').value = `${settings.width}x${settings.height}`;
    modal.querySelector('[data-ve-fps]').value = String(settings.fps);
    modal.querySelector('[data-ve-bitrate]').value = String(settings.bitrate_mbps);
    modal.querySelector('[data-ve-close]').onclick = () => { if (!exporting) { modal.remove(); modal = null; } };
    modal.querySelector('[data-ve-cancel]').onclick = () => { cancelled = true; };
    modal.querySelector('[data-ve-start]').onclick = () => startExport(modal);
  }

  function status(dialog, text, fraction) {
    const label = dialog.querySelector('[data-ve-status]');
    if (label) label.textContent = text;
    const track = dialog.querySelector('.visual-export-progress');
    const bar = dialog.querySelector('[data-ve-progress-bar]');
    if (track && Number.isFinite(fraction)) {
      track.hidden = false;
      bar.style.width = `${Math.max(0, Math.min(1, fraction)) * 100}%`;
    }
  }

  function rgbaVisible(color) {
    return color && color !== 'transparent' && !/rgba\([^)]*,\s*0(?:\.0+)?\s*\)/.test(color);
  }

  function roundedRect(ctx, x, y, w, h, radius) {
    const r = Math.max(0, Math.min(Number.parseFloat(radius) || 0, w / 2, h / 2));
    ctx.beginPath(); ctx.roundRect(x, y, w, h, r); return r;
  }

  async function createCompositor(target, includeChrome) {
    const player = document.getElementById('player');
    if (!player || !visible(player)) throw new Error('Open a song in the player before exporting.');
    const pr = player.getBoundingClientRect();
    const ctx = target.getContext('2d', { alpha: false });
    // Preserve the configured player's aspect ratio. A different output aspect
    // is letterboxed instead of stretching circles, text, and fret geometry.
    const scale = Math.min(target.width / pr.width, target.height / pr.height);
    const sx = scale, sy = scale;
    const ox = (target.width - pr.width * scale) / 2;
    const oy = (target.height - pr.height * scale) / 2;
    const ps = getComputedStyle(player);
    // #player-hud is the host's own top overlay (song metadata, clock, Up Next).
    // Its chrome-hide list in static/v3/index.html excludes it too, and it is a
    // sibling of #player-controls under #player, so nothing else here would catch
    // it. Left in, its live text would be baked into a precomposed layer at
    // export start and the video would carry a clock frozen at t=0.
    const skipChrome = el => !includeChrome && !!el.closest('#player-hud,#player-controls,#player-footer,#v3-railzone,[id^="v3-rail-pop-"]');
    const lyricSelector = '.splitscreen-lyrics-pane,.splitscreen-lyrics-overlay';
    // Staff View (alphaTab) swaps its rendered SVG(s) out from under us as
    // playback scrolls to a new system — a one-time snapshot captures
    // whichever SVG happened to exist at export start and then keeps
    // painting that same (soon-detached) element every frame, so the
    // notation either never appears or freezes on the first system
    // (issue #2). Treat it like the lyrics pane: re-described every frame.
    const staffSelector = '[data-staffview-instance]';

    function describe(el) {
      if (!visible(el) || skipChrome(el)) return null;
      const r = el.getBoundingClientRect();
      if (r.right <= pr.left || r.left >= pr.right || r.bottom <= pr.top || r.top >= pr.bottom) return null;
      const s = getComputedStyle(el);
      const direct = Array.from(el.childNodes).some(n => n.nodeType === Node.TEXT_NODE && n.textContent.trim());
      const media = el instanceof HTMLCanvasElement || el instanceof HTMLVideoElement || el instanceof HTMLImageElement || el instanceof SVGSVGElement;
      const bg = rgbaVisible(s.backgroundColor), border = Number.parseFloat(s.borderTopWidth) || 0;
      if (!media && !direct && !bg && !border) return null;
      return { el, r, media, direct, bg, border, z: Number.parseInt(s.zIndex, 10) || 0,
        opacity: Math.max(0, Math.min(1, Number(s.opacity) || 1)), backgroundColor: s.backgroundColor,
        borderColor: s.borderTopColor, radius: Number.parseFloat(s.borderRadius) || 0,
        fontSize: Number.parseFloat(s.fontSize) || 16, fontStyle: s.fontStyle,
        fontWeight: s.fontWeight, fontFamily: s.fontFamily, color: s.color, textAlign: s.textAlign };
    }

    // Rasterize a <svg> to an Image so the (synchronous) per-frame paint()
    // can composite it via drawImage — a raw SVGSVGElement isn't a valid
    // CanvasImageSource. Cached per element so re-describing the staff-view
    // subtree every frame doesn't re-encode/decode an unchanged SVG.
    const svgRasterCache = new WeakMap();
    async function rasterizeSvg(el) {
      if (svgRasterCache.has(el)) return svgRasterCache.get(el);
      let image = null;
      let url = null;
      try {
        const xml = new XMLSerializer().serializeToString(el);
        const blob = new Blob([xml], { type: 'image/svg+xml' });
        url = URL.createObjectURL(blob);
        image = new Image();
        image.src = url;
        await image.decode();
      } catch (_) { image = null; /* transient/unsupported — not cached, retried next frame */ }
      finally { if (url) URL.revokeObjectURL(url); }
      // Only cache a successful decode. A transient failure (e.g. the blob
      // wasn't ready yet) would otherwise be pinned to null forever, since
      // the staff-view subtree — the only caller that re-rasterizes per
      // element reference — is re-described every frame rather than once.
      if (image) svgRasterCache.set(el, image);
      return image;
    }

    // Classify the player subtree once (issue #9, phase 2). Everything that can
    // never change during an export is precomposed into offscreen layers;
    // canvases and videos stay dynamic because renderFrameAt() repaints them
    // every frame. Lyrics and Staff View are handled separately below.
    const liveElements = Array.from(player.getElementsByTagName('canvas'))
      .concat(Array.from(player.getElementsByTagName('video')));
    const liveSet = new Set(liveElements);
    const entries = [];
    for (const el of player.querySelectorAll('*')) {
      if (el.closest(lyricSelector) || el.closest(staffSelector)) continue;
      const d = describe(el);
      if (d) entries.push({ d, live: liveSet.has(el) });
    }
    entries.sort((a, b) => a.d.z - b.d.z);

    let layers = [];
    let pendingElements = new Set();
    let layerBuilds = 0;
    const LAYER_RETRY_FRAMES = 15;

    // Paints one descriptor onto `g`. `offsetX/offsetY` place the descriptor
    // into a layer canvas whose origin is that layer's top-left corner; the
    // default (0, 0) paints straight onto the export frame at its own
    // letterboxed position.
    function paint(d, g, offsetX, offsetY) {
      const { el, r } = d;
      if (!el.isConnected) return;
      const x = ox + (r.left - pr.left) * sx - (offsetX || 0);
      const y = oy + (r.top - pr.top) * sy - (offsetY || 0);
      const w = r.width * sx, h = r.height * sy;
      g.save(); g.globalAlpha = d.opacity;
      if (d.bg) {
        roundedRect(g, x, y, w, h, d.radius * scale);
        g.fillStyle = d.backgroundColor; g.fill();
      }
      if (d.media) {
        try { g.drawImage(d.raster || el, x, y, w, h); } catch (_) { /* unloaded/tainted visual asset */ }
      }
      if (d.border > 0 && rgbaVisible(d.borderColor)) {
        roundedRect(g, x, y, w, h, d.radius * scale);
        g.strokeStyle = d.borderColor; g.lineWidth = d.border * scale; g.stroke();
      }
      const direct = d.direct
        ? Array.from(el.childNodes).filter(n => n.nodeType === Node.TEXT_NODE)
          .map(n => n.textContent).join(' ').replace(/\s+/g, ' ').trim()
        : '';
      if (direct && !['SCRIPT', 'STYLE', 'OPTION'].includes(el.tagName)) {
        g.font = `${d.fontStyle} ${d.fontWeight} ${d.fontSize * scale}px ${d.fontFamily}`;
        g.fillStyle = d.color || '#fff'; g.textBaseline = 'middle';
        const align = d.textAlign === 'center' ? 'center' : d.textAlign === 'right' ? 'right' : 'left';
        g.textAlign = align;
        const tx = align === 'center' ? x + w / 2 : align === 'right' ? x + w : x;
        g.fillText(direct, tx, y + h / 2, w || undefined);
      }
      g.restore();
    }

    // Flattens one run of consecutive static descriptors into a single
    // offscreen layer the size of the run's bounding box. Source-over is
    // associative, so compositing that layer reproduces painting the run
    // directly onto the frame -- but only because the run holds nothing a
    // live node has to sit on top of: runs are cut at every live node, so
    // paint order (and therefore z-order) is preserved exactly.
    async function composeRun(run) {
      let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity, maxBorder = 0;
      for (const d of run) {
        const r = d.r;
        const x = ox + (r.left - pr.left) * sx, y = oy + (r.top - pr.top) * sy;
        x0 = Math.min(x0, x); y0 = Math.min(y0, y);
        x1 = Math.max(x1, x + r.width * sx); y1 = Math.max(y1, y + r.height * sy);
        maxBorder = Math.max(maxBorder, d.border * scale);
      }
      // Borders stroke centred on the descriptor box, so keep half a stroke of
      // padding plus rounding slack outside the union box.
      const pad = 2 + Math.ceil(maxBorder / 2);
      const left = Math.max(0, Math.floor(x0) - pad), top = Math.max(0, Math.floor(y0) - pad);
      const right = Math.min(target.width, Math.ceil(x1) + pad);
      const bottom = Math.min(target.height, Math.ceil(y1) + pad);
      const layer = document.createElement('canvas');
      layer.width = Math.max(1, right - left); layer.height = Math.max(1, bottom - top);
      const g = layer.getContext('2d');
      const pending = [];
      for (const d of run) {
        if (!d.el.isConnected) continue;
        if (d.el instanceof SVGSVGElement && !d.raster) d.raster = await rasterizeSvg(d.el);
        if (d.el instanceof SVGSVGElement && !d.raster) pending.push(d.el);
        if (d.el instanceof HTMLImageElement && !d.el.complete) pending.push(d.el);
        paint(d, g, left, top);
      }
      return { layer, left, top, pending };
    }

    async function buildLayers() {
      const built = [];
      let run = [];
      for (const entry of entries) {
        if (entry.live) {
          if (run.length) { built.push(await composeRun(run)); run = []; }
          built.push({ live: entry.d });
          continue;
        }
        run.push(entry.d);
      }
      if (run.length) built.push(await composeRun(run));
      return built;
    }

    // Layers are rebuilt while a referenced image/SVG is still decoding, so
    // art that finishes loading after the export started isn't missing from
    // every frame. MAX_LAYER_BUILDS keeps an asset that never decodes from
    // rebuilding forever.
    async function rebuildLayers() {
      for (let i = entries.length - 1; i >= 0; i--) if (!entries[i].d.el.isConnected) entries.splice(i, 1);
      layers = await buildLayers();
      layerBuilds++;
      pendingElements = new Set();
      for (const item of layers) for (const el of item.pending || []) pendingElements.add(el);
    }

    function pendingReady() {
      for (const el of pendingElements) {
        if (el instanceof HTMLImageElement ? !el.complete : !svgRasterCache.has(el)) return false;
      }
      return true;
    }

    await rebuildLayers();

    // Dynamic roots (split lyrics, Staff View) are cached per root and only
    // re-described when their own subtree actually changes -- a
    // MutationObserver scoped to the player invalidates just the affected
    // root. Their rects are re-measured every frame (cheap) because panes
    // scroll and animate, while the expensive computed-style reads only
    // happen on a real change.
    const rootStates = new Map();
    let dynamicRoots = [];
    let rootsDirty = true;

    function refreshRoots(force) {
      if (!rootsDirty) {
        if (force) for (const state of dynamicRoots) state.dirty = true;
        return;
      }
      rootsDirty = false;
      const found = Array.from(player.querySelectorAll(`${lyricSelector},${staffSelector}`));
      dynamicRoots = found.map(el => {
        let state = rootStates.get(el);
        if (!state) { state = { el, descriptors: null, dirty: true }; rootStates.set(el, state); }
        return state;
      });
      for (const el of Array.from(rootStates.keys())) if (!found.includes(el)) rootStates.delete(el);
      if (force) for (const state of dynamicRoots) state.dirty = true;
    }

    function descriptorsFor(state) {
      if (state.dirty || !state.descriptors) {
        state.descriptors = [state.el, ...state.el.querySelectorAll('*')].map(describe).filter(Boolean);
        state.dirty = false;
      }
      return state.descriptors;
    }

    const observer = new MutationObserver(records => {
      for (const record of records) {
        const node = record.target.nodeType === Node.ELEMENT_NODE ? record.target : record.target.parentElement;
        let owner = null;
        if (node) for (const [el, state] of rootStates) if (el === node || el.contains(node)) { owner = state; break; }
        if (owner) owner.dirty = true;
        // Only a childList outside a known root can add or remove a root; the
        // timeline HUD writes are characterData/attributes and would
        // otherwise invalidate the cache on every frame.
        else if (record.type === 'childList') rootsDirty = true;
      }
    });
    observer.observe(player, { childList: true, subtree: true, characterData: true, attributes: true });

    async function paintRoot(state) {
      let missingRaster = false;
      for (const d of descriptorsFor(state)) {
        // Rects move every frame (scrolling panes), so they are re-measured
        // rather than cached; style-derived fields stay cached until the
        // subtree actually changes.
        const r = d.el.getBoundingClientRect();
        // Two different situations that must not share one flag. A collapsed
        // box means the element stopped existing, so re-describe and let
        // describe() forget it. Merely scrolling outside the captured area is
        // the *normal* state for a lyrics pane -- marking the root dirty for it
        // would re-run describe() across the whole subtree every frame, which
        // is exactly the querySelectorAll + getComputedStyle cost this cache
        // exists to avoid. The rect is re-measured per frame, so a descriptor
        // that scrolls back in is picked up immediately.
        if (r.width <= 0 || r.height <= 0) { state.dirty = true; continue; }
        if (r.right <= pr.left || r.left >= pr.right || r.bottom <= pr.top || r.top >= pr.bottom) continue;
        d.r = r;
        // Staff View's rendered SVG(s) are swapped out entirely as playback
        // scrolls to a new system, so rasterize per element reference and
        // keep successful results in the WeakMap above.
        if (d.el instanceof SVGSVGElement && !d.raster) d.raster = await rasterizeSvg(d.el);
        if (d.el instanceof SVGSVGElement && !d.raster) missingRaster = true;
        paint(d, ctx);
      }
      // A transient rasterize failure must not be pinned by the descriptor
      // cache -- re-describe next frame so it is retried.
      if (missingRaster) state.dirty = true;
    }

    const fmtClock = value => `${Math.floor(value / 60)}:${String(Math.floor(value % 60)).padStart(2, '0')}`;
    const timelineClock = document.getElementById('hud-time');
    const timelineName = document.getElementById('v3-upnext-name');
    const timelineEta = document.getElementById('v3-upnext-eta');
    // The HUD is in skipChrome, so these writes can never reach the video: the
    // clock and Up Next line are static text baked into a precomposed layer at
    // export start, and updating them in the DOM only costs layout and style
    // invalidations on the host page. Keep the guard in step with skipChrome —
    // if the HUD is ever un-excluded (see the note on skipChrome above), these
    // writes become load-bearing again.
    const captured = el => !!el && !skipChrome(el);

    function updateTimeline(t, duration) {
      if (captured(timelineClock)) timelineClock.textContent = `${fmtClock(t)} / ${fmtClock(duration)}`;
      if (!captured(timelineName) || !captured(timelineEta)) return;
      const sections = window.highway?.getSections?.() || [];
      const next = sections.find(s => Number(s.time) > t + 0.05);
      if (next) {
        timelineName.textContent = next.name || next.label || 'Section';
        timelineEta.textContent = `in ${(next.time - t).toFixed(1)}s`;
      }
    }

    return {
      async composeFrame(frameIndex) {
        // Rebuild as soon as every pending asset has decoded, otherwise keep
        // retrying slowly (a few times a second) for the rest of a bounded
        // number of attempts.
        if (pendingElements.size && layerBuilds < MAX_LAYER_BUILDS
          && (pendingReady() || frameIndex % LAYER_RETRY_FRAMES === 0)) await rebuildLayers();
        ctx.fillStyle = '#000'; ctx.fillRect(0, 0, target.width, target.height);
        ctx.fillStyle = rgbaVisible(ps.backgroundColor) ? ps.backgroundColor : '#0f172a';
        ctx.fillRect(ox, oy, pr.width * scale, pr.height * scale);
        for (const item of layers) {
          if (item.live) paint(item.d, ctx);
          else ctx.drawImage(item.layer, item.left, item.top);
        }
        refreshRoots(frameIndex % DYNAMIC_REFRESH_FRAMES === 0);
        for (const state of dynamicRoots) await paintRoot(state);
      },
      updateTimeline,
      dispose() { observer.disconnect(); }
    };
  }

  // Issue #9, phase 1: pick the fastest configuration the browser will
  // actually accept. 'prefer-hardware' is tried for every codec first; the
  // browser's own answer for that request is the compatibility fallback (an
  // empty acceleration means "let the UA choose", i.e. the configuration this
  // plugin used before hardware was requested).
  async function encoderConfig(width, height, fps, bitrate) {
    const codecs = width >= 3840 ? ['avc1.640033', 'avc1.4d4033'] : ['avc1.640028', 'avc1.4d4028', 'avc1.42001f'];
    for (const acceleration of ['prefer-hardware', '']) {
      for (const codec of codecs) {
        const config = { codec, width, height, bitrate, framerate: fps, latencyMode: 'quality', avc: { format: 'annexb' } };
        if (acceleration) config.hardwareAcceleration = acceleration;
        try {
          const support = await VideoEncoder.isConfigSupported(config);
          if (support.supported) {
            return { config: support.config || config,
              // `isConfigSupported` echoes the configuration it settled on;
              // that value is what the progress UI reports as the encoder.
              label: `${support.config?.hardwareAcceleration || acceleration || 'default'} ${codec}` };
          }
        } catch (_) { /* unsupported combination -- try the next one */ }
      }
    }
    throw new Error('This browser cannot encode H.264 at the selected resolution. Try 1080p or Chromium/Chrome.');
  }

  // Issue #9, phase 1: backpressure instead of repeated flush(). flush()
  // drains the whole encoder and serializes rendering behind it; here the
  // frame loop only pauses while the encode queue is above the high-water
  // mark and resumes from the encoder's own `dequeue` event once it falls back
  // below the low-water mark.
  //
  // `dequeue` only shipped in Chromium 106, but the capability gate at the top
  // of startExport() is satisfied from 94, so probe for it rather than
  // assuming: `'ondequeue' in encoder` is true exactly where the WebCodecs IDL
  // exposes the handler. Where it is absent the interval below is the only
  // resume path, so it runs far more often instead of stalling 50 ms per park.
  function createEncodeBackpressure(encoder) {
    const hasDequeue = 'ondequeue' in encoder;
    const pollMs = hasDequeue ? 50 : 4;
    let release = null;
    let watchdog = 0;
    const drain = () => {
      if (!release || encoder.encodeQueueSize > ENCODE_LOW_WATER) return;
      const resume = release; release = null;
      clearInterval(watchdog); watchdog = 0;
      resume();
    };
    if (hasDequeue) encoder.addEventListener('dequeue', drain);
    return {
      async wait() {
        if (encoder.encodeQueueSize <= ENCODE_HIGH_WATER) return;
        await new Promise(resolve => {
          release = resolve;
          watchdog = setInterval(drain, pollMs);
          drain();
        });
      },
      stop() {
        if (hasDequeue) encoder.removeEventListener('dequeue', drain);
        clearInterval(watchdog); watchdog = 0;
        if (release) { const resume = release; release = null; resume(); }
      }
    };
  }

  function createMemorySink() {
    const chunks = [];
    return {
      push(data) { chunks.push(data); },
      async drain() {},
      blob() { return new Blob(chunks, { type: 'video/h264' }); }
    };
  }

  // Issue #9, phase 3: hand encoded chunks straight to the export session
  // instead of retaining the whole elementary stream in browser memory. The
  // encoder's output callback is synchronous, so sends are serialized on a
  // promise chain and awaited at the frame loop's yield points.
  function createStreamSink(send) {
    let chain = Promise.resolve();
    let failure = null;
    return {
      push(data) {
        chain = chain.then(() => send(data)).catch(err => { failure = failure || err; });
      },
      async drain() { await chain; if (failure) throw failure; }
    };
  }

  function elapsedLabel(seconds) {
    const s = Math.max(0, seconds);
    return `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
  }

  async function openExportSession(payload) {
    try {
      const response = await fetch(`/api/plugins/${PLUGIN_ID}/sessions`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
      });
      if (!response.ok) return null;
      const data = await response.json();
      return data && typeof data.session === 'string' ? data.session : null;
    } catch (_) { return null; }
  }

  async function discardExportSession(session) {
    if (!session) return;
    try {
      await fetch(`/api/plugins/${PLUGIN_ID}/sessions/${encodeURIComponent(session)}`, { method: 'DELETE' });
    } catch (_) { /* the server also expires abandoned sessions */ }
  }

  async function startExport(dialog) {
    if (exporting) return;
    const audio = document.getElementById('audio');
    const player = document.getElementById('player');
    if (!audio || !player || !visible(player)) return status(dialog, 'Open a song in the player first.');
    const songInfo = window.highway?.getSongInfo?.() || {};
    const duration = Number.isFinite(audio.duration) && audio.duration > 0
      ? audio.duration : Number(songInfo.duration);
    if (!Number.isFinite(duration) || duration <= 0) return status(dialog, 'Song audio is not ready yet.');
    if (!window.VideoEncoder || !window.VideoFrame) return status(dialog, 'WebCodecs is unavailable. Use a current Chromium or Chrome build.');
    // Prefer the pack's complete mixdown over the core audio element. When the
    // stems plugin is active, audio.currentSrc may be only one instrument stem;
    // in JUCE mode the element may be empty while the native player is active.
    const audioUrl = songInfo.full_mix_url || audio.currentSrc || audio.src || window._juceAudioUrl;
    if (!audioUrl) return status(dialog, 'No complete song mix is available for export.');

    const [width, height] = dialog.querySelector('[data-ve-resolution]').value.split('x').map(Number);
    const fps = Number(dialog.querySelector('[data-ve-fps]').value);
    const bitrateMbps = Number(dialog.querySelector('[data-ve-bitrate]').value);
    // The transport, speed presets, help button, and player rail are playback
    // controls rather than song visuals, so exports always omit them.
    const includeChrome = false;
    settings = { width, height, fps, bitrate_mbps: bitrateMbps, include_chrome: false };
    fetch(`/api/plugins/${PLUGIN_ID}/settings`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(settings) }).catch(() => {});

    exporting = true; cancelled = false;
    const startBtn = dialog.querySelector('[data-ve-start]');
    const cancelBtn = dialog.querySelector('[data-ve-cancel]');
    startBtn.disabled = true; cancelBtn.hidden = false;
    const wasPaused = audio.paused, oldTime = window._juceMode && window.jucePlayer ? window.jucePlayer.currentTime : audio.currentTime;
    audio.pause();
    const split = window.feedBackSplitscreen || window.slopsmithSplitscreen;
    const splitActive = !!split?.isActive?.();
    const renderFrameAt = splitActive ? split?.renderFrameAt : window.highway?.renderFrameAt;
    if (typeof renderFrameAt !== 'function') {
      exporting = false; startBtn.disabled = false; cancelBtn.hidden = true;
      if (!wasPaused) audio.play().catch(() => {});
      return status(dialog, 'This feedBack build does not provide deterministic frame rendering. Update the host and Splitscreen plugin.');
    }
    const output = document.createElement('canvas'); output.width = width; output.height = height;
    const info = window.highway?.getSongInfo?.() || songInfo;
    const exportName = `${info.artist || 'feedback'}-${info.title || 'export'}`;
    let encoder;
    let backpressure = null;
    let compositor = null;
    let session = null;
    try {
      // beginOfflineRender()/createCompositor() both used to run BEFORE this
      // try — a rejection from createCompositor (or a throwing
      // beginOfflineRender) skipped the finally block entirely: exporting
      // stayed true (wedging every future click behind the `if (exporting)
      // return` guard at the top of this function), the Render/Cancel
      // buttons stayed disabled/hidden forever, and splitscreen was left in
      // offline-render mode with no matching endOfflineRender() call. Moved
      // inside the try so any failure here still hits the same
      // catch/finally as a failure mid-render.
      if (splitActive) split.beginOfflineRender?.();
      compositor = await createCompositor(output, includeChrome);

      // Issue #9, phase 3: when the host serves the song mix from its own
      // origin the server can fetch it directly, so the browser skips the
      // full audio download and re-upload entirely. Anything else (a remote
      // host, a blob: URL) is uploaded once into the session, and an older
      // host without session routes falls back to the single-request mux
      // endpoint with the whole elementary stream kept in memory.
      let audioBlob = null;
      let serverAudio = null;
      const sameOrigin = (() => {
        try { return new URL(audioUrl, location.href).origin === location.origin; } catch (_) { return false; }
      })();
      const payload = { fps, duration, filename: exportName };
      if (sameOrigin) {
        const resolved = new URL(audioUrl, location.href);
        serverAudio = resolved.pathname + resolved.search;
      }
      session = await openExportSession(
        serverAudio ? { ...payload, audio_url: serverAudio } : payload
      );
      if (!session || !serverAudio) {
        status(dialog, 'Loading source audio…', 0);
        const audioResponse = await fetch(audioUrl);
        if (!audioResponse.ok) throw new Error(`Could not load song audio (HTTP ${audioResponse.status}).`);
        audioBlob = await audioResponse.blob();
        if (session) {
          const upload = await fetch(`/api/plugins/${PLUGIN_ID}/sessions/${encodeURIComponent(session)}/audio`, {
            method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: audioBlob
          });
          if (!upload.ok) throw new Error(`Audio upload failed (HTTP ${upload.status}).`);
          audioBlob = null;
        }
      }
      const profile = await encoderConfig(width, height, fps, Math.round(bitrateMbps * 1e6));
      let encodeError = null;
      const sink = session
        ? createStreamSink(async data => {
          const response = await fetch(`/api/plugins/${PLUGIN_ID}/sessions/${encodeURIComponent(session)}/video`, {
            method: 'POST', headers: { 'Content-Type': 'video/h264' }, body: data
          });
          if (!response.ok) throw new Error(`Video upload failed (HTTP ${response.status}).`);
        })
        : createMemorySink();
      encoder = new VideoEncoder({
        output(chunk) { const bytes = new Uint8Array(chunk.byteLength); chunk.copyTo(bytes); sink.push(bytes); },
        error(err) { encodeError = err; }
      });
      encoder.configure(profile.config);
      backpressure = createEncodeBackpressure(encoder);
      const frameCount = Math.ceil(duration * fps);
      const startedAt = performance.now();
      const tickEvery = Math.max(1, Math.floor(fps / 2));
      for (let i = 0; i < frameCount; i++) {
        if (cancelled) throw new DOMException('Export cancelled', 'AbortError');
        if (encodeError) throw encodeError;
        const t = Math.min(duration, i / fps);
        const painted = renderFrameAt.call(splitActive ? split : window.highway, t);
        if (painted === false) throw new Error('The selected visualization is not ready for offline rendering.');
        compositor.updateTimeline(t, duration);
        await compositor.composeFrame(i);
        const frame = new VideoFrame(output, { timestamp: Math.round(t * 1e6), duration: Math.round(1e6 / fps) });
        encoder.encode(frame, { keyFrame: i % (fps * 2) === 0 }); frame.close();
        // Phase 1: pause production only while the encoder queue is over the
        // high-water mark; the encoder keeps working on what is already
        // queued, so rendering and encoding stay overlapped.
        await backpressure.wait();
        if (i % tickEvery === 0) {
          const elapsed = (performance.now() - startedAt) / 1000;
          const realtime = t > 0 ? t / elapsed : 0;
          const rate = realtime <= 0 ? 'measuring render speed'
            : realtime >= 1 ? `${realtime.toFixed(1)}x realtime`
            : `${(1 / realtime).toFixed(1)}x slower than realtime`;
          status(dialog, `Rendering frame ${i + 1} of ${frameCount} (${Math.round(i / frameCount * 100)}%) — `
            + `${elapsedLabel(elapsed)} elapsed, ${rate}`, i / frameCount * 0.9);
          await sink.drain();
          await new Promise(resolve => setTimeout(resolve, 0));
        }
      }
      await encoder.flush(); encoder.close();
      backpressure.stop(); backpressure = null;
      encoder = null;
      if (encodeError) throw encodeError;
      await sink.drain();

      const elapsed = (performance.now() - startedAt) / 1000;
      const realtime = duration > 0 ? duration / elapsed : 0;
      status(dialog, 'Muxing video and audio…', 0.94);
      let mux;
      if (session) {
        mux = await fetch(`/api/plugins/${PLUGIN_ID}/sessions/${encodeURIComponent(session)}/mux`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ...payload, audio_url: serverAudio })
        });
      } else {
        const form = new FormData();
        form.append('video', sink.blob(), 'frames.h264');
        form.append('audio', audioBlob, 'audio'); form.append('fps', String(fps));
        form.append('duration', String(duration));
        form.append('filename', exportName);
        mux = await fetch(`/api/plugins/${PLUGIN_ID}/mux`, { method: 'POST', body: form });
      }
      if (!mux.ok) { const e = await mux.json().catch(() => ({})); throw new Error(e.error || `Mux failed (HTTP ${mux.status}).`); }
      const mp4 = await mux.blob(); const url = URL.createObjectURL(mp4);
      const a = document.createElement('a'); a.href = url;
      const disposition = mux.headers.get('content-disposition') || '';
      const match = disposition.match(/filename="?([^";]+)"?/i);
      a.download = match ? match[1] : 'feedback-export.mp4'; a.click();
      setTimeout(() => URL.revokeObjectURL(url), 60000);
      status(dialog, `Done — ${a.download} (${elapsedLabel(elapsed)} render, `
        + `${realtime.toFixed(1)}x realtime, ${profile.label})`, 1);
      session = null;
    } catch (err) {
      if (encoder && encoder.state !== 'closed') { try { encoder.close(); } catch (_) {} }
      status(dialog, err?.name === 'AbortError' ? 'Export cancelled.' : `Export failed: ${err?.message || err}`);
    } finally {
      if (backpressure) backpressure.stop();
      if (compositor) compositor.dispose();
      await discardExportSession(session);
      if (splitActive) split.endOfflineRender?.();
      try { audio.currentTime = oldTime; } catch (_) {}
      if (!wasPaused && !cancelled) audio.play().catch(() => {});
      exporting = false; startBtn.disabled = false; cancelBtn.hidden = true;
    }
  }

  let mountTimer = setInterval(mountButton, 1000);
  function stopMountPoll() {
    if (mountTimer) { clearInterval(mountTimer); mountTimer = null; }
  }
  mountButton();
  window.addEventListener('beforeunload', stopMountPoll, { once: true });
  window.feedBack?.on?.('screen:changed', mountButton);
})();
