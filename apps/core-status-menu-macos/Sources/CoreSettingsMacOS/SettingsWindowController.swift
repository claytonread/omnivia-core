// ============================================================================
// OmniVia Core Settings — the seven-pane settings window (MR-001, MR-002, §15).
//
// One owned window, opened/focused from the companion menu. Closing it never
// stops Core (the menu keeps its own lifecycle authority). Layout, row
// grammar, tokens and copy follow the Core Settings prototype (I01) and
// amendment v1.1 (I05): the OmniVia dark token layer (bg-content #1E1E20,
// cards on bg-content with a hairline, recovery blocks #232326, brick accent
// #D2756C, semantic success/
// warning/danger), the branded 60px header with the concentric-circle mark,
// the 200px source list with pill selection, group cards with 10px radius,
// status chips (icon + word, never colour alone) and OmniVia buttons.
//
// Passive refresh runs on open and Refresh status — never prompting, probing,
// registering or connecting (§8.4, MT-002).
// ============================================================================

import AppKit
import Combine
import ServiceManagement
import UserNotifications

// MARK: - OmniVia dark tokens (styles/tokens.css, dark layer)

enum Ov {
    /// A token that resolves per appearance, so the whole window follows the
    /// system the way `[data-theme]` and `prefers-color-scheme` do in the
    /// design. Light values come from the `:root` blocks, dark from the
    /// `[data-theme="dark"]` blocks of the same files.
    private static func dynamic(_ light: NSColor, _ dark: NSColor) -> NSColor {
        NSColor(name: nil) { appearance in
            appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua ? dark : light
        }
    }

    private static func srgb(_ hex: UInt32, _ alpha: CGFloat = 1) -> NSColor {
        NSColor(
            srgbRed: CGFloat((hex >> 16) & 0xFF) / 255.0,
            green: CGFloat((hex >> 8) & 0xFF) / 255.0,
            blue: CGFloat(hex & 0xFF) / 255.0,
            alpha: alpha
        )
    }

    private static func black(_ a: CGFloat) -> NSColor { NSColor.black.withAlphaComponent(a) }
    private static func white(_ a: CGFloat) -> NSColor { NSColor.white.withAlphaComponent(a) }

    // MARK: Surfaces and text (styles/brick-tokens.css)

    static let bgContent = dynamic(srgb(0xFFFFFF), srgb(0x1E1E20))
    static let bgContentSecondary = dynamic(srgb(0xFBFBFA), srgb(0x232326))
    static let bgElevated = dynamic(srgb(0xFFFFFF), srgb(0x26262E))
    static let bgInsetDeep = dynamic(srgb(0xEEEEF0), srgb(0x141417))
    static let textPrimary = dynamic(black(0.85), white(0.92))
    static let textSecondary = dynamic(black(0.55), white(0.58))
    static let textTertiary = dynamic(black(0.38), white(0.40))
    static let textQuaternary = dynamic(black(0.24), white(0.26))
    static let separator = dynamic(black(0.09), white(0.10))
    static let borderSubtle = dynamic(black(0.07), white(0.08))
    static let borderControl = dynamic(black(0.16), white(0.18))

    // MARK: Accent and semantics (styles/tokens.css)

    /// Brick 500 in light; lifted for contrast on dark.
    static let accent = dynamic(srgb(0x9E302A), srgb(0xD2756C))
    static let accentActive = dynamic(srgb(0x631A17), srgb(0xE6968D))
    static let success = dynamic(srgb(0x2E9E6B), srgb(0x3DBE83))
    static let warning = dynamic(srgb(0xC8821E), srgb(0xE0A33E))
    static let onAccent = NSColor.white

    // MARK: States (lib/ov-ui/tokens/ov-ui-palette.css)

    /// The hover and active surfaces are a warm grey, not plain black/white —
    /// either reads cold against the brick-tinted chrome.
    static let hoverBg = dynamic(
        NSColor(srgbRed: 120 / 255.0, green: 114 / 255.0, blue: 120 / 255.0, alpha: 0.09),
        NSColor(srgbRed: 185 / 255.0, green: 178 / 255.0, blue: 188 / 255.0, alpha: 0.09)
    )
    static let activeBg = dynamic(
        NSColor(srgbRed: 120 / 255.0, green: 114 / 255.0, blue: 120 / 255.0, alpha: 0.17),
        NSColor(srgbRed: 185 / 255.0, green: 178 / 255.0, blue: 188 / 255.0, alpha: 0.18)
    )
    /// `--ov-accent-muted`.
    static let accentMuted = dynamic(
        NSColor(srgbRed: 158 / 255.0, green: 48 / 255.0, blue: 42 / 255.0, alpha: 0.12),
        NSColor(srgbRed: 210 / 255.0, green: 117 / 255.0, blue: 108 / 255.0, alpha: 0.28)
    )
    /// `--ov-focus-ring`.
    static let focusRing = dynamic(
        NSColor(srgbRed: 158 / 255.0, green: 48 / 255.0, blue: 42 / 255.0, alpha: 0.50),
        NSColor(srgbRed: 210 / 255.0, green: 117 / 255.0, blue: 108 / 255.0, alpha: 0.60)
    )

    /// The selected source-list row. Light fills with the solid accent
    /// (`--ov-selection-bg`) and uses white for both label and glyph; dark
    /// overrides that to the accent at 28% with an accent-coloured glyph
    /// (`[data-theme="dark"] #sw-nav .nav-row[aria-current="page"]`). A solid
    /// brick block is too heavy on a dark 28pt row, and a 28% wash is too
    /// faint on a light one — so this is a real fork, not a tint.
    static var selectionIsSolid: Bool { !isDark }
    static let selectionBg = dynamic(srgb(0x9E302A), accentMuted)
    static let selectionText = dynamic(.white, .white)

    static var isDark: Bool {
        NSApp.effectiveAppearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua
    }

    // MARK: Materials and sheens (ov-ui-palette.css, os/overlay.css)
    //
    // Chrome is a translucent material with a sheen over it:
    // `background: var(--ov-…-sheen), var(--ov-material-…)`.
    //
    // `backdrop-filter` in the prototype blurs what is behind the element
    // *inside the page*, and the page's window is opaque
    // (`#cs-win{background:var(--ov-bg-content)}`). Blurring a flat colour
    // returns that colour, so the chrome composites to a fixed value and the
    // translucency never reaches the desktop. An `NSVisualEffectView` samples
    // the wallpaper instead, which leaves the bar and the source list unable
    // to match each other — so these are composited against the window's own
    // background, which is both faithful and deterministic.

    static let materialTitlebar = dynamic(
        srgb(0xF6F5F7, 0.70), srgb(0x1E1E24, 0.64)
    )
    static let materialSidebar = dynamic(
        srgb(0xF4F4F7, 0.46), srgb(0x16161B, 0.40)
    )

    static let chromeTitlebar = dynamic(
        srgb(0xF6F5F7, 0.70).composited(over: srgb(0xFFFFFF)),
        srgb(0x1E1E24, 0.64).composited(over: srgb(0x1E1E20))
    )
    static let chromeSidebar = dynamic(
        srgb(0xF4F4F7, 0.46).composited(over: srgb(0xFFFFFF)),
        srgb(0x16161B, 0.40).composited(over: srgb(0x1E1E20))
    )

    private static func brick(_ alpha: CGFloat) -> NSColor { srgb(0x9E302A, alpha) }
    private static func brickDark(_ alpha: CGFloat) -> NSColor { srgb(0xD2756C, alpha) }

    /// `--ov-titlebar-sheen`: a horizontal accent wash, gone by 52%.
    static var titlebarSheen: [(NSColor, CGFloat)] {
        isDark
            ? [(brickDark(0.13), 0.0), (brickDark(0.06), 0.24), (brickDark(0), 0.52), (brickDark(0), 1.0)]
            : [(brick(0.09), 0.0), (brick(0.045), 0.24), (brick(0), 0.52), (brick(0), 1.0)]
    }

    /// `--ov-sidebar-sheen`: accent at the head, gone by ~27%, with a warmer
    /// earth tone returning at the foot.
    static var sidebarSheen: [(NSColor, CGFloat)] {
        isDark
            ? [(brickDark(0.10), 0.0), (brickDark(0), 0.28), (srgb(0x966046, 0.07), 1.0)]
            : [(brick(0.07), 0.0), (brick(0), 0.26), (srgb(0xC4A896, 0.05), 1.0)]
    }

    /// `--ov-specular-highlight`: the lit top edge of a glass surface.
    static let specularHighlight = dynamic(white(0.55), white(0.10))
}

extension NSView {
    /// A dynamic colour as a CGColor for *this* view's appearance.
    ///
    /// `NSColor.cgColor` resolves against whatever drawing appearance happens
    /// to be current, which outside a draw cycle is the app's, not the
    /// window's. A layer colour taken that way keeps the wrong theme.
    func resolved(_ color: NSColor) -> CGColor {
        var out = color.cgColor
        effectiveAppearance.performAsCurrentDrawingAppearance { out = color.cgColor }
        return out
    }
}

extension NSColor {
    /// This colour composited over an opaque one, in sRGB.
    func composited(over background: NSColor) -> NSColor {
        guard let top = usingColorSpace(.sRGB), let under = background.usingColorSpace(.sRGB)
        else { return self }
        let a = top.alphaComponent
        return NSColor(
            srgbRed: top.redComponent * a + under.redComponent * (1 - a),
            green: top.greenComponent * a + under.greenComponent * (1 - a),
            blue: top.blueComponent * a + under.blueComponent * (1 - a),
            alpha: 1
        )
    }
}

/// Window chrome: a flat material with a sheen gradient over it, which is what
/// `background: var(--ov-…-sheen), var(--ov-material-…)` means — two layers,
/// the tint above the surface.
///
/// Deliberately not an `NSVisualEffectView`. See `Ov.chromeTitlebar`: the
/// design's translucency is over the window's own opaque background, so it
/// resolves to a fixed colour, whereas vibrancy would sample the desktop and
/// leave the bar and the source list unable to match each other.
class SheenView: NSView {
    private let sheen = CAGradientLayer()

    /// - Parameters:
    ///   - surface: the composited material colour.
    ///   - stops: colour and location pairs, in CSS gradient order.
    ///   - horizontal: true for a `90deg` (left-to-right) gradient, false for
    ///     `180deg` (top-to-bottom).
    ///   - specular: draw the 1px lit top edge.
    init(
        surface: NSColor,
        stops: [(NSColor, CGFloat)],
        horizontal: Bool,
        specular: Bool = false
    ) {
        super.init(frame: .zero)
        wantsLayer = true
        layer?.backgroundColor = surface.cgColor

        sheen.colors = stops.map { $0.0.cgColor }
        sheen.locations = stops.map { NSNumber(value: Double($0.1)) }
        // Layer coordinates run bottom-up, so a CSS 180deg (top-to-bottom)
        // gradient starts at y = 1.
        sheen.startPoint = horizontal ? CGPoint(x: 0, y: 0.5) : CGPoint(x: 0.5, y: 1)
        sheen.endPoint = horizontal ? CGPoint(x: 1, y: 0.5) : CGPoint(x: 0.5, y: 0)
        layer?.addSublayer(sheen)

        if specular {
            let line = CALayer()
            line.backgroundColor = Ov.specularHighlight.cgColor
            line.name = "specular"
            layer?.addSublayer(line)
        }
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("init(coder:) is not used") }

    override func layout() {
        super.layout()
        // The sheen is a plain sublayer, so it does not follow the view's
        // bounds on its own; implicit animation would also make it lag a
        // live resize behind the material it tints.
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        sheen.frame = bounds
        layer?.sublayers?
            .first { $0.name == "specular" }?
            .frame = CGRect(x: 0, y: bounds.height - 1, width: bounds.width, height: 1)
        CATransaction.commit()
    }
}

// MARK: - OmniVia typography (styles/tokens.css §1)

enum OvFont {
    /// CSS weight → NSFont.Weight (CoreText scale). The prototype's canonical
    /// weights are 400 / 500 / 590 / 700; 590 sits between medium (0.23) and
    /// semibold (0.30).
    private static func weight(_ css: CGFloat) -> NSFont.Weight {
        switch css {
        case ..<450: return .regular        // 400
        case ..<550: return .medium         // 500
        case ..<595: return NSFont.Weight(rawValue: 0.28) // 590
        case ..<660: return .semibold       // 600
        default: return .bold               // 700
        }
    }

    /// SF Pro (--ov-font-sans). NSFont.systemFont resolves the optical
    /// Text/Display cut automatically at the ~20px breakpoint.
    static func sans(_ size: CGFloat, _ cssWeight: CGFloat = 400) -> NSFont {
        .systemFont(ofSize: size, weight: weight(cssWeight))
    }

