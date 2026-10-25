"""Live system graph: real jobs, workers, documents, and models as nodes.

The dashboard's "neural network" is a rendering of this module's output. It is
deliberately *not* a decorative animation: a node exists because a row exists,
an edge exists because a real relationship exists, and a node's hover panel is
populated from that row's own columns.

See :mod:`synapse.graph.snapshot` for the provenance of each node and edge
kind, and for the bounding rules that keep a large corpus from killing the
browser that renders it.
"""

from synapse.graph.snapshot import (
    GraphEdge,
    GraphNode,
    GraphSnapshot,
    build_graph,
)

__all__ = [
    "GraphEdge",
    "GraphNode",
    "GraphSnapshot",
    "build_graph",
]
