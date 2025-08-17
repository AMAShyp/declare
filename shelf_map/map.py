# shelf_map/map.py
import math, time, logging, inspect
import streamlit as st
import plotly.graph_objects as go
from PIL import Image

from shelf_map.shelf_map_handler import ShelfMapHandler
from shelf_map.shelf_map_utils   import shelf_selector, item_locator

# ────────────────────────────────────────────────────────── helpers
log  = logging.getLogger("shelfmap"); log.setLevel(logging.INFO)
ping = lambda m,t0: st.write(f"⌛ {m} in {time.time()-t0:.2f}s")

handler = ShelfMapHandler()

# Show Plotly events for debugging when True
DEBUG_EVENTS = False

# compute the aspect ratio of the background image once so that
# overlays align perfectly with the picture.  We parse the PNG header
# directly to avoid depending on Pillow being available during tests.
def _img_ratio(path: str = "assets/shelf_map.png") -> float:
    """Return image height / width for the given PNG file."""
    try:
        with open(path, "rb") as f:
            f.seek(16)
            width = int.from_bytes(f.read(4), "big")
            height = int.from_bytes(f.read(4), "big")
        return height / width
    except Exception:
        return 1.0

PNG_RATIO = _img_ratio()

@st.cache_data(ttl=3600)
def load_locations():
    return handler.get_locations()        # all shelves

@st.cache_resource
def load_bg():
    return Image.open("assets/shelf_map.png")     # if you still want PNG

def _to_float(val):
    """Best-effort conversion of `val` to float without triggering Streamlit
    callbacks."""
    if val is None:
        return None
    try:
        return float(val)
    except Exception:
        pass
    if callable(val):
        try:
            return float(val())
        except Exception:
            pass
    return None


def inside(px: float, py: float, row: dict) -> bool:
    """Point-in-rotated-rectangle test in 0-1 data space."""
    w = float(row["w_pct"])
    h = float(row["h_pct"])
    cx = float(row["x_pct"]) + w / 2
    cy = 1 - (float(row["y_pct"]) + h / 2)  # flip Y once

    px = _to_float(px)
    py = _to_float(py)
    if px is None or py is None:
        return False
    dx, dy = px - cx, py - cy

    deg = float(row.get("rotation_deg") or 0.0)
    if deg:
        rad = -math.radians(deg)
        cos, sin = math.cos(rad), math.sin(rad)
        dx, dy = dx * cos - dy * sin, dx * sin + dy * cos

    return abs(dx) <= w/2 and abs(dy) <= h/2

