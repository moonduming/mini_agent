import openai


class ErrorCode:
    FORBIDDEN = "forbidden"

    # 未知内部异常
    INTERNAL_ERROR = "internal_error"

    # 整个 Agent 执行超时
    TIMEOUT = "timeout"

    # 模型请求超时
    MODEL_TIMEOUT = "model_timeout"

    # 上下文超过模型最大长度
    CONTEXT_LENGTH_EXCEEDED = "context_length_exceeded"

    # 模型 API 被限流
    RATE_LIMIT = "rate_limit"

    # 模型服务不可用
    MODEL_UNAVAILABLE = "model_unavailable"


class AgentError:
    """统一分析 Agent 执行过程中产生的异常。"""

    def __init__(self, error: Exception):
        self.error = error

        # 默认按照未知内部异常处理
        self.code = ErrorCode.INTERNAL_ERROR
        self.status = "failed"
        self.user_message = "The run failed. Please try again."

        self._classify()

    def _classify(self):
        """根据底层异常类型进行分类。"""

        if isinstance(self.error, PermissionError):
            self.code = ErrorCode.FORBIDDEN
            self.user_message = "You do not have access to this conversation. Start a new conversation."
            return

        # asyncio.timeout() 产生的超时
        if isinstance(self.error, TimeoutError):
            self.code = ErrorCode.TIMEOUT
            self.status = "timeout"
            self.user_message = "The run timed out. Please try again."
            return

        # 模型 API 请求超时
        if isinstance(self.error, openai.APITimeoutError):
            self.code = ErrorCode.MODEL_TIMEOUT
            self.status = "failed"
            self.user_message = "The model timed out. Please try again."
            return

        # 模型 API 限流
        if isinstance(self.error, openai.RateLimitError):
            self.code = ErrorCode.RATE_LIMIT
            self.status = "failed"
            self.user_message = "The model service is busy. Please try again shortly."
            return

        # 模型服务连接异常
        if isinstance(self.error, openai.APIConnectionError):
            self.code = ErrorCode.MODEL_UNAVAILABLE
            self.status = "failed"
            self.user_message = "The model service is temporarily unavailable. Please try again shortly."
            return

        # 400 类错误。
        # 注意：BadRequestError 不一定就是上下文超限，
        # 后续还需要根据 provider 返回的 error code/type 进一步判断。
        if isinstance(self.error, openai.BadRequestError):
            if self._is_context_length_error():
                self.code = ErrorCode.CONTEXT_LENGTH_EXCEEDED
                self.status = "failed"
                self.user_message = "This conversation is too long. Start a new conversation."
                return

    def _is_context_length_error(self) -> bool:
        """判断硅基流动返回的 400 BadRequestError 是否属于上下文长度超限。"""
        message = str(self.error).lower()
        context_error_keywords = (
            "context length",
            "context_length",
            "maximum context",
            "max context",
            "too many tokens",
        )

        return any(keyword in message for keyword in context_error_keywords)
