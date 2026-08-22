// SPDX-License-Identifier: AGPL-3.0-or-later
(function visualExportPlugin() {
  'use strict';
  const PLUGIN_ID = 'visual-export';
  if (window.__visualExportSetup) return;
  window.__visualExportSetup = true;

  let settings = { width: 1920, height: 1080, fps: 30, bitrate_mbps: 10, include_chrome: false };
  let cancelled = false;
  let exporting = false;
  let modal = null;

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

  function createCompositor(target, includeChrome) {
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
    const skipChrome = el => !includeChrome && !!el.closest('#player-controls,#player-footer,#v3-railzone,[id^="v3-rail-pop-"]');
    const lyricSelector = '.splitscreen-lyrics-pane,.splitscreen-lyrics-overlay';

    function describe(el) {
      if (!visible(el) || skipChrome(el)) return null;
      const r = el.getBoundingClientRect();
      if (r.right <= pr.left || r.left >= pr.right || r.bottom <= pr.top || r.top >= pr.bottom) return null;
      const s = getComputedStyle(el);
      const direct = Array.from(el.childNodes).some(n => n.nodeType === Node.TEXT_NODE && n.textContent.trim());
      const media = el instanceof HTMLCanvasElement || el instanceof HTMLVideoElement || el instanceof HTMLImageElement;
      const bg = rgbaVisible(s.backgroundColor), border = Number.parseFloat(s.borderTopWidth) || 0;
      if (!media && !direct && !bg && !border) return null;
      return { el, r, media, direct, bg, border, z: Number.parseInt(s.zIndex, 10) || 0,
        opacity: Math.max(0, Math.min(1, Number(s.opacity) || 1)), backgroundColor: s.backgroundColor,
        borderColor: s.borderTopColor, radius: Number.parseFloat(s.borderRadius) || 0,
        fontSize: Number.parseFloat(s.fontSize) || 16, fontStyle: s.fontStyle,
        fontWeight: s.fontWeight, fontFamily: s.fontFamily, color: s.color, textAlign: s.textAlign };
    }

    const staticNodes = Array.from(player.querySelectorAll('*'))
      .filter(el => !el.closest(lyricSelector)).map(describe).filter(Boolean)
      .sort((a, b) => a.z - b.z);

    function paint(d) {
      const { el, r } = d;
      if (!el.isConnected) return;
      const x = ox + (r.left - pr.left) * sx, y = oy + (r.top - pr.top) * sy;
      const w = r.width * sx, h = r.height * sy;
      ctx.save(); ctx.globalAlpha = d.opacity;
      if (d.bg) {
        roundedRect(ctx, x, y, w, h, d.radius * scale);
        ctx.fillStyle = d.backgroundColor; ctx.fill();
      }
      if (d.media) {
        try { ctx.drawImage(el, x, y, w, h); } catch (_) { /* unloaded/tainted visual asset */ }
      }
      if (d.border > 0 && rgbaVisible(d.borderColor)) {
        roundedRect(ctx, x, y, w, h, d.radius * scale);
        ctx.strokeStyle = d.borderColor; ctx.lineWidth = d.border * scale; ctx.stroke();
      }
      const direct = d.direct
        ? Array.from(el.childNodes).filter(n => n.nodeType === Node.TEXT_NODE)
          .map(n => n.textContent).join(' ').replace(/\s+/g, ' ').trim()
        : '';
      if (direct && !['SCRIPT', 'STYLE', 'OPTION'].includes(el.tagName)) {
        ctx.font = `${d.fontStyle} ${d.fontWeight} ${d.fontSize * scale}px ${d.fontFamily}`;
        ctx.fillStyle = d.color || '#fff'; ctx.textBaseline = 'middle';
        const align = d.textAlign === 'center' ? 'center' : d.textAlign === 'right' ? 'right' : 'left';
        ctx.textAlign = align;
        const tx = align === 'center' ? x + w / 2 : align === 'right' ? x + w : x;
        ctx.fillText(direct, tx, y + h / 2, w || undefined);
      }
      ctx.restore();
    }

    return function composePlayerFrame() {
      ctx.fillStyle = '#000'; ctx.fillRect(0, 0, target.width, target.height);
      ctx.fillStyle = rgbaVisible(ps.backgroundColor) ? ps.backgroundColor : '#0f172a';
      ctx.fillRect(ox, oy, pr.width * scale, pr.height * scale);
      for (const d of staticNodes) paint(d);
      // Split lyrics rebuild their spans at each timestamp, so only this tiny
      // subtree is described per frame; the rest of the player is cached.
      for (const root of player.querySelectorAll(lyricSelector)) {
        const dynamic = [root, ...root.querySelectorAll('*')].map(describe).filter(Boolean);
        for (const d of dynamic) paint(d);
      }
    };
  }

  function updateTimelineDom(t, duration) {
    const fmt = value => `${Math.floor(value / 60)}:${String(Math.floor(value % 60)).padStart(2, '0')}`;
    const clock = document.getElementById('hud-time');
    if (clock) clock.textContent = `${fmt(t)} / ${fmt(duration)}`;
    const sections = window.highway?.getSections?.() || [];
    const next = sections.find(s => Number(s.time) > t + 0.05);
    const name = document.getElementById('v3-upnext-name');
    const eta = document.getElementById('v3-upnext-eta');
    if (next && name && eta) { name.textContent = next.name || next.label || 'Section'; eta.textContent = `in ${(next.time - t).toFixed(1)}s`; }
  }

  async function encoderConfig(width, height, fps, bitrate) {
    const codecs = width >= 3840 ? ['avc1.640033', 'avc1.4d4033'] : ['avc1.640028', 'avc1.4d4028', 'avc1.42001f'];
    for (const codec of codecs) {
      const config = { codec, width, height, bitrate, framerate: fps, latencyMode: 'quality', avc: { format: 'annexb' } };
      try { if ((await VideoEncoder.isConfigSupported(config)).supported) return config; } catch (_) {}
    }
    throw new Error('This browser cannot encode H.264 at the selected resolution. Try 1080p or Chromium/Chrome.');
  }

  async function startExport(dialog) {
    if (exporting) return;
    const audio = document.getElementById('audio');
    const player = document.getElementById('player');
    if (!audio || !player || !visible(player)) return status(dialog, 'Open a song in the player first.');
    if (!Number.isFinite(audio.duration) || audio.duration <= 0) return status(dialog, 'Song audio is not ready yet.');
    if (!window.VideoEncoder || !window.VideoFrame) return status(dialog, 'WebCodecs is unavailable. Use a current Chromium or Chrome build.');
    const audioUrl = audio.currentSrc || audio.src;
    if (!audioUrl) return status(dialog, 'This audio route cannot be exported yet. Select the browser audio output.');

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
    const wasPaused = audio.paused, oldTime = audio.currentTime;
    audio.pause();
    const split = window.feedBackSplitscreen || window.slopsmithSplitscreen;
    const splitActive = !!split?.isActive?.();
    const renderFrameAt = splitActive ? split?.renderFrameAt : window.highway?.renderFrameAt;
    if (typeof renderFrameAt !== 'function') {
      exporting = false; startBtn.disabled = false; cancelBtn.hidden = true;
      if (!wasPaused) audio.play().catch(() => {});
      return status(dialog, 'This feedBack build does not provide deterministic frame rendering. Update the host and Splitscreen plugin.');
    }
    if (splitActive) split.beginOfflineRender?.();
    const output = document.createElement('canvas'); output.width = width; output.height = height;
    const composeFrame = createCompositor(output, includeChrome);
    let encoder;
    try {
      status(dialog, 'Loading source audio…', 0);
      const audioResponse = await fetch(audioUrl);
      if (!audioResponse.ok) throw new Error(`Could not load song audio (HTTP ${audioResponse.status}).`);
      const audioBlob = await audioResponse.blob();
      const config = await encoderConfig(width, height, fps, Math.round(bitrateMbps * 1e6));
      const chunks = [];
      let encodeError = null;
      encoder = new VideoEncoder({
        output(chunk) { const bytes = new Uint8Array(chunk.byteLength); chunk.copyTo(bytes); chunks.push(bytes); },
        error(err) { encodeError = err; }
      });
      encoder.configure(config);
      const frameCount = Math.ceil(audio.duration * fps);
      for (let i = 0; i < frameCount; i++) {
        if (cancelled) throw new DOMException('Export cancelled', 'AbortError');
        if (encodeError) throw encodeError;
        const t = Math.min(audio.duration, i / fps);
        const painted = renderFrameAt.call(splitActive ? split : window.highway, t);
        if (painted === false) throw new Error('The selected visualization is not ready for offline rendering.');
        updateTimelineDom(t, audio.duration);
        composeFrame();
        const frame = new VideoFrame(output, { timestamp: Math.round(t * 1e6), duration: Math.round(1e6 / fps) });
        encoder.encode(frame, { keyFrame: i % (fps * 2) === 0 }); frame.close();
        if (encoder.encodeQueueSize > 8) await encoder.flush();
        if (i % Math.max(1, Math.floor(fps / 2)) === 0) {
          status(dialog, `Rendering frame ${i + 1} of ${frameCount} (${Math.round(i / frameCount * 100)}%)`, i / frameCount * 0.9);
          await new Promise(resolve => setTimeout(resolve, 0));
        }
      }
      await encoder.flush(); encoder.close(); encoder = null;
      if (encodeError) throw encodeError;

      status(dialog, 'Muxing video and audio…', 0.94);
      const form = new FormData();
      form.append('video', new Blob(chunks, { type: 'video/h264' }), 'frames.h264');
      form.append('audio', audioBlob, 'audio'); form.append('fps', String(fps));
      form.append('duration', String(audio.duration));
      const info = window.highway?.getSongInfo?.() || {};
      form.append('filename', `${info.artist || 'feedback'}-${info.title || 'export'}`);
      const mux = await fetch(`/api/plugins/${PLUGIN_ID}/mux`, { method: 'POST', body: form });
      if (!mux.ok) { const e = await mux.json().catch(() => ({})); throw new Error(e.error || `Mux failed (HTTP ${mux.status}).`); }
      const mp4 = await mux.blob(); const url = URL.createObjectURL(mp4);
      const a = document.createElement('a'); a.href = url;
      const disposition = mux.headers.get('content-disposition') || '';
      const match = disposition.match(/filename="?([^";]+)"?/i);
      a.download = match ? match[1] : 'feedback-export.mp4'; a.click();
      setTimeout(() => URL.revokeObjectURL(url), 60000);
      status(dialog, `Done — ${a.download}`, 1);
    } catch (err) {
      if (encoder && encoder.state !== 'closed') { try { encoder.close(); } catch (_) {} }
      status(dialog, err?.name === 'AbortError' ? 'Export cancelled.' : `Export failed: ${err?.message || err}`);
    } finally {
      if (splitActive) split.endOfflineRender?.();
      try { audio.currentTime = oldTime; } catch (_) {}
      if (!wasPaused && !cancelled) audio.play().catch(() => {});
      exporting = false; startBtn.disabled = false; cancelBtn.hidden = true;
    }
  }

  mountButton();
  const mountTimer = setInterval(mountButton, 1000);
  window.addEventListener('beforeunload', () => clearInterval(mountTimer), { once: true });
  window.feedBack?.on?.('screen:changed', mountButton);
})();
