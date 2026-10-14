// ============================================================================
// "Add application" — the design's `U.sheet` as a window sheet.
//
// The design draws a modal anchored to the window (`.ai-scrim` + `.ai-sheet`):
// a 520px card with a titled header and close button, a scrolling body, and a
// right-aligned footer. On AppKit that *is* a window sheet, which brings the
// scrim, the modality and Escape with it, so this builds the three bands and
// lets `beginSheet` do the rest.
//
// Where it departs from the design, and why. The prototype adds arbitrary
// applications: you type a name, pick a type, pick a level. This installation
// has no such registry — `mcp configure` takes one of the hosts the CLI knows
// (`claude-code`, `codex`), and the name and type follow from that host. So
// the design's free-text "Application name" becomes a chooser over those
// hosts, and "Application type" becomes the value it is rather than a select
// whose second option this build cannot honour. A text field whose contents
// were discarded would claim a capability that is not there.
//
// The access radios are the design's, wording included.
// ============================================================================

import AppKit

@MainActor
final class AddApplicationSheet: NSObject {
    /// One host offered in the chooser.
    struct Choice {
        let host: String
        let displayName: String
    }

    private let choices: [Choice]
    private let workspace: String
    /// Called with the chosen host and the `mcp configure --profile` value.
    private let onSubmit: (String, String) -> Void

    private let sheet = NSWindow(
        contentRect: NSRect(x: 0, y: 0, width: 520, height: 100),
        styleMask: [.titled, .fullSizeContentView],
        backing: .buffered,
        defer: true
    )
    private var body = NSStackView()
    private var foot = NSStackView()
    private var root: NSView?
    /// The appearance every subview is built under. Layer colours are
    /// snapshots: one taken outside this is the app's theme, not the sheet's,
    /// and a body swapped in later would come back in the wrong one.
    private var appearance = NSAppearance.currentDrawing()
    private var lastSnippet: String?
    private var hostChooser: NSPopUpButton?
    private var profile = "restricted"
    private(set) var radios: [OvRadio] = []

    init(
        choices: [Choice],
        workspace: String,
        onSubmit: @escaping (String, String) -> Void
    ) {
        self.choices = choices
        self.workspace = workspace
        self.onSubmit = onSubmit
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
        // The design autofocuses the first field.
        sheet.initialFirstResponder = hostChooser
    }

    func close() {
        sheet.sheetParent?.endSheet(sheet)
    }

    // MARK: The three bands

    /// Internal, not private: the tests build the form without a window.
    func buildContent() -> NSView {
        let root = NSStackView()
        root.orientation = .vertical
        root.alignment = .leading
        root.spacing = 0
        root.wantsLayer = true
        root.layer?.backgroundColor = Ov.bgElevated.cgColor
        root.widthAnchor.constraint(equalToConstant: 520).isActive = true
        self.root = root

        root.addArrangedSubview(
            header(
                title: "Add application",
                sub: "Grant an application access to \(workspace). "
                    + "You’ll get connection instructions after it is added."
            )
        )
        root.addArrangedSubview(separator())

        // `.ai-sheet-b`: padding 16px 18px.
        body.orientation = .vertical
        body.alignment = .leading
        body.spacing = 14
        body.edgeInsets = NSEdgeInsets(top: 16, left: 18, bottom: 16, right: 18)
        buildForm()
        root.addArrangedSubview(body)
        body.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true

        root.addArrangedSubview(separator())

        // `.ai-sheet-f`: right-aligned, padding 13px 18px, its own surface.
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

        setFoot(
            [
                ovButton("Cancel", style: .plain, action: #selector(cancel)),
                ovButton("Add application", style: .primary, action: #selector(submit)),
            ]
        )
        return root
    }

    /// `.ai-sheet-h`: title, subtitle, and a close control.
    private func header(title: String, sub: String) -> NSView {
        let host = NSView()
        let text = NSStackView()
        text.orientation = .vertical
        text.alignment = .leading
        text.spacing = 4
        let heading = NSTextField(labelWithString: title)
        heading.font = OvFont.sans(15, 650)
        heading.textColor = Ov.textPrimary
        let subtitle = NSTextField(wrappingLabelWithString: sub)
        subtitle.font = OvFont.sans(12.5)
        subtitle.textColor = Ov.textSecondary
        text.addArrangedSubview(heading)
        text.addArrangedSubview(subtitle)
        text.translatesAutoresizingMaskIntoConstraints = false
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
            close.leadingAnchor.constraint(equalTo: text.trailingAnchor, constant: 12),
            close.trailingAnchor.constraint(equalTo: host.trailingAnchor, constant: -16),
            close.widthAnchor.constraint(equalToConstant: 22),
            close.heightAnchor.constraint(equalToConstant: 22),
        ])
        return host
    }

