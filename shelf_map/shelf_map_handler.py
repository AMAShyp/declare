# shelf_map/shelf_map_handler.py
from db_handler import DatabaseManager


class ShelfMapHandler(DatabaseManager):
    """All DB reads for the Shelf-Map page."""

    # ── shelf geometry ─────────────────────────────────────────────
    def get_locations(self) -> list[dict]:
        sql = """
            SELECT locid,
                   label,
                   x_pct, y_pct,
                   w_pct, h_pct,
                   COALESCE(rotation_deg, 0) AS rotation_deg
            FROM   shelf_map_locations
            ORDER  BY locid;
        """
        return self.fetch_data(sql).to_dict("records")

    # ── live stock on one shelf ────────────────────────────────────
    def get_stock_by_location(self, locid):
        sql = """
          SELECT s.shelfid,
                 i.itemid,
                 i.itemnameenglish AS item,
                 s.quantity,
                 s.expirationdate
          FROM   shelf s
          JOIN   item  i USING (itemid)
          WHERE  s.locid = %s
          ORDER  BY i.itemnameenglish, s.expirationdate;
        """
        return self.fetch_data(sql, (locid,))

    def get_stock_by_locations(self, locids: list[str]):
        """Return stock entries for all given shelf ``locid`` values."""
        if not locids:
            return self.fetch_data("SELECT NULL WHERE FALSE")
        placeholders = ", ".join(["%s"] * len(locids))
        sql = f"""
            SELECT s.locid,
                   s.shelfid,
                   i.itemid,
                   i.itemnameenglish AS item,
                   s.quantity,
                   s.expirationdate
              FROM shelf s
              JOIN item i USING (itemid)
             WHERE s.locid IN ({placeholders})
             ORDER BY s.locid, i.itemnameenglish, s.expirationdate;
        """
        return self.fetch_data(sql, tuple(locids))

    # ── item lookups ─────────────────────────────────────────────────
    def get_items_on_shelf(self):
        sql = """
            SELECT DISTINCT i.itemid,
                            i.itemnameenglish AS itemname
            FROM   shelf s
            JOIN   item  i USING (itemid)
            ORDER  BY i.itemnameenglish;
        """
        return self.fetch_data(sql)

    def get_locations_by_itemid(self, itemid):
        sql = """
            SELECT DISTINCT locid
            FROM   shelf
            WHERE  itemid = %s;
        """
        return self.fetch_data(sql, (int(itemid),))

    def get_locations_by_barcode(self, barcode):
        sql = """
            SELECT DISTINCT s.locid
            FROM   shelf s
            JOIN   item i ON s.itemid = i.itemid
            WHERE  i.barcode = %s
               OR i.packetbarcode = %s
               OR i.cartonbarcode = %s;
        """
        return self.fetch_data(sql, (barcode, barcode, barcode))

    def get_itemid_by_barcode(self, barcode):
        sql = """
            SELECT itemid
              FROM item
             WHERE barcode = %s
                OR packetbarcode = %s
                OR cartonbarcode = %s
             LIMIT 1;
        """
        df = self.fetch_data(sql, (barcode, barcode, barcode))
        return int(df.loc[0, "itemid"]) if not df.empty else None

    # ── stock details for a specific item ────────────────────────────
    def get_stock_for_item(self, itemid):
        """Return quantity and expirations for the given item across shelves."""
        sql = """
            SELECT s.locid,
                   s.shelfid,
                   s.quantity,
                   s.expirationdate
              FROM shelf s
             WHERE s.itemid = %s
             ORDER BY s.locid, s.expirationdate;
        """
        return self.fetch_data(sql, (int(itemid),))

    # ── aggregated quantity per shelf ───────────────────────────────
    def get_heatmap_data(self, near_days: int | None = None) -> list[dict]:
        """
        Return shelf geometry with aggregated quantity.

        If `near_days` is given, only items that expire **within N days**
        are counted; otherwise we aggregate all quantities.
        """
        if near_days is None:
            sql = """
                SELECT l.locid, l.label,
                       l.x_pct, l.y_pct, l.w_pct, l.h_pct,
                       COALESCE(l.rotation_deg,0) AS rotation_deg,
                       COALESCE(SUM(s.quantity),0) AS quantity
                  FROM shelf_map_locations l
             LEFT JOIN shelf s USING (locid)
              GROUP BY l.locid, l.label, l.x_pct, l.y_pct,
                       l.w_pct, l.h_pct, l.rotation_deg
              ORDER BY l.locid;
            """
            return self.fetch_data(sql).to_dict("records")

        sql = """
            SELECT l.locid, l.label,
                   l.x_pct, l.y_pct, l.w_pct, l.h_pct,
                   COALESCE(l.rotation_deg,0) AS rotation_deg,
                   COALESCE(SUM(s.quantity),0) AS quantity
              FROM shelf_map_locations l
         LEFT JOIN shelf s USING (locid)
             WHERE s.expirationdate <= CURRENT_DATE + %s::interval
          GROUP BY l.locid, l.label, l.x_pct, l.y_pct,
                   l.w_pct, l.h_pct, l.rotation_deg
          ORDER BY l.locid;
        """
        return self.fetch_data(sql, (f"{near_days} days",)).to_dict("records")

    def get_heatmap_threshold(self) -> list[dict]:
        """
        Return shelf geometry with:
        • quantity  = sum of units on that shelf
        • threshold = sum of item-level shelfthreshold values
        """
        sql = """
          SELECT l.locid,
                 l.label,
                 l.x_pct, l.y_pct,
                 l.w_pct, l.h_pct,
                 COALESCE(l.rotation_deg,0)               AS rotation_deg,
                 COALESCE(SUM(s.quantity),0)              AS quantity,
                 COALESCE(SUM(it.shelfthreshold),0)       AS threshold
            FROM shelf_map_locations l
       LEFT JOIN shelf s   USING (locid)
       LEFT JOIN item  it  ON it.itemid = s.itemid
        GROUP BY l.locid, l.label, l.x_pct, l.y_pct,
                 l.w_pct, l.h_pct, l.rotation_deg
        ORDER BY l.locid;
        """
        return self.fetch_data(sql).to_dict("records")
