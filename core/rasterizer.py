"""Renders UV wireframe data to a transparent PNG at a target resolution.

Coordinate convention: UV (0,0) is bottom-left in most DCC/interchange
formats, but image (0,0) is top-left, so V is flipped on render.

Width and height are independent: UVs are stretched to fill the canvas on
each axis separately (u * width, v * height), so non-square outputs will
distort a square UV layout rather than letterbox it.
"""

from dataclasses import dataclass, field

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen

from core.mesh_data import UVMesh

# Above this resolution (on the larger dimension), disable antialiasing: AA
# cost scales poorly and lines are dense enough at high pixel counts that
# aliasing is a non-issue.
AA_DISABLE_THRESHOLD = 8192

DEFAULT_COLORS = [
    QColor(0, 0, 0, 255),
    QColor(200, 30, 30, 255),
    QColor(30, 120, 200, 255),
    QColor(40, 160, 60, 255),
    QColor(180, 120, 20, 255),
    QColor(150, 50, 180, 255),
    QColor(0, 150, 150, 255),
    QColor(200, 100, 150, 255),
]


@dataclass
class RenderOptions:
    width: int = 4096
    height: int = 4096
    line_width: float = 1.0
    line_color: tuple[int, int, int, int] = (0, 0, 0, 255)
    color_by_group: bool = False
    included_group_names: set[str] | None = None  # None = include all
    draw_checker_background: bool = False
    checker_size: int = 64
    checker_colors: tuple[tuple[int, int, int, int], tuple[int, int, int, int]] = (
        (235, 235, 235, 255),
        (215, 215, 215, 255),
    )
    # Two independent knobs for matching the game's own shipped UV maps,
    # since either one is useful on its own: dropping the interior
    # triangulation still leaves a wireframe (just island outlines instead
    # of a triangle mesh), and filling islands is meaningful whether or not
    # the interior triangulation is also being drawn on top of that fill.
    #
    # island_silhouette_only: draws only each UV island's outer edge, no
    # interior triangulation at all -- "less triangle mesh, more like the
    # game's clean island outlines". An edge is treated as part of the
    # silhouette when only one triangle in the group uses it (i.e. it isn't
    # shared between two triangles), which is exactly what an island's
    # outer boundary looks like topologically.
    island_silhouette_only: bool = False
    # hide_quad_diagonals: when two triangles in a group share an edge, and
    # the quadrilateral formed by that edge's two endpoints plus each
    # triangle's opposite (non-shared) vertex is convex, that shared edge
    # is almost certainly just the triangulation split of what was
    # originally a quad face rather than a deliberate/structural edge --
    # hiding just those (and nothing else) gives a cleaner look closer to
    # the source quad topology without going as far as dropping all
    # interior structure the way island_silhouette_only does. A concave
    # quad is left alone, since that's what a genuine intentional edge
    # (e.g. a hard seam) typically produces instead. Has no visible effect
    # when island_silhouette_only is also on, since there's no interior
    # triangulation left to filter in the first place.
    hide_quad_diagonals: bool = False
    # island_fill: fills each island's interior with a flat translucent
    # color, alongside the normal interior triangulation wireframe.
    island_fill: bool = False
    fill_color: tuple[int, int, int, int] = (255, 255, 255, 255)
    fill_opacity: float = 0.2  # 0.0-1.0, applied on top of fill_color's own alpha
    # Boundary/silhouette pass now matches line_width/line_color by default
    # (see __post_init__) instead of being forced bolder -- the earlier
    # hardcoded 2.5px boundary was never something the user asked for, it
    # was just always on whenever island fill was enabled. These stay
    # available as explicit overrides for anyone who *does* want a heavier
    # outline; pass them in directly if so.
    boundary_line_width: float | None = None
    boundary_line_color: tuple[int, int, int, int] | None = None

    def __post_init__(self):
        if self.boundary_line_width is None:
            self.boundary_line_width = self.line_width
        if self.boundary_line_color is None:
            self.boundary_line_color = self.line_color


