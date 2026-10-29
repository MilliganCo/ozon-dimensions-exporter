#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import csv
import os
import re
import sys
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests
from dotenv import load_dotenv


BASE_URL = "https://api-seller.ozon.ru"
LIST_PATH = "/v3/product/list"
ATTR_PATH = "/v4/product/info/attributes"


# ---------------------------
# HTTP + retry
# ---------------------------

def ozon_post(
    session: requests.Session,
    client_id: str,
    api_key: str,
    path: str,
    payload: Dict[str, Any],
    timeout: int = 60,
    max_retries: int = 6,
) -> Dict[str, Any]:
    url = BASE_URL + path
    headers = {
        "Client-Id": client_id,
        "Api-Key": api_key,
        "Content-Type": "application/json",
    }

    backoff = 1.0
    last_err = None

    for attempt in range(1, max_retries + 1):
        try:
            resp = session.post(url, headers=headers, json=payload, timeout=timeout)
            if resp.status_code in (429, 500, 502, 503, 504):
                last_err = RuntimeError(f"HTTP {resp.status_code}: {resp.text[:500]}")
                time.sleep(backoff)
                backoff *= 2
                continue

            resp.raise_for_status()
            return resp.json()

        except Exception as e:
            last_err = e
            time.sleep(backoff)
            backoff *= 2

    raise RuntimeError(f"Request failed after {max_retries} retries: {last_err}")


# ---------------------------
# Helpers: JSON extraction
# ---------------------------

def _get(d: Dict[str, Any], *keys: str, default=None):
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def extract_list_items(list_json: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], str]:
    """
    Возвращает (items, last_id_from_response).
    Подстраховка под разные варианты обёртки "result".
    """
    result = list_json.get("result", list_json)

    items = result.get("items")
    if items is None:
        # иногда может быть "result": {"result": [...]}
        items = result.get("result")

    if not isinstance(items, list):
        items = []

    last_id = result.get("last_id") or result.get("cursor") or ""
    return items, str(last_id) if last_id is not None else ""


def extract_attr_items(attr_json: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], str]:
    """
    Для /v4/product/info/attributes обычно result — список товаров.
    Но на всякий случай поддержим и вариант с result.items.
    """
    result = attr_json.get("result", attr_json)

    items = result if isinstance(result, list) else result.get("items") or result.get("result")
    if not isinstance(items, list):
        items = []

    last_id = ""
    if isinstance(result, dict):
        last_id = result.get("last_id") or result.get("cursor") or ""
    return items, str(last_id) if last_id is not None else ""


# ---------------------------
# Parsing: dimensions & weight
# ---------------------------

_NUM_RE = re.compile(r"([-+]?\d+(?:[.,]\d+)?)\s*([a-zа-я]*)", re.IGNORECASE)

def normalize_name(s: str) -> str:
    s = (s or "").strip().lower()
    s = s.replace("ё", "е")
    s = re.sub(r"\s+", " ", s)
    return s


def parse_measure(value: str, kind: str) -> Optional[float]:
    """
    kind: 'dim' -> returns mm
          'weight' -> returns g
    Поддерживает строки вида: "12", "12 см", "0.3 кг", "120 мм", "1,5"
    """
    if value is None or value == "":
        return None
    if kind not in ("dim", "weight"):
        raise ValueError("kind must be 'dim' or 'weight'")
    s = str(value).strip().lower().replace("ё", "е")
    m = _NUM_RE.fullmatch(s)
    if not m:
        return None
    num = float(m.group(1).replace(",", "."))
    if num < 0:
        return None
    unit = m.group(2)
    if kind == "dim":
        factors = {"": 1.0, "mm": 1.0, "мм": 1.0, "cm": 10.0, "см": 10.0, "m": 1000.0, "м": 1000.0}
    else:
        factors = {"": 1.0, "kg": 1000.0, "кг": 1000.0, "g": 1.0, "г": 1.0, "гр": 1.0, "mg": 0.001, "мг": 0.001}
    factor = factors.get(unit)
    return None if factor is None else num * factor


