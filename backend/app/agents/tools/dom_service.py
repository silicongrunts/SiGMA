"""
Enhanced DOM Service — builds page snapshots via CDP DOMSnapshot.captureSnapshot.

Single CDP session, 2 calls (captureSnapshot + getLayoutMetrics).

Pipeline:
    build_snapshot(page)
    → _capture_cdp_data (1 session, 2 CDP calls)
    → _build_all_trees (no AX merge)
    → _mark_visibility (5 CSS checks)
    → _mark_interactive (HTML attributes + cursor:pointer)
    → _simplify_tree (collapse pass-through nodes, keep table structure)
    → _assign_refs
    → _serialize_to_sections (single pass, with folding)
    → _compose_output (density sort + truncation + virtual refs)
    → return (text, refs, virtual_refs)
"""

import asyncio
import re
from dataclasses import dataclass, field
from typing import Optional

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# ── Constants ────────────────────────────────────────────────────────────

REQUIRED_COMPUTED_STYLES = [
    "display", "visibility", "opacity",
    "cursor",
]

# Tags that are always interactive (when visible)
INTERACTIVE_TAGS = frozenset({
    "a", "button", "input", "textarea", "select",
    "summary", "details", "option", "optgroup",
})

# ARIA roles that indicate interactivity
INTERACTIVE_ROLES = frozenset({
    "button", "link", "tab", "menuitem", "option",
    "checkbox", "radio", "switch", "textbox", "combobox",
    "slider", "spinbutton", "searchbox", "menuitemcheckbox",
    "menuitemradio", "treeitem",
})

# Tags to skip entirely
SKIP_TAGS = frozenset({
    "style", "script", "head", "meta", "link", "title",
    "noscript", "template",
})

# Table family — structural hierarchy must be preserved (table > thead > tr > td)
TABLE_FAMILY_TAGS = frozenset({
    "table", "thead", "tbody", "tfoot", "tr", "td", "th",
    "caption", "colgroup", "col",
})

# Only these tags can be folded when 6+ consecutive siblings appear.
# Generic containers (div, section, article, etc.) are excluded because
# they may contain heterogeneous, content-rich children that should never
# be collapsed into a virtual ref.
FOLDABLE_TAGS = frozenset({"a", "li", "option"})

# Attributes to include in serialization
RICH_ATTRS = frozenset({
    "type", "placeholder", "value", "name",
    "href", "alt", "src",
    "aria-label", "aria-placeholder", "aria-valuetext",
    "aria-expanded", "aria-checked", "aria-selected",
    "aria-disabled", "aria-haspopup", "aria-autocomplete",
    "checked", "disabled", "selected", "readonly", "required",
    "contenteditable", "multiple", "accept",
    "inputmode", "autocomplete", "list", "min", "max",
    "minlength", "maxlength", "step", "pattern", "for",
})

# SVG child elements — decorative only, skip during tree building
SVG_CHILD_TAGS = frozenset({
    "path", "rect", "g", "circle", "ellipse", "line",
    "polyline", "polygon", "use", "defs", "clippath",
    "mask", "pattern", "image", "text", "tspan",
})

# Maximum text node / attribute / href lengths
MAX_TEXT_LEN = 8000
MAX_ATTR_LEN = 500
MAX_HREF_LEN = 150

# Pixel buffer beyond viewport edges — elements within this distance
# are still considered visible so the LLM sees nearby context.
VIEWPORT_THRESHOLD = 500

# Max chars shown per virtual-ref expansion. Longer content is paginated
# with sub-refs so the LLM can keep clicking to drill deeper.
VIRTUAL_REF_CHUNK_SIZE = 20000


# ── Data model ───────────────────────────────────────────────────────────

@dataclass
class DomNode:
    """Single node in the DOM tree built from CDP snapshot data."""
    node_name: str = ""           # "DIV", "INPUT", etc.
    node_value: str = ""          # text content for text nodes
    node_type: int = 1            # 1=element, 3=text
    attributes: dict = field(default_factory=dict)
    computed_styles: dict = field(default_factory=dict)
    backend_node_id: int = 0
    children: list = field(default_factory=list)
    parent: Optional["DomNode"] = field(default=None, repr=False)

    # Populated during processing
    is_visible: bool = True
    is_interactive: bool = False
    ref: str = ""                 # "e1", "e2", etc.
    bounding_box: Optional[dict] = None
    cursor_style: str = ""

    # Content document index for iframe linking
    content_document_idx: int = -1

    @property
    def tag(self) -> str:
        return self.node_name.lower()

    @property
    def is_text(self) -> bool:
        return self.node_type == 3


# ── DomService ───────────────────────────────────────────────────────────

