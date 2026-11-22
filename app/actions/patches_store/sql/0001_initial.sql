-- Patch store, initial schema.
--
-- Lives on Gundi's existing PostgreSQL instance (decided 2026-11-22), in its
-- own schema so it can sit in a dedicated database or beside other tables.
-- Plain PostgreSQL: no PostGIS, because that instance does not have it.
-- Pixels and patches are addressed by integer grid row and column, so
-- neighbour and overlap lookups are btree range scans; polygons (AOIs, patch
-- hulls) are stored as GeoJSON and never queried spatially by the database.
--
-- Design: "GNW Event Lifecycle: Specification and Implementation Plan".

CREATE SCHEMA IF NOT EXISTS gnw_patches;
SET LOCAL search_path TO gnw_patches;

CREATE TABLE aoi (
    aoi_id         uuid PRIMARY KEY,
    connection_id  uuid NOT NULL UNIQUE,          -- the Gundi integration
    geojson        jsonb NOT NULL,                -- resolved from the share link
    geostore_id    text,
    resolved_at    timestamptz NOT NULL
);

CREATE TABLE watched_tile (
    dataset          text NOT NULL,
    tile_id          text NOT NULL,               -- GNW tile name, e.g. 10N_010E
    watermark_date   date,                        -- newest alert date processed
    drivers_version  text,                        -- last drivers version applied
    curated_at       timestamptz,
    PRIMARY KEY (dataset, tile_id)
);

-- Which tiles each AOI needs; a tile is curated while any AOI watches it.
CREATE TABLE aoi_tile (
    aoi_id   uuid NOT NULL REFERENCES aoi ON DELETE CASCADE,
    dataset  text NOT NULL,
    tile_id  text NOT NULL,
    PRIMARY KEY (aoi_id, dataset, tile_id),
    FOREIGN KEY (dataset, tile_id) REFERENCES watched_tile
);
CREATE INDEX aoi_tile_tile_ix ON aoi_tile (dataset, tile_id);

CREATE SEQUENCE patch_created_order_seq;

CREATE TABLE patch (
    patch_id         uuid PRIMARY KEY,            -- minted at creation, never derived
    dataset          text NOT NULL,
    tile_id          text NOT NULL,               -- tile of the seed pixel
    status           text NOT NULL
                     CHECK (status IN ('open', 'closed', 'merged', 'withdrawn')),
    merged_into      uuid REFERENCES patch,
    created_order    bigint NOT NULL UNIQUE
                     DEFAULT nextval('patch_created_order_seq'),  -- merge survivor = lowest
    created_at       timestamptz NOT NULL DEFAULT now(),
    first_alert      date NOT NULL,
    last_alert       date NOT NULL,
    seed_row         int NOT NULL,
    seed_col         int NOT NULL,
    min_row          int NOT NULL,                -- bounding box, in grid cells
    max_row          int NOT NULL,
    min_col          int NOT NULL,
    max_col          int NOT NULL,
    pixel_count      int NOT NULL CHECK (pixel_count >= 0),
    area_m2          double precision NOT NULL,   -- latitude-corrected
    max_confidence   smallint NOT NULL,
    hull             jsonb,                       -- GeoJSON Polygon
    driver           text,
    driver_share     real,
    driver_coverage  boolean NOT NULL DEFAULT false,  -- inside the drivers extent
    drivers_version  text,
    driver_source    text,                        -- 'gnw' now; 'olmoearth' later
    version          int NOT NULL DEFAULT 1,
    CHECK ((status = 'merged') = (merged_into IS NOT NULL)),
    CHECK (min_row <= max_row AND min_col <= max_col),
    CHECK (first_alert <= last_alert)
);
-- Open patches near a batch of new pixels: range scan on rows, filter on cols.
CREATE INDEX patch_open_bbox_ix ON patch (dataset, min_row, max_row)
    WHERE status = 'open';
CREATE INDEX patch_close_ix ON patch (dataset, last_alert) WHERE status = 'open';
CREATE INDEX patch_tile_ix ON patch (dataset, tile_id, status);

CREATE TABLE patch_pixel (                        -- compacted away after close
    dataset       text NOT NULL,
    cell_row      int NOT NULL,
    cell_col      int NOT NULL,
    alert_date    date NOT NULL,
    confidence    smallint NOT NULL,
    driver_class  smallint,
    patch_id      uuid NOT NULL REFERENCES patch,
    PRIMARY KEY (dataset, cell_row, cell_col, alert_date)
);
CREATE INDEX patch_pixel_patch_ix ON patch_pixel (patch_id);

-- Change feed. seq has no gaps and is visible in order: see append_patch_change.
CREATE TABLE patch_change_head (
    dataset   text PRIMARY KEY,
    last_seq  bigint NOT NULL DEFAULT 0
);

CREATE TABLE patch_change (
    dataset        text NOT NULL,
    seq            bigint NOT NULL,
    patch_id       uuid NOT NULL REFERENCES patch,
    patch_version  int NOT NULL,
    kinds          text[] NOT NULL,               -- created, grown, shrunk, confidence_changed,
                                                  -- driver_assigned, merged, withdrawn, closed
    payload        jsonb NOT NULL DEFAULT '{}',
    created_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (dataset, seq)
);
CREATE INDEX patch_change_patch_ix ON patch_change (patch_id);

-- Append one change and return its seq.
--
-- A sequence would leave gaps on rollback and can expose seq n+1 before n
-- commits, so a consumer reading "everything after my cursor" could skip n
-- for good. Here every writer takes the dataset's head row first: the row
-- lock is held until commit, so numbers are handed out one transaction at a
-- time, a rollback returns its number, and seq order is commit order.
-- Writers for one dataset therefore queue here; keep the transaction short.
CREATE FUNCTION append_patch_change(
    p_dataset text, p_patch_id uuid, p_patch_version int, p_kinds text[],
    p_payload jsonb DEFAULT '{}'
) RETURNS bigint
LANGUAGE plpgsql
SET search_path = gnw_patches
AS $$
DECLARE
    v_seq bigint;
BEGIN
    INSERT INTO patch_change_head AS h (dataset, last_seq)
    VALUES (p_dataset, 1)
    ON CONFLICT (dataset) DO UPDATE SET last_seq = h.last_seq + 1
    RETURNING last_seq INTO v_seq;

    INSERT INTO patch_change (dataset, seq, patch_id, patch_version, kinds, payload)
    VALUES (p_dataset, v_seq, p_patch_id, p_patch_version, p_kinds, p_payload);
    RETURN v_seq;
END;
$$;

CREATE TABLE aoi_patch (
    aoi_id          uuid NOT NULL REFERENCES aoi ON DELETE CASCADE,
    patch_id        uuid NOT NULL REFERENCES patch,
    in_aoi_area_m2  double precision NOT NULL,
    PRIMARY KEY (aoi_id, patch_id)
);
CREATE INDEX aoi_patch_patch_ix ON aoi_patch (patch_id);

CREATE TABLE delivery (                           -- what each connection has sent
    connection_id      uuid NOT NULL,
    patch_id           uuid NOT NULL REFERENCES patch,
    gundi_event_id     uuid NOT NULL,             -- returned by the create; key for updates
    delivered_version  int NOT NULL,
    er_status          text NOT NULL CHECK (er_status IN ('active', 'resolved')),
    delivered_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (connection_id, patch_id)
);

CREATE TABLE delivery_cursor (
    connection_id  uuid NOT NULL,
    dataset        text NOT NULL,
    last_seq       bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (connection_id, dataset)
);
