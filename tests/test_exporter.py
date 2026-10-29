import importlib.util
import contextlib
import csv
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).parents[1] / "ozon_dims_weight_to_csv.py"
spec = importlib.util.spec_from_file_location("dimensions_exporter", SOURCE)
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


class ExporterTests(unittest.TestCase):
    def setUp(self):
        blocked = patch("requests.sessions.Session.request", side_effect=AssertionError("network forbidden in synthetic tests"))
        blocked.start()
        self.addCleanup(blocked.stop)

    def test_compact_and_overlapping_units(self):
        for value, kind, expected in [
            ("500 мг", "weight", 0.5),
            ("500mg", "weight", 0.5),
            ("2,5kg", "weight", 2500),
            ("12cm", "dim", 120),
            ("2м", "dim", 2000),
            ("1.5 m", "dim", 1500),
            ("120 мм", "dim", 120),
        ]:
            with self.subTest(value=value, kind=kind):
                self.assertEqual(exporter.parse_measure(value, kind), expected)

    def test_empty_and_non_numeric_measure(self):
        for value in ("", "unknown", None):
            self.assertIsNone(exporter.parse_measure(value, "dim"))

    def test_unknown_compound_and_negative_values_are_not_guessed(self):
        for value, kind in [("12 inches", "dim"), ("10x20 cm", "dim"), ("-2 mm", "dim"), ("-1 g", "weight")]:
            self.assertIsNone(exporter.parse_measure(value, kind))
        self.assertEqual(exporter.parse_measure("0", "weight"), 0)

    def test_top_fields_win_and_missing_fields_use_packaging_attributes(self):
        data = {
            "depth": 3, "dimension_unit": "cm", "weight": 1.25, "weight_unit": "kg",
            "attributes": [
                {"name": "Длина упаковки", "values": [{"value": "99 см"}]},
                {"name": "Ширина", "value": "999 мм"},
                {"name": "Ширина упаковки", "value": "4 cm"},
                {"name": "Высота упаковки", "value": "5 мм"},
            ],
        }
        result = exporter.extract_dims_weight(data)
        self.assertEqual([result[k] for k in ("length_mm", "width_mm", "height_mm", "weight_g")], [30, 40, 5, 1250])
        self.assertEqual(result["source"], "mixed")

    def test_nested_dimensions_keep_their_declared_unit(self):
        result = exporter.extract_dims_weight({"dimensions": {"length": 1.2, "width": 0.5, "height": 2, "unit": "m"}})
        self.assertEqual([result[k] for k in ("length_mm", "width_mm", "height_mm")], [1200, 500, 2000])

    def test_nested_measure_text_does_not_receive_a_second_unit(self):
        result = exporter.extract_dims_weight({"dimensions": {"length": "12 cm", "unit": "mm"}})
        self.assertEqual(result["length_mm"], 120)

    def test_attribute_values_and_not_found_are_explicit(self):
        product = {"info": {"attributes": [{"attribute_name": "Вес в упаковке", "values": ["0,25 кг"]}]}}
        result = exporter.extract_dims_weight(product)
        self.assertEqual(result["weight_g"], 250)
        self.assertEqual(result["source"], "attributes")
        self.assertEqual(exporter.extract_dims_weight({})["source"], "not_found")
        self.assertIsNone(exporter.extract_dims_weight({})["weight_g"])

    def test_product_pages_follow_cursor_and_stop_on_empty(self):
        responses = [
            {"result": {"items": [{"offer_id": "EXAMPLE-001"}], "last_id": "A"}},
            {"result": {"items": [{"offer_id": "EXAMPLE-002"}], "last_id": "B"}},
            {"result": {"items": [], "last_id": ""}},
        ]
        with patch.object(exporter, "ozon_post", side_effect=responses) as post:
            result = exporter.get_all_products(object(), "synthetic-client", "synthetic-key", "ALL", 2)
        self.assertEqual([x["offer_id"] for x in result], ["EXAMPLE-001", "EXAMPLE-002"])
        self.assertEqual([call.args[4]["last_id"] for call in post.call_args_list], ["", "A", "B"])

    def test_current_cursor_repeat_stops_without_extra_request(self):
        responses = [
            {"result": {"items": [{"offer_id": "EXAMPLE-001"}], "last_id": "A"}},
            {"result": {"items": [{"offer_id": "EXAMPLE-002"}], "last_id": "A"}},
        ]
        with patch.object(exporter, "ozon_post", side_effect=responses) as post:
            result = exporter.get_all_products(object(), "synthetic-client", "synthetic-key", "ALL", 2)
        self.assertEqual(len(result), 2)
        self.assertEqual(post.call_count, 2)

    def test_cyclic_product_cursor_is_an_error(self):
        responses = [{"result": {"items": [{"offer_id": "EXAMPLE"}], "last_id": cursor}} for cursor in ["A", "B", "A"]]
        with patch.object(exporter, "ozon_post", side_effect=responses):
            with self.assertRaisesRegex(RuntimeError, "cursor"):
                exporter.get_all_products(object(), "synthetic-client", "synthetic-key", "ALL", 2)

    def test_attribute_empty_page_terminates_even_with_cursor(self):
        with patch.object(exporter, "ozon_post", return_value={"result": {"items": [], "last_id": "A"}}) as post:
            self.assertEqual(exporter.fetch_attributes_by_offer_ids(object(), "synthetic-client", "synthetic-key", ["EXAMPLE"], "ALL"), [])
        self.assertEqual(post.call_count, 1)

    def test_attribute_list_wrapper_and_cursor_cycle(self):
        with patch.object(exporter, "ozon_post", return_value={"result": [{"offer_id": "EXAMPLE"}]}):
            self.assertEqual(len(exporter.fetch_attributes_by_offer_ids(object(), "synthetic-client", "synthetic-key", ["EXAMPLE"], "ALL")), 1)
        responses = [{"result": {"items": [{"offer_id": "EXAMPLE"}], "last_id": cursor}} for cursor in ["A", "B", "A"]]
        with patch.object(exporter, "ozon_post", side_effect=responses):
            with self.assertRaisesRegex(RuntimeError, "cursor"):
                exporter.fetch_attributes_by_offer_ids(object(), "synthetic-client", "synthetic-key", ["EXAMPLE"], "ALL")

    def test_missing_credentials_does_not_search_dotenv_or_create_session(self):
        with patch.dict(os.environ, {}, clear=True), patch("sys.argv", ["exporter.py"]), patch.object(exporter, "load_dotenv") as env, patch.object(exporter.requests, "Session") as session:
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(exporter.main(), 2)
        env.assert_not_called()
        session.assert_not_called()

    def test_invalid_batch_size_is_rejected_before_session(self):
        with patch.dict(os.environ, {"OZON_CLIENT_ID": "synthetic-client", "OZON_API_KEY": "synthetic-key"}, clear=True), patch("sys.argv", ["exporter.py", "--batch-size", "0"]), patch.object(exporter.requests, "Session") as session:
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as result:
                exporter.main()
        self.assertEqual(result.exception.code, 2)
        session.assert_not_called()

    def test_synthetic_cli_csv_uses_no_seller_request(self):
        work = SOURCE.parent / "work"
        work.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=work) as directory:
            output = Path(directory) / "synthetic.csv"
            products = [{"offer_id": "EXAMPLE-001", "product_id": 1, "name": "Synthetic"}]
            attrs = [{"offer_id": "EXAMPLE-001", "depth": 2, "width": 3, "height": 4, "dimension_unit": "cm", "weight": 500, "weight_unit": "mg"}]
            with patch.dict(os.environ, {"OZON_CLIENT_ID": "synthetic-client", "OZON_API_KEY": "synthetic-key"}, clear=True), patch("sys.argv", ["exporter.py", "--out", str(output)]), patch.object(exporter, "load_dotenv") as env, patch.object(exporter, "get_all_products", return_value=products), patch.object(exporter, "fetch_attributes_by_offer_ids", return_value=attrs), patch.object(exporter.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(exporter.main(), 0)
            env.assert_not_called()
            with output.open(encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["offer_id"], "EXAMPLE-001")
            self.assertEqual([float(rows[0][k]) for k in ("length_mm", "width_mm", "height_mm", "weight_g")], [20, 30, 40, 0.5])


if __name__ == "__main__":
    unittest.main()
