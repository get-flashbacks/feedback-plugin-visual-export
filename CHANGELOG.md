# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **Host compatibility is now checked by capability, and reported per
  component.** The single failure message ("does not provide deterministic
  frame rendering. Update the host and Splitscreen plugin") named both sides
  at once, so an out-of-date Split Screen was indistinguishable from an
  out-of-date feedBack host. The exporter now names the missing interface and
  the build that introduced it, and checks the two requirements separately:
  `highway.renderFrameAt()` is a feedBack requirement for every export, while
  Split Screen's `beginOfflineRender()` / `renderFrameAt()` /
  `endOfflineRender()` bridge is required only when a split layout is being
  exported.

### Removed

- **`minHost: "1.14.8"` from `plugin.json`.** The plugin spec defines
  `minHost` as the minimum *Host (core)* version, and `1.14.8` is a Split
  Screen plugin version, so the field claimed a core compatibility level the
  number says nothing about. No core version can replace it honestly:
  feedBack's `VERSION` has read `0.3.0-alpha.2` since 2026-08-10, before
  `highway.renderFrameAt()` landed in `f7c761c` on 2026-09-16, and feedBack
  publishes no releases — so `0.3.0-alpha.2` would admit hosts that cannot
  export, and anything higher would name a release that does not exist.
  `minHost` is also advisory-only in the current host. See
  get-flashbacks/feedBack issue #102. The manifest will regain the field when
  a feedBack release identifies a baseline.

### Fixed

- **A partial Split Screen offline-render bridge is reported as a version
  problem instead of a generic render failure.** The bridge calls were
  individually optional-chained, so a Split Screen build providing
  `renderFrameAt()` without `beginOfflineRender()` / `endOfflineRender()` got
  past the pre-flight check and then failed on the first frame with "The
  selected visualization is not ready for offline rendering" — which says
  nothing about the actual cause. (It did not corrupt the video: Split
  Screen's own `renderFrameAt()` returns `false` unless `beginOfflineRender()`
  has run.) All three hooks are now required together whenever Split Screen is
  active, and the message names which are missing.
- **`README.md` no longer lists `highway.getCanvas()` as a host dependency.**
  It is never called; highway canvases are read from the DOM snapshot instead.
