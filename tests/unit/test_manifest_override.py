import yaml

from berth.backends import manifest as manifest_mod


def test_load_with_override(tmp_path, monkeypatch):
    # Point SERVE_DIR at tmp_path so override file lookup happens here
    monkeypatch.setattr(manifest_mod.config, "SERVE_DIR", tmp_path)

    # Write a partial override that only changes the vllm pinned_tag
    override = tmp_path / "backends.override.yaml"
    override.write_text(yaml.safe_dump({
        "vllm": {"pinned_tag": "v9.9.9"}
    }))

    m = manifest_mod.load_manifest()
    assert m["vllm"].pinned_tag == "v9.9.9"
    # Other vllm fields preserved from packaged YAML
    assert m["vllm"].image == "vllm/vllm-openai"
    assert m["vllm"].internal_port == 8000
    # Other engines untouched
    assert m["sglang"].pinned_tag != "v9.9.9"


def test_load_without_override(tmp_path, monkeypatch):
    monkeypatch.setattr(manifest_mod.config, "SERVE_DIR", tmp_path)
    m = manifest_mod.load_manifest()
    # Packaged defaults present
    assert m["vllm"].image == "vllm/vllm-openai"


def test_write_override_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(manifest_mod.config, "SERVE_DIR", tmp_path)
    path = manifest_mod.write_override({"vllm": {"pinned_tag": "v1.2.3"}})
    assert path.exists()
    m = manifest_mod.load_manifest()
    assert m["vllm"].pinned_tag == "v1.2.3"

    # Second write merges instead of overwriting
    manifest_mod.write_override({"sglang": {"pinned_tag": "v9.9.9"}})
    m = manifest_mod.load_manifest()
    assert m["vllm"].pinned_tag == "v1.2.3"
    assert m["sglang"].pinned_tag == "v9.9.9"
