# KiCanvas vendored build

Source: https://kicanvas.org/kicanvas/kicanvas.js
Upstream: https://github.com/theacodes/kicanvas
Retrieved: 2026-10-24 UTC
Upstream SHA256: ca910f25276c3efb9aacb3a5d6341d4d9af4736d4c875fb0440d2cc856865ab7

Changes: removed automatic Google Fonts stylesheet injection; replaced the rounded display font with the system UI font; added CSS variable hooks for panel-title, section-label and body typography inside the native shadow roots. EEA supplies Cormorant small caps for headings and IBM Plex Mono for labels through its stylesheet. No parsing or EDA logic was changed. License and third-party notices are included in LICENSE.md.

EEA's KiCanvasBoard.tsx adapter uses the pinned build's board viewer fit/flip methods and enables its full sidebar and bottom toolbar. Layers, objects, footprints, nets, properties, board information and help are available through the native sidebar. The activation overlay is disabled; download and flip controls are provided by EEA's toolbar. Verify that adapter and the viewer browser checks when updating this file.

The build also imports `aether-forge-theme.js` and registers its PCB rendering palette alongside Witch Hazel and KiCad in Preferences → Theme. It uses the native color objects and preference persistence; the built-in themes and default remain available. The schematic palette is inherited from Witch Hazel.

The local `material-symbols-outlined.ttf` is a Google Material Symbols Outlined subset for the bundled viewer's icon names, retrieved from Google Fonts on 2026-10-24. Source project: https://github.com/google/material-design-icons. The subset includes category, check, close, download, flip, folder, help, hub, info, interests, layers, list, memory, search, settings, visibility and visibility_off. Its Apache 2.0 license is included in MATERIAL-SYMBOLS-LICENSE.txt. It is served locally; no Google Fonts requests are made at runtime.