def _draw_checker(painter: QPainter, width: int, height: int, opts: RenderOptions) -> None:
    c1 = QColor(*opts.checker_colors[0])
    c2 = QColor(*opts.checker_colors[1])
    size = max(1, opts.checker_size)
    painter.setPen(Qt.NoPen)
    y = 0
    row = 0
    while y < height:
        x = 0
        col = 0
        while x < width:
            use_c1 = (row + col) % 2 == 0
            painter.setBrush(c1 if use_c1 else c2)
            w = min(size, width - x)
            h = min(size, height - y)
            painter.drawRect(x, y, w, h)
            x += size
            col += 1
        y += size
        row += 1


def _boundary_edges(triangles: list[tuple[int, int, int]]) -> set[tuple[int, int]]:
    """Returns the set of edges that belong to only one triangle in this
    group (an edge shared by two triangles is interior to the surface). A
    boundary edge is exactly what happens along a UV island's outer
    silhouette, since there's no neighboring face on the other side within
    this UV layout. This is a purely topological test on already-computed
    triangle indices, so it costs one dict pass over the group's edges
    regardless of how the mesh was originally unwrapped.
    """
    edge_use_count: dict[tuple[int, int], int] = {}
    for a, b, c in triangles:
        for i1, i2 in ((a, b), (b, c), (c, a)):
            key = (i1, i2) if i1 < i2 else (i2, i1)
            edge_use_count[key] = edge_use_count.get(key, 0) + 1

    return {k for k, count in edge_use_count.items() if count == 1}


def _classify_edges(
    triangles: list[tuple[int, int, int]]
) -> tuple[set[tuple[int, int]], set[tuple[int, int]], dict[tuple[int, int], list[int]]]:
    """Single-pass companion to _boundary_edges() that also returns the
    interior edges (used by two or more triangles -- more than two only
    happens on non-manifold/garbage input, but is still treated as
    interior rather than dropped) and, for each edge, the "opposite"
    vertex of every triangle that uses it (the third vertex, not on the
    edge itself). That opposite-vertex data is exactly what
    _quad_diagonal_edges() needs to test whether a shared edge looks like
    the diagonal of a former quad face.
    """
    edge_use_count: dict[tuple[int, int], int] = {}
    edge_opposite: dict[tuple[int, int], list[int]] = {}
    for a, b, c in triangles:
        for i1, i2, opp in ((a, b, c), (b, c, a), (c, a, b)):
            key = (i1, i2) if i1 < i2 else (i2, i1)
            edge_use_count[key] = edge_use_count.get(key, 0) + 1
            edge_opposite.setdefault(key, []).append(opp)

    boundary = {k for k, count in edge_use_count.items() if count == 1}
    interior = {k for k, count in edge_use_count.items() if count >= 2}
    return interior, boundary, edge_opposite


def _is_convex_quad(points: list[QPointF]) -> bool:
    """True if the four points, taken in order, form a convex quadrilateral
    -- checked by confirming every consecutive turn (the cross product of
    each pair of adjacent edge vectors) has the same sign all the way
    around. Collinear points (near-zero cross product) are skipped rather
    than treated as a sign change, since a straight-line "corner" doesn't
    contradict convexity on its own."""
    n = len(points)
    sign = None
    for i in range(n):
        p0, p1, p2 = points[i], points[(i + 1) % n], points[(i + 2) % n]
        v1x, v1y = p1.x() - p0.x(), p1.y() - p0.y()
        v2x, v2y = p2.x() - p1.x(), p2.y() - p1.y()
        cross = v1x * v2y - v1y * v2x
        if abs(cross) < 1e-9:
            continue
        turn = cross > 0
        if sign is None:
            sign = turn
        elif turn != sign:
            return False
    return sign is not None


def _quad_diagonal_edges(
    interior_edges: set[tuple[int, int]],
    edge_opposite: dict[tuple[int, int], list[int]],
    uvs_px: list[QPointF],
) -> set[tuple[int, int]]:
    """Of the given interior edges, returns the subset that look like the
    diagonal of a former quad face: the edge is shared by exactly two
    triangles, and the quadrilateral formed by the edge's two endpoints
    plus each triangle's opposite vertex is convex. Edges shared by more
    than two triangles (non-manifold) are left alone -- there's no single
    well-defined "quad" to test there."""
    diagonals = set()
    for key in interior_edges:
        opps = edge_opposite.get(key, [])
        if len(opps) != 2:
            continue
        i1, i2 = key
        opp1, opp2 = opps
        try:
            p1, p2 = uvs_px[i1], uvs_px[i2]
            po1, po2 = uvs_px[opp1], uvs_px[opp2]
        except IndexError:
            continue
        if _is_convex_quad([p1, po1, p2, po2]):
            diagonals.add(key)
    return diagonals


