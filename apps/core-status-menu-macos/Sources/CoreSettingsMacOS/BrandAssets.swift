// ============================================================================
// OmniVia brand assets (Resources/brand — the attached brand pack).
// ============================================================================

import AppKit

public enum BrandAssets {
    /// Load a brand SVG from the target's resource bundle by name.
    public static func image(named name: String) -> NSImage? {
        guard let url = Bundle.module.url(
            forResource: name, withExtension: "svg", subdirectory: "Resources/brand"
        ) else { return nil }
        return NSImage(contentsOf: url)
    }
}
