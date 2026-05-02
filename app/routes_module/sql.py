from __future__ import annotations

FIND_TASK_ROUTE_CONTEXT_SQL = """
SELECT
    t.id::text,
    t.customer_id::text,
    COALESCE(c.slug, 'help') AS category_slug,
    t.title,
    t.extra,
    t.address_text,
    CASE WHEN t.location_point IS NULL THEN NULL ELSE ST_Y(t.location_point::geometry) END AS location_lat,
    CASE WHEN t.location_point IS NULL THEN NULL ELSE ST_X(t.location_point::geometry) END AS location_lon,
    ta.performer_id::text,
    ta.assignment_status::text,
    ta.execution_stage::text,
    COALESCE(ta.route_visibility::text, 'performer_only')
FROM tasks t
LEFT JOIN categories c
  ON c.id = t.category_id
LEFT JOIN LATERAL (
    SELECT performer_id, assignment_status, execution_stage, route_visibility
    FROM task_assignments
    WHERE task_id::text = t.id::text
      AND assignment_status IN ('assigned', 'in_progress')
    ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST
    LIMIT 1
) ta ON TRUE
WHERE t.id::text = %s
  AND t.deleted_at IS NULL
LIMIT 1
"""

FIND_TASK_ROUTE_POINTS_SQL = """
SELECT
    trp.point_order,
    trp.address_text,
    trp.point_kind,
    ST_Y(trp.point::geometry) AS latitude,
    ST_X(trp.point::geometry) AS longitude
FROM task_route_points trp
WHERE trp.task_id::text = %s
ORDER BY trp.point_order ASC, trp.created_at ASC
"""

FIND_KNOWN_ROUTE_POINT_BY_ADDRESS_SQL = """
WITH target AS (
    SELECT lower(regexp_replace(trim(%s), '[[:space:]]+', ' ', 'g')) AS address
),
candidates AS (
    SELECT
        trp.address_text AS address,
        ST_Y(trp.point::geometry) AS latitude,
        ST_X(trp.point::geometry) AS longitude,
        trp.created_at AS created_at
    FROM task_route_points trp
    WHERE trp.address_text IS NOT NULL

    UNION ALL

    SELECT
        t.extra->>'pickup_address' AS address,
        (t.extra->'pickup_point'->>'lat')::double precision AS latitude,
        (t.extra->'pickup_point'->>'lon')::double precision AS longitude,
        t.updated_at AS created_at
    FROM tasks t
    WHERE jsonb_typeof(t.extra->'pickup_point') = 'object'
      AND COALESCE(t.extra->'pickup_point'->>'lat', '') ~ '^-?[0-9]+(\\.[0-9]+)?$'
      AND COALESCE(t.extra->'pickup_point'->>'lon', '') ~ '^-?[0-9]+(\\.[0-9]+)?$'

    UNION ALL

    SELECT
        t.extra->>'dropoff_address' AS address,
        (t.extra->'dropoff_point'->>'lat')::double precision AS latitude,
        (t.extra->'dropoff_point'->>'lon')::double precision AS longitude,
        t.updated_at AS created_at
    FROM tasks t
    WHERE jsonb_typeof(t.extra->'dropoff_point') = 'object'
      AND COALESCE(t.extra->'dropoff_point'->>'lat', '') ~ '^-?[0-9]+(\\.[0-9]+)?$'
      AND COALESCE(t.extra->'dropoff_point'->>'lon', '') ~ '^-?[0-9]+(\\.[0-9]+)?$'

    UNION ALL

    SELECT
        t.extra#>>'{task,route,source,address}' AS address,
        (t.extra#>>'{task,route,source,point,lat}')::double precision AS latitude,
        (t.extra#>>'{task,route,source,point,lon}')::double precision AS longitude,
        t.updated_at AS created_at
    FROM tasks t
    WHERE jsonb_typeof(t.extra#>'{task,route,source,point}') = 'object'
      AND COALESCE(t.extra#>>'{task,route,source,point,lat}', '') ~ '^-?[0-9]+(\\.[0-9]+)?$'
      AND COALESCE(t.extra#>>'{task,route,source,point,lon}', '') ~ '^-?[0-9]+(\\.[0-9]+)?$'

    UNION ALL

    SELECT
        t.extra#>>'{task,route,destination,address}' AS address,
        (t.extra#>>'{task,route,destination,point,lat}')::double precision AS latitude,
        (t.extra#>>'{task,route,destination,point,lon}')::double precision AS longitude,
        t.updated_at AS created_at
    FROM tasks t
    WHERE jsonb_typeof(t.extra#>'{task,route,destination,point}') = 'object'
      AND COALESCE(t.extra#>>'{task,route,destination,point,lat}', '') ~ '^-?[0-9]+(\\.[0-9]+)?$'
      AND COALESCE(t.extra#>>'{task,route,destination,point,lon}', '') ~ '^-?[0-9]+(\\.[0-9]+)?$'
)
SELECT latitude, longitude
FROM candidates, target
WHERE candidates.address IS NOT NULL
  AND lower(regexp_replace(trim(candidates.address), '[[:space:]]+', ' ', 'g')) = target.address
  AND latitude BETWEEN -90 AND 90
  AND longitude BETWEEN -180 AND 180
ORDER BY created_at DESC NULLS LAST
LIMIT 1
"""

