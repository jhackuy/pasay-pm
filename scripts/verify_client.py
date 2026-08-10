#!/usr/bin/env python3
"""Independent DEV verification helper for PASay-PM (Hermes orchestration).

Reads API key from the property-management skill config.env, and provides a
thin authenticated HTTP client for GET/POST/PATCH/DELETE against the backend,
plus report queries. This is for Hermes-side verification only — it does not
seed anything.
"""
import json
import subprocess
import sys
import urllib.request
import urllib.error

BASE = "http://localhost:8000/api/v1"
ENV = "/Users/jhackuy/.hermes/skills/productivity/property-management/assets/config.env"


def load_key():
    for line in open(ENV):
        line = line.strip()
        if line.startswith("PROPERTY_API_KEY="):
            return line.split("=", 1)[1]
    raise RuntimeError("PROPERTY_API_KEY not found")


def req(method, path, body=None, key=None):
    key = key or load_key()
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Authorization", f"Bearer {key}")
    r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode())
        except Exception:
            detail = e.read().decode()
        return e.code, detail


def get(path, key=None):
    return req("GET", path, key=key)


def post(path, body, key=None):
    return req("POST", path, body, key)


def patch(path, body, key=None):
    return req("PATCH", path, body, key)


def delete(path, key=None):
    return req("DELETE", path, key=key)


if __name__ == "__main__":
    cmd = sys.argv[1]
    path = sys.argv[2]
    if cmd == "get":
        import pprint
        s, d = get(path)
        pprint.pprint((s, d))
    elif cmd == "post":
        s, d = post(path, json.loads(sys.argv[3]))
        print(s, json.dumps(d))
