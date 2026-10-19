-- The processing preferences one capture was admitted under (0071).
--
-- Additive only. One nullable column on the immutable capture row created by 0069.
-- No new table, no new trigger: `omnivia_document_captures` is already append-only
-- (UPDATE and DELETE are unconditional ABORTs, 0069), so a value written at INSERT
-- is immutable by construction and needs no guard of its own.
--
-- Why the capture row and not a late read
-- ---------------------------------------
--
-- Before this column the worker resolved the recognition languages and the OCR
-- catalogue from process-global configuration at the moment it picked the obligation
-- up. A preference changed between admission and processing therefore changed what an
-- already queued capture would do -- and because the extraction's operation key is
-- derived from the resolved profile digest, it changed the key as well, so the capture
-- settled under a profile identity nobody had been offered. Freezing the preferences
-- into the capture at admission makes the queued obligation, the immutable profile the
-- worker executes, and the identity `document.history` reports all derive from one
-- snapshot taken at one instant.
--
-- It belongs on the capture rather than on the outbox because it outlives the
-- obligation: `document.history` reports what a settled capture was admitted under long
-- after its outbox row has been settled, and a reprocess of that capture needs to be
-- distinguishable from the original admission's selection.
--
-- Why nullable, and what NULL means
-- ---------------------------------
--
-- A capture admitted before this migration has no recorded selection, and inventing one
-- retrospectively would be a claim about what ran. NULL therefore means exactly "this
-- capture recorded no selection", and the worker falls back to the behaviour that
-- applied when such a capture was admitted. New captures always carry a value.
--
-- Why JSON rather than three columns
-- ----------------------------------
--
-- The snapshot is read and written as one whole -- there is no query that selects
-- captures by recognition language -- and its members are the settings document's, which
-- is itself one JSON value under one key in `omnivia_app_settings`. Three columns would
-- make every later preference a migration against the capture table; one document makes
-- the two representations the same shape, so a stored preference and a frozen one cannot
-- disagree about what a member is called.

ALTER TABLE omnivia_document_captures
ADD COLUMN processing_settings_json TEXT
CHECK (processing_settings_json IS NULL
       OR (typeof(processing_settings_json) = 'text'
           AND length(CAST(processing_settings_json AS BLOB)) BETWEEN 2 AND 4096
           AND json_valid(processing_settings_json)
           AND json_type(processing_settings_json) = 'object'
           AND instr(processing_settings_json, char(0)) = 0));
