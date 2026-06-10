#!/usr/bin/env python3
"""CLI for controlling the iM.Master robot."""

import argparse
import asyncio
import sys

from robot_ble import RobotController, COMMANDS


async def interactive(controller: RobotController):
    """Interactive control mode."""
    if not await controller.connect():
        return

    print("\nCommands: forward, backward, left, right, spin_left, spin_right, stop, quit")
    print("Append duration in seconds: e.g. 'forward 1.5'\n")

    loop = asyncio.get_event_loop()
    while True:
        try:
            line = await loop.run_in_executor(None, lambda: input("robot> "))
        except (EOFError, KeyboardInterrupt):
            break

        parts = line.strip().split()
        if not parts:
            continue
        cmd = parts[0].lower()

        if cmd in ("quit", "exit", "q"):
            break
        if cmd not in COMMANDS:
            print(f"Unknown: {cmd}. Use: {list(COMMANDS.keys())}")
            continue

        duration = float(parts[1]) if len(parts) > 1 else 0
        await controller.send_command(cmd, duration)

    await controller.stop()
    controller.close()


async def one_shot(controller: RobotController, command: str, duration: float):
    """Single command mode."""
    if not await controller.connect():
        return
    await controller.send_command(command, duration)
    controller.close()


def main():
    parser = argparse.ArgumentParser(description="iM.Master Robot BLE Controller")
    parser.add_argument("command", nargs="?", help=f"Command: {list(COMMANDS.keys())}")
    parser.add_argument("-d", "--duration", type=float, default=1.0, help="Duration in seconds (default: 1.0)")
    parser.add_argument("-s", "--speed", type=int, choices=[1, 2, 3], default=3, help="Speed 1-3 (default: 3)")
    parser.add_argument("-t", "--type", default="98", choices=["98", "99"], help="Device type (default: 98)")
    parser.add_argument("--timeout", type=float, default=10.0, help="Discovery timeout (default: 10s)")
    args = parser.parse_args()

    controller = RobotController(speed=args.speed)
    controller.device_type = args.type

    if args.command:
        if args.command not in COMMANDS:
            print(f"Unknown command: {args.command}. Use: {list(COMMANDS.keys())}")
            sys.exit(1)
        asyncio.run(one_shot(controller, args.command, args.duration))
    else:
        asyncio.run(interactive(controller))


if __name__ == "__main__":
    main()
