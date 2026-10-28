import Foundation

/// What `deploy/pair-device.sh` prints and puts in its QR code:
/// `https://<box>:8443/#pair=<invite>`. One link serves both clients: an
/// Android camera opens it in Chrome, and this app splits it into the server
/// to talk to and the single-use invite to redeem there.
///
/// The invite is in the fragment, which a browser never sends, so it stays out
/// of the box's logs and off the Funnel path (docs/tasks/funnel-ingress.md).
/// Never log it, and never keep it past the pairing call.
struct PairingLink: Equatable {
    /// `scheme://host[:port]`, no path: what `PairedHost` stores.
    let host: String
    let invite: String

    // `secrets.token_urlsafe(32)` is 43 base64url characters. Loose bounds, so
    // a longer invite later still parses; this only has to reject junk early.
    private static func isInvite(_ s: String) -> Bool {
        (16...128).contains(s.utf8.count) && s.utf8.allSatisfy { c in
            (c >= 48 && c <= 57) || (c >= 65 && c <= 90) || (c >= 97 && c <= 122) || c == 45 || c == 95
        }
    }

    /// Takes a scanned or pasted link, or nil. https only, except plain http to
    /// the machine itself (a dev server): the invite must not cross a network
    /// in the clear.
    static func parse(_ text: String) -> PairingLink? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let components = URLComponents(string: trimmed),
              let scheme = components.scheme?.lowercased(),
              let host = components.host, !host.isEmpty,
              let fragment = components.fragment
        else { return nil }
        let loopback = ["localhost", "127.0.0.1", "::1"].contains(host.lowercased())
        guard scheme == "https" || (scheme == "http" && loopback) else { return nil }

        // A fragment is "k=v&k=v"; take pair=.
        let invite = fragment.split(separator: "&")
            .compactMap { part -> String? in
                guard part.hasPrefix("pair=") else { return nil }
                return String(part.dropFirst("pair=".count))
            }
            .first
        guard let invite, isInvite(invite) else { return nil }

        var origin = URLComponents()
        origin.scheme = scheme
        origin.host = host
        origin.port = components.port
        guard let url = origin.url else { return nil }
        return PairingLink(host: url.absoluteString, invite: invite)
    }
}