    /// SF Mono (--ov-font-mono) with tabular-nums, for paths, IDs, versions,
    /// counts, logs and code.
    static func mono(_ size: CGFloat, _ cssWeight: CGFloat = 400) -> NSFont {
        let base = NSFont.monospacedSystemFont(ofSize: size, weight: weight(cssWeight))
        let settings: [[NSFontDescriptor.FeatureKey: Any]] = [[
            .typeIdentifier: kNumberSpacingType,
            .selectorIdentifier: kMonospacedNumbersSelector,
        ]]
        let descriptor = base.fontDescriptor.addingAttributes([.featureSettings: settings])
        return NSFont(descriptor: descriptor, size: size) ?? base
    }

    /// A non-wrapping label with the token font, colour and letter-spacing
    /// (CSS `letter-spacing` em values → AppKit kern in points).
    static func label(
        _ text: String,
        size: CGFloat,
        cssWeight: CGFloat = 400,
        color: NSColor,
        tracking: CGFloat = 0,
        mono: Bool = false
    ) -> NSTextField {
        let font = mono ? self.mono(size, cssWeight) : sans(size, cssWeight)
        let attributed = NSAttributedString(string: text, attributes: [
            .font: font,
            .foregroundColor: color,
            .kern: tracking * size,
        ])
        return NSTextField(labelWithAttributedString: attributed)
    }

    // Prototype roles.
    static func paneTitle(_ text: String) -> NSTextField {
        label(text, size: 20, cssWeight: 700, color: Ov.textPrimary, tracking: -0.02)
    }
    static func groupHeader(_ text: String) -> NSTextField {
        label(text.uppercased(), size: 11, cssWeight: 700, color: Ov.textTertiary, tracking: 0.05)
    }
    static func wordmark(_ text: String) -> NSTextField {
        label(text, size: 13, cssWeight: 590, color: Ov.textPrimary, tracking: -0.01)
    }
}

@MainActor
public final class SettingsWindowController: NSObject, NSWindowDelegate {
    /// One window per companion (MR-001). Reopening focuses the existing one.
    private var window: NSWindow?

    private let coordinator: ReadinessCoordinator
    private let navigator: SettingsNavigator
    private let notificationCenter: UNUserNotificationCenter?
    private var cancellables: Set<AnyCancellable> = []
    private var appearanceObserver: NSKeyValueObservation?

    // Pane selection
    /// Order and labels follow `NAV` in the design's `core-settings.js`. That
    /// list is seven items: the readiness and Local Decisions amendments both
    /// say "exactly five", and the design has since added Capture and Document
    /// processing. The design file is the later of the two, so it wins.
    private enum Pane: String, CaseIterable {
        case general = "General"
        case data = "Data"
        case capture = "Capture"
        case documentProcessing = "Document processing"
        case processing = "Processing"
        case access = "Access"
        case maintenance = "Maintenance"

        /// The sentence under the pane title (design `U.head`'s second
        /// argument), verbatim.
        var lede: String {
            switch self {
            case .general: return "Choose how Core behaves on this Mac."
            case .data: return "See where your workspace is stored and keep it backed up."
            case .capture: return "How files and folders are added to Core from this Mac."
            case .documentProcessing:
                return "How Core reads text from captured documents and images on this Mac."
            case .processing: return "Control how Core processes the information you have added."
            case .access: return "Choose which applications can use this workspace."
            case .maintenance: return "Check Core health and resolve problems."
            }
        }

        /// SF Symbol nearest the design's Lucide icon.
        var symbol: String {
            switch self {
            case .general: return "slider.horizontal.3"          // sliders-horizontal
            case .data: return "internaldrive"                   // hard-drive
            // `square.3.layers.3d*` is macOS 14, and filled among outlines;
            // `square.stack.3d.up` is the nearest outline stack on 13.
            case .capture: return "square.stack.3d.up"            // layers
            // No SF equivalent of search-check exists, and a page with a
            // magnifier says "document processing" better than text alone.
            case .documentProcessing: return "doc.text.magnifyingglass" // search-check
            case .processing: return "arrow.triangle.2.circlepath" // refresh-cw
            case .access: return "shield"                        // shield
            case .maintenance: return "wrench.adjustable"        // wrench
            }
        }
    }

    /// Which disclosures the user has opened. View state, never service state,
    /// and deliberately not persisted: it is scoped to one window session the
    /// way the prototype's in-memory `open` map is.
    private var openDisclosures: Set<String> = []

    private var selectedPane: Pane = .general {
        didSet {
            guard oldValue != selectedPane else { return }
            render()
            renderSidebar()
            // A new pane starts at its own top. Carrying the previous pane's
            // offset over opens the next one already scrolled past its title.
            scrollToTop()
        }
    }

    private var sidebarStack: NSStackView?
    private var contentStack: NSStackView?

    /// Core preferences mirrored from the companion's own preference owner.
    /// These are Core facts, persisted by their existing owner (§14.1) — the
    /// readiness layer only reads them.
    public var startCoreAtLoginSelected = false
    public var attentionNotificationsSelected = false
    public var hasWorkspace = false

    public init(
        coordinator: ReadinessCoordinator,
        navigator: SettingsNavigator = SettingsNavigator(componentName: "OmniVia Core"),
        notificationCenter: UNUserNotificationCenter? = nil
    ) {
        self.coordinator = coordinator
        self.navigator = navigator
        // Resolve lazily and bundle-guarded: UNUserNotificationCenter.current()
        // raises when the process has no bundle identity (§13.2 honest states).
        if let notificationCenter {
            self.notificationCenter = notificationCenter
        } else if Bundle.main.bundleIdentifier != nil {
            self.notificationCenter = UNUserNotificationCenter.current()
        } else {
            self.notificationCenter = nil
        }
        super.init()
        // Pick up the stored preference rather than starting from false every
        // launch. The companion's own preferences stay machine-local: they
        // must work while Core is stopped, and they should not travel with a
        // portable workspace to another Mac.
        attentionNotificationsSelected = UserDefaults.standard.bool(
            forKey: Self.attentionNotificationsKey
        )
        coordinator.$snapshot.sink { [weak self] _ in self?.render() }.store(in: &cancellables)
        coordinator.$refreshInFlight.sink { [weak self] _ in self?.render() }.store(in: &cancellables)
    }

    // MARK: - Window lifecycle (MR-001)

    public func openOrFocus() {
        if let window {
            window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            coordinator.scheduleRefresh(.foregroundReturn)
            return
        }
        let window = buildWindow()
        self.window = window
        window.delegate = self
        window.center()
        observeSystemAppearance()
        positionWindowControls()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        coordinator.scheduleRefresh(.windowOpened)
    }

    // MARK: Window controls

    private var titlebarAccessoryInstalled = false

    /// Centre the traffic lights in the 60pt branded header.
    ///
    /// A titlebar accessory of that height is the supported way to make the
    /// titlebar tall, but AppKit still top-anchors the buttons inside it, so
    /// the lights sit level with the top edge rather than with the OmniVia
    /// mark. Two things make the correction stick:
    ///
    /// * the container is grown *and* re-anchored to the window's top edge —
    ///   growing it with `setFrameSize` alone keeps the old origin and pushes
    ///   it off the top, which clips the lights;
    /// * the target centre is converted from window coordinates into the bar's
    ///   own space, because the bar's orientation would otherwise flip the
    ///   offset and move the lights the wrong way.
    ///
    /// Titlebar layout re-anchors the buttons, so this is reasserted on every
    /// window update rather than run once at build time.
    private func positionWindowControls() {
        guard let window,
              let close = window.standardWindowButton(.closeButton),
              let bar = close.superview else { return }

        if !titlebarAccessoryInstalled {
            let accessory = NSTitlebarAccessoryViewController()
            accessory.view = NSView(frame: NSRect(x: 0, y: 0, width: 1, height: 60))
            accessory.layoutAttribute = .right
            window.addTitlebarAccessoryViewController(accessory)
            titlebarAccessoryInstalled = true
        }

        if let container = bar.superview {
            let target = NSRect(
                x: 0, y: window.frame.height - 60, width: window.frame.width, height: 60
            )
            if abs(container.frame.minY - target.minY) > 0.5
                || abs(container.frame.height - 60) > 0.5 {
                container.setFrameSize(NSSize(width: target.width, height: 60))
                container.setFrameOrigin(NSPoint(x: 0, y: target.minY))
            }
        }
        if abs(bar.frame.height - 60) > 0.5 {
            bar.setFrameSize(NSSize(width: bar.frame.width, height: 60))
        }

        let windowTop = window.frame.height
        for button in [
            close,
            window.standardWindowButton(.miniaturizeButton),
            window.standardWindowButton(.zoomButton),
        ].compactMap({ $0 }) {
            let centreInWindow = NSPoint(x: button.frame.midX, y: windowTop - 30)
            let centreInBar = bar.convert(centreInWindow, from: nil)
            var frame = button.frame
            frame.origin.y = centreInBar.y - frame.height / 2
            button.setFrameOrigin(frame.origin)
        }
    }

    public func windowDidResize(_ notification: Notification) {
        positionWindowControls()
    }

    public func windowDidUpdate(_ notification: Notification) {
        positionWindowControls()
    }

    /// While the preference is `system`, the Mac switching theme has to carry
    /// through to the window — which means rebuilding, because every layer
    /// colour was resolved when it was set.
    private func observeSystemAppearance() {
        appearanceObserver = NSApp.observe(\.effectiveAppearance) { [weak self] _, _ in
            Task { @MainActor in
                guard let self, Self.appearancePreference == .system else { return }
                self.rebuildForAppearance()
            }
        }
    }

    public func windowWillClose(_ notification: Notification) {
        // Cancel UI-owned passive work; Core is untouched (MT-001, §8.3).
        window = nil
        contentStack = nil
        sidebarStack = nil
    }

    // MARK: - Construction

