# Obsidian-like Compatibility

Compatibility here means representation-only.

Core can represent the following Obsidian-like concepts without depending on
Obsidian itself:

- notes
- wikilinks
- derived backlinks
- tags
- frontmatter-derived properties
- canvas/card-like objects through namespaced extensions
- embedded files
- note-to-task and note-to-note links
- source citations for note-derived facts

Mapping boundary:

- Core stores the portable contract shape
- a future importer may map vault artifacts into that shape
- Core does not parse markdown, scan a vault, run plugins, publish notes, or sync

See the static fixture proof:

- [Obsidian-like note graph fixture](../../services/omnivia-memory/tests/fixtures/knowledge/positive/obsidian_like_note_graph.json)
