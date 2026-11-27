"""Versioned multi-brand store catalog.

Brands are configured with ``AUTOPHONE_STORE_BRANDS`` as comma separated
``brand=SOURCE_BRAND`` pairs; ``brand`` is the catalog key used by App Profiles
and the API, ``SOURCE_BRAND`` is the value expected in the workbook "品牌" column.
The source workbooks are an import format, not a runtime dependency.  A build
step validates and normalizes them into a deterministic gzip JSONL snapshot.
At runtime :class:`StoreCatalog` hides the snapshot behind a small query API
and builds an in-memory SQLite index lazily.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import math
import os
import sqlite3
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from openpyxl import load_workbook

from autophone.assets.policy import assert_assets_writable


SCHEMA_VERSION = "store-catalog.v1"
RUN_CONTEXT_SCHEMA_VERSION = "store-context.v1"
SHEET_NAME = "门店信息"
NORMAL_STATUS = "正常营业"
DEFAULT_BRAND_SPEC = "brand_a=BRAND_A,brand_b=BRAND_B"


def parse_brand_spec(spec: str) -> dict[str, str]:
    """Parse ``brand=SOURCE_BRAND`` pairs into an ordered mapping."""
    brands: dict[str, str] = {}
    for item in str(spec or "").split(","):
        item = item.strip()
        if not item:
            continue
        key, separator, source = item.partition("=")
        key = key.strip().lower()
        source = source.strip()
        if not separator or not key or not source:
            raise ValueError(f"invalid store brand entry: {item!r}")
        brands[key] = source
    if not brands:
        raise ValueError("store brand configuration is empty")
    return brands


SOURCE_BRANDS = parse_brand_spec(
    os.environ.get("AUTOPHONE_STORE_BRANDS") or DEFAULT_BRAND_SPEC
)
VALID_BRANDS = frozenset(SOURCE_BRANDS)
VALID_COORDINATE_ROLES = frozenset({"source_store", "source_map"})

HEADERS = (
    "品牌",
    "餐厅编码",
    "餐厅名称",
    "餐厅简写",
    "餐厅地址",
    "邮箱",
    "门店坐标",
    "市场名称",
    "市场编码",
    "城市名称",
    "国标编码",
    "城市编码（老）",
    "餐厅状态",
    "Label",
    "门店类型",
    "环境照",
    "门店照",
    "餐厅大图",
    "地图经纬度",
    "容忍距离(米)",
)
EXCLUDED_HEADERS = ("环境照", "门店照", "餐厅大图")

DEFAULT_DATA_DIR = (
    Path(__file__).resolve().parent.parent / "_data" / "store-catalog"
)
DEFAULT_MANIFEST_PATH = DEFAULT_DATA_DIR / "manifest.json"
DEFAULT_SNAPSHOT_PATH = DEFAULT_DATA_DIR / "stores.jsonl.gz"


class StoreCatalogError(RuntimeError):
    """Base class for store catalog errors."""


class StoreCatalogUnavailable(StoreCatalogError):
    """Raised when the runtime snapshot is missing or unreadable."""


class StoreDataError(StoreCatalogError):
    """Raised when source data violates the store catalog contract."""


class StoreNotFound(StoreCatalogError):
    """Raised when a canonical store identity does not exist."""


@dataclass(frozen=True)
class Coordinate:
    role: str
    lng: float
    lat: float
    raw: str

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "lng": self.lng,
            "lat": self.lat,
            "raw": self.raw,
        }


@dataclass(frozen=True)
class Store:
    brand: str
    source_brand: str
    store_code: str
    name: str
    short_name: str
    address: str
    email: str
    market_name: str
    market_code: str
    city_name: str
    national_city_code: str
    legacy_city_code: str
    status: str
    labels: tuple[str, ...]
    store_type: str
    source_store: Coordinate | None
    source_map: Coordinate | None
    tolerance_m: int | None
    source_file: str
    source_row: int

    @property
    def identity(self) -> str:
        return f"{self.brand}:{self.store_code}"

    def coordinate(self, role: str) -> Coordinate | None:
        if role not in VALID_COORDINATE_ROLES:
            raise ValueError(f"unsupported coordinate role: {role}")
        return self.source_store if role == "source_store" else self.source_map

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "brand": self.brand,
            "source_brand": self.source_brand,
            "store_code": self.store_code,
            "name": self.name,
            "short_name": self.short_name,
            "address": self.address,
            "email": self.email,
            "market_name": self.market_name,
            "market_code": self.market_code,
            "city_name": self.city_name,
            "national_city_code": self.national_city_code,
            "legacy_city_code": self.legacy_city_code,
            "status": self.status,
            "labels": list(self.labels),
            "store_type": self.store_type,
            "coordinates": {
                "source_store": (
                    self.source_store.to_public_dict()
                    if self.source_store is not None
                    else None
                ),
                "source_map": (
                    self.source_map.to_public_dict()
                    if self.source_map is not None
                    else None
                ),
            },
            "tolerance_m": self.tolerance_m,
        }


@dataclass(frozen=True)
class StoreSearchResult:
    items: tuple[Store, ...]
    total: int
    limit: int
    offset: int

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "items": [item.to_public_dict() for item in self.items],
            "total": self.total,
            "limit": self.limit,
            "offset": self.offset,
        }


_SCHEMA_SQL = """
CREATE TABLE stores (
    brand TEXT NOT NULL,
    source_brand TEXT NOT NULL,
    store_code TEXT NOT NULL,
    name TEXT NOT NULL,
    short_name TEXT NOT NULL,
    address TEXT NOT NULL,
    email TEXT NOT NULL,
    store_coord_raw TEXT NOT NULL,
    store_lng REAL,
    store_lat REAL,
    market_name TEXT NOT NULL,
    market_code TEXT NOT NULL,
    city_name TEXT NOT NULL,
    national_city_code TEXT NOT NULL,
    legacy_city_code TEXT NOT NULL,
    status TEXT NOT NULL,
    labels_json TEXT NOT NULL,
    store_type TEXT NOT NULL,
    map_coord_raw TEXT NOT NULL,
    map_lng REAL,
    map_lat REAL,
    tolerance_m INTEGER,
    source_file TEXT NOT NULL,
    source_row INTEGER NOT NULL,
    PRIMARY KEY (brand, store_code)
);
CREATE INDEX stores_brand_name ON stores (brand, name);
CREATE INDEX stores_brand_city ON stores (brand, city_name);
CREATE INDEX stores_brand_status ON stores (brand, status);
CREATE INDEX stores_brand_market ON stores (brand, market_code);
"""

_COLUMNS = (
    "brand",
    "source_brand",
    "store_code",
    "name",
    "short_name",
    "address",
    "email",
    "store_coord_raw",
    "store_lng",
    "store_lat",
    "market_name",
    "market_code",
    "city_name",
    "national_city_code",
    "legacy_city_code",
    "status",
    "labels_json",
    "store_type",
    "map_coord_raw",
    "map_lng",
    "map_lat",
    "tolerance_m",
    "source_file",
    "source_row",
)


class StoreCatalog:
    """Resolve and query stores without exposing snapshot or SQL details."""

    def __init__(
        self,
        snapshot_path: Path = DEFAULT_SNAPSHOT_PATH,
        manifest_path: Path = DEFAULT_MANIFEST_PATH,
    ) -> None:
        self._snapshot_path = Path(snapshot_path)
        self._manifest_path = Path(manifest_path)
        self._connection: sqlite3.Connection | None = None
        self._manifest: dict[str, Any] | None = None
        self._lock = threading.RLock()

    @property
    def manifest(self) -> dict[str, Any]:
        self._ensure_loaded()
        assert self._manifest is not None
        return dict(self._manifest)

    def resolve(self, brand: str, store_code: str) -> Store:
        canonical_brand = _normalize_brand(brand)
        code = _required_text(store_code, "store_code")
        with self._lock:
            connection = self._ensure_loaded()
            row = connection.execute(
                "SELECT * FROM stores WHERE brand = ? AND store_code = ?",
                (canonical_brand, code),
            ).fetchone()
        if row is None:
            raise StoreNotFound(f"store not found: {canonical_brand}:{code}")
        return _row_to_store(row)

    def search(
        self,
        brand: str,
        query: str = "",
        *,
        status: str | None = None,
        market_code: str | None = None,
        city_code: str | None = None,
        limit: int = 30,
        offset: int = 0,
    ) -> StoreSearchResult:
        canonical_brand = _normalize_brand(brand)
        limit = max(1, min(int(limit), 100))
        offset = max(0, int(offset))
        query = str(query or "").strip()

        clauses = ["brand = ?"]
        params: list[Any] = [canonical_brand]
        if status:
            clauses.append("status = ?")
            params.append(str(status))
        if market_code:
            clauses.append("market_code = ?")
            params.append(str(market_code))
        if city_code:
            clauses.append("national_city_code = ?")
            params.append(str(city_code))

        order_params: list[Any] = []
        if query:
            escaped = _escape_like(query)
            pattern = f"%{escaped}%"
            prefix = f"{escaped}%"
            clauses.append(
                "(store_code LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\' "
                "OR short_name LIKE ? ESCAPE '\\' OR address LIKE ? ESCAPE '\\' "
                "OR city_name LIKE ? ESCAPE '\\')"
            )
            params.extend([pattern] * 5)
            order = (
                "CASE WHEN store_code = ? THEN 0 "
                "WHEN store_code LIKE ? ESCAPE '\\' THEN 1 "
                "WHEN name LIKE ? ESCAPE '\\' THEN 2 ELSE 3 END, "
                "city_name, store_code"
            )
            order_params.extend([query, prefix, prefix])
        else:
            order = (
                f"CASE WHEN status = '{NORMAL_STATUS}' THEN 0 ELSE 1 END, "
                "city_name, store_code"
            )

        where = " AND ".join(clauses)
        with self._lock:
            connection = self._ensure_loaded()
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM stores WHERE {where}", params
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"SELECT * FROM stores WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                [*params, *order_params, limit, offset],
            ).fetchall()
        return StoreSearchResult(
            items=tuple(_row_to_store(row) for row in rows),
            total=total,
            limit=limit,
            offset=offset,
        )

    def nearest(
        self,
        brand: str,
        lat: float,
        lng: float,
        *,
        role: str = "source_map",
        status: str | None = NORMAL_STATUS,
        limit: int = 10,
    ) -> list[tuple[Store, float]]:
        canonical_brand = _normalize_brand(brand)
        if role not in VALID_COORDINATE_ROLES:
            raise ValueError(f"unsupported coordinate role: {role}")
        lat_col = "store_lat" if role == "source_store" else "map_lat"
        lng_col = "store_lng" if role == "source_store" else "map_lng"
        clauses = ["brand = ?", f"{lat_col} IS NOT NULL", f"{lng_col} IS NOT NULL"]
        params: list[Any] = [canonical_brand]
        if status:
            clauses.append("status = ?")
            params.append(status)
        with self._lock:
            connection = self._ensure_loaded()
            rows = connection.execute(
                f"SELECT * FROM stores WHERE {' AND '.join(clauses)}", params
            ).fetchall()
        measured = [
            (
                _row_to_store(row),
                _haversine_m(
                    float(lat),
                    float(lng),
                    float(row[lat_col]),
                    float(row[lng_col]),
                ),
            )
            for row in rows
        ]
        measured.sort(key=lambda item: item[1])
        return measured[: max(1, min(int(limit), 100))]

    def coordinates_by_code(
        self,
        brand: str,
        *,
        role: str = "source_store",
        status: str | None = None,
    ) -> dict[str, tuple[float, float]]:
        canonical_brand = _normalize_brand(brand)
        if role not in VALID_COORDINATE_ROLES:
            raise ValueError(f"unsupported coordinate role: {role}")
        lat_col = "store_lat" if role == "source_store" else "map_lat"
        lng_col = "store_lng" if role == "source_store" else "map_lng"
        clauses = ["brand = ?", f"{lat_col} IS NOT NULL", f"{lng_col} IS NOT NULL"]
        params: list[Any] = [canonical_brand]
        if status:
            clauses.append("status = ?")
            params.append(status)
        with self._lock:
            rows = self._ensure_loaded().execute(
                f"SELECT store_code, {lat_col}, {lng_col} "
                f"FROM stores WHERE {' AND '.join(clauses)}",
                params,
            ).fetchall()
            return {
                str(row["store_code"]): (float(row[lat_col]), float(row[lng_col]))
                for row in rows
            }

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def _ensure_loaded(self) -> sqlite3.Connection:
        with self._lock:
            if self._connection is not None:
                return self._connection
            if not self._manifest_path.is_file() or not self._snapshot_path.is_file():
                raise StoreCatalogUnavailable(
                    "store catalog snapshot is missing; run the store import command"
                )
            try:
                manifest = json.loads(self._manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise StoreCatalogUnavailable(
                    f"cannot read store catalog manifest: {exc}"
                ) from exc
            if manifest.get("schema_version") != SCHEMA_VERSION:
                raise StoreCatalogUnavailable(
                    "unsupported store catalog schema: "
                    f"{manifest.get('schema_version')!r}"
                )

            connection = sqlite3.connect(":memory:", check_same_thread=False)
            connection.row_factory = sqlite3.Row
            try:
                connection.executescript(_SCHEMA_SQL)
                insert_sql = (
                    f"INSERT INTO stores ({', '.join(_COLUMNS)}) "
                    f"VALUES ({', '.join('?' for _ in _COLUMNS)})"
                )
                batch: list[tuple[Any, ...]] = []
                loaded = 0
                with gzip.open(self._snapshot_path, "rt", encoding="utf-8") as stream:
                    for line_number, line in enumerate(stream, start=1):
                        try:
                            record = json.loads(line)
                            batch.append(_record_to_row(record))
                        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                            raise StoreCatalogUnavailable(
                                f"invalid snapshot row {line_number}: {exc}"
                            ) from exc
                        if len(batch) >= 1000:
                            connection.executemany(insert_sql, batch)
                            loaded += len(batch)
                            batch.clear()
                if batch:
                    connection.executemany(insert_sql, batch)
                    loaded += len(batch)
                expected = int(manifest.get("total_count", -1))
                if expected != loaded:
                    raise StoreCatalogUnavailable(
                        f"snapshot row count mismatch: manifest={expected}, loaded={loaded}"
                    )
            except Exception:
                connection.close()
                raise
            self._manifest = manifest
            self._connection = connection
            return connection


_default_catalog_instance: StoreCatalog | None = None
_default_catalog_lock = threading.Lock()


def default_catalog() -> StoreCatalog:
    global _default_catalog_instance
    with _default_catalog_lock:
        if _default_catalog_instance is None:
            _default_catalog_instance = StoreCatalog()
        return _default_catalog_instance


def materialize_run_context(
    profile: Any,
    selection: Mapping[str, Any],
    *,
    catalog: StoreCatalog | None = None,
) -> dict[str, Any]:
    """Resolve an untrusted run selection into a canonical store snapshot.

    The browser only owns the choice (``brand`` + ``store_code``).  All labels,
    status and coordinates are read back from the packaged catalog so a run
    cannot persist stale or fabricated store metadata.  Keeping this seam here
    also gives location, operations and reports one meaning of "selected store".
    """

    if not isinstance(selection, Mapping):
        raise StoreDataError("store_context must be an object")
    profile_brand = str(getattr(profile, "store_brand", None) or "").strip()
    if not profile_brand:
        raise StoreDataError(
            f"app {getattr(profile, 'app', '<unknown>')!r} does not declare store_catalog"
        )
    try:
        brand = _normalize_brand(selection.get("brand"))
    except ValueError as exc:
        raise StoreDataError(str(exc)) from exc
    if brand != profile_brand:
        raise StoreDataError(
            f"store brand {brand!r} conflicts with app store brand {profile_brand!r}"
        )
    store_code = _required_text(selection.get("store_code"), "store_code")
    source = catalog or default_catalog()
    store = source.resolve(brand, store_code)
    snapshot_id = str(source.manifest.get("snapshot_id") or "").strip()
    if not snapshot_id:
        raise StoreCatalogUnavailable("store catalog manifest has no snapshot_id")

    def coordinate(role: str) -> dict[str, float] | None:
        value = store.coordinate(role)
        if value is None:
            return None
        return {"lng": value.lng, "lat": value.lat}

    return {
        "schema_version": RUN_CONTEXT_SCHEMA_VERSION,
        "catalog_snapshot_id": snapshot_id,
        "identity": store.identity,
        "brand": store.brand,
        "store_code": store.store_code,
        "name": store.name,
        "short_name": store.short_name,
        "address": store.address,
        "market_name": store.market_name,
        "market_code": store.market_code,
        "city_name": store.city_name,
        "national_city_code": store.national_city_code,
        "status": store.status,
        "store_type": store.store_type,
        "coordinates": {
            "source_store": coordinate("source_store"),
            "source_map": coordinate("source_map"),
        },
        "tolerance_m": store.tolerance_m,
    }


def reset_default_catalog() -> None:
    """Reset the process singleton, primarily for tests and snapshot refresh."""

    global _default_catalog_instance
    with _default_catalog_lock:
        if _default_catalog_instance is not None:
            _default_catalog_instance.close()
        _default_catalog_instance = None


def build_snapshot(
    sources: Mapping[str, Path],
    *,
    output_dir: Path = DEFAULT_DATA_DIR,
) -> dict[str, Any]:
    """Validate source workbooks and atomically publish a runtime snapshot."""
    assert_assets_writable("build packaged store catalog", path=output_dir)

    normalized_sources = {
        _normalize_brand(brand): Path(path) for brand, path in sources.items()
    }
    if set(normalized_sources) != VALID_BRANDS:
        raise StoreDataError(
            f"expected exactly {sorted(VALID_BRANDS)}, got {sorted(normalized_sources)}"
        )
    for path in normalized_sources.values():
        if not path.is_file():
            raise StoreDataError(f"source workbook does not exist: {path}")

    source_hashes = {
        brand: _sha256_file(path)
        for brand, path in sorted(normalized_sources.items())
    }
    snapshot_id = hashlib.sha256(
        json.dumps(source_hashes, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = output_dir / DEFAULT_SNAPSHOT_PATH.name
    manifest_path = output_dir / DEFAULT_MANIFEST_PATH.name
    seen: set[tuple[str, str]] = set()
    counts: dict[str, int] = {}
    missing_coordinates = {
        "source_store": 0,
        "source_map": 0,
    }

    temp_fd, temp_name = tempfile.mkstemp(
        prefix=".stores-", suffix=".jsonl.gz", dir=output_dir
    )
    os.close(temp_fd)
    temp_path = Path(temp_name)
    try:
        with temp_path.open("wb") as raw_stream:
            with gzip.GzipFile(
                filename="stores.jsonl",
                mode="wb",
                fileobj=raw_stream,
                mtime=0,
            ) as gzip_stream:
                with io.TextIOWrapper(
                    gzip_stream, encoding="utf-8", newline="\n"
                ) as text_stream:
                    for brand in sorted(normalized_sources):
                        path = normalized_sources[brand]
                        count = 0
                        for record in _iter_workbook_records(path, brand):
                            identity = (brand, str(record["store_code"]))
                            if identity in seen:
                                raise StoreDataError(
                                    "duplicate store identity: "
                                    f"{identity[0]}:{identity[1]}"
                                )
                            seen.add(identity)
                            if record["store_lat"] is None:
                                missing_coordinates["source_store"] += 1
                            if record["map_lat"] is None:
                                missing_coordinates["source_map"] += 1
                            text_stream.write(
                                json.dumps(
                                    record,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                )
                            )
                            text_stream.write("\n")
                            count += 1
                        counts[brand] = count
        os.replace(temp_path, snapshot_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_count": sum(counts.values()),
        "counts": counts,
        "source_sha256": source_hashes,
        "excluded_fields": list(EXCLUDED_HEADERS),
        "missing_coordinates": missing_coordinates,
        "snapshot_sha256": _sha256_file(snapshot_path),
    }
    _atomic_write_json(manifest_path, manifest)
    return manifest


def _iter_workbook_records(path: Path, brand: str) -> Iterable[dict[str, Any]]:
    workbook = load_workbook(path, read_only=True, data_only=True, keep_links=False)
    try:
        if SHEET_NAME not in workbook.sheetnames:
            raise StoreDataError(f"{path.name}: missing sheet {SHEET_NAME!r}")
        sheet = workbook[SHEET_NAME]
        rows = sheet.iter_rows(values_only=True)
        try:
            raw_headers = next(rows)
        except StopIteration as exc:
            raise StoreDataError(f"{path.name}: workbook is empty") from exc
        headers = tuple(_optional_text(value) for value in raw_headers)
        missing = [header for header in HEADERS if header not in headers]
        if missing:
            raise StoreDataError(f"{path.name}: missing headers: {missing}")
        index = {header: headers.index(header) for header in HEADERS}
        for row_number, values in enumerate(rows, start=2):
            if not any(value not in (None, "") for value in values):
                continue

            def cell(header: str) -> Any:
                position = index[header]
                return values[position] if position < len(values) else None

            source_brand = _required_text(cell("品牌"), f"{path.name}:{row_number}:品牌")
            expected_source_brand = SOURCE_BRANDS[brand]
            if source_brand != expected_source_brand:
                raise StoreDataError(
                    f"{path.name}:{row_number}: expected brand "
                    f"{expected_source_brand!r}, got {source_brand!r}"
                )
            store_code = _identifier(
                cell("餐厅编码"), f"{path.name}:{row_number}:餐厅编码", required=True
            )
            store_coordinate = _coordinate(
                cell("门店坐标"), f"{path.name}:{row_number}:门店坐标"
            )
            map_coordinate = _coordinate(
                cell("地图经纬度"), f"{path.name}:{row_number}:地图经纬度"
            )
            yield {
                "brand": brand,
                "source_brand": source_brand,
                "store_code": store_code,
                "name": _required_text(
                    cell("餐厅名称"), f"{path.name}:{row_number}:餐厅名称"
                ),
                "short_name": _optional_text(cell("餐厅简写")),
                "address": _optional_text(cell("餐厅地址")),
                "email": _optional_text(cell("邮箱")),
                "store_coord_raw": store_coordinate[0],
                "store_lng": store_coordinate[1],
                "store_lat": store_coordinate[2],
                "market_name": _optional_text(cell("市场名称")),
                "market_code": _identifier(
                    cell("市场编码"), f"{path.name}:{row_number}:市场编码"
                ),
                "city_name": _optional_text(cell("城市名称")),
                "national_city_code": _identifier(
                    cell("国标编码"), f"{path.name}:{row_number}:国标编码"
                ),
                "legacy_city_code": _identifier(
                    cell("城市编码（老）"),
                    f"{path.name}:{row_number}:城市编码（老）",
                ),
                "status": _optional_text(cell("餐厅状态")),
                "labels": _labels(
                    cell("Label"), f"{path.name}:{row_number}:Label"
                ),
                "store_type": _optional_text(cell("门店类型")),
                "map_coord_raw": map_coordinate[0],
                "map_lng": map_coordinate[1],
                "map_lat": map_coordinate[2],
                "tolerance_m": _integer(
                    cell("容忍距离(米)"),
                    f"{path.name}:{row_number}:容忍距离(米)",
                ),
                "source_file": path.name,
                "source_row": row_number,
            }
    finally:
        workbook.close()


def _normalize_brand(brand: str) -> str:
    normalized = str(brand or "").strip().lower()
    if normalized not in VALID_BRANDS:
        raise ValueError(f"unsupported store brand: {brand!r}")
    return normalized


def _required_text(value: Any, field: str) -> str:
    text = _optional_text(value)
    if not text:
        raise StoreDataError(f"{field} is required")
    return text


def _optional_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _identifier(value: Any, field: str, *, required: bool = False) -> str:
    if value is None or value == "":
        if required:
            raise StoreDataError(f"{field} is required")
        return ""
    if not isinstance(value, str):
        raise StoreDataError(
            f"{field} must be text so leading zeroes are preserved, got "
            f"{type(value).__name__}"
        )
    text = value.strip()
    if required and not text:
        raise StoreDataError(f"{field} is required")
    return text


def _coordinate(value: Any, field: str) -> tuple[str, float | None, float | None]:
    raw = _optional_text(value)
    if not raw:
        return "", None, None
    parts = [part.strip() for part in raw.split(",")]
    if len(parts) != 2:
        raise StoreDataError(f"{field} must use 'lng,lat', got {raw!r}")
    try:
        lng, lat = float(parts[0]), float(parts[1])
    except ValueError as exc:
        raise StoreDataError(f"{field} contains a non-numeric coordinate") from exc
    if not (-180 <= lng <= 180 and -90 <= lat <= 90):
        raise StoreDataError(f"{field} is outside valid coordinate bounds: {raw!r}")
    return raw, lng, lat


def _labels(value: Any, field: str) -> list[str]:
    raw = _optional_text(value)
    if not raw:
        return []
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StoreDataError(f"{field} is not valid JSON") from exc
    if not isinstance(decoded, list) or not all(
        isinstance(item, str) for item in decoded
    ):
        raise StoreDataError(f"{field} must be a JSON string array")
    return decoded


def _integer(value: Any, field: str) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise StoreDataError(f"{field} must be an integer") from exc
    if not number.is_integer():
        raise StoreDataError(f"{field} must be an integer")
    return int(number)


def _record_to_row(record: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        record["brand"],
        record["source_brand"],
        record["store_code"],
        record["name"],
        record["short_name"],
        record["address"],
        record["email"],
        record["store_coord_raw"],
        record["store_lng"],
        record["store_lat"],
        record["market_name"],
        record["market_code"],
        record["city_name"],
        record["national_city_code"],
        record["legacy_city_code"],
        record["status"],
        json.dumps(record["labels"], ensure_ascii=False, separators=(",", ":")),
        record["store_type"],
        record["map_coord_raw"],
        record["map_lng"],
        record["map_lat"],
        record["tolerance_m"],
        record["source_file"],
        record["source_row"],
    )


def _row_to_store(row: sqlite3.Row) -> Store:
    source_store = (
        Coordinate(
            role="source_store",
            lng=float(row["store_lng"]),
            lat=float(row["store_lat"]),
            raw=str(row["store_coord_raw"]),
        )
        if row["store_lng"] is not None and row["store_lat"] is not None
        else None
    )
    source_map = (
        Coordinate(
            role="source_map",
            lng=float(row["map_lng"]),
            lat=float(row["map_lat"]),
            raw=str(row["map_coord_raw"]),
        )
        if row["map_lng"] is not None and row["map_lat"] is not None
        else None
    )
    return Store(
        brand=str(row["brand"]),
        source_brand=str(row["source_brand"]),
        store_code=str(row["store_code"]),
        name=str(row["name"]),
        short_name=str(row["short_name"]),
        address=str(row["address"]),
        email=str(row["email"]),
        market_name=str(row["market_name"]),
        market_code=str(row["market_code"]),
        city_name=str(row["city_name"]),
        national_city_code=str(row["national_city_code"]),
        legacy_city_code=str(row["legacy_city_code"]),
        status=str(row["status"]),
        labels=tuple(json.loads(row["labels_json"])),
        store_type=str(row["store_type"]),
        source_store=source_store,
        source_map=source_map,
        tolerance_m=(
            int(row["tolerance_m"]) if row["tolerance_m"] is not None else None
        ),
        source_file=str(row["source_file"]),
        source_row=int(row["source_row"]),
    )


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    radius_m = 6_371_000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lng2 - lng1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return radius_m * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    assert_assets_writable("write packaged store catalog manifest", path=path)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temp_path = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temp_path, path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build or inspect the store catalog")
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build", help="build a normalized snapshot")
    build.add_argument(
        "--source",
        action="append",
        required=True,
        metavar="BRAND=PATH",
        help="one source workbook per configured brand",
    )
    build.add_argument("--output", type=Path, default=DEFAULT_DATA_DIR)

    search = subparsers.add_parser("search", help="search the packaged snapshot")
    search.add_argument("--brand", required=True, choices=sorted(VALID_BRANDS))
    search.add_argument("--query", default="")
    search.add_argument("--status")
    search.add_argument("--limit", type=int, default=30)

    args = parser.parse_args(argv)
    if args.command == "build":
        sources: dict[str, Path] = {}
        for item in args.source:
            brand, separator, path = str(item).partition("=")
            if not separator or not brand or not path:
                parser.error(f"--source expects BRAND=PATH, got {item!r}")
            sources[brand] = Path(path)
        result = build_snapshot(sources, output_dir=args.output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    result = default_catalog().search(
        args.brand,
        args.query,
        status=args.status,
        limit=args.limit,
    )
    print(json.dumps(result.to_public_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
