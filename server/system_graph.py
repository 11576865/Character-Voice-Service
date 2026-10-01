from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from server.config import VOICE_DIR
from server.model_registry import list_models
from server.runtime_registry import load_runtime_registry
from server.runtime_supervisor import runtime_supervisor
from server.voice_bindings import list_bindings
from server.voice_profiles import iter_real_profile_paths, read_valid_profile


GRAPH_SCHEMA_VERSION = 1


def _stable_id(kind: str, value: str) -> str:
    return f"{kind}:{value}"


def _revision(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


@dataclass
class _GraphBuilder:
    nodes: dict[str, dict]
    edges: dict[tuple[str, str, str], dict]
    warnings: list[dict]

    @classmethod
    def create(cls) -> "_GraphBuilder":
        return cls(nodes={}, edges={}, warnings=[])

    def node(self, node_id: str, kind: str, **attrs: Any) -> str:
        payload = {"id": node_id, "kind": kind, **attrs}
        previous = self.nodes.get(node_id)
        if previous is None:
            self.nodes[node_id] = payload
        else:
            # Keep a single identity while allowing later sources to enrich it.
            for key, value in payload.items():
                if value is not None and key not in {"id", "kind"}:
                    previous[key] = value
        return node_id

    def edge(self, source: str, relation: str, target: str, **attrs: Any) -> None:
        key = (source, relation, target)
        payload = {
            "source": source,
            "relation": relation,
            "target": target,
            **attrs,
        }
        self.edges[key] = payload

    def warn(self, code: str, message: str, **attrs: Any) -> None:
        self.warnings.append({"code": code, "message": message, **attrs})

    def result(self) -> dict:
        nodes = sorted(self.nodes.values(), key=lambda item: item["id"])
        edges = sorted(
            self.edges.values(),
            key=lambda item: (item["source"], item["relation"], item["target"]),
        )
        warnings = sorted(
            self.warnings,
            key=lambda item: (item.get("code", ""), item.get("message", "")),
        )
        body = {
            "schema_version": GRAPH_SCHEMA_VERSION,
            "nodes": nodes,
            "edges": edges,
            "warnings": warnings,
        }
        body["revision"] = _revision(body)
        body["summary"] = {
            "nodes": len(nodes),
            "edges": len(edges),
            "warnings": len(warnings),
            "voices": sum(1 for item in nodes if item["kind"] == "voice"),
            "models": sum(1 for item in nodes if item["kind"] == "model"),
            "runtimes": sum(1 for item in nodes if item["kind"] == "runtime"),
            "dependencies": sum(1 for item in nodes if item["kind"] == "dependency"),
        }
        return body


def _safe_runtime_status(engine_id: str) -> dict:
    try:
        return runtime_supervisor.status(engine_id)
    except Exception as exc:
        return {
            "engine": engine_id,
            "status": "unknown",
            "ready": False,
            "error": str(exc),
        }


def _runtime_dependency_id(runtime_id: str, dependency: dict, index: int) -> str:
    dep_id = str(dependency.get("id") or f"dependency-{index + 1}")
    return _stable_id("dependency", f"{runtime_id}/{dep_id}")


def build_system_graph(*, include_runtime_status: bool = True) -> dict:
    """
    Build the authoritative relationship graph for the Character Voice System.

    The graph is descriptive: it connects identities and ownership across
    Character/Reference -> VoiceBinding -> Model -> Engine -> Runtime ->
    dependency/lifecycle/resource. Filesystem paths are attributes, not identity.
    """
    graph = _GraphBuilder.create()

    runtime_registry = load_runtime_registry()
    runtime_by_engine = runtime_registry.runtimes

    # Engines/runtimes form the execution side of the graph.
    for engine_id, spec in sorted(runtime_by_engine.items()):
        engine_node = graph.node(
            _stable_id("engine", engine_id),
            "engine",
            engine_id=engine_id,
        )
        runtime_status = _safe_runtime_status(engine_id) if include_runtime_status else None
        runtime_node = graph.node(
            _stable_id("runtime", spec.runtime_id),
            "runtime",
            runtime_id=spec.runtime_id,
            engine_id=engine_id,
            version=spec.runtime_version,
            enabled=spec.enabled,
            mode=spec.mode,
            lifecycle_owner=spec.lifecycle_owner,
            endpoint=spec.endpoint,
            health_url=spec.health_url,
            exclusive_group=spec.exclusive_group,
            start_on_demand=spec.start_on_demand,
            configuration_revision=spec.configuration_revision,
            status=runtime_status,
        )
        graph.edge(engine_node, "executed_by", runtime_node)

        owner_id = str(spec.lifecycle_owner or "unspecified")
        owner_node = graph.node(
            _stable_id("lifecycle-owner", owner_id),
            "lifecycle-owner",
            owner_id=owner_id,
        )
        graph.edge(runtime_node, "lifecycle_owned_by", owner_node)

        if spec.exclusive_group:
            resource_node = graph.node(
                _stable_id("resource", spec.exclusive_group),
                "resource",
                resource_id=spec.exclusive_group,
                resource_kind="exclusive-group",
            )
            graph.edge(runtime_node, "claims_resource", resource_node)

        for index, dependency in enumerate(spec.dependencies):
            dependency_node = graph.node(
                _runtime_dependency_id(spec.runtime_id, dependency, index),
                "dependency",
                dependency_id=str(dependency.get("id") or f"dependency-{index + 1}"),
                dependency_kind=str(dependency.get("kind") or "unknown"),
                ownership=str(dependency.get("ownership") or "unspecified"),
                version=dependency.get("version"),
                path=dependency.get("path"),
                source=dependency.get("source"),
                scope=dependency.get("scope"),
            )
            graph.edge(runtime_node, "depends_on", dependency_node)

    # Registered immutable/shared models.
    try:
        models = list_models()
    except Exception as exc:
        models = []
        graph.warn("model-registry-error", f"Model Registry could not be read: {exc}")

    model_ids: set[str] = set()
    for model in models:
        model_id = str(model.get("model_id") or "").strip()
        if not model_id:
            continue
        model_ids.add(model_id)
        model_node = graph.node(
            _stable_id("model", model_id),
            "model",
            model_id=model_id,
            name=model.get("name"),
            scope=model.get("scope"),
            voice_id=model.get("voice_id"),
            engine=model.get("engine"),
            engine_version=model.get("engine_version"),
            status=model.get("status"),
            present=model.get("present"),
            revision=model.get("revision"),
        )
        engine_id = str(model.get("engine") or "").strip()
        if engine_id:
            engine_node = graph.node(
                _stable_id("engine", engine_id),
                "engine",
                engine_id=engine_id,
            )
            graph.edge(model_node, "served_by", engine_node)
            if engine_id not in runtime_by_engine:
                graph.warn(
                    "engine-without-runtime",
                    f"Model {model_id} uses engine {engine_id}, but no runtime is registered.",
                    model_id=model_id,
                    engine_id=engine_id,
                )

    # Bindings are the stable semantic bridge between a character and a model.
    try:
        bindings = list_bindings()
    except Exception as exc:
        bindings = []
        graph.warn("binding-registry-error", f"VoiceBinding Registry could not be read: {exc}")

    binding_by_voice: dict[str, list[dict]] = {}
    for binding in bindings:
        binding_id = str(binding["binding_id"])
        voice_id = str(binding["voice_id"])
        model_id = str(binding["model_id"])
        engine_id = str(binding["engine"])
        binding_by_voice.setdefault(voice_id, []).append(binding)

        binding_node = graph.node(
            _stable_id("binding", binding_id),
            "binding",
            binding_id=binding_id,
            voice_id=voice_id,
            model_id=model_id,
            engine_id=engine_id,
            revision=binding.get("revision"),
            emotion_policy=binding.get("emotion_policy"),
        )
        voice_node = graph.node(
            _stable_id("voice", voice_id),
            "voice",
            voice_id=voice_id,
        )
        model_node = graph.node(
            _stable_id("model", model_id),
            "model",
            model_id=model_id,
        )
        engine_node = graph.node(
            _stable_id("engine", engine_id),
            "engine",
            engine_id=engine_id,
        )
        graph.edge(voice_node, "has_binding", binding_node)
        graph.edge(binding_node, "uses_model", model_node)
        graph.edge(binding_node, "targets_engine", engine_node)

        if model_id not in model_ids:
            graph.warn(
                "binding-model-unregistered",
                f"Binding {binding_id} refers to model {model_id}, which is not present in Model Registry.",
                binding_id=binding_id,
                model_id=model_id,
            )

    # Character profiles add human-facing identity and references.
    for profile_path in iter_real_profile_paths(VOICE_DIR) or []:
        voice_id = profile_path.stem
        try:
            profile = read_valid_profile(profile_path)
        except Exception as exc:
            graph.warn(
                "voice-profile-invalid",
                f"Voice profile {voice_id} could not be read: {exc}",
                voice_id=voice_id,
            )
            continue

        voice_node = graph.node(
            _stable_id("voice", voice_id),
            "voice",
            voice_id=voice_id,
            name=profile.get("name"),
            target_language=profile.get("target_language"),
            profile_schema_version=profile.get("schema_version"),
            default_model=profile.get("default_model"),
            default_reference=profile.get("default_reference"),
        )

        for reference_id, reference in sorted(profile.get("references", {}).items()):
            reference_node = graph.node(
                _stable_id("reference", f"{voice_id}/{reference_id}"),
                "reference",
                voice_id=voice_id,
                reference_id=reference_id,
                name=reference.get("name"),
                language=reference.get("language"),
                emotion=reference.get("emotion"),
                roles=reference.get("roles"),
                path=reference.get("audio"),
            )
            graph.edge(voice_node, "has_reference", reference_node)

        # Inline/legacy model aliases remain visible, but registered model identity wins
        # when model_id is present.
        for alias, model in sorted(profile.get("models", {}).items()):
            registry_model_id = str(model.get("model_id") or "").strip()
            identity = registry_model_id or f"{voice_id}/alias/{alias}"
            model_node = graph.node(
                _stable_id("model", identity),
                "model",
                model_id=registry_model_id or None,
                alias=alias,
                voice_id=voice_id,
                name=model.get("name"),
                engine=model.get("engine"),
                engine_version=model.get("version"),
                revision=model.get("revision"),
                scope=model.get("scope"),
                managed=model.get("managed"),
                source="model-registry" if registry_model_id else "voice-profile",
            )
            graph.edge(voice_node, "declares_model", model_node)
            engine_id = str(model.get("engine") or "").strip()
            if engine_id:
                engine_node = graph.node(
                    _stable_id("engine", engine_id),
                    "engine",
                    engine_id=engine_id,
                )
                graph.edge(model_node, "served_by", engine_node)

        for binding in binding_by_voice.get(voice_id, []):
            speaker_reference_id = str(binding.get("speaker_reference_id") or "")
            if speaker_reference_id:
                reference_node = _stable_id(
                    "reference", f"{voice_id}/{speaker_reference_id}"
                )
                if reference_node not in graph.nodes:
                    graph.warn(
                        "binding-reference-missing",
                        f"Binding {binding['binding_id']} refers to missing speaker reference {speaker_reference_id}.",
                        binding_id=binding["binding_id"],
                        reference_id=speaker_reference_id,
                    )
                else:
                    graph.edge(
                        _stable_id("binding", binding["binding_id"]),
                        "uses_speaker_reference",
                        reference_node,
                    )

            emotion_reference_id = str(binding.get("emotion_reference_id") or "")
            if emotion_reference_id:
                reference_node = _stable_id(
                    "reference", f"{voice_id}/{emotion_reference_id}"
                )
                if reference_node not in graph.nodes:
                    graph.warn(
                        "binding-emotion-reference-missing",
                        f"Binding {binding['binding_id']} refers to missing emotion reference {emotion_reference_id}.",
                        binding_id=binding["binding_id"],
                        reference_id=emotion_reference_id,
                    )
                else:
                    graph.edge(
                        _stable_id("binding", binding["binding_id"]),
                        "uses_emotion_reference",
                        reference_node,
                    )

    # Detect engines that exist semantically but have no execution runtime.
    for node in list(graph.nodes.values()):
        if node["kind"] != "engine":
            continue
        engine_id = str(node.get("engine_id") or "")
        if engine_id and engine_id not in runtime_by_engine:
            graph.warn(
                "engine-runtime-unregistered",
                f"Engine {engine_id} has no Runtime Registry entry.",
                engine_id=engine_id,
            )

    return graph.result()


def trace_voice(voice_id: str) -> dict:
    graph = build_system_graph()
    voice_node = _stable_id("voice", voice_id)
    if voice_node not in {node["id"] for node in graph["nodes"]}:
        raise KeyError(f"voice not found in system graph: {voice_id}")

    adjacency: dict[str, list[dict]] = {}
    for edge in graph["edges"]:
        adjacency.setdefault(edge["source"], []).append(edge)

    keep: set[str] = {voice_node}
    frontier = [voice_node]
    while frontier:
        current = frontier.pop()
        for edge in adjacency.get(current, []):
            target = edge["target"]
            if target not in keep:
                keep.add(target)
                frontier.append(target)

    return {
        "schema_version": graph["schema_version"],
        "graph_revision": graph["revision"],
        "voice_id": voice_id,
        "nodes": [node for node in graph["nodes"] if node["id"] in keep],
        "edges": [
            edge
            for edge in graph["edges"]
            if edge["source"] in keep and edge["target"] in keep
        ],
        "warnings": [
            warning
            for warning in graph["warnings"]
            if warning.get("voice_id") in {None, voice_id}
        ],
    }
