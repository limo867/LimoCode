import argparse

from coding_agent.config import Config
from coding_agent.web import serve


def main() -> None:
    parser = argparse.ArgumentParser(description="Local Coding Agent web server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--workspace")
    parser.add_argument("--demo", action="store_true", help="Use demo model for API tasks by default")
    args = parser.parse_args()
    server = serve(Config.from_env(args.workspace), args.host, args.port, demo=args.demo)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