    private func buildWindow() -> NSWindow {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 900, height: 640),
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        window.title = "Core Settings"
        // The design's default is `appearance: "system"`, which resolves
        // through `prefers-color-scheme`; nil appearance is AppKit's
        // equivalent. Forcing `.darkAqua` meant the window could never follow
        // the Mac, and on a light Mac it was simply the wrong theme.
        window.appearance = Self.appearancePreference.nsAppearance
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
        window.backgroundColor = Ov.bgContent
        window.isReleasedWhenClosed = false
        // `#cs-win{height:min(640px, 100vh-110px);min-height:420px}` — the
        // design window adapts, so this one does too. 640 wide is the
        // breakpoint `core-settings.css` narrows the sidebar at; stopping
        // there keeps the pane usable without needing that rule.
        window.minSize = NSSize(width: 640, height: 420)
        window.contentView = buildContent(appearance: Self.targetAppearance)
        renderSidebar()
        render()
        return window
    }

    /// The whole view tree, built against `window`'s current appearance.
    ///
    /// Rebuilt wholesale when the theme changes: a layer's colour is a
    /// snapshot taken when it was set, so there is nothing to invalidate.
    private func buildContent(appearance: NSAppearance) -> NSView {
        var built: NSView!
        // Resolve every colour taken during this build against the appearance
        // being switched *to*, passed in explicitly.
        //
        // Reading it back from the window does not work: setting
        // `window.appearance` does not update `effectiveAppearance` before
        // this returns, so the rebuild resolved layer colours against the old
        // theme while the text fields — live dynamic colours — resolved
        // against the new one. The result was white text on white cards.
        appearance.performAsCurrentDrawingAppearance {
            built = self.buildContentTree()
        }
        return built
    }

    /// The appearance the window should be drawn in right now.
    private static var targetAppearance: NSAppearance {
        appearancePreference.nsAppearance ?? NSApp.effectiveAppearance
    }

    private func buildContentTree() -> NSView {
        // ---- Branded header (60px, prototype .cs-head) ----
        let header = headerView()

        // ---- Sidebar (200px, prototype .sw-side) ----
        let sidebar = buildSidebar()

        // ---- Content pane (prototype .sw-pane) ----
        let content = NSStackView()
        content.orientation = .vertical
        content.alignment = .leading
        content.spacing = 0
        content.edgeInsets = NSEdgeInsets(top: 24, left: 24, bottom: 56, right: 24)
        let contentHost = FlippedView()
        contentHost.wantsLayer = true
        contentHost.layer?.backgroundColor = Ov.bgContent.cgColor
        // The document view must be constraint-driven too. Left on its
        // autoresizing mask it keeps whatever frame the scroll view hands it,
        // and the pane renders empty however much content is stacked inside.
        contentHost.translatesAutoresizingMaskIntoConstraints = false
        content.translatesAutoresizingMaskIntoConstraints = false
        contentHost.addSubview(content)
        NSLayoutConstraint.activate([
            content.topAnchor.constraint(equalTo: contentHost.topAnchor),
            content.leadingAnchor.constraint(equalTo: contentHost.leadingAnchor),
            content.trailingAnchor.constraint(equalTo: contentHost.trailingAnchor),
            // Without this the host has no height to derive from its content.
            content.bottomAnchor.constraint(equalTo: contentHost.bottomAnchor),
        ])
        let scroll = NSScrollView()
        scroll.documentView = contentHost
        scroll.hasVerticalScroller = true
        scroll.drawsBackground = true
        scroll.backgroundColor = Ov.bgContent
        scroll.translatesAutoresizingMaskIntoConstraints = false
        // Match the clip view's width so the pane fills horizontally and
        // wrapping labels get a real `preferredMaxLayoutWidth` to wrap against;
        // only the vertical axis scrolls.
        NSLayoutConstraint.activate([
            contentHost.widthAnchor.constraint(equalTo: scroll.contentView.widthAnchor),
        ])
        scroll.setContentHuggingPriority(.init(1), for: .horizontal)
        contentStack = content

        // ---- Body: sidebar + pane ----
        let body = NSStackView()
        body.orientation = .horizontal
        body.alignment = .top
        body.spacing = 0
        // Default gravity-area distribution centres the slack, which leaves a
        // dead gap to the left of the sidebar. `.fill` hands the slack to the
        // lowest-hugging child — the pane — and keeps the sidebar flush.
        body.distribution = .fill
        body.addArrangedSubview(sidebar)
        body.addArrangedSubview(scroll)

        let root = NSStackView()
        root.orientation = .vertical
        root.spacing = 0
        root.addArrangedSubview(header)
        root.addArrangedSubview(body)

        NSLayoutConstraint.activate([
            sidebar.widthAnchor.constraint(equalToConstant: 200),
            // A horizontal NSStackView has no "fill" alignment, so the sidebar
            // and the pane are given the body's height explicitly. Otherwise
            // both collapse to their intrinsic height and the pane has none.
            sidebar.heightAnchor.constraint(equalTo: body.heightAnchor),
            scroll.heightAnchor.constraint(equalTo: body.heightAnchor),
            // The pane's width is otherwise circular — the document view is
            // sized from the clip view, and the scroll view from the document
            // view — so it settles on an intrinsic size and leaves the sidebar
            // off the window edge. Giving the body the window's width breaks
            // the cycle and `.fill` then hands the remainder to the pane.
            body.widthAnchor.constraint(equalTo: root.widthAnchor),
            header.widthAnchor.constraint(equalTo: root.widthAnchor),
        ])
        return root
    }

    /// 60px branded header: the concentric-circle mark in brick, "OmniVia"
    /// wordmark with "Core Settings" secondary (prototype .cs-brand).
    private func headerView() -> NSView {
        let header = DraggableSheenView(
            surface: Ov.chromeTitlebar,
            stops: Ov.titlebarSheen,
            horizontal: true,
            specular: true
        )
        header.frame = NSRect(x: 0, y: 0, width: 900, height: 60)
        header.heightAnchor.constraint(equalToConstant: 60).isActive = true

        let row = NSStackView()
        row.orientation = .horizontal
        row.alignment = .centerY
        row.spacing = 10
        row.edgeInsets = NSEdgeInsets(top: 0, left: 78, bottom: 0, right: 16)
        row.translatesAutoresizingMaskIntoConstraints = false
        header.addSubview(row)
        NSLayoutConstraint.activate([
            row.topAnchor.constraint(equalTo: header.topAnchor),
            row.bottomAnchor.constraint(equalTo: header.bottomAnchor),
            row.leadingAnchor.constraint(equalTo: header.leadingAnchor),
            row.trailingAnchor.constraint(lessThanOrEqualTo: header.trailingAnchor),
        ])

        // The mark (`.cs-brand .mk`, 22×22, accent): the brand SVG rendered as
        // a template so it tints, with the drawn tile as fallback. Both get an
        // explicit size — the fallback has no intrinsic one, so without it the
        // stack collapsed the mark to nothing and the failure was invisible.
        let markView: NSView
        if let image = BrandAssets.image(named: "omnivia-mark") {
            image.isTemplate = true
            let icon = NSImageView()
            icon.image = image
            icon.imageScaling = .scaleProportionallyUpOrDown
            icon.contentTintColor = Ov.accent
            markView = icon
        } else {
            markView = MarkView(frame: NSRect(x: 0, y: 0, width: 22, height: 22))
        }
        markView.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            markView.widthAnchor.constraint(equalToConstant: 22),
            markView.heightAnchor.constraint(equalToConstant: 22),
        ])
        row.addArrangedSubview(markView)

        let titleColumn = NSStackView()
        titleColumn.orientation = .vertical
        titleColumn.alignment = .leading
        titleColumn.spacing = 1
        let wordmark = OvFont.wordmark("OmniVia")
        let sub = OvFont.label("Core Settings", size: 11, color: Ov.textSecondary)
        titleColumn.addArrangedSubview(wordmark)
        titleColumn.addArrangedSubview(sub)
        row.addArrangedSubview(titleColumn)

        // Bottom separator.
        let line = NSView()
        line.wantsLayer = true
        line.layer?.backgroundColor = Ov.separator.cgColor
        line.translatesAutoresizingMaskIntoConstraints = false
        header.addSubview(line)
        NSLayoutConstraint.activate([
            line.heightAnchor.constraint(equalToConstant: 1),
            line.leadingAnchor.constraint(equalTo: header.leadingAnchor),
            line.trailingAnchor.constraint(equalTo: header.trailingAnchor),
            line.bottomAnchor.constraint(equalTo: header.bottomAnchor),
        ])
        return header
    }

    // MARK: Sidebar

    /// The source list: a translucent sidebar material with the brick sheen
    /// over it, the scrolling nav above, and the service-status footer pinned
    /// to the bottom edge (`.sw-side` / `.sw-nav` / `.cs-foot`).
    private func buildSidebar() -> NSView {
        let effect = SheenView(
            surface: Ov.chromeSidebar,
            stops: Ov.sidebarSheen,
            horizontal: false
        )
        effect.frame = NSRect(x: 0, y: 0, width: 200, height: 640)

        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 0
        stack.edgeInsets = NSEdgeInsets(top: 10, left: 8, bottom: 12, right: 8)
        stack.translatesAutoresizingMaskIntoConstraints = false
        effect.addSubview(stack)

        let footer = buildSidebarFooter()
        footer.translatesAutoresizingMaskIntoConstraints = false
        effect.addSubview(footer)

        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: effect.topAnchor),
            stack.leadingAnchor.constraint(equalTo: effect.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: effect.trailingAnchor),
            // The nav takes the space above the footer rather than the footer
            // trailing the last pane; `.cs-foot` is pinned to the sidebar's
            // bottom edge and `.sw-nav` is the flexible child.
            stack.bottomAnchor.constraint(lessThanOrEqualTo: footer.topAnchor),
            footer.leadingAnchor.constraint(equalTo: effect.leadingAnchor),
            footer.trailingAnchor.constraint(equalTo: effect.trailingAnchor),
            footer.bottomAnchor.constraint(equalTo: effect.bottomAnchor),
        ])
        sidebarStack = stack
        return effect
    }

    /// `.cs-foot`: a hairline rule, a status dot and one line of text.
    ///
    /// The design's footer reports the service state. This window never invokes
    /// a lifecycle command (MR-005), so it does not know that state and says so
    /// rather than showing a dot that implies one — an unknown rendered as a
    /// green tick is exactly what the amendment's §A6 forbids.
    private func buildSidebarFooter() -> NSView {
        let container = NSView()

        let rule = NSView()
        rule.wantsLayer = true
        rule.layer?.backgroundColor = Ov.separator.cgColor
        rule.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(rule)

        let dot = NSView()
        dot.wantsLayer = true
        dot.layer?.cornerRadius = 3.5
        dot.layer?.backgroundColor = Ov.textQuaternary.cgColor
        dot.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(dot)

        let label = NSTextField(wrappingLabelWithString: "Core status lives in the menu")
        label.font = OvFont.sans(11.5)
        label.textColor = Ov.textSecondary
        label.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(label)

        NSLayoutConstraint.activate([
            rule.topAnchor.constraint(equalTo: container.topAnchor),
            rule.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            rule.trailingAnchor.constraint(equalTo: container.trailingAnchor),
            rule.heightAnchor.constraint(equalToConstant: 1),

            dot.widthAnchor.constraint(equalToConstant: 7),
            dot.heightAnchor.constraint(equalToConstant: 7),
            dot.leadingAnchor.constraint(equalTo: container.leadingAnchor, constant: 16),
            dot.topAnchor.constraint(equalTo: container.topAnchor, constant: 10 + 3),

            label.leadingAnchor.constraint(equalTo: dot.trailingAnchor, constant: 7),
            label.trailingAnchor.constraint(equalTo: container.trailingAnchor, constant: -16),
            label.topAnchor.constraint(equalTo: container.topAnchor, constant: 10),
            label.bottomAnchor.constraint(equalTo: container.bottomAnchor, constant: -12),
        ])
        return container
    }

    private func renderSidebar() {
        Self.targetAppearance.performAsCurrentDrawingAppearance { self.renderSidebarBody() }
    }

    private func renderSidebarBody() {
        guard let stack = sidebarStack else { return }
        stack.views.forEach { stack.removeView($0) }

        let panes = Pane.allCases
        for (index, pane) in panes.enumerated() {
            let row = SidebarRowView(title: pane.rawValue, symbol: pane.symbol)
            row.isSelected = pane == selectedPane
            row.onSelect = { [weak self] in self?.selectedPane = pane }
            row.moveSelection = { [weak self] delta in
                guard let self else { return }
                let target: Int
                switch delta {
                case .min: target = 0
                case .max: target = panes.count - 1
                default: target = min(max(index + delta, 0), panes.count - 1)
                }
                guard target != index else { return }
                self.selectedPane = panes[target]
                self.focusSidebarRow(at: target)
            }
            stack.addArrangedSubview(row)
            // Inside the nav's 8pt side padding, not across it. Pinned to the
            // stack's full width, the 6pt-radius pill ran edge to edge of the
            // sidebar and stopped reading as a rounded row at all.
            row.widthAnchor.constraint(
                equalTo: stack.widthAnchor,
                constant: -(stack.edgeInsets.left + stack.edgeInsets.right)
            ).isActive = true
        }

        // The footer is pinned to the sidebar's bottom edge by `buildSidebar`,
        // not appended after the last row.
    }

    /// Return the pane to its top. `contentHost` is a `FlippedView`, so the
    /// document's origin is its top-left and the top is simply y = 0.
    private func scrollToTop() {
        guard let scroll = contentStack?.enclosingScrollView else { return }
        // The new pane's views are added but not yet laid out, so the clip
        // view would otherwise clamp the scroll against the old content height.
        scroll.layoutSubtreeIfNeeded()
        scroll.contentView.scroll(to: .zero)
        scroll.reflectScrolledClipView(scroll.contentView)
    }

    /// Keep the keyboard on the sidebar after an arrow key: selecting a pane
    /// rebuilds every row, so the focused view is gone by the time the key
    /// handler returns.
    private func focusSidebarRow(at index: Int) {
        guard let stack = sidebarStack else { return }
        let rows = stack.views.compactMap { $0 as? SidebarRowView }
        guard rows.indices.contains(index) else { return }
        window?.makeFirstResponder(rows[index])
    }

    // MARK: - Rendering

    private func render() {
        // Cards and rows are built here, not in `buildContentTree`, so this
        // needs the same appearance the shell was built in. Without it the
        // chrome followed the theme and the pane did not — white text on
        // white cards, because layer fills are snapshots and text colours
        // are live.
        Self.targetAppearance.performAsCurrentDrawingAppearance { self.renderBody() }
    }

    private func renderBody() {
        guard let content = contentStack else { return }
        content.views.forEach { content.removeView($0) }

        let title = OvFont.paneTitle(selectedPane.rawValue)
        content.addArrangedSubview(title)
        content.addArrangedSubview(ovSpacer(5))
        let lede = NSTextField(wrappingLabelWithString: selectedPane.lede)
        lede.font = OvFont.sans(12.5)
        lede.textColor = Ov.textSecondary
        content.addArrangedSubview(lede)
        content.addArrangedSubview(ovSpacer(18))

        switch selectedPane {
        case .general: renderGeneral(content)
        case .data: renderData(content)
        case .capture: renderCapture(content)
        case .documentProcessing: renderDocumentProcessing(content)
        case .processing: renderProcessing(content)
        case .access: renderAccess(content)
        case .maintenance: renderMaintenance(content)
        }
        content.addArrangedSubview(ovSpacer(8))
    }

    // MARK: Card/group helpers (prototype .sw-group / .sw-rows)

    private func card(
        _ stack: NSStackView, header: String?, followedByDisclosure: Bool = false
    ) -> NSStackView {
        if let header {
            let label = OvFont.groupHeader(header)
            stack.addArrangedSubview(label)
            stack.addArrangedSubview(ovSpacer(8))
        }
        let card = NSStackView()
        card.orientation = .vertical
        card.alignment = .leading
        card.spacing = 0
        card.edgeInsets = NSEdgeInsets(top: 0, left: 0, bottom: 0, right: 0)
        card.wantsLayer = true
        card.layer?.backgroundColor = Ov.bgContent.cgColor
        card.layer?.cornerRadius = 10
        card.layer?.borderWidth = 1
        card.layer?.borderColor = Ov.borderSubtle.cgColor
        stack.addArrangedSubview(card)
        // Cards fill the pane. Left intrinsic, each card is only as wide as its
        // widest row, so a trailing chip lands wherever that row's text happens
        // to end and the status column never lines up.
        card.widthAnchor.constraint(
            equalTo: stack.widthAnchor,
            constant: -(stack.edgeInsets.left + stack.edgeInsets.right)
        ).isActive = true
        // A disclosure belonging to this group sits 10pt under the rows box
        // (`.cs-disc{margin-top:10px}`), which it supplies itself; the 18pt
        // gap is between *groups*.
        if !followedByDisclosure { stack.addArrangedSubview(ovSpacer(18)) }
        return card
    }

    /// `notBuilt` marks a control the design specifies but whose backend does
    /// not exist in this build. It is deliberately the brand accent rather than
    /// success/warning: it is neither healthy nor broken, and an amber chip
    /// would read as a fault the user could repair (amendment §A6 — "A
    /// preference deliberately switched off is not an issue to repair").
    /// Apply the `.sw-rows` chrome to a stack that already holds rows. A
    /// disclosure's *body* is a rows box; its summary is not, which is why the
    /// chrome goes here rather than around the whole disclosure.
    @discardableResult
    private func card(_ rows: NSStackView) -> NSStackView {
        rows.wantsLayer = true
        rows.layer?.backgroundColor = Ov.bgContent.cgColor
        rows.layer?.cornerRadius = 10
        rows.layer?.borderWidth = 1
        rows.layer?.borderColor = Ov.borderSubtle.cgColor
        rows.layer?.masksToBounds = true
        return rows
    }

    /// The design's status kinds, one glyph each (`core-mac.js` → `KIND`):
    ///
    /// ```js
    /// ok: check-circle-2   neutral/unknown: circle-dashed   approval: alert-circle
    /// attention: alert-triangle   stale: clock   off: circle-slash   blocked: x-circle
    /// ```
    ///
    /// `notBuilt` is the one addition — a feature absent from this build, which
    /// the design never has to describe. It takes its own glyph rather than
    /// borrowing `off`'s slash: "switched off" and "does not exist" must not be
    /// the same mark in two colours, which is the failure §A6 names.
    private enum ChipTone {
        case ok, neutral, approval, attention, unknown, stale, checking, off, blocked, notBuilt
    }

    private static func chipGlyph(_ tone: ChipTone) -> String {
        switch tone {
        case .ok: return "checkmark.circle"          // check-circle-2 (outline)
        case .neutral, .unknown: return "circle.dashed"
        case .approval: return "exclamationmark.circle"
        case .attention: return "exclamationmark.triangle"
        case .stale: return "clock"
        case .off: return "circle.slash"
        case .blocked: return "xmark.circle"
        case .notBuilt: return "minus.circle"
        case .checking: return ""                     // a spinner, not a glyph
        }
    }

    private static func chipColor(_ tone: ChipTone) -> NSColor {
        switch tone {
        case .ok: return Ov.success
        case .approval, .attention: return Ov.warning
        case .notBuilt: return Ov.accent
        case .neutral, .unknown, .stale, .off, .blocked, .checking: return Ov.textTertiary
        }
    }

    /// `.cs-status.warn{color:var(--ov-text-primary)}` — only the two warning
    /// kinds lift their label; everything else stays secondary.
    private static func chipTextColor(_ tone: ChipTone) -> NSColor {
        switch tone {
        case .approval, .attention, .notBuilt: return Ov.textPrimary
        default: return Ov.textSecondary
        }
    }

    /// Status chip (prototype .cs-status): icon + word, never colour alone
    /// (amendment §A6, §A22 — the word carries the state without the colour).
    private func chip(_ text: String, tone: ChipTone) -> NSView {
        let row = NSStackView()
        row.orientation = .horizontal
        row.alignment = .centerY
        row.spacing = 5

        let leading: NSView
        if tone == .checking {
            // `.cs-sp` is a 12px spinning ring, not a glyph. A static dashed
            // circle here read as "unknown", which is a different state.
            let spinner = NSProgressIndicator()
            spinner.style = .spinning
            spinner.controlSize = .small
            spinner.isIndeterminate = true
            spinner.startAnimation(nil)
            spinner.widthAnchor.constraint(equalToConstant: 13).isActive = true
            spinner.heightAnchor.constraint(equalToConstant: 13).isActive = true
            leading = spinner
        } else {
            let icon = NSImageView()
            icon.image = NSImage(
                systemSymbolName: Self.chipGlyph(tone), accessibilityDescription: text
            )?.withSymbolConfiguration(.init(pointSize: 13, weight: .regular))
            icon.contentTintColor = Self.chipColor(tone)
            leading = icon
        }
        leading.setContentHuggingPriority(.required, for: .horizontal)

        let label = OvFont.label(text, size: 11.5, color: Self.chipTextColor(tone))
        row.addArrangedSubview(leading)
        row.addArrangedSubview(label)
        return row
    }

    /// Row (prototype .sw-row): title line with trailing control column and an
    /// optional detail under it. Returns the row stack for appending chips.
    @discardableResult
    private func row(
        into card: NSStackView,
        title: String,
        detail: String?,
        trailing: NSView? = nil,
        compact: Bool = false
    ) -> NSStackView {
        let line = NSStackView()
        line.orientation = .horizontal
        line.alignment = .centerY
        line.spacing = 8
        // A disclosure body's rows are tighter and lighter than a pane's.
        let t = OvFont.label(
            title, size: 13, cssWeight: compact ? 500 : 600, color: Ov.textPrimary
        )
        line.addArrangedSubview(t)
        if let trailing {
            let spring = NSView()
            spring.setContentHuggingPriority(.init(1), for: .horizontal)
            line.addArrangedSubview(spring)
            line.addArrangedSubview(trailing)
        }
        let body = NSStackView()
        body.orientation = .vertical
        body.alignment = .leading
        body.spacing = 3
        let pad: CGFloat = compact ? 10 : 13
        body.edgeInsets = NSEdgeInsets(top: pad, left: 16, bottom: pad, right: 16)
        body.addArrangedSubview(line)
        var detailLabel: NSTextField?
        if let detail {
            let d = NSTextField(wrappingLabelWithString: detail)
            d.font = OvFont.sans(11.5)
            d.textColor = Ov.textTertiary
            body.addArrangedSubview(d)
            detailLabel = d
        }
        card.addArrangedSubview(body)
        // The row and its title line both span the card, so the spring in
        // `line` actually has slack to push the trailing control to the right
        // edge. Without these the chip column is ragged.
        body.widthAnchor.constraint(equalTo: card.widthAnchor).isActive = true
        line.widthAnchor.constraint(
            equalTo: body.widthAnchor,
            constant: -(body.edgeInsets.left + body.edgeInsets.right)
        ).isActive = true
        if let detailLabel {
            wrap(detailLabel, within: body, inset: body.edgeInsets.left + body.edgeInsets.right)
        }
        addSeparator(below: body, in: card)
        return body
    }

    /// A rule *between* rows. `.sw-row:last-child{border-bottom:none}`, so the
    /// previous row's rule is added when the next one arrives rather than
    /// trailing every row — otherwise the last separator lands straight on the
    /// card's own border and every card ends in a doubled hairline.
    private func addSeparator(below view: NSView, in card: NSStackView) {
        guard let index = card.arrangedSubviews.firstIndex(of: view), index > 0 else { return }
        let line = NSView()
        line.wantsLayer = true
        line.layer?.backgroundColor = Ov.separator.cgColor
        line.heightAnchor.constraint(equalToConstant: 1).isActive = true
        card.insertArrangedSubview(line, at: index)
        // A bare NSView has no intrinsic width, so the rule needs one or it
        // draws as nothing in a leading-aligned stack.
        line.widthAnchor.constraint(equalTo: card.widthAnchor).isActive = true
    }

    /// Recovery block (prototype .cs-fix): title, known effect, actions.
    private func recoveryBlock(
        title: String,
        detail: String? = nil,
        actions: [NSButton],
        warn: Bool = false
    ) -> NSStackView {
        let block = NSStackView()
        block.orientation = .vertical
        block.alignment = .leading
        block.spacing = 6
        block.edgeInsets = NSEdgeInsets(top: 10, left: 12, bottom: 10, right: 12)
        block.wantsLayer = true
        block.layer?.backgroundColor = Ov.bgContentSecondary.cgColor
        block.layer?.cornerRadius = 8
        block.layer?.borderWidth = 1
        block.layer?.borderColor = warn ? Ov.warning.withAlphaComponent(0.35).cgColor : Ov.borderSubtle.cgColor

        // .cs-fix-t 12.5/600; .cs-fix-d 11.5 secondary mt 3; .cs-fix-a mt 9.
        let t = NSTextField(wrappingLabelWithString: title)
        t.font = OvFont.sans(12.5, 600)
        t.textColor = Ov.textPrimary
        block.addArrangedSubview(t)
        if let detail {
            let d = NSTextField(wrappingLabelWithString: detail)
            d.font = OvFont.sans(11.5)
            d.textColor = Ov.textSecondary
            block.addArrangedSubview(d)
        }
        if !actions.isEmpty {
            block.addArrangedSubview(ovSpacer(6)) // .cs-fix-a margin-top 9 = 3 row gap + 6
            let acts = NSStackView()
            acts.orientation = .horizontal
            acts.spacing = 8
            acts.alignment = .centerY
            actions.forEach { acts.addArrangedSubview($0) }
            block.addArrangedSubview(acts)
        }
        return block
    }

    /// Pane note (prototype .cs-note / .cs-note.attn): an icon and a sentence,
    /// used for the standing facts a pane needs to state rather than a status
    /// a row owns.
    /// Pane note (`.cs-note`): an icon and a sentence, for the standing facts a
    /// pane states rather than a status a row owns.
    ///
    /// Deliberately unboxed. `.cs-note` declares no background, border, padding
    /// or radius — it is tertiary text with a 13px glyph. Drawing it as a
    /// filled slab made it read as a recovery block, which *is* boxed, losing
    /// the one cue that separates "a fact about this pane" from "something you
    /// can act on".
    private func note(_ text: String, symbol: String = "info.circle", attn: Bool = false) -> NSStackView {
        let box = NSStackView()
        box.orientation = .horizontal
        box.alignment = .firstBaseline
        box.spacing = 8
        box.edgeInsets = NSEdgeInsets(top: 14, left: 0, bottom: 0, right: 0)
        let icon = NSImageView()
        if let image = NSImage(systemSymbolName: symbol, accessibilityDescription: nil)?
            .withSymbolConfiguration(.init(pointSize: 13, weight: .regular)) {
            icon.image = image
            icon.contentTintColor = attn ? Ov.warning : Ov.textTertiary
        }
        icon.setContentHuggingPriority(.required, for: .horizontal)
        let label = NSTextField(wrappingLabelWithString: text)
        label.font = OvFont.sans(11.5)
        label.textColor = attn ? Ov.textSecondary : Ov.textTertiary
        box.addArrangedSubview(icon)
        box.addArrangedSubview(label)
        wrap(label, within: box, inset: 8 + 13 + 8)
        return box
    }

    /// Let a wrapping label follow its container instead of a fixed
    /// `preferredMaxLayoutWidth`. Hard-coded wrap widths meant text could not
    /// reflow, which is only invisible while the window cannot be resized.
    private func wrap(_ label: NSTextField, within container: NSView, inset: CGFloat) {
        label.translatesAutoresizingMaskIntoConstraints = false
        let width = label.widthAnchor.constraint(
            equalTo: container.widthAnchor, constant: -inset
        )
        width.priority = .defaultHigh
        width.isActive = true
    }

    /// Disclosure (prototype .cs-disc): a summary row that reveals a body.
    /// NSStackView collapses a hidden arranged subview, so toggling `isHidden`
    /// is the whole mechanism. Open state is keyed by `id` and survives the
    /// full re-render that every snapshot change triggers.
    @discardableResult
    private func disclosure(
        into parent: NSStackView, id: String, label: String, body: NSView
    ) -> NSStackView {
        let box = NSStackView()
        box.orientation = .vertical
        box.alignment = .leading
        box.spacing = 0

        let isOpen = openDisclosures.contains(id)
        let chevron = NSImageView()
        // Swap the glyph rather than rotating the view. `frameCenterRotation`
        // on a constraint-driven image view rotates about a frame the layout
        // then re-sizes, which rendered the open state as a stray tick.
        chevron.image = NSImage(
            systemSymbolName: isOpen ? "chevron.down" : "chevron.right",
            accessibilityDescription: nil
        )?.withSymbolConfiguration(.init(pointSize: 11, weight: .semibold))
        chevron.contentTintColor = Ov.textTertiary
        chevron.setContentHuggingPriority(.required, for: .horizontal)

        let summary = DisclosureButton(id: id)
        summary.target = self
        summary.action = #selector(toggleDisclosure(_:))
        summary.setAccessibilityLabel(label)
        summary.setAccessibilityExpanded(isOpen)

        let line = NSStackView(views: [chevron, OvFont.label(
            label, size: 12.5, cssWeight: 590, color: Ov.textSecondary
        )])
        line.orientation = .horizontal
        line.alignment = .centerY
        line.spacing = 5
        line.edgeInsets = NSEdgeInsets(top: 3, left: 3, bottom: 3, right: 6)
        summary.addSubview(line)
        line.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            line.leadingAnchor.constraint(equalTo: summary.leadingAnchor),
            line.trailingAnchor.constraint(equalTo: summary.trailingAnchor),
            line.topAnchor.constraint(equalTo: summary.topAnchor),
            line.bottomAnchor.constraint(equalTo: summary.bottomAnchor),
        ])
        // `.cs-disc>summary` is inline-flex: it is as wide as its own label, so
        // the hover fill is a pill around the words rather than a bar across
        // the pane.
        summary.setContentHuggingPriority(.required, for: .horizontal)

        box.edgeInsets = NSEdgeInsets(top: 10, left: 0, bottom: 0, right: 0)
        box.addArrangedSubview(summary)
        let gap = ovSpacer(8)
        box.addArrangedSubview(gap)
        box.addArrangedSubview(body)
        body.isHidden = !isOpen
        gap.isHidden = !isOpen

        parent.addArrangedSubview(box)
        // The box follows the pane, and the rows box fills it. Tying the body
        // to the box alone was circular — the box sized to its content and the
        // content to the box — so the panel settled far narrower than a card.
        NSLayoutConstraint.activate([
            box.widthAnchor.constraint(
                equalTo: parent.widthAnchor,
                constant: -(parent.edgeInsets.left + parent.edgeInsets.right)
            ),
            body.widthAnchor.constraint(equalTo: box.widthAnchor),
        ])
        return box
    }

    /// A control the design specifies whose backend is absent in this build.
    /// The row renders at full fidelity — title, detail, trailing chip — so the
    /// shape of the finished pane is visible, but it offers no control to
    /// change, because there is nothing behind it to change (§A5 — "Hide an
    /// unimplemented control rather than falsely offering installation").
    @discardableResult
    private func notBuiltRow(
        into card: NSStackView,
        title: String,
        detail: String?,
        status: String = "Not in this build",
        tone: ChipTone = .notBuilt,
        compact: Bool = false
    ) -> NSStackView {
        // Status goes in the row's supporting area, under the detail, which is
        // where the design puts it (`d: "…<br>" + status`). The trailing slot
        // holds controls; putting a chip there mixed status into the control
        // column and left General — which does follow the design — looking
        // like a different pane.
        let body = row(into: card, title: title, detail: detail, compact: compact)
        body.addArrangedSubview(ovSpacer(1))
        body.addArrangedSubview(chip(status, tone: tone))
        return body
    }

    /// `.ov-toggle`. `NSSwitch` is the native control: correct hit target,
    /// keyboard operable and announced properly by VoiceOver, none of which a
    /// hand-drawn pill would get for free. Its "on" fill follows the user's
    /// system accent rather than brick — that is macOS behaviour for a real
    /// switch, and worth more than the colour match.
    private func ovToggle(on: Bool, enabled: Bool, label: String) -> NSSwitch {
        let toggle = NSSwitch()
        toggle.state = on ? .on : .off
        toggle.isEnabled = enabled
        toggle.setAccessibilityLabel(label)
        return toggle
    }

    // MARK: OmniVia buttons (prototype .ov-btn / .ov-btn--bordered / --primary)

    private func button(
        _ title: String,
        style: OvButtonStyle = .bordered,
        action: Selector,
        subject: String? = nil
    ) -> NSButton {
        let button = OvButton(title: title, style: style)
        button.target = self
        button.action = action
        if let subject { button.setAccessibilityLabel("\(title) — \(subject)") }
        return button
    }

    // MARK: General

    /// General (design `core-panes.js` → `general()`): four behaviour rows in
    /// one unlabelled group, then the macOS access summary, then the note.
    ///
    /// The summary goes *below* the settings, not above them — it is a
    /// lightweight roll-up of the rows, and leading with it makes the pane
    /// read as a permissions checklist, which §A1 rules out.
    private func renderGeneral(_ content: NSStackView) {
        let preferences = currentPreferences()
        let behaviour = card(content, header: nil)

        // ---- Open menu bar at login ----
        // A preference and an OS registration are separate facts (§A3). This
        // build installs no login item, so there is no registration to report
        // and nothing for the toggle to change.
        let menuBody = row(
            into: behaviour,
            title: "Open menu bar at login",
            detail: "Keep Core status and settings available when you sign in.",
            trailing: ovToggle(on: false, enabled: false, label: "Open menu bar at login")
        )
        menuBody.addArrangedSubview(ovSpacer(2))
        menuBody.addArrangedSubview(chip("No login item in this build", tone: .notBuilt))

        renderStartupRow(into: behaviour)
        renderNotificationsRow(into: behaviour)

        // ---- Appearance ----
        // System / Light / Dark, per the design. Only the dark token layer
        // exists in this build, so the control is shown in its finished shape
        // and marked rather than offering a switch that cannot take effect.
        let appearance = NSSegmentedControl(
            labels: Self.AppearancePreference.allCases.map(\.label),
            trackingMode: .selectOne,
            target: self,
            action: #selector(changeAppearance(_:))
        )
        appearance.selectedSegment = Self.AppearancePreference.allCases
            .firstIndex(of: Self.appearancePreference) ?? 0
        appearance.setAccessibilityLabel("Appearance")
        row(
            into: behaviour,
            title: "Appearance",
            detail: "Applies to this window and the menu bar item.",
            trailing: appearance
        )

        // ---- macOS access summary (one per window; §7.5 aggregation) ----
        let summary = ReadinessReducer.summarize(
            checks: coordinator.snapshot.checks,
            preferences: preferences,
            refreshInFlight: coordinator.refreshInFlight,
            everObserved: coordinator.snapshot.observedAt != nil
        )
        let summaryCard = card(content, header: "macOS access")
        let headlineRow = NSStackView()
        headlineRow.orientation = .horizontal
        headlineRow.alignment = .centerY
        headlineRow.spacing = 8
        let headline = NSTextField(wrappingLabelWithString: summary.headline)
        headline.font = OvFont.sans(13, 590)
        headline.textColor = Ov.textPrimary
        headlineRow.addArrangedSubview(headline)
        let spring = NSView()
        spring.setContentHuggingPriority(.init(1), for: .horizontal)
        headlineRow.addArrangedSubview(spring)
        headlineRow.addArrangedSubview(
            button("Refresh status", style: .bordered, action: #selector(refreshStatus))
        )
        let summaryBody = NSStackView()
        summaryBody.orientation = .vertical
        summaryBody.alignment = .leading
        summaryBody.spacing = 4
        // Share the row inset (16) rather than sitting flush to the card edge.
        summaryBody.edgeInsets = NSEdgeInsets(top: 13, left: 16, bottom: 13, right: 16)
        summaryBody.addArrangedSubview(headlineRow)
        if let detail = summary.detail {
            let d = NSTextField(wrappingLabelWithString: detail)
            d.font = OvFont.sans(11.5)
            d.textColor = Ov.textTertiary
            summaryBody.addArrangedSubview(d)
        }
        summaryCard.addArrangedSubview(summaryBody)

        if summary.kind == .attention {
            for issue in summary.issues {
                var actions: [NSButton] = []
                var detail: String?
                switch issue.checkID {
                case .coreBackground, .companionLogin:
                    actions = [button("Open Login Items", style: .bordered, action: #selector(openLoginItems), subject: "Core startup")]
                    detail = "System Settings → General → Login Items & Extensions. Allow \"OmniVia Core\" under Allow in the background."
                case .notificationsDelivery:
                    actions = [button("Open Notifications", style: .bordered, action: #selector(openNotifications), subject: "notifications")]
                    detail = "System Settings → Notifications → OmniVia Core. Turn on Allow notifications and Alerts."
                default: break
                }
                guard !actions.isEmpty else { continue }
                summaryCard.addArrangedSubview(
                    recoveryBlock(title: issue.title, detail: detail, actions: actions, warn: true)
                )
            }
        }

        generalClosingNote(content)
    }

    /// Start Core at login (companion login / Core background are distinct
    /// subjects, MT-008).
    private func renderStartupRow(into behaviour: NSStackView) {
        let startupCheck = coordinator.snapshot.checks.first { $0.checkID == .coreBackground }
        let startupRow = ReadinessReducer.startupRow(check: startupCheck, serviceRunning: nil)
        let startupState: (String, ChipTone)
        switch startupRow.registration {
        case "Enabled": startupState = ("Enabled", .ok)
        case "Needs approval": startupState = ("Needs approval", .approval)
        case "Not set up": startupState = ("Off · Optional", .off)
        default:
            startupState = coordinator.refreshInFlight
                ? ("Checking…", .checking)
                : ("Not checked", .unknown)
        }
        let startupBody = row(
            into: behaviour,
            title: "Start Core at login",
            detail: startupRow.detail ?? "Starts the Core service itself, separately from the menu bar item.",
            trailing: ovToggle(on: false, enabled: false, label: "Start Core at login")
        )
        startupBody.addArrangedSubview(ovSpacer(2))
        startupBody.addArrangedSubview(chip(startupState.0, tone: startupState.1))
        // Binding disposition (§19): no qualified registration mechanism exists,
        // so the row shows the honest state and the OS route — never a
        // registration this build cannot own.
        startupBody.addArrangedSubview(ovSpacer(5))
        startupBody.addArrangedSubview(
            recoveryBlock(
                title: "No startup mechanism is installed in this build.",
                detail: "When a qualified registration exists, its state and recovery appear here. Opening Login Items is always available.",
                actions: [button("Open Login Items", style: .bordered, action: #selector(openLoginItems), subject: "Core startup")]
            )
        )
    }

    /// Notify me when Core needs attention. The preference is this window's
    /// own and is live; whether macOS will deliver is a separate fact (§A3).
    private func renderNotificationsRow(into behaviour: NSStackView) {
        let notifyCheck = coordinator.snapshot.checks.first { $0.checkID == .notificationsDelivery }
        let notifyState: (String, ChipTone)
        switch notifyCheck?.observedState {
        case .allowed: notifyState = ("Allowed", .ok)
        case .limitedDelivery: notifyState = ("Alerts off · badges allowed", .attention)
        case .denied: notifyState = ("Blocked in macOS", .attention)
        // Not yet asked is a decision waiting on the user, not an unknown.
        case .notDetermined: notifyState = ("Needs your permission", .approval)
        case .unrecognized: notifyState = ("Not checked", .unknown)
        default:
            notifyState = coordinator.refreshInFlight
                ? ("Checking…", .checking)
                : ("Not checked", .unknown)
        }
        let notifyToggle = ovToggle(
            on: attentionNotificationsSelected,
            enabled: true,
            label: "Notify me when Core needs attention"
        )
        notifyToggle.target = self
        notifyToggle.action = #selector(toggleAttentionNotifications(_:))
        let notifyBody = row(
            into: behaviour,
            title: "Notify me when Core needs attention",
            detail: "Only for problems you can act on. Routine activity stays quiet.",
            trailing: notifyToggle
        )
        notifyBody.addArrangedSubview(ovSpacer(2))
        notifyBody.addArrangedSubview(chip(notifyState.0, tone: notifyState.1))
        var notifyActions: [NSButton] = []
        if notifyCheck?.observedState == .notDetermined {
            // Explicit, user-initiated request only; never on load (MT-013).
            notifyActions.append(button("Allow notifications…", style: .primary, action: #selector(requestNotifications), subject: "notifications"))
        } else if notifyCheck?.observedState == .denied || notifyCheck?.observedState == .limitedDelivery {
            notifyActions.append(button("Open Notifications", style: .bordered, action: #selector(openNotifications), subject: "notifications"))
        }
        if !notifyActions.isEmpty {
            notifyBody.addArrangedSubview(ovSpacer(5))
            notifyBody.addArrangedSubview(recoveryBlock(title: recoveryTitle(for: notifyCheck), actions: notifyActions, warn: notifyCheck?.observedState == .denied))
        }
    }

    private func generalClosingNote(_ content: NSStackView) {
        // The design closes General with this, and it is the pane's note rather
        // than the sidebar footer's line — the footer reports service state.
        content.addArrangedSubview(note("Closing this window does not stop Core."))
    }

    private func recoveryTitle(for check: ReadinessCheck?) -> String {
        switch check?.observedState {
        case .denied: return "Notifications are blocked in macOS."
        case .limitedDelivery: return "Alerts are off in macOS; only badges are allowed."
        default: return "Notifications need your permission before they can be delivered."
        }
    }

    // MARK: Data

    /// Data (prototype `data()`): workspace context, where it is stored, and
    /// backups. The window is a thin client — it holds the workspace ref and
    /// the installation revision it was launched with, and nothing else. Size,
    /// path and backup history are Core's to report and Core does not report
    /// them to this window yet, so they are marked rather than guessed. The
    /// companion must not derive the workspace path from the installation
    /// layout: that layout is Core's, not a shape this client may assume.
    private func renderData(_ content: NSStackView) {
        let context = coordinator.snapshot.context

        // ---- Workspace context (prototype wsContext) ----
        let workspaceCard = card(content, header: nil)
        row(
            into: workspaceCard,
            title: "Workspace",
            detail: "Managed here: Core on this Mac. Other applications keep their own workspace bindings.",
            trailing: OvFont.label(
                context.workspaceRef, size: 11.5, color: Ov.textSecondary, mono: true
            )
        )

        // ---- Storage ----
        let storageCard = card(content, header: "Storage", followedByDisclosure: true)
        row(
            into: storageCard,
            title: "Location",
            detail: nil,
            trailing: OvFont.label("This Mac", size: 12.5, color: Ov.textPrimary)
        )
        notBuiltRow(
            into: storageCard,
            title: "Storage location",
            detail: "Reported by Core. Shown for reference; it can’t be edited here. Core does not report the workspace path to this window in this build, and the companion does not infer it.",
            status: "Not reported",
            tone: .neutral
        )
        notBuiltRow(
            into: storageCard,
            title: "Storage used",
            detail: "A size and its breakdown appear here once Core reports them.",
            status: "Not reported",
            tone: .neutral
        )

        // The design's Storage group carries a breakdown behind a disclosure.
        let breakdownBody = NSStackView()
        breakdownBody.orientation = .vertical
        breakdownBody.alignment = .leading
        breakdownBody.spacing = 0
        for part in ["Source content", "Records", "Indexes", "Temporary data"] {
            notBuiltRow(
                into: breakdownBody,
                title: part,
                detail: nil,
                status: "Not reported",
                tone: .neutral,
                compact: true
            )
        }
        disclosure(
            into: content, id: "storage-details", label: "Storage details",
            body: card(breakdownBody)
        )
        content.addArrangedSubview(ovSpacer(18))

        // ---- Backups ----
        let backupCard = card(content, header: "Backups", followedByDisclosure: true)
        notBuiltRow(
            into: backupCard,
            title: "Last successful backup",
            detail: "Back up this workspace to keep a copy outside Core. Backup history appears here once the backup feature exists.",
            status: "Not in this build"
        )
        notBuiltRow(
            into: backupCard,
            title: "Back up",
            detail: "Creates a new backup at the destination, then verifies it.",
            status: "Not in this build"
        )

        let optionsBody = NSStackView()
        optionsBody.orientation = .vertical
        optionsBody.alignment = .leading
        optionsBody.spacing = 0
        notBuiltRow(
            into: optionsBody,
            title: "Destination",
            detail: "Where Core writes backups.",
            status: "Not in this build",
            compact: true
        )
        notBuiltRow(
            into: optionsBody,
            title: "Schedule",
            detail: "How often Core backs this workspace up.",
            status: "Not in this build",
            compact: true
        )
        notBuiltRow(
            into: optionsBody,
            title: "Keep backups for",
            detail: "How long Core keeps earlier backups before removing them.",
            status: "Not in this build",
            compact: true
        )
        notBuiltRow(
            into: optionsBody,
            title: "Destination access",
            detail: "A bounded test — it may create and remove a small temporary file at the destination — appears here with the backup feature. Read access and write access are separate results.",
            status: "Not in this build",
            compact: true
        )
        disclosure(into: content, id: "backup-options", label: "Options", body: card(optionsBody))

        content.addArrangedSubview(
            note(
                "Storage and backups belong to a workspace. These controls are the finished shape of the pane; the backup feature that fills them is not installed in this build, so there is nothing here to switch off or repair.",
                symbol: "internaldrive"
            )
        )
    }

    // MARK: Capture

    /// Capture (design `core-intake-panes.js` → `capture()`): the standing
    /// defaults every Add or Watch is shown against, and the limits in force.
    /// Every row here is a statement of fact rather than a control — in the
    /// design too, the defaults are chosen per item at capture time, not set
    /// on this page. Core does not report them to this window in this build.
    private func renderCapture(_ content: NSStackView) {
        let defaults = card(content, header: "Defaults")
        notBuiltRow(
            into: defaults,
            title: "Destination",
            detail: "Shown before every Add or Watch. Core doesn’t add anything without it.",
            status: "Not reported",
            tone: .neutral
        )
        notBuiltRow(
            into: defaults,
            title: "New files",
            detail: "Keep updated is chosen per file when you add it.",
            status: "Not reported",
            tone: .neutral
        )
        notBuiltRow(
            into: defaults,
            title: "Folders",
            detail: "Include subfolders is on by default and shown in every scope review. Watching starts with Include existing files.",
            status: "Not reported",
            tone: .neutral
        )
        notBuiltRow(
            into: defaults,
            title: "Cloud files",
            detail: "Each file or folder has its own Download cloud files automatically setting. This page doesn’t override them.",
            status: "Not reported",
            tone: .neutral
        )

        // The limits are the design's verbatim table. They are constants of the
        // capture contract rather than reported state, so they are shown plainly.
        let limits = card(content, header: "Limits in effect")
        let table: [(String, String)] = [
            ("Text, Markdown, pasted text", "1 MiB per item, valid UTF-8"),
            ("PDF, Office, CSV/TSV, images", "50 MiB per item"),
            ("RTF, local HTML", "20 MiB per item"),
            ("Manual addition", "10 items, 200 MiB"),
            ("Waiting on this Mac", "512 MiB or 100 items"),
            ("Watched folders", "20"),
            ("Kept-updated files", "1,000"),
            ("Folder run or watch", "10,000 eligible files"),
        ]
        for (name, value) in table {
            row(
                into: limits,
                title: name,
                detail: nil,
                trailing: OvFont.label(value, size: 11.5, color: Ov.textSecondary, mono: true)
            )
        }
        content.addArrangedSubview(
            note("Workspace policy can be stricter. Notifications aren’t needed to capture documents.")
        )
    }

    // MARK: Document processing

    /// Document processing (design `core-intake-panes.js` → `docproc()`): how
    /// text is read from captured files, and which language resources are
    /// installed. The language rows carry a full install lifecycle in the
    /// design — download, verify, install, storage-full — all of which belongs
    /// to a capture backend this build does not have.
    private func renderDocumentProcessing(_ content: NSStackView) {
        let recognition = card(content, header: "Text recognition")
        notBuiltRow(
            into: recognition,
            title: "Profile",
            detail: "Reads native text and recognises text on every page and image, on this Mac. The alternative, Text only, is faster but doesn’t recognise images of text, including scanned pages.",
            status: "Not in this build"
        )
        notBuiltRow(
            into: recognition,
            title: "Recognise text in embedded document images",
            detail: "Word, PowerPoint, Excel, RTF and local HTML. Only images inside the captured file; linked images are never loaded.",
            status: "Not in this build"
        )

        // Language packs. The design installs up to three; the sizes below are
        // the design's own figures and are shown as the catalogue, not as a
        // claim about what is on this Mac.
        let languages = card(content, header: "Languages")
        let packs: [(String, String)] = [
            ("English", "18 MiB"),
            ("French", "48 MiB"),
            ("German", "41 MiB"),
            ("Spanish", "39 MiB"),
            ("Japanese", "Not available for this version"),
        ]
        for (name, size) in packs {
            notBuiltRow(
                into: languages,
                title: name,
                detail: size,
                status: "Not in this build"
            )
        }
        content.addArrangedSubview(
            note(
                "Choose up to three. Language resources are downloaded, verified and installed by the capture feature, which is not installed in this build — so nothing here can be selected or downloaded yet.",
            )
        )
    }

    // MARK: Processing

    /// Processing (design `core-panes.js` → `processing()`, plus the Local
    /// Decisions group from `core-decisions.js` → `section()`).
    private func renderProcessing(_ content: NSStackView) {
        let automatic = card(content, header: "Automatic processing")
        notBuiltRow(
            into: automatic,
            title: "Process changes automatically",
            detail: "Process updates from sources you have allowed.",
            status: "Not in this build"
        )
        notBuiltRow(
            into: automatic,
            title: "Sources",
            detail: "Sources are approved in OmniVia. Per-source access status and recovery appear beside each source once that feature exists.",
            status: "None configured",
            tone: .neutral
        )
        notBuiltRow(
            into: automatic,
            title: "Resource use",
            detail: "How much of this Mac Core may use while processing.",
            status: "Not in this build"
        )
        notBuiltRow(
            into: automatic,
            title: "Pause heavy processing on battery",
            detail: "Light updates continue; extraction and indexing wait for power.",
            status: "Not in this build"
        )

        let activity = card(content, header: "Activity")
        notBuiltRow(
            into: activity,
            title: "Processing state unknown",
            detail: "Core does not report processing activity to this window in this build.",
            status: "Not reported",
            tone: .neutral
        )

        // ---- Local Decisions (LD-01 overview) ----
        // The honest state is "no decision engine in this build". The design's
        // not-installed state offers "Install model…", which this deliberately
        // does not: §A5 says to hide an unimplemented control rather than
        // falsely offer installation, and there is no runtime to install into.
        let decisionsLede = NSTextField(
            wrappingLabelWithString: "Suggest categories and follow-ups using information you choose."
        )
        decisionsLede.font = OvFont.sans(12)
        decisionsLede.textColor = Ov.textSecondary
        content.addArrangedSubview(OvFont.groupHeader("Local Decisions"))
        content.addArrangedSubview(ovSpacer(6))
        content.addArrangedSubview(decisionsLede)
        content.addArrangedSubview(ovSpacer(8))

        let decisions = card(content, header: nil)
        notBuiltRow(
            into: decisions,
            title: "Local Decisions",
            detail: "Makes routine judgements locally using information Core is already allowed to access. The decision engine is not part of this build, so there is no model to install and nothing to turn on.",
            status: "Not in this build"
        )
        content.addArrangedSubview(
            note("Recommendations stay advisory. A prediction does not itself approve a change or authorise an action.")
        )
        content.addArrangedSubview(ovSpacer(18))

        // ---- Processing details ----
        let detailsBody = NSStackView()
        detailsBody.orientation = .vertical
        detailsBody.alignment = .leading
        detailsBody.spacing = 0
        for dependency in ["Search index", "Text extraction", "Semantic embeddings"] {
            notBuiltRow(
                into: detailsBody,
                title: dependency,
                detail: nil,
                status: "Not reported",
                compact: true
            )
        }
        disclosure(
            into: content, id: "proc-details", label: "Processing details",
            body: card(detailsBody)
        )
        content.addArrangedSubview(
            note(
                "Everything runs on this Mac. No workspace content is sent to an external service.",
                symbol: "checkmark.shield"
            )
        )
    }

    // MARK: Access

    /// Access (design `core-panes.js` → `access()`): which applications may use
    /// this workspace, and how this Mac is reachable. The design puts "Add
    /// application…" in the pane header; with no grant store behind it that
    /// would be a disabled button floating beside the title, so it is stated
    /// as a row instead.
    private func renderAccess(_ content: NSStackView) {
        let apps = card(content, header: "Connected applications")
        notBuiltRow(
            into: apps,
            title: "No configured applications",
            detail: "Applications that may use this workspace are listed here with their access level. Per-application access is granted by a store this build does not have.",
            status: "None configured",
            tone: .neutral
        )
        notBuiltRow(
            into: apps,
            title: "Add application…",
            detail: "New applications start as Read only and are raised to Read and contribute explicitly.",
            status: "Not in this build"
        )
        content.addArrangedSubview(
            note("Configured access is not a live connection. macOS permissions and Core’s own workspace authority stay separate facts.")
        )
        content.addArrangedSubview(ovSpacer(18))

        let connectionBody = NSStackView()
        connectionBody.orientation = .vertical
        connectionBody.alignment = .leading
        connectionBody.spacing = 0
        notBuiltRow(
            into: connectionBody,
            title: "Transport",
            detail: "How applications reach Core on this Mac.",
            status: "Not reported",
            tone: .neutral,
            compact: true
        )
        notBuiltRow(
            into: connectionBody,
            title: "Reachable from",
            detail: nil,
            status: "Not reported",
            tone: .neutral,
            compact: true
        )
        notBuiltRow(
            into: connectionBody,
            title: "Socket",
            detail: "Core does not report its socket path to this window in this build.",
            status: "Not reported",
            tone: .neutral,
            compact: true
        )
        disclosure(
            into: content, id: "conn-details", label: "Connection details",
            body: card(connectionBody)
        )
        content.addArrangedSubview(
            note(
                "Connections are local to this Mac. Core is not exposed to other devices or the internet.",
                symbol: "checkmark.shield"
            )
        )
    }

    // MARK: Maintenance

    /// Maintenance (design `core-panes.js` → `maintenance()`): Core's health,
    /// diagnostics, and the index rebuild. Lifecycle lives in the menu, which
    /// owns it — this pane states that rather than offering a second set of
    /// start/stop controls (MR-005: the settings window invokes no lifecycle
    /// command).
    private func renderMaintenance(_ content: NSStackView) {
        let core = card(content, header: "Core")
        notBuiltRow(
            into: core,
            title: "Service",
            detail: "Runs on this Mac. Closing this window does not stop it. Start, stop and restart live in the Core menu, which owns the lifecycle; this window never invokes it.",
            status: "Shown in the menu",
            tone: .neutral
        )
        notBuiltRow(
            into: core,
            title: "Core version",
            detail: nil,
            status: "Not reported",
            tone: .neutral
        )
        row(
            into: core,
            title: "Companion version",
            detail: "This settings window and the menu bar item.",
            trailing: OvFont.label(
                companionVersion, size: 11.5, color: Ov.textSecondary, mono: true
            )
        )
        notBuiltRow(
            into: core,
            title: "Updates",
            detail: "Installed with the OmniVia installer. No updater is available here.",
            status: "Not in this build"
        )

        let diagnostics = card(content, header: "Diagnostics")
        notBuiltRow(
            into: diagnostics,
            title: "Logs",
            detail: "Recent Core and companion events on this Mac. Workspace content is never logged.",
            status: "Not in this build"
        )
        notBuiltRow(
            into: diagnostics,
            title: "Diagnostics bundle",
            detail: "Collect a bundle to share with support. You review what it includes first.",
            status: "Not in this build"
        )
        notBuiltRow(
            into: diagnostics,
            title: "Share to Core extension",
            detail: "Installed and verified in a supported Share menu are different states; neither is claimed here.",
            status: "Not in this build"
        )

        let optionsBody = NSStackView()
        optionsBody.orientation = .vertical
        optionsBody.alignment = .leading
        optionsBody.spacing = 0
        notBuiltRow(
            into: optionsBody,
            title: "Search indexes",
            detail: "Rebuilds the search and embedding indexes from workspace records. Records themselves are not changed or deleted.",
            status: "Not in this build",
            compact: true
        )
        disclosure(
            into: content, id: "maint-options", label: "Maintenance options",
            body: card(optionsBody)
        )

        renderMacCheckDetails(content)
    }

    /// The companion's own version, which is a fact this process actually has.
    private var companionVersion: String {
        let info = Bundle.main.infoDictionary
        let short = info?["CFBundleShortVersionString"] as? String
        let build = info?["CFBundleVersion"] as? String
        switch (short, build) {
        case let (version?, build?): return "\(version) (\(build))"
        case let (version?, nil): return version
        default: return "Not reported"
        }
    }

    private func renderMacCheckDetails(_ content: NSStackView) {
        let detailsCard = card(content, header: "macOS check details")
        let snapshot = coordinator.snapshot
        let observed = snapshot.observedAt.map { ISO8601DateFormatter().string(from: $0) } ?? "never"
        let details = """
        Generation: \(snapshot.generation)
        Installation: \(snapshot.context.installationRevision)
        Workspace: \(snapshot.context.workspaceRef)
        Observed: \(observed)
        """
        let text = NSTextField(wrappingLabelWithString: details)
        text.font = OvFont.mono(11)
        text.textColor = Ov.textSecondary
        let logBox = NSStackView()
        logBox.orientation = .vertical
        logBox.alignment = .leading
        logBox.edgeInsets = NSEdgeInsets(top: 10, left: 12, bottom: 10, right: 12)
        logBox.wantsLayer = true
        logBox.layer?.backgroundColor = Ov.bgInsetDeep.cgColor
        logBox.layer?.cornerRadius = 7
        logBox.addArrangedSubview(text)
        detailsCard.addArrangedSubview(logBox)
        detailsCard.addArrangedSubview(ovSpacer(8))
        detailsCard.addArrangedSubview(
            button("Refresh status", style: .bordered, action: #selector(refreshStatus))
        )

        // Diagnostics are bounded and safe by construction (§14.2): the fields
        // above are the whole export — no paths, no bookmarks, no secrets.
        let note = NSTextField(wrappingLabelWithString: "These details are what diagnostics export contains. Nothing else is recorded.")
        note.font = OvFont.sans(11.5)
        note.textColor = Ov.textTertiary
        detailsCard.addArrangedSubview(note)
    }

    // MARK: Helpers

    private func currentPreferences() -> ReadinessPreferences {
        ReadinessPreferences(
            startCoreAtLoginSelected: startCoreAtLoginSelected,
            attentionNotificationsSelected: attentionNotificationsSelected,
            hasWorkspace: hasWorkspace
        )
    }

    // MARK: - Explicit actions (§11)

    /// Opening or closing a disclosure is pure view state: it re-renders the
    /// pane but never triggers a refresh, a probe or a service call (§A2 —
    /// changing panes must have no side effects).
    @objc private func toggleDisclosure(_ sender: NSButton) {
        guard let id = (sender as? DisclosureButton)?.disclosureID else { return }
        if openDisclosures.contains(id) {
            openDisclosures.remove(id)
        } else {
            openDisclosures.insert(id)
        }
        render()
    }

    /// The attention preference is this window's own, and is machine-local:
    /// it belongs to the companion, not to a portable workspace, and must stay
    /// settable while Core is stopped. Restores the key the deleted web host
    /// persisted, so an existing preference is picked up rather than reset.
    @objc private func toggleAttentionNotifications(_ sender: NSSwitch) {
        attentionNotificationsSelected = sender.state == .on
        UserDefaults.standard.set(
            attentionNotificationsSelected, forKey: Self.attentionNotificationsKey
        )
        coordinator.scheduleRefresh(.explicitRefresh)
    }

    static let attentionNotificationsKey = "omnivia.core.settings.attentionNotifications"
    static let appearanceKey = "omnivia.core.settings.appearance"

    /// System / Light / Dark, as the design's Appearance control offers.
    enum AppearancePreference: String, CaseIterable {
        case system, light, dark

        var nsAppearance: NSAppearance? {
            switch self {
            case .system: return nil   // follow the Mac
            case .light: return NSAppearance(named: .aqua)
            case .dark: return NSAppearance(named: .darkAqua)
            }
        }

        var label: String { rawValue.prefix(1).uppercased() + rawValue.dropFirst() }
    }

    static var appearancePreference: AppearancePreference {
        get {
            UserDefaults.standard.string(forKey: appearanceKey)
                .flatMap(AppearancePreference.init(rawValue:)) ?? .system
        }
        set { UserDefaults.standard.set(newValue.rawValue, forKey: appearanceKey) }
    }

    @objc private func changeAppearance(_ sender: NSSegmentedControl) {
        let choice = AppearancePreference.allCases[sender.selectedSegment]
        Self.appearancePreference = choice
        window?.appearance = choice.nsAppearance
        rebuildForAppearance()
    }

    /// Layer-backed colours are snapshots, so a theme change cannot simply
    /// invalidate — the chrome, the source list and the pane are rebuilt
    /// against the new appearance.
    private func rebuildForAppearance() {
        guard let window else { return }
        window.contentView = buildContent(appearance: Self.targetAppearance)
        renderSidebar()
        render()
        positionWindowControls()
    }

    @objc private func refreshStatus() {
        coordinator.scheduleRefresh(.explicitRefresh)
    }

    @objc private func openLoginItems() { openDestination(.loginItems) }

    /// One route for every System Settings destination, so the alert's Try
    /// again can re-run exactly what failed.
    private func openDestination(_ destination: SettingsDestination) {
        Task { @MainActor in
            let (result, route) = await navigator.open(destination)
            presentNavigation(result, route: route, destination: destination)
        }
    }

    @objc private func openNotifications() { openDestination(.notifications) }

    /// Explicit notification request (§9.2, §11.2): only from this trusted
    /// button, only once per pending interaction; the passive provider re-reads
    /// the sender's settings afterwards — the request result itself is never
    /// treated as a permission grant.
    @objc private func requestNotifications() {
        guard let center = notificationCenter else { return }
        Task { @MainActor in
            let settings = await center.notificationSettings()
            if settings.authorizationStatus == .notDetermined {
                _ = try? await center.requestAuthorization(options: [.alert, .sound])
            }
            // Re-read through the passive provider; the reducer reflects denial
            // or limited delivery honestly (MT-014/MT-015).
            coordinator.scheduleRefresh(.explicitRefresh)
        }
    }

    private func presentNavigation(_ result: NavigationResult, route: String?, destination: SettingsDestination) {
        guard let window else { return }
        let alert = NSAlert()
        switch result {
        case .navigationRequested, .genericSettingsOpened:
            alert.messageText = "System Settings opened"
            // Navigation requested — never "access enabled" (§10.4).
            alert.informativeText = (route ?? navigator.manualRoute(for: destination) ?? "")
                + "\n\nStatus re-reads when you come back to this window."
        case .launchFailed, .cancelled, .unsupportedDestination:
            alert.messageText = "System Settings could not be opened"
            alert.informativeText = (route ?? navigator.manualRoute(for: destination) ?? "")
                + "\n\nNothing has changed."
            // §A7 asks for the manual route *with* Try again. The copy named a
            // button that was never added, so it pointed at nothing.
            alert.addButton(withTitle: "Try again")
            alert.addButton(withTitle: "Cancel")
        }
        alert.alertStyle = result == .launchFailed ? .warning : .informational
        alert.beginSheetModal(for: window) { [weak self] response in
            guard response == .alertFirstButtonReturn else { return }
            switch result {
            case .launchFailed, .cancelled, .unsupportedDestination:
                // Retrying opens System Settings again. A failed launch changed
                // no permission, so this re-attempts the navigation and nothing
                // else (§A8 — the result must not change because we retried).
                self?.openDestination(destination)
            default: break
            }
        }
    }
}

// MARK: - The OmniVia brand mark (assets/omnivia-wordmark.svg geometry:
// rounded brick tile, rx 34/120, with two white concentric rings)

final class MarkView: NSView {
    var tile = NSColor(srgbRed: 0x9E / 255.0, green: 0x30 / 255.0, blue: 0x2A / 255.0, alpha: 1)
        { didSet { needsDisplay = true } }

    override func draw(_ dirtyRect: NSRect) {
        guard let context = NSGraphicsContext.current?.cgContext else { return }
        // viewBox geometry: tile 120×120 (rx 34); ring paths on the 80pt circle
        // set (outer ring r 37.8→31.2, inner ring r 23.7→18.3 around centre).
        let size = min(bounds.width, bounds.height)
        context.setFillColor(tile.cgColor)
        let radius = size * 34.0 / 120.0
        let path = NSBezierPath(roundedRect: bounds, xRadius: radius, yRadius: radius)
        path.fill()

        let centre = CGPoint(x: bounds.midX, y: bounds.midY)
        // Ring geometry from the wordmark SVG, normalised to the 120 tile.
        // Outer ring: outer r 37.8, inner 31.2 → annulus centred on tile.
        // Inner ring: outer r 23.7, inner 18.3.
        let rings: [(outer: CGFloat, inner: CGFloat)] = [
            (37.8 / 120.0, 31.2 / 120.0),
            (23.7 / 120.0, 18.3 / 120.0),
        ]
        context.setFillColor(NSColor.white.cgColor)
        for ring in rings {
            let outer = ring.outer * size
            let inner = ring.inner * size
            let outerPath = CGPath(ellipseIn: CGRect(
                x: centre.x - outer, y: centre.y - outer, width: outer * 2, height: outer * 2
            ), transform: nil)
            let innerPath = CGPath(ellipseIn: CGRect(
                x: centre.x - inner, y: centre.y - inner, width: inner * 2, height: inner * 2
            ), transform: nil)
            // Annulus: even-odd fill of the outer ellipse minus the inner one.
            context.addPath(outerPath)
            context.addPath(innerPath)
            context.fillPath(using: .evenOdd)
        }
    }
}

// MARK: - Sidebar row (prototype source-list item: hover + selected pill)

final class SidebarRowView: NSView {
    var isSelected = false { didSet { applySelection() } }
    var onSelect: (() -> Void)?

    private let label: NSTextField
    private let icon: NSImageView

    init(title: String, symbol: String) {
        label = OvFont.label(title, size: 13, color: Ov.textPrimary)
        icon = NSImageView()
        // A 16×16 glyph box (`#sw-nav .nav-row svg`); the symbol is sized 15 so
        // wide glyphs fit it without being scaled down.
        icon.image = NSImage(systemSymbolName: symbol, accessibilityDescription: title)?
            .withSymbolConfiguration(.init(pointSize: 15, weight: .regular))
        icon.imageScaling = .scaleProportionallyDown
        icon.contentTintColor = Ov.textSecondary
        super.init(frame: .zero)
        wantsLayer = true
        layer?.cornerRadius = 6

        let row = NSStackView()
        row.orientation = .horizontal
        row.alignment = .centerY
        row.spacing = 8
        row.edgeInsets = NSEdgeInsets(top: 0, left: 8, bottom: 0, right: 8)
        row.translatesAutoresizingMaskIntoConstraints = false
        addSubview(row)
        heightAnchor.constraint(equalToConstant: 28).isActive = true
        NSLayoutConstraint.activate([
            row.topAnchor.constraint(equalTo: topAnchor),
            row.bottomAnchor.constraint(equalTo: bottomAnchor),
            row.leadingAnchor.constraint(equalTo: leadingAnchor),
            row.trailingAnchor.constraint(equalTo: trailingAnchor),
        ])
        row.addArrangedSubview(icon)
        row.addArrangedSubview(label)
        // A fixed glyph box. SF Symbols have per-glyph widths — a wrench is
        // not as wide as a shield — so without one the label's left edge moved
        // from row to row and the list read as ragged.
        icon.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            icon.widthAnchor.constraint(equalToConstant: 16),
            icon.heightAnchor.constraint(equalToConstant: 16),
        ])

        addGestureRecognizer(NSClickGestureRecognizer(target: self, action: #selector(clicked)))

        // A bare NSView carries no role and no press action, so the sidebar
        // reached assistive technology as seven unrelated labels with nothing
        // to activate, and the keyboard could not get to it at all. The design
        // gives this list arrow-key navigation (`#sw-nav` keydown) and a
        // current-page marker; both are accessibility requirements (§A22), not
        // polish.
        setAccessibilityElement(true)
        setAccessibilityRole(.button)
        setAccessibilityLabel(title)
        // The brick ring is drawn in `draw(_:)`; leaving the system ring on
        // would stack a blue one over it.
        focusRingType = .none
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    override func accessibilityPerformPress() -> Bool {
        onSelect?()
        return true
    }

    // MARK: Keyboard

    override var acceptsFirstResponder: Bool { true }
    override var canBecomeKeyView: Bool { true }

    /// Whether the most recent interaction anywhere in the source list came
    /// from the keyboard. This is `:focus-visible`: the design rings a row
    /// focused by Tab or an arrow key, and not one that merely holds first
    /// responder — which, at window open, the first row always does. Ringing
    /// on plain first responder put a permanent border around General.
    private static var focusCameFromKeyboard = false

    private var showsFocusRing = false {
        didSet { if oldValue != showsFocusRing { applySelection() } }
    }

    /// The system ring is blue; the design's is brick
    /// (`--ov-focus-ring-shadow`). Drawn rather than tinted, because AppKit
    /// gives no colour control over the system ring — so the system one is
    /// turned off and an equivalent is painted in its place, same 3pt spread
    /// and same row radius, rather than simply removed.
    override func becomeFirstResponder() -> Bool {
        showsFocusRing = Self.focusCameFromKeyboard
        return super.becomeFirstResponder()
    }

    override func resignFirstResponder() -> Bool {
        showsFocusRing = false
        return super.resignFirstResponder()
    }

    override func keyDown(with event: NSEvent) {
        Self.focusCameFromKeyboard = true
        showsFocusRing = true
        switch event.keyCode {
        case 36, 49: onSelect?()                       // Return, Space
        case 125: moveSelection?(1)                    // Down
        case 126: moveSelection?(-1)                   // Up
        case 115: moveSelection?(.min)                 // Home
        case 119: moveSelection?(.max)                 // End
        default: super.keyDown(with: event)
        }
    }

    /// Set by the window: step the selection by `delta`, or jump to the first
    /// or last pane for `.min` / `.max`.
    var moveSelection: ((Int) -> Void)?

    /// Dark mode does not fill the selected row with the solid accent — that
    /// is the light-mode treatment. It uses the accent at 28% with white text
    /// and an accent-coloured icon, so the row reads as selected without
    /// turning a 28px strip into a block of brick
    /// (`[data-theme="dark"] #sw-nav .nav-row[aria-current="page"]`).
    private func applySelection() {
        // Light fills with the solid accent; dark uses the 28% wash. See
        // `Ov.selectionBg` — a solid brick block is too heavy on a dark row
        // and a 28% wash is too faint on a light one.
        layer?.backgroundColor = isSelected
            ? resolved(Ov.selectionBg) : NSColor.clear.cgColor
        // The ring is the layer's border, not a stroke in `draw(_:)`. Drawn
        // there it never reached the screen: this view is layer-backed with a
        // background colour, and the layer's own fill is what gets composited.
        // A border paints above that fill and cannot be lost the same way.
        layer?.borderWidth = showsFocusRing ? 3 : 0
        layer?.borderColor = showsFocusRing ? resolved(Ov.focusRing) : nil
        label.textColor = isSelected ? Ov.selectionText : Ov.textPrimary
        label.font = OvFont.sans(13, isSelected ? 500 : 400)
        // On the solid light fill the glyph must be white to stay legible;
        // on the dark wash the design tints it with the accent itself.
        icon.contentTintColor = isSelected
            ? (Ov.selectionIsSolid ? Ov.selectionText : Ov.accent)
            : Ov.textSecondary
    }

    override func layout() {
        super.layout()
        applySelection()
    }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        addTrackingArea(NSTrackingArea(
            rect: bounds,
            options: [.mouseEnteredAndExited, .activeInKeyWindow],
            owner: self
        ))
    }

    override func mouseEntered(with event: NSEvent) {
        if !isSelected { layer?.backgroundColor = resolved(Ov.hoverBg) }
        icon.contentTintColor = isSelected
            ? (Ov.selectionIsSolid ? Ov.selectionText : Ov.accent)
            : Ov.textPrimary
    }

    override func mouseExited(with event: NSEvent) { applySelection() }

    override func mouseDown(with event: NSEvent) {
        Self.focusCameFromKeyboard = false
        showsFocusRing = false
        if !isSelected { layer?.backgroundColor = resolved(Ov.activeBg) }
        super.mouseDown(with: event)
    }

    override func mouseUp(with event: NSEvent) {
        super.mouseUp(with: event)
        applySelection()
    }

    @objc private func clicked() { onSelect?() }
}

// MARK: - OmniVia buttons (.ov-btn / .ov-btn--bordered / --primary)

/// The branded header stands in for the system titlebar, so it has to drag the
/// window like one. The real titlebar is underneath — this view sits over it
/// because the window uses `.fullSizeContentView` — and a view that swallows
/// the click without acting on it leaves the window stuck.
///
/// `performDrag(with:)` hands the gesture to the window server, which gives the
/// genuine article: snapping, Spaces, and Stage Manager. Controls are untouched,
/// since an NSButton consumes its own `mouseDown` and this is never reached —
/// the same split the design draws with `-webkit-app-region: drag` on the bar
/// and `no-drag` on the controls inside it.
final class DraggableSheenView: SheenView {
    override func mouseDown(with event: NSEvent) {
        window?.performDrag(with: event)
    }

    /// The wordmark and the mark must not eat the drag. An `NSTextField`
    /// answers `hitTest` with itself even when it is a plain label, so a
    /// press on "OmniVia" would land on the label and the window would not
    /// move. Anything in the bar that is not a live control is treated as the
    /// bar itself; real controls keep their clicks.
    override func hitTest(_ point: NSPoint) -> NSView? {
        guard let hit = super.hitTest(point) else { return nil }
        if let control = hit as? NSControl, control.isEnabled, control.action != nil {
            return control
        }
        return self
    }

    /// Double-clicking a titlebar zooms or minimises, per the user's setting in
    /// System Settings → Desktop & Dock. A custom titlebar that ignores this
    /// feels broken in a way that is hard to name.
    override func mouseUp(with event: NSEvent) {
        guard event.clickCount == 2 else {
            super.mouseUp(with: event)
            return
        }
        let action = UserDefaults.standard
            .string(forKey: "AppleActionOnDoubleClick") ?? "Maximize"
        switch action {
        case "Minimize": window?.miniaturize(nil)
        case "None": break
        default: window?.zoom(nil)
        }
    }
}

/// A scroll view's document view must be flipped for its origin to be the
/// top-left. Unflipped, content stacks from the bottom and "scroll to the top"
/// has no single meaning.
final class FlippedView: NSView {
    override var isFlipped: Bool { true }
}

/// A borderless summary row that remembers which disclosure it drives, so one
/// action selector can serve every disclosure in the window.
final class DisclosureButton: NSButton {
    let disclosureID: String

    init(id: String) {
        self.disclosureID = id
        super.init(frame: .zero)
        title = ""
        isBordered = false
        setButtonType(.momentaryChange)
        // Brick, like the source list — not the system blue, which was the one
        // place in the window still ringing in the OS accent.
        focusRingType = .none
        wantsLayer = true
        layer?.cornerRadius = 5
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("init(coder:) is not used") }

    override var intrinsicContentSize: NSSize { NSSize(width: NSView.noIntrinsicMetric, height: 22) }

    override func becomeFirstResponder() -> Bool {
        layer?.borderWidth = 3
        layer?.borderColor = Ov.focusRing.cgColor
        return super.becomeFirstResponder()
    }

    override func resignFirstResponder() -> Bool {
        layer?.borderWidth = 0
        return super.resignFirstResponder()
    }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        trackingAreas.forEach(removeTrackingArea)
        addTrackingArea(NSTrackingArea(
            rect: bounds, options: [.mouseEnteredAndExited, .activeInKeyWindow], owner: self
        ))
    }

    override func mouseEntered(with event: NSEvent) {
        layer?.backgroundColor = Ov.hoverBg.cgColor
    }

    override func mouseExited(with event: NSEvent) {
        layer?.backgroundColor = NSColor.clear.cgColor
    }
}

enum OvButtonStyle { case bordered, primary }

final class OvButton: NSButton {
    private let style: OvButtonStyle

    init(title: String, style: OvButtonStyle, size: OvButtonSize = .row) {
        self.style = style
        self.size = size
        super.init(frame: .zero)
        self.title = title
        isBordered = false
        setButtonType(.momentaryPushIn)
        wantsLayer = true
        layer?.cornerRadius = 6
        // .cs-fix-a/.sw-row-c .ov-btn: height 26, 12.5px, 0 11 padding.
        font = OvFont.sans(size == .row ? 12.5 : 13, 500)
        contentTintColor = style == .primary ? Ov.onAccent : Ov.textPrimary
        applyStyle()

        let height: CGFloat = size == .row ? 26 : 28
        let padding: CGFloat = size == .row ? 11 : 12
        heightAnchor.constraint(equalToConstant: height).isActive = true
        widthAnchor.constraint(greaterThanOrEqualToConstant: textWidth + padding * 2).isActive = true
    }

    enum OvButtonSize { case row, md }
    private let size: OvButtonSize

    required init?(coder: NSCoder) { fatalError("not used") }

    private var textWidth: CGFloat {
        let attributes: [NSAttributedString.Key: Any] = [.font: font ?? NSFont.systemFont(ofSize: 12.5)]
        return (title as NSString).size(withAttributes: attributes).width
    }

    private func applyStyle() {
        if style == .primary {
            layer?.backgroundColor = Ov.accent.cgColor
            layer?.borderWidth = 0
        } else {
            layer?.backgroundColor = Ov.bgElevated.cgColor
            layer?.borderWidth = 1
            layer?.borderColor = Ov.borderControl.cgColor
        }
    }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        guard window != nil, trackingAreas.isEmpty else { return }
        addTrackingArea(NSTrackingArea(rect: bounds, options: [.mouseEnteredAndExited, .activeInKeyWindow], owner: self))
    }

    override func mouseEntered(with event: NSEvent) {
        layer?.backgroundColor = (style == .primary ? Ov.accentActive : Ov.hoverBg).cgColor
    }

    override func mouseExited(with event: NSEvent) { applyStyle() }
}

// MARK: - Small layout helpers

private func ovSpacer(_ height: CGFloat) -> NSView {
    let view = NSView()
    view.translatesAutoresizingMaskIntoConstraints = false
    view.heightAnchor.constraint(equalToConstant: height).isActive = true
    return view
}