def values_to_text(attr: Dict[str, Any]) -> str:
    """
    Извлекает значение атрибута в строку из разных возможных форматов.
    """
    # варианты: {"values":[{"value":"10"}]}, {"value":"10"}, {"values":["10"]}
    if "value" in attr and isinstance(attr["value"], (str, int, float)):
        return str(attr["value"])

    vals = attr.get("values")
    if isinstance(vals, list) and vals:
        v0 = vals[0]
        if isinstance(v0, dict) and "value" in v0:
            return str(v0["value"])
        if isinstance(v0, (str, int, float)):
            return str(v0)

    return ""


def build_attr_map(product: Dict[str, Any]) -> Dict[str, str]:
    """
    Строит map: normalized_attribute_name -> value_text
    """
    # Пробуем разные варианты расположения атрибутов
    attrs = (
        product.get("attributes") or
        product.get("attrs") or
        product.get("attribute_values") or
        product.get("info") or  # может быть в info.attributes
        []
    )

    # Если attrs не список, пробуем найти его в других местах
    if not isinstance(attrs, list):
        # Проверяем вложенные структуры
        if isinstance(product, dict):
            # Проверяем info.attributes
            info = product.get("info")
            if isinstance(info, dict):
                attrs = info.get("attributes") or info.get("attrs") or []

            # Если все еще не список, ищем по ключам
            if not isinstance(attrs, list):
                for key in product.keys():
                    if "attr" in key.lower() and isinstance(product[key], list):
                        attrs = product[key]
                        break

    if not isinstance(attrs, list):
        return {}

    out: Dict[str, str] = {}
    for a in attrs:
        if not isinstance(a, dict):
            continue
        # Пробуем разные варианты названий полей для имени атрибута
        name = (
            a.get("name") or
            a.get("attribute_name") or
            a.get("title") or
            a.get("attribute_id") or
            a.get("id") or
            a.get("dictionary_value_id") or  # может быть ID вместо имени
            ""
        )
        n = normalize_name(str(name))
        if not n:
            continue
        val = values_to_text(a)
        if val:
            out[n] = str(val)
    return out


DIM_KEYS_PRIORITY = {
    "length": [
        "длина упаковки", "длина", "глубина", "depth", "length",
    ],
    "width": [
        "ширина упаковки", "ширина", "width",
    ],
    "height": [
        "высота упаковки", "высота", "height",
    ],
    "weight": [
        "вес в упаковке", "вес", "weight",
    ],
}

def pick_by_priority(attr_map: Dict[str, str], keys: List[str]) -> Optional[str]:
    for k in keys:
        nk = normalize_name(k)
        if nk in attr_map:
            return attr_map[nk]
    # fallback: contains match
    for k in keys:
        nk = normalize_name(k)
        for an, av in attr_map.items():
            if nk and nk in an:
                return av
    return None


