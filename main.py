import argparse
import json
from pathlib import Path

from coding_agent import Agent, Config
from coding_agent.agent import DemoModel
from coding_agent.llm_client import LLMConfigurationError, OpenAICompatibleClient


def main() -> None:
    parser = argparse.ArgumentParser(description="Local coding agent")
    parser.add_argument("task", nargs="?", help="Programming task")
    parser.add_argument("--workspace", help="Workspace directory")
    parser.add_argument("--demo", action="store_true", help="Use the offline demo model")
    parser.add_argument("--model")
    parser.add_argument("--max-turns", type=int)
    parser.add_argument("--timeout", type=int)
    parser.add_argument("--log-file")
    args = parser.parse_args()
    task = args.task or input("Task> ").strip()
    config = Config.from_env(args.workspace)
    if args.model or args.max_turns or args.timeout:
        config = Config(
            workspace=config.workspace,
            model=args.model or config.model,
            base_url=config.base_url,
            api_key=config.api_key,
            model_timeout=config.model_timeout,
            model_retries=config.model_retries,
            max_turns=args.max_turns or config.max_turns,
            command_timeout=args.timeout or config.command_timeout,
            max_output_chars=config.max_output_chars,
            max_file_chars=config.max_file_chars,
            max_history_messages=config.max_history_messages,
            max_history_chars=config.max_history_chars,
        )
    if args.demo:
        model = DemoModel()
    else:
        try:
            model = OpenAICompatibleClient(config)
        except LLMConfigurationError as exc:
            parser.error(f"{exc}; set LLM_API_KEY or pass --demo")
    agent = Agent(config, model=model)
    result = agent.run(task)
    print(result)
    if args.log_file:
        Path(args.log_file).write_text(json.dumps({"status": agent.last_status, "operations": agent.execution_log}, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
