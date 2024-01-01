from experiments.run_runtime_staleness import (
    classify_stale, replay_simcxl, replay_tail_latency,
)


def test_stale_requires_eviction_inside_window():
    assert classify_stale(True, 3, 4, 150, 100, 200)
    assert not classify_stale(False, 3, 4, 150, 100, 200)
    assert not classify_stale(True, 3, 4, 90, 100, 200)
    assert not classify_stale(True, 3, 3, 150, 100, 200)
    assert not classify_stale(True, 3, 4, 250, 100, 200)


def test_endpoint_replay_removes_only_stale_payload():
    rows = [
        {"generation_end_ns": 0, "stale": False},
        {"generation_end_ns": 1, "stale": True},
    ]
    replay = replay_simcxl(rows, [2.0])
    assert replay["endpoint_desc_per_s"][0] > replay["passive_desc_per_s"][0]
    assert replay["stale_payload_mib"] > 0


def test_tail_replay_exposes_passive_saturation():
    rows = [
        {"generation_end_ns": i, "stale": i % 10 == 0}
        for i in range(1000)
    ]
    replay = replay_tail_latency(rows, bandwidth_gbs=4.0,
                                 offered_load=[0.9, 1.05])
    assert replay["passive"]["p99_ms"][1] > replay["endpoint"]["p99_ms"][1]
    assert (replay["passive"]["peak_queue_depth"][1] >
            replay["endpoint"]["peak_queue_depth"][1])
