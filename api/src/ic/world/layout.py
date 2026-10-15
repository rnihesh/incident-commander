"""Seeded 2D positions for Service nodes (stored as x, y) so the UI graph is stable."""

import networkx as nx

from ic.world.model import World


def apply_layout(world: World, seed: int) -> None:
    g = nx.Graph()
    g.add_nodes_from(s.id for s in world.services)
    g.add_edges_from((d.caller_id, d.callee_id) for d in world.dependencies)
    # method="force" is the pure-numpy implementation (no scipy needed).
    pos = nx.spring_layout(g, seed=seed, iterations=60, method="force")
    for s in world.services:
        s.x, s.y = round(float(pos[s.id][0]), 4), round(float(pos[s.id][1]), 4)
