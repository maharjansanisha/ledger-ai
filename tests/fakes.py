"""Test doubles shared by test files (no network)."""

from types import SimpleNamespace

from google.genai import errors


class FakeClient:
    """Returns (or raises) the queued outcomes in order; records every call."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.models = self

    def generate_content(self, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(text=outcome)


def server_error(code=503, status="UNAVAILABLE"):
    return errors.ServerError(code, {"error": {"code": code, "message": "overloaded", "status": status}})


def client_error(code, status):
    return errors.ClientError(code, {"error": {"code": code, "message": "x", "status": status}})
