# streamlit_page_multi_declare_by_location.py
import streamlit as st
import pandas as pd
import numpy as np
import pydeck as pdk

from db_handler import DatabaseManager
from shelf_map.shelf_map_handler import ShelfMapHandler

# Optional: barcode scanning (QR)
try:
    from streamlit_qrcode_scanner import qrcode_scanner
    QR_AVAILABLE = True
except ImportError:
    QR_AVAILABLE = False

# ------------- PAGE CONFIG -------------
st.set_page_config(layout="wide")
st.title("🟢 Multi-Declare: Pick Location → Add Multiple Items")

# ------------- HELPERS (geometry + map) -------------
def to_float(x):
    try:
        return float(x)
    except Exception:
        return 0.0

def make_rectangle(x, y, w, h, deg):
    """Closed polygon ([lon, lat]) in normalized 0..1 space with rotation."""
    cx = x + w / 2.0
    cy = y + h / 2.0
    rad = np.deg2rad(float(deg or 0.0))
    c, s = np.cos(rad), np.sin(rad)
    corners = np.array([[-w/2, -h/2], [w/2, -h/2], [w/2, h/2], [-w/2, h/2]])
    rot = corners @ np.array([[c, -s], [s, c]])
    abs_pts = rot + [cx, cy]
    pts = abs_pts.tolist()
    pts.append(pts[0])  # close
    return pts

def build_deck(shelf_locs, highlight_locs, selected_locid=""):
    """
    - Base PolygonLayer 'shelves'
    - Optional selected overlay (blue outline) for the clicked shelf
    - Tooltip shows a single-line label
    """
    hi = set(map(str, highlight_locs or []))
    rows = []
    for row in shelf_locs:
        locid = str(row.get("locid"))
        x, y, w, h = map(to_float, (row["x_pct"], row["y_pct"], row["w_pct"], row["h_pct"]))
        deg = to_float(row.get("rotation_deg") or 0)
        coords = make_rectangle(x, y, w, h, deg)
        is_hi = locid in hi
        rows.append({
            "polygon": coords,
            "locid": locid,
            "label_text": str(row.get("label") or locid),
            "fill_color": [220, 53, 69, 190] if is_hi else [180, 180, 180, 70],
            "line_color": [216, 0, 12, 255] if is_hi else [120, 120, 120, 255],
        })
    df = pd.DataFrame(rows)

    base_layer = pdk.Layer(
        "PolygonLayer",
        id="shelves",
        data=df,
        get_polygon="polygon",
        get_fill_color="fill_color",
        get_line_color="line_color",
        pickable=True,
        auto_highlight=True,
        filled=True,
        stroked=True,
        get_line_width=2,
    )

    layers = [base_layer]

    # Optional selected overlay (blue outline + semi-transparent fill)
    if selected_locid:
        sel_df = df[df["locid"] == str(selected_locid)]
        if not sel_df.empty:
            sel_layer = pdk.Layer(
                "PolygonLayer",
                id="selected-outline",
                data=sel_df,
                get_polygon="polygon",
                get_fill_color=[30, 144, 255, 40],
                get_line_color=[16, 98, 234, 255],
                pickable=False,
                filled=True,
                stroked=True,
                get_line_width=3,
            )
            layers.append(sel_layer)

    view_state = pdk.ViewState(
        longitude=0.5, latitude=0.5, zoom=6, min_zoom=4, max_zoom=20, pitch=0, bearing=0
    )

    deck = pdk.Deck(
        layers=layers,
        initial_view_state=view_state,
        map_provider=None,  # normalized canvas 0..1
        tooltip={"html": "<b>{label_text}</b>", "style": {"fontSize": "14px", "font-family": "monospace"}},
        height=520,
    )
    return deck

