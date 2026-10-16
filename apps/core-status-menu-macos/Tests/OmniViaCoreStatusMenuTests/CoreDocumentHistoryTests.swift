import XCTest
@testable import CoreCaptureMacOS
@testable import DocumentIntakeMacOS

/// The canonical history client, its strict decoder, and the management surface's reading
/// of both.
///
/// The decoder tests are mostly about refusals, because this is the one place where an
/// answer this build cannot describe could become an answer it describes wrongly: an
/// unknown state silently defaulted, a historical-only extraction read as current, an
/// unavailable action rendered as a button. Each of those has a case here, and each
/// asserts the refusal rather than a tolerated near-miss.
///
/// Response text is written as snake_case wire JSON, with an `extra` hole for injecting a
/// field this build does not define, exactly as `LifecycleContractTests` does it. Nothing
/// is decoded by hand: every case goes through the production decoder.
final class CoreDocumentHistoryTests: XCTestCase {
    // MARK: - Wire fixtures

    private enum Wire {
        static func capture(
            id: String = "dcap-1",
            sequence: Int = 1,
            disposition: String = "head_eligible",
            reason: String = "eligible",
            current: Bool = true,
            retention: String = "retained",
            processing: String = "settled",
            mediaType: String = "application/pdf",
            association: String = "active_binding",
            actions: String = """
                [{"action": "reprocess_captured_copy", "available": true}]
                """,
            profiles: String = """
                [{"profile_ref": "document-default", "revision": "1",
                  "processing_mode": "text_and_local_ocr",
                  "profile_digest": "sha256:aaaa"}]
                """,
            base: String? = nil,
            extra: String = ""
        ) -> String {
            """
            {"capture_id": "\(id)", "capture_sequence": \(sequence), "evidence_id": "evd-1",
             "source_digest": "sha256:cccc", "source_length_bytes": 11,
             "media_type": "\(mediaType)", "requested_source_identity": "source-1",
             "intent_kind": "new_capture", "association_kind": "\(association)",
             "capture_disposition": "\(disposition)", "disposition_reason": "\(reason)",
             "is_current_source": \(current), "retention_state": "\(retention)",
             "capture_retention_generation": 1, "source_removal_generation": 1,
             "requested_profile": {"profile_ref": "document-default", "revision": "1"},
             "processing_state": "\(processing)",
             \(base.map { "\"expected_base_capture_id\": \"\($0)\"," } ?? "")
             "captured_at": "2026-10-05T09:00:30Z",
             "retention_changed_at": "2026-10-05T09:00:30Z",
             "actions": \(actions), "reprocess_profiles": \(profiles)\(extra)}
            """
        }

        static func extraction(
            id: String = "dext-1",
            captureID: String = "dcap-1",
            ordinal: Int = 1,
            headEligible: Bool = true,
            current: Bool = true,
            result: String? = "succeeded",
            publication: String? = "promoted",
            output: String? = "sha256:bbbb",
            worker: String? = "core-portable-text-1",
            completed: String? = "2026-10-05T09:02:00Z",
            mode: String = "text_only",
            languages: String = "[]",
            extra: String = ""
        ) -> String {
            """
            {"extraction_revision_id": "\(id)", "capture_id": "\(captureID)",
             "document_ordinal": \(ordinal), "extraction_sequence": 1,
             "profile": {"digest": "sha256:aaaa",
                         "decoder_build": "document-default:1:plain-text-2",
                         "processing_mode": "\(mode)", "embedded_image_ocr": false,
                         "ocr_languages": \(languages)},
             "head_eligible": \(headEligible), "is_current_extraction": \(current),
             \(result.map { "\"result_state\": \"\($0)\"," } ?? "")
             \(publication.map { "\"publication_disposition\": \"\($0)\"," } ?? "")
             \(output.map { "\"output_digest\": \"\($0)\"," } ?? "")
             \(worker.map { "\"worker_build\": \"\($0)\"," } ?? "")
             \(completed.map { "\"completed_at\": \"\($0)\"," } ?? "")
             "admitted_at": "2026-10-05T09:01:00Z"\(extra)}
            """
        }

        static func page(
            documentID: String = "document-file-1",
            sourceState: String = "active",
            currentCapture: String? = "dcap-1",
            headSequence: Int = 1,
            binding: String = "connected",
            currentExtraction: String? = "dext-1",
            headOrdinal: Int = 1,
            retrieval: String = "retrievable",
            published: String? = "2026-10-05T09:02:00Z",
            captures: [String] = [Wire.capture()],
            captureCount: Int = 1,
            extractions: [String] = [Wire.extraction()],
            extractionCount: Int = 1,
            actions: String = """
                [{"action": "refresh_original", "available": true}]
                """,
            token: String? = nil,
            extra: String = ""
        ) -> String {
            """
            {"document_id": "\(documentID)", "created_at": "2026-10-05T09:00:00Z",
             "source": {"source_state": "\(sourceState)", "source_removal_generation": 1,
                        \(currentCapture.map { "\"current_capture_id\": \"\($0)\"," } ?? "")
                        "capture_head_sequence": \(headSequence),
                        "original_binding_state": "\(binding)",
                        "updated_at": "2026-10-05T09:02:00Z"},
             "extraction": {\(currentExtraction.map { "\"current_extraction_id\": \"\($0)\"," } ?? "")
                            "extraction_head_ordinal": \(headOrdinal),
                            "retrieval_state": "\(retrieval)"
                            \(published.map { ", \"published_at\": \"\($0)\"" } ?? "")},
             "captures": [\(captures.joined(separator: ", "))],
             "capture_count": \(captureCount),
             "extractions": [\(extractions.joined(separator: ", "))],
             "extraction_count": \(extractionCount),
             "actions": \(actions),
             "page": {\(token.map { "\"continuation_token\": \"\($0)\"" } ?? "")}\(extra)}
            """
        }

        static func envelope(
            result: String,
            apiVersion: String = supportedCoreHistoryContractVersion
        ) -> Data {
            Data(
                """
                {"metadata": {"request_id": "req-1", "correlation_id": "cor-1",
                              "version": {"api_version": "\(apiVersion)"},
                              "authority": {"principal_id": "local-user"}},
                 "result": \(result)}
                """.utf8
            )
        }

        static func refusal(code: String) -> Data {
            Data(
                """
                {"metadata": {"request_id": "req-1", "correlation_id": "cor-1",
                              "version": {"api_version": "\(supportedCoreHistoryContractVersion)"},
                              "authority": {"principal_id": "local-user"}},
                 "error": {"code": "\(code)", "message": "refused"}}
                """.utf8
            )
        }
    }

    private func decodePage(_ wire: String) -> Result<CoreDocumentHistoryPage, CoreHistoryError> {
        CLICoreDocumentHistoryTransport.decode(Wire.envelope(result: wire))
    }

