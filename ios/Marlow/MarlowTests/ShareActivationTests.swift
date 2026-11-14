import Foundation
import Testing
import UIKit
@testable import Marlow

/// The share sheet's `NSExtensionActivationRule`: a predicate STRING, because
/// `TRUEPREDICATE` is rejected by App Review. Marlow is offered for one to six
/// items of any type, for a web URL, and for plain text, and for nothing else.
/// Which type of file is never part of it: the server reads whatever arrives
/// and labels what it can't open.
struct ShareActivationTests {

    /// `NSPredicate(format:)` raises an Objective-C exception on a malformed
    /// string, which would crash the test process; the share sheet itself
    /// would silently never list Marlow.
    @Test func thePredicateParses() {
        let predicate = ShareActivation.makePredicate()
        #expect(predicate.predicateFormat.contains("SUBQUERY"))
        #expect(ShareActivation.predicate != "TRUEPREDICATE")
    }

    /// One constant for the composer's cap and the share sheet's: the string is
    /// built from it, so the sheet can never offer seven.
    @Test func theCountIsCappedAtTheSharedSix() {
        #expect(ShareActivation.predicate.contains("<= \(AttachmentLimits.maxItems)"))
        #expect(ShareActivation.predicate.contains(">= 1"))
        #expect(AttachmentLimits.maxItems == 6)
    }

    /// The Info.plist is a literal in project.yml (a build setting cannot call
    /// Swift), so this is what keeps the two from drifting: the built
    /// extension's rule must be the string the app was tested with.
    @Test func theBuiltExtensionCarriesTheSamePredicate() throws {
        let plugIns = try #require(Bundle.main.builtInPlugInsURL, "the app embeds its extensions")
        let appexes = (try? FileManager.default.contentsOfDirectory(at: plugIns, includingPropertiesForKeys: nil)) ?? []
        let share = try #require(appexes.first { $0.lastPathComponent == "MarlowShare.appex" },
                                 "MarlowShare is embedded in the app")
        let info = try #require(Bundle(url: share)?.infoDictionary)
        let extensionInfo = try #require(info["NSExtension"] as? [String: Any])
        #expect(extensionInfo["NSExtensionPointIdentifier"] as? String == "com.apple.share-services")
        let attributes = try #require(extensionInfo["NSExtensionAttributes"] as? [String: Any])
        let rule = try #require(attributes["NSExtensionActivationRule"] as? String,
                                "a predicate string, not a dictionary and not TRUEPREDICATE")

        #expect(Self.collapse(rule) == Self.collapse(ShareActivation.predicate))
        #expect(Bundle(url: share)?.bundleIdentifier == "com.mikemason.Marlow.Share")
    }

    private static func collapse(_ text: String) -> String {
        text.split(whereSeparator: \.isWhitespace).joined(separator: " ")
    }

    // MARK: - What it matches

    /// Evaluated the way the system does, against the extension items'
    /// registered type identifiers.
    private func offered(_ items: [[String]]) throws -> Bool {
        let predicate = ShareActivation.makePredicate()
        let extensionItems = items.map { types in
            ["attachments": types.map { ["registeredTypeIdentifiers": [$0]] }] as NSDictionary
        }
        return predicate.evaluate(with: ["extensionItems": extensionItems] as NSDictionary)
    }

    @Test func oneFileOfAnyKindIsOffered() throws {
        #expect(try offered([["com.adobe.pdf"]]))
        #expect(try offered([["org.openxmlformats.wordprocessingml.document"]]))
        #expect(try offered([["com.apple.iwork.numbers.sffnumbers"]]), "a type nobody here has heard of")
        #expect(try offered([["public.jpeg"]]))
    }

    @Test func sixAreOfferedAndSevenAreNot() throws {
        #expect(try offered([Array(repeating: "com.adobe.pdf", count: 6)]))
        #expect(try offered([Array(repeating: "com.adobe.pdf", count: 7)]) == false)
    }

    @Test func nothingSharedIsNotOffered() throws {
        #expect(try offered([[]]) == false)
        #expect(try offered([]) == false)
    }

    @Test func aWebURLAndPlainTextAreOffered() throws {
        #expect(try offered([["public.url"]]))
        #expect(try offered([["public.plain-text"]]))
        #expect(try offered([["public.utf8-plain-text"]]))
    }
}