class DomService:
    """Builds enhanced DOM snapshots via CDP DOMSnapshot.captureSnapshot."""

    def __init__(self):
        self._max_chars = settings.BROWSER_DOM_MAX_CHARS
        self._fold_threshold = settings.BROWSER_FOLD_THRESHOLD
        self._ref_counter = 0

    async def build_snapshot(
        self,
        page,
    ) -> tuple[str, dict[str, int], dict[str, str]]:
        """Build an enhanced DOM snapshot.

        Args:
            page: Playwright Page object.

        Returns:
            (serialized_text, {ref: backend_node_id}, {virtual_ref: content})
        """
        self._ref_counter = 0
        try:
            return await asyncio.wait_for(
                self._build_snapshot_inner(page),
                timeout=15,
            )
        except asyncio.TimeoutError:
            logger.warning("DomService.build_snapshot timed out")
            return "(DOM snapshot timed out)", {}, {}

    async def build_clean_html(self, page) -> str:
        """Build cleaned HTML using the same CDP visibility pipeline as DOM mode.

        Returns visible-only HTML suitable for markdown conversion.
        """
        self._ref_counter = 0
        try:
            return await asyncio.wait_for(
                self._build_clean_html_inner(page),
                timeout=15,
            )
        except asyncio.TimeoutError:
            logger.warning("DomService.build_clean_html timed out")
            return await page.content()

    async def _build_snapshot_inner(
        self, page,
    ) -> tuple[str, dict[str, int], dict[str, str]]:
        # 1. Capture CDP data (1 session, 2 calls)
        cdp_data, dpr = await self._capture_cdp_data(page)
        if not cdp_data:
            return "(empty page)", {}, {}

        # 1b. Capture viewport dimensions + scroll position for visibility
        self._viewport_w = None
        self._viewport_h = None
        self._scroll_x = 0
        self._scroll_y = 0
        try:
            viewport = page.viewport_size
            if viewport:
                self._viewport_w = viewport["width"]
                self._viewport_h = viewport["height"]
        except Exception:
            logger.debug("Failed to read browser viewport size", exc_info=True)
        try:
            pos = await page.evaluate("() => ({x: window.scrollX, y: window.scrollY})")
            self._scroll_x = pos["x"]
            self._scroll_y = pos["y"]
        except Exception:
            logger.debug("Failed to read browser scroll position", exc_info=True)

        # 2. Build node trees for all frames
        root = self._build_all_trees(cdp_data, dpr=dpr)
        if not root:
            return "(empty page)", {}, {}

        # 3. Mark visibility (CSS only)
        self._mark_visibility(root)

        # 4. Mark interactive elements
        self._mark_interactive(root)

        # 5. Simplify tree (collapse pass-through nodes)
        if not self._simplify_tree(root):
            return "(empty page)", {}, {}

        # 6. Assign refs to interactive elements
        refs = self._assign_refs(root)

        # 7. Serialize to sections (single pass with folding)
        sections, virtual_refs = self._serialize_to_sections(root)

        # 8. Compose output (density sort + truncation)
        text, trunc_virtual_refs = self._compose_output(sections)

        # Merge virtual refs (fold + trunc)
        virtual_refs.update(trunc_virtual_refs)

        return text, refs, virtual_refs

    async def _build_clean_html_inner(self, page) -> str:
        """Reuse CDP pipeline (steps 1-5), output cleaned HTML."""
        # 1. Capture CDP data
        cdp_data, dpr = await self._capture_cdp_data(page)
        if not cdp_data:
            return "<html></html>"

        # 1b. Viewport dimensions for visibility
        self._viewport_w = None
        self._viewport_h = None
        self._scroll_x = 0
        self._scroll_y = 0
        try:
            viewport = page.viewport_size
            if viewport:
                self._viewport_w = viewport["width"]
                self._viewport_h = viewport["height"]
        except Exception:
            logger.debug("Failed to read browser viewport size", exc_info=True)
        try:
            pos = await page.evaluate("() => ({x: window.scrollX, y: window.scrollY})")
            self._scroll_x = pos["x"]
            self._scroll_y = pos["y"]
        except Exception:
            logger.debug("Failed to read browser scroll position", exc_info=True)

        # 2. Build node trees
        root = self._build_all_trees(cdp_data, dpr=dpr)
        if not root:
            return "<html></html>"

        # 3. Mark visibility (same logic as DOM mode)
        self._mark_visibility(root)

        # 4. Simplify tree
        if not self._simplify_tree(root):
            return "<html></html>"

        # 5. Serialize visible nodes to HTML
        return self._serialize_visible_html(root)

    # ── HTML serializer (visible-only, for markdown mode) ──────────────

    # Tags that must not have closing tags
    _VOID_TAGS = frozenset({
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    })

    def _serialize_visible_html(self, node: DomNode) -> str:
        """Serialize visible DOM tree to HTML string."""
        from html import escape

        parts: list[str] = []
        self._write_visible_html(node, parts, escape)
        return "".join(parts)

    def _write_visible_html(
        self, node: DomNode, parts: list[str], escape
    ) -> None:
        """Walk visible tree, appending HTML fragments to parts."""
        if node.is_text:
            if node.node_value:
                parts.append(escape(node.node_value, quote=False))
            return

        if not node.is_visible:
            return

        tag = node.tag
        if not tag:
            # Element with no tag name — serialize children only
            for child in node.children:
                self._write_visible_html(child, parts, escape)
            return

        # Build opening tag with cleaned attributes
        attrs = self._build_html_attrs(node)
        parts.append(f"<{tag}{attrs}>")

        # Children
        for child in node.children:
            self._write_visible_html(child, parts, escape)

        # Closing tag (skip for void elements)
        if tag not in self._VOID_TAGS:
            parts.append(f"</{tag}>")

    def _build_html_attrs(self, node: DomNode) -> str:
        """Build attribute string for an element, stripping junk hrefs."""
        attrs = node.attributes
        if not attrs:
            return ""

        parts: list[str] = []
        for key, value in attrs.items():
            if key.startswith("data-sigma-") or key.startswith("__"):
                continue
            if key == "href":
                v = (value or "").strip()
                if v.startswith("#") or re.match(r"^javascript\s*:", v, re.I):
                    continue
            parts.append(f' {key}="{value}"')
        return "".join(parts)

    # ── CDP capture (1 session) ────────────────────────────────────────

    async def _capture_cdp_data(self, page) -> tuple[Optional[dict], float]:
        """Capture DOM snapshot + device pixel ratio in one CDP session."""
        try:
            cdp = await page.context.new_cdp_session(page)
            try:
                # Call 1: DOM snapshot
                snapshot = await cdp.send("DOMSnapshot.captureSnapshot", {
                    "computedStyles": REQUIRED_COMPUTED_STYLES,
                    "includePaintOrder": False,
                    "includeDOMRects": True,
                })
                # Call 2: Device pixel ratio
                dpr = 1.0
                try:
                    metrics = await cdp.send("Page.getLayoutMetrics")
                    css = metrics.get("cssVisualViewport", {})
                    device = metrics.get("visualViewport", {})
                    css_w = css.get("clientWidth", 0)
                    dev_w = device.get("clientWidth", 0)
                    if dev_w > 0 and css_w > 0:
                        dpr = dev_w / css_w
                except Exception:
                    logger.debug("Failed to infer device pixel ratio from CDP metrics", exc_info=True)
                return snapshot, dpr
            finally:
                await cdp.detach()
        except Exception as e:
            logger.error("CDP capture failed: %s", e, exc_info=True)
            return None, 1.0

    # ── Tree building ──────────────────────────────────────────────────

    def _build_all_trees(self, cdp_data: dict, dpr: float = 1.0) -> Optional[DomNode]:
        """Build DomNode trees for ALL frames in the CDP snapshot."""
        documents = cdp_data.get("documents", [])
        if not documents:
            return None

        strings = cdp_data.get("strings", [])

        doc_roots: list[Optional[DomNode]] = []
        for doc in documents:
            root = self._build_document_tree(doc, strings, dpr)
            doc_roots.append(root)

        main_root = doc_roots[0]
        if not main_root:
            return None

        # Link child documents to iframe hosts
        iframes = self._collect_iframes(main_root)
        for idx, child_root in enumerate(doc_roots[1:], start=1):
            if child_root is None:
                continue
            iframe_slot = idx - 1
            if iframe_slot < len(iframes):
                host = iframes[iframe_slot]
                host.children.append(child_root)
                child_root.parent = host

        return main_root

    def _build_document_tree(self, doc: dict, strings: list, dpr: float) -> Optional[DomNode]:
        """Build a DomNode tree from a single CDP snapshot document."""
        nodes = doc.get("nodes", {})
        layout = doc.get("layout", {})

        node_name_idxs = nodes.get("nodeName", [])
        node_value_idxs = nodes.get("nodeValue", [])
        node_type_list = nodes.get("nodeType", [])
        backend_node_ids = nodes.get("backendNodeId", [])
        attributes_list = nodes.get("attributes", [])
        parent_indices = nodes.get("parentIndex", [-1] * len(node_type_list))

        layout_node_indices = layout.get("nodeIndex", [])
        bounds_list = layout.get("bounds", [])
        layout_styles_list = layout.get("styles", [])

        style_names = REQUIRED_COMPUTED_STYLES

        def s(idx):
            return strings[idx] if 0 <= idx < len(strings) else ""

        # Build layout lookup from nodeIndex to layout data.
        layout_map = {}
        for layout_idx, ni in enumerate(layout_node_indices):
            entry = {
                "bounds": bounds_list[layout_idx] if layout_idx < len(bounds_list) else None,
            }
            if layout_idx < len(layout_styles_list):
                style_indices = layout_styles_list[layout_idx]
                node_styles = {}
                for j, style_idx in enumerate(style_indices):
                    if j < len(style_names) and style_idx >= 0:
                        node_styles[style_names[j]] = s(style_idx)
                entry["styles"] = node_styles
            else:
                entry["styles"] = {}

            if ni not in layout_map:
                layout_map[ni] = entry

        # Build all nodes — skip non-element/non-text
        cdp_to_dom: list[Optional[int]] = []
        dom_nodes: list[Optional[DomNode]] = []

        for i in range(len(node_type_list)):
            nt = node_type_list[i]
            if nt not in (1, 3):
                cdp_to_dom.append(None)
                continue

            nn = s(node_name_idxs[i]) if i < len(node_name_idxs) else ""
            nv = s(node_value_idxs[i]) if i < len(node_value_idxs) else ""
            bnid = backend_node_ids[i] if i < len(backend_node_ids) else 0

            # Skip SVG child elements
            if nt == 1 and nn.lower() in SVG_CHILD_TAGS:
                cdp_to_dom.append(None)
                continue

            # Skip CSS pseudo-elements (::before, ::after, etc.)
            if nt == 1 and nn.startswith("::"):
                cdp_to_dom.append(None)
                continue

            node = DomNode(
                node_name=nn,
                node_value=nv,
                node_type=nt,
                backend_node_id=bnid,
            )

            # Attributes
            if nt == 1 and i < len(attributes_list):
                attrs = attributes_list[i]
                if attrs:
                    for j in range(0, len(attrs), 2):
                        ak = s(attrs[j])
                        av = s(attrs[j + 1]) if j + 1 < len(attrs) else ""
                        if ak:
                            node.attributes[ak] = av

            # Layout data
            if nt == 1:
                ld = layout_map.get(i)
                if ld:
                    b = ld.get("bounds")
                    if b and len(b) >= 4:
                        node.bounding_box = {
                            "x": b[0] / dpr, "y": b[1] / dpr,
                            "w": b[2] / dpr, "h": b[3] / dpr,
                        }
                    node.computed_styles = ld.get("styles", {})
                    node.cursor_style = node.computed_styles.get("cursor", "")

            cdp_to_dom.append(len(dom_nodes))
            dom_nodes.append(node)

        # Reverse map: dom index → CDP index
        dom_to_cdp: list[int] = [0] * len(dom_nodes)
        for ci, di in enumerate(cdp_to_dom):
            if di is not None:
                dom_to_cdp[di] = ci

        # Set parent-child relationships
        for i in range(len(dom_nodes)):
            cdp_idx = dom_to_cdp[i]
            pi = parent_indices[cdp_idx] if cdp_idx < len(parent_indices) else -1
            while pi >= 0:
                parent_dom_idx = cdp_to_dom[pi] if pi < len(cdp_to_dom) else None
                if parent_dom_idx is not None and parent_dom_idx < len(dom_nodes):
                    dom_nodes[i].parent = dom_nodes[parent_dom_idx]
                    dom_nodes[parent_dom_idx].children.append(dom_nodes[i])
                    break
                pi = parent_indices[pi] if pi < len(parent_indices) else -1

        root = None
        for node in dom_nodes:
            if node.parent is None and node.node_type == 1:
                root = node
                break
        return root

    @staticmethod
    def _collect_iframes(node: DomNode) -> list[DomNode]:
        result: list[DomNode] = []
        _walk_collect_iframes(node, result)
        return result

    # ── Visibility (CSS checks + viewport intersection) ─────────────────

    def _mark_visibility(self, node: DomNode):
        """Mark visibility — CSS checks + viewport intersection."""
        if node.is_text:
            node.is_visible = node.parent.is_visible if node.parent else True
        else:
            tag = node.tag

            if tag in SKIP_TAGS:
                node.is_visible = False
                # Still recurse for display:contents edge cases
                for child in node.children:
                    child.is_visible = False
                return

            styles = node.computed_styles
            display = styles.get("display", "")
            visibility = styles.get("visibility", "")
            try:
                opacity = float(styles.get("opacity", "1"))
            except (ValueError, TypeError):
                opacity = 1.0

            if display == "none":
                node.is_visible = False
            elif visibility in ("hidden", "collapse"):
                node.is_visible = False
            elif opacity <= 0:
                node.is_visible = False
            elif node.bounding_box:
                w, h = node.bounding_box.get("w", 0), node.bounding_box.get("h", 0)
                if w <= 0 and h <= 0:
                    node.is_visible = False
                elif node.is_visible and self._viewport_w is not None:
                    # Viewport intersection check — drop elements far outside
                    # the visible area (off-screen modals, dropdowns, etc.)
                    x = node.bounding_box.get("x", 0)
                    y = node.bounding_box.get("y", 0)
                    if not self._intersects_viewport(x, y, w, h):
                        node.is_visible = False
            elif not node.computed_styles:
                # CDP did not assign layout data → no layout box → invisible.
                # Catches display:none elements (which CDP omits from the
                # layout tree entirely) and their descendants.
                node.is_visible = False

        for child in node.children:
            self._mark_visibility(child)

    def _intersects_viewport(self, x: float, y: float, w: float, h: float) -> bool:
        """Check if rect (x,y,w,h) overlaps viewport area + buffer."""
        vp_top = self._scroll_y - VIEWPORT_THRESHOLD
        vp_bottom = self._scroll_y + self._viewport_h + VIEWPORT_THRESHOLD
        vp_left = self._scroll_x - VIEWPORT_THRESHOLD
        vp_right = self._scroll_x + self._viewport_w + VIEWPORT_THRESHOLD
        return x + w > vp_left and x < vp_right and y + h > vp_top and y < vp_bottom

    # ── Interactive detection ──────────────────────────────────────────

    def _is_interactive_element(self, node: DomNode) -> bool:
        """Determine if a node is interactive (pure HTML attributes)."""
        if node.is_text:
            return False

        tag = node.tag
        attrs = node.attributes

        if tag in SKIP_TAGS or tag in ("html", "body", "head"):
            return False

        # 0. Disabled overrides all
        if attrs.get("disabled") is not None or attrs.get("aria-disabled") == "true":
            return False

        # 1. Label handling (before tag check to avoid double-activation)
        if tag == "label":
            if attrs.get("for"):
                return False
            if self._wraps_form_control(node, max_depth=2):
                return True

        # 2. Interactive tags
        if tag in INTERACTIVE_TAGS:
            if tag == "a":
                href = attrs.get("href", "")
                if href and not href.startswith("javascript:"):
                    return True
                # No valid href — may still be JS-driven (Angular router, etc.);
                # fall through to check role, tabindex, event handlers, cursor.
            elif tag == "input" and attrs.get("type", "").lower() == "hidden":
                return False
            else:
                return True

        # 3. ARIA role
        role = attrs.get("role", "").lower()
        if role in INTERACTIVE_ROLES:
            return True

        # 4. contenteditable
        ce = attrs.get("contenteditable")
        if ce is not None and ce.lower() in ("true", ""):
            return True

        # 5-7: non-standard detection — require visible content to avoid
        # marking decorative/empty elements (e.g. icon spans with cursor:pointer)
        has_content = self._has_visible_content(node)

        # 5. tabindex
        if attrs.get("tabindex") is not None and has_content:
            return True

        # 6. Event handler attributes
        for ev in ("onclick", "onmousedown", "onmouseup", "onkeydown", "onkeyup"):
            if attrs.get(ev) and has_content:
                return True

        # 7. cursor: pointer (skip if inside interactive ancestor — cursor inherits)
        if node.cursor_style == "pointer" and has_content \
                and not self._has_interactive_ancestor(node):
            return True

        return False

    @staticmethod
    def _has_visible_content(node: DomNode) -> bool:
        """Check if element has visible text, element children, or descriptive attrs."""
        for child in node.children:
            if child.is_text and child.node_value.strip():
                return True
        if any(not c.is_text for c in node.children):
            return True
        for attr in ("aria-label", "title", "alt"):
            val = node.attributes.get(attr)
            if val and val.strip():
                return True
        return False

    @staticmethod
    def _has_interactive_ancestor(node: DomNode) -> bool:
        """Check if any ancestor is interactive (cursor:pointer inherits from it)."""
        ancestor = node.parent
        while ancestor:
            if ancestor.is_interactive:
                return True
            ancestor = ancestor.parent
        return False

    def _mark_interactive(self, node: DomNode):
        if not node.is_text and node.is_visible:
            node.is_interactive = self._is_interactive_element(node)
        for child in node.children:
            self._mark_interactive(child)

    @staticmethod
    def _wraps_form_control(node: DomNode, max_depth: int = 2) -> bool:
        if max_depth <= 0:
            return False
        for child in node.children:
            if child.is_text:
                continue
            if child.tag in ("input", "select", "textarea"):
                return True
            if DomService._wraps_form_control(child, max_depth - 1):
                return True
        return False

    # ── Simplify tree (collapse pass-through nodes) ───────────────────

    def _simplify_tree(self, node: DomNode) -> bool:
        """Collapse pass-through nodes and remove invisible/empty nodes.

        A non-interactive, non-text node that has exactly 1 element child
        and no direct text is a "pass-through" — its child is promoted to
        replace it.  Table family tags are exempt to preserve table structure.

        Returns True if the node survives.
        """
        if node.is_text:
            return node.is_visible and bool(node.node_value.strip())

        # SKIP_TAGS (style, script, head, title, etc.) — prune entire subtree
        if node.tag in SKIP_TAGS:
            return False

        # Recurse into children first
        surviving = [c for c in node.children if self._simplify_tree(c)]

        # Collapse pass-through children (bottom-up)
        surviving = self._collapse_passthrough(surviving)

        node.children = surviving
        # Fix parent pointers
        for child in surviving:
            child.parent = node

        if surviving:
            return True
        if node.tag in ("html", "body"):
            return True
        if node.is_visible and node.is_interactive:
            return True
        return False

    @staticmethod
    def _collapse_passthrough(children: list) -> list:
        """Collapse pass-through children: promote their grandchildren up.

        A pass-through node is a non-interactive element with exactly 1 element
        child and no direct text, and not a table family tag.
        """
        result = []
        for child in children:
            if child.is_text or child.is_interactive:
                result.append(child)
                continue

            # Table family never collapses
            if child.tag in TABLE_FAMILY_TAGS:
                result.append(child)
                continue

            # html/body never collapses (body must survive for _find_body)
            if child.tag in ("html", "body"):
                result.append(child)
                continue

            elem_children = [c for c in child.children if not c.is_text]
            has_direct_text = any(c.is_text and c.node_value.strip() for c in child.children)

            if len(elem_children) == 1 and not has_direct_text:
                # Pass-through: promote children
                result.extend(child.children)
            else:
                result.append(child)

        return result

    # ── Ref assignment ─────────────────────────────────────────────────

    def _assign_refs(self, node: DomNode) -> dict[str, int]:
        refs: dict[str, int] = {}
        self._ref_counter = 0
        self._assign_refs_recursive(node, refs)
        return refs

    def _assign_refs_recursive(self, node: DomNode, refs: dict[str, int]):
        if (node.is_interactive and node.is_visible
                and node.backend_node_id > 0):
            self._ref_counter += 1
            ref = f"e{self._ref_counter}"
            node.ref = ref
            refs[ref] = node.backend_node_id

        for child in node.children:
            self._assign_refs_recursive(child, refs)

    # ── Serialization (single pass) ────────────────────────────────────

    def _serialize_to_sections(
        self, root: DomNode
    ) -> tuple[list[tuple[int, str]], dict[str, str]]:
        """Serialize tree into per-body-section texts + fold virtual refs.

        Returns:
            sections: [(text_chars, serialized_text), ...] for each body child
            virtual_refs: {fold_ref: fold_content}
        """
        body = self._find_body(root)
        if not body:
            text = self._serialize(root)
            chars = len(text)
            return [(chars, text)], {}

        virtual_refs: dict[str, str] = {}
        fold_counter = 0
        sections: list[tuple[int, str]] = []

        for child in body.children:
            if child.is_text:
                text = self._renderable_text(child)
                if text:
                    sections.append((len(text), text))
                continue
            if not child.is_visible:
                continue

            lines: list[str] = []
            fold_counter = self._serialize_node_with_folding(
                child, lines, virtual_refs, fold_counter
            )
            section_text = "\n".join(lines)
            if section_text.strip():
                sections.append((len(section_text), section_text))

        return sections, virtual_refs

    def _serialize_node_with_folding(
        self,
        node: DomNode,
        lines: list,
        virtual_refs: dict[str, str],
        fold_counter: int,
    ) -> int:
        """Serialize a node, folding consecutive same-tag children.

        Returns updated fold_counter.
        """
        # Text nodes
        if node.is_text:
            text = self._renderable_text(node)
            if text:
                if len(text) > MAX_TEXT_LEN:
                    text = text[:MAX_TEXT_LEN] + "..."
                lines.append(text)
            return fold_counter

        tag = node.tag

        # Skip non-visible — render children only
        if not node.is_visible:
            for child in node.children:
                fold_counter = self._serialize_node_with_folding(
                    child, lines, virtual_refs, fold_counter
                )
            return fold_counter

        if tag in SKIP_TAGS:
            return fold_counter
        if tag in ("svg", "math"):
            return fold_counter

        # iframe
        if tag in ("iframe", "frame") and node.children:
            src = node.attributes.get("src", "")
            prefix = f"[ref={node.ref}]" if node.ref else ""
            lines.append(f'{prefix}<iframe src="{src[:80]}">')
            for child in node.children:
                fold_counter = self._serialize_node_with_folding(
                    child, lines, virtual_refs, fold_counter
                )
            lines.append("</iframe>")
            return fold_counter

        # Build prefix
        prefix = f"[ref={node.ref}]" if node.ref else ""

        # Attribute string
        include_href = tag != "a" or bool(node.ref)
        attrs_str = self._format_attrs(node, include_href=include_href)
        attrs_str += self._format_compound_control(node)

        has_element_children = any(not c.is_text for c in node.children)
        if has_element_children:
            if node.ref:
                # Ref'd element → inline children (one line)
                child_lines: list[str] = []
                fold_counter = self._serialize_children_with_folding(
                    node.children, child_lines, virtual_refs, fold_counter
                )
                lines.append(
                    f"{prefix}<{tag}{attrs_str}>{''.join(child_lines)}</{tag}>"
                )
            else:
                lines.append(f"{prefix}<{tag}{attrs_str}>")
                fold_counter = self._serialize_children_with_folding(
                    node.children, lines, virtual_refs, fold_counter
                )
                lines.append(f"</{tag}>")
        else:
            direct_text = self._get_direct_text(node)
            if direct_text:
                t = direct_text[:MAX_TEXT_LEN]
                if len(direct_text) > MAX_TEXT_LEN:
                    t += "..."
                lines.append(f"{prefix}<{tag}{attrs_str}>{t}</{tag}>")
            else:
                lines.append(f"{prefix}<{tag}{attrs_str}/>")

        return fold_counter

    def _serialize_children_with_folding(
        self,
        children: list,
        lines: list,
        virtual_refs: dict[str, str],
        fold_counter: int,
    ) -> int:
        """Serialize children, folding consecutive same-tag groups."""
        threshold = self._fold_threshold

        i = 0
        while i < len(children):
            # Find consecutive group of same-tag visible element children
            child = children[i]
            if child.is_text:
                if not child.is_visible or not self._renderable_text(child):
                    i += 1
                    continue
                fold_counter = self._serialize_node_with_folding(
                    child, lines, virtual_refs, fold_counter
                )
                i += 1
                continue
            if not child.is_visible:
                fold_counter = self._serialize_node_with_folding(
                    child, lines, virtual_refs, fold_counter
                )
                i += 1
                continue

            tag = child.tag
            group_end = i + 1
            while group_end < len(children):
                next_child = children[group_end]
                if next_child.is_text:
                    if not next_child.is_visible or not self._renderable_text(next_child):
                        group_end += 1
                        continue
                    break
                if not next_child.is_visible:
                    group_end += 1
                    continue
                if next_child.tag != tag:
                    break
                group_end += 1

            # Count visible same-tag elements (skip invisible in count)
            visible_indices = [
                j for j in range(i, group_end)
                if not children[j].is_text and children[j].is_visible
            ]
            group_size = len(visible_indices)

            if group_size <= threshold or tag not in FOLDABLE_TAGS:
                # No folding — either too few items or tag is not safe to fold
                # (e.g. <div>/<section> may contain heterogeneous content)
                for j in range(i, group_end):
                    fold_counter = self._serialize_node_with_folding(
                        children[j], lines, virtual_refs, fold_counter
                    )
            else:
                # Fold: keep first 2 visible + last 1 visible, fold the middle
                first_two = visible_indices[:2]
                last_one = visible_indices[-1]

                # Serialize first 2 visible
                for j in first_two:
                    fold_counter = self._serialize_node_with_folding(
                        children[j], lines, virtual_refs, fold_counter
                    )

                # Collect folded items (visible only)
                middle = visible_indices[2:-1]
                folded_lines: list[str] = []
                for j in middle:
                    self._serialize_node_raw(children[j], folded_lines)

                fold_counter += 1
                fold_ref = f"f{fold_counter}"
                folded_count = len(middle)
                folded_content = "\n".join(folded_lines)

                # Collect ref range info
                ref_range = self._get_fold_ref_range(children, middle[0], middle[-1] + 1)

                if ref_range:
                    lines.append(
                        f"[ref={fold_ref}]"
                        f"... {folded_count} similar <{tag}> items "
                        f"(refs {ref_range}), click to expand ...[/ref={fold_ref}]"
                    )
                else:
                    lines.append(
                        f"[ref={fold_ref}]"
                        f"... {folded_count} similar <{tag}> items, "
                        f"click to expand ...[/ref={fold_ref}]"
                    )

                # Include already-shown items in stored content for context
                shown_lines: list[str] = []
                for j in first_two:
                    self._serialize_node_raw(children[j], shown_lines)
                last_lines: list[str] = []
                self._serialize_node_raw(children[last_one], last_lines)

                virtual_refs[fold_ref] = (
                    "\n".join(shown_lines) + "\n"
                    + folded_content + "\n"
                    + "\n".join(last_lines)
                )

                # Serialize last 1 visible
                fold_counter = self._serialize_node_with_folding(
                    children[last_one], lines, virtual_refs, fold_counter
                )

            i = group_end

        return fold_counter

    def _serialize_node_raw(self, node: DomNode, lines: list):
        """Serialize node without folding (for virtual ref content)."""
        if node.is_text:
            text = self._renderable_text(node)
            if text:
                lines.append(text[:MAX_TEXT_LEN])
            return

        tag = node.tag
        if tag in SKIP_TAGS or tag in ("svg", "math"):
            return

        prefix = f"[ref={node.ref}]" if node.ref else ""
        include_href = tag != "a" or bool(node.ref)
        attrs_str = self._format_attrs(node, include_href=include_href)
        attrs_str += self._format_compound_control(node)

        has_element_children = any(not c.is_text for c in node.children)
        if has_element_children:
            lines.append(f"{prefix}<{tag}{attrs_str}>")
            for child in node.children:
                self._serialize_node_raw(child, lines)
            lines.append(f"</{tag}>")
        else:
            direct_text = self._get_direct_text(node)
            if direct_text:
                lines.append(f"{prefix}<{tag}{attrs_str}>{direct_text[:MAX_TEXT_LEN]}</{tag}>")
            else:
                lines.append(f"{prefix}<{tag}{attrs_str}/>")

    @staticmethod
    def _get_fold_ref_range(
        children: list, start: int, end: int
    ) -> str:
        """Get ref range string for folded items (e.g. 'e3-e7')."""
        refs = []
        for j in range(start, end):
            if hasattr(children[j], "ref") and children[j].ref:
                refs.append(children[j].ref)
        if not refs:
            return ""
        return f"{refs[0]}-{refs[-1]}"

    # ── Output composition (density sort + truncation) ─────────────────

    def _compose_output(
        self, sections: list[tuple[int, str]]
    ) -> tuple[str, dict[str, str]]:
        """Compose final output from sections.

        Returns (text, truncation_virtual_refs).

        Each truncated section (partial or fully excluded) gets its own
        numbered virtual ref so the LLM can selectively expand what it needs.
        """
        full_text = "\n".join(s[1] for s in sections)
        if len(full_text) <= self._max_chars:
            return full_text, {}

        # Phase 1: Pick which sections to include (by density priority)
        sorted_sections = sorted(sections, key=lambda x: -x[0])
        included_ids: set[int] = set()
        budget_for: dict[int, int] = {}  # section id → max chars allowed
        remaining = self._max_chars

        for _, section_text in sorted_sections:
            if len(section_text) <= remaining:
                included_ids.add(id(section_text))
                budget_for[id(section_text)] = len(section_text)
                remaining -= len(section_text)
            elif remaining > 0:
                included_ids.add(id(section_text))
                budget_for[id(section_text)] = remaining
                remaining = 0
                break

        # Phase 2: Output in document order, respecting budget.
        # Partially-truncated sections get an inline ref for the remainder.
        result: list[str] = []
        trunc_idx: int = 0
        virtual_refs: dict[str, str] = {}

        for _, section_text in sections:
            sid = id(section_text)
            if sid in included_ids:
                budget = budget_for[sid]
                if len(section_text) <= budget:
                    result.append(section_text)
                else:
                    result.append(section_text[:budget])
                    remainder = section_text[budget:]
                    if remainder.strip():
                        trunc_idx += 1
                        ref_key = f"t{trunc_idx}"
                        result.append(
                            self._build_trunc_marker(ref_key, remainder, "truncated")
                        )
                        self._store_virtual_ref_paginated(ref_key, remainder, virtual_refs)

        # Phase 3: Fully excluded sections — each gets its own ref
        for _, section_text in sections:
            if id(section_text) not in included_ids:
                if section_text.strip():
                    trunc_idx += 1
                    ref_key = f"t{trunc_idx}"
                    result.append(
                        self._build_trunc_marker(ref_key, section_text, "excluded")
                    )
                    self._store_virtual_ref_paginated(ref_key, section_text, virtual_refs)

        if not virtual_refs:
            return "\n".join(result) if result else full_text[:self._max_chars], {}

        return "\n".join(result), virtual_refs

    def _build_trunc_marker(
        self, ref_key: str, content: str, label: str
    ) -> str:
        """Build a clickable truncation marker with beginning/end preview."""
        preview_beginning = content[:200].strip()
        preview_end = content[-200:].strip()
        return (
            f"\n[ref={ref_key}]\n"
            f"--- {label} content ({len(content)} chars, click to expand) ---\n"
            f'  Beginning: "{preview_beginning[:150]}..."\n'
            f'  End: "...{preview_end[-150:]}"\n'
            f"---\n"
            f"[/ref={ref_key}]\n"
        )

    def _store_virtual_ref_paginated(
        self, ref_key: str, content: str, virtual_refs: dict[str, str]
    ) -> None:
        """Store content as a virtual ref, paginating if it exceeds chunk size.

        Long content is split into chunks linked by sub-refs (t1 → t1-a → t1-a-a),
        so the LLM can keep clicking to drill deeper without hitting a dead end.
        """
        if len(content) <= VIRTUAL_REF_CHUNK_SIZE:
            virtual_refs[ref_key] = content
            return

        chunk = content[:VIRTUAL_REF_CHUNK_SIZE]
        rest = content[VIRTUAL_REF_CHUNK_SIZE:]

        sub_key = f"{ref_key}-a"
        sub_marker = self._build_trunc_marker(sub_key, rest, "continued")

        # Recurse so the sub-ref is itself paginated if needed
        self._store_virtual_ref_paginated(sub_key, rest, virtual_refs)

        virtual_refs[ref_key] = chunk + sub_marker

    # ── Serialization helpers ──────────────────────────────────────────

    def _serialize(self, root: DomNode) -> str:
        """Simple full serialization (fallback, no folding)."""
        lines = []
        self._serialize_simple(root, lines)
        return "\n".join(lines)

    def _serialize_simple(self, node: DomNode, lines: list):
        if node.is_text:
            text = self._renderable_text(node)
            if text:
                lines.append(text[:MAX_TEXT_LEN])
            return

        tag = node.tag
        if not node.is_visible:
            for child in node.children:
                self._serialize_simple(child, lines)
            return
        if tag in SKIP_TAGS or tag in ("svg", "math"):
            return

        prefix = f"[ref={node.ref}]" if node.ref else ""
        include_href = tag != "a" or bool(node.ref)
        attrs_str = self._format_attrs(node, include_href=include_href)
        attrs_str += self._format_compound_control(node)

        has_element_children = any(not c.is_text for c in node.children)
        if has_element_children:
            lines.append(f"{prefix}<{tag}{attrs_str}>")
            for child in node.children:
                self._serialize_simple(child, lines)
            lines.append(f"</{tag}>")
        else:
            direct_text = self._get_direct_text(node)
            if direct_text:
                lines.append(f"{prefix}<{tag}{attrs_str}>{direct_text[:MAX_TEXT_LEN]}</{tag}>")
            else:
                lines.append(f"{prefix}<{tag}{attrs_str}/>")

    def _format_attrs(self, node: DomNode, include_href: bool = True) -> str:
        tag = node.tag
        attrs = node.attributes
        parts = []

        for key in RICH_ATTRS:
            val = attrs.get(key)
            if val is None:
                continue
            if not val and key != "type":
                continue
            if tag == "input" and attrs.get("type") == "password" and key == "value":
                continue
            if key == "href":
                if not include_href:
                    continue
                if val.startswith("javascript:"):
                    continue
                if len(val) > MAX_HREF_LEN:
                    val = val[:MAX_HREF_LEN] + "..."
            if len(val) > MAX_ATTR_LEN:
                val = val[:MAX_ATTR_LEN] + "..."
            parts.append(f' {key}="{val}"')

        return "".join(parts)

    @staticmethod
    def _format_compound_control(node: DomNode) -> str:
        tag = node.tag
        attrs = node.attributes
        input_type = attrs.get("type", "").lower()

        if tag == "input":
            if input_type in ("date", "time", "datetime-local", "month", "week"):
                format_map = {
                    "date": "YYYY-MM-DD", "time": "HH:MM",
                    "datetime-local": "YYYY-MM-DDTHH:MM", "month": "YYYY-MM",
                    "week": "YYYY-W##",
                }
                fmt = format_map.get(input_type, "")
                placeholder = attrs.get("placeholder", "")
                if placeholder and placeholder != fmt:
                    return ""
                return f' format="{fmt}" placeholder="{fmt}"'
            elif input_type == "range":
                mn = attrs.get("min", "0")
                mx = attrs.get("max", "100")
                return f" (slider, min={mn} max={mx})"
            elif input_type == "number":
                mn = attrs.get("min", "")
                mx = attrs.get("max", "")
                hint = " (spinner"
                if mn:
                    hint += f" min={mn}"
                if mx:
                    hint += f" max={mx}"
                return hint + ")"
            elif input_type == "color":
                return " (color picker: hex value + palette)"
            elif input_type == "file":
                multiple = "multiple" in attrs
                return f" (file picker, {'multi' if multiple else 'single'} file)"

        elif tag == "select":
            option_count = 0
            first_options: list[str] = []
            for child in node.children:
                if child.tag == "option" and option_count < 4:
                    text = child.node_value.strip() if child.is_text else ""
                    if not text and child.children:
                        for tc in child.children:
                            if tc.is_text and tc.node_value.strip():
                                text = tc.node_value.strip()
                                break
                    if text:
                        first_options.append(text[:25])
                    option_count += 1
                elif child.tag == "option":
                    option_count += 1
            if option_count > 0:
                preview = "|".join(first_options[:4])
                if option_count > 4:
                    preview += f"|+{option_count - 4} more"
                return f" (dropdown, {option_count} options: {preview})"

        elif tag == "details":
            return " (expandable: click to toggle)"

        return ""

    @staticmethod
    def _renderable_text(node: DomNode) -> Optional[str]:
        """Return stripped text if renderable, None if noise (empty/whitespace)."""
        text = node.node_value.strip()
        return text if text else None

    def _get_direct_text(self, node: DomNode) -> str:
        texts = []
        for child in node.children:
            if child.is_text and child.node_value.strip():
                texts.append(child.node_value.strip())
        return " ".join(texts)[:MAX_TEXT_LEN]

    @staticmethod
    def _find_body(node: DomNode) -> Optional[DomNode]:
        if node.tag == "body":
            return node
        for child in node.children:
            found = DomService._find_body(child)
            if found:
                return found
        return None


# ── Helpers ──────────────────────────────────────────────────────────────

def _walk_collect_iframes(node, result: list) -> None:
    if node.tag in ("iframe", "frame"):
        result.append(node)
    for child in node.children:
        _walk_collect_iframes(child, result)


# ── Singleton ────────────────────────────────────────────────────────────

_dom_service: Optional[DomService] = None


def get_dom_service() -> DomService:
    global _dom_service
    if _dom_service is None:
        _dom_service = DomService()
    return _dom_service
