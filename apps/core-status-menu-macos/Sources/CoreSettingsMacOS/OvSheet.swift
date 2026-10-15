// ============================================================================
// `U.sheet` — the design's one modal shape, as a window sheet.
//
// `.ai-scrim` + `.ai-sheet`: a 520pt card with a titled header and close
// control, a body, and a right-aligned footer. On AppKit that *is* a window
// sheet, which brings the scrim, the modality and Escape with it, so this
// builds the three bands and lets `beginSheet` do the rest.
//
// Every sheet in the design is this shape with a different body and footer —
// add an application, change its access level, revoke it — so the shape lives
// here once and each use supplies its own contents.
//
// Layer colours are snapshots, so everything is built inside `draw`, against
// the appearance the sheet was presented with. A body swapped in later
// without it comes back in the app's theme rather than the window's: that is
// how a light-mode sheet ended up with a black code block.
// ============================================================================

import AppKit

@MainActor
class OvSheet: NSObject {
    private let title: String
    private let subtitle: String

    private let sheet = NSWindow(
        contentRect: NSRect(x: 0, y: 0, width: 520, height: 100),
        styleMask: [.titled, .fullSizeContentView],
        backing: .buffered,
        defer: true
    )
    private var root: NSView?
    private var body = NSStackView()
    private var foot = NSStackView()
    private var appearance = NSAppearance.currentDrawing()

    /// `.ai-sheet-b` inner width: 520 less the band's 18pt gutters.
    /// `nonisolated` so it can be a default argument, which is evaluated in the
    /// caller's context rather than this class's.
    nonisolated static let contentWidth: CGFloat = 484

    init(title: String, subtitle: String) {
        self.title = title
        self.subtitle = subtitle
        super.init()
    }

    // MARK: Presenting

    func present(in parent: NSWindow, appearance: NSAppearance) {
        self.appearance = appearance
        sheet.appearance = appearance
        sheet.titlebarAppearsTransparent = true
        sheet.titleVisibility = .hidden
        sheet.isMovableByWindowBackground = false
        draw { self.sheet.contentView = self.buildContent() }
        parent.beginSheet(sheet)
        sheet.initialFirstResponder = initialResponder
    }

    func close() { sheet.sheetParent?.endSheet(sheet) }

    /// Overridden by a sheet with a form, so the first field takes focus.
    var initialResponder: NSView? { nil }

    /// The sheet's starting body and footer.
    func makeBody() -> [NSView] { [] }
    func makeFoot() -> [NSView] { [] }

    // MARK: The three bands

    private func buildContent() -> NSView {
        let root = NSStackView()
        root.orientation = .vertical
        root.alignment = .leading
        root.spacing = 0
        root.wantsLayer = true
        root.layer?.backgroundColor = Ov.bgElevated.cgColor
        root.widthAnchor.constraint(equalToConstant: 520).isActive = true
        self.root = root

        root.addArrangedSubview(header())
        root.addArrangedSubview(separator())

        body.orientation = .vertical
        body.alignment = .leading
        body.spacing = 14
        body.edgeInsets = NSEdgeInsets(top: 16, left: 18, bottom: 16, right: 18)
        makeBody().forEach { body.addArrangedSubview($0) }
        root.addArrangedSubview(body)
        body.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true

        root.addArrangedSubview(separator())

        // `.ai-sheet-f`: right-aligned, padding 13/18, its own surface.
        let footHost = NSView()
        footHost.wantsLayer = true
        footHost.layer?.backgroundColor = Ov.bgContentSecondary.cgColor
        foot.orientation = .horizontal
        foot.alignment = .centerY
        foot.spacing = 8
        foot.edgeInsets = NSEdgeInsets(top: 13, left: 18, bottom: 13, right: 18)
        foot.translatesAutoresizingMaskIntoConstraints = false
        footHost.addSubview(foot)
        NSLayoutConstraint.activate([
            foot.topAnchor.constraint(equalTo: footHost.topAnchor),
            foot.bottomAnchor.constraint(equalTo: footHost.bottomAnchor),
            foot.trailingAnchor.constraint(equalTo: footHost.trailingAnchor),
            foot.leadingAnchor.constraint(greaterThanOrEqualTo: footHost.leadingAnchor),
        ])
        root.addArrangedSubview(footHost)
        footHost.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true
        makeFoot().forEach { foot.addArrangedSubview($0) }
        return root
    }

