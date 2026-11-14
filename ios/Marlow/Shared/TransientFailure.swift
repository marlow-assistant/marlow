import Foundation

/// Whether a failed send is worth trying again, for the app's send loop and
/// the share extension alike: a dropped connection or the server's own trouble
/// is, a request the server has looked at and refused is not (retrying it just
/// delays the same answer).
enum TransientFailure {
    static func isTransient(_ code: URLError.Code) -> Bool {
        switch code {
        case .notConnectedToInternet, .networkConnectionLost, .timedOut,
             .cannotConnectToHost, .cannotFindHost, .dnsLookupFailed,
             .dataNotAllowed, .internationalRoamingOff, .callIsActive,
             .secureConnectionFailed:
            return true
        default:
            return false
        }
    }

    /// 0 is the app's own stand-in for "no HTTP response at all" (a
    /// connection-level failure wearing a status costume, not a real code). 5xx
    /// is the server's own transient trouble, not a rejection of the request.
    static func isTransient(status: Int) -> Bool {
        status == 0 || status >= 500
    }

    static func isTransient(_ error: Error) -> Bool {
        (error as? URLError).map { isTransient($0.code) } ?? false
    }
}
