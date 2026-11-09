"""Controller-owned text smoke. Never opens audio/SRS or claims radio delivery."""
import argparse
from datetime import datetime, timezone
import http.client
import json
import sys
import time
from urllib.parse import urlsplit
import uuid

from integrations.hermes_radio.contract import encode_response, parse_request, strict_json
from integrations.hermes_radio.transcript import Transcript


def smoke(url, question, transcript):
    target = urlsplit(url)
    if (target.scheme != "http" or target.hostname != "127.0.0.1" or target.port is None or
            target.path != "/radio/v1/exchange" or target.query or target.fragment or target.username or target.password):
        raise ValueError("invalid_smoke_endpoint")
    now = datetime.now(timezone.utc).isoformat()
    body = {"transmission_id": "text-smoke-" + uuid.uuid4().hex, "speaker": "Prometheus", "transcript": question,
            "frequency_hz": 133000000, "modulation": "AM", "received_at": now, "transcribed_at": now}
    request = parse_request(json.dumps(body).encode())
    transcript.log(kind="text_smoke_input", request=request, text=question, outcome="not_radio")
    connection = http.client.HTTPConnection(target.hostname, target.port, timeout=12)
    started, first, texts, total_bytes = time.monotonic(), None, [], 0
    try:
        connection.request("POST", target.path, body=json.dumps(body).encode(),
                           headers={"Content-Type": "application/json", "Accept": "application/x-ndjson"})
        reply = connection.getresponse()
        if reply.status != 200 or reply.getheader("Content-Type") != "application/x-ndjson":
            raise ValueError("smoke_transport_failed")
        while True:
            line = reply.readline(65537)
            total_bytes += len(line)
            if not line or total_bytes > 65536:
                raise ValueError("smoke_incomplete")
            if line == b"[DONE]\n":
                break
            part = strict_json(line)
            encode_response(part)
            if part["transmission_id"] != request.transmission_id or len(texts) >= 2:
                raise ValueError("smoke_invalid_response")
            if first is None:
                first = round((time.monotonic()-started)*1000)
            texts.append(part["speech"])
            transcript.log(kind="text_smoke_output", request=request, text=part["speech"], outcome="text_only_not_spoken",
                           timings_ms={"first_sentence": first}, state=part["state"])
        return {"id": request.transmission_id, "question": question, "answer": " ".join(texts),
                "first_sentence_ms": first, "total_ms": round((time.monotonic()-started)*1000), "radio_delivery": False}
    except (OSError, ValueError, http.client.HTTPException):
        transcript.log(kind="text_smoke_error", request=request, outcome="failed_not_radio")
        raise ValueError("text_smoke_failed") from None
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8650/radio/v1/exchange")
    parser.add_argument("--question", action="append", required=True)
    parser.add_argument("--transcript-dir")
    args = parser.parse_args()
    try:
        transcript = Transcript(args.transcript_dir)
        for question in args.question:
            print(json.dumps(smoke(args.url, question, transcript)), flush=True)
    except (ValueError, OSError):
        print("text_smoke_failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
