# streamlit_app_location_then_multi_declare.py (clean version)
import time
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

# ==================== PAGE CONFIG ====================
st.set_page_config(layout="centered")
st.title("📍 Confirm Location → 📦 Declare Multiple Items")

st.markdown("""
<style>
.step-title {font-size:1.2rem;margin:0.35rem 0 0.2rem 0;}
.catline {margin:0.05em 0;font-size:0.98em;}
.cat-class {color:#C61C1C;font-weight:bold;}
.cat-dept {color:#004CBB;font-weight:bold;}
.cat-sect {color:#098A23;font-weight:bold;}
.cat-family {color:#FF8800;font-weight:bold;}
.cat-val {color:#111;}
.okchip{display:inline-block;background:#E6F4EA;color:#0F9D58;border:1px solid #9CD1B5;
        padding:.15em .55em;border-radius:.6em;font-weight:600;margin-left:.35em;}
.badchip{display:inline-block;background:#FDEDED;color:#D93025;border:1px solid #F3B1AB;
        padding:.15em .55em;border-radius:.6em;font-weight:600;margin-left:.35em;}
.scan-hint {font-size:1.05em;color:#087911;font-weight:600;background:#eafdff;padding:.2em .7em;border-radius:.45em;margin:.4em 0 .8em 0;text-align:center;}
.small-dim {color:#666;font-size:.9em;margin-top:.25rem;}
.tbl-note {color:#444;margin:0.35rem 0 0.3rem 0;}
.locked-loc {background:#f4f9ff;border:1.5px solid #9dc3f5;border-radius:.6em;padding:.55em .8em;margin:.45em 0;}
</style>
""", unsafe_allow_html=True)

# ==================== HELPERS ====================
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
    pts.append(pts[0])  # close polygon
    return pts

