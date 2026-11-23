"""The evaluation judge: DeepSeek-V4-Pro through the project's Foundry chat client."""

from deepeval.models import DeepEvalBaseLLM
from deepeval.models.llms.utils import trim_and_load_json
from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage
from langchain_core.runnables import Runnable
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from dispute_casework.foundry import FoundrySettings, chat_model

JUDGE_DEPLOYMENT = "DeepSeek-V4-Pro"
# Twice the longest single attempt observed in the judge-selection runs
# (178.7 s), under Microsoft's 29-minute ceiling for reasoning models.
JUDGE_TIMEOUT_SECONDS = 360
JUDGE_MAX_RETRIES = 8


class Judge(DeepEvalBaseLLM):  # type: ignore[no-untyped-call]
    """A DeepEval model on the judge deployment.

    With a schema it asks for JSON mode and parses the answer the way DeepEval's
    own model classes do, so the metric receives the schema instance.
    """

    def __init__(self, settings: FoundrySettings) -> None:
        self.chat = chat_model(
            settings,
            JUDGE_DEPLOYMENT,
            timeout_seconds=JUDGE_TIMEOUT_SECONDS,
            max_retries=JUDGE_MAX_RETRIES,
        )
        super().__init__(JUDGE_DEPLOYMENT)

    def load_model(self) -> ChatOpenAI:  # type: ignore[override]
        return self.chat

    def get_model_name(self) -> str:
        return JUDGE_DEPLOYMENT

    def generate(  # type: ignore[override]
        self, prompt: str, schema: type[BaseModel] | None = None
    ) -> BaseModel | str:
        return parse(self._bound(schema).invoke(prompt), schema)

    async def a_generate(  # type: ignore[override]
        self, prompt: str, schema: type[BaseModel] | None = None
    ) -> BaseModel | str:
        return parse(await self._bound(schema).ainvoke(prompt), schema)

    def _bound(
        self, schema: type[BaseModel] | None
    ) -> Runnable[LanguageModelInput, AIMessage]:
        if schema is None:
            return self.chat.bind(temperature=0.0)
        return self.chat.bind(temperature=0.0, response_format={"type": "json_object"})


def parse(message: AIMessage, schema: type[BaseModel] | None) -> BaseModel | str:
    content = str(message.text)
    if schema is None:
        return content
    return schema.model_validate(trim_and_load_json(content))
