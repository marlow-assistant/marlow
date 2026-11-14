import Foundation

/// The share extension's `NSExtensionActivationRule`.
///
/// A predicate STRING: the dictionary form cannot say "one to six files of any
/// type", and `TRUEPREDICATE`, the usual shortcut, is rejected by App Review.
/// Marlow is offered when something is shared that is
///
/// - one to `AttachmentLimits.maxItems` items of any type (`public.item`, the
///   root of every file and data type, so no type list lives here and a new
///   one never needs a release; the server reads whatever arrives and labels
///   what it can't open), or
/// - a web URL, or
/// - plain text.
///
/// A URL and text are items too. They are named anyway so a Safari page or a
/// text selection is still offered when more rides along in the same share,
/// which the count would otherwise refuse.
///
/// **This is the one place the string is written in Swift; `project.yml`
/// carries a literal copy**, because a build setting cannot call Swift.
/// `ShareActivationTests` reads the built extension's Info.plist and fails if
/// the two differ, and parses this one so a typo cannot ship a sheet that
/// silently never lists Marlow.
enum ShareActivation {
    private static func attachments(conformingTo type: String) -> String {
        "SUBQUERY ($extensionItem.attachments, $attachment, "
            + "ANY $attachment.registeredTypeIdentifiers UTI-CONFORMS-TO \"\(type)\").@count"
    }

    private static func anyItem(conformingTo type: String) -> String {
        "SUBQUERY (extensionItems, $extensionItem, \(attachments(conformingTo: type)) >= 1).@count >= 1"
    }

    static let predicate: String = {
        let items = attachments(conformingTo: "public.item")
        let itemsInRange = "SUBQUERY (extensionItems, $extensionItem, \(items) >= 1 AND \(items) <= \(AttachmentLimits.maxItems)).@count >= 1"
        return [itemsInRange, anyItem(conformingTo: "public.url"), anyItem(conformingTo: "public.plain-text")]
            .joined(separator: " OR ")
    }()

    /// Parsing is the test: `NSPredicate(format:)` raises an Objective-C
    /// exception on a malformed string, which takes the test run down loudly
    /// instead of shipping a sheet that never lists Marlow.
    static func makePredicate() -> NSPredicate {
        NSPredicate(format: predicate)
    }
}
