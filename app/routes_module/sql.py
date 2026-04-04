from __future__ import annotations

FIND_TASK_ROUTE_CONTEXT_SQL = """
SELECT
    t.id::text,
    t.customer_id::text,
    COALESCE(c.slug, 'help') AS category_slug,
    t.title,
    t.extra,
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
        a.id,
        a.title,
        a.category,
        a.status,
        a.data,
        COALESCE(
            a.location_point,
            CASE
                WHEN jsonb_typeof(a.data -> 'point') = 'object'
                     AND COALESCE(a.data -> 'point' ->> 'lat', '') ~ '^-?[0-9]+(\\.[0-9]+)?$'
                     AND COALESCE(a.data -> 'point' ->> 'lon', '') ~ '^-?[0-9]+(\\.[0-9]+)?$'
                THEN ST_SetSRID(
                    ST_MakePoint(
                        (a.data -> 'point' ->> 'lon')::double precision,
                        (a.data -> 'point' ->> 'lat')::double precision
                    ),
                    4326
                )::geography
                ELSE NULL
            END
        ) AS point_geog
FROM announcements a
WHERE a.deleted_at IS NULL
  AND a.status = 'active'
  AND a.id::text <> %s
  AND a.user_id::text <> %s
  AND NOT EXISTS (
      SELECT 1
      FROM announcement_offers ao
      WHERE ao.announcement_id::text = a.id::text
        AND ao.status = 'accepted'
        AND ao.deleted_at IS NULL
  )
)
SELECT
    c.id,
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
