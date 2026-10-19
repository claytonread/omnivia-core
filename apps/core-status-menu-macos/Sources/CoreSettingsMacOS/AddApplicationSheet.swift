// ============================================================================
// "Add application" — `ACT["acc-add"]`, on the shared `OvSheet` shell.
//
// Where it departs from the design, and why. The prototype adds arbitrary
// applications: you type a name, pick a type, pick a level. This installation
// has no such registry — `mcp configure` takes one of the hosts the CLI knows
// (`claude-code`, `codex`), and the name and type follow from that host. So
// the design's free-text "Application name" is a chooser over those hosts,
// and "Application type" states the one value it can be rather than offering
// a second this build cannot honour. A text field whose contents were
// discarded would claim a capability that is not there.
//
// The access radios are the design's, wording included.
//
// The added state carries the snippet, because `mcp configure` writes this
// installation's own protected configuration but deliberately leaves the
// host's file alone — "the file is theirs" — so the snippet is the step still
// to be done, not a nicety.
// ============================================================================

import AppKit

@MainActor
final class AddApplicationSheet: OvSheet {
    struct Choice {
        let host: String
        let displayName: String
    }

    private let choices: [Choice]
    /// Called with the chosen host and the `mcp configure --profile` value.
    private let onSubmit: (String, String) -> Void

    private var hostChooser: NSPopUpButton?
    private var profile = "restricted"
    private(set) var radios: [OvRadio] = []
    private var lastSnippet: String?

    init(
        choices: [Choice],
        workspace: String,
        onSubmit: @escaping (String, String) -> Void
    ) {
        self.choices = choices
        self.onSubmit = onSubmit
        super.init(
            title: "Add application",
            subtitle: "Grant an application access to \(workspace). "
                + "You’ll get connection instructions after it is added."
        )
    }

    override var initialResponder: NSView? { hostChooser }

    // MARK: The form

    override func makeBody() -> [NSView] {
        let chooser = NSPopUpButton(frame: .zero, pullsDown: false)
        for choice in choices {
            let item = NSMenuItem(title: choice.displayName, action: nil, keyEquivalent: "")
            // The borderless popup draws its title from the menu item, so the
            // font and colour have to live there.
            item.attributedTitle = NSAttributedString(
                string: choice.displayName,
                attributes: [.font: OvFont.sans(13), .foregroundColor: Ov.textPrimary]
            )
            chooser.menu?.addItem(item)
        }
        chooser.setAccessibilityLabel("Application")
        hostChooser = chooser

        let kind = NSTextField(labelWithString: "MCP client")
        kind.font = OvFont.sans(13)
        kind.textColor = Ov.textPrimary

        return [
            field(
                "Application",
                control: Self.selectBox(chooser),
                help: "Applications this installation knows how to configure. "
                    + "Each one gets its own credential for this workspace."
            ),
            field(
                "Application type",
                control: kind,
                help: "This build configures MCP clients. Command-line tools are not in it.",
                helpAttention: true
            ),
            field("Access", control: accessRadios(), help: nil),
        ]
    }

