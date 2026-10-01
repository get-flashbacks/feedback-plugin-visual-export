# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Render progress now reports elapsed time, how far the render is from real
  time, and — when the export finishes — the render time and the encoder
  configuration that was used.
- Export sessions let the browser stream encoded H.264 chunks to the server as
  they are produced instead of keeping the whole elementary stream in memory.
  The session's total encoded bytes stay capped by the existing 2 GB limit
  across all appends. Cancelling an export still deletes the session
  immediately, and abandoned sessions are expired server-side.
- Set `FEEDBACK_PUBLIC_ORIGIN` (for example `http://127.0.0.1:5173`) to let the
  server fetch a same-host song mix itself, so the browser no longer downloads
  and re-uploads it. Without it, or for a mix the server cannot fetch, the
  browser uploads the audio once to the session as before. The fetch refuses
  redirects and only ever contacts that configured origin — the `Host` header
  is not trusted to choose it.

### Changed

- The frame loop now applies encoder backpressure from the encoder's `dequeue`
  event (pause above a high-water mark, resume below a low-water mark) instead
  of repeatedly calling `flush()`, which drained the encoder and serialized
  rendering behind it. `flush()` is now only called once, to finalize.
- Hardware H.264 encoding is requested first and falls back to the previous
  configuration when the browser cannot provide it.
- The static part of the scene is precomposed into z-safe offscreen layers
  rather than repainted from cached descriptors on every frame. Live highway
  canvases, videos, lyrics, and Staff View are still painted per frame, and
  their descriptors are now cached and invalidated from a mutation observer on
  the affected subtree rather than re-queried every frame.
- The HUD timeline text is only written while the chrome displaying it is
  captured. The `#player-hud` overlay is excluded from capture alongside the
  controls, so those updates no longer reach exported frames. The guard stays
  in step with the capture list in case this changes.
