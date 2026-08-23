import importlib.util
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI


MODULE_PATH = Path(__file__).resolve().parents[1] / "routes.py"
SPEC = importlib.util.spec_from_file_location("visual_export_routes", MODULE_PATH)
routes = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(routes)


class RoutesTest(unittest.TestCase):
    def test_export_settings_schema(self):
        self.assertTrue(routes._is_valid_setting("width", 1920))
        self.assertTrue(routes._is_valid_setting("height", 1080))
        self.assertTrue(routes._is_valid_setting("fps", 60))
        self.assertTrue(routes._is_valid_setting("bitrate_mbps", 10))
        self.assertTrue(routes._is_valid_setting("include_chrome", False))
        self.assertFalse(routes._is_valid_setting("fps", 59))
        self.assertFalse(routes._is_valid_setting("bitrate_mbps", 1000))
        self.assertFalse(routes._is_valid_setting("unknown", True))

    def test_setup_registers_settings_capabilities_and_mux_routes(self):
        with tempfile.TemporaryDirectory() as directory:
            app = FastAPI()
            routes.setup(app, {"config_dir": Path(directory)})
            registered = {
                (route.path, method)
                for route in app.routes
                for method in (route.methods or set())
            }
        base = "/api/plugins/visual_export"
        self.assertIn((f"{base}/settings", "GET"), registered)
        self.assertIn((f"{base}/settings", "POST"), registered)
        self.assertIn((f"{base}/capabilities", "GET"), registered)
        self.assertIn((f"{base}/mux", "POST"), registered)


if __name__ == "__main__":
    unittest.main()
