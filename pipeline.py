"""
Weather ETL Pipeline — Lahore, Pakistan
========================================
Extracts 90-day historical weather data from the Open-Meteo API,
applies statistical transformations and anomaly detection,
loads the result into SQLite, and writes an analytics report to the log.

Stages: Extract -> Validate -> Transform -> Load -> Analyze
"""

import logging
import sqlite3
import sys
from datetime import datetime
from typing import Optional

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


API_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude=31.5&longitude=74.3"
    "&daily=temperature_2m_max,temperature_2m_min,precipitation_sum"
    "&past_days=90&forecast_days=1"
    "&timezone=Asia/Karachi"
)

DB_PATH = "weather_pipeline.db"
WEATHER_TABLE = "lahore_weather"
RUNS_TABLE = "pipeline_runs"

ROLLING_WINDOW = 7          # days for rolling statistics
PRECIP_THRESHOLD = 0.0      # mm — above this value is classified as a rainy day
ANOMALY_STD_FACTOR = 2.0    # standard deviations to flag a temperature anomaly
TREND_DELTA_C = 0.5         # minimum mean difference (°C) to classify a trend
REQUEST_TIMEOUT = 30        # seconds
MAX_RETRIES = 3
BACKOFF_FACTOR = 1          # seconds between retries (exponential)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("pipeline.log", mode="a", encoding="utf-8"),
    ],
)

logger = logging.getLogger("weather_etl")


