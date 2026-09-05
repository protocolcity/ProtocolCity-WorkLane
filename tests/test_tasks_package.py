"""Lock the api/tasks.py → worklane.api.tasks package peel."""
from __future__ import annotations

import unittest

from worklane.api import tasks as tasks_pkg
from worklane.api.tasks import crud, helpers, ops, products
from worklane.api.tasks._router import router as shared_router


class TasksPackagePeelTest(unittest.TestCase):
    def test_stable_import_surface(self) -> None:
        """Callers keep importing router and helpers from worklane.api.tasks."""
        self.assertIs(tasks_pkg.router, shared_router)
        self.assertIs(
            tasks_pkg._workforce_workers_for_product,
            helpers._workforce_workers_for_product,
        )
        self.assertIs(
            tasks_pkg._invalidate_attention_cache,
            ops._invalidate_attention_cache,
        )
        self.assertEqual(tasks_pkg.DEFAULT_AGENT_ID, "founder")

    def test_routes_still_register_on_shared_router(self) -> None:
        paths = {getattr(r, "path", None) for r in tasks_pkg.router.routes}
        for path in (
            "/api/admin/products",
            "/api/admin/tasks",
            "/api/admin/tasks/ready",
            "/api/admin/tasks/{task_id}",
            "/api/admin/tasks/{task_id}/comments",
            "/api/dev/attention",
            "/api/dev/queue/ready",
            "/api/admin/identity",
            "/api/ops/tickets-health",
        ):
            self.assertIn(path, paths)

    def test_boundaries(self) -> None:
        self.assertTrue(hasattr(products, "api_list_products"))
        self.assertTrue(hasattr(crud, "api_create_task"))
        self.assertTrue(hasattr(ops, "api_dev_attention"))
        self.assertFalse(hasattr(helpers, "router"))


if __name__ == "__main__":
    unittest.main()
