import Foundation
import Testing
@testable import Marlow

@MainActor
@Suite(.serialized) // APIClient.onUnauthorized is process-global
struct SessionStoreTests {
    @Test func aRefusedTokenThatIsNotTheCurrentOneChangesNothing() {
        // The test host holds no paired token, so any token is "not current":
        // the case of a slow request from an earlier pairing arriving late.
        let session = SessionStore()
        session.handleUnauthorized("some-old-token")
        #expect(session.notice == nil)
    }

    @Test func theStoreRegistersItselfForUnauthorizedAnswers() {
        APIClient.onUnauthorized = nil
        _ = SessionStore()
        #expect(APIClient.onUnauthorized != nil)
        APIClient.onUnauthorized = nil
    }
}
