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
  `window.highway.getSongInfo()` (`screen.js:219`; `songInfo.full_mix_url`
  is the *preferred* audio source, falling back to `audio.currentSrc` /
  `audio.src` / `window._juceAudioUrl` at `screen.js:227` — the whole
  export only bails when all four are empty, so a host with no
  `getSongInfo` at all can still export via the `<audio>` element) and
  `window.highway.getSections()` for HUD text (`screen.js:198`) — but both
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

## Frame capture: static snapshot + per-frame dynamic layer

`screen.js`'s compositor snapshots the DOM once at export start (media
elements — canvas/video/img — plus one exception below) and only redraws
per frame what's expected to change (highway canvases via
`renderFrameAt`, dynamic text/lyrics). This is why export speed is
independent of song duration but does depend on visualization cost and
resolution.

**Known gap: Staff View (alphaTab) notation goes stale or never
appears.** Staff View renders sheet music as `<svg>`, swapped out by
alphaTab as playback scrolls to a new system. The one-time snapshot
rasterizes each top-level `<svg>` **once, at export start** — see the
comment at `screen.js:131-135` — so a Staff View export shows either
nothing (if alphaTab hadn't rendered yet at snapshot time) or whatever was
on screen at t=0, frozen, for the entire video. This is tracked as issue
**#2** (open) with a candidate fix in PR **#4** (open, not yet merged as
of this writing): exclude Staff View's container from the one-time
snapshot and instead re-describe/re-rasterize it every composited frame
(via a `WeakMap` cache keyed by element, so an unchanged SVG isn't
re-encoded every frame), the same way the split-lyrics pane already
handles its own per-timestamp DOM rebuilds. **Do not assume this is fixed
without checking whether #4 has merged** — the bug reproduces against the
`main` branch's current `screen.js` as of this file's writing.

**Known non-goal:** Jumping Tab panes don't provide deterministic frame
rendering and aren't supported in offline split exports (README's own
"Limits" section) — this is a stated limitation, not a bug to fix here
without a host-side contract for it first.

## Mount lifecycle

`mountButton()` injects the "Export video" control into the v3 player
chrome's Plugins rail. A `setInterval` poll (`screen.js:327`) exists only
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