def extract_dims_weight(product: Dict[str, Any]) -> Dict[str, Any]:
    """
    Возвращает:
      length_mm, width_mm, height_mm, weight_g (float|None)
      *_raw (str)
      source ("top_level"|"attributes"|"mixed"|"not_found")
    """
    # 1) пробуем top-level поля (если вдруг есть)
    length_raw = ""
    width_raw = ""
    height_raw = ""
    weight_raw = ""
    src_top = False
    src_attr = False

    # Сначала проверяем прямые поля на верхнем уровне (Ozon API возвращает их так)
    # Ozon API возвращает: height, depth (длина/глубина), width, weight
    # Также есть dimension_unit и weight_unit для единиц измерения

    if product.get("depth") is not None:
        depth_val = product.get("depth")
        dimension_unit = product.get("dimension_unit", "mm")
        # Если единица не мм, нужно конвертировать (но обычно уже в мм)
        length_raw = f"{depth_val} {dimension_unit}" if dimension_unit != "mm" else str(depth_val)
        src_top = True
    elif product.get("length") is not None:
        length_val = product.get("length")
        dimension_unit = product.get("dimension_unit", "mm")
        length_raw = f"{length_val} {dimension_unit}" if dimension_unit != "mm" else str(length_val)
        src_top = True

    if product.get("width") is not None:
        width_val = product.get("width")
        dimension_unit = product.get("dimension_unit", "mm")
        width_raw = f"{width_val} {dimension_unit}" if dimension_unit != "mm" else str(width_val)
        src_top = True

    if product.get("height") is not None:
        height_val = product.get("height")
        dimension_unit = product.get("dimension_unit", "mm")
        height_raw = f"{height_val} {dimension_unit}" if dimension_unit != "mm" else str(height_val)
        src_top = True

    if product.get("weight") is not None:
        weight_val = product.get("weight")
        weight_unit = product.get("weight_unit", "g")
        # Если единица не г, нужно конвертировать (но обычно уже в г)
        weight_raw = f"{weight_val} {weight_unit}" if weight_unit != "g" else str(weight_val)
        src_top = True

    # Также проверяем вложенную структуру dimensions (на случай других форматов)
    dims = product.get("dimensions") or product.get("dimension") or None
    if isinstance(dims, dict):
        nested_unit = dims.get("dimension_unit") or dims.get("unit") or product.get("dimension_unit") or "mm"
        def nested_text(value):
            raw = str(value).strip()
            parsed = _NUM_RE.fullmatch(raw.lower())
            return f"{raw} {nested_unit}" if parsed and not parsed.group(2) else raw
        # возможные ключи: height/width/depth/length
        # и/или package_* (не гарантируем)
        if not length_raw:
            for lk in ("length", "depth"):
                if lk in dims and dims[lk] is not None:
                    length_raw = nested_text(dims[lk])
                    src_top = True
                    break
        if not width_raw and "width" in dims and dims["width"] is not None:
            width_raw = nested_text(dims["width"])
            src_top = True
        if not height_raw and "height" in dims and dims["height"] is not None:
            height_raw = nested_text(dims["height"])
            src_top = True

    # 2) если чего-то нет — ищем в attributes
    attr_map = build_attr_map(product)

    if not length_raw:
        v = pick_by_priority(attr_map, DIM_KEYS_PRIORITY["length"])
        if v:
            length_raw = v
            src_attr = True
    if not width_raw:
        v = pick_by_priority(attr_map, DIM_KEYS_PRIORITY["width"])
        if v:
            width_raw = v
            src_attr = True
    if not height_raw:
        v = pick_by_priority(attr_map, DIM_KEYS_PRIORITY["height"])
        if v:
            height_raw = v
            src_attr = True
    if not weight_raw:
        v = pick_by_priority(attr_map, DIM_KEYS_PRIORITY["weight"])
        if v:
            weight_raw = v
            src_attr = True

    length_mm = parse_measure(length_raw, "dim") if length_raw else None
    width_mm = parse_measure(width_raw, "dim") if width_raw else None
    height_mm = parse_measure(height_raw, "dim") if height_raw else None
    weight_g = parse_measure(weight_raw, "weight") if weight_raw else None

    if src_top and src_attr:
        source = "mixed"
    elif src_top:
        source = "top_level"
    elif src_attr:
        source = "attributes"
    else:
        source = "not_found"

    return {
        "length_mm": length_mm,
        "width_mm": width_mm,
        "height_mm": height_mm,
        "weight_g": weight_g,
        "length_raw": length_raw,
        "width_raw": width_raw,
        "height_raw": height_raw,
        "weight_raw": weight_raw,
        "source": source,
    }


# ---------------------------
# Main logic
# ---------------------------

