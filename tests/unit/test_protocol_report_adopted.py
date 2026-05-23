from berth.cluster.protocol import ReportAdopted, decode_frame, encode_frame


def test_report_adopted_round_trips():
    frame = ReportAdopted(endpoints=[{
        "model_name": "nvidia/MiniMax-M2.7-NVFP4",
        "served_model_name": "nvidia/MiniMax-M2.7-NVFP4",
        "address": "127.0.0.1", "port": 30011,
        "container_id": "cid-1", "gpu_ids": [7],
        "vram_reserved_mb": 268000, "alive": True,
    }])
    decoded = decode_frame(encode_frame(frame))
    assert isinstance(decoded, ReportAdopted)
    assert decoded.endpoints[0]["port"] == 30011
    assert decoded.type == "report_adopted"
