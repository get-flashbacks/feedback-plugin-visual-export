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
  issue #102 for the org-wide core-compatibility audit (it tracks this
  dependency but deliberately does **not** pin a version for it; see the
  `minHost` bullet). The export also calls
  `window.highway.getSongInfo()` (`screen.js:281`; `songInfo.full_mix_url`
  is the *preferred* audio source, falling back to `audio.currentSrc` /
  `audio.src` / `window._juceAudioUrl` at `screen.js:289` — the whole
  export only bails when all four are empty, so a host with no
  `getSongInfo` at all can still export via the `<audio>` element) and
  `window.highway.getSections()` for HUD text (`screen.js:230`) — but both
  are long-standing core APIs in `get-flashbacks/feedBack`'s
  `static/highway.js`, present in all 35 commits back to its root commit
  `6c110398` (Jun 16), and are **not** part of the `f7c761c` floor (issue
  #102 attributes only `renderFrameAt`/`renderFrame`/`setExternalFrameDriver`
  to that commit). `getCanvas()` is never called in `screen.js` — canvases are
  found as `HTMLCanvasElement` instances during the DOM snapshot and drawn with
  `ctx.drawImage`. `README.md` used to list `getCanvas()` as a dependency; that
  was wrong and is now removed, so don't reintroduce it.
- **Split-layout export:** additionally needs Splitscreen's
  `beginOfflineRender()` / `renderFrameAt(time)` / `endOfflineRender()`
  bridge. Split Screen's own v1.14.20 release notes describe these as
  "deterministic offline-render hooks added in `2301dd5` and completed in
  `87e3622a`" — use that attribution rather than either commit alone. The
  bridge first shipped in manifest **v1.14.8**: `87e3622a` ("fix: support
  deterministic offline export") is the commit that set that repo's manifest
  to 1.14.8, and its parent `7cd0ec5` is manifest `1.14.7` with no
  `beginOfflineRender`. `2301dd5` ("feat: coordinate split panel frames",
  02:33Z) lands 99 minutes after `87e3622a` (00:51Z) on the same day and
  is also inside 1.14.8. Cite the floor as 1.14.8 and cite both commits;
  do not assert that one of them "introduced" the bridge on its own.
  Before `87e3622a` existed, **no** Splitscreen version implemented this
  bridge at all — issues #6 and #7 in this repo document that history; #7 in
  particular is worth reading before citing any Splitscreen version as a
  floor, since the first two rounds of that issue concluded (correctly, at
  the time) that no working version existed yet.
  The three hooks are genuinely all-or-nothing rather than a nicety: the
  plugin's `renderFrameAt` starts with `if (!active ||
  !_offlineRenderActive || ...) return false`, so a host exposing
  `renderFrameAt` without `beginOfflineRender` can never paint a frame.
- **`plugin.json` deliberately declares no `minHost`.** Issue #6 asked for
  the host floor to be declared, and that cannot be done honestly: per
  `feedBack-plugin-spec` §4.1, `minHost` means the minimum **Host (core)**
  version, and the value that sat there before (`1.14.8`) was a *Splitscreen*
  version — the exact core/plugin conflation to avoid. There is also no
  core version that is a true floor: feedBack's `VERSION` file has read
  `0.3.0-alpha.2` since commit `ec1157ac` (2026-08-10), five weeks before
  `f7c761c`, and it still reads `0.3.0-alpha.2` on `main` — `f7c761c`'s own
  parent reports the same string — while feedBack publishes no releases or
  tags at all. So `0.3.0-alpha.2` admits ~5 weeks of pre-`renderFrameAt`
  builds, and any higher number would name a release that does not exist.
  feedBack issue #102 says the same thing and proposes publishing a
  traceable baseline version instead. `minHost` is advisory-only in the
  current host anyway (no `minHost` reference in feedBack's
  `static/js/plugin-loader.js`), so an inaccurate value buys nothing and
  would mislead a future enforcing host. The requirement is instead enforced
  by capability in `frameDriverProblem()` (`screen.js`), which reports the
  core gap and the Splitscreen gap separately. When a real feedBack release
  identifies a baseline, add `minHost` back at that version.
- **WebCodecs (browser, separate from the host version):** Chromium-family
  H.264 `VideoEncoder` support. Checked independently in `startExport` before
  any host lookup, so a missing WebCodecs build reports as itself rather than
  as a compatibility problem.
- **FFmpeg (server, separate again):** reachable via `PATH` or the desktop
  app's bundled `resources/bin`. Only needed once frames are rendered, so its
  absence surfaces from `routes.py` as a muxing failure (503) rather than as a
  host compatibility error.

## Frame capture: static snapshot + per-frame dynamic layer

`screen.js`'s compositor snapshots the DOM once at export start (media
elements — canvas/video/img — plus one exception below) and only redraws
per frame what's expected to change (highway canvases via
`renderFrameAt`, dynamic text/lyrics). This is why export speed is
independent of song duration but does depend on visualization cost and
resolution.

**Fixed: Staff View (alphaTab) notation no longer goes stale.** Staff View
renders sheet music as `<svg>`, swapped out by alphaTab as playback scrolls to
a new system, so a one-time snapshot froze whatever was on screen at t=0.
Resolved by PR **#4** (commit `fb03961`, "Fix Staff View notation never
appearing in exported video"), which excludes Staff View's container from the
one-time snapshot and re-describes/re-rasterizes it every composited frame,
using a `WeakMap` cache keyed by element so an unchanged SVG isn't re-encoded
each frame — the same way the split-lyrics pane already handles its own
per-timestamp DOM rebuilds. Issue #2 is closed (2026-09-28). Earlier revisions
of this file listed this as an open gap and pointed at PR #4 as unmerged; that
warning was left in place after the fix landed.

**Known non-goal:** Jumping Tab panes don't provide deterministic frame
rendering and aren't supported in offline split exports (README's own
"Limits" section) — this is a stated limitation, not a bug to fix here
without a host-side contract for it first.

## Mount lifecycle

`mountButton()` injects the "Export video" control into the v3 player
chrome's Plugins rail. A `setInterval` poll (`screen.js:390`) exists only
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
