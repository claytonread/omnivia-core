/* ============================================================================
   OmniVia — local icon set (offline).

   Why this file exists: lib/icon-boot.js injects Lucide from unpkg. That is a
   network dependency, so the handoff could not be validated offline and the
   packaged Electron app would ship a request it cannot rely on.

   This module renders the glyphs these screens actually use, from inline SVG,
   with no request of any kind. It keeps Lucide's geometry conventions — 24×24
   box, 1.6 stroke, round caps and joins, currentColor — so icons still inherit
   text colour and adapt across themes and states.

   It defers to window.lucide when a host has already provided it (the full app
   shell), so the two can coexist: this is a floor, not a replacement.

   Design intent remains SF Symbols in the native build; map by name.
   ============================================================================ */
(function () {
  "use strict";

  /* Each entry is the inner markup of a 24×24 viewBox. */
  var P = {
    "activity": '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
    "alert-circle": '<circle cx="12" cy="12" r="10"/><path d="M12 8v4"/><path d="M12 16h.01"/>',
    "alert-triangle": '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
    "arrow-right": '<path d="M5 12h14"/><path d="m12 5 7 7-7 7"/>',
    /* Added for the discovery amendment: one glyph per detector category, so a
       row's icon says what KIND of thing was found before its label is read. */
    "box": '<path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/><path d="m3.3 7 8.7 5 8.7-5"/><path d="M12 22V12"/>',
    "code": '<path d="m16 18 6-6-6-6"/><path d="m8 6-6 6 6 6"/>',
    "git-merge": '<circle cx="18" cy="18" r="3"/><circle cx="6" cy="6" r="3"/><path d="M6 21V9a9 9 0 0 0 9 9"/>',
    "layout-grid": '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/>',
    "monitor-off": '<path d="M17 17H4a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2"/><path d="M22 15V7a2 2 0 0 0-2-2H9"/><path d="M8 21h8"/><path d="M12 17v4"/><path d="m2 2 20 20"/>',
    "plug": '<path d="M12 22v-5"/><path d="M9 8V2"/><path d="M15 8V2"/><path d="M18 8v5a6 6 0 0 1-12 0V8Z"/>',
    "puzzle": '<path d="M13 3.5a2 2 0 0 1 4 0V5h3a1 1 0 0 1 1 1v3.5h-1.5a2 2 0 0 0 0 4H21V17a1 1 0 0 1-1 1h-3v1.5a2 2 0 0 1-4 0V18H6a1 1 0 0 1-1-1v-3.5h1.5a2 2 0 0 0 0-4H5V6a1 1 0 0 1 1-1h7Z"/>',
    "search-x": '<path d="m13.5 8.5-5 5"/><path d="m8.5 8.5 5 5"/><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
    "terminal": '<path d="m4 17 6-6-6-6"/><path d="M12 19h8"/>',
    "badge-check": '<path d="M3.85 8.62a4 4 0 0 1 4.78-4.77 4 4 0 0 1 6.74 0 4 4 0 0 1 4.78 4.78 4 4 0 0 1 0 6.74 4 4 0 0 1-4.77 4.78 4 4 0 0 1-6.75 0 4 4 0 0 1-4.78-4.77 4 4 0 0 1 0-6.76Z"/><path d="m9 12 2 2 4-4"/>',
    "ban": '<circle cx="12" cy="12" r="10"/><path d="m4.9 4.9 14.2 14.2"/>',
    "bell": '<path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/><path d="M21 18H3v-1l1.5-1.5V10a7.5 7.5 0 0 1 15 0v5.5L21 17Z"/>',
    "check": '<path d="M20 6 9 17l-5-5"/>',
    "check-circle-2": '<circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/>',
    "chevron-down": '<path d="m6 9 6 6 6-6"/>',
    "chevron-right": '<path d="m9 18 6-6-6-6"/>',
    "circle-dashed": '<path d="M10.1 2.2a10 10 0 0 0-3.5 1.5"/><path d="M3.7 6.6a10 10 0 0 0-1.5 3.5"/><path d="M2.2 13.9a10 10 0 0 0 1.5 3.5"/><path d="M6.6 20.3a10 10 0 0 0 3.5 1.5"/><path d="M13.9 21.8a10 10 0 0 0 3.5-1.5"/><path d="M20.3 17.4a10 10 0 0 0 1.5-3.5"/><path d="M21.8 10.1a10 10 0 0 0-1.5-3.5"/><path d="M17.4 3.7a10 10 0 0 0-3.5-1.5"/>',
    "circle-slash": '<circle cx="12" cy="12" r="10"/><path d="M9 15 15 9"/>',
    "clock": '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
    "cloud": '<path d="M17.5 19H9a7 7 0 1 1 6.7-9h1.8a4.5 4.5 0 1 1 0 9Z"/>',
    "cloud-off": '<path d="m2 2 20 20"/><path d="M5.8 5.8A7 7 0 0 0 9 19h8.5a4.5 4.5 0 0 0 3.2-7.7"/><path d="M15.7 10A7 7 0 0 0 10 5.1"/>',
    "external-link": '<path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
    "flask-conical": '<path d="M10 2v7.5L4.2 19A1.5 1.5 0 0 0 5.5 21h13a1.5 1.5 0 0 0 1.3-2L14 9.5V2"/><path d="M8.5 2h7"/><path d="M6.8 15h10.4"/>',
    "hammer": '<path d="m15 12-8.4 8.4a2 2 0 0 1-2.8-2.8L12 9"/><path d="M17.6 6.4 14 10l3 3 3.6-3.6a2 2 0 0 0 0-2.8l-.2-.2a2 2 0 0 0-2.8 0Z"/><path d="M12.5 4.5 9 8"/>',
    "hard-drive": '<path d="M22 12H2"/><path d="M5.5 5h13a2 2 0 0 1 1.8 1.1l1.5 3A2 2 0 0 1 22 10v6a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2v-6a2 2 0 0 1 .2-.9l1.5-3A2 2 0 0 1 5.5 5Z"/><path d="M6 15h.01"/><path d="M10 15h.01"/>',
    "id-card": '<path d="M16 10h2"/><path d="M16 14h2"/><path d="M6.2 15a3 3 0 0 1 5.6 0"/><circle cx="9" cy="11" r="2"/><rect x="2" y="5" width="20" height="14" rx="2"/>',
    "info": '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
    "key-round": '<path d="M2.6 18.4a2 2 0 0 0 0 2.8 2 2 0 0 0 2.8 0l1.4-1.4v-2h2v-2h2l1.6-1.6a6 6 0 1 0-3.4-3.4Z"/><circle cx="16.5" cy="7.5" r="1"/>',
    "layers": '<path d="m12 2 9 5-9 5-9-5 9-5Z"/><path d="m3 12 9 5 9-5"/><path d="m3 17 9 5 9-5"/>',
    "link": '<path d="M9 15l6-6"/><path d="M11 6.5 12.6 5a4.6 4.6 0 0 1 6.5 6.5L17.5 13"/><path d="M13 17.5 11.4 19a4.6 4.6 0 0 1-6.5-6.5L6.5 11"/>',
    "loader": '<path d="M12 2v4"/><path d="m16.2 7.8 2.9-2.9"/><path d="M18 12h4"/><path d="m16.2 16.2 2.9 2.9"/><path d="M12 18v4"/><path d="m4.9 19.1 2.9-2.9"/><path d="M2 12h4"/><path d="m4.9 4.9 2.9 2.9"/>',
    "lock": '<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
    "log-in": '<path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/><path d="m10 17 5-5-5-5"/><path d="M15 12H3"/>',
    "more-horizontal": '<circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/><circle cx="5" cy="12" r="1"/>',
    "pencil": '<path d="M21.2 5.4a2 2 0 0 0 0-2.8l-.8-.8a2 2 0 0 0-2.8 0L3 16.4V21h4.6Z"/><path d="m15 5 4 4"/>',
    "play": '<path d="M6 3.5v17l14-8.5Z"/>',
    "plus": '<path d="M12 5v14"/><path d="M5 12h14"/>',
    "power": '<path d="M12 2v10"/><path d="M18.4 6.6a9 9 0 1 1-12.8 0"/>',
    "refresh-cw": '<path d="M21 12a9 9 0 0 0-9-9 9 9 0 0 0-6.4 2.6L3 8"/><path d="M3 3v5h5"/><path d="M3 12a9 9 0 0 0 9 9 9 9 0 0 0 6.4-2.6L21 16"/><path d="M21 21v-5h-5"/>',
    "repeat": '<path d="m17 2 4 4-4 4"/><path d="M3 11V9a4 4 0 0 1 4-4h14"/><path d="m7 22-4-4 4-4"/><path d="M21 13v2a4 4 0 0 1-4 4H3"/>',
    "search": '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
    "search-check": '<path d="m8 11 2 2 4-4"/><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
    "server": '<rect x="2" y="3" width="20" height="8" rx="2"/><rect x="2" y="13" width="20" height="8" rx="2"/><path d="M6 7h.01"/><path d="M6 17h.01"/>',
    "shield": '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z"/>',
    "shield-check": '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z"/><path d="m9 12 2 2 4-4"/>',
    "sliders-horizontal": '<path d="M21 4h-9"/><path d="M8 4H3"/><path d="M21 12h-5"/><path d="M12 12H3"/><path d="M21 20h-9"/><path d="M8 20H3"/><circle cx="10" cy="4" r="2"/><circle cx="14" cy="12" r="2"/><circle cx="10" cy="20" r="2"/>',
    "sparkles": '<path d="M12 3l1.9 4.6L18.5 9.5l-4.6 1.9L12 16l-1.9-4.6L5.5 9.5l4.6-1.9Z"/><path d="M19 15l.9 2.1L22 18l-2.1.9L19 21l-.9-2.1L16 18l2.1-.9Z"/>',
    "square": '<rect x="3" y="3" width="18" height="18" rx="2"/>',
    "sun-moon": '<path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.9 4.9 1.4 1.4"/><path d="m17.7 17.7 1.4 1.4"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.3 17.7-1.4 1.4"/><path d="m19.1 4.9-1.4 1.4"/><circle cx="12" cy="12" r="4"/>',
    "trash-2": '<path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M10 11v6"/><path d="M14 11v6"/>',
    "wrench": '<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.8-3.8a6 6 0 0 1-7.9 7.9l-6.9 6.9a2.1 2.1 0 0 1-3-3l6.9-6.9a6 6 0 0 1 7.9-7.9Z"/>',
    "x": '<path d="M18 6 6 18"/><path d="M6 6l12 12"/>'
  };

  function svg(name) {
    var inner = P[name];
    if (!inner) return null;
    return '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" ' +
      'fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" ' +
      'stroke-linejoin="round" class="lucide lucide-' + name + '" aria-hidden="true" focusable="false">' +
      inner + "</svg>";
  }

  /* Replace every un-rendered <i data-lucide="…"> in place, preserving its
     classes on the produced <svg> so existing sizing rules keep working. */
  function render(root) {
    var scope = root || document;
    var nodes = scope.querySelectorAll("i[data-lucide]");
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      var name = el.getAttribute("data-lucide");
      var markup = svg(name);
      if (!markup) {
        /* Unknown glyph: leave an inert, correctly-sized placeholder rather than
           an empty box that collapses the layout it sits in. */
        el.setAttribute("data-lucide-missing", name);
        el.removeAttribute("data-lucide");
        continue;
      }
      var tmp = document.createElement("div");
      tmp.innerHTML = markup;
      var s = tmp.firstChild;
      if (el.className) s.setAttribute("class", s.getAttribute("class") + " " + el.className);
      el.parentNode.replaceChild(s, el);
    }
  }

  /* Hosts that already loaded Lucide keep it; this becomes a no-op fallback for
     names Lucide also knows, and still fills in when Lucide is absent. */
  var prior = window.OVRenderIcons;
  window.OVRenderIcons = function (root) {
    if (window.lucide && typeof window.lucide.createIcons === "function") {
      try { window.lucide.createIcons({ attrs: { "stroke-width": 1.6 } }); } catch (e) {}
    }
    render(root);
    if (prior && prior !== window.OVRenderIcons) { try { prior(root); } catch (e) {} }
  };
  window.OVIconsLocal = { names: Object.keys(P), svg: svg, render: render };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { window.OVRenderIcons(); });
  } else window.OVRenderIcons();
})();
