// ============================================================================
// OmniVia icon set — the design's own glyphs (lib/ov-icons-local.js).
//
// Why not SF Symbols. The shim's header says "design intent remains SF Symbols
// in the native build; map by name", and that was the first approach here. It
// does not hold up for a source list: SF Symbols at one point size have very
// different optical boxes, and measured in the window the seven sidebar glyphs
// ranged 10.0–17.0pt tall and 12.0–16.5pt wide. Constraining them to a 16×16
// box made it worse, because `scaleProportionallyDown` then rescaled each one
// by a different factor.
//
// Two of them have no SF equivalent at all: Lucide's `layers` and
// `search-check` (there is no `checkmark.magnifyingglass`), so those were
// already approximations.
//
// These paths are Lucide's, copied from the design's own offline set: one
// 24×24 viewBox, stroke 1.6, round caps and joins. Uniform by construction,
// and the same glyphs the design preview draws.
// ============================================================================

import AppKit

enum OvIcon: String {
    // Source list
    case slidersHorizontal = "sliders-horizontal"
    case hardDrive = "hard-drive"
    case layers
    case searchCheck = "search-check"
    case refreshCw = "refresh-cw"
    case shield
    case wrench

    // Status (core-mac.js `KIND`)
    case checkCircle2 = "check-circle-2"
    case circleDashed = "circle-dashed"
    case alertCircle = "alert-circle"
    case alertTriangle = "alert-triangle"
    case clock
    case circleSlash = "circle-slash"
    case xCircle = "x-circle"
    case ban
    /// Lucide's `minus-circle`, in the set's own conventions. Not in the
    /// design's offline shim because the web prototype never needs it — it has
    /// no "absent from this build" state to draw.
    case minusCircle = "minus-circle"

    // Notes and chrome
    case info
    case shieldCheck = "shield-check"
    case chevronRight = "chevron-right"
    case chevronDown = "chevron-down"
    case plus
    case x
    case check

    // Core Settings v2 (six-pane redesign)
    case folderOpen = "folder-open"
    case cpu
    case chevronLeft = "chevron-left"
    case search
    case code
    case sparkles
    case calendarClock = "calendar-clock"

