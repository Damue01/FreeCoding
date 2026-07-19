from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

DEFAULT_QUESTION = "请用一句话推荐一个上海适合散步的公园"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send one question to the local Meituan API"
    )
    parser.add_argument("question", nargs="?", default=DEFAULT_QUESTION)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=float, default=140)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = json.dumps(
        {
            "target": "meituan",
            "question": args.question,
            "wait": True,
            "timeout_seconds": min(args.timeout - 5, 600),
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{args.base_url.rstrip('/')}/v1/ask",
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=args.timeout) as response:
            job = json.load(response)
    except urllib.error.HTTPError as exc:
        print(exc.read().decode("utf-8", errors="replace"), file=sys.stderr)
        return 2
    if job.get("question") != args.question:
        print("The API did not preserve the UTF-8 question exactly.", file=sys.stderr)
        return 3
    print(json.dumps(job, ensure_ascii=False, indent=2))
    return 0 if job.get("status") == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
