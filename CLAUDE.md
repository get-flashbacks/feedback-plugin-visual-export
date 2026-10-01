# Visual Export — AI Agent Guide

Renders the current player layout (main highway, or the active Splitscreen
arrangement) to an MP4 without live playback: the browser drives each
highway through explicit song timestamps, composites the visible canvases,
encodes H.264 via WebCodecs, and the plugin server muxes those frames with
the original audio via FFmpeg. See `README.md` for the user-facing flow;
this file covers the parts an agent needs that the README doesn't.

## Host contract this plugin depends on

- **Single-highway export:** `window.highway.renderFrameAt(time)`, added
  to feedBack core in `f7c761c` (Sep 16) — see `get-flashbacks/feedBack`
  issue #102 for the org-wide core-compatibility audit that pins this
  floor (it does not pin the Splitscreen floor — see that bullet). The
  export also calls
  `window.highway.getSongInfo()` (`screen.js:533`; `songInfo.full_mix_url`
  is the *preferred* audio source, falling back to `audio.currentSrc` /
  `audio.src` / `window._juceAudioUrl` (`screen.js:541`) — the whole
  export only bails when all four are empty, so a host with no
  `getSongInfo` at all can still export via the `<audio>` element) and
  `window.highway.getSections()` for HUD text (`screen.js:392`) — but both
  are long-standing core APIs in `get-flashbacks/feedBack`'s
  `static/highway.js`, present in all 35 commits back to its root commit
  `6c110398` (Jun 16), and are **not** part of the `f7c761c` floor (issue
  #102 attributes only `renderFrameAt`/`renderFrame`/`setExternalFrameDriver`
  to that commit). `getCanvas()` is not called anywhere in `screen.js`
  despite being listed as a dependency in `README.md:40` — don't rely on
  it.
- **Split-layout export:** additionally needs Splitscreen's
  `beginOfflineRender()` / `renderFrameAt(time)` / `endOfflineRender()`
  bridge, added in `feedback-plugin-splitscreen` commit `87e3622a`
  (manifest **v1.14.8** — not pinned by issue #102, which covers only
  feedBack-core floors; this Splitscreen version comes from that repo's
  own history). Before that commit existed, **no** Splitscreen version
  implemented this bridge at all — issues #6 and #7 in this repo document
  that history; #7 in particular is worth reading before citing any
  Splitscreen version as a floor, since the first two rounds of that issue
  concluded (correctly, at the time) that no working version existed yet.
- **`minHost: "1.0.0"`** in `plugin.json` reflects neither of the above —
  see issue #6. Don't treat it as accurate; it's a known-wrong placeholder
  tracked for correction, not a decision already made.
- Chromium-family WebCodecs H.264 support, and FFmpeg reachable via `PATH`
  or the desktop app's bundled `resources/bin`.

## Frame capture: precomposed layers + per-frame dynamic roots

`screen.js`'s compositor classifies the player subtree once at export start:
canvases and videos stay dynamic (renderFrameAt repaints them every frame),
everything else that is not inside a lyrics/Staff View root is immutable and
gets flattened into offscreen layers cut at every live node, so z-order is
preserved. Layers are rebuilt while a referenced image/SVG is still decoding
(`MAX_LAYER_BUILDS` bounds that retry loop) so late-loading art isn't missing
from the whole export. Lyrics and Staff View roots are cached per root and
invalidated by a `MutationObserver` on the player; their rects are re-measured
every frame because panes scroll, while style-derived fields are only recomputed
after a real change. Export speed is independent of song duration but does
depend on visualization cost and resolution.

**Don't flatten across live nodes.** Precomposing a run that contains a
highway canvas is what the original implementation effectively did not do, and
it is the whole reason runs are cut: a live canvas must be composited between
the static pixels above and below it.

**Layer bounds are snapshot-based, under the lock.** `video_bytes` is the
file's size, so it may only be read *inside* `append_lock`. Snapshotting before
the acquire gives every in-flight append the same stale base, each gets a full
`MAX_VIDEO_BYTES` of headroom, and a rollback can truncate below the accepted
prefix. A rejected append truncates back to that snapshot — never unlink, or a
single over-limit chunk destroys the whole render.

