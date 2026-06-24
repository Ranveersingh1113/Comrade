"""Embeddings: deterministic checks with a mocked client (no live API call;
the real call is exercised by the 6d smoke)."""
from shared import embeddings


class _FakeEmb:
    def __init__(self, values):
        self.values = values


class _FakeResp:
    def __init__(self, embs):
        self.embeddings = embs


class _FakeModels:
    def __init__(self):
        self.calls = []

    def embed_content(self, model, contents, config):
        self.calls.append((model, contents, config))
        return _FakeResp([_FakeEmb([3.0, 4.0]) for _ in contents])


class _FakeClient:
    def __init__(self):
        self.models = _FakeModels()


def test_l2_normalize_unit_length():
    assert embeddings._l2_normalize([3.0, 4.0]) == [0.6, 0.8]


def test_embed_empty_returns_empty():
    assert embeddings.embed([]) == []


def test_embed_passes_config_and_normalizes(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(embeddings, "_get_client", lambda: fake)

    out = embeddings.embed(["a", "b"], task_type="RETRIEVAL_QUERY")

    assert out == [[0.6, 0.8], [0.6, 0.8]]  # normalized
    model, contents, config = fake.models.calls[0]
    assert model == embeddings.MODEL
    assert contents == ["a", "b"]
    assert config.output_dimensionality == embeddings.DIM
    assert config.task_type == "RETRIEVAL_QUERY"
