import Foundation
import UniformTypeIdentifiers

/// How one shared logical item is captured.
///
/// Every case is a captured copy. There is deliberately no original case: a
/// Share provider hands over a representation whose lifetime ends with the
/// host, and a provider file URL — in place or temporary — is never durable
/// original authority. So `loadInPlaceFileRepresentation` is never called, no
/// bookmark is ever taken from a shared item, and nothing a share delivers is
/// monitored. Linking an original is a separate, explicit action that happens
/// in the companion's own reviewed chooser.
public enum ShareSnapshotKind: String, Codable, Sendable {
    /// A file or data representation, copied before the provider expires.
    case file
    /// An image representation, copied as its own bytes.
    case image
    /// Plain text, written as UTF-8 bytes exactly as handed over.
    case text
    /// An inert `http`/`https` link, written as its own UTF-8 text. The link is
    /// never opened, resolved or fetched: the capture is the text of the link.
    case link

    /// Lower wins when one logical item offers several representations. A real
    /// file beats bytes-in-memory, which beat a link, which beats the title
    /// text a host sends beside it.
    var captureRank: Int {
        switch self {
        case .file: 0
        case .image: 1
        case .link: 2
        case .text: 3
        }
    }
}

public struct ShareRepresentation: Equatable, Sendable {
    public let typeIdentifier: String
    public let kind: ShareSnapshotKind

    public init(typeIdentifier: String, kind: ShareSnapshotKind) {
        self.typeIdentifier = typeIdentifier
        self.kind = kind
    }
}

/// One chosen representation, and where in the host's input it came from.
public struct ShareSlotChoice: Equatable, Sendable {
    public let itemIndex: Int
    public let attachmentIndex: Int
    public let representation: ShareRepresentation

    public init(itemIndex: Int, attachmentIndex: Int, representation: ShareRepresentation) {
        self.itemIndex = itemIndex
        self.attachmentIndex = attachmentIndex
        self.representation = representation
    }
}

/// Picks the one representation each shared logical item is captured from.
public enum ShareRepresentationPolicy {
    /// A link is captured as text, so its length is bounded like a short note.
    public static let maximumLinkBytes = 2_048

    /// The single representation to capture one provider from, or nil when this
    /// build can capture nothing it offers.
    ///
    /// A provider's registered identifiers are deduplicated and consulted in
    /// one fixed order, so the same provider always yields the same choice.
    public static func choose(from registeredTypeIdentifiers: [String]) -> ShareRepresentation? {
        let identifiers = deduplicated(registeredTypeIdentifiers)
        // A file URL first: it is the only representation that names the bytes
        // the person actually shared, and it is still copied, never bookmarked.
        if let identifier = identifiers.first(where: { $0 == UTType.fileURL.identifier }) {
            return ShareRepresentation(typeIdentifier: identifier, kind: .file)
        }
        if let identifier = identifiers.first(where: { conforms($0, to: .image) }) {
            return ShareRepresentation(typeIdentifier: identifier, kind: .image)
        }
        if let identifier = identifiers.first(where: { conforms($0, to: .url) }) {
            return ShareRepresentation(typeIdentifier: identifier, kind: .link)
        }
        // Only plain text, never `public.text` at large: rich text and HTML
        // carry markup that is not the text of a note, so they are copied as
        // the documents they are by the data case below.
        if let identifier = identifiers.first(where: { conforms($0, to: .plainText) }) {
            return ShareRepresentation(typeIdentifier: identifier, kind: .text)
        }
        if let identifier = identifiers.first(where: { conforms($0, to: .data) }) {
            return ShareRepresentation(typeIdentifier: identifier, kind: .file)
        }
        return nil
    }

    /// One representation per logical slot, in the order the host supplied it.
    ///
    /// `items[i][j]` is the registered type identifiers of attachment `j` of
    /// extension item `i`. Within one item only the attachments of that item's
    /// single best kind are kept, which collapses a host that sends one logical
    /// share as alternative representations — a link and its title text — to
    /// one slot, while keeping a multi-file share as one slot per file.
    ///
    /// Returns nil when any item offers nothing this build can capture: a
    /// partly captured selection is not what the person shared, so the whole
    /// selection is refused rather than narrowed.
    public static func slots(forItems items: [[[String]]]) -> [ShareSlotChoice]? {
        var slots: [ShareSlotChoice] = []
        for (itemIndex, attachments) in items.enumerated() {
            let classified = attachments.enumerated().compactMap { index, identifiers in
                choose(from: identifiers).map { (index, $0) }
            }
            guard let best = classified.map({ $0.1.kind.captureRank }).min() else { return nil }
            for (attachmentIndex, representation) in classified
            where representation.kind.captureRank == best {
                slots.append(ShareSlotChoice(
                    itemIndex: itemIndex,
                    attachmentIndex: attachmentIndex,
                    representation: representation
                ))
            }
        }
        return slots.isEmpty ? nil : slots
    }

    /// True for a link this build will capture as inert text: `http` or
    /// `https`, with a host, short enough to be a link rather than a payload.
    /// Nothing is requested from it — the capture is the text of the link.
    public static func isCapturableLink(_ url: URL) -> Bool {
        guard let scheme = url.scheme?.lowercased(),
              scheme == "http" || scheme == "https",
              let host = url.host,
              !host.isEmpty,
              url.absoluteString.utf8.count <= maximumLinkBytes
        else { return false }
        return true
    }

    private static func deduplicated(_ values: [String]) -> [String] {
        var seen = Set<String>()
        return values.filter { seen.insert($0).inserted }
    }

    private static func conforms(_ identifier: String, to type: UTType) -> Bool {
        UTType(identifier)?.conforms(to: type) ?? false
    }
}
