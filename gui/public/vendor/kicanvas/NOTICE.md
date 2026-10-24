# KiCanvas vendored build

Source: https://kicanvas.org/kicanvas/kicanvas.js
Upstream: https://github.com/theacodes/kicanvas
Retrieved: 2026-10-24 UTC
Upstream SHA256: ca910f25276c3efb9aacb3a5d6341d4d9af4736d4c875fb0440d2cc856865ab7

Changes: removed automatic Google Fonts stylesheet injection; replaced the rounded display font with the system UI font. No parsing or EDA logic was changed. License and third-party notices are included in LICENSE.md.

EEA's KiCanvasBoard.tsx adapter uses the pinned build's board viewer fit/flip methods and hides its bottom toolbar. Verify that adapter and the viewer browser checks when updating this file.