def _build_session() -> requests.Session:
    session = requests.Session()
    retry_policy = Retry(
        total=MAX_RETRIES,
        backoff_factor=BACKOFF_FACTOR,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry_policy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def extract(session: requests.Session) -> pd.DataFrame:
    """Fetch raw daily weather data from the Open-Meteo API."""
    logger.info("EXTRACT — requesting data from Open-Meteo API")

    try:
        response = session.get(API_URL, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        logger.error("HTTP error from API: %s", exc)
        raise
    except requests.exceptions.ConnectionError as exc:
        logger.error("Connection failed: %s", exc)
        raise
    except requests.exceptions.Timeout:
        logger.error("Request timed out after %ds", REQUEST_TIMEOUT)
        raise

    payload = response.json()

    required_fields = {"time", "temperature_2m_max", "temperature_2m_min", "precipitation_sum"}
    daily_block = payload.get("daily", {})
    missing_fields = required_fields - set(daily_block.keys())
    if missing_fields:
        raise ValueError(f"API response missing expected fields: {sorted(missing_fields)}")

    df = pd.DataFrame({
        "date": daily_block["time"],
        "temp_max": daily_block["temperature_2m_max"],
        "temp_min": daily_block["temperature_2m_min"],
        "precipitation": daily_block["precipitation_sum"],
    })

    if df.empty:
        raise ValueError("API returned an empty dataset")

    logger.info("EXTRACT — received %d records", len(df))
    return df


def transform(df: pd.DataFrame) -> pd.DataFrame:
    """
    Clean and enrich the raw weather DataFrame.

    Added features:
      - temp_avg            : daily mean temperature
      - temp_range          : diurnal temperature range
      - rolling_7d_avg_temp : 7-day rolling average temperature
      - rolling_7d_precip   : 7-day rolling total precipitation
      - is_rainy            : boolean flag for rain days
      - temp_anomaly        : boolean flag for statistical outliers
      - period_trend        : dataset-level warming/cooling label
      - pipeline_run_ts     : UTC timestamp of this pipeline execution
    """
    logger.info("TRANSFORM — processing %d raw records", len(df))
    df = df.copy()

    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)

    initial_count = len(df)
    df = df.dropna(subset=["temp_max", "temp_min", "precipitation"])
    dropped = initial_count - len(df)
    if dropped > 0:
        logger.warning("TRANSFORM — dropped %d rows with null values", dropped)

    df["temp_avg"] = ((df["temp_max"] + df["temp_min"]) / 2).round(2)
    df["temp_range"] = (df["temp_max"] - df["temp_min"]).round(2)

    rolling_temp = df["temp_avg"].rolling(window=ROLLING_WINDOW, min_periods=ROLLING_WINDOW)
    df["rolling_7d_avg_temp"] = rolling_temp.mean().round(2)
    df["rolling_7d_std_temp"] = rolling_temp.std().round(3)
    df["rolling_7d_precip"] = (
        df["precipitation"].rolling(window=ROLLING_WINDOW, min_periods=ROLLING_WINDOW).sum().round(2)
    )

    df["is_rainy"] = df["precipitation"] > PRECIP_THRESHOLD

    deviation = (df["temp_avg"] - df["rolling_7d_avg_temp"]).abs()
    df["temp_anomaly"] = (deviation > ANOMALY_STD_FACTOR * df["rolling_7d_std_temp"]).fillna(False)

    midpoint = len(df) // 2
    first_half_mean = df["temp_avg"].iloc[:midpoint].mean()
    second_half_mean = df["temp_avg"].iloc[midpoint:].mean()
    delta = second_half_mean - first_half_mean
    if delta > TREND_DELTA_C:
        trend_label = "warming"
    elif delta < -TREND_DELTA_C:
        trend_label = "cooling"
    else:
        trend_label = "stable"
    df["period_trend"] = trend_label

    df["pipeline_run_ts"] = datetime.utcnow().isoformat(timespec="seconds")

    df = df.dropna(subset=["rolling_7d_avg_temp"]).reset_index(drop=True)

    logger.info("TRANSFORM — %d records after enrichment (window warm-up removed)", len(df))
    return df


def load(df: pd.DataFrame) -> int:
    """Persist transformed data to SQLite and record the pipeline run metadata."""
    logger.info("LOAD — writing %d records to '%s'", len(df), DB_PATH)

    run_record = {
        "run_ts": datetime.utcnow().isoformat(timespec="seconds"),
        "records_loaded": len(df),
        "status": "success",
        "error_message": None,
    }

    try:
        with sqlite3.connect(DB_PATH) as conn:
            df.to_sql(WEATHER_TABLE, conn, if_exists="replace", index=False)

            pd.DataFrame([run_record]).to_sql(
                RUNS_TABLE, conn, if_exists="append", index=False
            )
    except sqlite3.Error as exc:
        logger.error("LOAD — database error: %s", exc)
        raise

    logger.info("LOAD — complete")
    return len(df)


def analyze(df: pd.DataFrame) -> dict:
    """
    Compute and log a summary analytics report from the transformed dataset.

    Returns a dict of key metrics suitable for downstream use (dashboards, alerts).
    """
    hottest_row = df.loc[df["temp_max"].idxmax()]
    coldest_row = df.loc[df["temp_min"].idxmin()]
    rainy_days = int(df["is_rainy"].sum())
    anomaly_days = int(df["temp_anomaly"].sum())
    trend = str(df["period_trend"].iloc[-1])

    metrics = {
        "period_start": df["date"].min().date().isoformat(),
        "period_end": df["date"].max().date().isoformat(),
        "total_days": len(df),
        "avg_temp_c": round(float(df["temp_avg"].mean()), 2),
        "max_temp_c": round(float(df["temp_max"].max()), 2),
        "max_temp_date": hottest_row["date"].date().isoformat(),
        "min_temp_c": round(float(df["temp_min"].min()), 2),
        "min_temp_date": coldest_row["date"].date().isoformat(),
        "avg_temp_range_c": round(float(df["temp_range"].mean()), 2),
        "rainy_days": rainy_days,
        "rainy_day_pct": round(rainy_days / len(df) * 100, 1),
        "total_precipitation_mm": round(float(df["precipitation"].sum()), 2),
        "last_7d_avg_temp_c": round(float(df["rolling_7d_avg_temp"].iloc[-1]), 2),
        "last_7d_total_precip_mm": round(float(df["rolling_7d_precip"].iloc[-1]), 2),
        "anomaly_days": anomaly_days,
        "temperature_trend": trend,
    }

    separator = "-" * 55
    logger.info(separator)
    logger.info("ANALYTICS REPORT — Lahore Weather (%s to %s)", metrics["period_start"], metrics["period_end"])
    logger.info(separator)
    logger.info("Total days analyzed        : %d", metrics["total_days"])
    logger.info("Average temperature        : %.2f C", metrics["avg_temp_c"])
    logger.info("Hottest day                : %s (%.1f C)", metrics["max_temp_date"], metrics["max_temp_c"])
    logger.info("Coldest day                : %s (%.1f C)", metrics["min_temp_date"], metrics["min_temp_c"])
    logger.info("Avg diurnal range          : %.2f C", metrics["avg_temp_range_c"])
    logger.info("Rainy days                 : %d / %d (%.1f%%)", rainy_days, len(df), metrics["rainy_day_pct"])
    logger.info("Total precipitation        : %.2f mm", metrics["total_precipitation_mm"])
    logger.info("Last 7-day avg temperature : %.2f C", metrics["last_7d_avg_temp_c"])
    logger.info("Last 7-day precipitation   : %.2f mm", metrics["last_7d_total_precip_mm"])
    logger.info("Temperature anomaly days   : %d", anomaly_days)
    logger.info("Period temperature trend   : %s", trend.upper())
    logger.info(separator)

    return metrics


def run_pipeline() -> Optional[dict]:
    """
    Execute the full ETL pipeline.

    Returns the analytics metrics dict on success.
    Exits with code 1 on unrecoverable failure.
    """
    start_ts = datetime.utcnow()
    logger.info("Pipeline starting at %s UTC", start_ts.strftime("%Y-%m-%d %H:%M:%S"))

    session = _build_session()
    try:
        raw_df = extract(session)
        clean_df = transform(raw_df)
        records_loaded = load(clean_df)
        metrics = analyze(clean_df)

        elapsed = (datetime.utcnow() - start_ts).total_seconds()
        logger.info("Pipeline completed in %.2fs — %d records processed", elapsed, records_loaded)
        return metrics

    except Exception:
        elapsed = (datetime.utcnow() - start_ts).total_seconds()
        logger.exception("Pipeline failed after %.2fs", elapsed)

        try:
            with sqlite3.connect(DB_PATH) as conn:
                pd.DataFrame([{
                    "run_ts": datetime.utcnow().isoformat(timespec="seconds"),
                    "records_loaded": 0,
                    "status": "failed",
                    "error_message": "See pipeline.log for details",
                }]).to_sql(RUNS_TABLE, conn, if_exists="append", index=False)
        except Exception:  # noqa: BLE001
            pass

        sys.exit(1)

    finally:
        session.close()


if __name__ == "__main__":
    run_pipeline()