    /// `.ai-sheet-h`: title, subtitle, and a close control.
    private func header() -> NSView {
        let host = NSView()
        let text = NSStackView()
        text.orientation = .vertical
        text.alignment = .leading
        text.spacing = 4
        let heading = NSTextField(labelWithString: title)
        heading.font = OvFont.sans(15, 650)
        heading.textColor = Ov.textPrimary
        text.addArrangedSubview(heading)
        if !subtitle.isEmpty {
            let sub = NSTextField(wrappingLabelWithString: subtitle)
            sub.font = OvFont.sans(12.5)
            sub.textColor = Ov.textSecondary
            text.addArrangedSubview(sub)
        }
        text.translatesAutoresizingMaskIntoConstraints = false
        text.setContentHuggingPriority(.init(1), for: .horizontal)
        host.addSubview(text)

        let close = NSButton()
        close.isBordered = false
        close.bezelStyle = .regularSquare
        close.image = OvIcon.x.image(size: 15)
        close.contentTintColor = Ov.textTertiary
        close.title = ""
        close.target = self
        close.action = #selector(cancel)
        close.setAccessibilityLabel("Close")
        close.translatesAutoresizingMaskIntoConstraints = false
        host.addSubview(close)

        NSLayoutConstraint.activate([
            text.topAnchor.constraint(equalTo: host.topAnchor, constant: 18),
            text.leadingAnchor.constraint(equalTo: host.leadingAnchor, constant: 18),
            text.bottomAnchor.constraint(equalTo: host.bottomAnchor, constant: -14),
            close.topAnchor.constraint(equalTo: host.topAnchor, constant: 16),
            // Pinned to the right edge, not to the end of the title: an
            // equality here dragged the close control into the middle of the
            // header whenever the subtitle was short.
            close.leadingAnchor.constraint(
                greaterThanOrEqualTo: text.trailingAnchor, constant: 12
            ),
            close.trailingAnchor.constraint(equalTo: host.trailingAnchor, constant: -16),
            close.widthAnchor.constraint(equalToConstant: 22),
            close.heightAnchor.constraint(equalToConstant: 22),
        ])
        return host
    }

    // MARK: Swapping contents (`U.sheetUpdate`)

    func setBody(_ views: [NSView]) {
        body.views.forEach { body.removeView($0) }
        views.forEach { body.addArrangedSubview($0) }
        resize()
    }

    func setFoot(_ views: [NSView]) {
        foot.views.forEach { foot.removeView($0) }
        views.forEach { foot.addArrangedSubview($0) }
        // An empty footer keeps the band's height, so the sheet does not jump
        // while a write is running.
        if views.isEmpty { foot.addArrangedSubview(ovSpacer(2)) }
        resize()
    }

    func draw(_ work: () -> Void) {
        appearance.performAsCurrentDrawingAppearance(work)
    }

    /// A sheet does not follow its content's fitting size on its own, so a
    /// body swap would leave the window at the old height with the footer
    /// hanging off the bottom.
    private func resize() {
        guard let root, sheet.sheetParent != nil else { return }
        root.layoutSubtreeIfNeeded()
        sheet.setContentSize(root.fittingSize)
    }

    // MARK: Shared pieces

    func paragraph(_ text: String, secondary: Bool = false) -> NSView {
        let label = NSTextField(wrappingLabelWithString: text)
        label.font = OvFont.sans(12.5)
        label.textColor = secondary ? Ov.textSecondary : Ov.textPrimary
        label.preferredMaxLayoutWidth = Self.contentWidth
        return label
    }

    /// `.cs-check`: a tick or a cross, a line, and an optional quieter
    /// qualifier under it. Used by the promote sheet to say what a level
    /// does and, as pointedly, what it does not.
    func checkLine(_ text: String, included: Bool, qualifier: String? = nil) -> NSView {
        let line = NSStackView()
        line.orientation = .horizontal
        line.alignment = .top
        line.spacing = 9
        line.edgeInsets = NSEdgeInsets(top: 6, left: 0, bottom: 6, right: 0)

        let icon = NSImageView()
        icon.image = (included ? OvIcon.check : OvIcon.x).image(size: 14)
        icon.contentTintColor = included ? Ov.success : Ov.textTertiary
        icon.translatesAutoresizingMaskIntoConstraints = false
        icon.widthAnchor.constraint(equalToConstant: 14).isActive = true
        icon.heightAnchor.constraint(equalToConstant: 14).isActive = true

        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 2
        let label = NSTextField(wrappingLabelWithString: text)
        label.font = OvFont.sans(12.5)
        label.textColor = Ov.textPrimary
        label.preferredMaxLayoutWidth = Self.contentWidth - 23
        stack.addArrangedSubview(label)
        if let qualifier {
            let q = NSTextField(wrappingLabelWithString: qualifier)
            q.font = OvFont.sans(11.5)
            q.textColor = Ov.textTertiary
            q.preferredMaxLayoutWidth = Self.contentWidth - 23
            stack.addArrangedSubview(q)
        }
        line.addArrangedSubview(icon)
        line.addArrangedSubview(stack)
        return line
    }

