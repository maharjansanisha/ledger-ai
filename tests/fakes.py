"""Test doubles shared by test files (no network)."""

import re
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


class FakeIndex:
    """In-memory stand-in for a Pinecone integrated-embedding index (rag.set_index).

    search() scores a record by the share of the query's words it contains, so tests
    can steer which chunks come back. `fail` makes every call raise that exception.
    """

    def __init__(self, text_field="text", fail=None):
        self.text_field = text_field
        self.records = {}          # (namespace, _id) -> record
        self.upsert_calls = []
        self.fail = fail

    def _check(self):
        if self.fail is not None:
            raise self.fail

    def upsert_records(self, *, namespace, records):
        self._check()
        self.upsert_calls.append(len(records))
        for record in records:
            self.records[(namespace, record["_id"])] = dict(record)

    def search(self, *, namespace, top_k, inputs, fields):
        self._check()
        words = set(re.findall(r"\w+", inputs["text"].lower()))
        hits = []
        for (ns, record_id), record in self.records.items():
            if ns != namespace:
                continue
            text_words = set(re.findall(r"\w+", record[self.text_field].lower()))
            score = len(words & text_words) / max(len(words), 1)
            hits.append(SimpleNamespace(id=record_id, score=score, fields={f: record.get(f) for f in fields}))
        hits.sort(key=lambda h: h.score, reverse=True)
        return SimpleNamespace(result=SimpleNamespace(hits=hits[:top_k]))

    def delete(self, *, ids, namespace):
        self._check()
        for record_id in ids:
            self.records.pop((namespace, record_id), None)