    // MARK: The form

    private func buildForm() {
        // `.ai-field`: Application — the design's free-text name, as the
        // chooser this installation can actually honour.
        let chooser = NSPopUpButton(frame: .zero, pullsDown: false)
        for choice in choices {
            let item = NSMenuItem(title: choice.displayName, action: nil, keyEquivalent: "")
            // The borderless popup takes its drawn title from the menu item,
            // so the font and colour have to live there.
            item.attributedTitle = NSAttributedString(
                string: choice.displayName,
                attributes: [.font: OvFont.sans(13), .foregroundColor: Ov.textPrimary]
            )
            chooser.menu?.addItem(item)
        }
        chooser.setAccessibilityLabel("Application")
        hostChooser = chooser
        body.addArrangedSubview(
            field(
                "Application",
                control: Self.selectBox(chooser),
                help: "Applications this installation knows how to configure. "
                    + "Each one gets its own credential for this workspace."
            )
        )

        // Application type. One value, because `mcp configure` only ever
        // writes an MCP client configuration — shown rather than offered.
        let kind = NSTextField(labelWithString: "MCP client")
        kind.font = OvFont.sans(13)
        kind.textColor = Ov.textPrimary
        body.addArrangedSubview(
            field(
                "Application type",
                control: kind,
                help: "This build configures MCP clients. Command-line tools are not in it.",
                helpAttention: true
            )
        )

        body.addArrangedSubview(
            field(
                "Access",
                control: accessRadios(),
                help: nil
            )
        )
    }

    /// `.cs-radio`: a radio, a bold title and a line of description, with a
    /// hairline between the pair. Wording is the design's.
    private func accessRadios() -> NSView {
        let group = NSStackView()
        group.orientation = .vertical
        group.alignment = .leading
        group.spacing = 0

        let options = [
            (
                "restricted", "Read only",
                "Can search and retrieve information it is allowed to access."
            ),
            (
                "authoring", "Read and contribute",
                "Can also submit evidence and proposed memory. It cannot approve "
                    + "its own contributions as trusted knowledge."
            ),
        ]
        for (index, option) in options.enumerated() {
            let (value, title, detail) = option
            let line = NSStackView()
            line.orientation = .horizontal
            line.alignment = .top
            line.spacing = 10
            line.edgeInsets = NSEdgeInsets(top: 9, left: 0, bottom: 9, right: 0)

            let radio = OvRadio()
            radio.identifier = NSUserInterfaceItemIdentifier(value)
            radio.isOn = value == profile
            radio.setAccessibilityLabel(title)
            radios.append(radio)

            let text = NSStackView()
            text.orientation = .vertical
            text.alignment = .leading
            text.spacing = 2
            let name = NSTextField(labelWithString: title)
            name.font = OvFont.sans(13, 600)
            name.textColor = Ov.textPrimary
            let note = NSTextField(wrappingLabelWithString: detail)
            note.font = OvFont.sans(11.5)
            note.textColor = Ov.textSecondary
            note.preferredMaxLayoutWidth = 440
            text.addArrangedSubview(name)
            text.addArrangedSubview(note)

            line.addArrangedSubview(radio)
            line.addArrangedSubview(text)
            // `.cs-radio` is a <label>: the whole row picks the option.
            let hit = RowClick { [weak self] in self?.pickLevel(radio) }
            hit.translatesAutoresizingMaskIntoConstraints = false
            line.addSubview(hit, positioned: .above, relativeTo: nil)
            NSLayoutConstraint.activate([
                hit.topAnchor.constraint(equalTo: line.topAnchor),
                hit.bottomAnchor.constraint(equalTo: line.bottomAnchor),
                hit.leadingAnchor.constraint(equalTo: line.leadingAnchor),
                hit.trailingAnchor.constraint(equalTo: line.trailingAnchor),
            ])
            group.addArrangedSubview(line)
            line.widthAnchor.constraint(equalTo: group.widthAnchor).isActive = true
            // `.cs-radio:last-child{border-bottom:none}`
            if index < options.count - 1 { group.addArrangedSubview(separator()) }
        }
        return group
    }

    /// `.ai-field`: a 12px/590 label, the control, and optional help under it.
    private func field(
        _ label: String, control: NSView, help: String?, helpAttention: Bool = false
    ) -> NSView {
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 5
        let caption = NSTextField(labelWithString: label)
        caption.font = OvFont.sans(12, 590)
        caption.textColor = Ov.textSecondary
        stack.addArrangedSubview(caption)
        stack.addArrangedSubview(control)
        if let help {
            let hint = NSTextField(wrappingLabelWithString: help)
            hint.font = OvFont.sans(11.5)
            // Amber marks the part of the design this build does not have,
            // the same flag the panes use for an unbuilt control.
            hint.textColor = helpAttention ? Ov.warning : Ov.textTertiary
            hint.preferredMaxLayoutWidth = 484
            stack.addArrangedSubview(hint)
        }
        return stack
    }

