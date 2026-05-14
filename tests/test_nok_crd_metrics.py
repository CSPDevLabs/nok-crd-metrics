import importlib.util
from pathlib import Path
from unittest.mock import MagicMock,patch,call, mock_open
import pytest
from kubernetes.client.exceptions import ApiException
from http.server import BaseHTTPRequestHandler

MODULE_PATH = Path(__file__).resolve().parent.parent / "nok-crd-metrics.py"
spec = importlib.util.spec_from_file_location(
    "nok_crd_metrics",
    MODULE_PATH
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
GenericCrdExporter = module.GenericCrdExporter
HEALTH_STATUS = module.HEALTH_STATUS
HealthCheckHandler = module.HealthCheckHandler

# Fixtures

@pytest.fixture(autouse=True)
def reset_global_state():
    """Reset global mutable state before and after every test."""
    HEALTH_STATUS["ok"] = True
    HEALTH_STATUS["message"] = "All metrics are scraping successfully."
    yield
    HEALTH_STATUS["ok"] = True
    HEALTH_STATUS["message"] = "All metrics are scraping successfully."


@pytest.fixture
def exporter():
    """Clean exporter with mocked Kubernetes API."""
    with patch("kubernetes.config.load_incluster_config"), \
         patch("kubernetes.config.load_kube_config"), \
         patch("kubernetes.client.CustomObjectsApi") as mock_custom_api:

        exp = GenericCrdExporter()
        exp.custom_api = mock_custom_api.return_value
        yield exp



# resolve_path() Tests
class TestResolvePath:

    def test_numeric_value(self, exporter):
        item = {"spec": {"count": 42}}
        assert exporter.resolve_path(item, "spec.count") == 42.0

    def test_float_string(self, exporter):
        item = {"spec": {"ratio": "0.85"}}
        assert exporter.resolve_path(item, "spec.ratio") == 0.85

    @pytest.mark.parametrize("value,expected", [
        (True, 1.0), (False, 0.0),
    ])
    def test_boolean(self, exporter, value, expected):
        item = {"status": {"ready": value}}
        assert exporter.resolve_path(item, "status.ready") == expected

    @pytest.mark.parametrize("value,expected", [
        ("True", 1.0), ("true", 1.0), ("reachable", 1.0),
        ("enabled", 1.0), ("ready", 1.0), ("ok", 1.0),
        ("False", 0.0), ("false", 0.0), ("unreachable", 0.0),
        ("disabled", 0.0), ("failed", 0.0), ("notready", 0.0),
    ])
    def test_string_state_conversion(self, exporter, value, expected):
        item = {"status": {"state": value}}
        assert exporter.resolve_path(item, "status.state") == expected

    def test_length_query(self, exporter):
        item = {"spec": {"deviations": [1, 2, 3, 4, 5]}}
        assert exporter.resolve_path(item, "spec.deviations.length") == 5.0

    def test_length_on_non_list(self, exporter):
        item = {"spec": {"value": "not-a-list"}}
        assert exporter.resolve_path(item, "spec.value.length") == 1.0

    def test_missing_path_metric_mode(self, exporter):
        assert exporter.resolve_path({}, "spec.unknown") == 0.0

    def test_missing_path_label_mode(self, exporter):
        assert exporter.resolve_path({}, "spec.unknown", is_label=True) == "unknown"

    def test_invalid_jsonpath_metric(self, exporter):
        assert exporter.resolve_path({"spec": {"x": 1}}, "spec.[") == 0.0

    def test_invalid_jsonpath_label(self, exporter):
        assert exporter.resolve_path({"spec": {"x": 1}}, "spec.[", is_label=True) == "error"

    def test_label_always_string(self, exporter):
        result = exporter.resolve_path({"spec": {"count": 123}}, "spec.count", is_label=True)
        assert result == "123"
        assert isinstance(result, str)



# HealthCheckHandler Tests
class TestHealthCheckHandler:

    def create_handler(self):
        handler = HealthCheckHandler.__new__(HealthCheckHandler)
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        handler.wfile = MagicMock()
        handler.wfile.write = MagicMock()
        return handler

    def test_healthy_endpoint(self):
        handler = self.create_handler()
        handler.path = '/healthy'
        HEALTH_STATUS["ok"] = True
        handler.do_GET()
        handler.send_response.assert_called_with(200)
        handler.wfile.write.assert_called_with(b"OK")

    def test_unhealthy_endpoint(self):
        handler = self.create_handler()
        handler.path = '/healthy'
        HEALTH_STATUS["ok"] = False
        HEALTH_STATUS["message"] = "RBAC denied"
        handler.do_GET()
        handler.send_response.assert_called_with(500)
        handler.wfile.write.assert_called_with(
            b"FAILED: RBAC denied"
        )

    def test_unknown_path(self):
        handler = self.create_handler()
        handler.path = '/notfound'
        handler.do_GET()
        handler.send_response.assert_called_with(404)



# wait_for_rbac() Tests
class TestWaitForRBAC:

    def test_retries_on_403_then_succeeds(self, exporter):
        exporter.custom_api.list_namespaced_custom_object.side_effect = [
            ApiException(status=403),
            {"items": []}  # success
        ]

        with patch("time.sleep") as mock_sleep:
            exporter.wait_for_rbac()

        assert exporter.custom_api.list_namespaced_custom_object.call_count == 2
        mock_sleep.assert_called_once_with(5)



# watch_definitions() Tests
class TestWatchDefinitions:

    @patch.object(module.watch.Watch, "stream")
    def test_added_event_creates_metric(self, mock_stream, exporter):
        event = {
            "type": "ADDED",
            "object": {
                "spec": {
                    "metricName": "test_metric",
                    "help": "A test metric",
                    "resource": {"group": "g", "version": "v1", "plural": "tests"},
                    "labelMappings": [{"label": "vendor", "path": "spec.vendor"}],
                    "valuePath": "status.ready"
                }
            }
        }
        mock_stream.side_effect = [[event],Exception("stop")]

        with patch("time.sleep", side_effect=Exception("stop")):
            with pytest.raises(Exception):
                exporter.watch_definitions()

        assert "test_metric" in exporter.metrics
        assert "test_metric" in exporter.definitions
        assert "test_metric" in exporter.active_metric_labels


# scrape_loop() Tests
class TestScrapeLoop:

    @patch.object(module, "start_http_server")
    @patch.object(module, "HTTPServer")
    @patch("time.sleep")
    def test_successful_scrape_and_stale_removal(self, mock_sleep, mock_httpserver, mock_start_http, exporter):
        # Setup
        gauge = MagicMock()
        exporter.metrics["test_metric"] = gauge
        exporter.definitions["test_metric"] = {
            "resource": {"group": "g", "version": "v1", "plural": "targets"},
            "labelMappings": [{"label": "vendor", "path": "spec.vendor"}],
            "valuePath": "status.ready"
        }
        exporter.active_metric_labels["test_metric"] = {
            ("cisco", "router2", "default")  # stale
        }

        exporter.custom_api.list_namespaced_custom_object.return_value = {
            "items": [{
                "metadata": {"name": "router1", "namespace": "default"},
                "spec": {"vendor": "nokia"},
                "status": {"ready": "True"}
            }]
        }

        # Run exactly one full iteration
        mock_sleep.side_effect = [None, Exception("stop after one cycle")]

        with pytest.raises(Exception):
            exporter.scrape_loop()

        # Verify new metric was set
        gauge.labels.assert_called_with(
            vendor="nokia",
            resource_name="router1",
            resource_namespace="default"
        )
        gauge.labels.return_value.set.assert_called_with(1.0)

        # Verify stale metric was removed
        gauge.remove.assert_called_with(
            vendor="cisco",
            resource_name="router2",
            resource_namespace="default"
        )

    @patch.object(module, "start_http_server")
    @patch.object(module, "HTTPServer")
    @patch("time.sleep")
    def test_rbac_403_marks_unhealthy(self, mock_sleep, mock_httpserver, mock_start_http, exporter):
        exporter.metrics["test_metric"] = MagicMock()
        exporter.definitions["test_metric"] = {
            "resource": {"group": "g", "version": "v1", "plural": "targets"},
            "labelMappings": [],
            "valuePath": "status.ready"
        }

        exporter.custom_api.list_namespaced_custom_object.side_effect = ApiException(status=403)

        mock_sleep.side_effect = [None, Exception("stop")]

        with pytest.raises(Exception):
            exporter.scrape_loop()

        assert HEALTH_STATUS["ok"] is False
        assert "RBAC denied" in HEALTH_STATUS["message"]

    @patch.object(module, "start_http_server")
    @patch.object(module, "HTTPServer")
    @patch("time.sleep")
    def test_generic_error_marks_unhealthy(self, mock_sleep, mock_httpserver, mock_start_http, exporter):
        exporter.metrics["test_metric"] = MagicMock()
        exporter.definitions["test_metric"] = {
            "resource": {"group": "g", "version": "v1", "plural": "targets"},
            "labelMappings": [],
            "valuePath": "status.ready"
        }

        exporter.custom_api.list_namespaced_custom_object.side_effect = Exception("boom")
        mock_sleep.side_effect = [None, Exception("stop")]
        with pytest.raises(Exception):
            exporter.scrape_loop()
        assert HEALTH_STATUS["ok"] is False
        assert "Unexpected error" in HEALTH_STATUS["message"]


# Namespace detection
def test_namespace_from_serviceaccount():
    m = mock_open(read_data="production-ns")
    with patch("builtins.open", m), \
         patch("kubernetes.config.load_incluster_config"), \
         patch("kubernetes.config.load_kube_config"), \
         patch("kubernetes.client.CustomObjectsApi"):
        exp = GenericCrdExporter()
        assert exp.namespace == "production-ns"