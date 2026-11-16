"""共用异常。

单独放一个模块，是因为 exfat.py 和 recover.py 都要用它，
放在 recover 里会让 exfat 反向依赖恢复流程。
"""


class Cancelled(RuntimeError):
    """用户主动中止。已恢复的部分照常保留、照常写报告。"""
