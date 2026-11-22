"""The evaluation judge: DeepSeek-V4-Pro through the project's Foundry chat client."""

import time

from deepeval.models import DeepEvalBaseLLM
from deepeval.models.llms.utils import trim_and_load_json
from judge_validity import request_outcome
from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage
from langchain_core.runnables import Runnable
from langchain_openai import ChatOpenAI
from pydantic import BaseModel
from request_log import Attempts, RequestLog, request_row

from dispute_casework.foundry import FoundrySettings, chat_model

JUDGE_DEPLOYMENT = "DeepSeek-V4-Pro"
# Microsoft's client-timeout guidance for reasoning models: up to 29 minutes.
JUDGE_TIMEOUT_SECONDS = 1740
# A retry after a failed TLS handshake usually succeeds (phase 1d diagnostic).
JUDGE_MAX_RETRIES = 5


class Judge(DeepEvalBaseLLM):  # type: ignore[no-untyped-call]
    """A DeepEval model for one test case; each request becomes one row of the log.

    With a schema it asks for JSON mode and parses the answer the way DeepEval's
    own model classes do, so the metric receives the schema instance.
    """

    def __init__(
        self,
        settings: FoundrySettings,
        log: RequestLog,
        case_id: str,
        repeat: int,
        *,
        remeasured: bool = False,
    ) -> None:
        self.log = log
        self.case_id = case_id
        self.repeat = repeat
        self.remeasured = remeasured
        self.attempts = Attempts()
        self.chat = chat_model(
            settings,
            JUDGE_DEPLOYMENT,
            timeout_seconds=JUDGE_TIMEOUT_SECONDS,
            max_retries=JUDGE_MAX_RETRIES,
            http_client=self.attempts.sync_client,
            http_async_client=self.attempts.async_client,
        )
        super().__init__(JUDGE_DEPLOYMENT)

    def load_model(self) -> ChatOpenAI:  # type: ignore[override]
        return self.chat

    def get_model_name(self) -> str:
        return JUDGE_DEPLOYMENT

    def generate(  # type: ignore[override]
        self, prompt: str, schema: type[BaseModel] | None = None
    ) -> BaseModel | str:
        started_at = self._start()
        try:
            message = self.complete(prompt, schema)
        except Exception as error:
            self._record(prompt, schema, started_at, None, error)
            raise
        return self._finish(prompt, schema, started_at, message)

    async def a_generate(  # type: ignore[override]
        self, prompt: str, schema: type[BaseModel] | None = None
    ) -> BaseModel | str:
        started_at = self._start()
        try:
            message = await self.acomplete(prompt, schema)
        except Exception as error:
            self._record(prompt, schema, started_at, None, error)
            raise
        return self._finish(prompt, schema, started_at, message)

    def complete(self, prompt: str, schema: type[BaseModel] | None) -> AIMessage:
        return self._bound(schema).invoke(prompt)

    async def acomplete(self, prompt: str, schema: type[BaseModel] | None) -> AIMessage:
        return await self._bound(schema).ainvoke(prompt)

    def _bound(
        self, schema: type[BaseModel] | None
    ) -> Runnable[LanguageModelInput, AIMessage]:
        if schema is None:
            return self.chat.bind(temperature=0.0)
        return self.chat.bind(temperature=0.0, response_format={"type": "json_object"})

    def _start(self) -> float:
        self.attempts.current = []
        return time.time()

    def _finish(
        self,
        prompt: str,
        schema: type[BaseModel] | None,
        started_at: float,
        message: AIMessage,
    ) -> BaseModel | str:
        content = str(message.text)
        if schema is None:
            self._record(prompt, schema, started_at, message, None)
            return content
        try:
            parsed = schema.model_validate(trim_and_load_json(content))
        except Exception as error:
            self._record(prompt, schema, started_at, message, error)
            raise
        self._record(prompt, schema, started_at, message, None)
        return parsed

    def _record(
        self,
        prompt: str,
        schema: type[BaseModel] | None,
        started_at: float,
        message: AIMessage | None,
        error: BaseException | None,
    ) -> None:
        content = str(message.text) if message else None
        self.log.write(
            request_row(
                case_id=self.case_id,
                repeat=self.repeat,
                remeasured=self.remeasured,
                step=schema.__name__ if schema else None,
                prompt=prompt,
                started_at=started_at,
                attempts=self.attempts.current,
                message=message,
                error=error,
                outcome=request_outcome(schema is not None, content, error),
            )
        )