FIND_CURRENT_ROUTE_TASK_SQL = """
SELECT ta.task_id::text
FROM task_assignments ta
JOIN tasks t
  ON t.id::text = ta.task_id::text
WHERE ta.performer_id::text = %s
  AND ta.assignment_status IN ('assigned', 'in_progress')
  AND COALESCE(ta.route_visibility::text, 'performer_only') <> 'hidden'
  AND t.deleted_at IS NULL
ORDER BY ta.updated_at DESC NULLS LAST, ta.created_at DESC NULLS LAST
LIMIT 1
"""

NEARBY_TASKS_BY_ROUTE_SQL = """
WITH route AS (
    SELECT ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)::geography AS geog
),
candidates AS (
    SELECT
        t.id,
        t.title,
        COALESCE(c.slug, COALESCE(t.extra->>'category', t.extra->>'main_group', 'help')) AS category,
        CASE
            WHEN t.status IN ('published', 'in_responses') THEN 'active'
            ELSE t.status::text
        END AS status,
        t.extra AS data,
        COALESCE(
            t.location_point,
            CASE
                WHEN jsonb_typeof(t.extra -> 'point') = 'object'
                     AND COALESCE(t.extra -> 'point' ->> 'lat', '') ~ '^-?[0-9]+(\\.[0-9]+)?$'
                     AND COALESCE(t.extra -> 'point' ->> 'lon', '') ~ '^-?[0-9]+(\\.[0-9]+)?$'
                THEN ST_SetSRID(
                    ST_MakePoint(
                        (t.extra -> 'point' ->> 'lon')::double precision,
                        (t.extra -> 'point' ->> 'lat')::double precision
                    ),
                    4326
                )::geography
                ELSE NULL
            END
        ) AS point_geog
FROM tasks t
LEFT JOIN categories c
  ON c.id = t.category_id
WHERE t.deleted_at IS NULL
  AND t.moderation_status = 'published'
  AND t.status IN ('published', 'in_responses')
  AND t.id::text <> %s
  AND t.customer_id::text <> %s
  AND NOT EXISTS (
      SELECT 1
      FROM task_assignments ta
      WHERE ta.task_id = t.id
        AND ta.assignment_status IN ('assigned', 'in_progress')
  )
)
SELECT
    c.id::text,
    c.title,
    c.category,
    c.status,
    c.data,
    ST_Y(c.point_geog::geometry) AS latitude,
    ST_X(c.point_geog::geometry) AS longitude,
    ST_Distance(c.point_geog, (SELECT geog FROM route)) AS distance_to_route_meters
FROM candidates c
WHERE c.point_geog IS NOT NULL
  AND ST_DWithin(c.point_geog, (SELECT geog FROM route), %s)
ORDER BY distance_to_route_meters ASC
LIMIT %s
"""