    /// `U.note`: an info glyph and a quieter line.
    func note(_ text: String) -> NSView {
        let box = NSStackView()
        box.orientation = .horizontal
        box.alignment = .firstBaseline
        box.spacing = 8
        let icon = NSImageView()
        icon.image = OvIcon.info.image(size: 13)
        icon.contentTintColor = Ov.textTertiary
        icon.translatesAutoresizingMaskIntoConstraints = false
        icon.widthAnchor.constraint(equalToConstant: 13).isActive = true
        let label = NSTextField(wrappingLabelWithString: text)
        label.font = OvFont.sans(12)
        label.textColor = Ov.textSecondary
        label.preferredMaxLayoutWidth = Self.contentWidth - 21
        box.addArrangedSubview(icon)
        box.addArrangedSubview(label)
        return box
    }

    func ovButton(_ title: String, style: OvButtonStyle, action: Selector) -> NSButton {
        let button = OvButton(title: title, style: style, size: .md)
        button.target = self
        button.action = action
        return button
    }

    func separator() -> NSView {
        let rule = NSView()
        rule.wantsLayer = true
        rule.layer?.backgroundColor = Ov.separator.cgColor
        rule.translatesAutoresizingMaskIntoConstraints = false
        rule.heightAnchor.constraint(equalToConstant: 1).isActive = true
        return rule
    }

    @objc func cancel() { close() }
}

/// `cancelFoot`: every confirming sheet in the design is one plain Cancel and
/// one primary action.
@MainActor
final class ConfirmSheet: OvSheet {
    private let bodyText: String
    private let checks: [(String, Bool, String?)]
    private let confirmTitle: String
    private let noteText: String
    private let onConfirm: () -> Void
    /// Run when the sheet is dismissed without confirming. A level change
    /// proposes through a control, so the caller has to put that control back
    /// — a picker left showing a level the service was never asked for is the
    /// misreport §A6 rules out.
    private let onCancel: (() -> Void)?

    init(
        title: String,
        subtitle: String,
        body: String = "",
        checks: [(String, Bool, String?)] = [],
        note: String = "",
        confirm: String,
        onCancel: (() -> Void)? = nil,
        onConfirm: @escaping () -> Void
    ) {
        self.bodyText = body
        self.checks = checks
        self.noteText = note
        self.confirmTitle = confirm
        self.onCancel = onCancel
        self.onConfirm = onConfirm
        super.init(title: title, subtitle: subtitle)
    }

    override func cancel() {
        onCancel?()
        super.cancel()
    }

    override func makeBody() -> [NSView] {
        var views: [NSView] = []
        if !bodyText.isEmpty { views.append(paragraph(bodyText)) }
        views.append(contentsOf: checks.map { checkLine($0.0, included: $0.1, qualifier: $0.2) })
        if !noteText.isEmpty { views.append(note(noteText)) }
        return views
    }

    override func makeFoot() -> [NSView] {
        [
            ovButton("Cancel", style: .plain, action: #selector(cancel)),
            ovButton(confirmTitle, style: .primary, action: #selector(confirm)),
        ]
    }

    /// The caller decides what the sheet becomes next: most show a pending
    /// body against the write, so this closes nothing by itself.
    @objc private func confirm() { onConfirm() }

    func showWorking(_ message: String) {
        draw {
            self.setBody([self.paragraph(message)])
            self.setFoot([])
        }
    }

    func showFailure(_ message: String) {
        draw {
            self.setBody([self.paragraph(message)])
            self.setFoot([self.ovButton("Close", style: .plain, action: #selector(Self.cancel))])
        }
    }
}
