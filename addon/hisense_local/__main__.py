"""Command line: run | import-session | selfcheck."""
import argparse
import json
import logging
import re
import sys

from . import session as sessionlib
from . import wire

HEX_FRAME = re.compile(r"48537b[0-9a-f]+?0d0a")


def cmd_import(args):
    sess, report = sessionlib.import_capture_files(args.login, args.commands, args.library)
    sessionlib.write_private(args.out, sess.data)
    print(f"session {report['session'][:4]}… written to {args.out} (0600)")
    print(f"frames in commands file: {report['frames_in_file']}, of this session: "
          f"{report['frames_of_session']}")
    print(f"library labels: {report['library_labels']}, imported: {report['imported']}")
    for key in ("missing", "other_session", "not_a_matching_201", "unsupported"):
        if report[key]:
            print(f"{key}: {', '.join(sorted(report[key]))}")
    return 0


def cmd_decode(args):
    """Decode every status block found as hex frames in a text file (a capture log)."""
    with open(args.decode) as fh:
        text = fh.read()
    found = 0
    for match in HEX_FRAME.finditer(text):
        try:
            frame = wire.parse(bytes.fromhex(match.group(0)))
        except (wire.BadFrame, ValueError):
            continue
        block = wire.uart_block(frame.body)
        if block is None:
            continue
        values, ok = wire.uart_values(block)
        found += 1
        where = {v: [i for i, x in enumerate(values) if x == v] for v in args.expect}
        print(f"id={frame.msg_id} values={len(values)} checksum_ok={ok} "
              + " ".join(f"{v}@{pos}" for v, pos in where.items()))
        if args.dump:
            print("  " + " ".join(map(str, values)))
    print(f"{found} status blocks")
    return 0 if found else 1


def main(argv=None):
    parser = argparse.ArgumentParser(prog="hisense_local")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="serve the washer and bridge it to MQTT")
    run.add_argument("--options", default="/data/options.json")
    run.add_argument("--data", default="/data")
    run.add_argument("--tls", default="/data/tls")
    run.add_argument("--share", default="/share/hisense_local")

    imp = sub.add_parser("import-session", help="build a session file from capture files")
    imp.add_argument("--login", required=True)
    imp.add_argument("--commands", required=True)
    imp.add_argument("--library", required=True)
    imp.add_argument("--out", required=True)

    chk = sub.add_parser("selfcheck", help="offline decode, or calibration against the washer")
    chk.add_argument("--decode", metavar="FILE", help="text file holding hex frames")
    chk.add_argument("--expect", type=int, action="append", default=[],
                     help="report where this value sits in each status")
    chk.add_argument("--dump", action="store_true", help="print all decoded values")
    chk.add_argument("--calibrate", action="store_true",
                     help="serve the washer, step through safe settings, write the byte map")
    chk.add_argument("--with-cycle", action="store_true",
                     help="also run START, PAUSE, RESUME, STOP on the quick program (empty drum)")
    chk.add_argument("--options", default="/data/options.json")
    chk.add_argument("--data", default="/data")
    chk.add_argument("--tls", default="/data/tls")
    chk.add_argument("--share", default="/share/hisense_local")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.command == "import-session":
        return cmd_import(args)
    if args.command == "selfcheck" and args.decode:
        return cmd_decode(args)
    from . import app  # imports asyncio server code only when serving
    if args.command == "selfcheck" and args.calibrate:
        return app.calibrate(args)
    if args.command == "run":
        return app.run(args)
    parser.error("selfcheck needs --decode or --calibrate")


if __name__ == "__main__":
    sys.exit(main())
