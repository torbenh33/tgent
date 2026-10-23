
import asyncio
import configparser
import os
from typing import Any

from openai import OpenAI

class LLMClient:
    def __init__(self, config_file='config.ini'):
        self.config = configparser.ConfigParser()
        if not os.path.exists(config_file):
            raise FileNotFoundError(f"Configuration file not found: {config_file}")
        self.config.read(config_file)

        settings = self.config['settings']
        self.base_url = settings.get('base_url')
        self.api_key = settings.get('api_key')
        self.model = settings.get('model')

        if not all([self.base_url, self.api_key, self.model]):
            raise ValueError("Missing required configuration in config.ini: base_url, api_key, or model.")

        self.client = OpenAI(
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=1800,
        )

    async def model_callback(self, params: dict[str, Any]) -> dict[str, Any]:
        """Create an OpenAI-compatible model response for an agent request."""
        allowed = {"messages", "tools", "tool_choice", "temperature", "top_p", "max_tokens", "stop"}
        unknown = set(params) - allowed
        if unknown:
            raise ValueError(f"Unsupported model request field(s): {', '.join(sorted(unknown))}")
        request = dict(params)
        request["model"] = self.model
        request["reasoning_effort"] = "none"
        response = await asyncio.to_thread(self.client.chat.completions.create, **request)
        if not hasattr(response, "model_dump"):
            raise RuntimeError("OpenAI-compatible client returned an unsupported completion object")
        return response.model_dump(mode="json")

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