# ────────────────────────────────────────────────────────── main page
def map_tab():
    """Interactive shelf map with click and search capabilities."""
    t0 = time.time()

    show_png = st.checkbox("Show floor-plan image", value=False)

    locs  = load_locations()
    img   = load_bg() if show_png else None

    col_shelf, col_name, col_barcode = st.columns(3)
    with col_shelf:
        dropdown = shelf_selector(locs)

    item_loc, item_id, searched = item_locator(handler, col_name, col_barcode)

    highlight = st.session_state.get("shelfmap_highlight")
    if isinstance(highlight, str):
        highlight = [highlight]

    not_found = False

    if item_loc:
        new = item_loc if isinstance(item_loc, list) else [item_loc]
        if new != highlight:
            highlight = new
            st.session_state["shelfmap_highlight"] = highlight
    elif searched:
        highlight = None
        st.session_state.pop("shelfmap_highlight", None)
        not_found = True
    elif dropdown and highlight != [dropdown]:
        highlight = [dropdown]
        st.session_state["shelfmap_highlight"] = highlight

    title_hi = ", ".join(highlight) if isinstance(highlight, list) else highlight
    msg = "This item is not available in shelves" if not_found else f"Highlight: {title_hi}"
    ping(msg, t0)

    # ───── draw rectangles & halo ───────────────────────────────────
    shapes = []
    for row in locs:
        x = float(row["x_pct"])
        y = float(row["y_pct"])
        w = float(row["w_pct"])
        h = float(row["h_pct"])
        deg = float(row.get("rotation_deg") or 0.0)

        cx = x + w/2
        cy = 1 - (y + h/2)

        y = 1 - y - h                       # flip Y once for drawing
        is_hi = highlight and row["locid"] in highlight
        fill  = "rgba(26,188,156,0.15)" if not is_hi else "rgba(255,128,0,0.25)"
        line  = dict(width=2 if is_hi else 1,
                     color="#FF8000" if is_hi else "#1ABC9C")

        if deg == 0:
            shapes.append(dict(type="rect", x0=x, y0=y, x1=x+w, y1=y+h,
                               line=line, fillcolor=fill))
        else:
            rad = math.radians(deg)
            cos, sin = math.cos(rad), math.sin(rad)
            pts = [(-w/2, -h/2), (w/2, -h/2), (w/2, h/2), (-w/2, h/2)]
            path = "M " + " L ".join(
                f"{cx+u*cos-v*sin},{cy+u*sin+v*cos}" for u, v in pts) + " Z"
            shapes.append(dict(type="path", path=path,
                               line=line, fillcolor=fill))

        if is_hi:                          # orange halo
            r = max(w, h) * 0.65
            shapes.append(dict(type="circle", xref="x", yref="y",
                               x0=cx - r, x1=cx + r, y0=cy - r, y1=cy + r,
                               line=dict(color="#FF8000", width=2, dash="dot")))

    # ───── Plotly figure ────────────────────────────────────────────
    fig = go.Figure()
    if img is not None:
        fig.add_layout_image(dict(
            source=img, xref="x", yref="y",
            x=0, y=1, sizex=1, sizey=1,
            xanchor="left", yanchor="top", layer="below"))

    fig.update_layout(shapes=shapes, height=700,
                      margin=dict(l=0,r=0,t=0,b=0))
    fig.update_xaxes(visible=False, range=[0,1], constrain="domain")
    # Maintain the original aspect ratio of the floor-plan image so that
    # drawn rectangles match the picture regardless of zoom level.
    fig.update_yaxes(visible=False, range=[0,1],
                     scaleanchor="x", scaleratio=PNG_RATIO)

    # cover-trace to capture click positions reliably. `st.plotly_chart`
    # reports the coordinates of the clicked data point, so a single
    # invisible marker at `(0, 0)` would cause every click to report
    # `(0, 0)`.  Instead we add a grid of invisible points covering the
    # whole [0, 1] range so that a nearby point is always selected and the
    # event returns the proper coordinates.
    step      = 0.01
    grid      = [i * step for i in range(int(1 / step) + 1)]
    cover_x   = []
    cover_y   = []
    for x in grid:
        cover_x.extend([x] * len(grid))
        cover_y.extend(grid)
    fig.add_trace(go.Scatter(
        x=cover_x, y=cover_y, mode="markers",
        marker=dict(size=1, opacity=0),
        hoverinfo="none", showlegend=False))

    # Pass the figure as a positional argument so that both the old
    # ``figure_or_data`` and the newer ``fig`` parameter names are
    # supported.  Additional parameters are provided via ``kwargs`` to
    # remain compatible with multiple Streamlit versions.
    kwargs = dict(key="shelfmap", height=700)
    sig = inspect.signature(st.plotly_chart)
    if "on_click" in sig.parameters:
        kwargs["on_click"] = "rerun"
    if "on_select" in sig.parameters:
        kwargs["on_select"] = "rerun"
        kwargs["selection_mode"] = "points"

    event_dict = st.plotly_chart(fig, **kwargs)

    # ───── click handling ──────────────────────────────────────────
    pt = None
    if event_dict:
        points = None
        if hasattr(event_dict, "points"):
            points = event_dict.points
        elif isinstance(event_dict, dict):
            points = event_dict.get("points")
        if not points:
            selection = None
            if hasattr(event_dict, "selection"):
                selection = event_dict.selection
                if hasattr(selection, "points"):
                    points = selection.points
            elif isinstance(event_dict, dict):
                selection = event_dict.get("selection")
                if isinstance(selection, dict):
                    points = selection.get("points")
        if callable(points):
            try:
                points = points()
            except Exception:
                points = None
        if points:
            if not isinstance(points, list):
                points = [points]
            pt = points[0]
        else:
            x = getattr(event_dict, "x", None)
            y = getattr(event_dict, "y", None)
            if isinstance(event_dict, dict):
                x = event_dict.get("x", x)
                y = event_dict.get("y", y)
            if x is not None and y is not None:
                pt = {"x": x, "y": y}

    if pt is not None:
        px = pt["x"] if isinstance(pt, dict) else getattr(pt, "x", None)
        py = pt["y"] if isinstance(pt, dict) else getattr(pt, "y", None)
        if px is None or py is None:
            pass
        else:
            hit = next((r for r in locs if inside(px, py, r)), None)
            if hit:
                locid = hit["locid"]
                current = st.session_state.get("shelfmap_highlight")
                if isinstance(current, str):
                    current = [current]
                if current != [locid]:
                    st.session_state["shelfmap_highlight"] = [locid]
                    # Clear item search inputs so that clicking on the map does
                    # not keep re-triggering the previous item lookup.
                    st.session_state.pop("item_name_selector", None)
                    st.session_state.pop("item_barcode_input", None)
                    st.rerun()
        # click in aisle ⇒ ignore

    # ───── stock panel ─────────────────────────────────────────────
    if highlight:
        title = ", ".join(highlight) if isinstance(highlight, list) else str(highlight)
        st.subheader(f"📍 {title}")
        if isinstance(highlight, list) and len(highlight) == 1:
            shelf = highlight[0]
            stock = handler.get_stock_by_location(shelf)
            st.dataframe(
                stock if not stock.empty else {"info": ["No items on this shelf"]},
                use_container_width=True)

    # ───── item availability table ───────────────────────────────────
    if item_id:
        item_stock = handler.get_stock_for_item(item_id)
        st.markdown("---")
        st.subheader("📝 Item Availability")
        st.dataframe(
            item_stock if not item_stock.empty else {"info": ["Item not on shelf"]},
            use_container_width=True,
        )