**Known gap: Staff View (alphaTab) notation goes stale or never
appears.** Staff View renders sheet music as `<svg>`, swapped out by
alphaTab as playback scrolls to a new system. Its container is excluded from
the precomposed layers and re-described (and re-rasterized per element
reference, via a `WeakMap` cache) instead, the same way the split-lyrics pane
handles its own per-timestamp DOM rebuilds — so this behaviour is now shared
with lyrics rather than special-cased, but the underlying issue is tracked as
issue **#2**. Don't assume that bug is fixed without checking whether #4 has
merged.

## Encoder pipeline and transport

The frame loop never calls `flush()` except once at the end. It applies
backpressure from the encoder's `dequeue` event instead
(`createEncodeBackpressure`, high/low watermarks) so rendering, compositing,
and encoding stay pipelined. `dequeue` only exists from Chromium 106 while the
`VideoEncoder`/`VideoFrame` capability gate is satisfied from 94, so
`createEncodeBackpressure` probes `'ondequeue' in encoder` and polls faster
when it is absent — don't assume the event exists. Hardware encoding is
requested via `hardwareAcceleration: 'prefer-hardware'` first, with the
previous configuration as the fallback. The progress UI reports elapsed time
and realtime factor so changes are measurable — see issue #9.

Encoded chunks go to a server-side export session (`/sessions`, streamed to
`/sessions/{id}/video`) when those routes exist; the browser falls back to
buffering everything and POSTing `/mux` when the host predates them or the
audio source needs uploading. Session temp dirs are cleaned on cancel, on mux,
and by a TTL sweep — keep all three paths.

**Server-side audio resolution is opt-in and origin-pinned.** The browser only
ever sends a site-relative `audio_url`, but that constrains the path, not the
destination: `urlopen` follows cross-origin 30x, and `request.base_url` is built
from the client-controlled `Host` header. So the origin comes from the
`FEEDBACK_PUBLIC_ORIGIN` environment variable alone (`_expected_origin()`), and
`_AUDIO_OPENER` refuses every redirect plus double-checks `response.geturl()`.

The browser and server must agree on who supplies the audio, or every export
fails at mux time *after* the whole render: `create_session` returns
`audio_fetch` and `screen.js` only skips the upload when it is true. For the
same reason, audio resolution happens **before** `mux_session` pops the session —
a fetch failure answers 409 `audio_required` and keeps the session, so the
browser can upload the mix and retry. Don't move either decision earlier or
later. Don't "simplify" the origin by reading it off the request.

**Known non-goal:** Jumping Tab panes don't provide deterministic frame
rendering and aren't supported in offline split exports (README's own
"Limits" section) — this is a stated limitation, not a bug to fix here
without a host-side contract for it first.

## Mount lifecycle

`mountButton()` injects the "Export video" control into the v3 player
chrome's Plugins rail. A `setInterval` poll (`screen.js:711`) exists only
to catch the slot not being ready yet at initial page load (a v3-chrome
mount race); once the first successful mount happens, the poll stops
itself and all future remounts are covered by the `screen:changed`
listener instead — don't "fix" the poll into running continuously, that
would reintroduce exactly the per-frame-adjacent polling cost this
codebase's sibling plugins are warned against elsewhere in this org.

## Versioning

Bump `version` in `plugin.json` whenever a change is user-visible, same
convention as every other plugin in this org — the version cache-busts
the served JS/CSS URL. `CHANGELOG.md`'s `[Unreleased]` section should be
updated alongside; as of this writing it has no entries yet despite real
shipped changes (plugin rename, export-capture improvements, the
persist-wedges-on-setup-failure fix) — worth backfilling before the next
release rather than assuming "empty Unreleased" means "nothing to
document."

## Testing

```bash
python3 -m pip install pytest fastapi python-multipart   # none are bundled — no requirements manifest in this repo
python3 -m pytest tests/test_routes.py
```

In a clean checkout with no dependencies preinstalled, `python3 -m pytest`
fails outright (`No module named pytest`); after installing pytest,
`tests/test_routes.py` fails to even **collect** (`No module named
'fastapi'`, a module-scope import at `tests/test_routes.py:6`) — this is
the actual first-run failure mode, not the narrower one below. Once
`pytest` and `fastapi` are installed, one test in this file
(`test_setup_registers_settings_capabilities_and_mux_routes`) additionally
needs `python-multipart` — without it, only that one test fails with a
clear `RuntimeError`, not a collection error; the rest of the suite is
unaffected.

No JS test harness exists in this repo yet — `screen.js` has no
`node --test`-reachable exports. Verifying a `screen.js` change today
means exercising it in a real browser session against a live host.
