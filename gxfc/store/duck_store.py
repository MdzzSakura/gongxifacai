"""本地 DuckDB 数据存储:采集阶段写入,筛选阶段只读。

快照表(涨跌停池/板块榜/业绩预告等)按周期列(trade_date 或 quarter_end)累积;
日K表按 (代码,日期) 唯一增量追加。中文列名在 SQL 中一律用双引号包裹。
"""
import duckdb


class DuckStore:
    def __init__(self, db_path: str):
        self._con = duckdb.connect(db_path)

    def table_exists(self, table: str) -> bool:
        row = self._con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = ?",
            [table],
        ).fetchone()
        return row[0] > 0

    def close(self) -> None:
        self._con.close()
