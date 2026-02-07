from typing import Any
import pandas as pd

class DataValidator:
    @staticmethod
    def validate_1min_candles(df: pd.DataFrame, datetime_column: str) -> bool:
        # Check if majority of intervals are 1 minute (allowing for small gaps)
        times = pd.to_datetime(df[datetime_column])
        diffs = times.diff().dropna().dt.total_seconds()
        valid = (diffs == 60).sum() / len(diffs) > 0.95  # 95% intervals must be 1 min
        return valid

    @staticmethod
    def validate_required_columns(df: pd.DataFrame, required_columns: list[str]) -> bool:
        return all(col in df.columns for col in required_columns)