    // MARK: Body swaps (`U.sheetUpdate`)

    /// The pending state: the form is replaced, so the choice cannot be
    /// changed under a write that is already running.
    func showWorking(_ message: String) {
        draw {
            self.setBody([self.paragraph(message)])
            self.setFoot([])
        }
    }

    private func draw(_ body: () -> Void) {
        appearance.performAsCurrentDrawingAppearance(body)
    }

    /// The design's added state: what happened, the thing to paste, and a
    /// note. `mcp configure` writes this installation's own configuration but
    /// leaves the host's file alone, so the snippet is not a nicety — it is
    /// the remaining step, and saying "it will just connect" would be false.
    func showAdded(name: String, level: String, snippet: String) {
        lastSnippet = snippet
        draw {
            self.setBody([
                self.paragraph(
                    "\(name) was added with \(level) access. Add this to its own configuration:"
                ),
                self.codeBlock(snippet),
                self.paragraph(
                    "Connections are local to this Mac. The snippet carries no credential — "
                        + "that is held by this installation, not by the application.",
                    secondary: true
                ),
            ])
            self.setFoot([
                self.ovButton("Copy", style: .bordered, action: #selector(Self.copySnippet)),
                self.ovButton("Done", style: .primary, action: #selector(Self.cancel)),
            ])
        }
    }

    /// `.ai-code`: mono 11px/1.6, padding 10/12, radius 7, inset-deep.
    private func codeBlock(_ text: String) -> NSView {
        let label = NSTextField(wrappingLabelWithString: text)
        label.font = NSFont.monospacedSystemFont(ofSize: 11, weight: .regular)
        label.textColor = Ov.textSecondary
        label.isSelectable = true
        label.preferredMaxLayoutWidth = 460
        label.translatesAutoresizingMaskIntoConstraints = false

        let box = NSView()
        box.wantsLayer = true
        box.layer?.backgroundColor = Ov.bgInsetDeep.cgColor
        box.layer?.cornerRadius = 7
        box.addSubview(label)
        NSLayoutConstraint.activate([
            label.topAnchor.constraint(equalTo: box.topAnchor, constant: 10),
            label.bottomAnchor.constraint(equalTo: box.bottomAnchor, constant: -10),
            label.leadingAnchor.constraint(equalTo: box.leadingAnchor, constant: 12),
            label.trailingAnchor.constraint(equalTo: box.trailingAnchor, constant: -12),
            box.widthAnchor.constraint(equalToConstant: 484),
        ])
        return box
    }

    func showFailure(_ message: String) {
        draw {
            self.setBody([self.paragraph(message)])
            self.setFoot([self.ovButton("Close", style: .plain, action: #selector(Self.cancel))])
        }
    }

    private func paragraph(_ text: String, secondary: Bool = false) -> NSView {
        let label = NSTextField(wrappingLabelWithString: text)
        label.font = OvFont.sans(12.5)
        label.textColor = secondary ? Ov.textSecondary : Ov.textPrimary
        label.preferredMaxLayoutWidth = 484
        return label
    }

    private func setBody(_ views: [NSView]) {
        body.views.forEach { body.removeView($0) }
        views.forEach { body.addArrangedSubview($0) }
        resize()
    }

    private func setFoot(_ views: [NSView]) {
        foot.views.forEach { foot.removeView($0) }
        views.forEach { foot.addArrangedSubview($0) }
        // An empty footer still holds the band's height, so the sheet does not
        // jump when the buttons go away during the write.
        if views.isEmpty { foot.addArrangedSubview(ovSpacer(2)) }
        resize()
    }

    /// A sheet does not follow its content's fitting size on its own, so a
    /// body swap would otherwise leave the window at the form's height with
    /// the footer hanging off the bottom.
    private func resize() {
        guard let root, sheet.sheetParent != nil else { return }
        root.layoutSubtreeIfNeeded()
        sheet.setContentSize(root.fittingSize)
    }

    private func ovButton(_ title: String, style: OvButtonStyle, action: Selector) -> NSButton {
        let button = OvButton(title: title, style: style, size: .md)
        button.target = self
        button.action = action
        return button
    }

    /// Layer colours here are plain `.cgColor` on purpose: everything is
    /// built inside `present`'s drawing-appearance block, where that resolves
    /// against the sheet's appearance. `resolved()` would read the view's own
    /// `effectiveAppearance`, which before it has a window is the app's.
    private func separator() -> NSView {
        let rule = NSView()
        rule.wantsLayer = true
        rule.layer?.backgroundColor = Ov.separator.cgColor
        rule.translatesAutoresizingMaskIntoConstraints = false
        rule.heightAnchor.constraint(equalToConstant: 1).isActive = true
        return rule
    }

    // MARK: Actions

    func pickLevel(_ sender: OvRadio) {
        profile = sender.identifier?.rawValue ?? "restricted"
        radios.forEach { $0.isOn = $0 === sender }
    }

    /// `.ai-select`: an inset, bordered box with the set's own chevron. The
    /// stock popup draws a blue system chevron that nothing else in this
    /// window has.
    private static func selectBox(_ popup: NSPopUpButton) -> NSView {
        popup.isBordered = false
        // Otherwise the cell keeps drawing its own arrows beside the title,
        // next to the chevron this box supplies.
        (popup.cell as? NSPopUpButtonCell)?.arrowPosition = .noArrow
        popup.wantsLayer = true
        popup.translatesAutoresizingMaskIntoConstraints = false

        let box = NSView()
        box.wantsLayer = true
        box.layer?.backgroundColor = Ov.bgInset.cgColor
        box.layer?.cornerRadius = 6
        box.layer?.borderWidth = 1
        box.layer?.borderColor = Ov.borderControl.cgColor
        box.addSubview(popup)

        let chevron = NSImageView()
        chevron.image = OvIcon.chevronDown.image(size: 13)
        chevron.contentTintColor = Ov.textTertiary
        chevron.translatesAutoresizingMaskIntoConstraints = false
        box.addSubview(chevron)

        NSLayoutConstraint.activate([
            box.heightAnchor.constraint(equalToConstant: 30),
            box.widthAnchor.constraint(equalToConstant: 484),
            popup.leadingAnchor.constraint(equalTo: box.leadingAnchor, constant: 6),
            popup.trailingAnchor.constraint(equalTo: chevron.leadingAnchor, constant: -6),
            popup.centerYAnchor.constraint(equalTo: box.centerYAnchor),
            chevron.trailingAnchor.constraint(equalTo: box.trailingAnchor, constant: -9),
            chevron.centerYAnchor.constraint(equalTo: box.centerYAnchor),
        ])
        return box
    }

    @objc private func cancel() { close() }

    @objc private func copySnippet() {
        guard let lastSnippet else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(lastSnippet, forType: .string)
    }

    /// Selecting in the chooser, for the tests — on screen the popup does it.
    func chooseApplication(at index: Int) { hostChooser?.selectItem(at: index) }

    @objc func submit() {
        let index = hostChooser?.indexOfSelectedItem ?? 0
        guard choices.indices.contains(index) else { return }
        onSubmit(choices[index].host, profile)
    }
}


/// `.cs-radio input{accent-color:var(--ov-accent)}`. AppKit's radio takes the
/// system accent, which is the one colour in this sheet the design does not
/// choose, so the control is drawn here instead.
final class OvRadio: NSView {
    var isOn = false { didSet { needsDisplay = true } }

    // 14pt circle with the design's `margin:2px 0 0`, and room under it: a
    // 14pt-tall box clipped the stroke's bottom edge.
    override var intrinsicContentSize: NSSize { NSSize(width: 14, height: 17) }
    override var isFlipped: Bool { true }

    override func draw(_ dirtyRect: NSRect) {
        let circle = NSBezierPath(ovalIn: NSRect(x: 0.5, y: 2.5, width: 13, height: 13))
        if isOn {
            Ov.accent.setFill()
            circle.fill()
            NSColor.white.setFill()
            NSBezierPath(ovalIn: NSRect(x: 5, y: 7, width: 4, height: 4)).fill()
        } else {
            Ov.bgElevated.setFill()
            circle.fill()
            Ov.borderControl.setStroke()
            circle.lineWidth = 1
            circle.stroke()
        }
    }

    override func accessibilityRole() -> NSAccessibility.Role? { .radioButton }
    override func accessibilityValue() -> Any? { isOn }
    override func isAccessibilityElement() -> Bool { true }
}

/// A transparent click target laid over a whole radio row, so the row behaves
/// like the design's `<label>` wrapper.
final class RowClick: NSView {
    private let onClick: () -> Void
    init(_ onClick: @escaping () -> Void) {
        self.onClick = onClick
        super.init(frame: .zero)
    }
    required init?(coder: NSCoder) { fatalError("not used") }
    override func mouseDown(with event: NSEvent) { onClick() }
}
