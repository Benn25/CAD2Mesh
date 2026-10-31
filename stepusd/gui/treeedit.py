"""Editing the assembly tree: create and delete components, and reparent parts.

The scene graph read from STEP is a plain `Node` tree, so it can be rearranged
in place. The only subtlety is that every node's matrix is LOCAL to its parent:
moving a node to a new parent must rewrite that matrix or the part jumps across
the model. `reparent` composes the inverse of the new parent's world transform
so the geometry stays exactly where it was.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QTreeWidget, QAbstractItemView

from OCP.gp import gp_Trsf

from ..reader import Node


class AssemblyTree(QTreeWidget):
    """QTreeWidget that reports drops instead of performing them.

    We rebuild the widget from the Node tree after every edit, so letting Qt
    also move rows internally would double the move.
    """
    #: (list of dragged Nodes, target Node or None). Nodes, not items: handling
    #: the drop rebuilds the tree, so the QTreeWidgetItems are gone by then.
    dropped = Signal(object, object)

    #: set by MainWindow: int id -> Node. Item data holds the ID, never the Node
    #: itself -- Qt serialises item data into MIME for an internal move, and a
    #: Node contains a gp_Trsf, which is not picklable ("cannot pickle
    #: 'OCP.OCP.gp.gp_Trsf' object" the moment a drag starts).
    node_resolver = None

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)

    def _node_of(self, item):
        """The Node an item stands for; a body row resolves to its part."""
        if item is None or self.node_resolver is None:
            return None
        nid = item.data(0, Qt.UserRole + 1)
        if nid is None:
            par = item.parent()
            nid = par.data(0, Qt.UserRole + 1) if par is not None else None
        return self.node_resolver(nid) if nid is not None else None

    def dropEvent(self, ev):
        # Resolve everything to Nodes NOW, while the items still exist, then
        # hand the work to the next event-loop turn. Rebuilding the tree inside
        # dropEvent destroys the items Qt is still using for the drag, which
        # surfaces as "Slot 'MainWindow::_selection_changed()' not found".
        target = self._node_of(self.itemAt(ev.position().toPoint()))
        nodes = []
        for it in self.selectedItems():
            n = self._node_of(it)
            if n is not None and n not in nodes:
                nodes.append(n)
        ev.accept()
        if nodes:
            QTimer.singleShot(0, lambda: self.dropped.emit(nodes, target))


def world_transforms(roots) -> dict:
    """id(node) -> world transform."""
    out = {}

    def walk(node, parent_world):
        w = parent_world.Multiplied(node.matrix)
        out[id(node)] = w
        for c in node.children:
            walk(c, w)

    for r in roots:
        walk(r, gp_Trsf())
    return out


def parent_map(roots) -> dict:
    """id(node) -> parent node (None for a root)."""
    out = {}

    def walk(node, parent):
        out[id(node)] = parent
        for c in node.children:
            walk(c, node)

    for r in roots:
        walk(r, None)
    return out


def is_ancestor(candidate, node, parents) -> bool:
    p = parents.get(id(node))
    while p is not None:
        if p is candidate:
            return True
        p = parents.get(id(p))
    return False


def reparent(roots, node, new_parent) -> bool:
    """Move `node` under `new_parent` (None = top level), keeping it in place.

    Returns False when the move is impossible (onto itself or its own
    descendant), which would detach a whole branch from the tree.
    """
    parents = parent_map(roots)
    if node is new_parent:
        return False
    if new_parent is not None and is_ancestor(node, new_parent, parents):
        return False
    old_parent = parents.get(id(node))
    if old_parent is new_parent:
        return True

    worlds = world_transforms(roots)
    world_node = worlds[id(node)]
    if old_parent is None:
        roots.remove(node)
    else:
        old_parent.children.remove(node)

    new_world_parent = worlds[id(new_parent)] if new_parent is not None else gp_Trsf()
    # local = inverse(parent world) * node world  -> geometry does not move
    node.matrix = new_world_parent.Inverted().Multiplied(world_node)

    if new_parent is None:
        roots.append(node)
    else:
        new_parent.children.append(node)
    return True


def new_component(roots, parent, name="Component") -> Node:
    """Add an empty component under `parent` (None = top level)."""
    node = Node(name=name, matrix=gp_Trsf(), proto=None, children=[])
    if parent is None:
        roots.append(node)
    else:
        parent.children.append(node)
    return node


def delete_component(roots, node) -> bool:
    """Remove a component, lifting its children to its parent.

    Only components (nodes with no geometry of their own) can be deleted; the
    children keep their world placement, so nothing moves on screen.
    """
    if node.proto is not None:
        return False
    parents = parent_map(roots)
    parent = parents.get(id(node))
    worlds = world_transforms(roots)
    parent_world = worlds[id(parent)] if parent is not None else gp_Trsf()

    for child in list(node.children):
        child.matrix = parent_world.Inverted().Multiplied(worlds[id(child)])
        if parent is None:
            roots.append(child)
        else:
            parent.children.append(child)
    node.children.clear()

    if parent is None:
        if node in roots:
            roots.remove(node)
    else:
        parent.children.remove(node)
    return True
