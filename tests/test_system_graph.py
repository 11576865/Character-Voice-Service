from types import SimpleNamespace

import server.system_graph as system_graph


class FakeRuntime:
    def __init__(self):
        self.engine_id = "index-tts"
        self.runtime_id = "index-tts-2.5-local"
        self.runtime_version = "2.5"
        self.enabled = True
        self.mode = "external"
        self.lifecycle_owner = "system-supervisor"
        self.endpoint = "http://127.0.0.1:9882"
        self.health_url = "http://127.0.0.1:9882/health"
        self.exclusive_group = "gpu-0"
        self.start_on_demand = True
        self.configuration_revision = "r" * 64
        self.dependencies = (
            {
                "id": "python-runtime",
                "kind": "python-runtime",
                "ownership": "engine-private",
                "path": "D:/env/python.exe",
            },
        )


def test_system_graph_connects_binding_model_engine_runtime_dependency(monkeypatch):
    monkeypatch.setattr(
        system_graph,
        "load_runtime_registry",
        lambda: SimpleNamespace(runtimes={"index-tts": FakeRuntime()}),
    )
    monkeypatch.setattr(
        system_graph,
        "_safe_runtime_status",
        lambda engine_id: {
            "engine": engine_id,
            "status": "ready",
            "ready": True,
        },
    )
    monkeypatch.setattr(
        system_graph,
        "list_models",
        lambda: [
            {
                "model_id": "index-tts-2.5",
                "name": "IndexTTS 2.5",
                "scope": "shared",
                "voice_id": None,
                "engine": "index-tts",
                "engine_version": "2.5",
                "status": "validated",
                "present": True,
                "revision": "m" * 64,
            }
        ],
    )
    monkeypatch.setattr(
        system_graph,
        "list_bindings",
        lambda: [
            {
                "binding_id": "march-index",
                "voice_id": "march-7th",
                "engine": "index-tts",
                "model_id": "index-tts-2.5",
                "speaker_reference_id": "neutral",
                "emotion_reference_id": None,
                "emotion_policy": "speaker",
                "revision": "b" * 64,
            }
        ],
    )
    monkeypatch.setattr(system_graph, "iter_real_profile_paths", lambda _: [])

    graph = system_graph.build_system_graph()

    node_ids = {node["id"] for node in graph["nodes"]}
    assert "binding:march-index" in node_ids
    assert "model:index-tts-2.5" in node_ids
    assert "engine:index-tts" in node_ids
    assert "runtime:index-tts-2.5-local" in node_ids
    assert "dependency:index-tts-2.5-local/python-runtime" in node_ids
    assert "lifecycle-owner:system-supervisor" in node_ids
    assert "resource:gpu-0" in node_ids

    edges = {
        (edge["source"], edge["relation"], edge["target"])
        for edge in graph["edges"]
    }
    assert (
        "binding:march-index",
        "uses_model",
        "model:index-tts-2.5",
    ) in edges
    assert (
        "model:index-tts-2.5",
        "served_by",
        "engine:index-tts",
    ) in edges
    assert (
        "engine:index-tts",
        "executed_by",
        "runtime:index-tts-2.5-local",
    ) in edges
    assert (
        "runtime:index-tts-2.5-local",
        "depends_on",
        "dependency:index-tts-2.5-local/python-runtime",
    ) in edges
    assert len(graph["revision"]) == 64


def test_system_graph_warns_when_semantic_engine_has_no_runtime(monkeypatch):
    monkeypatch.setattr(
        system_graph,
        "load_runtime_registry",
        lambda: SimpleNamespace(runtimes={}),
    )
    monkeypatch.setattr(
        system_graph,
        "list_models",
        lambda: [
            {
                "model_id": "m1",
                "engine": "gpt-sovits",
                "engine_version": "v4",
                "status": "validated",
                "present": True,
            }
        ],
    )
    monkeypatch.setattr(system_graph, "list_bindings", lambda: [])
    monkeypatch.setattr(system_graph, "iter_real_profile_paths", lambda _: [])

    graph = system_graph.build_system_graph(include_runtime_status=False)
    codes = {warning["code"] for warning in graph["warnings"]}
    assert "engine-without-runtime" in codes
    assert "engine-runtime-unregistered" in codes
