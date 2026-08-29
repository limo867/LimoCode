import argparse
from dataclasses import replace

from coding_agent.config import Config
from coding_agent.web import serve


def main() -> None:
    parser = argparse.ArgumentParser(description="Local Coding Agent web server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--workspace")
    parser.add_argument("--demo", action="store_true", help="Use demo model for API tasks by default")
    parser.add_argument("--model")
    parser.add_argument("--max-turns", type=int)
    parser.add_argument("--timeout", type=int, help="Command timeout in seconds")
    args = parser.parse_args()
    config = Config.from_env(args.workspace)
    config = replace(
        config,
        model=args.model or config.model,
        max_turns=args.max_turns or config.max_turns,
        command_timeout=args.timeout or config.command_timeout,
    )
    server = serve(config, args.host, args.port, demo=args.demo)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
