import argparse
import json
from pathlib import Path

from coding_agent import Agent, Config
from coding_agent.agent import DemoModel
from coding_agent.llm_client import LLMConfigurationError, OpenAICompatibleClient
from coding_agent.memory import MemoryStore


def main() -> None:
    parser = argparse.ArgumentParser(description="Local coding agent")
    parser.add_argument("task", nargs="?", help="Programming task")
    parser.add_argument("--workspace", help="Workspace directory")
    parser.add_argument("--demo", action="store_true", help="Use the offline demo model")
    parser.add_argument("--model")
    parser.add_argument("--max-turns", type=int)
    parser.add_argument("--timeout", type=int)
    parser.add_argument("--model-timeout", type=int, help="Model API timeout in seconds")
    parser.add_argument("--approval-timeout", type=int, help="High-risk command approval timeout in seconds")
    parser.add_argument("--min-request-interval", type=int, help="Minimum interval between model requests in milliseconds")
    parser.add_argument("--log-file")
    args = parser.parse_args()
    task = args.task or input("Task> ").strip()
    config = Config.from_env(args.workspace)
    config = config.with_overrides(
        model=args.model,
        max_turns=args.max_turns,
        command_timeout=args.timeout,
        model_timeout=args.model_timeout,
        command_approval_timeout=args.approval_timeout,
        model_min_request_interval_ms=args.min_request_interval,
    )
    if args.demo:
        model = DemoModel()
    else:
        try:
            model = OpenAICompatibleClient(config)
        except LLMConfigurationError as exc:
            parser.error(f"{exc}; set LLM_API_KEY or pass --demo")
    memory_store = MemoryStore(config.memory_db)
    try:
        agent = Agent(config, model=model, memory_store=memory_store)
        result = agent.run(task)
        print(result)
        if args.log_file:
            Path(args.log_file).write_text(
                json.dumps(
                    {
                        "status": agent.last_status,
                        "operations": agent.execution_log,
                        "memory": memory_store.status(),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
    finally:
        memory_store.close()


if __name__ == "__main__":
    main()