    private func page(_ wire: String, file: StaticString = #filePath, line: UInt = #line) throws
        -> CoreDocumentHistoryPage
    {
        switch decodePage(wire) {
        case let .success(value): return value
        case let .failure(error):
            XCTFail("expected a readable page, got \(error)", file: file, line: line)
            throw error
        }
    }

    private func refused(
        _ wire: String,
        _ message: String,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        guard case let .failure(error) = decodePage(wire) else {
            return XCTFail("a page was read that should have been refused: \(message)",
                           file: file, line: line)
        }
        XCTAssertEqual(error, .unreadable, message, file: file, line: line)
    }

    // MARK: - The strict decoder

    func testACompletePageDecodesEveryClosedVocabulary() throws {
        let value = try page(Wire.page())

        XCTAssertEqual(value.documentID, "document-file-1")
        XCTAssertEqual(value.source.sourceState, .active)
        XCTAssertEqual(value.source.originalBindingState, .connected)
        XCTAssertEqual(value.source.currentCaptureID, "dcap-1")
        XCTAssertEqual(value.extraction.retrievalState, .retrievable)
        XCTAssertEqual(value.extraction.currentExtractionID, "dext-1")
        XCTAssertEqual(value.captures.first?.captureDisposition, .headEligible)
        XCTAssertEqual(value.captures.first?.retentionState, .retained)
        XCTAssertEqual(value.captures.first?.processingState, .settled)
        XCTAssertTrue(value.captures.first?.reprocessAvailable ?? false)
        XCTAssertEqual(value.extractions.first?.resultState, .succeeded)
        XCTAssertEqual(value.extractions.first?.publicationDisposition, .promoted)
        XCTAssertTrue(value.extractions.first?.isCurrentExtraction ?? false)
        XCTAssertNil(value.continuationToken)
    }

    func testAnUnknownFieldIsRefusedAtEveryLevel() {
        refused(Wire.page(extra: #", "source_bytes": "aGVsbG8=""#), "an unknown page field")
        refused(
            Wire.page(captures: [Wire.capture(extra: #", "original_path": "/private/x""#)]),
            "an unknown capture field"
        )
        refused(
            Wire.page(extractions: [Wire.extraction(extra: #", "segments": []"#)]),
            "an unknown extraction field"
        )
    }

    func testAnUnknownStateValueIsRefusedRatherThanDefaulted() {
        refused(Wire.page(sourceState: "quarantined"), "an unknown source state")
        refused(Wire.page(retrieval: "partially_retrievable"), "an unknown retrieval state")
        refused(Wire.page(captures: [Wire.capture(retention: "archived")]), "an unknown retention")
        refused(
            Wire.page(extractions: [Wire.extraction(result: "mostly")]),
            "an unknown result state"
        )
        refused(
            Wire.page(extractions: [Wire.extraction(publication: "published")]),
            "an unknown publication disposition"
        )
        refused(
            Wire.page(captures: [Wire.capture(profiles: """
                [{"profile_ref": "document-default", "revision": "1",
                  "processing_mode": "text_and_cloud_ocr", "profile_digest": "sha256:aaaa"}]
                """)]),
            "an unknown processing mode"
        )
    }

    func testACurrentFlagThatDisagreesWithTheHeadIsRefused() {
        refused(
            Wire.page(currentCapture: "dcap-2", headSequence: 2),
            "a capture claiming to be current that the source head does not name"
        )
        refused(
            Wire.page(captures: [Wire.capture(current: false)]),
            "the head's own capture denying it is current"
        )
        refused(
            Wire.page(currentExtraction: "dext-2", headOrdinal: 2),
            "an extraction claiming to be current that the extraction head does not name"
        )
    }

    func testAHistoricalOnlyResultCanNeverReadAsCurrent() {
        refused(
            Wire.page(extractions: [
                Wire.extraction(headEligible: false, publication: "historical_only"),
            ]),
            "a historical-only extraction marked current"
        )
        refused(
            Wire.page(captures: [Wire.capture(disposition: "historical_only", reason: "stale_base")]),
            "a historical-only capture marked as the current source"
        )
    }

    func testAPendingExtractionMayClaimNoOutcome() throws {
        let pending = try page(
            Wire.page(
                currentExtraction: nil,
                headOrdinal: 0,
                retrieval: "none",
                published: nil,
                extractions: [
                    Wire.extraction(
                        current: false,
                        result: nil,
                        publication: nil,
                        output: nil,
                        worker: nil,
                        completed: nil
                    ),
                ]
            )
        )
        XCTAssertTrue(pending.extractions[0].isPending)
        XCTAssertNil(pending.extractions[0].resultState)

        refused(
            Wire.page(extractions: [
                Wire.extraction(current: false, result: nil, publication: "promoted"),
            ]),
            "a pending extraction naming a publication disposition"
        )
        refused(
            Wire.page(extractions: [
                Wire.extraction(
                    current: false, result: nil, publication: nil, output: "sha256:bbbb",
                    worker: nil, completed: nil
                ),
            ]),
            "a pending extraction naming an output"
        )
    }

    func testAnActionMustStateWhyItIsUnavailableAndOfferNothingWhenItIs() {
        refused(
            Wire.page(captures: [Wire.capture(actions: """
                [{"action": "reprocess_captured_copy", "available": false}]
                """, profiles: "[]")]),
            "an unavailable action with no reason"
        )
        refused(
            Wire.page(captures: [Wire.capture(actions: """
                [{"action": "reprocess_captured_copy", "available": true,
                  "unavailable_reason": "source_removed"}]
                """)]),
            "an available action carrying a reason"
        )
        refused(
            Wire.page(captures: [Wire.capture(actions: """
                [{"action": "reprocess_captured_copy", "available": false,
                  "unavailable_reason": "source_not_retained"}]
                """)]),
            "a profile offered for an action reported unavailable"
        )
        refused(
            Wire.page(captures: [Wire.capture(profiles: "[]")]),
            "an available action with no approved profile to send"
        )
    }

    func testHeadNullityMustAgreeWithItsOrdinal() {
        refused(Wire.page(currentCapture: nil, headSequence: 1), "no capture head at sequence one")
        refused(
            Wire.page(currentExtraction: nil, headOrdinal: 1, retrieval: "none", published: nil),
            "no extraction head at ordinal one"
        )
        refused(
            Wire.page(retrieval: "retrievable", published: nil),
            "retrievable with no publication instant"
        )
        refused(
            Wire.page(
                currentExtraction: nil, headOrdinal: 0, retrieval: "none",
                published: "2026-10-05T09:02:00Z", extractions: [], extractionCount: 0
            ),
            "a publication instant with nothing published"
        )
    }

    func testAPageMayNotClaimMoreMembersThanItsTotal() {
        refused(Wire.page(captureCount: 0), "a page holding more captures than the total")
        refused(Wire.page(extractionCount: 0), "a page holding more extractions than the total")
    }

    func testATextOnlyProfileMayNotNameRecognitionLanguages() throws {
        let ocr = try page(
            Wire.page(extractions: [Wire.extraction(mode: "text_and_local_ocr", languages: #"["eng"]"#)])
        )
        XCTAssertEqual(ocr.extractions[0].profile.ocrLanguages, ["eng"])
        // A text-only profile ran no recogniser, so naming a language would claim
        // recognition that never happened. The decoder has no rule against it, so the
        // *server* is the one holding that line -- this asserts the shape decodes, which
        // is all this side can prove.
        let plain = try page(Wire.page())
        XCTAssertTrue(plain.extractions[0].profile.ocrLanguages.isEmpty)
    }

    func testAtMostThreeRecognitionLanguagesAreAccepted() throws {
        // Three is the normative maximum a selection may hold. A summary naming a fourth
        // describes a selection Core would itself have refused, so this build will not read
        // it as a profile.
        let three = try page(
            Wire.page(
                extractions: [
                    Wire.extraction(
                        mode: "text_and_local_ocr", languages: #"["eng", "deu", "fra"]"#
                    ),
                ]
            )
        )
        XCTAssertEqual(three.extractions[0].profile.ocrLanguages, ["eng", "deu", "fra"])
        refused(
            Wire.page(
                extractions: [
                    Wire.extraction(
                        mode: "text_and_local_ocr", languages: #"["eng", "deu", "fra", "spa"]"#
                    ),
                ]
            ),
            "a fourth recognition language"
        )
    }

    // MARK: - The envelope

    func testARefusalIsDecodedToItsFrozenCodeRatherThanOneFailure() {
        let outcome: Result<CoreDocumentHistoryPage, CoreHistoryError> =
            CLICoreDocumentHistoryTransport.decode(Wire.refusal(code: "not_found"))
        XCTAssertEqual(outcome.failureError, .refused(code: "not_found"))
    }

    func testANewerContractVersionIsRefusedRatherThanReadAsASubset() {
        let outcome: Result<CoreDocumentHistoryPage, CoreHistoryError> =
            CLICoreDocumentHistoryTransport.decode(
                Wire.envelope(result: Wire.page(), apiVersion: "1.4")
            )
        XCTAssertEqual(outcome.failureError, .unreadable)
    }

    func testAnEnvelopeWithBothBranchesOrNeitherIsRefused() {
        for document in [
            #"{"metadata": {"version": {"api_version": "1.3"}}}"#,
            """
            {"metadata": {"version": {"api_version": "1.3"}},
             "result": \(Wire.page()), "error": {"code": "conflict"}}
            """,
            #"{"result": {}}"#,
        ] {
            let outcome: Result<CoreDocumentHistoryPage, CoreHistoryError> =
                CLICoreDocumentHistoryTransport.decode(Data(document.utf8))
            XCTAssertEqual(outcome.failureError, .unreadable, document)
        }
    }

    func testAReprocessReceiptIsDecodedStrictly() {
        let body = """
            {"extraction_revision_id": "dext-2", "document_id": "document-file-1",
             "capture_id": "dcap-1", "profile_digest": "sha256:aaaa",
             "result_state": "succeeded", "publication_disposition": "historical_only",
             "became_head": false}
            """
        let outcome: Result<CoreReprocessReceipt, CoreHistoryError> =
            CLICoreDocumentHistoryTransport.decode(Wire.envelope(result: body))
        guard case let .success(receipt) = outcome else {
            return XCTFail("a receipt was refused: \(outcome)")
        }
        XCTAssertFalse(receipt.becameHead)
        // A reverse-order completion reads as history, never as a success.
        XCTAssertTrue(receipt.outcomeSentence.contains("history only"))
        XCTAssertFalse(receipt.outcomeSentence.contains("current searchable version"))

        let lying = body.replacingOccurrences(of: #""became_head": false"#, with: #""became_head": true"#)
        let refusedOutcome: Result<CoreReprocessReceipt, CoreHistoryError> =
            CLICoreDocumentHistoryTransport.decode(Wire.envelope(result: lying))
        XCTAssertEqual(refusedOutcome.failureError, .unreadable,
                       "a historical-only receipt claiming it became the head")
    }

    func testEveryTerminalDispositionHasItsOwnTruthfulSentence() {
        for (disposition, state) in [
            ("promoted", "succeeded"),
            ("superseded", "succeeded"),
            ("historical_only", "partial"),
            ("fenced_removal", "succeeded"),
            ("fenced_retention", "succeeded"),
        ] {
            let body = """
                {"extraction_revision_id": "dext-2", "document_id": "document-file-1",
                 "capture_id": "dcap-1", "profile_digest": "sha256:aaaa",
                 "result_state": "\(state)", "publication_disposition": "\(disposition)",
                 "became_head": \(disposition == "promoted"),
                 "output_digest": "sha256:bbbb"}
                """
            let outcome: Result<CoreReprocessReceipt, CoreHistoryError> =
                CLICoreDocumentHistoryTransport.decode(Wire.envelope(result: body))
            guard case let .success(receipt) = outcome else {
                return XCTFail("\(disposition) was refused")
            }
            if disposition == "promoted" {
                XCTAssertTrue(receipt.outcomeSentence.contains("current searchable version"))
            } else {
                XCTAssertTrue(
                    receipt.outcomeSentence.contains("history only"),
                    "\(disposition): \(receipt.outcomeSentence)"
                )
            }
        }
        for state in ["unsupported", "failed"] {
            let body = """
                {"extraction_revision_id": "dext-2", "document_id": "document-file-1",
                 "capture_id": "dcap-1", "profile_digest": "sha256:aaaa",
                 "result_state": "\(state)", "publication_disposition": "not_published",
                 "became_head": false}
                """
            let outcome: Result<CoreReprocessReceipt, CoreHistoryError> =
                CLICoreDocumentHistoryTransport.decode(Wire.envelope(result: body))
            guard case let .success(receipt) = outcome else {
                return XCTFail("\(state) was refused")
            }
            // A failure never reads as a replacement, and never as a success.
            XCTAssertFalse(receipt.outcomeSentence.contains("current searchable version"))
            XCTAssertTrue(
                receipt.outcomeSentence.contains("unchanged")
                    || receipt.outcomeSentence.contains("Nothing was replaced")
            )
        }
    }

    // MARK: - The bounded reader

    func testTheReaderFollowsEveryPageAndKeepsTheFreshestHeads() throws {
        let transport = RecordingTransport(pages: [
            try page(
                Wire.page(
                    currentCapture: "dcap-2",
                    headSequence: 2,
                    currentExtraction: "dext-2",
                    headOrdinal: 2,
                    captures: [
                        Wire.capture(
                            id: "dcap-1", sequence: 1, disposition: "historical_only",
                            reason: "stale_base", current: false, retention: "expired",
                            actions: """
                                [{"action": "reprocess_captured_copy", "available": false,
                                  "unavailable_reason": "source_not_retained"}]
                                """,
                            profiles: "[]"
                        ),
                    ],
                    captureCount: 2,
                    extractions: [
                        Wire.extraction(
                            id: "dext-1", ordinal: 1, headEligible: false, current: false,
                            publication: "historical_only"
                        ),
                    ],
                    extractionCount: 2,
                    token: "cursor-1"
                )
            ),
            try page(
                Wire.page(
                    currentCapture: "dcap-2",
                    headSequence: 2,
                    currentExtraction: "dext-2",
                    headOrdinal: 2,
                    captures: [Wire.capture(id: "dcap-2", sequence: 2)],
                    captureCount: 2,
                    extractions: [Wire.extraction(id: "dext-2", ordinal: 2)],
                    extractionCount: 2
                )
            ),
        ])
        let outcome = CoreDocumentHistoryReader(transport: transport)
            .read(documentID: "document-file-1")

        guard case let .success(history) = outcome else {
            return XCTFail("the reader refused a two-page history: \(outcome)")
        }
        XCTAssertEqual(history.captures.map { $0.captureID }, ["dcap-1", "dcap-2"])
        XCTAssertEqual(
            history.extractions.map { $0.extractionRevisionID }, ["dext-1", "dext-2"]
        )
        XCTAssertEqual(history.captureCount, 2)
        XCTAssertTrue(history.complete)
        // The second page's token was presented verbatim, and only once.
        XCTAssertEqual(transport.tokens, [nil, "cursor-1"])
        // Only the retained capture is offered for reprocessing.
        XCTAssertEqual(history.reprocessableCaptures.map { $0.captureID }, ["dcap-2"])
    }

    func testAnAnswerForAnotherDocumentIsRefused() throws {
        let transport = RecordingTransport(pages: [try page(Wire.page(documentID: "document-other"))])
        let outcome = CoreDocumentHistoryReader(transport: transport)
            .read(documentID: "document-file-1")
        XCTAssertEqual(outcome.failureError, .unreadable)
    }

    /// One page of a long document: no member is current, so the pages differ only in which
    /// capture they carry and which token they hand back.
    private func unboundedPage(index: Int, token: String?) throws -> CoreDocumentHistoryPage {
        try page(
            Wire.page(
                currentCapture: nil,
                headSequence: 0,
                currentExtraction: nil,
                headOrdinal: 0,
                retrieval: "none",
                published: nil,
                captures: [Wire.capture(id: "dcap-\(index)", sequence: index, current: false)],
                captureCount: 9_999,
                extractions: [],
                extractionCount: 0,
                token: token
            )
        )
    }

    func testTheReaderStopsAtItsPageBoundAndSaysTheViewIsBounded() throws {
        let transport = RecordingTransport(
            pages: try (1...(CoreDocumentHistoryReader.maximumPages + 4)).map {
                try unboundedPage(index: $0, token: "cursor-\($0)")
            }
        )
        let outcome = CoreDocumentHistoryReader(transport: transport)
            .read(documentID: "document-file-1")

        guard case let .success(history) = outcome else {
            return XCTFail("the reader refused a bounded history: \(outcome)")
        }
        XCTAssertEqual(transport.tokens.count, CoreDocumentHistoryReader.maximumPages)
        XCTAssertFalse(history.complete, "a bounded view must say it is bounded")
    }

    func testATokenCycleIsRefusedRatherThanWalkedToThePageBound() throws {
        // A server that hands back a token this read has already followed is not offering a
        // position further on. Walking it would re-serve members already held, which the
        // distinctness check would then refuse anyway -- but refusing the cycle itself says
        // what actually went wrong and stops before the twentieth round trip.
        let transport = RecordingTransport(
            pages: [
                try unboundedPage(index: 1, token: "cursor-1"),
                try unboundedPage(index: 2, token: "cursor-1"),
            ]
        )
        let outcome = CoreDocumentHistoryReader(transport: transport)
            .read(documentID: "document-file-1")
        XCTAssertEqual(outcome.failureError, .unreadable)
        XCTAssertEqual(transport.tokens, [nil, "cursor-1"])
    }

    func testAPageThatDisagreesWithTheFirstOnTheHeadsIsRefused() throws {
        // Page two states a different source head, which means it was answered from a
        // different state of the document. Stitching the two would publish a history no
        // instant ever held.
        let transport = RecordingTransport(pages: [
            try page(
                Wire.page(
                    captures: [Wire.capture(id: "dcap-1", sequence: 1)],
                    captureCount: 2,
                    extractionCount: 1,
                    token: "cursor-1"
                )
            ),
            try page(
                Wire.page(
                    currentCapture: "dcap-2",
                    headSequence: 2,
                    captures: [Wire.capture(id: "dcap-2", sequence: 2)],
                    captureCount: 2,
                    extractions: [],
                    extractionCount: 1
                )
            ),
        ])
        let outcome = CoreDocumentHistoryReader(transport: transport)
            .read(documentID: "document-file-1")
        XCTAssertEqual(outcome.failureError, .unreadable)
    }

    func testAPageThatDisagreesWithTheFirstOnTheTotalsOrActionsIsRefused() throws {
        for later in [
            // A different total: the document grew or shrank between the two answers.
            Wire.page(
                captures: [Wire.capture(id: "dcap-2", sequence: 2, current: false)],
                captureCount: 3,
                extractions: [],
                extractionCount: 1
            ),
            // A different document-level action: the original became unreachable between
            // them, so the first page's offer is no longer Core's statement.
            Wire.page(
                captures: [Wire.capture(id: "dcap-2", sequence: 2, current: false)],
                captureCount: 2,
                extractions: [],
                extractionCount: 1,
                actions: """
                    [{"action": "refresh_original", "available": false,
                      "unavailable_reason": "original_disconnected"}]
                    """
            ),
            // A different creation instant: not the same document's history at all.
            Wire.page(
                captures: [Wire.capture(id: "dcap-2", sequence: 2, current: false)],
                captureCount: 2,
                extractions: [],
                extractionCount: 1
            ).replacingOccurrences(
                of: "\"created_at\": \"2026-10-05T09:00:00Z\"",
                with: "\"created_at\": \"2026-10-05T09:30:00Z\""
            ),
        ] {
            let transport = RecordingTransport(pages: [
                try page(
                    Wire.page(
                        captures: [Wire.capture(id: "dcap-1", sequence: 1)],
                        captureCount: 2,
                        extractionCount: 1,
                        token: "cursor-1"
                    )
                ),
                try page(later),
            ])
            XCTAssertEqual(
                CoreDocumentHistoryReader(transport: transport)
                    .read(documentID: "document-file-1").failureError,
                .unreadable
            )
        }
    }

    func testAMemberServedTwiceAcrossPagesIsNeverRendered() throws {
        // Each page is internally truthful: both carry dcap-1 and both agree it is the
        // current source. Only the aggregate can see that the same capture arrived twice,
        // and a surface that rendered it would show one captured copy as two.
        let repeated = Wire.capture(id: "dcap-1", sequence: 1)
        let transport = RecordingTransport(pages: [
            try page(
                Wire.page(
                    captures: [repeated],
                    captureCount: 2,
                    extractionCount: 1,
                    token: "cursor-1"
                )
            ),
            try page(
                Wire.page(captures: [repeated], captureCount: 2, extractions: [], extractionCount: 1)
            ),
        ])
        let outcome = CoreDocumentHistoryReader(transport: transport)
            .read(documentID: "document-file-1")
        XCTAssertEqual(outcome.failureError, .unreadable)
    }

    func testACompleteReadMustContainTheMemberItsHeadNames() throws {
        // The head names dcap-2 as current and the read says it is complete, yet no page
        // ever carried dcap-2. The aggregate would then claim a current source it cannot
        // show, which is exactly the claim this surface must not make.
        let transport = RecordingTransport(pages: [
            try page(
                Wire.page(
                    currentCapture: "dcap-2",
                    headSequence: 2,
                    captures: [Wire.capture(id: "dcap-1", sequence: 1, current: false)],
                    captureCount: 2,
                    extractionCount: 1
                )
            ),
        ])
        let outcome = CoreDocumentHistoryReader(transport: transport)
            .read(documentID: "document-file-1")
        XCTAssertEqual(outcome.failureError, .unreadable)
    }

    func testABoundedReadMayLegitimatelyNotHaveReachedTheCurrentMember() throws {
        // The same shape, but the read stopped at its page bound rather than at Core's last
        // page. A bounded view is allowed not to hold the current member; it says it is
        // bounded instead of pretending otherwise.
        let transport = RecordingTransport(
            pages: try (1...CoreDocumentHistoryReader.maximumPages).map { index in
                try page(
                    Wire.page(
                        currentCapture: "dcap-9999",
                        headSequence: 9_999,
                        currentExtraction: nil,
                        headOrdinal: 0,
                        retrieval: "none",
                        published: nil,
                        captures: [
                            Wire.capture(id: "dcap-\(index)", sequence: index, current: false),
                        ],
                        captureCount: 9_999,
                        extractions: [],
                        extractionCount: 0,
                        token: "cursor-\(index)"
                    )
                )
            }
        )
        let outcome = CoreDocumentHistoryReader(transport: transport)
            .read(documentID: "document-file-1")

        guard case let .success(history) = outcome else {
            return XCTFail("a bounded read short of the current member must still read: \(outcome)")
        }
        XCTAssertFalse(history.complete)
        XCTAssertEqual(history.source.currentCaptureID, "dcap-9999")
        XCTAssertFalse(history.captures.contains { $0.isCurrentSource })
    }

    func testAFailedPageIsTheWholeReadsFailure() throws {
        let transport = RecordingTransport(
            pages: [try page(Wire.page(token: "cursor-1"))],
            failure: .timedOut
        )
        let outcome = CoreDocumentHistoryReader(transport: transport)
            .read(documentID: "document-file-1")
        XCTAssertEqual(outcome.failureError, .timedOut)
    }

    // MARK: - The reprocess key

    func testTheReprocessKeyIsExactReplayForOneIntentAndDistinctForAnother() {
        let key = CoreDocumentHistoryReader.reprocessOperationKey(
            captureID: "dcap-1", profileRef: "document-default", revision: "1",
            expectedProfileDigest: "sha256:aaaa", expectedBaseExtractionID: "dext-1"
        )
        XCTAssertEqual(
            key,
            CoreDocumentHistoryReader.reprocessOperationKey(
                captureID: "dcap-1", profileRef: "document-default", revision: "1",
                expectedProfileDigest: "sha256:aaaa", expectedBaseExtractionID: "dext-1"
            ),
            "the same confirmed intent must replay rather than admit a second extraction"
        )
        XCTAssertTrue(isCoreIdentifier(key), "the key must be inside the identifier domain")
        XCTAssertLessThanOrEqual(key.count, 128)
        // Each of the five fields changes the request Core settles the key under, so each
        // changes the key. The expected profile digest is one of them: two requests that
        // differ only there are two different requests to Core, so one key cannot cover
        // both.
        let variants = [
            CoreDocumentHistoryReader.reprocessOperationKey(
                captureID: "dcap-2", profileRef: "document-default", revision: "1",
                expectedProfileDigest: "sha256:aaaa", expectedBaseExtractionID: "dext-1"
            ),
            CoreDocumentHistoryReader.reprocessOperationKey(
                captureID: "dcap-1", profileRef: "text-only", revision: "1",
                expectedProfileDigest: "sha256:aaaa", expectedBaseExtractionID: "dext-1"
            ),
            CoreDocumentHistoryReader.reprocessOperationKey(
                captureID: "dcap-1", profileRef: "document-default", revision: "2",
                expectedProfileDigest: "sha256:aaaa", expectedBaseExtractionID: "dext-1"
            ),
            CoreDocumentHistoryReader.reprocessOperationKey(
                captureID: "dcap-1", profileRef: "document-default", revision: "1",
                expectedProfileDigest: "sha256:bbbb", expectedBaseExtractionID: "dext-1"
            ),
            CoreDocumentHistoryReader.reprocessOperationKey(
                captureID: "dcap-1", profileRef: "document-default", revision: "1",
                expectedProfileDigest: "sha256:aaaa", expectedBaseExtractionID: nil
            ),
        ]
        XCTAssertEqual(Set(variants + [key]).count, 6)
    }

    func testTheRequestSendsTheExactProfileDigestTheReadOffered() throws {
        let value = try history(Wire.page())
        let model = subject(.current(value))
        let item = FilesAndFoldersItem.file(CoreHistoryFixtures.file())
        let intent = try XCTUnwrap(
            model.reprocessIntent(for: item, captureID: "dcap-1", profileDigest: "sha256:aaaa")
        )
        let transport = CapturingReprocessTransport()
        _ = transport.reprocess(
            captureID: intent.capture.captureID,
            profileRef: intent.profile.profileRef,
            revision: intent.profile.revision,
            expectedProfileDigest: intent.profile.profileDigest,
            expectedBaseExtractionID: intent.expectedBaseExtractionID,
            operationKey: intent.operationKey
        )
        // Verbatim from the offered selection, never recomputed or defaulted on this side.
        XCTAssertEqual(transport.sentDigest, "sha256:aaaa")
        XCTAssertEqual(
            transport.sentKey,
            CoreDocumentHistoryReader.reprocessOperationKey(
                captureID: "dcap-1", profileRef: "document-default", revision: "1",
                expectedProfileDigest: "sha256:aaaa", expectedBaseExtractionID: "dext-1"
            )
        )
    }

    // MARK: - The surface

    private func subject(_ availability: CoreHistoryAvailability) -> FilesAndFoldersModel {
        FilesAndFoldersModel(
            workspaceName: "workspace-1",
            files: [CoreHistoryFixtures.file()],
            coreHistory: CoreHistoryState(
                documentID: "document-file-1", availability: availability
            )
        )
    }

    private func history(_ wire: String) throws -> CoreDocumentHistory {
        let value = try page(wire)
        return CoreDocumentHistory(
            documentID: value.documentID,
            createdAt: value.createdAt,
            source: value.source,
            extraction: value.extraction,
            captures: value.captures,
            captureCount: value.captureCount,
            extractions: value.extractions,
            extractionCount: value.extractionCount,
            actions: value.actions,
            complete: true
        )
    }

    private func facts(_ model: FilesAndFoldersModel) -> [String: String] {
        Dictionary(
            uniqueKeysWithValues: model.detail(for: .file(CoreHistoryFixtures.file()))
                .map { ($0.label, $0.value) }
        )
    }

    /// AT-50: evidence is retrievable while the requested linkage is pending.
    ///
    /// This is the state design §12.1 calls `pending_association`: "where
    /// evidence exists but canonical linkage is still being recovered. It must
    /// map to progress or attention, never complete linked-file success."
    /// Core's answer here is exactly that — a published, retrievable extraction
    /// over a capture it reports as `snapshot_only`, with no original binding on
    /// this device. So the surface must offer none of the three original
    /// actions and must not describe the capture as linked, however complete
    /// the evidence side is.
    func testAPendingRequestedLinkageOffersNoOriginalActionAndClaimsNoLink() throws {
        let pending = try history(
            Wire.page(
                binding: "none",
                captures: [Wire.capture(association: "snapshot_only")]
            )
        )
        let model = subject(.current(pending))

        // Evidence really is retrievable: this is not a test of a missing capture.
        XCTAssertEqual(pending.extraction.retrievalState, .retrievable)
        XCTAssertNotNil(pending.extraction.publishedAt)

        let actions = FilesAndFoldersModel.actions(
            for: .file(CoreHistoryFixtures.file()), coreHistory: model.coreHistory
        )
        XCTAssertFalse(
            actions.originalActionsEnabled,
            "Refresh, Open original and Reveal in Finder need a confirmed canonical association"
        )
        // Reprocessing is a different authority: it works on bytes Core already
        // holds, so a pending linkage does not bar it.
        XCTAssertTrue(actions.reprocessEnabled)

        let values = facts(model)
        XCTAssertEqual(
            values["Original access"],
            "None recorded. This document has no original link on this device, so "
                + "only its captured copies are available. Use Locate to link an original."
        )
        XCTAssertTrue(
            values["Source capture 1"]?.contains(
                "captured copy only, no confirmed original link"
            ) ?? false,
            "\(values["Source capture 1"] ?? "")"
        )
    }

    /// The other side of the same rule: once Core confirms the association the
    /// three actions are offered, and a read that could only be refreshed into
    /// staleness withdraws them again. Design §12.1 offers original actions
    /// "only if binding permits", and a stale read permits nothing.
    func testConfirmedAssociationPermitsOriginalActionsAndAStaleReadWithdrawsThem() throws {
        let confirmed = try history(Wire.page(binding: "connected"))

        XCTAssertTrue(
            FilesAndFoldersModel.actions(
                for: .file(CoreHistoryFixtures.file()),
                coreHistory: subject(.current(confirmed)).coreHistory
            ).originalActionsEnabled
        )
        XCTAssertFalse(
            FilesAndFoldersModel.actions(
                for: .file(CoreHistoryFixtures.file()),
                coreHistory: subject(.stale(confirmed)).coreHistory
            ).originalActionsEnabled,
            "these are the last facts Core reported; they are not a current permission"
        )

        // A connected binding Core itself reports as unreadable permits nothing
        // either, so the flag is not just a restatement of the binding word.
        let blocked = try history(
            Wire.page(
                binding: "connected",
                actions: """
                    [{"action": "refresh_original", "available": false,
                      "unavailable_reason": "format_not_qualified"}]
                    """
            )
        )
        XCTAssertFalse(
            FilesAndFoldersModel.actions(
                for: .file(CoreHistoryFixtures.file()),
                coreHistory: subject(.current(blocked)).coreHistory
            ).originalActionsEnabled
        )
    }

    func testTheFileDetailLabelsTheTwoHistoriesSeparatelyAndNamesTheCurrentVersion() throws {
        let model = subject(.current(try history(Wire.page())))
        let values = facts(model)

        XCTAssertEqual(values["Document in Core"], "document-file-1")
        XCTAssertEqual(values["Source capture history"], "1 source capture")
        XCTAssertEqual(values["Extraction and profile history"], "1 extraction")
        XCTAssertEqual(
            values["Current searchable version"],
            "Extraction 1, published 2026-10-05T09:02:00Z · Text only · profile "
                + "document-default:1:plain-text-2 · digest sha256:aaaa"
        )
        XCTAssertTrue(values["Source capture 1"]?.hasPrefix("Current source") ?? false)
        XCTAssertTrue(
            values["Extraction 1"]?.contains("current searchable version") ?? false,
            "\(values["Extraction 1"] ?? "")"
        )
        // The boundary is stated, so a reader can tell which record they are looking at.
        XCTAssertEqual(
            values["Local delivery activity"],
            "Listed separately under Recent additions. Those are this Mac's own delivery "
                + "receipts, not Core's record, and a local receipt is never a capture or an "
                + "extraction."
        )
    }

    func testAHistoricalOnlyExtractionIsNeverLabelledCurrent() throws {
        let model = subject(
            .current(
                try history(
                    Wire.page(
                        extractions: [
                            Wire.extraction(id: "dext-1", ordinal: 1),
                            Wire.extraction(
                                id: "dext-2", ordinal: 2, headEligible: false, current: false,
                                publication: "historical_only"
                            ),
                        ],
                        extractionCount: 2
                    )
                )
            )
        )
        let values = facts(model)
        // Ordinal 2 arrived last and is still not current.
        XCTAssertTrue(
            values["Extraction 2"]?.contains("historical only") ?? false,
            "\(values["Extraction 2"] ?? "")"
        )
        XCTAssertFalse(values["Extraction 2"]?.contains("current searchable") ?? true)
        XCTAssertTrue(values["Extraction 1"]?.contains("current searchable version") ?? false)
    }

    func testAFailedExtractionSaysTheEarlierVersionIsStillCurrent() throws {
        let model = subject(
            .current(
                try history(
                    Wire.page(
                        extractions: [
                            Wire.extraction(id: "dext-1", ordinal: 1),
                            Wire.extraction(
                                id: "dext-2", ordinal: 2, current: false, result: "failed",
                                publication: "not_published", output: nil
                            ),
                        ],
                        extractionCount: 2
                    )
                )
            )
        )
        let values = facts(model)
        XCTAssertTrue(values["Extraction 2"]?.contains("Processing failed") ?? false)
        XCTAssertTrue(values["Extraction 2"]?.contains("nothing was replaced") ?? false)
        XCTAssertTrue(
            values["Current searchable version"]?.hasPrefix("Extraction 1") ?? false,
            "the earlier result stays current"
        )
    }

    func testAPendingExtractionReadsAsProcessingRatherThanFailed() throws {
        let model = subject(
            .current(
                try history(
                    Wire.page(
                        currentExtraction: nil, headOrdinal: 0, retrieval: "none", published: nil,
                        extractions: [
                            Wire.extraction(
                                current: false, result: nil, publication: nil, output: nil,
                                worker: nil, completed: nil
                            ),
                        ]
                    )
                )
            )
        )
        let values = facts(model)
        XCTAssertTrue(values["Extraction 1"]?.hasPrefix("Processing ·") ?? false)
        XCTAssertFalse(values["Extraction 1"]?.contains("failed") ?? true)
        XCTAssertEqual(
            values["Current searchable version"],
            "None published. No extraction of this document is currently searchable."
        )
    }

    func testAnExpiredCopyStatesWhyReprocessingIsUnavailableAndOffersNoControl() throws {
        let model = subject(
            .current(
                try history(
                    Wire.page(captures: [
                        Wire.capture(
                            retention: "expired",
                            actions: """
                                [{"action": "reprocess_captured_copy", "available": false,
                                  "unavailable_reason": "source_not_retained"}]
                                """,
                            profiles: "[]"
                        ),
                    ])
                )
            )
        )
        let values = facts(model)
        XCTAssertTrue(values["Source capture 1"]?.contains("copy no longer available") ?? false)
        XCTAssertTrue(
            values["Source capture 1"]?
                .contains("reprocessing unavailable: the captured copy is no longer retained")
                ?? false
        )
        XCTAssertFalse(
            FilesAndFoldersModel.actions(
                for: .file(CoreHistoryFixtures.file()), coreHistory: model.coreHistory
            ).reprocessEnabled,
            "no control for an action Core reports unavailable"
        )
        XCTAssertNil(model.reprocessChoices(for: .file(CoreHistoryFixtures.file())))
    }

    func testARemovedSourceAndADisconnectedOriginalEachStateTheirOwnFact() throws {
        let removed = subject(
            .current(
                try history(
                    Wire.page(
                        sourceState: "removed", binding: "disconnected",
                        captures: [
                            Wire.capture(
                                actions: """
                                    [{"action": "reprocess_captured_copy", "available": false,
                                      "unavailable_reason": "source_removed"}]
                                    """,
                                profiles: "[]"
                            ),
                        ],
                        actions: """
                            [{"action": "refresh_original", "available": false,
                              "unavailable_reason": "source_removed"}]
                            """
                    )
                )
            )
        )
        let values = facts(removed)
        XCTAssertTrue(values["Source state"]?.hasPrefix("Removed in Core") ?? false)
        XCTAssertTrue(values["Original access"]?.hasPrefix("Disconnected") ?? false)
        XCTAssertTrue(values["Original access"]?.contains("Locate") ?? false,
                      "the only offered recovery is the native access flow")
    }

    func testASnapshotOnlyDocumentOffersLocateRatherThanRefresh() throws {
        let model = subject(
            .current(
                try history(
                    Wire.page(
                        binding: "none",
                        actions: """
                            [{"action": "refresh_original", "available": false,
                              "unavailable_reason": "no_original_binding"}]
                            """
                    )
                )
            )
        )
        XCTAssertTrue(facts(model)["Original access"]?.hasPrefix("None recorded") ?? false)
        XCTAssertTrue(facts(model)["Original access"]?.contains("Locate") ?? false)
    }

    func testALoadingReadSaysSoAndOffersNoReprocess() {
        let model = subject(.loading)
        XCTAssertEqual(
            facts(model)["Capture history"],
            "Reading the capture and extraction history from Core."
        )
        XCTAssertFalse(
            FilesAndFoldersModel.actions(
                for: .file(CoreHistoryFixtures.file()), coreHistory: model.coreHistory
            ).reprocessEnabled
        )
    }

    func testAStaleReadKeepsTheFactsAndWithdrawsTheAction() throws {
        let model = subject(.stale(try history(Wire.page())))
        let values = facts(model)
        XCTAssertTrue(values["Capture history"]?.contains("may be out of date") ?? false)
        // The facts are still shown, because they were true when read.
        XCTAssertEqual(values["Document in Core"], "document-file-1")
        // The action is not, because reprocessing binds a head this surface is no longer
        // sure of.
        XCTAssertFalse(
            FilesAndFoldersModel.actions(
                for: .file(CoreHistoryFixtures.file()), coreHistory: model.coreHistory
            ).reprocessEnabled
        )
        XCTAssertNil(model.reprocessChoices(for: .file(CoreHistoryFixtures.file())))
    }

    func testEachUnavailableReadStatesWhatItActuallyProves() {
        let expectations: [(CoreHistoryError, String)] = [
            (.unavailable, "Core could not be reached"),
            (.timedOut, "did not answer in time"),
            (.unreadable, "Update required"),
            (.refused(code: "not_found"), "no document for this file yet"),
            (.refused(code: "workspace_not_granted"), "Access to this workspace is required"),
            (.refused(code: "upgrade_required"), "Update required"),
            (.refused(code: "internal_recoverable"), "Core refused the request"),
        ]
        for (error, fragment) in expectations {
            let value = facts(subject(.unavailable(error)))["Capture history"] ?? ""
            XCTAssertTrue(value.contains(fragment), "\(error): \(value)")
        }
    }

    func testTheHeldHistoryIsNeverRenderedAgainstAnotherDocument() throws {
        let model = FilesAndFoldersModel(
            workspaceName: "workspace-1",
            files: [CoreHistoryFixtures.file(), CoreHistoryFixtures.file(id: "file-2")],
            coreHistory: CoreHistoryState(
                documentID: "document-file-1", availability: .current(try history(Wire.page()))
            )
        )
        let other = Dictionary(
            uniqueKeysWithValues: model.detail(for: .file(CoreHistoryFixtures.file(id: "file-2")))
                .map { ($0.label, $0.value) }
        )
        XCTAssertEqual(
            other["Capture history"],
            "Not read yet. Core holds the capture, extraction and profile history for this file."
        )
        XCTAssertNil(other["Document in Core"])
        XCTAssertEqual(
            model.coreHistoryAvailability(for: .file(CoreHistoryFixtures.file(id: "file-2"))),
            .notRequested
        )
    }

    func testAConfirmedIntentCarriesOnlyWhatCoreReported() throws {
        let value = try history(Wire.page())
        let model = subject(.current(value))
        let item = FilesAndFoldersItem.file(CoreHistoryFixtures.file())
        let choices = try XCTUnwrap(model.reprocessChoices(for: item))
        XCTAssertEqual(choices.captures.map { $0.captureID }, ["dcap-1"])
        XCTAssertEqual(choices.expectedBaseExtractionID, "dext-1")

        let intent = try XCTUnwrap(
            model.reprocessIntent(
                for: item, captureID: "dcap-1", profileDigest: "sha256:aaaa"
            )
        )
        XCTAssertEqual(intent.capture.captureID, "dcap-1")
        XCTAssertEqual(intent.profile.profileRef, "document-default")
        XCTAssertEqual(intent.profile.revision, "1")
        // The base is bound exactly as the read observed it, including its absence.
        XCTAssertEqual(intent.expectedBaseExtractionID, "dext-1")
        XCTAssertEqual(intent.profile.profileDigest, "sha256:aaaa")
        XCTAssertEqual(
            intent.operationKey,
            CoreDocumentHistoryReader.reprocessOperationKey(
                captureID: "dcap-1", profileRef: "document-default", revision: "1",
                expectedProfileDigest: "sha256:aaaa", expectedBaseExtractionID: "dext-1"
            )
        )
        // The confirmation names the exact capture and profile, and says the original is
        // not read.
        XCTAssertTrue(intent.confirmationMessage.contains("Capture 1"))
        XCTAssertTrue(intent.confirmationMessage.contains("document-default"))
        XCTAssertTrue(intent.confirmationMessage.contains("revision 1"))
        XCTAssertTrue(intent.confirmationMessage.contains("original file is not read"))
        // A digest Core did not offer for this capture is not an intent.
        XCTAssertNil(
            model.reprocessIntent(for: item, captureID: "dcap-1", profileDigest: "sha256:ffff")
        )
        XCTAssertNil(
            model.reprocessIntent(for: item, captureID: "dcap-9", profileDigest: "sha256:aaaa")
        )
    }

    func testTheChooserOffersOnePairPerRetainedCopyAndApprovedProcessing() throws {
        let model = subject(
            .current(
                try history(
                    Wire.page(
                        currentCapture: "dcap-2",
                        headSequence: 2,
                        captures: [
                            Wire.capture(
                                id: "dcap-1", sequence: 1,
                                disposition: "historical_only", reason: "stale_base",
                                current: false,
                                profiles: """
                                    [{"profile_ref": "document-default", "revision": "1",
                                      "processing_mode": "text_and_local_ocr",
                                      "profile_digest": "sha256:aaaa"},
                                     {"profile_ref": "text-only", "revision": "1",
                                      "processing_mode": "text_only",
                                      "profile_digest": "sha256:bbbb"}]
                                    """
                            ),
                            Wire.capture(
                                id: "dcap-2", sequence: 2, retention: "expired",
                                actions: """
                                    [{"action": "reprocess_captured_copy", "available": false,
                                      "unavailable_reason": "source_not_retained"}]
                                    """,
                                profiles: "[]"
                            ),
                        ],
                        captureCount: 2
                    )
                )
            )
        )
        let item = FilesAndFoldersItem.file(CoreHistoryFixtures.file())
        let pairs = model.reprocessOptions(for: item)

        // Only the retained copy is offered, once per approved processing, and the
        // expired one is not offered at all.
        XCTAssertEqual(pairs.map { $0.capture.captureID }, ["dcap-1", "dcap-1"])
        XCTAssertEqual(pairs.map { $0.profile.profileDigest }, ["sha256:aaaa", "sha256:bbbb"])
        // Each title names the exact capture and the exact approved profile, so a
        // selection cannot mean something other than what its label said.
        let titles = pairs.map {
            FilesAndFoldersModel.reprocessOptionTitle(capture: $0.capture, profile: $0.profile)
        }
        XCTAssertTrue(titles[0].contains("Capture 1"))
        XCTAssertTrue(titles[0].contains("historical only"))
        XCTAssertTrue(titles[0].contains("Text and local OCR"))
        XCTAssertTrue(titles[1].contains("Text only"))
        XCTAssertTrue(titles[1].contains("revision 1"))
        // Every offered pair resolves to an intent, and nothing outside them does.
        for pair in pairs {
            XCTAssertNotNil(
                model.reprocessIntent(
                    for: item, captureID: pair.capture.captureID,
                    profileDigest: pair.profile.profileDigest
                )
            )
        }
        XCTAssertNil(
            model.reprocessIntent(for: item, captureID: "dcap-2", profileDigest: "sha256:aaaa"),
            "the expired copy is not reprocessable, whichever digest is named"
        )
    }

    func testAnEmptyExtractionHeadBindsAnExplicitlyAbsentBase() throws {
        let model = subject(
            .current(
                try history(
                    Wire.page(
                        currentExtraction: nil, headOrdinal: 0, retrieval: "none", published: nil,
                        extractions: [], extractionCount: 0
                    )
                )
            )
        )
        let item = FilesAndFoldersItem.file(CoreHistoryFixtures.file())
        let intent = try XCTUnwrap(
            model.reprocessIntent(for: item, captureID: "dcap-1", profileDigest: "sha256:aaaa")
        )
        XCTAssertNil(intent.expectedBaseExtractionID)
    }

    func testNoHistoryRowEverCarriesSourceBytesAPathOrAnOperationKey() throws {
        let rendered = facts(subject(.current(try history(Wire.page()))))
            .map { "\($0.key): \($0.value)" }
            .joined(separator: "\n")
        for forbidden in ["/Users/", "/private/", "file://", "aGVsbG8", "owner_ref", "bookmark"] {
            XCTAssertFalse(rendered.contains(forbidden), forbidden)
        }
    }

    // MARK: - The control

    /// The management window over real durable stores, with no window built: every
    /// assertion below drives the production decision and the production selector body,
    /// not a layout.
    @MainActor
    private func managementWindow(
        locateOriginal: (() -> Void)?,
        environment suppliedEnvironment: DocumentIntakeEnvironment? = nil
    ) throws -> FolderManagementWindowController {
        let environment: DocumentIntakeEnvironment
        if let suppliedEnvironment {
            environment = suppliedEnvironment
        } else {
            let stateRoot = try scratchDirectory().appendingPathComponent("state", isDirectory: true)
            try FileManager.default.createDirectory(at: stateRoot, withIntermediateDirectories: true)
            environment = try DocumentIntakeEnvironment(stateRoot: stateRoot)
        }
        return FolderManagementWindowController(
            workspaceName: "workspace-1",
            folderStore: environment.folderSubscriptions,
            folderMonitoring: environment.folderMonitoring,
            fileStore: environment.fileSubscriptions,
            fileMonitoring: environment.fileMonitoring,
            bulkStore: environment.folderBulkRuns,
            bulkCoordinator: environment.folderBulkCoordinator,
            activityStore: environment.captureActivity,
            observeActivity: { try environment.recordCaptureActivity() },
            rootGroups: { environment.rootAccessGroups() },
            documentRoutes: { environment.documentAccessRoutes() },
            historyTransport: nil,
            locateOriginal: locateOriginal
        )
    }

    private func scratchDirectory() throws -> URL {
        let root = URL(fileURLWithPath: "/private/tmp", isDirectory: true)
            .appendingPathComponent("omnivia-at50-" + UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: root) }
        return root
    }

    /// AT-50 on the control itself, not just on the decision behind it.
    ///
    /// The window used to enable and run its original-file action from two local facts —
    /// a file is selected and an owner is wired — which hold throughout a pending
    /// association. Both the enabled state and the selector's own check must now refuse,
    /// and the selector must refuse independently: a stale pass that left the button
    /// enabled is not a permission.
    @MainActor
    func testAPendingLinkageNeitherEnablesNorInvokesTheOriginalAction() throws {
        let pending = try history(
            Wire.page(
                binding: "none",
                captures: [Wire.capture(association: "snapshot_only")]
            )
        )
        var invocations = 0
        let window = try managementWindow { invocations += 1 }
        window.historyState = CoreHistoryState(
            documentID: "document-file-1", availability: .current(pending)
        )
        let item = FilesAndFoldersItem.file(CoreHistoryFixtures.file())

        XCTAssertFalse(
            window.permitsOriginalAction(for: item),
            "a requested but unconfirmed association permits no original action"
        )
        XCTAssertFalse(window.invokeOriginalAction(for: item))
        XCTAssertEqual(invocations, 0, "a pending association must never reach the native owner")

        // Nothing selected, and a selection that is not this document, are both closed
        // for the same reason: neither is a file Core has confirmed an association for.
        XCTAssertFalse(window.permitsOriginalAction(for: nil))
        XCTAssertFalse(window.invokeOriginalAction(for: nil))
        XCTAssertEqual(invocations, 0)
    }

    /// The other side of the same rule, through the same two production entry points.
    @MainActor
    func testAConfirmedAssociationEnablesAndInvokesTheOriginalActionExactlyOnce() throws {
        let confirmed = try history(Wire.page(binding: "connected"))
        var invocations = 0
        let window = try managementWindow { invocations += 1 }
        window.historyState = CoreHistoryState(
            documentID: "document-file-1", availability: .current(confirmed)
        )
        let item = FilesAndFoldersItem.file(CoreHistoryFixtures.file())

        XCTAssertTrue(window.permitsOriginalAction(for: item))
        XCTAssertTrue(window.invokeOriginalAction(for: item))
        XCTAssertEqual(invocations, 1)

        // The held answer is about exactly one document, so another file's row is not
        // covered by it however confirmed that one is.
        let other = FilesAndFoldersItem.file(CoreHistoryFixtures.file(id: "file-2"))
        XCTAssertFalse(window.permitsOriginalAction(for: other))
        XCTAssertFalse(window.invokeOriginalAction(for: other))

        // Those are the last facts Core reported, not a current permission.
        window.historyState = CoreHistoryState(
            documentID: "document-file-1", availability: .stale(confirmed)
        )
        XCTAssertFalse(window.permitsOriginalAction(for: item))
        XCTAssertFalse(window.invokeOriginalAction(for: item))
        XCTAssertEqual(invocations, 1)

        // And with no owner of native access wired there is nothing to invoke, whatever
        // Core confirms.
        let unwired = try managementWindow(locateOriginal: nil)
        unwired.historyState = CoreHistoryState(
            documentID: "document-file-1", availability: .current(confirmed)
        )
        XCTAssertFalse(unwired.permitsOriginalAction(for: item))
        XCTAssertFalse(unwired.invokeOriginalAction(for: item))
    }

    // MARK: - AT-49 and AT-50 over the real stores

    private func environment(
        in root: URL,
        codec: CountingBookmarkCodec
    ) throws -> DocumentIntakeEnvironment {
        let state = root.appendingPathComponent("state", isDirectory: true)
        try FileManager.default.createDirectory(at: state, withIntermediateDirectories: true)
        return try DocumentIntakeEnvironment(stateRoot: state, bookmarkCodec: codec)
    }

    /// AT-49. A reviewed but unsubmitted candidate holds a provisional grant. Nothing reads it,
    /// the broker refuses Refresh/Open/Reveal on it, the management window's real model and
    /// controller entry points refuse even when Core's answer alone would have offered them,
    /// and preparation is allowed for exactly that grant at exactly that generation. Its own
    /// publication promotes it, and a restart then recovers without reading the original.
    @MainActor
    func testAnUnsubmittedCandidateIsNeverReadAndServesNoOriginalActionUntilPublished() async throws {
        let root = try scratchDirectory()
        let original = root.appendingPathComponent("invoice.txt")
        try Data("invoice".utf8).write(to: original)
        let codec = CountingBookmarkCodec()
        let environment = try environment(in: root, codec: codec)
        let submission = ManualCaptureSubmission(
            environment: environment, binding: .companion(workspaceRef: "workspace-1")
        )
        let candidate = try submission.adoptOriginal(
            original, displayName: "invoice.txt", surface: .finderQuickAction
        )
        guard case let .adoptedOriginal(accessRef, generation, _) = candidate.source else {
            return XCTFail("an adopted original carries its own grant")
        }

        // Candidate reads are denied: adopting reads nothing and queues nothing.
        XCTAssertEqual(codec.starts, 0)
        XCTAssertEqual(environment.queue.allEntries(), [])
        XCTAssertEqual(
            FilesAndFoldersModel.provisionalAccessRefs(routes: environment.documentAccessRoutes()),
            [accessRef]
        )

        // The row, Core's confirmation and the window are all as permissive as they can be.
        let row = CoreHistoryFixtures.file(accessRef: accessRef)
        let item = FilesAndFoldersItem.file(row)
        let confirmed = CoreHistoryState(
            documentID: row.linkedDocumentID,
            availability: .current(try history(Wire.page(binding: "connected")))
        )
        XCTAssertTrue(
            FilesAndFoldersModel.actions(for: item, coreHistory: confirmed).originalActionsEnabled,
            "control: Core's answer alone offers the three actions"
        )
        XCTAssertFalse(
            FilesAndFoldersModel.actions(
                for: item,
                coreHistory: confirmed,
                provisionalAccessRefs: FilesAndFoldersModel.provisionalAccessRefs(
                    routes: environment.documentAccessRoutes()
                )
            ).originalActionsEnabled
        )
        var invocations = 0
        let window = try managementWindow(
            locateOriginal: { invocations += 1 }, environment: environment
        )
        window.historyState = confirmed
        XCTAssertFalse(window.permitsOriginalAction(for: item))
        XCTAssertFalse(window.invokeOriginalAction(for: item))
        XCTAssertEqual(invocations, 0)

        // The broker agrees, and a refusal reads nothing.
        XCTAssertThrowsError(
            try environment.accessStore.withReadAccess(
                accessRef: accessRef, expectedGeneration: generation, purpose: .originalAction
            ) { _ in () }
        ) { XCTAssertEqual($0 as? NativeAccessError, .routeNotActive) }
        // Preparation is scoped to its own grant at its own generation.
        XCTAssertThrowsError(
            try environment.accessStore.withReadAccess(
                accessRef: accessRef, expectedGeneration: generation + 1
            ) { _ in () }
        )
        XCTAssertThrowsError(
            try environment.accessStore.withReadAccess(
                accessRef: "access-other", expectedGeneration: generation
            ) { _ in () }
        )
        XCTAssertEqual(codec.starts, 0)

        // Publication is the only thing that promotes it, and only by preparing its own intent.
        let entries = try await submission.submit([candidate])
        XCTAssertGreaterThan(codec.starts, 0)
        XCTAssertEqual(entries.first?.intent.originalBinding?.accessRecordId, accessRef)
        XCTAssertEqual(
            FilesAndFoldersModel.provisionalAccessRefs(routes: environment.documentAccessRoutes()),
            []
        )
        XCTAssertTrue(window.permitsOriginalAction(for: item))
        XCTAssertTrue(window.invokeOriginalAction(for: item))
        XCTAssertEqual(invocations, 1)

        // Published recovery never rereads the original: it is gone, and nothing reads it.
        try FileManager.default.removeItem(at: original)
        let restartCodec = CountingBookmarkCodec()
        let restarted = try self.environment(in: root, codec: restartCodec)
        _ = await restarted.fileMonitoring.reconcileReceipts()
        _ = try restarted.recordCaptureActivity()
        XCTAssertEqual(restarted.queue.allEntries(), entries)
        XCTAssertEqual(
            try Data(contentsOf: restarted.queue.payloadURL(for: entries[0].intent.itemId)),
            Data("invoice".utf8)
        )
        XCTAssertEqual(restartCodec.starts, 0)
    }

    /// AT-50. A real keep-updated request is held pending: queued on this Mac, not yet accepted
    /// by Core. Through the window's own projection it stays progress, never a linked row or a
    /// completed capture, and a restart's reconciliation changes none of its frozen identity.
    /// Only Core accepting the capture moves it, and only then does Core's confirmation offer
    /// the original actions.
    @MainActor
    func testAHeldPendingLinkageRequestStaysProgressUntilCoreAcceptsAndKeepsItsIdentity() async throws {
        let root = try scratchDirectory()
        let original = root.appendingPathComponent("invoice.txt")
        try Data("invoice".utf8).write(to: original)
        let environment = try environment(in: root, codec: CountingBookmarkCodec())
        let entries = try await ManualCaptureSubmission(
            environment: environment, binding: .companion(workspaceRef: "workspace-1")
        ).submit([ManualCaptureCandidate(url: original, keepUpdated: true)])
        let entry = try XCTUnwrap(entries.first)
        let itemID = entry.intent.itemId
        let frozenBytes = try Data(contentsOf: environment.queue.payloadURL(for: itemID))
        let subscription = try XCTUnwrap(environment.fileSubscriptions.all().first)
        XCTAssertEqual(subscription.state, .waitingForCore)

        let pending = try history(
            Wire.page(binding: "none", captures: [Wire.capture(association: "snapshot_only")])
        )
        func project(
            _ environment: DocumentIntakeEnvironment,
            _ answer: CoreDocumentHistory
        ) throws -> FolderManagementWindowController {
            let window = try managementWindow(locateOriginal: {}, environment: environment)
            window.historyState = CoreHistoryState(
                documentID: subscription.linkedDocumentID, availability: .current(answer)
            )
            window.reload()
            return window
        }
        func assertPending(_ window: FolderManagementWindowController) throws {
            let model = window.model
            let file = try XCTUnwrap(model.items(segment: .files).first)
            XCTAssertEqual(model.items(segment: .files).count, 1)
            XCTAssertEqual(file.statusLabel, "Waiting for Core")
            XCTAssertEqual(file.statusFilter, .activeOrPending)
            XCTAssertEqual(model.items(segment: .folders), [], "no provisional active-link row")
            let facts = Dictionary(
                uniqueKeysWithValues: model.detail(for: file).map { ($0.label, $0.value) }
            )
            XCTAssertTrue(facts["Updates"]?.hasPrefix("Not confirmed") ?? false, "\(facts)")
            XCTAssertFalse(facts.values.contains { $0.contains("Kept updated") })
            let addition = try XCTUnwrap(model.items(segment: .recentAdditions).first)
            XCTAssertEqual(addition.statusLabel, "Queued on this Mac")
            XCTAssertEqual(addition.countText, "0/1", "nothing is accepted, so nothing succeeded")
            XCTAssertFalse(window.permitsOriginalAction(for: file))
            XCTAssertFalse(window.invokeOriginalAction(for: file))
        }
        try assertPending(try project(environment, pending))

        // Reconciliation after a restart: same entry, key, bytes and grant generation.
        let reopened = try self.environment(in: root, codec: CountingBookmarkCodec())
        _ = await reopened.fileMonitoring.reconcileReceipts()
        XCTAssertEqual(reopened.queue.allEntries(), entries)
        XCTAssertEqual(reopened.queue.allEntries().first?.intent.operationKey, entry.intent.operationKey)
        XCTAssertEqual(
            try Data(contentsOf: reopened.queue.payloadURL(for: itemID)), frozenBytes
        )
        let after = try XCTUnwrap(reopened.fileSubscriptions.all().first)
        XCTAssertEqual(after, subscription)
        XCTAssertEqual(
            reopened.documentAccessRoutes().first { $0.accessRef == subscription.accessRef }?.generation,
            subscription.bindingGeneration
        )
        XCTAssertEqual(
            try reopened.recordCaptureActivity().first { $0.itemID == itemID }?.operationKey,
            entry.intent.operationKey
        )
        try assertPending(try project(reopened, pending))

        // The canonical association: Core accepts the capture, and only now does the row move.
        let digest = "sha256:" + String(repeating: "d", count: 64)
        let staging = try reopened.queue.beginStage(
            itemID: itemID, requestDigest: digest, descriptorDigest: digest
        )
        _ = try reopened.queue.markAdmitted(
            itemID: itemID,
            attemptGeneration: staging.attemptGeneration,
            captureID: "dcap-" + String(repeating: "a", count: 64)
        )
        _ = await reopened.fileMonitoring.reconcileReceipts()
        let accepted = try XCTUnwrap(reopened.fileSubscriptions.all().first)
        XCTAssertEqual(accepted.state, .active)
        XCTAssertEqual(accepted.accessRef, subscription.accessRef)
        XCTAssertEqual(accepted.bindingGeneration, subscription.bindingGeneration)
        XCTAssertEqual(reopened.queue.allEntries().first?.intent.operationKey, entry.intent.operationKey)
        let linked = try project(reopened, try history(Wire.page(binding: "connected")))
        let file = try XCTUnwrap(linked.model.items(segment: .files).first)
        XCTAssertEqual(file.statusLabel, "Watching")
        XCTAssertTrue(linked.permitsOriginalAction(for: file))
    }

    // MARK: - AT-27 · Locate during preparation

    /// AT-27. A watched file's refresh is mid-preparation — its bytes are being
    /// read — when the person uses the management window's Locate action. The
    /// action is the window's own, gated by the same decision as the button, and
    /// its owner does what the companion wires it to: it puts the chosen file
    /// through review and the one Add path (the native chooser, which needs a
    /// run loop, is the only thing replaced, by a fixed pick).
    ///
    /// Locate adds the chosen file as its own logical document under a grant of
    /// its own. The refresh that was already in flight therefore publishes what
    /// it read, under the association it was prepared for, and the located file
    /// is published under its own — the chosen file's bytes never appear under
    /// the old document, grant or generation. The generation fence that guards a
    /// same-grant relink is `testGenerationChangeAfterReadsRejectsWholeBatch`.
    @MainActor
    func testLocateDuringARefreshPreparationNeverPublishesTheChosenFileUnderTheOldAssociation() async throws {
        let root = try scratchDirectory()
        let original = root.appendingPathComponent("watched.txt")
        let chosen = root.appendingPathComponent("chosen.txt")
        try Data("baseline".utf8).write(to: original)
        try Data("the file Locate chose".utf8).write(to: chosen)
        let reader = PausingReader()
        let clock = SchedulingClock()
        let state = root.appendingPathComponent("state", isDirectory: true)
        try FileManager.default.createDirectory(at: state, withIntermediateDirectories: true)
        let environment = try DocumentIntakeEnvironment(
            stateRoot: state,
            bookmarkCodec: CountingBookmarkCodec(),
            reader: reader,
            monotonicClock: clock.reader
        )
        let submission = ManualCaptureSubmission(
            environment: environment, binding: .companion(workspaceRef: "workspace-1")
        )

        // One admitted baseline, which is what makes the watch active.
        let baseline = try await submission.submit(
            [ManualCaptureCandidate(url: original, keepUpdated: true)]
        )
        let digest = "sha256:" + String(repeating: "d", count: 64)
        let staging = try environment.queue.beginStage(
            itemID: baseline[0].intent.itemId, requestDigest: digest, descriptorDigest: digest
        )
        _ = try environment.queue.markAdmitted(
            itemID: baseline[0].intent.itemId,
            attemptGeneration: staging.attemptGeneration,
            captureID: "dcap-" + String(repeating: "a", count: 64)
        )
        _ = await environment.fileMonitoring.reconcileReceipts()
        let watch = try XCTUnwrap(environment.fileSubscriptions.all().first)
        XCTAssertEqual(watch.state, .active)

        // The window, over the same stores, with Locate wired as the companion
        // wires it.
        let panel = CapturePanelController(
            workspaceName: "workspace-1",
            submitter: submission,
            folderSubmitter: nil,
            onQueued: nil,
            opensWindow: false
        )
        var located: Task<Void, Never>?
        let window = try managementWindow(
            locateOriginal: {
                panel.addIncoming([chosen])
                located = panel.beginSubmit()
            },
            environment: environment
        )
        window.historyState = CoreHistoryState(
            documentID: watch.linkedDocumentID,
            availability: .current(try history(Wire.page(binding: "connected")))
        )
        window.reload()
        let row = try XCTUnwrap(window.model.items(segment: .files).first)
        XCTAssertTrue(window.permitsOriginalAction(for: row))

        // The refresh starts preparing and stops with its original open.
        try Data("a newer version".utf8).write(to: original)
        await environment.fileMonitoring.markDirty(subscriptionIDs: [watch.subscriptionID])
        clock.advance(3_000)
        reader.arm(original.lastPathComponent)
        let refresh = Task.detached { await environment.fileMonitoring.reconcileAll() }
        await Task.detached { XCTAssertTrue(reader.waitUntilReached()) }.value

        // Locate lands while that read is in flight, and runs to completion.
        XCTAssertTrue(window.invokeOriginalAction(for: row))
        await located?.value
        reader.release()
        _ = await refresh.value

        let entries = environment.queue.allEntries()
        // An admitted entry's payload is already gone, so it reads as nil.
        func payload(_ entry: CaptureQueueEntry) -> Data? {
            try? Data(contentsOf: environment.queue.payloadURL(for: entry.intent.itemId))
        }
        let refreshed = try XCTUnwrap(entries.first {
            $0.intent.originalBinding?.accessRecordId == watch.accessRef
                && $0.intent.itemId != watch.baselineItemID
        })
        let chosenEntry = try XCTUnwrap(
            entries.first { payload($0) == Data("the file Locate chose".utf8) }
        )
        // The old association published exactly what it read, at its own generation.
        XCTAssertEqual(payload(refreshed), Data("a newer version".utf8))
        XCTAssertEqual(refreshed.intent.originalBinding?.bindingGeneration, watch.bindingGeneration)
        XCTAssertEqual(refreshed.intent.originalBinding?.linkedDocumentId, watch.linkedDocumentID)
        // The chosen file is somebody else's association entirely.
        XCTAssertNotEqual(chosenEntry.intent.originalBinding?.accessRecordId, watch.accessRef)
        XCTAssertNotEqual(chosenEntry.intent.originalBinding?.linkedDocumentId, watch.linkedDocumentID)
        XCTAssertNotEqual(chosenEntry.intent.requestedSourceIdentity, watch.requestedSourceIdentity)
        // Nothing carrying the old document, grant or source identity holds the chosen bytes,
        // and the old grant was neither relinked nor revoked by the action.
        for entry in entries where entry.intent.originalBinding?.accessRecordId == watch.accessRef {
            XCTAssertNotEqual(payload(entry), Data("the file Locate chose".utf8))
        }
        let grant = try XCTUnwrap(
            environment.accessStore.descriptors().first { $0.accessRef == watch.accessRef }
        )
        XCTAssertEqual(grant.generation, watch.bindingGeneration)
        XCTAssertEqual(grant.state, .connected)
    }
}

// MARK: - Helpers

/// Stops one named original's read until the test lets it go, and reads every
/// other file straight through. Lets a test hold a preparation mid-read.
private final class PausingReader: CoordinatedOriginalReading, @unchecked Sendable {
    private let lock = NSLock()
    private var blocked: String?
    private let reached = DispatchSemaphore(value: 0)
    private let released = DispatchSemaphore(value: 0)

    func arm(_ name: String) { lock.withLock { blocked = name } }
    func waitUntilReached() -> Bool { reached.wait(timeout: .now() + 10) == .success }
    func release() { released.signal() }

    func readStableBytes(from url: URL, maximumBytes: UInt64) throws -> Data {
        let hold = lock.withLock { () -> Bool in
            guard blocked == url.lastPathComponent else { return false }
            blocked = nil
            return true
        }
        if hold {
            reached.signal()
            _ = released.wait(timeout: .now() + 10)
        }
        return try Data(contentsOf: url)
    }
}

/// The process-monotonic clock the scheduler measures its quiet period in.
private final class SchedulingClock: @unchecked Sendable {
    private let lock = NSLock()
    private var value: Int64 = 0

    func advance(_ delta: Int64) { lock.withLock { value += delta } }
    var reader: @Sendable () -> Int64 { { [self] in self.lock.withLock { self.value } } }
}

private enum CoreHistoryFixtures {
    static func file(
        id: String = "file-1",
        name: String = "Notes.pdf",
        accessRef: String? = nil
    ) -> FileSubscription {
        FileSubscription(
            subscriptionID: id,
            accessRef: accessRef ?? "access-\(id)",
            linkedDocumentID: "document-\(id)",
            requestedSourceIdentity: "identity-\(id)",
            displayName: name,
            contentKind: .pdf,
            bindingGeneration: 1,
            baselineItemID: "baseline-\(id)",
            targetRef: "target-1",
            workspaceRef: "workspace-1",
            targetBindingRevision: "binding-1",
            callerContextBinding: CallerContextBinding(
                clientContextRef: "client-1",
                principalRef: "principal-1",
                contextRevision: "context-1"
            ),
            configGeneration: 1,
            consentGeneration: 1,
            consentRecordedAt: "2026-10-14T00:00:00Z",
            state: .active,
            currentSuccessfulCaptureID: nil,
            lastObservedDigest: nil,
            lastObservedByteLength: nil,
            lastObservedAt: nil,
            lastQueuedItemID: nil,
            dirtyEpoch: 1,
            needsReconciliation: false,
            lastCheckedAt: "2026-10-15T09:00:00Z"
        )
    }
}

/// Hands back canned pages in order and records the token each request presented, so a
/// token cannot be silently dropped, reused or invented.
private final class RecordingTransport: CoreDocumentHistoryTransport, @unchecked Sendable {
    private let lock = NSLock()
    private var remaining: [CoreDocumentHistoryPage]
    private let failure: CoreHistoryError?
    private var presented: [String?] = []

    init(pages: [CoreDocumentHistoryPage], failure: CoreHistoryError? = nil) {
        remaining = pages
        self.failure = failure
    }

    var tokens: [String?] { lock.withLock { presented } }

    func history(
        documentID: String,
        limit: Int,
        continuationToken: String?
    ) -> Result<CoreDocumentHistoryPage, CoreHistoryError> {
        lock.withLock {
            presented.append(continuationToken)
            guard !remaining.isEmpty else { return .failure(failure ?? .unreadable) }
            return .success(remaining.removeFirst())
        }
    }

    func reprocess(
        captureID: String,
        profileRef: String,
        revision: String,
        expectedProfileDigest: String,
        expectedBaseExtractionID: String?,
        operationKey: String
    ) -> Result<CoreReprocessReceipt, CoreHistoryError> {
        .failure(.unreadable)
    }
}

/// Records the one reprocess request it is handed, so the fields the surface actually sends
/// are asserted rather than assumed from the intent it built.
private final class CapturingReprocessTransport: CoreDocumentHistoryTransport, @unchecked Sendable {
    private(set) var sentDigest: String?
    private(set) var sentKey: String?

    func history(
        documentID: String,
        limit: Int,
        continuationToken: String?
    ) -> Result<CoreDocumentHistoryPage, CoreHistoryError> {
        .failure(.unreadable)
    }

    func reprocess(
        captureID: String,
        profileRef: String,
        revision: String,
        expectedProfileDigest: String,
        expectedBaseExtractionID: String?,
        operationKey: String
    ) -> Result<CoreReprocessReceipt, CoreHistoryError> {
        sentDigest = expectedProfileDigest
        sentKey = operationKey
        return .failure(.unreadable)
    }
}

extension Result where Failure == CoreHistoryError {
    var failureError: CoreHistoryError? {
        if case let .failure(error) = self { return error }
        return nil
    }
}

private final class CountingBookmarkCodec: SecurityScopedBookmarkCoding, @unchecked Sendable {
    private(set) var starts = 0

    func makeReadOnlyBookmark(for url: URL) throws -> Data { Data(url.path.utf8) }

    func resolveReadOnlyBookmark(_ data: Data) throws -> (url: URL, stale: Bool) {
        (URL(fileURLWithPath: String(decoding: data, as: UTF8.self)), false)
    }

    func startAccessing(_ url: URL) -> Bool {
        starts += 1
        return true
    }

    func stopAccessing(_ url: URL) {}
}
