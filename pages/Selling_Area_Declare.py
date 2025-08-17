# streamlit_app_pydeck_declare_fullmap_click_to_select.py
import streamlit as st
import pandas as pd
import numpy as np
import pydeck as pdk

from db_handler import DatabaseManager
from shelf_map import ShelfMapHandler


# Optional: barcode scanning (QR)
try:
    from streamlit_qrcode_scanner import qrcode_scanner
    QR_AVAILABLE = True
except ImportError:
    QR_AVAILABLE = False

# ---------------- CONFIG ----------------
st.set_page_config(layout="centered")
st.title("🟢 Declare Selling Area Quantity (by Barcode) — Click shelf on map to select")

# ---------------- GEOMETRY HELPERS ----------------
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
    - Base PolygonLayer 'shelves' (grey or red for highlight)
    - Optional selected overlay (blue outline) for the clicked shelf
    - Tooltip shows a single-line label
    """
    hi = set(map(str, highlight_locs))
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
        id="shelves",                      # << important for selection state
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
                get_fill_color=[30, 144, 255, 40],   # light blue tint
                get_line_color=[16, 98, 234, 255],   # vivid blue outline
                pickable=False,
                filled=True,
                stroked=True,
                get_line_width=3,
            )
            layers.append(sel_layer)

    view_state = pdk.ViewState(longitude=0.5, latitude=0.5, zoom=6, min_zoom=4, max_zoom=20, pitch=0, bearing=0)

    deck = pdk.Deck(
        layers=layers,
        initial_view_state=view_state,
        map_provider=None,  # normalized canvas 0..1
        tooltip={"html": "<b>{label_text}</b>", "style": {"fontSize": "14px", "font-family": "monospace"}},
        height=550,
    )
    return deck

# ---------------- DATA ACCESS ----------------
class DeclareHandler(DatabaseManager):
    def get_item_by_barcode(self, barcode):
        df = self.fetch_data("""
            SELECT itemid, itemnameenglish AS name, barcode,
                   familycat, sectioncat, departmentcat, classcat
            FROM item
            WHERE barcode = %s
            LIMIT 1
        """, (barcode,))
        return df.iloc[0] if not df.empty else None

    # READ-ONLY inventory total (optional info)
    def get_inventory_total(self, itemid):
        df = self.fetch_data("""
            SELECT SUM(quantity) as total
            FROM inventory
            WHERE itemid=%s AND quantity > 0
        """, (int(itemid),))
        return int(df.iloc[0]['total']) if not df.empty and df.iloc[0]['total'] is not None else 0

    # Historical locids where this item ever had an entry (for map highlight/defaults)
    def get_item_locations(self, itemid):
        df = self.fetch_data("""
            SELECT DISTINCT locid
            FROM shelfentries
            WHERE itemid=%s AND locid IS NOT NULL AND locid <> ''
            ORDER BY locid
        """, (int(itemid),))
        return df["locid"].tolist() if not df.empty else []

    # Insert a new declaration row (append-only) — NO expirationdate
    def insert_declaration(self, itemid, locid, qty, who="Unknown"):
        # entrydate/created_at/createdby are handled by table defaults.
        # We omit expirationdate entirely per your new rule (must allow NULL).
        self.execute_command("""
            INSERT INTO shelfentries
                (itemid, quantity, locid, trx_type, note, reference_id, reference_type)
            VALUES
                (%s, %s, %s, 'STOCKTAKE', 'declare', NULL, NULL)
        """, (int(itemid), int(qty), str(locid)))

    # Recent declarations at a location (for the bottom panel)
    def get_recent_declarations_at_location(self, locid, limit=200):
        df = self.fetch_data("""
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
        """, (str(locid), int(limit)))
        return df if not df.empty else pd.DataFrame(columns=["entryid", "itemid", "name", "barcode", "quantity", "entrydate"])

# ---------------- STYLE ----------------
st.markdown("""
<style>
.catline {margin:0.08em 0 0.09em 0;font-size:1.1em;}
.cat-class {color:#C61C1C;font-weight:bold;}
.cat-dept {color:#004CBB;font-weight:bold;}
.cat-sect {color:#098A23;font-weight:bold;}
.cat-family {color:#FF8800;font-weight:bold;}
.cat-val {color:#111;}
.scan-hint {font-size:1.1em;color:#087911;font-weight:600;background:#eafdff;padding:.2em .7em;border-radius:.45em;margin:.4em 0 .6em 0;text-align:center;}
.small-dim {color:#666;font-size:.92em;margin-top:.25rem;}
</style>
""", unsafe_allow_html=True)

# ---------------- STATE ----------------
st.session_state.setdefault("latest_declaration", {})
st.session_state.setdefault("latest_itemid", None)
st.session_state.setdefault("picked_locid", "")  # updated by map clicks

handler = DeclareHandler()
map_handler = ShelfMapHandler()

tab1, tab2 = st.tabs(["📷 Scan via camera", "⌨️ Type/paste barcode"])

# ---------------- CORE FLOW ----------------
def declare_logic(barcode, reset_callback):
    item = None
    itemid = None
    if barcode:
        item = handler.get_item_by_barcode(barcode)
        if item is not None:
            itemid = int(item['itemid'])

    # Reset message when switching items
    if itemid and st.session_state["latest_itemid"] != itemid:
        st.session_state["latest_declaration"] = {}
        st.session_state["latest_itemid"] = itemid
        st.session_state["picked_locid"] = ""  # clear previous selection

    if not barcode:
        st.info("Please scan or enter the item barcode.")
        return

    if item is None and barcode.strip():
        st.error("❌ Barcode not found in the item table.")
        return

    # ---------- Item details ----------
    st.markdown(f"**Item:** {item['name']}<br>🔖 Barcode: {item['barcode']}", unsafe_allow_html=True)
    st.markdown(
        f"<div class='catline'><span class='cat-class'>Class:</span> <span class='cat-val'>{item['classcat']}</span></div>"
        f"<div class='catline'><span class='cat-dept'>Department:</span> <span class='cat-val'>{item['departmentcat']}</span></div>"
        f"<div class='catline'><span class='cat-sect'>Section:</span> <span class='cat-val'>{item['sectioncat']}</span></div>"
        f"<div class='catline'><span class='cat-family'>Family:</span> <span class='cat-val'>{item['familycat']}</span></div>",
        unsafe_allow_html=True
    )

    # ---------- Data pulls ----------
    item_locs_history = handler.get_item_locations(itemid)
    inventory_total = handler.get_inventory_total(itemid)  # read-only info
    shelf_locs = map_handler.get_locations()  # FULL MAP

    # ---------- MAP (click to select) ----------
    st.markdown("#### 🗺️ Click a shelf to select it")
    deck = build_deck(shelf_locs, item_locs_history, st.session_state["picked_locid"])
    event = st.pydeck_chart(
        deck,
        use_container_width=True,
        on_select="rerun",
        selection_mode="single-object",
        key="main_shelf_map",
    )

    # Extract clicked object → update picked_locid
    try:
        sel = getattr(event, "selection", None) or event.get("selection") if isinstance(event, dict) else None
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

    # ---------- Chosen shelf ----------
    chosen_locid = st.session_state["picked_locid"]
    if chosen_locid:
        st.success(f"Selected shelf: **{chosen_locid}**")
        st.caption("Click a different shelf to change.")
    else:
        st.info("Click a shelf on the map to select it. (You can still proceed without selection if you type a locid.)")

    # ---------- Quantity & actions ----------
    st.info(f"**Available in inventory (read-only):** {inventory_total}")

    new_qty = st.number_input(
        "Declare current selling area quantity",
        min_value=0, value=0, step=1, key="declare_qty", label_visibility="visible"
    )

    manual_locid = st.text_input(
        "Or type a locid (optional)",
        value=chosen_locid,
        key="declare_locid_text",
        label_visibility="collapsed",
        help="This overrides the clicked shelf if filled."
    ).strip()
    final_locid = manual_locid or chosen_locid

    c1, c2 = st.columns([2, 1])
    confirm_clicked = c1.button("✅ Confirm Declaration", key="btn_confirm_declaration")
    if c2.button("🔄 New Scan", key="btn_new_scan"):
        for k in ["barcode_cam", "barcode_input", "declare_qty", "declare_locid_text"]:
            st.session_state.pop(k, None)
        st.session_state["picked_locid"] = ""
        st.rerun()

    if confirm_clicked:
        if not final_locid:
            st.error("Please click a shelf on the map (or type a locid) before confirming.")
            return
        if new_qty <= 0:
            st.error("Quantity must be greater than zero to declare.")
            return

        # Append-only: insert a new row into shelfentries (NO expirationdate)
        handler.insert_declaration(itemid=itemid, locid=final_locid, qty=new_qty)

        st.success(f"Recorded declaration for '{item['name']}' at {final_locid} with quantity {new_qty}.")

        st.session_state["latest_declaration"] = {
            "itemid": itemid,
            "itemname": item['name'],
            "barcode": item['barcode'],
            "locid": final_locid,
            "qty": new_qty
        }
        st.rerun()

def show_latest_declaration_and_items():
    latest = st.session_state.get("latest_declaration")
    if latest and "itemid" in latest:
        st.markdown(
            f"""<div style='background:#e7f8e9;border:1.5px solid #47bd72;
                  border-radius:0.5em;padding:0.65em 1em 0.6em 1em;
                  margin-top:1em;font-size:1.09em;'>
                <b>Latest Declaration:</b><br>
                <b>Item:</b> <span style='color:#1c4680'>{latest["itemname"]}</span><br>
                <b>Barcode:</b> <span style='color:#222;font-family:monospace'>{latest["barcode"]}</span><br>
                <b>Location:</b> <span style='color:#098A23'>{latest["locid"]}</span><br>
                <b>Quantity:</b> <span style='color:#C61C1C'>{latest["qty"]}</span>
            </div>""",
            unsafe_allow_html=True
        )
        recents = DeclareHandler().get_recent_declarations_at_location(latest["locid"])
        if not recents.empty:
            st.markdown(
                f"<br/><b>Recent declarations at location <span style='color:#098A23'>{latest['locid']}</span>:</b>",
                unsafe_allow_html=True
            )
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
            st.info("No declarations recorded yet for this shelf location.")

# ---------------- TABS ----------------
with tab1:
    if QR_AVAILABLE:
        st.markdown(
            "<div class='scan-hint'>Aim the barcode at your phone or webcam for instant detection.<br>Hold steady and close to the lens.</div>",
            unsafe_allow_html=True
        )
        barcode = qrcode_scanner(key="barcode_cam") or ""
        if barcode:
            st.success(f"Scanned: {barcode}")
        declare_logic(barcode, reset_callback=lambda: None)
    else:
        st.warning("Camera scanning not available. Please use tab 2 or pip install streamlit-qrcode-scanner.")

with tab2:
    barcode = st.text_input("Scan or enter barcode", key="barcode_input", max_chars=32, label_visibility="visible")
    declare_logic(barcode, reset_callback=lambda: None)

show_latest_declaration_and_items()
