"""Run one real-model task and save a reviewable, redacted transcript."""

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from coding_agent.agent import Agent
from coding_agent.config import Config
from coding_agent.llm_client import LLMConfigurationError, OpenAICompatibleClient


def redact(value: object, api_key: str) -> object:
    if isinstance(value, str):
        return value.replace(api_key, "[REDACTED_API_KEY]")
    if isinstance(value, dict):
        return {key: redact(item, api_key) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, api_key) for item in value]
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="Record one real Coding Agent run")
    parser.add_argument("task", help="Task to run in the selected workspace")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--output", default="examples/real-model-transcript.json")
    args = parser.parse_args()
    config = Config.from_env(args.workspace)
    if not config.api_key:
        raise SystemExit("LLM_API_KEY must be set before recording a real-model demonstration")
    try:
        agent = Agent(config, OpenAICompatibleClient(config))
    except LLMConfigurationError as exc:
        raise SystemExit(str(exc)) from exc
    result = agent.run(args.task)
    transcript = {
        "task": args.task,
        "status": agent.last_status,
        "result": result,
        "operations": agent.execution_log,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(redact(transcript, config.api_key), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote redacted transcript to {output}")


if __name__ == "__main__":
    main()
