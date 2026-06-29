"""基于 SQLite 的 DataFrame 缓存，避免重复访问 AKShare。

缓存以键值对存储：键是调用方约定的字符串（如 "sector:20260629"），
值是 DataFrame 序列化后的 JSON 文本。同键 set 覆盖旧值。
DataFrame 的 dtype 信息嵌入在 payload 中以便恢复。
"""
import json
import sqlite3
from io import StringIO

import pandas as pd


class DataFrameCache:
    def __init__(self, db_path: str):
        self._db_path = db_path
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS df_cache ("
                "cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            )

    def get(self, key: str):
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT payload FROM df_cache WHERE cache_key = ?", (key,)
            ).fetchone()
        if row is None:
            return None

        data = json.loads(row[0])

        # 新格式：包含 data 和 dtypes
        if isinstance(data, dict) and "data" in data and "dtypes" in data:
            # 构建 dtype 字典，将 'object' 转换为 'str'
            dtype_map = {}
            for col, dtype_str in data["dtypes"].items():
                if dtype_str == "object":
                    dtype_map[col] = "str"
                else:
                    dtype_map[col] = dtype_str

            df = pd.read_json(
                StringIO(json.dumps(data["data"])), orient="split", dtype=dtype_map
            )
        else:
            # 旧格式：直接 DataFrame JSON
            df = pd.read_json(StringIO(row[0]), orient="split")

        return df

    def set(self, key: str, df: pd.DataFrame) -> None:
        payload = json.dumps({
            "data": json.loads(df.to_json(orient="split")),
            "dtypes": {col: str(dtype) for col, dtype in df.dtypes.items()}
        })
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO df_cache (cache_key, payload) VALUES (?, ?)",
                (key, payload),
            )