def build_deck(shelf_locs, highlight_locs, selected_locid=""):
    """
    - Base PolygonLayer 'shelves'
    - Optional selected overlay (blue outline)
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

    # Selected overlay
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
        longitude=0.5, latitude=0.5,
        zoom=6, min_zoom=4, max_zoom=20, pitch=0, bearing=0
    )

    deck = pdk.Deck(
        layers=layers,
        initial_view_state=view_state,
        map_provider=None,  # normalized canvas 0..1
        tooltip={"html": "<b>{label_text}</b>", "style": {"fontSize": "14px", "font-family": "monospace"}},
        height=550,
    )
    return deck

# ==================== DATA ACCESS ====================
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

    def get_inventory_total(self, itemid):
        df = self.fetch_data("""
            SELECT SUM(quantity) as total
            FROM inventory
            WHERE itemid=%s AND quantity > 0
        """, (int(itemid),))
        return int(df.iloc[0]['total']) if not df.empty and df.iloc[0]['total'] is not None else 0

    def get_item_locations(self, itemid):
        df = self.fetch_data("""
            SELECT DISTINCT locid
            FROM shelfentries
            WHERE itemid=%s AND locid IS NOT NULL AND locid <> ''
            ORDER BY locid
        """, (int(itemid),))
        return df["locid"].tolist() if not df.empty else []

    def bulk_insert_declarations(self, rows):
        """
        rows: list of dicts with keys: itemid, locid, qty
        """
        if not rows:
            return
        values = [(int(r["itemid"]), int(r["qty"]), str(r["locid"])) for r in rows]
        self.execute_many("""
            INSERT INTO shelfentries
                (itemid, quantity, locid, trx_type, note, reference_id, reference_type)
            VALUES
                (%s, %s, %s, 'STOCKTAKE', 'declare', NULL, NULL)
        """, values)

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

# ==================== STATE ====================
st.session_state.setdefault("picked_locid", "")
st.session_state.setdefault("loc_confirmed", False)
st.session_state.setdefault("staged_items", [])  # list of dicts: {itemid, name, barcode, qty}
st.session_state.setdefault("last_add_signature", ("", 0.0))  # (signature, ts)

handler = DeclareHandler()
map_handler = ShelfMapHandler()

# ==================== STEP 1: SELECT & CONFIRM LOCATION ====================
st.markdown("<div class='step-title'>STEP 1 — Choose a shelf location and confirm</div>", unsafe_allow_html=True)

shelf_locs = map_handler.get_locations()
deck = build_deck(shelf_locs, highlight_locs=None, selected_locid=st.session_state["picked_locid"])

event = st.pydeck_chart(
    deck,
    use_container_width=True,
    on_select="rerun",
    selection_mode="single-object",
    key="loc_select_map",
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

c1, c2 = st.columns([2, 1])
typed_locid = c1.text_input(
    "Or type a locid manually (optional):",
    value=st.session_state["picked_locid"],
    key="manual_locid_entry",
    placeholder="e.g., A1-03-002"
).strip()

confirm_loc = c2.button("✅ Confirm Location", type="primary", use_container_width=True)

if confirm_loc:
    final_locid = typed_locid or st.session_state["picked_locid"]
    if not final_locid:
        st.error("Please click a shelf on the map or type a locid before confirming.")
    else:
        st.session_state["picked_locid"] = final_locid
        st.session_state["loc_confirmed"] = True
        st.success(f"Location confirmed: **{final_locid}**")

if st.session_state["loc_confirmed"]:
    st.markdown(
        f"<div class='locked-loc'>Confirmed location: <b>{st.session_state['picked_locid']}</b>"
        f" <span class='okchip'>Locked</span></div>", unsafe_allow_html=True
    )
    if st.button("🔓 Change location"):
        st.session_state["loc_confirmed"] = False
        st.session_state["staged_items"] = []  # clear staged when changing location
        st.info("Location unlocked. Pick or type a new location, then confirm.")
else:
    st.info("Choose a location on the map or type one, then press **Confirm Location**.")
    st.stop()  # halt until location is confirmed

# ==================== STEP 2: STAGE MULTIPLE ITEMS ====================
st.markdown("<div class='step-title'>STEP 2 — Add items (you can add many) for the confirmed location</div>", unsafe_allow_html=True)
locid = st.session_state["picked_locid"]

with st.expander("➕ Add item by barcode", expanded=True):
    # We use a FORM to prevent half-state during reruns
    with st.form(key="add_item_form", clear_on_submit=False):
        left, mid, right = st.columns([2.2, 1, 1])

        # Optional QR scanner only PREFILLS the barcode field
        prefill = ""
        if QR_AVAILABLE:
            st.markdown("<div class='scan-hint'>Scan with webcam/phone, then press **Add to list**.</div>", unsafe_allow_html=True)
            scanned = qrcode_scanner(key="barcode_cam_multi") or ""
            if scanned:
                prefill = str(scanned).strip()

        # Bind a dedicated state key; do NOT auto-fill from old adds
        if "barcode_input_multi" not in st.session_state:
            st.session_state["barcode_input_multi"] = ""

        # If scanner read something this run, prefill once
        if prefill:
            st.session_state["barcode_input_multi"] = prefill

        barcode_input = left.text_input(
            "Barcode",
            key="barcode_input_multi",
            max_chars=32,
            placeholder="Scan or type barcode"
        ).strip()

        qty_input = mid.number_input(
            "Quantity",
            min_value=1, value=1, step=1, key="qty_input_field"
        )

        submitted = right.form_submit_button("Add to list", use_container_width=True)

    if submitted:
        if not barcode_input:
            st.warning("Please provide a barcode.")
        else:
            item = handler.get_item_by_barcode(barcode_input)
            if item is None:
                st.error("Barcode not found in the item table.")
            else:
                # De-dupe very recent identical add (handles fast reruns/double clicks)
                signature = f"{locid}|{barcode_input}|{qty_input}"
                last_sig, last_ts = st.session_state["last_add_signature"]
                now = time.time()
                if signature == last_sig and (now - last_ts) < 2.0:
                    st.info("That exact add was just processed.")
                else:
                    itemid = int(item["itemid"])
                    merged = False
                    for row in st.session_state["staged_items"]:
                        if row["itemid"] == itemid:
                            row["qty"] += int(qty_input)
                            merged = True
                            break
                    if not merged:
                        st.session_state["staged_items"].append({
                            "itemid": itemid,
                            "name": item["name"],
                            "barcode": str(item["barcode"]),
                            "qty": int(qty_input),
                        })
                    st.success(f"Added: {item['name']} × {int(qty_input)}")

                    # Remember signature moment to prevent immediate duplicate on rerun
                    st.session_state["last_add_signature"] = (signature, now)

                # Clear the input fields for the next scan/type
                st.session_state["barcode_input_multi"] = ""
                st.session_state["qty_input_field"] = 1

# Staged items table with inline quantity editing & remove
if st.session_state["staged_items"]:
    st.markdown("<div class='tbl-note'>Review your staged items. You can adjust quantities or remove rows.</div>", unsafe_allow_html=True)
    new_rows = []
    for idx, row in enumerate(st.session_state["staged_items"]):
        c1, c2, c3, c4, c5 = st.columns([3, 2, 1.2, 1.2, 1])
        with c1:
            st.markdown(f"**{row['name']}**  \n`{row['barcode']}`")
        with c2:
            inv = handler.get_inventory_total(row["itemid"])
            st.caption(f"Inventory (read-only): **{inv}**")
        with c3:
            new_qty = st.number_input(
                f"Qty #{idx+1}",
                min_value=1, value=int(row["qty"]), step=1, key=f"qty_edit_{idx}"
            )
        with c4:
            locs = handler.get_item_locations(row["itemid"])
            st.caption(f"Seen at: {', '.join(map(str, locs[:3]))}{'…' if len(locs)>3 else ''}" if locs else "Seen at: —")
        with c5:
            remove = st.button("🗑️", key=f"rm_{idx}")
            if remove:
                continue  # drop this row
        new_rows.append({**row, "qty": int(new_qty)})

    st.session_state["staged_items"] = new_rows

    # Final actions row
    cL, cR = st.columns([1, 2])
    clear_all = cL.button("Clear list")
    commit = cR.button("✅ Confirm ALL declarations to this location", type="primary")

    if clear_all:
        st.session_state["staged_items"] = []
        st.info("Staged list cleared.")

    if commit:
        rows = [
            {"itemid": r["itemid"], "locid": locid, "qty": int(r["qty"])}
            for r in st.session_state["staged_items"]
            if int(r["qty"]) > 0
        ]
        if not rows:
            st.error("Nothing to commit. Please add items with positive quantities.")
        else:
            try:
                handler.bulk_insert_declarations(rows)
                st.success(f"Recorded {len(rows)} declarations to location **{locid}**.")
                st.session_state["staged_items"] = []
            except Exception:
                st.error("Could not save declarations.")
else:
    st.info("No items staged yet. Add items above by scanning or typing a barcode.")

# ==================== RECENT DECLARATIONS AT LOCATION ====================
st.markdown("<hr/>", unsafe_allow_html=True)
st.markdown("#### 🕒 Recent declarations at this location")
recents = handler.get_recent_declarations_at_location(locid, limit=200)
if recents.empty:
    st.caption("No declarations recorded yet for this shelf location.")
else:
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