# ------------- DATA ACCESS -------------
class DeclareHandler(DatabaseManager):
    def get_item_by_barcode(self, barcode):
        df = self.fetch_data(
            """
            SELECT itemid, itemnameenglish AS name, barcode,
                   familycat, sectioncat, departmentcat, classcat
            FROM item
            WHERE barcode = %s
            LIMIT 1
            """,
            (barcode,),
        )
        return df.iloc[0] if not df.empty else None

    def insert_declaration(self, itemid, locid, qty):
        # Append-only: insert a new row into shelfentries (NO expirationdate)
        self.execute_command(
            """
            INSERT INTO shelfentries
                (itemid, quantity, locid, trx_type, note, reference_id, reference_type)
            VALUES
                (%s, %s, %s, 'STOCKTAKE', 'declare', NULL, NULL)
            """,
            (int(itemid), int(qty), str(locid)),
        )

    def get_recent_declarations_at_location(self, locid, limit=200):
        df = self.fetch_data(
            """
            SELECT
                se.entryid,
                se.itemid,
                i.itemnameenglish AS name,
                i.barcode,
                se.quantity,
                se.entrydate
            FROM shelfentries se
            JOIN item i ON i.itemid = se.itemid
            WHERE se.locid = %s AND se.note = 'declare'
            ORDER BY se.entrydate DESC, se.entryid DESC
            LIMIT %s
            """,
            (str(locid), int(limit)),
        )
        return df if not df.empty else pd.DataFrame(columns=["entryid", "itemid", "name", "barcode", "quantity", "entrydate"])

# ------------- STATE -------------
st.session_state.setdefault("picked_locid", "")            # from map click (pre-confirm)
st.session_state.setdefault("confirmed_locid", "")         # fixed after confirm
st.session_state.setdefault("items_cart", [])              # [{'itemid','name','barcode','qty'}]
st.session_state.setdefault("last_scanned_barcode", "")

handler = DeclareHandler()
map_handler = ShelfMapHandler()

# ------------- STEP 1: PICK + CONFIRM LOCATION -------------
st.subheader("1) Choose a Shelf Location")

# If not yet confirmed, allow picking from map + manual override
if not st.session_state["confirmed_locid"]:
    shelf_locs = map_handler.get_locations()
    deck = build_deck(shelf_locs, highlight_locs=[], selected_locid=st.session_state["picked_locid"])
    event = st.pydeck_chart(
        deck,
        use_container_width=True,
        on_select="rerun",
        selection_mode="single-object",
        key="pick_loc_map",
    )

    # Extract clicked object → update picked_locid
    try:
        sel = getattr(event, "selection", None) or (event.get("selection") if isinstance(event, dict) else None)
        if sel:
            objs = sel.get("objects", {}) if isinstance(sel, dict) else {}
            picked_list = objs.get("shelves") or []
            if picked_list:
                first = picked_list[0]
                data = first.get("object") if isinstance(first, dict) and "object" in first else first
                locid_clicked = str(data.get("locid") or "")
                if locid_clicked:
                    st.session_state["picked_locid"] = locid_clicked
    except Exception:
        pass  # fail gracefully

    c1, c2 = st.columns([3, 2])
    with c1:
        st.text_input(
            "Or type a locid (optional)",
            value=st.session_state["picked_locid"],
            key="typed_locid",
            help="If filled, this overrides the picked shelf.",
        )
    with c2:
        confirm = st.button("✅ Confirm Location", use_container_width=True)
        if confirm:
            final_loc = (st.session_state.get("typed_locid") or st.session_state["picked_locid"]).strip()
            if not final_loc:
                st.error("Please select a shelf on the map or type a locid before confirming.")
            else:
                st.session_state["confirmed_locid"] = final_loc
                st.success(f"Location confirmed: **{final_loc}**")
                st.rerun()
else:
    # Location already confirmed — show banner + controls
    with st.container():
        st.markdown(
            f"""<div style='background:#e7f8ff;border:1.5px solid #66b1ff;
                   border-radius:0.5em;padding:.7em 1em;margin:.4em 0 1em 0;'>
                   <b>Confirmed Location:</b> <span style='color:#0b63d1;'>{st.session_state["confirmed_locid"]}</span>
                 </div>""",
            unsafe_allow_html=True
        )
    c1, c2 = st.columns([1, 1])
    with c1:
        if st.button("🧹 New Declare (pick a new location)", use_container_width=True):
            # Reset everything to start over
            for k in ["picked_locid", "confirmed_locid", "items_cart", "last_scanned_barcode"]:
                st.session_state[k] = "" if isinstance(st.session_state[k], str) else []
            st.rerun()
    with c2:
        st.write("")  # spacer

# ------------- STEP 2: DECLARE MULTIPLE ITEMS -------------
st.subheader("2) Add Items for This Location")

if not st.session_state["confirmed_locid"]:
    st.info("Confirm a location above to start adding items.")
