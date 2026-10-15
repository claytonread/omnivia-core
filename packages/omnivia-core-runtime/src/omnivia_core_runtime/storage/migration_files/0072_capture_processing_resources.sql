-- The exact OCR resource identities one capture was admitted against (0072).
--
-- Additive only. One nullable column on the immutable capture row created by 0069,
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
-- This column closes it. The ordered resource-set identities an OCR capture is
-- admitted against are resolved once, at admission, against the installation's managed
-- cache, and written here; the same admission durably pins those exact identities in
-- the managed resource store, so the bytes cannot be removed or evicted while the
-- obligation is unresolved. Execution serves exactly these identities or refuses with
-- a resource condition. It never resolves whatever happens to be installed when the
-- obligation's turn comes.
--
-- Why the identities and not the digest
-- -------------------------------------
--
-- Storing the resolved profile digest would record the *answer* while leaving the
-- question unanswerable: a digest cannot be checked against bytes, cannot say which
-- model revision it was taken over, and cannot be pinned. The five-field identity --
-- language, engine revision, model revision, byte size, content digest -- is what the
-- managed store installs, pins and verifies, so freezing it is what makes the digest
-- derived from it reproducible. The digest therefore stays derived and is never a
-- second copy of the truth that could disagree with the first.
--
-- Why nullable, and what NULL means
-- ---------------------------------
--
-- Exactly 0071's reasoning, one migration later. A capture admitted before this
-- column recorded no resource selection, and inventing identities for it
-- retrospectively would be a claim about bytes nobody read. NULL therefore means
-- "this capture recorded no resource selection", and such a capture resolves against
-- the packaged fail-closed catalogue -- which installs no model and names no model
-- directory -- rather than against the installation's current cache. That is
-- deterministic, invents no identity, and is the one behaviour a model installed
-- after the fact cannot change. New captures always carry a value, including the
-- captures that pin nothing: a text-only capture records that it resolved no
-- resources, so "pinned nothing deliberately" and "recorded nothing at all" stay
-- distinguishable facts rather than one ambiguous NULL.
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

ALTER TABLE omnivia_document_captures
ADD COLUMN processing_resources_json TEXT
CHECK (processing_resources_json IS NULL
       OR (typeof(processing_resources_json) = 'text'
           AND length(CAST(processing_resources_json AS BLOB)) BETWEEN 2 AND 4096
           AND json_valid(processing_resources_json)
           AND json_type(processing_resources_json) = 'object'
           AND instr(processing_resources_json, char(0)) = 0));
