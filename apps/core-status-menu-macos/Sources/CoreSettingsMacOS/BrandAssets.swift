// ============================================================================
// OmniVia brand assets (Resources/brand — the attached brand pack).
// ============================================================================

import AppKit

public enum BrandAssets {
    /// Load a brand SVG from the target's resource bundle by name.
    ///
    /// `.copy("Resources/brand")` puts the folder in the bundle under its last
    /// path component, and `Bundle.module` is already rooted at the bundle's
    /// resource directory — so the subdirectory to ask for is `brand`, not
    /// `Resources/brand`. Asking for the latter silently returned nil for
    /// every asset, and because the caller's fallback drew at zero size, the
    /// result was no mark at all rather than a visible failure.
    ///
    /// The bare-name lookup after it is the belt to that braces: if the copy
    /// rule is ever changed to flatten the folder, the assets keep resolving.
    public static func image(named name: String) -> NSImage? {
        let url = Bundle.module.url(
            forResource: name, withExtension: "svg", subdirectory: "brand"
        ) ?? Bundle.module.url(forResource: name, withExtension: "svg")
        guard let url else { return nil }
        return NSImage(contentsOf: url)
    }
}
