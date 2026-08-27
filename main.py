import argparse

from coding_agent import Agent, Config
from coding_agent.agent import DemoModel
from coding_agent.llm_client import LLMConfigurationError, OpenAICompatibleClient


def main() -> None:
    parser = argparse.ArgumentParser(description="Local coding agent")
    parser.add_argument("task", nargs="?", help="Programming task")
    parser.add_argument("--workspace", help="Workspace directory")
    parser.add_argument("--demo", action="store_true", help="Use the offline demo model")
    args = parser.parse_args()
    task = args.task or input("Task> ").strip()
    config = Config.from_env(args.workspace)
    if args.demo:
        model = DemoModel()
    else:
        try:
            model = OpenAICompatibleClient(config)
        except LLMConfigurationError as exc:
            parser.error(f"{exc}; set LLM_API_KEY or pass --demo")
    print(Agent(config, model=model).run(task))


if __name__ == "__main__":
    main()
