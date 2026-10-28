"""Steps 5-6: validate the graph (acyclic, valid references) and export to DAGitty."""
from __future__ import annotations

import re
from typing import List, Tuple

import networkx as nx

from .schemas import Critique, Edge, Extraction, Node


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")
    return s[:40] or "node"


def validate(nodes: List[Node], edges: List[Edge]) -> Tuple[List[Node], List[Edge], List[str]]:
    """Fix what we safely can and report every problem found."""
    issues: List[str] = []

    # unique ids
    seen, clean_nodes = set(), []
    for n in nodes:
        n.id = slug(n.id or n.label)
        if n.role in ("exposure", "outcome"):
            n.adjusted = False  # the model sometimes marks these; it's meaningless
        if n.id in seen:
            issues.append(f"Duplicate node '{n.id}' merged.")
            continue
        seen.add(n.id)
        clean_nodes.append(n)

    for role in ("exposure", "outcome"):
        count = sum(n.role == role for n in clean_nodes)
        if count == 0:
            issues.append(f"No {role} node found.")
        elif count > 1:
            issues.append(f"{count} {role} nodes found; expected one.")

    # edges must reference real nodes, no self-loops, no duplicates
    ids = {n.id for n in clean_nodes}
    g = nx.DiGraph()
    g.add_nodes_from(ids)
    clean_edges: List[Edge] = []
    for e in edges:
        e.source, e.target = slug(e.source), slug(e.target)
        if e.source not in ids or e.target not in ids:
            issues.append(f"Edge {e.source} -> {e.target} dropped (unknown node).")
            continue
        if e.source == e.target or g.has_edge(e.source, e.target):
            continue
        g.add_edge(e.source, e.target)
        if not nx.is_directed_acyclic_graph(g):
            g.remove_edge(e.source, e.target)
            issues.append(f"Edge {e.source} -> {e.target} dropped (would create a cycle).")
            continue
        clean_edges.append(e)
    return clean_nodes, clean_edges, issues


def ensure_core_edges(ex: Extraction) -> Extraction:
    """Make sure exposure -> outcome exists and confounders point at both."""
    exp = next((n.id for n in ex.nodes if n.role == "exposure"), None)
    out = next((n.id for n in ex.nodes if n.role == "outcome"), None)
    have = {(slug(e.source), slug(e.target)) for e in ex.edges}
    has_out_edges = {slug(e.source) for e in ex.edges}
    if exp and out and (exp, out) not in have:
        ex.edges.append(Edge(source=exp, target=out))
    for n in ex.nodes:
        if n.role == "confounder" and exp and out and n.id not in has_out_edges:
            ex.edges += [Edge(source=n.id, target=exp), Edge(source=n.id, target=out)]
    return ex


def merge_suggestions(nodes: List[Node], edges: List[Edge], crit: Critique) -> Tuple[List[Node], List[Edge]]:
    """Add AI-suggested confounders (not rejected ones) as unadjusted nodes flagged ai_suggested."""
    nodes, edges = list(nodes), list(edges)
    ids = {n.id for n in nodes}
    labels = {n.label.lower(): n.id for n in nodes}
    exp = next((n.id for n in nodes if n.role == "exposure"), None)
    out = next((n.id for n in nodes if n.role == "outcome"), None)
    for mc in crit.missing_confounders:
        if mc.status == "rejected":
            continue
        nid = slug(mc.label)
        if nid in ids or mc.label.lower() in labels:
            continue
        nodes.append(Node(id=nid, label=mc.label, role="confounder", adjusted=False,
                          snippet="", ai_suggested=True))
        ids.add(nid)
        added = False
        for e in mc.edges_to_add:
            tgt = labels.get(e.target.lower(), slug(e.target))
            if tgt in ids and tgt != nid:
                edges.append(Edge(source=nid, target=tgt, ai_suggested=True))
                added = True
        if not added and exp and out:
            edges += [Edge(source=nid, target=exp, ai_suggested=True),
                      Edge(source=nid, target=out, ai_suggested=True)]
    return nodes, edges


def to_dagitty(nodes: List[Node], edges: List[Edge]) -> str:
    """DAGitty syntax, paste into https://dagitty.net or dagitty::dagitty() in R."""
    lines = ["dag {"]
    for n in nodes:
        attrs = []
        if n.role == "exposure":
            attrs.append("exposure")
        elif n.role == "outcome":
            attrs.append("outcome")
        elif n.adjusted:
            attrs.append("adjusted")
        lines.append(f"{n.id}" + (f" [{','.join(attrs)}]" if attrs else ""))
    for e in edges:
        lines.append(f"{e.source} -> {e.target}")
    lines.append("}")
    return "\n".join(lines)


def open_backdoors(nodes: List[Node], edges: List[Edge]) -> List[str]:
    """Labels of unadjusted common causes of exposure and outcome (simple screen)."""
    g = nx.DiGraph([(e.source, e.target) for e in edges])
    exp = next((n.id for n in nodes if n.role == "exposure"), None)
    out = next((n.id for n in nodes if n.role == "outcome"), None)
    if not exp or not out or exp not in g or out not in g:
        return []
    g_no_exp = g.copy()
    g_no_exp.remove_edges_from(list(g.out_edges(exp)))
    flagged = []
    for n in nodes:
        if n.role in ("exposure", "outcome") or n.adjusted or n.id not in g:
            continue
        if nx.has_path(g, n.id, exp) and nx.has_path(g_no_exp, n.id, out):
            flagged.append(n.label)
    return flagged