    override func makeFoot() -> [NSView] {
        [
            ovButton("Cancel", style: .plain, action: #selector(cancel)),
            ovButton("Add application", style: .primary, action: #selector(submit)),
        ]
    }

    /// `.cs-radio`: a radio, a bold title and a line of description, with a
    /// hairline between the pair. Wording is the design's.
    private func accessRadios() -> NSView {
        let group = NSStackView()
        group.orientation = .vertical
        group.alignment = .leading
        group.spacing = 0

        for (index, option) in AccessLevel.all.enumerated() {
            let line = NSStackView()
            line.orientation = .horizontal
            line.alignment = .top
            line.spacing = 10
            line.edgeInsets = NSEdgeInsets(top: 9, left: 0, bottom: 9, right: 0)

            let radio = OvRadio()
            radio.identifier = NSUserInterfaceItemIdentifier(option.profile)
            radio.isOn = option.profile == profile
            radio.setAccessibilityLabel(option.name)
            radios.append(radio)

            let text = NSStackView()
            text.orientation = .vertical
            text.alignment = .leading
            text.spacing = 2
            let name = NSTextField(labelWithString: option.name)
            name.font = OvFont.sans(13, 600)
            name.textColor = Ov.textPrimary
            let note = NSTextField(wrappingLabelWithString: option.explanation)
            note.font = OvFont.sans(11.5)
            note.textColor = Ov.textSecondary
            note.preferredMaxLayoutWidth = Self.contentWidth - 44
            text.addArrangedSubview(name)
            text.addArrangedSubview(note)

            line.addArrangedSubview(radio)
            line.addArrangedSubview(text)
            group.addArrangedSubview(line)
            line.widthAnchor.constraint(equalTo: group.widthAnchor).isActive = true

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
            if index < AccessLevel.all.count - 1 { group.addArrangedSubview(separator()) }
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
            // Amber marks the part of the design this build has not got, the
            // same flag the panes use for an unbuilt control.
            hint.textColor = helpAttention ? Ov.warning : Ov.textTertiary
            hint.preferredMaxLayoutWidth = Self.contentWidth
            stack.addArrangedSubview(hint)
        }
        return stack
    }

    /// `.ai-select`: an inset, bordered box with the set's own chevron. The
    /// stock popup draws a blue system chevron that nothing else here has.
    static func selectBox(
        _ popup: NSPopUpButton,
        width: CGFloat = OvSheet.contentWidth,
        height: CGFloat = 30
    ) -> NSView {
        popup.isBordered = false
        // Otherwise the cell keeps drawing its own arrows beside the title,
        // next to the chevron this box supplies.
        (popup.cell as? NSPopUpButtonCell)?.arrowPosition = .noArrow
        popup.wantsLayer = true
        popup.translatesAutoresizingMaskIntoConstraints = false

        let box = NSView()
        box.wantsLayer = true
        // `.ai-select` is inset; the row-sized `.cs-select` sits on the
        // elevated surface instead, so it reads as a control on a card.
        box.layer?.backgroundColor = (height < 30 ? Ov.bgElevated : Ov.bgInset).cgColor
        box.layer?.cornerRadius = 6
        box.layer?.borderWidth = 1
        box.layer?.borderColor = Ov.borderControl.cgColor
        box.addSubview(popup)

        // Pass-through: a plain NSImageView swallows the click, so the
        // chevron — the obvious place to press — did nothing.
        let chevron = PassThroughImageView()
        chevron.image = OvIcon.chevronDown.image(size: 13)
        chevron.contentTintColor = Ov.textTertiary
        chevron.translatesAutoresizingMaskIntoConstraints = false
        box.addSubview(chevron)

        NSLayoutConstraint.activate([
            box.heightAnchor.constraint(equalToConstant: height),
            box.widthAnchor.constraint(equalToConstant: width),
            popup.leadingAnchor.constraint(equalTo: box.leadingAnchor, constant: height < 30 ? 4 : 6),
            popup.trailingAnchor.constraint(equalTo: box.trailingAnchor),
            popup.topAnchor.constraint(equalTo: box.topAnchor),
            popup.bottomAnchor.constraint(equalTo: box.bottomAnchor),
            chevron.trailingAnchor.constraint(equalTo: box.trailingAnchor, constant: -9),
            chevron.centerYAnchor.constraint(equalTo: box.centerYAnchor),
        ])
        return box
    }

    // MARK: Outcomes

    func showWorking(_ message: String) {
        draw {
            self.setBody([self.paragraph(message)])
            self.setFoot([])
        }
    }

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

    func showFailure(_ message: String) {
        draw {
            self.setBody([self.paragraph(message)])
            self.setFoot([self.ovButton("Close", style: .plain, action: #selector(Self.cancel))])
        }
    }

    /// `.ai-code`: mono 11px/1.6, padding 10/12, radius 7, inset-deep.
    private func codeBlock(_ text: String) -> NSView {
        let label = NSTextField(wrappingLabelWithString: text)
        label.font = NSFont.monospacedSystemFont(ofSize: 11, weight: .regular)
        label.textColor = Ov.textSecondary
        label.isSelectable = true
        label.preferredMaxLayoutWidth = Self.contentWidth - 24
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
            box.widthAnchor.constraint(equalToConstant: Self.contentWidth),
        ])
        return box
    }

    // MARK: Actions

    func pickLevel(_ sender: OvRadio) {
        profile = sender.identifier?.rawValue ?? "restricted"
        radios.forEach { $0.isOn = $0 === sender }
    }

    /// Selecting in the chooser, for the tests — on screen the popup does it.
    func chooseApplication(at index: Int) { hostChooser?.selectItem(at: index) }

    @objc private func copySnippet() {
        guard let lastSnippet else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(lastSnippet, forType: .string)
    }

    @objc func submit() {
        let index = hostChooser?.indexOfSelectedItem ?? 0
        guard choices.indices.contains(index) else { return }
        onSubmit(choices[index].host, profile)
    }
}

/// The two access levels, named once. `authoring` carries the
/// `workspace_contributor` role and eleven tools against restricted's six.
struct AccessLevel {
    let profile: String
    let name: String
    let explanation: String

    static let readOnly = AccessLevel(
        profile: "restricted",
        name: "Read only",
        explanation: "Can search and retrieve information it is allowed to access."
    )
    static let contribute = AccessLevel(
        profile: "authoring",
        name: "Read and contribute",
        explanation: "Can also submit evidence and proposed memory. It cannot approve "
            + "its own contributions as trusted knowledge."
    )
    static let all = [readOnly, contribute]

    static func named(_ profile: String) -> AccessLevel {
        profile == "authoring" ? contribute : readOnly
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

/// A transparent click target over a whole radio row, so the row behaves like
/// the design's `<label>` wrapper.
final class RowClick: NSView {
    private let onClick: () -> Void
    init(_ onClick: @escaping () -> Void) {
        self.onClick = onClick
        super.init(frame: .zero)
    }
    required init?(coder: NSCoder) { fatalError("not used") }
    override func mouseDown(with event: NSEvent) { onClick() }
}


/// An image that is decoration: it never takes the click, so a glyph laid
/// over a control does not stop the control working.
final class PassThroughImageView: NSImageView {
    override func hitTest(_ point: NSPoint) -> NSView? { nil }
}
