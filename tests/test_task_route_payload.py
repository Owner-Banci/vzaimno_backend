from __future__ import annotations

import unittest

from app.task_compat import ensure_task_payload, route_points_from_payload


class TaskRoutePayloadTests(unittest.TestCase):
    def test_route_points_from_payload_reads_nested_route_points(self) -> None:
        data = {
            "task": {
                "route": {
                    "source": {
                        "address": "Москва, Маросейка 8",
                        "point": {"lat": 55.7563, "lon": 37.6358},
                    },
                    "destination": {
                        "address": "Москва, Проспект Мира 20",
                        "point": {"lat": 55.7796, "lon": 37.6328},
                    },
                },
            },
        }

        points = route_points_from_payload("task-1", data)

        self.assertEqual(len(points), 2)
        self.assertEqual(points[0]["address_text"], "Москва, Маросейка 8")
        self.assertEqual(points[0]["point"], (55.7563, 37.6358))
        self.assertEqual(points[1]["address_text"], "Москва, Проспект Мира 20")
        self.assertEqual(points[1]["point"], (55.7796, 37.6328))

    def test_ensure_task_payload_fills_empty_nested_route_from_legacy_points(self) -> None:
        data = {
            "pickup_address": "Москва, Маросейка 8",
            "dropoff_address": "Москва, Проспект Мира 20",
            "pickup_point": {"lat": 55.7563, "lon": 37.6358},
            "dropoff_point": {"lat": 55.7796, "lon": 37.6328},
            "task": {
                "route": {
                    "source": {"address": None, "point": None},
                    "destination": {"address": None, "point": None},
                },
            },
        }

        normalized = ensure_task_payload(data, title="Маршрут", announcement_status="active")
        route = normalized["task"]["route"]

        self.assertEqual(route["source"]["address"], "Москва, Маросейка 8")
        self.assertEqual(route["source"]["point"], {"lat": 55.7563, "lon": 37.6358})
        self.assertEqual(route["destination"]["address"], "Москва, Проспект Мира 20")
        self.assertEqual(route["destination"]["point"], {"lat": 55.7796, "lon": 37.6328})


if __name__ == "__main__":
    unittest.main()
