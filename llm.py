
import asyncio
import tomllib
from pathlib import Path
from typing import Any

from openai import OpenAI

class LLMClient:
    def __init__(self, config_file='config.toml'):
        self.config_file = Path(config_file)
        if not self.config_file.exists():
            raise FileNotFoundError(f"Configuration file not found: {config_file}")
        with self.config_file.open('rb') as config_stream:
            self.config = tomllib.load(config_stream)

        settings = self.config.get('settings')
        if not isinstance(settings, dict):
            raise ValueError("Configuration must contain a [settings] table.")
        self.base_url = settings.get('base_url')
        self.api_key = settings.get('api_key')
        self.model = settings.get('model')
        self.api = settings.get('api', 'chat.completions')
        if isinstance(self.api, str):
            self.api = self.api.strip().casefold()
        if self.api == 'chat_completions':
            self.api = 'chat.completions'

        if not all([self.base_url, self.api_key, self.model]):
            raise ValueError("Missing required configuration in config.toml: base_url, api_key, or model.")
        if self.api not in {'chat.completions', 'responses'}:
            raise ValueError("Invalid API in config.toml: api must be 'chat.completions' or 'responses'.")

        self.chat_completions_options = self._request_options('chat_completions')
        self.responses_options = self._request_options('responses')
        self.client = OpenAI(
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=1800,
        )

    def _request_options(self, api: str) -> dict[str, Any]:
        options = self.config.get(api, {})
        if not isinstance(options, dict):
            raise ValueError(f"The [{api}] configuration must be a TOML table.")
        reserved = {'model', 'messages', 'input', 'tools', 'tool_choice', 'max_tokens', 'max_output_tokens'}
        conflict = reserved.intersection(options)
        if conflict:
            raise ValueError(f"Request options cannot override reserved field(s): {', '.join(sorted(conflict))}")
        return dict(options)

    async def model_callback(self, params: dict[str, Any]) -> dict[str, Any]:
        """Create a chat-completion-shaped response using the configured API."""
        if self.api == 'responses':
            return await self.responses_callback(params)
        return await self.chat_completions_callback(params)

    @staticmethod
    def _validate_model_params(params: dict[str, Any]) -> None:
        allowed = {"messages", "tools", "tool_choice", "temperature", "top_p", "max_tokens", "stop"}
        unknown = set(params) - allowed
        if unknown:
            raise ValueError(f"Unsupported model request field(s): {', '.join(sorted(unknown))}")

    async def chat_completions_callback(self, params: dict[str, Any]) -> dict[str, Any]:
        """Create a response with the Chat Completions API."""
        self._validate_model_params(params)
        request = dict(self.chat_completions_options)
        request.update(params)
        request["model"] = self.model
        request.setdefault("reasoning_effort", "none")
        response = await asyncio.to_thread(self.client.chat.completions.create, **request)
        if not hasattr(response, "model_dump"):
            raise RuntimeError("OpenAI-compatible client returned an unsupported completion object")
        return response.model_dump(mode="json")

    async def responses_callback(self, params: dict[str, Any]) -> dict[str, Any]:
        """Call the Responses API and adapt its result to the worker chat format."""
        self._validate_model_params(params)
        if params.get("stop") is not None:
            raise ValueError("The Responses API does not support the stop parameter")

        request: dict[str, Any] = dict(self.responses_options)
        request.update({"model": self.model, "input": self._response_input(params.get("messages", []))})
        for key in ("temperature", "top_p"):
            if key in params:
                request[key] = params[key]
        if "max_tokens" in params:
            request["max_output_tokens"] = params["max_tokens"]
        request.setdefault("reasoning", {"effort": "low"})
        if "tools" in params:
            request["tools"] = [self._response_tool(tool) for tool in params["tools"]]
        if "tool_choice" in params:
            request["tool_choice"] = self._response_tool_choice(params["tool_choice"])

        response = await asyncio.to_thread(self.client.responses.create, **request)
        if not hasattr(response, "model_dump"):
            raise RuntimeError("OpenAI-compatible client returned an unsupported response object")
        return self._adapt_response(response.model_dump(mode="json"))

    @staticmethod
    def _response_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert Chat Completions history, including tool exchanges, to input items."""
        items: list[dict[str, Any]] = []
        for message in messages:
            role = message.get("role")
            if role == "tool":
                call_id = message.get("tool_call_id")
                if not call_id:
                    raise ValueError("Tool messages must include tool_call_id for the Responses API")
                items.append({
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": message.get("content", ""),
                })
                continue

            calls = message.get("tool_calls", [])
            legacy_call = message.get("function_call")
            adapted = {key: value for key, value in message.items() if key not in {"tool_calls", "function_call"}}
            if (not calls and not isinstance(legacy_call, dict)) or adapted.get("content") is not None:
                items.append(adapted)
            for call in calls:
                function = call.get("function", {})
                call_id = call.get("id")
                name = function.get("name")
                if not call_id or not name:
                    raise ValueError("Tool calls must include an id and function.name for the Responses API")
                items.append({
                    "type": "function_call",
                    "call_id": call_id,
                    "name": name,
                    "arguments": function.get("arguments", "{}"),
                })
            if isinstance(legacy_call, dict):
                call_id = legacy_call.get("id")
                name = legacy_call.get("name")
                if not call_id or not name:
                    raise ValueError("Function calls must include an id and name for the Responses API")
                items.append({
                    "type": "function_call",
                    "call_id": call_id,
                    "name": name,
                    "arguments": legacy_call.get("arguments", "{}"),
                })
        return items

    @staticmethod
    def _response_tool(tool: dict[str, Any]) -> dict[str, Any]:
        if tool.get("type") != "function" or not isinstance(tool.get("function"), dict):
            raise ValueError("Responses API tools must use Chat Completions function-tool format")
        function = tool["function"]
        adapted = {"type": "function", "name": function["name"]}
        for key in ("description", "parameters", "strict"):
            if key in function:
                adapted[key] = function[key]
        return adapted

    @staticmethod
    def _response_tool_choice(choice: Any) -> Any:
        if isinstance(choice, dict) and choice.get("type") == "function":
            function = choice.get("function")
            if not isinstance(function, dict) or "name" not in function:
                raise ValueError("Function tool_choice must include function.name")
            return {"type": "function", "name": function["name"]}
        return choice

    @staticmethod
    def _adapt_response(response: dict[str, Any]) -> dict[str, Any]:
        if response.get("status") == "failed":
            error = response.get("error") or "unknown error"
            raise RuntimeError(f"Responses API request failed: {error}")

        content: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        refusal = None
        for item in response.get("output", []):
            if item.get("type") == "message":
                for part in item.get("content", []):
                    if part.get("type") == "output_text":
                        content.append(part.get("text", ""))
                    elif part.get("type") == "refusal":
                        refusal = part.get("refusal")
            elif item.get("type") == "function_call":
                tool_calls.append({
                    "id": item.get("call_id") or item.get("id"),
                    "type": "function",
                    "function": {
                        "name": item["name"],
                        "arguments": item.get("arguments", "{}"),
                    },
                })

        message: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(content) if content else None,
        }
        if tool_calls:
            message["tool_calls"] = tool_calls
        if refusal is not None:
            message["refusal"] = refusal
        details = response.get("incomplete_details") or {}
        finish_reason = "length" if response.get("status") == "incomplete" and details.get("reason") == "max_output_tokens" else (
            "tool_calls" if tool_calls else "stop"
        )
        usage = response.get("usage")
        chat_usage = None
        if isinstance(usage, dict):
            prompt_tokens = usage.get("input_tokens", 0)
            completion_tokens = usage.get("output_tokens", 0)
            chat_usage = {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": usage.get("total_tokens", prompt_tokens + completion_tokens),
            }
        result: dict[str, Any] = {
            "id": response.get("id"),
            "object": "chat.completion",
            "created": response.get("created_at", 0),
            "model": response.get("model"),
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        }
        if chat_usage is not None:
            result["usage"] = chat_usage
        return result

    def chat(self, messages):
        """
        Generates a chat completion from the OpenAI API.

        Args:
            messages (list[dict]): A list of message dictionaries, including 'role' and 'content'.

        Returns:
            str: The content of the assistant's response.

        Examples:
            >>> llm = LLMClient()
            >>> messages = [ {"role": "system", "content": "You are a helpful assistant."}, {"role": "user", "content": "What is the capital of France?"} ]
            >>> response = llm.chat(messages)
            >>> print(response)
            The capital of France is **Paris**.
        """
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages
        )
        #return response.choices[0].message.content
        return response

if __name__ == "__main__":
    try:
        llm = LLMClient()

        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "What is the capital of France?"},
            {"role": "assistant", "content": "The capital of France is Paris."},
            {"role": "user", "content": "How many people live there?"}
        ]
        response_content = llm.chat(messages)
        print(response_content)

    except FileNotFoundError as e:
        print(f"Error: {e}")
    except ValueError as e:
        print(f"Configuration Error: {e}")
    except Exception as e:
        print(f"An error occurred during API call: {e}")