else:
    # Input: scan/type barcodes, set qty, add to cart
    tabs = st.tabs(["📷 Scan via camera", "⌨️ Type/paste barcode"])

    def add_item_to_cart(scanned_barcode: str, qty: int):
        if not scanned_barcode:
            st.warning("Please scan or enter a barcode.")
            return
        item = handler.get_item_by_barcode(scanned_barcode)
        if item is None:
            st.error("❌ Barcode not found in the item table.")
            return
        # If item already in cart, just bump its qty
        cart = st.session_state["items_cart"]
        for row in cart:
            if int(row["itemid"]) == int(item["itemid"]):
                row["qty"] = max(0, int(row["qty"])) + int(qty)
                st.success(f"Updated '{item['name']}' quantity to {row['qty']}.")
                return
        cart.append({
            "itemid": int(item["itemid"]),
            "name": str(item["name"]),
            "barcode": str(item["barcode"]),
            "qty": int(qty)
        })
        st.success(f"Added '{item['name']}' ({item['barcode']}) with qty {qty}.")

    with tabs[0]:
        if QR_AVAILABLE:
            st.caption("Aim the barcode at your phone or webcam for instant detection.")
            scanned = qrcode_scanner(key="barcode_multi_cam") or ""
            qty_cam = st.number_input("Qty", min_value=1, value=1, step=1, key="qty_cam")
            c1, c2 = st.columns([3, 1])
            with c1:
                st.text_input("Last scanned", value=scanned, key="last_scanned_barcode", disabled=True)
            with c2:
                if st.button("➕ Add", key="add_cam"):
                    add_item_to_cart(scanned, qty_cam)
        else:
            st.warning("Camera scanning not available. Install: pip install streamlit-qrcode-scanner")

    with tabs[1]:
        typed_bc = st.text_input("Enter barcode", key="typed_barcode")
        qty_typed = st.number_input("Qty", min_value=1, value=1, step=1, key="qty_typed")
        c1, c2 = st.columns([3, 1])
        with c2:
            if st.button("➕ Add", key="add_typed"):
                add_item_to_cart(typed_bc, qty_typed)

    # Cart table with per-row quantity edits + remove
    cart = st.session_state["items_cart"]
    if cart:
        st.markdown("#### Pending Declarations")
        # Render each row with editable qty & remove button
        to_remove = []
        for i, row in enumerate(cart):
            col1, col2, col3, col4, col5 = st.columns([3, 3, 2, 2, 2])
            with col1:
                st.write(f"**{row['name']}**")
            with col2:
                st.code(row["barcode"])
            with col3:
                new_qty = st.number_input(
                    "Qty",
                    min_value=0, value=int(row["qty"]), step=1,
                    key=f"qty_row_{i}", label_visibility="collapsed"
                )
                row["qty"] = int(new_qty)
            with col4:
                if st.button("🗑️ Remove", key=f"rem_{i}"):
                    to_remove.append(i)
            with col5:
                st.write(f"Item ID: {row['itemid']}")
            st.markdown("<hr style='margin:0.2rem 0 0.6rem 0;'>", unsafe_allow_html=True)

        # Apply removals (from end to start)
        for idx in sorted(to_remove, reverse=True):
            cart.pop(idx)

        # Save all rows
        save_col1, save_col2 = st.columns([2, 1])
        with save_col1:
            st.write(f"**Location:** {st.session_state['confirmed_locid']}")
        with save_col2:
            if st.button("✅ Save All Declarations", use_container_width=True):
                # Filter out zero-qty lines
                valid_rows = [r for r in cart if int(r["qty"]) > 0]
                if not valid_rows:
                    st.error("No positive quantities to save.")
                else:
                    for r in valid_rows:
                        handler.insert_declaration(
                            itemid=r["itemid"],
                            locid=st.session_state["confirmed_locid"],
                            qty=int(r["qty"]),
                        )
                    st.success(f"Saved {len(valid_rows)} declaration(s) to {st.session_state['confirmed_locid']}.")
                    # keep location so user can continue adding; clear cart
                    st.session_state["items_cart"] = []
                    st.rerun()
    else:
        st.info("No items added yet. Scan or enter a barcode above, set quantity, then click ➕ Add.")

    # Recent declarations for the confirmed location
    st.markdown("#### Recent Declarations at This Location")
    recents = handler.get_recent_declarations_at_location(st.session_state["confirmed_locid"])
    if not recents.empty:
        st.dataframe(
            recents.rename(columns={
                "entryid": "Entry ID",
                "itemid": "Item ID",
                "name": "Item Name",
                "barcode": "Barcode",
                "quantity": "Declared Qty",
                "entrydate": "Entry Date"
            }),
            hide_index=True,
            use_container_width=True
        )
    else:
        st.caption("No declarations recorded yet for this shelf location.")
