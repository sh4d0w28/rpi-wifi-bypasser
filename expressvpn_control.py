#!/usr/bin/env python3

import json
import sys

from rpi_ap_tools.system.expressvpn import connect_auto, connect_region, disconnect, get_status_summary, list_country_groups, list_regions


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        print("Usage: expressvpn_control.py status|regions|countries|connect [smart|<region>]|disconnect")
        return 0
    command = args[0]
    if command == "status":
        print(json.dumps(get_status_summary(), indent=2, sort_keys=True))
        return 0
    if command == "regions":
        print(json.dumps(list_regions(), indent=2, sort_keys=True))
        return 0
    if command == "countries":
        print(json.dumps(list_country_groups(), indent=2, sort_keys=True))
        return 0
    if command == "connect":
        if len(args) < 2 or args[1] == "smart":
            result = connect_auto()
        else:
            result = connect_region(args[1])
        print(result["message"])
        return 0 if result["ok"] else 1
    if command == "disconnect":
        result = disconnect()
        print(result["message"])
        return 0 if result["ok"] else 1
    print(f"Unknown command: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
