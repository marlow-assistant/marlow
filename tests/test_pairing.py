import pytest

from server.pairing import PairingError, PairingStore, pairing_store


def test_happy_path_pairing_issues_a_working_device_token():
    invite = pairing_store.issue_invite("iPhone")
    token = pairing_store.redeem(invite, "iPhone")
    assert pairing_store.is_valid_token(token)
    assert pairing_store.device_name_for(token) == "iPhone"


def test_the_invite_is_held_only_as_a_hash():
    invite = pairing_store.issue_invite("iPhone")
    assert invite not in pairing_store._invites
    assert invite not in repr(pairing_store._invites)


def test_unknown_invite_is_rejected():
    pairing_store.issue_invite("iPhone")
    with pytest.raises(PairingError):
        pairing_store.redeem("not-a-real-invite", "iPhone")


def test_invite_cannot_be_redeemed_twice():
    invite = pairing_store.issue_invite("iPhone")
    pairing_store.redeem(invite, "iPhone")
    with pytest.raises(PairingError):
        pairing_store.redeem(invite, "iPhone")


def test_expired_invite_is_rejected_and_looks_like_any_other_failure():
    invite = pairing_store.issue_invite("iPhone")
    # Simulate time passing without a real sleep.
    for entry in pairing_store._invites.values():
        entry["created_at"] -= 999999
    with pytest.raises(PairingError) as expired:
        pairing_store.redeem(invite, "iPhone")
    with pytest.raises(PairingError) as unknown:
        pairing_store.redeem("nope", "iPhone")
    assert str(expired.value) == str(unknown.value)


def test_racing_redemptions_of_one_invite_yield_one_token():
    import threading

    store = PairingStore()
    invite = store.issue_invite("iPhone")
    results: list[object] = []

    def go():
        try:
            results.append(store.redeem(invite, "iPhone"))
        except PairingError:
            results.append(None)

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(r is not None for r in results) == 1


def test_a_blank_device_name_falls_back_to_the_one_on_the_invite():
    token = pairing_store.redeem(pairing_store.issue_invite("Kitchen iPad"), "  ")
    assert pairing_store.device_name_for(token) == "Kitchen iPad"


def test_revoke_removes_the_token_and_its_push_registrations():
    token = pairing_store.redeem(pairing_store.issue_invite("iPhone"), "iPhone")
    pairing_store.set_apns_token(token, "apns")
    pairing_store.set_webpush_subscription(token, {"endpoint": "https://push.example/x", "keys": {}})
    assert pairing_store.revoke(token) is True
    assert not pairing_store.is_valid_token(token)
    assert pairing_store.apns_token_for(token) is None
    assert pairing_store.webpush_subscription_for(token) is None
    assert pairing_store.revoke(token) is False


def test_revoke_is_persisted(monkeypatch, tmp_path):
    from server import config

    monkeypatch.setattr(
        "server.pairing.settings", type(config.settings)(data_dir=tmp_path, log_dir=tmp_path)
    )
    store = PairingStore()
    token = store.redeem(store.issue_invite("iPad"), "iPad")
    store.revoke(token)
    assert not PairingStore().is_valid_token(token)


def test_list_and_revoke_by_fingerprint_never_expose_the_token():
    store = PairingStore()
    token = store.redeem(store.issue_invite("Lost phone"), "Lost phone")
    listing = store.list_devices()
    assert token not in repr(listing)
    mine = [d for d in listing if d["device_name"] == "Lost phone"]
    assert len(mine) == 1
    assert store.revoke_by_fingerprint(mine[0]["fingerprint"]) is True
    assert not store.is_valid_token(token)
    assert store.revoke_by_fingerprint("ffffffff") is False


def test_all_device_tokens_lists_every_paired_device():
    store = PairingStore()
    token1 = store.redeem(store.issue_invite("iPhone"), "iPhone")
    token2 = store.redeem(store.issue_invite("iPad"), "iPad")

    # Other tests share the same persisted device_tokens.json (see
    # test_device_tokens_persist_across_store_instances below), so assert
    # containment rather than equality.
    assert {token1, token2} <= set(store.all_device_tokens())


def test_device_tokens_persist_across_store_instances(monkeypatch, tmp_path):
    from server import config

    fake_settings = type(config.settings)(data_dir=tmp_path, log_dir=tmp_path)
    monkeypatch.setattr("server.pairing.settings", fake_settings)

    store_a = PairingStore()
    token = store_a.redeem(store_a.issue_invite("iPad"), "iPad")

    store_b = PairingStore()  # fresh instance, same data_dir -> should reload the token
    assert store_b.is_valid_token(token)
    assert store_b.device_name_for(token) == "iPad"


def test_apns_token_round_trip():
    token = pairing_store.redeem(pairing_store.issue_invite("iPhone"), "iPhone")

    assert pairing_store.apns_token_for(token) is None
    pairing_store.set_apns_token(token, "fake-apns-token")
    assert pairing_store.apns_token_for(token) == "fake-apns-token"


def test_apns_token_is_updatable_after_pairing_for_rotation():
    token = pairing_store.redeem(pairing_store.issue_invite("iPhone"), "iPhone")

    pairing_store.set_apns_token(token, "first-apns-token")
    pairing_store.set_apns_token(token, "rotated-apns-token")
    assert pairing_store.apns_token_for(token) == "rotated-apns-token"


