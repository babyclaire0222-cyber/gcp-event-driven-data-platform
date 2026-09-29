from ratelimit import MemoryBackend


def test_blocks_after_limit_then_recovers(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("ratelimit.time.monotonic", lambda: now[0])
    b = MemoryBackend()

    assert [b.hit("u", 3, 60)[0] for _ in range(3)] == [True, True, True]
    allowed, retry_after = b.hit("u", 3, 60)
    assert not allowed and 0 < retry_after <= 61

    now[0] += 61  # window slides past the first hits
    assert b.hit("u", 3, 60)[0]


def test_limits_are_per_key():
    b = MemoryBackend()
    for _ in range(3):
        b.hit("alice", 3, 60)
    assert not b.hit("alice", 3, 60)[0]
    assert b.hit("bob", 3, 60)[0]
