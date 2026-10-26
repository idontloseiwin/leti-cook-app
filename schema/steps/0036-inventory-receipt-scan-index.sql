-- Inventory scan cleanup deletes scans together with their receipts. The FK
-- cascade and the cleanup probe find receipts by scan_id alone, which the
-- scan_transition partial index cannot serve, so every deleted scan read the
-- whole receipts table. scan_id = $1 implies scan_id IS NOT NULL.
CREATE INDEX typed_inventory_receipts_scan_id_idx ON public.typed_inventory_receipts (scan_id)
    WHERE scan_id IS NOT NULL;