def _polygon_signed_area(points: list[QPointF]) -> float:
    """Shoelace signed area. Sign indicates winding direction; magnitude/2
    is the polygon's area. Used to tell an island's outer loop (larger
    area) apart from any hole loops nested inside it (smaller area,
    opposite winding from a consistent triangulation)."""
    area = 0.0
    n = len(points)
    for i in range(n):
        x1, y1 = points[i].x(), points[i].y()
        x2, y2 = points[(i + 1) % n].x(), points[(i + 1) % n].y()
        area += x1 * y2 - x2 * y1
    return area / 2.0


def _bbox(points: list[QPointF]) -> tuple[float, float, float, float]:
    """(min_x, min_y, max_x, max_y) of a loop's points, used as a cheap
    O(n) fast-reject before the O(n) point-in-polygon test in
    _polygon_fully_inside -- most island pairs on a real mesh don't
    overlap at all, and a bbox check throws those out without ever
    touching the per-vertex ray-cast."""
    xs = [p.x() for p in points]
    ys = [p.y() for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def _bbox_fully_inside(inner: tuple[float, float, float, float], outer: tuple[float, float, float, float]) -> bool:
    return (
        inner[0] >= outer[0]
        and inner[1] >= outer[1]
        and inner[2] <= outer[2]
        and inner[3] <= outer[3]
    )


def _point_in_polygon(pt: QPointF, points: list[QPointF]) -> bool:
    """Standard ray-casting point-in-polygon test (boundary-inclusive
    enough for this use), used only to determine loop containment for
    island clustering -- not for per-pixel fill, so it doesn't need to be
    fast, just correct."""
    x, y = pt.x(), pt.y()
    inside = False
    n = len(points)
    for i in range(n):
        x1, y1 = points[i].x(), points[i].y()
        x2, y2 = points[(i + 1) % n].x(), points[(i + 1) % n].y()
        if (y1 > y) != (y2 > y):
            x_at_y = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < x_at_y:
                inside = not inside
    return inside


def _polygon_fully_inside(
    inner: list[QPointF],
    outer: list[QPointF],
    inner_bbox: tuple[float, float, float, float] | None = None,
    outer_bbox: tuple[float, float, float, float] | None = None,
) -> bool:
    """True only if every vertex of `inner` lies inside `outer`. A genuine
    hole loop is fully enclosed by its island's outer loop; two islands
    that merely overlap in UV space will each have at least one vertex
    outside the other, so this -- rather than testing just one sample
    point -- is what tells "B is a hole in A" apart from "A and B happen
    to overlap but are unrelated shapes".

    Precomputed bboxes can be passed in to skip the per-call recompute
    (see _group_loops_into_islands, which computes each loop's bbox once
    up front rather than on every pairwise comparison). If inner's bbox
    isn't itself contained in outer's bbox, inner can't possibly be fully
    inside outer, so the expensive per-vertex ray-cast is skipped entirely.
    """
    if inner_bbox is None:
        inner_bbox = _bbox(inner)
    if outer_bbox is None:
        outer_bbox = _bbox(outer)
    if not _bbox_fully_inside(inner_bbox, outer_bbox):
        return False
    return all(_point_in_polygon(p, outer) for p in inner)


def _group_loops_into_islands(loops_px: list[list[QPointF]]) -> list[list[list[QPointF]]]:
    """Groups a flat list of boundary loops (already converted to pixel
    points) into islands: each island is one outer loop plus whatever hole
    loops are fully nested inside it. This is what lets each island be
    filled as its own independent QPainterPath -- so two unrelated islands
    that happen to overlap in UV space simply paint over each other like
    any other overlapping shapes, while a genuine hole inside a single
    island still correctly cuts out through odd-even fill within that one
    island's own path.

    Loops are sorted largest-area-first and each smaller loop is assigned
    as a hole of the smallest already-placed loop that fully contains it
    (i.e. its most immediate/tightest enclosing loop), so holes-within-
    holes (an island inside a hole inside another island) nest correctly
    too.

    Performance: on a mesh with many small islands (a full vehicle body
    easily has hundreds), the naive version of this check -- an O(n)
    point-in-polygon test per pair of loops, run twice per pair (once for
    "who's my parent", once again for "what's my nesting depth") -- is
    what made "Fill islands" noticeably freeze the UI. Two things fix that
    without changing the result:
      1. Each loop's bbox is computed once up front and reused for every
         comparison involving that loop, so non-overlapping islands (the
         overwhelming majority of pairs on a real mesh) are rejected in
         O(1) instead of paying for a full per-vertex ray-cast.
      2. Containment against every already-placed loop is computed once
         per loop (not twice) and reused for both the immediate-parent
         lookup and the nesting-depth count.
    """
    indexed = sorted(
        range(len(loops_px)),
        key=lambda i: abs(_polygon_signed_area(loops_px[i])),
        reverse=True,
    )
    by_area = [loops_px[i] for i in indexed]
    bboxes = [_bbox(loop) for loop in by_area]

    islands: list[list[list[QPointF]]] = []
    placed: list[list[QPointF]] = []  # flat, in placement order (largest first)
    placed_bboxes: list[tuple[float, float, float, float]] = []

    for loop, loop_bbox in zip(by_area, bboxes):
        # Single pass over already-placed loops: collect every placed loop
        # that fully contains this one, tracking both the tightest
        # (smallest-area) match for "immediate parent" and the total count
        # for nesting depth -- covers what used to be two separate O(n)
        # passes making the same containment calls.
        containing_indices: list[int] = []
        parent_idx = None
        parent_area = None
        for idx, (candidate, candidate_bbox) in enumerate(zip(placed, placed_bboxes)):
            if _polygon_fully_inside(loop, candidate, loop_bbox, candidate_bbox):
                containing_indices.append(idx)
                area = abs(_polygon_signed_area(candidate))
                if parent_area is None or area < parent_area:
                    parent_area = area
                    parent_idx = idx

        if parent_idx is None:
            # Not fully inside anything already placed -- this is a new
            # island's outer loop (this also covers the "merely overlaps"
            # case, since a loop that only partially overlaps another
            # isn't fully inside it).
            islands.append([loop])
        else:
            # Fully nested inside something. An odd nesting depth means
            # this loop is a hole cutting into its immediate parent; an
            # even depth means it's actually a solid island sitting inside
            # a hole (e.g. a smaller separate part placed inside a
            # cutout).
            depth = len(containing_indices)
            if depth % 2 == 1:
                for island in islands:
                    if island[0] is placed[parent_idx] or any(
                        h is placed[parent_idx] for h in island[1:]
                    ):
                        island.append(loop)
                        break
                else:
                    islands.append([loop])
            else:
                islands.append([loop])

        placed.append(loop)
        placed_bboxes.append(loop_bbox)

    return islands


def _build_island_loops(triangles: list[tuple[int, int, int]]) -> list[list[int]]:
    """Walks each group's boundary edges into one or more closed polygon
    loops (vertex-index order) -- one loop per UV island (or per hole
    within an island). Each triangle winds its edges in a consistent
    direction, so re-deriving a directed version of each boundary edge from
    the triangle that owns it lets the loops be walked head-to-tail instead
    of guessing a direction. Returns [] if the edges don't form clean
    closed loops (e.g. a degenerate/non-manifold group); the caller falls
    back to per-edge line drawing in that case instead of a garbled fill.
    """
    boundary = _boundary_edges(triangles)
    if not boundary:
        return []

    directed: dict[int, int] = {}
    for a, b, c in triangles:
        for i1, i2 in ((a, b), (b, c), (c, a)):
            key = (i1, i2) if i1 < i2 else (i2, i1)
            if key in boundary:
                directed[i1] = i2

    if len(directed) != len(boundary):
        return []  # a vertex started more than one boundary edge -- non-manifold

    visited_starts: set[int] = set()
    loops: list[list[int]] = []
    for start in directed:
        if start in visited_starts:
            continue
        loop = [start]
        visited_starts.add(start)
        current = directed[start]
        steps = 0
        while current != start and steps <= len(directed) + 1:
            loop.append(current)
            visited_starts.add(current)
            nxt = directed.get(current)
            if nxt is None:
                return []  # dangling edge, not a clean closed loop
            current = nxt
            steps += 1
        if current != start:
            return []  # walked too far without closing -- non-manifold
        loops.append(loop)

    return loops


def render_uv_template(mesh: UVMesh, opts: RenderOptions) -> QImage:
    """Renders the given mesh's UV groups to a QImage at opts.width x opts.height."""
    width = opts.width
    height = opts.height
    img = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)

    painter = QPainter(img)
    try:
        painter.setRenderHint(
            QPainter.Antialiasing, max(width, height) <= AA_DISABLE_THRESHOLD
        )

        if opts.draw_checker_background:
            _draw_checker(painter, width, height, opts)

        base_pen = QPen(QColor(*opts.line_color))
        base_pen.setWidthF(max(0.1, opts.line_width))
        base_pen.setCosmetic(False)
        painter.setPen(base_pen)

        groups = mesh.groups
        if opts.included_group_names is not None:
            groups = [g for g in groups if g.name in opts.included_group_names]

        if opts.island_fill:
            boundary_pen = QPen(QColor(*opts.boundary_line_color))
            boundary_pen.setWidthF(max(0.1, opts.boundary_line_width))
            boundary_pen.setCosmetic(False)

            interior_pen = QPen(QColor(*opts.line_color))
            interior_pen.setWidthF(max(0.1, opts.line_width))
            interior_pen.setCosmetic(False)

            fill_base = QColor(*opts.fill_color)
            fill_alpha = round(fill_base.alpha() * max(0.0, min(1.0, opts.fill_opacity)))
            fill_color = QColor(fill_base.red(), fill_base.green(), fill_base.blue(), fill_alpha)

            # Pre-compute each group's boundary loops and UV-space points
            # once, reused across both the fill phase and the line phase
            # below instead of recomputing per phase.
            group_loops: list[list[list[int]]] = []
            group_uvs_px: list[list[QPointF]] = []
            for group in groups:
                group_uvs_px.append(
                    [QPointF(u * width, (1.0 - v) * height) for (u, v) in group.uvs]
                )
                group_loops.append(_build_island_loops(group.triangles))

            # ---- Fill phase: every island across every group is unioned
            # into shared QPainterPath(s) and drawn in as few drawPath()
            # calls as possible, before any lines. Islands that overlap in
            # UV space (common for tiled/mirrored panels sharing texture
            # space) used to each get their own drawPath() call with the
            # same translucent brush -- Qt composites each call against
            # what's already on the canvas, so an overlap region ended up
            # painted twice and came out visibly lighter/darker than the
            # rest of the fill. QPainterPath.addPath() unions shapes
            # geometrically before any pixels are touched, so accumulating
            # into one path first and drawing once fixes that regardless of
            # how many islands cover a given region.
            #
            # When color_by_group is on, each group's color is real
            # information the user asked to see, so unioning stays
            # per-group (still fixing overlap *within* a group) rather than
            # merging across groups, which would erase the distinct colors.
            shared_fill_path: QPainterPath | None = None
            if not opts.color_by_group:
                shared_fill_path = QPainterPath()
                shared_fill_path.setFillRule(Qt.WindingFill)

            for gi, group in enumerate(groups):
                uvs_px = group_uvs_px[gi]
                loops = group_loops[gi]
                if not loops:
                    continue

                loops_px: list[list[QPointF]] = []
                for loop in loops:
                    try:
                        points = [uvs_px[i] for i in loop]
                    except IndexError:
                        continue
                    if len(points) >= 3:
                        loops_px.append(points)
                if not loops_px:
                    continue

                group_fill_path = QPainterPath()
                group_fill_path.setFillRule(Qt.WindingFill)
                for island_loops in _group_loops_into_islands(loops_px):
                    for loop_points in island_loops:
                        island_path = QPainterPath()
                        island_path.moveTo(loop_points[0])
                        for pt in loop_points[1:]:
                            island_path.lineTo(pt)
                        island_path.closeSubpath()
                        group_fill_path.addPath(island_path)

                if opts.color_by_group:
                    color = DEFAULT_COLORS[gi % len(DEFAULT_COLORS)]
                    painter.setPen(Qt.NoPen)
                    painter.setBrush(color)
                    painter.drawPath(group_fill_path)
                    painter.setBrush(Qt.NoBrush)
                else:
                    shared_fill_path.addPath(group_fill_path)

            if shared_fill_path is not None:
                painter.setPen(Qt.NoPen)
                painter.setBrush(fill_color)
                painter.drawPath(shared_fill_path)
                painter.setBrush(Qt.NoBrush)

            # ---- Line phase: interior triangulation, then island boundary
            # outlines drawn last so they never get visually broken up by
            # an interior line crossing them -- per group so
            # color_by_group still gives each group its own line color. ----
            for gi, group in enumerate(groups):
                uvs_px = group_uvs_px[gi]
                loops = group_loops[gi]

                if opts.color_by_group:
                    color = DEFAULT_COLORS[gi % len(DEFAULT_COLORS)]
                    boundary_pen = QPen(color)
                    boundary_pen.setWidthF(max(0.1, opts.boundary_line_width))
                    interior_pen = QPen(color)
                    interior_pen.setWidthF(max(0.1, opts.line_width))

                # Only the true interior edges (shared by two-or-more
                # triangles) are drawn here -- boundary edges are handled
                # by the loop-based pass below instead, so each edge is
                # drawn exactly once regardless of which pen it ends up
                # with. Skipped entirely when island_silhouette_only is on,
                # since that means no interior triangulation at all.
                if not opts.island_silhouette_only:
                    painter.setPen(interior_pen)
                    interior_edges, _boundary, edge_opposite = _classify_edges(group.triangles)
                    if opts.hide_quad_diagonals:
                        interior_edges = interior_edges - _quad_diagonal_edges(
                            interior_edges, edge_opposite, uvs_px
                        )
                    for i1, i2 in interior_edges:
                        try:
                            painter.drawLine(uvs_px[i1], uvs_px[i2])
                        except IndexError:
                            continue

                painter.setPen(boundary_pen)
                if loops:
                    for loop in loops:
                        try:
                            points = [uvs_px[i] for i in loop]
                        except IndexError:
                            continue
                        if len(points) < 2:
                            continue
                        for i in range(len(points)):
                            painter.drawLine(points[i], points[(i + 1) % len(points)])
                else:
                    # Non-manifold/degenerate group: fall back to drawing
                    # just the boundary edges as unordered line segments.
                    boundary = _boundary_edges(group.triangles)
                    for i1, i2 in boundary:
                        try:
                            painter.drawLine(uvs_px[i1], uvs_px[i2])
                        except IndexError:
                            continue
        else:
            for gi, group in enumerate(groups):
                if opts.color_by_group:
                    color = DEFAULT_COLORS[gi % len(DEFAULT_COLORS)]
                    boundary_pen = QPen(color)
                    boundary_pen.setWidthF(max(0.1, opts.boundary_line_width))
                    interior_pen = QPen(color)
                    interior_pen.setWidthF(max(0.1, opts.line_width))
                else:
                    boundary_pen = QPen(QColor(*opts.boundary_line_color))
                    boundary_pen.setWidthF(max(0.1, opts.boundary_line_width))
                    interior_pen = QPen(QColor(*opts.line_color))
                    interior_pen.setWidthF(max(0.1, opts.line_width))

                uvs_px = [
                    QPointF(u * width, (1.0 - v) * height) for (u, v) in group.uvs
                ]

                # Same interior/boundary split as the island_fill branch
                # above, so island_silhouette_only and hide_quad_diagonals
                # behave identically whether or not fill is also on.
                interior_edges, boundary_edges, edge_opposite = _classify_edges(group.triangles)

                if not opts.island_silhouette_only:
                    if opts.hide_quad_diagonals:
                        interior_edges = interior_edges - _quad_diagonal_edges(
                            interior_edges, edge_opposite, uvs_px
                        )
                    painter.setPen(interior_pen)
                    for i1, i2 in interior_edges:
                        try:
                            painter.drawLine(uvs_px[i1], uvs_px[i2])
                        except IndexError:
                            continue

                painter.setPen(boundary_pen)
                for i1, i2 in boundary_edges:
                    try:
                        painter.drawLine(uvs_px[i1], uvs_px[i2])
                    except IndexError:
                        continue
    finally:
        painter.end()

    return img


def save_render(img: QImage, out_path: str) -> bool:
    return img.save(out_path, "PNG")
