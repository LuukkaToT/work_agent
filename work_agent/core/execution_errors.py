"""图运行器与网关共用的执行结果：阻塞需人工处理，可重试可原任务续跑。"""


class ExecutionBlocked(RuntimeError):
    """执行卡住，需要和解、改请求或新开一轮；不能原样重放。"""

    def __init__(self, code: str, message: str) -> None:
        """
        参数:
            code: 稳定错误码，给网关/前端分支用。
            message: 给人看的说明。
        """
        self.code = code
        self.message = message
        super().__init__(message)


class ExecutionRetryable(RuntimeError):
    """同一任务可在不改操作键的前提下续跑（租约过期、瞬时失败等）。"""

    def __init__(self, code: str, message: str) -> None:
        """
        参数:
            code: 稳定错误码。
            message: 给人看的说明。
        """
        self.code = code
        self.message = message
        super().__init__(message)
