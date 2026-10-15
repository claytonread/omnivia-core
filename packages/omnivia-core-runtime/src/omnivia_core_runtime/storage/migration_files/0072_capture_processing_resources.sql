-- The exact OCR resources, and the exact profile, one capture was admitted with (0072).
--
-- Additive only. Two nullable columns on the immutable capture row created by 0069,
-- beside the preferences 0071 froze onto it. No new table and no new trigger, for
-- the reason 0071 gives: `omnivia_document_captures` is already append-only (UPDATE
-- and DELETE are unconditional ABORTs, 0069), so a value written at INSERT is
-- immutable by construction.
--
-- What 0071 left open
-- -------------------
--
-- 0071 froze the *intent* -- the mode, the ordered recognition languages, whether
-- embedded images are examined -- and deliberately resolved nothing against the
-- installation, because whether a model is installed was said to have a true answer
-- only at execution. That left the resource half of the identity floating. A capture
-- admitted while `deu` was installed at one model revision and executed after the
-- operator replaced that revision resolved against the revision installed *then*:
-- nothing was substituted silently, but the model hashes the profile pinned -- and so
-- the profile digest, and so the extraction's operation key, which is derived from it
-- -- were not the ones the same row would have produced at admission. A language
-- removed in that window resolved as a missing language instead.
--
-- These columns close it. The ordered resource-set identities an OCR capture is
-- admitted against are resolved once, at admission, against the installation's managed
-- cache, and written here; the same admission durably pins those exact identities in
-- the managed resource store, so the bytes cannot be removed or evicted while the
-- obligation is unresolved. Execution serves exactly these identities or refuses with
-- a resource condition. It never resolves whatever happens to be installed when the
-- obligation's turn comes, and it checks the pin rather than writing it, so a claim
-- that is gone pauses the obligation instead of being recreated beneath it.
--
-- And the digest those two documents resolved to is written beside them, so the one
-- input that is not on the row -- this build's own adapters, decoders and engine -- is
-- compared too rather than assumed.
--
-- Why the identities, and why the digest as well
-- ----------------------------------------------
--
-- The identities are the decision; the digest is a seal over it. A digest alone would
-- record the *answer* while leaving the question unanswerable: it cannot be checked
-- against bytes, cannot say which model revision it was taken over, and cannot be
-- pinned. The five-field identity -- language, engine revision, model revision, byte
-- size, content digest -- is what the managed store installs, pins and verifies, so
-- freezing it is what makes the profile derived from it reproducible. Which is why
-- `processing_resources_json` is the authority and nothing reads the digest column to
-- decide what to execute.
--
-- But "the same inputs" is only half of "the same profile". The profile is also the
-- build's own: the adapter and its version, the PDF and image decoder builds, the
-- engine revision, the parser features, the limits actually enforced. Those are not on
-- the row and cannot be, and a capture admitted by one build and executed by another
-- after an upgrade would resolve its unchanged frozen decision to a *different*
-- profile -- and therefore a different digest, and therefore a different extraction
-- operation key, which is derived from it. END-02 requires the digest offered or
-- derived at admission to remain unchanged, so the admitted digest is written here and
-- execution compares what it resolves against it. Equal is the ordinary case and the
-- work proceeds; unequal pauses the obligation with its outbox row pending and its
-- pins held, because settling under the new digest would record an extraction under an
-- identity nobody was offered and abandon the replay of the one that was.
--
-- The two columns therefore cannot disagree about anything: the digest is not a second
-- copy of the decision, it is a statement about which build resolved it. It is written
-- from exactly the settings and identities being written beside it, in the same
-- function and the same instant, so it is never a second consultation of the live
-- preferences or of the installation's cache.
--
-- Why nullable, and what NULL means
-- ---------------------------------
--
-- Exactly 0071's reasoning, one migration later, and it applies to both columns
-- together: a capture admitted before them recorded neither, so the two are NULL or
-- neither is, and a stored digest always has the decision it was taken over beside it.
--
-- A capture admitted before these
-- columns recorded no resource selection, and inventing identities for it
-- retrospectively would be a claim about bytes nobody read. NULL therefore means
-- "this capture recorded no resource selection", and such a capture resolves against
-- the packaged fail-closed catalogue -- which installs no model and names no model
-- directory -- rather than against the installation's current cache. That is
-- deterministic, invents no identity, and is the one behaviour a model installed
-- after the fact cannot change. New captures always carry a value, including the
-- captures that pin nothing: a text-only capture records that it resolved no
-- resources, so "pinned nothing deliberately" and "recorded nothing at all" stay
-- distinguishable facts rather than one ambiguous NULL. The same captures always carry
-- a digest, the text-only ones included: a text-only profile has an identity too, and
-- it is as capable of moving under an upgrade as a recognising one.
--
-- Why JSON rather than columns
-- ----------------------------
--
-- The selection is read and written as one whole, it is ordered, and it holds up to
-- four identities of five fields each -- `deu+eng+fra` plus the orientation data is
-- the widest selection §14.5 allows. No query selects captures by model digest. The
-- bound below is the same 4096 bytes 0071 uses, which is ample for four identities
-- whose every field is a token of at most 64 characters and far short of what a
-- malformed value would need to become a storage problem.
--
-- The digest is not a document and gets a column of its own, shaped exactly like every
-- other profile digest 0069 stores: `sha256:` and 64 lower-case hex digits. The same
-- CHECK, so a value this column accepts is a value `omnivia_extraction_profiles` would
-- accept as a key, and a comparison between them is a comparison of like with like.

ALTER TABLE omnivia_document_captures
ADD COLUMN processing_resources_json TEXT
CHECK (processing_resources_json IS NULL
       OR (typeof(processing_resources_json) = 'text'
           AND length(CAST(processing_resources_json AS BLOB)) BETWEEN 2 AND 4096
           AND json_valid(processing_resources_json)
           AND json_type(processing_resources_json) = 'object'
           AND instr(processing_resources_json, char(0)) = 0));

ALTER TABLE omnivia_document_captures
ADD COLUMN processing_profile_digest TEXT
CHECK (processing_profile_digest IS NULL
       OR (typeof(processing_profile_digest) = 'text'
           AND length(processing_profile_digest) = 71
           AND substr(processing_profile_digest, 1, 7) = 'sha256:'
           AND substr(processing_profile_digest, 8) NOT GLOB '*[^0-9a-f]*'));
