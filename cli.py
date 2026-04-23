"""EvoAgent CLI - interactive terminal for testing.

Usage:
    python cli.py                      # default user=test, new session
    python cli.py --user alice         # specify user
    python cli.py --session abc123     # resume session
    python cli.py --plugins websearch  # load plugins
    python cli.py --debug              # show debug output
"""

import argparse
import asyncio
import json
import uuid

from dotenv import load_dotenv

load_dotenv()

# Env must be loaded before these imports — they read settings at module time.
from core.agent import EvoAgent  # noqa: E402
from core.plugins import PLUGIN_CLASSES  # noqa: E402

# ANSI colors
C_RESET = "\033[0m"
C_GREEN = "\033[32m"
C_CYAN = "\033[36m"
C_YELLOW = "\033[33m"
C_DIM = "\033[2m"
C_BOLD = "\033[1m"
C_MAGENTA = "\033[35m"


def parse_args():
    parser = argparse.ArgumentParser(description="EvoAgent CLI")
    parser.add_argument("--user", default="test", help="User ID (default: test)")
    parser.add_argument("--session", default=None, help="Session ID (default: new)")
    parser.add_argument(
        "--plugins",
        nargs="*",
        default=None,
        help="Plugins to load (e.g. websearch fetchurl imagegeneration)",
    )
    parser.add_argument(
        "--all-plugins",
        action="store_true",
        help="Load all available plugins",
    )
    parser.add_argument(
        "--no-plugins",
        action="store_true",
        help="Disable all plugins (override DEFAULT_PLUGINS)",
    )
    parser.add_argument("--model", default=None, help="Model override")
    parser.add_argument("--debug", action="store_true", help="Show debug info")
    return parser.parse_args()


async def run_once(agent: EvoAgent, user_input: str, debug: bool = False):
    """Run one turn of conversation and print results."""
    async for event in agent.run_stream(user_input):
        code = event.get("code")
        content = event.get("content", "")

        if code == 2:
            # Text content — print inline (streaming)
            print(f"{C_GREEN}{content}{C_RESET}", end="", flush=True)

        elif code == 3:
            # Markers
            if content == "stream_start":
                pass
            elif content == "stream_end":
                print()  # newline after streaming
            elif content.startswith("error:"):
                print(f"\n{C_YELLOW}Error: {content}{C_RESET}")

        elif code == 4:
            # Tool call
            try:
                data = json.loads(content)
                name = data.get("name", "?")
                args_str = data.get("arguments", "")
                if debug:
                    print(f"\n  {C_CYAN}[Tool Call] {name}{C_RESET}")
                    # Pretty print args (truncated)
                    try:
                        args = json.loads(args_str)
                        for k, v in args.items():
                            v_str = str(v)
                            if len(v_str) > 120:
                                v_str = v_str[:120] + "..."
                            print(f"    {C_DIM}{k}: {v_str}{C_RESET}")
                    except json.JSONDecodeError:
                        print(f"    {C_DIM}{args_str[:200]}{C_RESET}")
                else:
                    print(f"\n  {C_CYAN}[{name}]{C_RESET}", end="", flush=True)
            except json.JSONDecodeError:
                pass

        elif code == 5:
            # Tool result
            if debug:
                try:
                    data = json.loads(content)
                    result = data.get("result", "")
                    if len(result) > 300:
                        result = result[:300] + "..."
                    print(f"  {C_DIM}→ {result}{C_RESET}")
                except json.JSONDecodeError:
                    pass
            else:
                print(f" {C_DIM}done{C_RESET}", end="", flush=True)


async def main():
    args = parse_args()

    session_id = args.session or uuid.uuid4().hex[:8]
    user_id = args.user

    # Resolve plugin list
    if args.no_plugins:
        plugin_list = []
    elif args.all_plugins:
        plugin_list = list(PLUGIN_CLASSES.keys())
    elif args.plugins is not None:
        plugin_list = args.plugins
    else:
        plugin_list = None  # let agent use DEFAULT_PLUGINS from settings

    print(f"{C_BOLD}EvoAgent CLI{C_RESET}")
    display_plugins = plugin_list if plugin_list is not None else "(default)"
    print(f"{C_DIM}user={user_id}  session={session_id}  plugins={display_plugins or '(none)'}{C_RESET}")
    print(f"{C_DIM}Type 'quit' to exit, 'debug' to toggle debug mode{C_RESET}")
    print(f"{C_DIM}{'─' * 50}{C_RESET}")

    debug = args.debug

    while True:
        try:
            user_input = input(f"\n{C_MAGENTA}> {C_RESET}").strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n{C_DIM}Bye!{C_RESET}")
            break

        if not user_input:
            continue
        if user_input.lower() == "quit":
            print(f"{C_DIM}Bye!{C_RESET}")
            break
        if user_input.lower() == "debug":
            debug = not debug
            print(f"{C_DIM}Debug mode: {'ON' if debug else 'OFF'}{C_RESET}")
            continue

        # Create agent fresh each turn (same as HTTP endpoint)
        agent = EvoAgent(
            user_id=user_id,
            session_id=session_id,
            plugins=plugin_list,
            model=args.model,
        )

        print()
        await run_once(agent, user_input, debug=debug)

        # Wait a moment for async post-session tasks to flush
        await asyncio.sleep(0.5)


def run() -> None:
    """Sync entry point for the `evoagent-cli` console script."""
    asyncio.run(main())


if __name__ == "__main__":
    run()
