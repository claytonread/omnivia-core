/* ============================================================================
   OmniVia Core Settings — small presentation helpers over the shared settings
   shell classes (.sw-*, .ov-*, .ai-sheet). No new visual language.
   ============================================================================ */
(function () {
  "use strict";
  function esc(s) { return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"); }
  function ico(n, c) { return '<i data-lucide="' + n + '"' + (c ? ' class="' + c + '"' : "") + "></i>"; }
  function icons() { if (window.OVRenderIcons) window.OVRenderIcons(); }
  function attrs(o) { var s = ""; for (var k in o) if (o[k] != null && o[k] !== false) s += " " + k + (o[k] === true ? "" : '="' + esc(o[k]) + '"'); return s; }

  function row(o) {
    return '<div class="sw-row' + (o.off ? " off" : "") + '"' + (o.id ? ' id="' + esc(o.id) + '"' : "") + '>' +
      '<div class="sw-row-l"><div class="sw-row-t"' + (o.tid ? ' id="' + esc(o.tid) + '"' : "") + '>' + o.t + "</div>" +
      (o.d ? '<div class="sw-row-d">' + o.d + "</div>" : "") + (o.err ? '<div class="cs-inline-err">' + ico("alert-circle") + "<span>" + esc(o.err) + "</span></div>" : "") + "</div>" +
      (o.c ? '<div class="sw-row-c">' + o.c + "</div>" : "") + "</div>" + (o.sub ? '<div class="sw-row-sub">' + o.sub + "</div>" : "");
  }
  function rows(inner) { return '<div class="sw-rows">' + inner + "</div>"; }
  function group(label, inner, after) { return '<section class="sw-group">' + (label ? '<h2 class="sw-group-h">' + esc(label) + "</h2>" : "") + inner + (after || "") + "</section>"; }
  function head(t, d, right) { return '<div class="sw-head" style="display:flex;justify-content:space-between;gap:16px;align-items:flex-start"><div><h1 style="margin-top:0">' + esc(t) + '</h1><div class="sub">' + esc(d) + "</div></div>" + (right ? '<div style="padding-top:2px;flex:none">' + right + "</div>" : "") + "</div>"; }

  function toggle(act, on, o) {
    o = o || {};
    return '<button class="ov-toggle' + (on ? " on" : "") + '" role="switch" aria-checked="' + (on ? "true" : "false") + '"' + attrs({ "data-act": act, "data-arg": o.arg, "aria-labelledby": o.by, "aria-label": o.label, disabled: !!o.disabled }) + '><span class="knob"></span></button>';
  }
  function btn(label, cls, o) { o = o || {}; return '<button class="ov-btn ' + (cls || "ov-btn--bordered") + '"' + attrs({ "data-act": o.act, "data-arg": o.arg, disabled: !!o.disabled, "aria-label": o.label, id: o.id }) + ">" + (o.icon ? ico(o.icon) : "") + esc(label) + "</button>"; }
  function seg(act, opts, cur, o) {
    o = o || {};
    return '<div class="sw-seg" role="radiogroup"' + attrs({ "aria-labelledby": o.by }) + ">" + opts.map(function (p) {
      return '<button role="radio" aria-checked="' + (p[0] === cur ? "true" : "false") + '"' + (p[0] === cur ? ' class="sel"' : "") + attrs({ "data-act": act, "data-arg": p[0], disabled: !!o.disabled }) + ">" + esc(p[1]) + "</button>";
    }).join("") + "</div>";
  }
  function select(act, opts, cur, o) {
    o = o || {};
    return '<select class="cs-select"' + attrs({ "data-act": act, "data-arg": o.arg, "aria-label": o.label, disabled: !!o.disabled }) + ">" + opts.map(function (p) {
      return '<option value="' + esc(p[0]) + '"' + (p[0] === cur ? " selected" : "") + ">" + esc(p[1]) + "</option>";
    }).join("") + "</select>";
  }
  function pending(t) { return '<span class="cs-pending"><span class="cs-sp" aria-hidden="true"></span>' + esc(t) + "</span>"; }
  function pill(t, kind) { return '<span class="ov-pill' + (kind ? " ov-pill--" + kind : "") + '">' + esc(t) + "</span>"; }
  function health(t, dot) { return '<span class="ai-health"><span class="ai-dot ' + (dot || "") + '"></span>' + esc(t) + "</span>"; }
  function note(t, icon, attn) { return '<div class="cs-note' + (attn ? " attn" : "") + '">' + ico(icon || "info") + "<span>" + t + "</span></div>"; }
  function scope(t) { return '<span class="cs-scope">' + ico("hard-drive") + esc(t) + "</span>"; }
  function path(p) { return '<span class="cs-path" title="' + esc(p) + '"><span>' + esc(p) + "</span></span>"; }
  function dl(pairs) { return '<dl class="ai-dl">' + pairs.map(function (p) { return '<div class="ai-dl-r"><dt>' + esc(p[0]) + "</dt><dd>" + p[1] + "</dd></div>"; }).join("") + "</dl>"; }

  /* Disclosures remember their open state across re-renders. */
  var open = {};
  function disc(id, label, body) {
    return '<details class="cs-disc" data-disc="' + esc(id) + '"' + (open[id] ? " open" : "") + "><summary>" + ico("chevron-right") + esc(label) + '</summary><div class="cs-disc-b">' + body + "</div></details>";
  }
  document.addEventListener("toggle", function (e) { var d = e.target; if (d && d.matches && d.matches("details[data-disc]")) open[d.getAttribute("data-disc")] = d.open; }, true);

  /* ---- sheet: one compact modal anchored to this window ------------------- */
  var sheetEl = null, sheetFocus = null;
  function sheet(o) {
    closeSheet(true);
    sheetFocus = document.activeElement;
    var el = document.createElement("div");
    el.className = "ai-scrim"; el.id = "cs-sheet";
    el.innerHTML = '<div class="ai-sheet" role="dialog" aria-modal="true" aria-labelledby="cs-sheet-t"' + (o.wide ? ' style="width:min(560px,94%)"' : "") + '>' +
      '<div class="ai-sheet-h"><div class="tt"><h2 class="ai-sheet-t" id="cs-sheet-t">' + esc(o.title) + "</h2>" + (o.sub ? '<div class="ai-sheet-s">' + o.sub + "</div>" : "") + "</div>" +
      '<button class="ai-x" data-sheet-close aria-label="Close">' + ico("x") + "</button></div>" +
      '<div class="ai-sheet-b" id="cs-sheet-b">' + o.body + "</div>" +
      (o.foot ? '<div class="ai-sheet-f" id="cs-sheet-f">' + o.foot + "</div>" : "") + "</div>";
    document.body.appendChild(el);
    sheetEl = el; icons();
    el.addEventListener("click", function (e) { if (e.target === el || e.target.closest("[data-sheet-close]")) { if (o.onCancel) o.onCancel(); closeSheet(); } });
    var first = el.querySelector("[autofocus]") || el.querySelector(".ai-sheet-b input,.ai-sheet-b select,.ai-sheet-b button") || el.querySelector(".ai-sheet-f .ov-btn--primary") || el.querySelector("[data-sheet-close]");
    if (first) setTimeout(function () { first.focus(); }, 0);
    el._onCancel = o.onCancel;
    return el;
  }
  function sheetUpdate(body, foot) {
    if (!sheetEl) return;
    var b = sheetEl.querySelector("#cs-sheet-b"), f = sheetEl.querySelector("#cs-sheet-f");
    if (body != null && b) b.innerHTML = body;
    if (foot != null && f) f.innerHTML = foot;
    icons();
  }
  function closeSheet(silent) {
    if (!sheetEl) return;
    sheetEl.remove(); sheetEl = null;
    if (!silent && sheetFocus && sheetFocus.focus && document.contains(sheetFocus)) sheetFocus.focus();
  }
  document.addEventListener("keydown", function (e) {
    if (!sheetEl) return;
    if (e.key === "Escape") { e.stopPropagation(); if (sheetEl._onCancel) sheetEl._onCancel(); closeSheet(); return; }
    if (e.key === "Tab") {
      var f = [].slice.call(sheetEl.querySelectorAll('button:not([disabled]),input:not([disabled]),select:not([disabled]),[tabindex]:not([tabindex="-1"])'));
      if (!f.length) return;
      if (e.shiftKey && document.activeElement === f[0]) { e.preventDefault(); f[f.length - 1].focus(); }
      else if (!e.shiftKey && document.activeElement === f[f.length - 1]) { e.preventDefault(); f[0].focus(); }
    }
  }, true);

  function toast(msg, kind, icon) {
    var host = document.getElementById("sw-toasts"); if (!host) return;
    var t = document.createElement("div");
    t.className = "sw-toast" + (kind ? " " + kind : ""); t.setAttribute("role", "status");
    t.innerHTML = (icon ? ico(icon) : "") + "<span></span>"; t.querySelector("span").textContent = msg;
    host.appendChild(t); icons();
    setTimeout(function () { t.classList.add("out"); setTimeout(function () { t.remove(); }, 200); }, 2800);
  }
  function live(t) { var l = document.getElementById("sw-live"); if (l) l.textContent = t; }

  window.CoreUI = { esc: esc, ico: ico, icons: icons, row: row, rows: rows, group: group, head: head, toggle: toggle, btn: btn, seg: seg, select: select, pending: pending, pill: pill, health: health, note: note, scope: scope, path: path, dl: dl, disc: disc, sheet: sheet, sheetUpdate: sheetUpdate, closeSheet: closeSheet, toast: toast, live: live, isSheetOpen: function () { return !!sheetEl; } };
})();
