-- Phase 2 resolution: lossless source-derived TemporalInstant persistence.
--
-- These adjacent columns extend the immutable evidence, observation and
-- assertion records created by 0027. Existing rows remain valid with null
-- auxiliary metadata. New source-derived values are validated by the typed
-- temporal contract before insertion; these column constraints independently
-- enforce the storage representation, size limits and null relationships.
-- Existing append-only and writer-fence triggers continue to guard the tables.

ALTER TABLE omnivia_semantic_evidence_items
ADD COLUMN source_time_original_text TEXT
CHECK (source_time_original_text IS NULL OR
       (source_time_us IS NOT NULL AND typeof(source_time_original_text) = 'text'
        AND length(source_time_original_text) BETWEEN 1 AND 2048));

ALTER TABLE omnivia_semantic_evidence_items
ADD COLUMN source_time_timezone TEXT
CHECK (source_time_timezone IS NULL OR
       (source_time_us IS NOT NULL AND source_time_original_text IS NOT NULL
        AND typeof(source_time_timezone) = 'text'
        AND length(source_time_timezone) BETWEEN 1 AND 255));

ALTER TABLE omnivia_semantic_observations
ADD COLUMN source_time_original_text TEXT
CHECK (source_time_original_text IS NULL OR
       (source_time_us IS NOT NULL AND typeof(source_time_original_text) = 'text'
        AND length(source_time_original_text) BETWEEN 1 AND 2048));

ALTER TABLE omnivia_semantic_observations
ADD COLUMN source_time_timezone TEXT
CHECK (source_time_timezone IS NULL OR
       (source_time_us IS NOT NULL AND source_time_original_text IS NOT NULL
        AND typeof(source_time_timezone) = 'text'
        AND length(source_time_timezone) BETWEEN 1 AND 255));

ALTER TABLE omnivia_semantic_assertions
ADD COLUMN valid_from_original_text TEXT
CHECK (valid_from_original_text IS NULL OR
       (valid_from_us IS NOT NULL AND typeof(valid_from_original_text) = 'text'
        AND length(valid_from_original_text) BETWEEN 1 AND 2048));

ALTER TABLE omnivia_semantic_assertions
ADD COLUMN valid_from_timezone TEXT
CHECK (valid_from_timezone IS NULL OR
       (valid_from_us IS NOT NULL AND valid_from_original_text IS NOT NULL
        AND typeof(valid_from_timezone) = 'text'
        AND length(valid_from_timezone) BETWEEN 1 AND 255));

ALTER TABLE omnivia_semantic_assertions
ADD COLUMN valid_to_original_text TEXT
CHECK (valid_to_original_text IS NULL OR
       (valid_to_us IS NOT NULL AND typeof(valid_to_original_text) = 'text'
        AND length(valid_to_original_text) BETWEEN 1 AND 2048));

ALTER TABLE omnivia_semantic_assertions
ADD COLUMN valid_to_timezone TEXT
CHECK (valid_to_timezone IS NULL OR
       (valid_to_us IS NOT NULL AND valid_to_original_text IS NOT NULL
        AND typeof(valid_to_timezone) = 'text'
        AND length(valid_to_timezone) BETWEEN 1 AND 255));

ALTER TABLE omnivia_semantic_assertions
ADD COLUMN attested_from_original_text TEXT
CHECK (attested_from_original_text IS NULL OR
       (attested_from_us IS NOT NULL AND typeof(attested_from_original_text) = 'text'
        AND length(attested_from_original_text) BETWEEN 1 AND 2048));

ALTER TABLE omnivia_semantic_assertions
ADD COLUMN attested_from_timezone TEXT
CHECK (attested_from_timezone IS NULL OR
       (attested_from_us IS NOT NULL AND attested_from_original_text IS NOT NULL
        AND typeof(attested_from_timezone) = 'text'
        AND length(attested_from_timezone) BETWEEN 1 AND 255));

ALTER TABLE omnivia_semantic_assertions
ADD COLUMN attested_to_original_text TEXT
CHECK (attested_to_original_text IS NULL OR
       (attested_to_us IS NOT NULL AND typeof(attested_to_original_text) = 'text'
        AND length(attested_to_original_text) BETWEEN 1 AND 2048));

ALTER TABLE omnivia_semantic_assertions
ADD COLUMN attested_to_timezone TEXT
CHECK (attested_to_timezone IS NULL OR
       (attested_to_us IS NOT NULL AND attested_to_original_text IS NOT NULL
        AND typeof(attested_to_timezone) = 'text'
        AND length(attested_to_timezone) BETWEEN 1 AND 255));