    /// The inner markup of the 24×24 viewBox, verbatim from the design.
    private var path: String {
        switch self {
        case .slidersHorizontal:
            return """
            <path d="M21 4h-9"/><path d="M8 4H3"/><path d="M21 12h-5"/><path d="M12 12H3"/>\
            <path d="M21 20h-9"/><path d="M8 20H3"/><circle cx="10" cy="4" r="2"/>\
            <circle cx="14" cy="12" r="2"/><circle cx="10" cy="20" r="2"/>
            """
        case .hardDrive:
            return """
            <path d="M22 12H2"/><path d="M5.5 5h13a2 2 0 0 1 1.8 1.1l1.5 3A2 2 0 0 1 22 10v6a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2v-6a2 2 0 0 1 .2-.9l1.5-3A2 2 0 0 1 5.5 5Z"/>\
            <path d="M6 15h.01"/><path d="M10 15h.01"/>
            """
        case .layers:
            return #"<path d="m12 2 9 5-9 5-9-5 9-5Z"/><path d="m3 12 9 5 9-5"/><path d="m3 17 9 5 9-5"/>"#
        case .searchCheck:
            return #"<path d="m8 11 2 2 4-4"/><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>"#
        case .refreshCw:
            return """
            <path d="M21 12a9 9 0 0 0-9-9 9 9 0 0 0-6.4 2.6L3 8"/><path d="M3 3v5h5"/>\
            <path d="M3 12a9 9 0 0 0 9 9 9 9 0 0 0 6.4-2.6L21 16"/><path d="M21 21v-5h-5"/>
            """
        case .shield:
            return #"<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z"/>"#
        case .wrench:
            return #"<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.8-3.8a6 6 0 0 1-7.9 7.9l-6.9 6.9a2.1 2.1 0 0 1-3-3l6.9-6.9a6 6 0 0 1 7.9-7.9Z"/>"#
        case .checkCircle2:
            return #"<circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/>"#
        case .circleDashed:
            return """
            <path d="M10.1 2.2a10 10 0 0 0-3.5 1.5"/><path d="M3.7 6.6a10 10 0 0 0-1.5 3.5"/>\
            <path d="M2.2 13.9a10 10 0 0 0 1.5 3.5"/><path d="M6.6 20.3a10 10 0 0 0 3.5 1.5"/>\
            <path d="M13.9 21.8a10 10 0 0 0 3.5-1.5"/><path d="M20.3 17.4a10 10 0 0 0 1.5-3.5"/>\
            <path d="M21.8 10.1a10 10 0 0 0-1.5-3.5"/><path d="M17.4 3.7a10 10 0 0 0-3.5-1.5"/>
            """
        case .alertCircle:
            return #"<circle cx="12" cy="12" r="10"/><path d="M12 8v4"/><path d="M12 16h.01"/>"#
        case .alertTriangle:
            return """
            <path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/>\
            <path d="M12 9v4"/><path d="M12 17h.01"/>
            """
        case .clock:
            return #"<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>"#
        case .circleSlash:
            return #"<circle cx="12" cy="12" r="10"/><path d="M9 15 15 9"/>"#
        case .xCircle:
            return #"<circle cx="12" cy="12" r="10"/><path d="m15 9-6 6"/><path d="m9 9 6 6"/>"#
        case .ban:
            return #"<circle cx="12" cy="12" r="10"/><path d="m4.9 4.9 14.2 14.2"/>"#
        case .minusCircle:
            return #"<circle cx="12" cy="12" r="10"/><path d="M8 12h8"/>"#
        case .info:
            return #"<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>"#
        case .shieldCheck:
            return #"<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z"/><path d="m9 12 2 2 4-4"/>"#
        case .chevronRight:
            return #"<path d="m9 18 6-6-6-6"/>"#
        case .chevronDown:
            return #"<path d="m6 9 6 6 6-6"/>"#
        case .plus:
            return #"<path d="M12 5v14"/><path d="M5 12h14"/>"#
        case .x:
            return #"<path d="M18 6 6 18"/><path d="M6 6l12 12"/>"#
        case .check:
            return #"<path d="M20 6 9 17l-5-5"/>"#
        case .folderOpen:
            return #"<path d="m6 14 1.5-2.9A2 2 0 0 1 9.24 10H20a2 2 0 0 1 1.94 2.5l-1.54 6a2 2 0 0 1-1.95 1.5H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h3.9a2 2 0 0 1 1.69.9l.81 1.2a2 2 0 0 0 1.67.9H18a2 2 0 0 1 2 2v2"/>"#
        case .cpu:
            return #"<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M15 2v2"/><path d="M15 20v2"/><path d="M2 15h2"/><path d="M2 9h2"/><path d="M20 15h2"/><path d="M20 9h2"/><path d="M9 2v2"/><path d="M9 20v2"/>"#
        case .chevronLeft:
            return #"<path d="m15 18-6-6 6-6"/>"#
        case .search:
            return #"<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>"#
        case .code:
            return #"<path d="m16 18 6-6-6-6"/><path d="m8 6-6 6 6 6"/>"#
        case .sparkles:
            return #"<path d="M12 3l1.9 4.6L18.5 9.5l-4.6 1.9L12 16l-1.9-4.6L5.5 9.5l4.6-1.9Z"/><path d="M19 15l.9 2.1L22 18l-2.1.9L19 21l-.9-2.1L16 18l2.1-.9Z"/>"#
        case .calendarClock:
            return #"<path d="M21 7.5V6a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h3.5"/><path d="M16 2v4"/><path d="M8 2v4"/><path d="M3 10h5"/><path d="M17.5 17.5 16 16.3V14"/><circle cx="16" cy="16" r="6"/>"#
        }
    }

    /// A template image at `size`, tintable through `contentTintColor`.
    ///
    /// Drawn black rather than `currentColor`: NSImage has no CSS cascade to
    /// resolve that against, and a template image keys off alpha, so the
    /// stroke colour only has to be opaque.
    func image(size: CGFloat) -> NSImage? {
        let svg = """
        <svg xmlns="http://www.w3.org/2000/svg" width="\(Int(size))" height="\(Int(size))" \
        viewBox="0 0 24 24" fill="none" stroke="black" stroke-width="1.6" \
        stroke-linecap="round" stroke-linejoin="round">\(path)</svg>
        """
        guard let image = NSImage(data: Data(svg.utf8)) else { return nil }
        image.size = NSSize(width: size, height: size)
        image.isTemplate = true
        return image
    }
}
