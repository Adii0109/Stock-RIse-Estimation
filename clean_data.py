#!/usr/bin/env python3
"""
Stock Rise Estimation - data cleaning pipeline (implements the SRS, FR-1 .. FR-10).

Usage
-----
    python clean_data.py --input StockData.csv --outdir output
    python clean_data.py --input StockData.csv --outdir output --config config.json

The raw file is a "merge-csv.com" export: a banner, then 14 tables stacked on top
of each other, each with its own header row.  The script
  1. splits the file into tables,           (FR-1)
  2. fixes types and text,                  (FR-2, FR-7.1)
  3. handles missing values & duplicates,   (FR-3, FR-4)
  4. applies validity rules -> rejects,     (FR-5, FR-8)
  5. flags outliers (never deletes them),   (FR-6)
  6. converts money to a base currency,     (FR-7.3, FR-7.4)
  7. builds the product-by-day panel and the stock-rise target,  (FR-9)
  8. writes cleaned CSVs, rejects, report and log.               (FR-10)

Requires: Python 3.10+, pandas >= 2.0, NumPy >= 1.24.
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import re
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger("cleaning")

# --------------------------------------------------------------------------- #
# Configuration (all thresholds live here, not in the code - NFR-5)
# --------------------------------------------------------------------------- #
DEFAULT_CONFIG = {
    "encoding": "utf-8",
    "analysis_start": "2025-01-01",
    "analysis_end": "2025-12-31",
    "base_currency": "USD",
    # Units of base currency per 1 unit of each currency.  The file contains no
    # exchange rates (assumption A-3): these are PLACEHOLDER annual-average rates
    # and should be replaced with the rates your project agrees on.
    "exchange_rates": {"USD": 1.0, "EUR": 1.13, "TRY": 0.0256},
    "ffill_limit_days": 3,
    "iqr_k": 1.5,
    "winsor_low": 0.01,
    "winsor_high": 0.99,
    "temp_min_c": -50.0,
    "temp_max_c": 60.0,
    "orphan_rate_threshold": 0.01,
    "overdelivery_tolerance": 0.0,
}

# Expected tables: name -> (column list, key columns that must not be null)
SCHEMAS: dict[str, tuple[list[str], list[str]]] = {
    "inventory_daily": (["product_id", "date", "on_hand_qty", "in_transit_qty", "reserved_qty"],
                        ["product_id", "date"]),
    "abc_class": (["abc_id", "product_id", "period_start", "period_end", "abc_class"],
                  ["abc_id", "product_id"]),
    "calendar": (["date", "dow", "week_of_year", "month", "is_weekend"], ["date"]),
    "customers": (["customer_id", "customer_city", "customer_name", "customer_segment"],
                  ["customer_id"]),
    "products": (["product_id", "product_code", "product_name", "category", "uom", "is_active",
                  "pro_currency", "base_price", "base_cost", "holding_cost_rate"], ["product_id"]),
    "product_supplier": (["product_supplier_id", "product_id", "supplier_id", "is_primary",
                          "min_order_qty"], ["product_supplier_id", "product_id", "supplier_id"]),
    "purchase_orders": (["po_line_id", "product_id", "supplier_id", "route_id", "order_date",
                         "receipt_date", "qty_ordered", "qty_received", "unit_cost",
                         "purch_currency"],
                        ["po_line_id", "product_id", "supplier_id", "route_id"]),
    "sales": (["sales_id", "product_id", "customer_id", "channel_id", "order_date", "qty",
               "unit_price", "discount_rate", "sale_currency"],
              ["sales_id", "product_id", "customer_id", "channel_id", "order_date"]),
    "channels": (["channel_id", "channel_name", "fulfillment_type", "ship_mode", "sla_days"],
                 ["channel_id"]),
    "events": (["event_id", "date", "event_name", "event_type", "city", "impact_level"],
               ["event_id", "date"]),
    "suppliers": (["supplier_id", "supplier_name", "supplier_city", "supplier_country",
                   "is_active"], ["supplier_id"]),
    "routes": (["route_id", "supplier_id", "origin_city", "destination_city", "transport_mode",
                "lead_time_mean_days", "lead_time_std_days", "shipment_cost"],
               ["route_id", "supplier_id"]),
    "weather": (["date", "city", "temp_c", "precip_mm", "is_extreme"], ["date", "city"]),
}
HEADER_TO_NAME = {",".join(cols): name for name, (cols, _) in SCHEMAS.items()}

DATE_COLS = {"date", "order_date", "receipt_date", "period_start", "period_end"}
BOOL_COLS = {"is_weekend", "is_active", "is_primary"}
CITY_COLS = {"customer_city", "supplier_city", "origin_city", "destination_city", "city"}
CATEGORY_COLS = {"category", "uom", "abc_class", "channel_name", "transport_mode", "event_type",
                 "pro_currency", "purch_currency", "sale_currency", "customer_segment",
                 "fulfillment_type", "ship_mode", "supplier_country"}


# --------------------------------------------------------------------------- #
# Bookkeeping helpers
# --------------------------------------------------------------------------- #
class Audit:
    """Collects rejected rows and rule results for the report (FR-10.2/10.3)."""

    def __init__(self) -> None:
        self.rejects: list[pd.DataFrame] = []
        self.rules: list[dict] = []

    def rule(self, rule_id: str, table: str, checked: int, violations: int, action: str) -> None:
        self.rules.append({"rule_id": rule_id, "table": table, "rows_checked": int(checked),
                           "violations": int(violations), "action": action})
        level = logging.WARNING if violations else logging.INFO
        log.log(level, "%-8s %-17s checked=%-6d violations=%-5d -> %s",
                rule_id, table, checked, violations, action)

    def reject(self, df: pd.DataFrame, mask: pd.Series, table: str, id_col: str,
               rule_id: str, reason: str) -> pd.DataFrame:
        """Move rows where *mask* is True into the rejects table; return the kept rows."""
        mask = mask.fillna(False).astype(bool)
        if mask.any():
            bad = df.loc[mask]
            self.rejects.append(pd.DataFrame({
                "source_table": table,
                "row_identifier": bad[id_col].astype(str).to_numpy(),
                "rule_id": rule_id,
                "reason": reason,
            }))
        self.rule(rule_id, table, len(df), int(mask.sum()),
                  "rejected" if mask.any() else "none")
        return df.loc[~mask].copy()

    def rejects_frame(self) -> pd.DataFrame:
        if not self.rejects:
            return pd.DataFrame(columns=["source_table", "row_identifier", "rule_id", "reason"])
        return pd.concat(self.rejects, ignore_index=True)


def snake(name: str) -> str:
    return re.sub(r"[^0-9a-z]+", "_", name.strip().lower()).strip("_")


def iqr_flag(s: pd.Series, k: float = 1.5) -> pd.Series:
    """FR-6.1: True where a value lies outside [Q1 - k*IQR, Q3 + k*IQR]."""
    q1, q3 = s.quantile([0.25, 0.75])
    iqr = q3 - q1
    return (s < q1 - k * iqr) | (s > q3 + k * iqr)


# --------------------------------------------------------------------------- #
# FR-1  Ingestion and parsing
# --------------------------------------------------------------------------- #
def split_blocks(path: Path, encoding: str) -> tuple[list[dict], dict]:
    """Split the merged file into blocks.  Works for files with or without banner,
    and with CRLF, LF or mixed line endings (str.splitlines handles all)."""
    with open(path, "r", encoding=encoding, newline="") as fh:   # read-only (NFR-4)
        raw = fh.read().lstrip("\ufeff")                         # drop BOM if present
    all_lines = raw.splitlines()

    blocks: list[dict] = []
    cur: dict | None = None
    skipped = 0
    for line in all_lines:
        line = line.strip()
        if not line or line.startswith("#"):                     # FR-1.2
            skipped += 1
            continue
        first = line.split(",", 1)[0]
        if not re.match(r"^-?\d", first):                        # FR-1.3 header row
            cur = {"header": line, "rows": []}
            blocks.append(cur)
        else:
            if cur is None:
                raise ValueError("Data row found before any header row: %r" % line[:60])
            cur["rows"].append(line)

    stats = {"total_lines": len(all_lines), "skipped_lines": skipped,
             "header_lines": len(blocks), "data_rows": sum(len(b["rows"]) for b in blocks)}
    # FR-1.5 reconciliation: data rows + header lines + banner/blank lines = total lines
    if stats["data_rows"] + stats["header_lines"] + stats["skipped_lines"] != stats["total_lines"]:
        raise ValueError("Line count does not reconcile: %s" % stats)
    return blocks, stats


def blocks_to_frames(blocks: list[dict]) -> dict[str, list[pd.DataFrame]]:
    """FR-1.4: one DataFrame per block, named from its header signature."""
    frames: dict[str, list[pd.DataFrame]] = {}
    for b in blocks:
        name = HEADER_TO_NAME.get(b["header"].replace(" ", ""))
        if name is None:                                         # FR-1.6
            raise ValueError("Unexpected block header: %r" % b["header"])
        text = "\n".join([b["header"]] + b["rows"])
        # keep_default_na=False: only an EMPTY field is missing; the literal text
        # "None"/"NA" is never silently turned into NaN.
        df = pd.read_csv(io.StringIO(text), keep_default_na=False, na_values=[""])
        frames.setdefault(name, []).append(df)
        log.info("block %-17s rows=%d cols=%d", name, len(df), df.shape[1])
    missing = set(SCHEMAS) - set(frames)
    if missing:
        raise ValueError("Missing expected blocks: %s" % sorted(missing))
    return frames


# --------------------------------------------------------------------------- #
# FR-2  Types and text
# --------------------------------------------------------------------------- #
def to_bool(s: pd.Series, col: str) -> pd.Series:
    if s.dtype == bool:
        return s
    m = {"true": True, "false": False, "1": True, "0": False, "yes": True, "no": False}
    out = s.astype("string").str.strip().str.lower().map(m)
    if out.isna().any():
        raise ValueError("Column %s has values that are not boolean" % col)
    return out.astype(bool)


def standardise(df: pd.DataFrame, table: str) -> pd.DataFrame:
    df = df.copy()
    df.columns = [snake(c) for c in df.columns]                  # FR-2.5
    for c in df.columns:
        if pd.api.types.is_string_dtype(df[c]) or df[c].dtype == object:
            df[c] = df[c].astype("string").str.strip()           # FR-2.5
        if c in CITY_COLS:                                       # FR-7.1
            df[c] = df[c].str.lower()
        if c in DATE_COLS:                                       # FR-2.1 strict parse
            df[c] = pd.to_datetime(df[c], format="%Y-%m-%d", errors="raise")
        if c in BOOL_COLS:                                       # FR-2.2
            df[c] = to_bool(df[c], c)
    if table == "weather":
        df["is_extreme"] = df["is_extreme"].astype("int8")       # FR-2.2
    return df


# --------------------------------------------------------------------------- #
# Table-by-table cleaning
# --------------------------------------------------------------------------- #
def drop_exact_and_key_duplicates(df, table, key, audit):
    n = len(df)
    exact = df.duplicated()
    audit.rule("FR-4.1", table, n, int(exact.sum()), "dropped" if exact.any() else "none")
    df = df.loc[~exact].copy()
    dup_key = df.duplicated(key, keep=False)                     # FR-4.2
    if dup_key.any():
        # conflicting duplicates: keep none of them in the clean table
        first_key = key[0]
        df = audit.reject(df, dup_key, table, first_key, "FR-4.2",
                          "conflicting duplicate key %s" % key)
    else:
        audit.rule("FR-4.2", table, len(df), 0, "none")
    return df


def null_keys(df, table, keys, audit):
    mask = df[keys].isna().any(axis=1)                           # FR-3.3
    return audit.reject(df, mask, table, keys[0], "FR-3.3", "null in key column")


def clean_inventory(df, cfg, audit):
    t = "inventory_daily"
    qty = ["on_hand_qty", "in_transit_qty", "reserved_qty"]
    df = null_keys(df, t, ["product_id", "date"], audit)
    df = drop_exact_and_key_duplicates(df, t, ["product_id", "date"], audit)

    # FR-4.4 completeness: one row per product per day; fill short gaps only (FR-3.4)
    days = pd.date_range(df["date"].min(), df["date"].max(), freq="D")
    full = pd.MultiIndex.from_product([sorted(df["product_id"].unique()), days],
                                      names=["product_id", "date"])
    missing = len(full) - len(df)
    audit.rule("FR-4.4", t, len(full), missing, "gap handling" if missing else "none")
    df = df.sort_values(["product_id", "date"]).reset_index(drop=True)
    for c in qty:
        df[c + "_imputed"] = False
    if missing:
        df = df.set_index(["product_id", "date"]).reindex(full).reset_index()
        for c in qty:
            was_na = df[c].isna()
            df[c] = df.groupby("product_id")[c].ffill(limit=cfg["ffill_limit_days"])
            df[c + "_imputed"] = was_na & df[c].notna()          # FR-3.5
        df = df.sort_values(["product_id", "date"]).reset_index(drop=True)
    for c in qty:                                                # keep integer type
        df[c] = df[c].astype("Int64") if df[c].isna().any() else df[c].astype("int64")

    # FR-5.1 negative quantities -> rejects
    neg = (df[qty] < 0).any(axis=1)
    df = audit.reject(df, neg, t, "product_id", "FR-5.1", "negative quantity")
    # FR-5.2 reserved > on_hand -> flag (kept)
    df["reserved_gt_onhand_flag"] = (df["reserved_qty"] > df["on_hand_qty"]).fillna(False)
    audit.rule("FR-5.2", t, len(df), int(df["reserved_gt_onhand_flag"].sum()), "flagged")
    # FR-6.3 stock-outs are real zeros, never missing
    df["stockout_flag"] = df["on_hand_qty"].eq(0)
    audit.rule("FR-6.3", t, len(df), int(df["stockout_flag"].sum()), "labelled as stock-out")
    # FR-6.1 outlier flags (values kept)
    for c in qty:
        df[c + "_outlier"] = iqr_flag(df[c].astype(float), cfg["iqr_k"])
        audit.rule("FR-6.1", t + "." + c, len(df), int(df[c + "_outlier"].sum()), "flagged")
    return df


def clean_abc(frames, audit):
    t = "abc_class"
    a, *rest = frames
    for other in rest:                                           # FR-4.3
        same = a.equals(other)
        audit.rule("FR-4.3", t, len(other), 0 if same else len(other),
                   "duplicate block dropped" if same else "CONFLICT - both kept, review")
        if not same:
            a = pd.concat([a, other], ignore_index=True)
    return a


def clean_abc_rules(df, audit):
    t = "abc_class"
    df = null_keys(df, t, ["abc_id", "product_id"], audit)
    df = drop_exact_and_key_duplicates(df, t, ["abc_id"], audit)
    df = audit.reject(df, ~df["abc_class"].isin(["A", "B", "C"]), t, "abc_id", "FR-5.8",
                      "abc_class not in {A,B,C}")
    df = audit.reject(df, df["period_start"] >= df["period_end"], t, "abc_id", "FR-5.8",
                      "period_start not before period_end")
    d = df.sort_values(["product_id", "period_start"])
    overlap = (d["period_start"] <= d.groupby("product_id")["period_end"].shift())
    df = audit.reject(df, overlap.reindex(df.index), t, "abc_id", "FR-5.8",
                      "overlapping ABC periods for one product")
    return df


def clean_calendar(df, audit):
    t = "calendar"
    df = null_keys(df, t, ["date"], audit)
    df = drop_exact_and_key_duplicates(df, t, ["date"], audit)
    bad = df["is_weekend"] != (df["dow"] >= 5)                   # consistency check
    audit.rule("FR-7.2", t + ".is_weekend", len(df), int(bad.sum()), "reported")
    return df


def clean_products(df, audit):
    t = "products"
    df = null_keys(df, t, ["product_id"], audit)
    df = drop_exact_and_key_duplicates(df, t, ["product_id"], audit)
    bad = (~df["category"].str.match(r"^CAT_\d{2}$", na=False)
           | ~df["product_code"].str.match(r"^PRD-\d{4}$", na=False)
           | ~df["uom"].isin(["EA", "KG"]))
    audit.rule("FR-7.2", t, len(df), int(bad.sum()), "reported")    # S: report only
    df = audit.reject(df, (df["base_price"] <= 0) | (df["base_cost"] <= 0), t, "product_id",
                      "FR-5.3", "base_price/base_cost must be > 0")
    cost_gt = df["base_cost"] > df["base_price"]
    audit.rule("FR-5.4", t, len(df), int(cost_gt.sum()), "reported")
    return df


def clean_sales(df, cfg, audit):
    t = "sales"
    df = null_keys(df, t, SCHEMAS[t][1], audit)
    df = drop_exact_and_key_duplicates(df, t, ["sales_id"], audit)
    df = audit.reject(df, df["qty"] < 0, t, "sales_id", "FR-5.1", "negative qty")
    df = audit.reject(df, ~df["discount_rate"].between(0, 1, inclusive="left"), t, "sales_id",
                      "FR-5.3", "discount_rate outside [0,1)")
    df = audit.reject(df, df["unit_price"] <= 0, t, "sales_id", "FR-5.3", "unit_price <= 0")
    for c in ["qty", "discount_rate"]:                           # FR-6.1
        df[c + "_outlier"] = iqr_flag(df[c], cfg["iqr_k"])
        audit.rule("FR-6.1", t + "." + c, len(df), int(df[c + "_outlier"].sum()), "flagged")
    return df


def clean_purchase_orders(df, cfg, audit):
    t = "purchase_orders"
    df = null_keys(df, t, SCHEMAS[t][1], audit)
    df = drop_exact_and_key_duplicates(df, t, ["po_line_id"], audit)
    df = audit.reject(df, (df[["qty_ordered", "qty_received"]] < 0).any(axis=1), t, "po_line_id",
                      "FR-5.1", "negative quantity")
    df = audit.reject(df, df["unit_cost"] <= 0, t, "po_line_id", "FR-5.3", "unit_cost <= 0")
    df = audit.reject(df, df["receipt_date"] < df["order_date"], t, "po_line_id", "FR-5.5",
                      "receipt_date before order_date")
    over = df["qty_received"] > df["qty_ordered"] * (1 + cfg["overdelivery_tolerance"])
    audit.rule("FR-5.6", t, len(df), int(over.sum()), "reported")
    # FR-5.7 shortfall flag and fill rate (NumPy, safe division)
    ordered = df["qty_ordered"].to_numpy(dtype=float)
    received = df["qty_received"].to_numpy(dtype=float)
    df["shortfall_flag"] = received < ordered
    df["fill_rate"] = np.divide(received, ordered, out=np.full_like(received, np.nan),
                                where=ordered > 0).round(6)
    audit.rule("FR-5.7", t, len(df), int(df["shortfall_flag"].sum()), "flagged (kept)")
    for c in ["qty_ordered", "qty_received", "unit_cost"]:
        df[c + "_outlier"] = iqr_flag(df[c], cfg["iqr_k"])
        audit.rule("FR-6.1", t + "." + c, len(df), int(df[c + "_outlier"].sum()), "flagged")
    return df


def clean_channels(df, audit):
    t = "channels"
    df = null_keys(df, t, ["channel_id"], audit)
    df = drop_exact_and_key_duplicates(df, t, ["channel_id"], audit)
    n_null = int(df["ship_mode"].isna().sum())                   # FR-3.2 structural null
    df["ship_mode"] = df["ship_mode"].fillna("None")
    audit.rule("FR-3.2", t + ".ship_mode", len(df), n_null, "filled with label 'None'")
    return df


def clean_events(df, cfg, audit):
    t = "events"
    df = null_keys(df, t, ["event_id", "date"], audit)
    df = drop_exact_and_key_duplicates(df, t, ["event_id"], audit)
    df["impact_level_outlier"] = iqr_flag(df["impact_level"], cfg["iqr_k"])
    return df


def clean_weather(df, cfg, audit):
    t = "weather"
    df = null_keys(df, t, ["date", "city"], audit)
    df = drop_exact_and_key_duplicates(df, t, ["date", "city"], audit)
    # FR-5.9: sub-zero temperatures are VALID; only physically implausible values flagged
    implausible = ~df["temp_c"].between(cfg["temp_min_c"], cfg["temp_max_c"])
    df = audit.reject(df, implausible, t, "city", "FR-5.9", "temp_c outside plausible bounds")
    df = audit.reject(df, df["precip_mm"] < 0, t, "city", "FR-5.1", "negative precipitation")
    for c in ["temp_c", "precip_mm"]:                            # FR-6.1
        df[c + "_outlier"] = iqr_flag(df[c], cfg["iqr_k"])
        audit.rule("FR-6.1", t + "." + c, len(df), int(df[c + "_outlier"].sum()), "flagged")
    return df


def clean_simple(df, table, audit):
    keys = SCHEMAS[table][1]
    df = null_keys(df, table, keys, audit)
    return drop_exact_and_key_duplicates(df, table, keys[:1], audit)


# --------------------------------------------------------------------------- #
# FR-8  Referential integrity
# --------------------------------------------------------------------------- #
FK_RULES = [
    ("sales", "product_id", "products", "product_id"),
    ("sales", "customer_id", "customers", "customer_id"),
    ("sales", "channel_id", "channels", "channel_id"),
    ("purchase_orders", "product_id", "products", "product_id"),
    ("purchase_orders", "supplier_id", "suppliers", "supplier_id"),
    ("purchase_orders", "route_id", "routes", "route_id"),
    ("product_supplier", "product_id", "products", "product_id"),
    ("product_supplier", "supplier_id", "suppliers", "supplier_id"),
    ("routes", "supplier_id", "suppliers", "supplier_id"),
    ("abc_class", "product_id", "products", "product_id"),
    ("inventory_daily", "product_id", "products", "product_id"),
]
ID_COL = {"sales": "sales_id", "purchase_orders": "po_line_id", "product_supplier":
          "product_supplier_id", "routes": "route_id", "abc_class": "abc_id",
          "inventory_daily": "product_id"}


def check_integrity(T: dict[str, pd.DataFrame], cfg, audit) -> None:
    for child, fk, parent, pk in FK_RULES:
        orphan = ~T[child][fk].isin(T[parent][pk])
        rate = orphan.mean() if len(orphan) else 0.0
        if rate > cfg["orphan_rate_threshold"]:
            raise ValueError("Orphan rate %.2f%% for %s.%s -> %s.%s exceeds threshold"
                             % (rate * 100, child, fk, parent, pk))
        T[child] = audit.reject(T[child], orphan, child, ID_COL[child], "FR-8.1",
                                "orphan %s -> %s.%s" % (fk, parent, pk))


# --------------------------------------------------------------------------- #
# FR-7.3 / 7.4  Currency conversion and net sales
# --------------------------------------------------------------------------- #
def convert(df, amount_col, currency_col, cfg, out_col):
    rates = cfg["exchange_rates"]
    unknown = set(df[currency_col].dropna().unique()) - set(rates)
    if unknown:                                                  # stop, never guess
        raise ValueError("No exchange rate configured for: %s" % sorted(unknown))
    df[out_col] = (df[amount_col] * df[currency_col].map(rates).astype(float)).round(4)


# --------------------------------------------------------------------------- #
# FR-9  Panel and target
# --------------------------------------------------------------------------- #
def build_panel(T, cfg, audit) -> pd.DataFrame:
    start, end = pd.Timestamp(cfg["analysis_start"]), pd.Timestamp(cfg["analysis_end"])
    inv = T["inventory_daily"]
    inv = inv[(inv["date"] >= start) & (inv["date"] <= end)].copy()

    # daily sales per product
    s = T["sales"].copy()
    s["net_value_usd"] = s["net_sales_value_usd"]
    daily_sales = (s.groupby(["product_id", "order_date"])
                    .agg(sales_qty=("qty", "sum"), sales_lines=("sales_id", "count"),
                         sales_net_value_usd=("net_value_usd", "sum"))
                    .reset_index().rename(columns={"order_date": "date"}))
    # daily receipts per product (by receipt_date)
    daily_rec = (T["purchase_orders"].groupby(["product_id", "receipt_date"])
                 .agg(receipts_qty=("qty_received", "sum")).reset_index()
                 .rename(columns={"receipt_date": "date"}))

    p = inv.merge(daily_sales, on=["product_id", "date"], how="left")
    p = p.merge(daily_rec, on=["product_id", "date"], how="left")
    # FR-9.4: no sales / no receipts is a true zero, not a missing value
    for c in ["sales_qty", "sales_lines", "sales_net_value_usd", "receipts_qty"]:
        p[c] = p[c].fillna(0)
    for c in ["sales_qty", "sales_lines", "receipts_qty"]:
        p[c] = p[c].astype("int64")

    # ABC class valid on each date (as-of join on period_start, then check period_end)
    abc = T["abc_class"][["product_id", "period_start", "period_end", "abc_class"]]
    p = p.sort_values("date")
    p = pd.merge_asof(p, abc.sort_values("period_start"), left_on="date", right_on="period_start",
                      by="product_id", direction="backward")
    outside = p["date"] > p["period_end"]
    p.loc[outside, "abc_class"] = np.nan
    p["abc_class"] = p["abc_class"].astype("string").fillna("Unclassified")
    p = p.drop(columns=["period_start", "period_end"])

    # product attributes
    prod = T["products"][["product_id", "product_code", "category", "uom", "pro_currency",
                          "base_price_usd", "base_cost_usd", "holding_cost_rate"]]
    p = p.merge(prod, on="product_id", how="left")

    # calendar trimmed to the analysis window (FR-9.2)
    cal = T["calendar"]
    cal = cal[(cal["date"] >= start) & (cal["date"] <= end)]
    p = p.merge(cal, on="date", how="left")

    # FR-9.5 inbound-city weather and events (destination city of the routes)
    dest = T["routes"]["destination_city"].mode().iloc[0]
    w = T["weather"][T["weather"]["city"] == dest][["date", "temp_c", "precip_mm", "is_extreme"]]
    w = w.rename(columns={"temp_c": "dest_temp_c", "precip_mm": "dest_precip_mm",
                          "is_extreme": "dest_is_extreme"})
    e = (T["events"][T["events"]["city"] == dest].groupby("date")
         .agg(dest_event_count=("event_id", "count"), dest_event_max_impact=("impact_level", "max"))
         .reset_index())
    p = p.merge(w, on="date", how="left").merge(e, on="date", how="left")
    p["dest_event_count"] = p["dest_event_count"].fillna(0).astype("int64")
    p["dest_event_max_impact"] = p["dest_event_max_impact"].fillna(0.0)
    log.info("weather / events joined for destination city '%s'", dest)

    # FR-9.3 target (date-sorted per product)
    p = p.sort_values(["product_id", "date"]).reset_index(drop=True)
    g = p.groupby("product_id", sort=False)
    p["stock_rise"] = g["on_hand_qty"].diff()                    # first day per product = NaN
    p["stock_rise_flag"] = pd.array(np.where(p["stock_rise"].isna(), pd.NA,
                                             (p["stock_rise"] > 0).astype(int)), dtype="Int8")

    # FR-9.6 lag / rolling features use PAST data only (shift first, then roll)
    p["on_hand_lag1"] = g["on_hand_qty"].shift(1)
    p["stock_rise_lag1"] = g["stock_rise"].shift(1)
    p["sales_qty_lag1"] = g["sales_qty"].shift(1)
    p["receipts_qty_lag1"] = g["receipts_qty"].shift(1)
    p["sales_qty_roll7_mean"] = (g["sales_qty"].transform(lambda x: x.shift(1).rolling(7).mean())
                                 .round(4))
    p["on_hand_roll7_mean"] = (g["on_hand_qty"].transform(lambda x: x.shift(1).rolling(7).mean())
                               .round(4))

    # FR-6.2 winsorised copies for modelling (originals untouched)
    for src, dst in [("reserved_qty", "reserved_qty_win"), ("dest_precip_mm", "dest_precip_mm_win")]:
        lo, hi = p[src].quantile([cfg["winsor_low"], cfg["winsor_high"]])
        p[dst] = np.clip(p[src], lo, hi)
    return p


# --------------------------------------------------------------------------- #
# Reporting (FR-10)
# --------------------------------------------------------------------------- #
def write_report(outdir, stats, summary_rows, audit, T, panel, rejects):
    rep = outdir / "reports"
    rep.mkdir(parents=True, exist_ok=True)
    rules = pd.DataFrame(audit.rules)
    rules.to_csv(rep / "data_quality_summary.csv", index=False)
    pd.DataFrame(summary_rows).to_csv(rep / "table_summary.csv", index=False)

    lines = ["# Data-quality report - Stock Rise Estimation", "",
             "## Ingestion (FR-1.5)", "",
             "- total lines in file: %d" % stats["total_lines"],
             "- banner / blank lines skipped: %d" % stats["skipped_lines"],
             "- header lines (blocks): %d" % stats["header_lines"],
             "- data rows: %d" % stats["data_rows"], "",
             "## Tables (rows in -> rows out)", "",
             "| table | rows in | rows out | nulls out | columns |", "|---|---|---|---|---|"]
    for r in summary_rows:
        lines.append("| %s | %d | %d | %d | %d |" % (r["table"], r["rows_in"], r["rows_out"],
                                                    r["nulls_out"], r["columns"]))
    lines += ["", "## Rule results", "", "| rule | table | checked | violations | action |",
              "|---|---|---|---|---|"]
    for r in audit.rules:
        lines.append("| %s | %s | %d | %d | %s |" % (r["rule_id"], r["table"], r["rows_checked"],
                                                      r["violations"], r["action"]))
    lines += ["", "## Output", "",
              "- panel rows: %d, products: %d, days: %d" % (len(panel), panel["product_id"].nunique(),
                                                           panel["date"].nunique()),
              "- stock_rise nulls (first day per product): %d" % panel["stock_rise"].isna().sum(),
              "- rejected rows: %d" % len(rejects), ""]
    (rep / "data_quality_report.md").write_text("\n".join(lines), encoding="utf-8")


def run(input_path: Path, outdir: Path, cfg: dict) -> pd.DataFrame:
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "logs").mkdir(exist_ok=True)
    fh = logging.FileHandler(outdir / "logs" / "cleaning.log", mode="w", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(fh)
    audit = Audit()

    # ---- FR-1 -------------------------------------------------------------
    blocks, stats = split_blocks(input_path, cfg["encoding"])
    log.info("file reconciled: %s", stats)
    frames = blocks_to_frames(blocks)

    # ---- FR-2 / FR-4.3 ----------------------------------------------------
    rows_in = {k: sum(len(f) for f in v) for k, v in frames.items()}
    abc_raw = clean_abc([standardise(f, "abc_class") for f in frames["abc_class"]], audit)
    T = {k: standardise(v[0], k) for k, v in frames.items() if k != "abc_class"}
    T["abc_class"] = abc_raw

    # ---- per-table rules ---------------------------------------------------
    T["inventory_daily"] = clean_inventory(T["inventory_daily"], cfg, audit)
    T["abc_class"] = clean_abc_rules(T["abc_class"], audit)
    T["calendar"] = clean_calendar(T["calendar"], audit)
    T["products"] = clean_products(T["products"], audit)
    T["sales"] = clean_sales(T["sales"], cfg, audit)
    T["purchase_orders"] = clean_purchase_orders(T["purchase_orders"], cfg, audit)
    T["channels"] = clean_channels(T["channels"], audit)
    T["events"] = clean_events(T["events"], cfg, audit)
    T["weather"] = clean_weather(T["weather"], cfg, audit)
    for name in ["customers", "product_supplier", "suppliers", "routes"]:
        T[name] = clean_simple(T[name], name, audit)

    # ---- FR-8 ---------------------------------------------------------------
    check_integrity(T, cfg, audit)

    # ---- FR-7.3 / 7.4 currency ------------------------------------------------
    convert(T["products"], "base_price", "pro_currency", cfg, "base_price_usd")
    convert(T["products"], "base_cost", "pro_currency", cfg, "base_cost_usd")
    convert(T["sales"], "unit_price", "sale_currency", cfg, "unit_price_usd")
    T["sales"]["net_sales_value"] = (T["sales"]["qty"] * T["sales"]["unit_price"]
                                     * (1 - T["sales"]["discount_rate"])).round(4)
    T["sales"]["net_sales_value_usd"] = (T["sales"]["qty"] * T["sales"]["unit_price_usd"]
                                         * (1 - T["sales"]["discount_rate"])).round(4)
    convert(T["purchase_orders"], "unit_cost", "purch_currency", cfg, "unit_cost_usd")
    # shipment_cost has no currency column in the file; it is treated as base currency.
    audit.rule("FR-7.3", "routes.shipment_cost", len(T["routes"]), 0,
               "no currency column in source - assumed base currency")

    # FR-2.4 memory-friendly categories
    for df in T.values():
        for c in df.columns:
            if c in CATEGORY_COLS:
                df[c] = df[c].astype("category")

    # ---- FR-9 -----------------------------------------------------------------
    panel = build_panel(T, cfg, audit)

    # ---- FR-10 outputs ---------------------------------------------------------
    clean_dir = outdir / "clean"
    clean_dir.mkdir(exist_ok=True)
    summary, null_rows = [], []
    for name, df in T.items():
        out = df.copy()
        for c in out.select_dtypes(include="datetime").columns:
            out[c] = out[c].dt.strftime("%Y-%m-%d")
        out.to_csv(clean_dir / ("%s.csv" % name), index=False)
        summary.append({"table": name, "rows_in": rows_in[name], "rows_out": len(df),
                        "nulls_out": int(df.isna().sum().sum()), "columns": df.shape[1],
                        "memory_kb": int(df.memory_usage(deep=True).sum() // 1024)})
        for c in df.columns:                                     # FR-3.1
            null_rows.append({"table": name, "column": c, "dtype": str(df[c].dtype),
                              "nulls": int(df[c].isna().sum()),
                              "null_pct": round(100 * df[c].isna().mean(), 4)})
    pout = panel.copy()
    pout["date"] = pout["date"].dt.strftime("%Y-%m-%d")
    pout.to_csv(outdir / "StockData_cleaned.csv", index=False)
    rejects = audit.rejects_frame()
    (outdir / "rejects").mkdir(exist_ok=True)
    rejects.to_csv(outdir / "rejects" / "rejects.csv", index=False)
    (outdir / "reports").mkdir(parents=True, exist_ok=True)
    pd.DataFrame(null_rows).to_csv(outdir / "reports" / "null_report.csv", index=False)
    write_report(outdir, stats, summary, audit, T, panel, rejects)
    log.info("done: panel %s, rejects %d", panel.shape, len(rejects))
    log.removeHandler(fh)
    fh.close()
    return panel


def main() -> None:
    ap = argparse.ArgumentParser(description="Stock Rise Estimation - data cleaning")
    ap.add_argument("--input", required=True, type=Path, help="raw merged CSV (.csv or .txt)")
    ap.add_argument("--outdir", default=Path("output"), type=Path)
    ap.add_argument("--config", type=Path, help="optional JSON file overriding DEFAULT_CONFIG")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    logging.basicConfig(level=args.log_level.upper(), format="%(levelname)s %(message)s")
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if args.config:
        cfg.update(json.loads(args.config.read_text(encoding="utf-8")))
    run(args.input, args.outdir, cfg)


if __name__ == "__main__":
    main()