def chunked(seq: List[Any], n: int) -> Iterable[List[Any]]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def get_all_products(session: requests.Session, client_id: str, api_key: str, visibility: str, limit: int) -> List[Dict[str, Any]]:
    all_items: List[Dict[str, Any]] = []
    last_id = ""
    seen_cursors = set()
    while True:
        payload = {
            "filter": {"visibility": visibility},
            "last_id": last_id,
            "limit": limit,
        }
        data = ozon_post(session, client_id, api_key, LIST_PATH, payload)
        items, next_last_id = extract_list_items(data)

        if not items:
            break

        all_items.extend(items)

        # стоп-условие пагинации
        if not next_last_id or next_last_id == last_id:
            break
        if next_last_id in seen_cursors:
            raise RuntimeError("Product pagination cursor cycle")
        seen_cursors.add(next_last_id)
        last_id = next_last_id

    return all_items


def fetch_attributes_by_offer_ids(
    session: requests.Session,
    client_id: str,
    api_key: str,
    offer_ids: List[str],
    visibility: str,
    save_raw_response: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []

    # Если в ответе окажется пагинация last_id — пройдёмся по ней (на всякий случай).
    last_id = ""
    seen_cursors = set()
    while True:
        payload = {
            "filter": {
                "offer_id": offer_ids,
                "visibility": visibility,
            },
            "last_id": last_id,
            "limit": 1000,
        }
        data = ozon_post(session, client_id, api_key, ATTR_PATH, payload)

        # Сохраняем сырой ответ для отладки (только первый раз)
        if save_raw_response is not None and len(save_raw_response) == 0:
            save_raw_response.append({
                "request_payload": payload,
                "response_data": data,
            })

        items, next_last_id = extract_attr_items(data)
        if not items:
            break
        out.extend(items)

        if not next_last_id or next_last_id == last_id:
            break
        if next_last_id in seen_cursors:
            raise RuntimeError("Attribute pagination cursor cycle")
        seen_cursors.add(next_last_id)
        last_id = next_last_id

    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Export Ozon product dimensions & weight to CSV via Seller API.")
    parser.add_argument("--env-file", help="Explicit private .env path; no automatic .env search")
    parser.add_argument("--client-id", default="", help="Ozon Client-Id (or env OZON_CLIENT_ID)")
    parser.add_argument("--api-key", default="", help="Ozon Api-Key (prefer private env OZON_API_KEY)")
    parser.add_argument("--visibility", default="ALL", help="Visibility filter (e.g. ALL, VISIBLE, INVISIBLE, ARCHIVED, ...)")
    parser.add_argument("--list-limit", type=int, default=1000, help="Page size for /v3/product/list (1..1000)")
    parser.add_argument("--batch-size", type=int, default=1000, help="Batch size for /v4/product/info/attributes (<=1000)")
    parser.add_argument("--out", default="ozon_dims_weight.csv", help="Output CSV path")
    parser.add_argument("--debug", action="store_true", help="Print debug information about API responses")
    parser.add_argument("--save-raw", help="Save raw API response to JSON file for debugging")

    args = parser.parse_args()
    if not 1 <= args.list_limit <= 1000 or not 1 <= args.batch_size <= 1000:
        parser.error("--list-limit and --batch-size must be between 1 and 1000")
    if args.env_file:
        if not os.path.isfile(args.env_file):
            parser.error("--env-file must name an existing private file")
        load_dotenv(dotenv_path=args.env_file, override=False)
    args.client_id = args.client_id or os.getenv("OZON_CLIENT_ID", "")
    args.api_key = args.api_key or os.getenv("OZON_API_KEY", "")

    if not args.client_id or not args.api_key:
        print("ERROR: provide --client-id/--api-key or set OZON_CLIENT_ID/OZON_API_KEY env vars", file=sys.stderr)
        return 2

    session = requests.Session()

    # 1) get products list
    products = get_all_products(session, args.client_id, args.api_key, args.visibility, args.list_limit)

    # extract offer_ids + ids
    rows_index: Dict[str, Dict[str, Any]] = {}
    offer_ids: List[str] = []

    for p in products:
        offer_id = p.get("offer_id") or p.get("offerId") or p.get("offerID")
        if not offer_id:
            continue
        offer_id = str(offer_id)
        offer_ids.append(offer_id)
        rows_index[offer_id] = {
            "offer_id": offer_id,
            "product_id": p.get("product_id") or p.get("id") or "",
            "sku": p.get("sku") or "",
            "name": p.get("name") or p.get("title") or "",
        }

    # 2) fetch attributes in batches
    result_rows: List[Dict[str, Any]] = []
    raw_responses: List[Dict[str, Any]] = []

    for batch in chunked(offer_ids, min(args.batch_size, 1000)):
        attrs_products = fetch_attributes_by_offer_ids(
            session, args.client_id, args.api_key, batch, args.visibility,
            save_raw_response=raw_responses if args.save_raw else None
        )

        for prod in attrs_products:
            offer_id = prod.get("offer_id") or prod.get("offerId") or prod.get("offerID") or ""
            offer_id = str(offer_id) if offer_id is not None else ""
            base = rows_index.get(offer_id, {"offer_id": offer_id, "product_id": "", "sku": "", "name": ""})

            # Отладочный вывод структуры ответа
            if args.debug and len(result_rows) < 3:
                import json
                print(f"\n[DEBUG] Product structure for offer_id={offer_id}:", file=sys.stderr)
                print(json.dumps(prod, indent=2, ensure_ascii=False)[:2000], file=sys.stderr)
                print("\n[DEBUG] Available keys:", list(prod.keys())[:20], file=sys.stderr)
                attrs_in_prod = prod.get("attributes") or prod.get("attrs") or []
                print(f"[DEBUG] Attributes found: {type(attrs_in_prod)}, length: {len(attrs_in_prod) if isinstance(attrs_in_prod, list) else 'N/A'}", file=sys.stderr)
                if isinstance(attrs_in_prod, list) and attrs_in_prod:
                    print(f"[DEBUG] First attribute sample: {json.dumps(attrs_in_prod[0], indent=2, ensure_ascii=False)[:500]}", file=sys.stderr)

                # Проверяем attr_map
                attr_map = build_attr_map(prod)
                print(f"[DEBUG] Built attr_map keys: {list(attr_map.keys())[:30]}", file=sys.stderr)
                print(f"[DEBUG] Looking for dimensions in attr_map...", file=sys.stderr)
                for dim_type in ["length", "width", "height", "weight"]:
                    keys = DIM_KEYS_PRIORITY[dim_type]
                    found = pick_by_priority(attr_map, keys)
                    print(f"[DEBUG] {dim_type}: found={found}, searched_keys={keys[:3]}", file=sys.stderr)

            dims = extract_dims_weight(prod)

            result_rows.append({
                "offer_id": base.get("offer_id", ""),
                "product_id": prod.get("product_id") or base.get("product_id", ""),
                "sku": prod.get("sku") or base.get("sku", ""),
                "name": base.get("name", "") or prod.get("name") or prod.get("title") or "",
                "length_mm": dims["length_mm"],
                "width_mm": dims["width_mm"],
                "height_mm": dims["height_mm"],
                "weight_g": dims["weight_g"],
                "length_raw": dims["length_raw"],
                "width_raw": dims["width_raw"],
                "height_raw": dims["height_raw"],
                "weight_raw": dims["weight_raw"],
                "source": dims["source"],
            })

        # небольшая пауза, чтобы меньше упираться в лимиты
        time.sleep(0.15)

    # Сохраняем сырой ответ если нужно
    if args.save_raw and raw_responses:
        import json
        with open(args.save_raw, "w", encoding="utf-8") as f:
            json.dump(raw_responses, f, indent=2, ensure_ascii=False)
        print(f"DEBUG: saved raw API response -> {args.save_raw}", file=sys.stderr)

    # 3) write CSV
    fieldnames = [
        "offer_id", "product_id", "sku", "name",
        "length_mm", "width_mm", "height_mm", "weight_g",
        "length_raw", "width_raw", "height_raw", "weight_raw",
        "source",
    ]

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in result_rows:
            w.writerow(r)

    print(f"OK: wrote {len(result_rows)} rows -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
