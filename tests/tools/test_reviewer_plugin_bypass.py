from __future__ import annotations

import json


def test_active_reviewer_bypasses_all_plugin_mutation_surfaces(monkeypatch):
    import model_tools
    from tools import reviewer_surface

    observed = []

    def canonical(args, **_kwargs):
        observed.append(dict(args))
        return json.dumps({"value": args["value"]})

    monkeypatch.setattr(reviewer_surface, "typed_reviewer_active", lambda: True)
    monkeypatch.setattr(
        reviewer_surface, "canonical_reviewer_handler",
        lambda name: (canonical, False) if name == "read_file" else None,
    )
    monkeypatch.setattr(reviewer_surface, "canonical_reviewer_schema", lambda _name: None)

    import hermes_cli.middleware as middleware
    import hermes_cli.plugins as plugins
    import hermes_cli.lifecycle as lifecycle

    monkeypatch.setattr(middleware, "apply_tool_request_middleware", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("request middleware ran")))
    monkeypatch.setattr(middleware, "run_tool_execution_middleware", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("execution middleware ran")))
    monkeypatch.setattr(plugins, "resolve_pre_tool_block", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("pre hook ran")))
    monkeypatch.setattr(model_tools, "_emit_post_tool_call_hook", lambda **_k: (_ for _ in ()).throw(AssertionError("post hook ran")))
    monkeypatch.setattr(lifecycle, "has_hook", lambda _name: True)
    monkeypatch.setattr(lifecycle, "invoke_hook", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("transform hook ran")))

    result = json.loads(model_tools.handle_function_call("read_file", {"value": "original"}))

    assert observed == [{"value": "original"}]
    assert result == {"value": "original"}


def test_generic_tool_dispatch_preserves_plugin_extensibility(monkeypatch):
    import model_tools
    from tools import reviewer_surface

    monkeypatch.setattr(reviewer_surface, "typed_reviewer_active", lambda: False)
    monkeypatch.setattr(
        "hermes_cli.plugins.resolve_pre_tool_block",
        lambda *_a, **_k: "blocked by generic plugin",
    )

    result = json.loads(model_tools.handle_function_call("read_file", {"path": "missing"}))

    assert "blocked by generic plugin" in result["error"]