import Foundation
import XCTest
@testable import DocumentIntakeMacOS

/// What one shared logical item is captured from. Every outcome here is a
/// captured copy: the policy has no original case to return.
final class ShareRepresentationPolicyTests: XCTestCase {
    func testFileURLRepresentationIsCapturedAsACopy() {
        let choice = ShareRepresentationPolicy.choose(from: ["public.file-url", "com.adobe.pdf"])
        XCTAssertEqual(choice?.typeIdentifier, "public.file-url")
        XCTAssertEqual(choice?.kind, .file)
    }

    /// A provider that offers only bytes, and one that offers the file URL of
    /// those same bytes, both end as snapshots. Nothing about a provider's file
    /// URL — in place or temporary — makes it durable original authority.
    func testDataAndURLVariantsBothRemainSnapshots() {
        let fromData = ShareRepresentationPolicy.choose(from: ["com.adobe.pdf"])
        let fromURL = ShareRepresentationPolicy.choose(from: ["public.file-url"])
        XCTAssertEqual(fromData?.kind, .file)
        XCTAssertEqual(fromURL?.kind, .file)
        for kind in [fromData?.kind, fromURL?.kind] {
            XCTAssertNotNil(kind)
            // The closed kind set is snapshot-only by construction.
            XCTAssertTrue([.file, .image, .text, .link].contains(kind!))
        }
    }

    func testImageIsPreferredOverCompanionText() {
        let choice = ShareRepresentationPolicy.choose(from: ["public.plain-text", "public.jpeg"])
        XCTAssertEqual(choice?.kind, .image)
        XCTAssertEqual(choice?.typeIdentifier, "public.jpeg")
    }

    func testPlainTextIsClassifiedAsTextAndRichTextIsNot() {
        XCTAssertEqual(ShareRepresentationPolicy.choose(from: ["public.utf8-plain-text"])?.kind, .text)
        // RTF carries markup, so it is copied as the document it is rather
        // than written out as if it were a note.
        XCTAssertEqual(ShareRepresentationPolicy.choose(from: ["public.rtf"])?.kind, .file)
    }

    func testWebLinkIsClassifiedAsAnInertLink() {
        XCTAssertEqual(ShareRepresentationPolicy.choose(from: ["public.url"])?.kind, .link)
    }

    func testUnknownRepresentationIsRefused() {
        XCTAssertNil(ShareRepresentationPolicy.choose(from: []))
        XCTAssertNil(ShareRepresentationPolicy.choose(from: ["com.example.not-a-type"]))
    }

    func testRepeatedRepresentationsAreDeduplicated() {
        let choice = ShareRepresentationPolicy.choose(
            from: ["public.file-url", "public.file-url", "public.file-url"]
        )
        XCTAssertEqual(choice?.typeIdentifier, "public.file-url")
    }

    /// Only http(s) links with a host are captured, and only at note length.
    /// Capturing one writes the text of the link; nothing is ever requested
    /// from it.
    func testOnlyInertWebLinksAreCapturable() {
        XCTAssertTrue(ShareRepresentationPolicy.isCapturableLink(
            URL(string: "https://example.com/a?b=c")!
        ))
        XCTAssertTrue(ShareRepresentationPolicy.isCapturableLink(URL(string: "http://example.com")!))
        for refused in [
            "file:///etc/passwd",
            "javascript:alert(1)",
            "data:text/plain,hello",
            "ftp://example.com/x",
            "https://",
        ] {
            guard let url = URL(string: refused) else { continue }
            XCTAssertFalse(
                ShareRepresentationPolicy.isCapturableLink(url),
                "\(refused) must not be capturable"
            )
        }
        let long = "https://example.com/" + String(repeating: "a", count: 4_096)
        XCTAssertFalse(ShareRepresentationPolicy.isCapturableLink(URL(string: long)!))
    }

    // MARK: Slots

    /// A multi-file share is one slot per file, in the order the host gave it.
    func testEachSharedFileBecomesItsOwnSlotInOrder() {
        let slots = ShareRepresentationPolicy.slots(forItems: [[
            ["public.file-url", "public.jpeg"],
            ["public.file-url", "com.adobe.pdf"],
        ]])
        XCTAssertEqual(slots?.count, 2)
        XCTAssertEqual(slots?.map(\.attachmentIndex), [0, 1])
        XCTAssertEqual(slots?.map { $0.representation.kind }, [.file, .file])
    }

    /// A host that sends one logical share as alternative representations — a
    /// link and the page title beside it — is one slot, not two.
    func testAlternativeRepresentationsOfOneItemCollapseToOneSlot() {
        let slots = ShareRepresentationPolicy.slots(forItems: [[
            ["public.url"],
            ["public.plain-text"],
        ]])
        XCTAssertEqual(slots?.count, 1)
        XCTAssertEqual(slots?.first?.representation.kind, .link)
        XCTAssertEqual(slots?.first?.attachmentIndex, 0)
    }

    func testSeveralExtensionItemsKeepTheirOrder() {
        let slots = ShareRepresentationPolicy.slots(forItems: [
            [["public.url"]],
            [["public.file-url"]],
            [["public.plain-text"]],
        ])
        XCTAssertEqual(slots?.map(\.itemIndex), [0, 1, 2])
        XCTAssertEqual(slots?.map { $0.representation.kind }, [.link, .file, .text])
    }

    /// One item this build cannot capture refuses the whole selection: a partly
    /// captured selection is not what the person shared.
    func testOneUncapturableItemRefusesTheWholeSelection() {
        XCTAssertNil(ShareRepresentationPolicy.slots(forItems: [
            [["public.file-url"]],
            [["com.example.not-a-type"]],
        ]))
        XCTAssertNil(ShareRepresentationPolicy.slots(forItems: [[]]))
        XCTAssertNil(ShareRepresentationPolicy.slots(forItems: []))
    }
}
