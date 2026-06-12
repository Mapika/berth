from berth.cluster import adopted
from berth.cluster.agent_client import build_adopted_report, register_adopted_endpoints


class _Disp:
    def __init__(self):
        self.registered = {}
    def register_endpoint(self, *, container_id, address, port):
        self.registered[container_id] = (address, port)
    def unregister_endpoint(self, *, container_id):
        self.registered.pop(container_id, None)


def _entry():
    return adopted.AdoptedEndpoint(
        name="minimax", model_name="m", served_model_name="served-m",
        address="127.0.0.1", port=30011, container_id="cid-1",
        gpu_ids=[7], vram_reserved_mb=268000, image_tag="external")


def test_build_adopted_report_shapes_frame():
    frame = build_adopted_report([_entry()], alive_by_cid={"cid-1": True})
    assert frame.type == "report_adopted"
    ep = frame.endpoints[0]
    assert ep["served_model_name"] == "served-m"
    assert ep["alive"] is True
    assert "name" not in ep


def test_register_adopted_endpoints_registers_each():
    disp = _Disp()
    register_adopted_endpoints(disp, [_entry()])
    assert disp.registered == {"cid-1": ("127.0.0.1", 30011)}


def test_register_adopted_endpoints_unregisters_removed():
    disp = _Disp()
    register_adopted_endpoints(disp, [_entry()])
    # Managed deployments share the dispatcher; they must survive the sync.
    disp.register_endpoint(
        container_id="managed-cid", address="127.0.0.1", port=30000)
    register_adopted_endpoints(disp, [])
    assert disp.registered == {"managed-cid": ("127.0.0.1", 30000)}
