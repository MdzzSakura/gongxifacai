"""唯一动作层:全部写操作以子进程调用既有 CLI,web 进程永不持有写连接。

收益:复用 CLI 的全部参数校验、幂等与友好报错;子进程结束即释放 DuckDB 写锁。
子进程统一注入 PYTHONIOENCODING=utf-8——Windows 下子进程默认 GBK 输出,
父进程按 UTF-8 解码会乱码。
"""
import os
import subprocess
import sys


def _child_env() -> dict:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def journal_add_argv(db_path: str, code: str, name: str, strategy: str, plan: str,
                     date: str, price: float, shares: int) -> list:
    """开仓命令。注意 --db 是主解析器参数,必须位于子命令 add 之前。"""
    return [sys.executable, "-m", "gxfc.journal", "--db", db_path, "add",
            "--code", code, "--name", name, "--strategy", strategy,
            "--plan", plan, "--date", date,
            "--price", str(price), "--shares", str(shares)]


def journal_close_argv(db_path: str, trade_id: str, date: str, price: float,
                       reason: str, followed: bool, note: str) -> list:
    argv = [sys.executable, "-m", "gxfc.journal", "--db", db_path, "close", trade_id,
            "--date", date, "--price", str(price), "--reason", reason,
            "--followed" if followed else "--broke"]
    if note:
        argv += ["--note", note]
    return argv


def ingest_argv(db_path: str) -> list:
    return [sys.executable, "-m", "gxfc.ingest", "--db", db_path]


def screen_argv(db_path: str) -> list:
    return [sys.executable, "-m", "gxfc.screen", "--db", db_path]


def run_action(argv: list, runner=None) -> tuple:
    """执行一次性写命令,返回 (是否成功, stdout+stderr 合并输出)。

    runner 注入供测试打桩(签名兼容 subprocess.run)。
    """
    run = runner or subprocess.run
    proc = run(argv, capture_output=True, text=True, encoding="utf-8",
               errors="replace", env=_child_env())
    return proc.returncode == 0, ((proc.stdout or "") + (proc.stderr or "")).strip()


def start_stream(argv: list) -> subprocess.Popen:
    """启动长任务(采集/筛选),stdout/stderr 合并行缓冲,供页面实时滚动。"""
    return subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace",
                            bufsize=1, env=_child_env())
