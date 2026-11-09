"""Bridge-request helpers shared by live tests."""
import http.client
import json

STAMP = "2026-01-01T00:00:00Z"

def bridge_request(transcript="status"):
    return {"transmission_id": "synthetic-transmission", "transcript": transcript, "speaker": "Prometheus",
            "frequency_hz": 133000000, "modulation": "AM", "received_at": STAMP, "transcribed_at": STAMP}



def wire(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


def post(port, body):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request("POST", "/radio/v1/exchange", body=body, headers={"Content-Type": "application/json"})
        reply = connection.getresponse()
        return reply.status, reply.read()
    finally:
        connection.close()
