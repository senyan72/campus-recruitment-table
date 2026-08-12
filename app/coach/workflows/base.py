"""Workflow 注册表与契约。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol


class Workflow(Protocol):
    name: str
    version: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]

    def run(self, payload: dict[str, Any], *, context: dict[str, Any] | None = None) -> dict[str, Any]:
        ...


@dataclass
class WorkflowMeta:
    name: str
    version: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]


class _FnWorkflow:
    def __init__(
        self,
        *,
        name: str,
        version: str,
        description: str,
        input_schema: dict[str, Any],
        output_schema: dict[str, Any],
        handler: Callable[[dict[str, Any], dict[str, Any] | None], dict[str, Any]],
    ) -> None:
        self.name = name
        self.version = version
        self.description = description
        self.input_schema = input_schema
        self.output_schema = output_schema
        self._handler = handler

    def run(self, payload: dict[str, Any], *, context: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._handler(payload, context)


def make_workflow(
    *,
    name: str,
    version: str,
    description: str,
    input_schema: dict[str, Any],
    output_schema: dict[str, Any],
    handler: Callable[[dict[str, Any], dict[str, Any] | None], dict[str, Any]],
) -> Workflow:
    return _FnWorkflow(
        name=name,
        version=version,
        description=description,
        input_schema=input_schema,
        output_schema=output_schema,
        handler=handler,
    )


class WorkflowRegistry:
    def __init__(self) -> None:
        self._items: dict[str, Workflow] = {}

    def register(self, workflow: Workflow) -> None:
        self._items[workflow.name] = workflow

    def get(self, name: str) -> Workflow | None:
        return self._items.get(name)

    def has(self, name: str) -> bool:
        return name in self._items

    def list(self) -> list[dict[str, Any]]:
        return [
            {
                "name": w.name,
                "version": w.version,
                "description": w.description,
                "input_schema": w.input_schema,
                "output_schema": w.output_schema,
            }
            for w in self._items.values()
        ]

    def run(self, name: str, payload: dict[str, Any], *, context: dict[str, Any] | None = None) -> dict[str, Any]:
        wf = self.get(name)
        if not wf:
            raise KeyError(f"未知 workflow: {name}")
        return wf.run(payload, context=context)
