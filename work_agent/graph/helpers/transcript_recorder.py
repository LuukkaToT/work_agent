"""诊断历史记录器：保存模型实际输入、原始响应和工具事件的引用。

大正文进入 transcript 的内容寻址归档，事件只保存引用和组装清单；这样既能
还原模型看到的消息，也不会把每轮的完整日志反复塞进 checkpoint。存储接口
负责身份隔离、脱敏和幂等，本模块只负责把消息协议转换为可回读的数据。

特别注意：读取旧事件只用于审计和上下文回读，不是执行恢复。这里不会根据
历史响应跳过模型或工具调用，也不会把缺失响应猜成执行失败后自动重试。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from langchain_core.messages import BaseMessage, messages_from_dict, messages_to_dict

from work_agent.core.transcript import Transcript, TranscriptError
from work_agent.graph.helpers.deadline import Deadline, DeadlineExceeded, invoke_with_deadline


def message_payload(messages: Sequence[BaseMessage]) -> list[dict[str, Any]]:
    """保留消息协议正文，排除与复原输入无关的服务端响应头。

    工具参数、原文、调用 ID 和用量均保留。整个模型客户端或连接配置从不参与
    序列化；供应商额外返回的 HTTP 头也不需要写进诊断历史。其余敏感字段仍由
    transcript 的脱敏策略处理，因此历史是可审计的脱敏副本，而非秘密备份。
    """
    payload = messages_to_dict(list(messages))
    for item in payload:
        data = item["data"]
        metadata = data.get("response_metadata") or {}
        data["response_metadata"] = {
            key: metadata[key]
            for key in ("model_name", "model", "finish_reason", "token_usage", "usage")
            if key in metadata
        }
    return payload


class TranscriptRecorder:
    """绑定一个 agent 执行流的记录器；不负责工具调度和重试。

    参数:
        transcript: 已经由宿主绑定用户、任务、agent 和本次执行 ID 的历史流。
            模型提供的参数不能改变这些范围。
        deadline: 可选截止。已取消后禁止再写成功的模型响应和工具结果。
    """

    def __init__(self, transcript: Transcript, deadline: Deadline | None = None) -> None:
        self.transcript = transcript
        self.deadline = deadline

    def messages(
        self,
        event_id: str,
        kind: str,
        messages: Sequence[BaseMessage],
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> str:
        """先保存完整消息正文，再发布引用该正文的事件。

        返回:
            消息正文的 artifact ID，可经相同身份范围重新打开。

        异常:
            TranscriptError: 正文或事件落库失败。原文已写但事件未提交时可能留下
                未被引用的正文，不能据此宣称工具执行已完成；重试写入可内容去重。
            DeadlineExceeded: 调查已取消或到期，成功结果不得再写入。
        """
        if self.deadline is not None:
            self.deadline.check()
        ref = self.transcript.put_json(message_payload(messages))
        self.transcript.record(
            event_id,
            kind,
            {
                **dict(metadata or {}),
                "messages_ref": ref.artifact_id,
                "message_count": len(messages),
            },
        )
        return ref.artifact_id

    def failure(self, event_id: str, exc: Exception, *, stage: str) -> None:
        """记录取证或模型调用失败，不把失败文本当成日志证据。

        仅记录异常类型和脱敏后的说明，不保存 traceback 中可能包含的局部变量。
        如果记录失败，存储异常直接向上传播，不能为了保留原异常而吞掉丢历史。
        """
        self.transcript.record(
            event_id,
            "failure",
            {"stage": stage, "error_type": type(exc).__name__, "message": str(exc)},
        )

    def invoke(
        self,
        invocation_id: str,
        model: Any,
        messages: list[BaseMessage],
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> Any:
        """在模型调用前后分别提交记录，保留失败前已经取得的完整历史。

        ``invocation_id`` 由宿主按阶段与序号生成。相同执行流不能重用一次调用的
        ID 发起新的推理；重新执行应分配新的 execution_id，而不是覆盖旧响应。
        支持普通消息与 include_raw=True 的结构化结果，不依赖具体模型供应商。
        """
        self.messages(f"{invocation_id}/input", "model_input", messages, metadata=metadata)
        try:
            result = invoke_with_deadline(model, messages, self.deadline)
        except DeadlineExceeded:
            raise
        except TranscriptError:
            raise
        except Exception as exc:
            self.failure(f"{invocation_id}/failure", exc, stage="model")
            raise

        if self.deadline is not None:
            self.deadline.check()
        if isinstance(result, BaseMessage):
            self.messages(f"{invocation_id}/response", "model_response", [result])
        elif isinstance(result, dict):
            raw = result.get("raw")
            parsed = result.get("parsed")
            parsed = parsed.model_dump(mode="json") if hasattr(parsed, "model_dump") else parsed
            ref = self.transcript.put_json(parsed)
            details = {
                "parsed_ref": ref.artifact_id,
                "parsing_error": str(result.get("parsing_error") or ""),
            }
            self.messages(
                f"{invocation_id}/response", "model_response",
                [raw] if isinstance(raw, BaseMessage) else [], metadata=details,
            )
        else:
            # 某些注入模型直接返回解析对象，也要留下原始可序列化结果，不能只记长度。
            value = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
            ref = self.transcript.put_json(value)
            self.transcript.record(
                f"{invocation_id}/response", "model_response", {"result_ref": ref.artifact_id}
            )
        return result


def load_recorded_messages(transcript: Transcript, event_id: str) -> list[BaseMessage]:
    """回读一份模型输入或原始响应，不执行任何模型或工具。

    参数:
        transcript: 宿主鉴权后绑定的历史流，不能由模型选择其他用户或 agent。
        event_id: 带 messages_ref 的事件 ID，如 ``react/001/input``。

    异常:
        FileNotFoundError: 当前范围内无此事件，或事件不含消息引用。
        TranscriptError: 历史过期、存储不可用等情况，不能降级成空历史。
    """
    event = transcript.get(event_id)
    if event is None or not event.payload.get("messages_ref"):
        raise FileNotFoundError("当前历史流中没有该消息事件")
    return messages_from_dict(transcript.read_json(event.payload["messages_ref"]))