def test_set_apns_token_on_unknown_device_token_is_a_noop():
    # No route can reach this (require_device_token already validated), but
    # exercised directly to confirm it doesn't raise.
    pairing_store.set_apns_token("not-a-real-device-token", "fake-apns-token")
    assert pairing_store.apns_token_for("not-a-real-device-token") is None


def test_clear_apns_token_removes_it():
    token = pairing_store.redeem(pairing_store.issue_invite("iPhone"), "iPhone")

    pairing_store.set_apns_token(token, "fake-apns-token")
    pairing_store.clear_apns_token(token)
    assert pairing_store.apns_token_for(token) is None


def test_clear_apns_token_on_device_with_none_set_is_a_noop():
    token = pairing_store.redeem(pairing_store.issue_invite("iPhone"), "iPhone")

    pairing_store.clear_apns_token(token)  # must not raise
    assert pairing_store.apns_token_for(token) is None


def test_apns_token_persists_across_store_instances(monkeypatch, tmp_path):
    from server import config

    fake_settings = type(config.settings)(data_dir=tmp_path, log_dir=tmp_path)
    monkeypatch.setattr("server.pairing.settings", fake_settings)

    store_a = PairingStore()
    token = store_a.redeem(store_a.issue_invite("iPad"), "iPad")
    store_a.set_apns_token(token, "fake-apns-token")

    store_b = PairingStore()
    assert store_b.apns_token_for(token) == "fake-apns-token"


def test_old_format_device_tokens_json_without_apns_key_loads_fine(monkeypatch, tmp_path):
    import json

    from server import config

    fake_settings = type(config.settings)(data_dir=tmp_path, log_dir=tmp_path)
    monkeypatch.setattr("server.pairing.settings", fake_settings)

    fake_settings.data_dir.mkdir(parents=True, exist_ok=True)
    fake_settings.device_tokens_path.write_text(
        json.dumps({"an-old-token": {"device_name": "old-iPhone", "paired_at": 123.0}})
    )

    store = PairingStore()
    assert store.is_valid_token("an-old-token")
    assert store.device_name_for("an-old-token") == "old-iPhone"
    assert store.apns_token_for("an-old-token") is None

    # And it must still be settable/updatable from here.
    store.set_apns_token("an-old-token", "newly-registered-token")
    assert store.apns_token_for("an-old-token") == "newly-registered-token"


def _pair(name: str = "iPhone") -> str:
    return pairing_store.redeem(pairing_store.issue_invite(name), name)


def test_timezone_round_trip():
    token = _pair()
    assert pairing_store.timezone_for(token) is None
    pairing_store.set_timezone(token, "Europe/Lisbon")
    assert pairing_store.timezone_for(token) == "Europe/Lisbon"


def test_latest_timezone_is_none_before_any_device_reports(monkeypatch, tmp_path):
    # An isolated store: a bare PairingStore() reloads the shared data_dir and
    # would inherit whatever an earlier test in this file reported.
    from server import config

    monkeypatch.setattr(
        "server.pairing.settings", type(config.settings)(data_dir=tmp_path, log_dir=tmp_path)
    )
    assert PairingStore().latest_timezone() is None


def test_latest_timezone_prefers_the_most_recent_report():
    """Mike travels, and a dormant paired device (the simulator, an old phone)
    must not out-vote the handset in his pocket. Recency decides, not pairing
    order -- so the older device is deliberately the one paired LAST here.
    """
    handset = _pair("iPhone")
    dormant = _pair("Simulator")

    pairing_store.set_timezone(dormant, "America/Edmonton")
    pairing_store.set_timezone(handset, "Europe/Lisbon")

    assert pairing_store.latest_timezone() == "Europe/Lisbon"


def test_set_timezone_on_unknown_device_token_is_a_noop():
    pairing_store.set_timezone("not-a-real-device-token", "Europe/Lisbon")


def test_repeating_the_same_timezone_does_not_rewrite_the_file(monkeypatch):
    """Every message carries the timezone, so this runs on every turn. Writing
    device_tokens.json each time would be a disk write per message for a value
    that changes a few times a year.
    """
    token = _pair()
    pairing_store.set_timezone(token, "Europe/Lisbon")

    writes = []
    monkeypatch.setattr(pairing_store, "_save_tokens", lambda: writes.append(1))
    pairing_store.set_timezone(token, "Europe/Lisbon")
    assert writes == []
    pairing_store.set_timezone(token, "America/Edmonton")
    assert writes == [1]


def test_device_name_is_stripped_of_control_characters_and_capped():
    token = pairing_store.redeem(
        pairing_store.issue_invite("x"), "evil\n0123abcd  fake row\x1b[2J" + "z" * 200
    )
    name = pairing_store.device_name_for(token)
    assert "\n" not in name and "\x1b" not in name
    assert len(name) <= 64


def test_a_failed_token_write_leaves_no_half_paired_token(monkeypatch):
    store = PairingStore()
    before = set(store._tokens)

    def boom():
        raise OSError("disk full")

    monkeypatch.setattr(store, "_save_tokens", boom)
    with pytest.raises(OSError):
        store.redeem(store.issue_invite("iPhone"), "iPhone")
    assert set(store._tokens) == before
