"""``POST /v1/bind``."""

from __future__ import annotations

from typing import Any

import httpx
import pytest


class TestBind:
    async def test_it_returns_the_nearest_catalog_code(self, client: httpx.AsyncClient) -> None:
        response = await client.post("/v1/bind", json={"query": "heart rate"})

        assert response.status_code == 200
        body = response.json()
        assert body["coding"]["code"] == "8867-4"
        assert body["coding"]["system"] == "http://loinc.org"
        assert body["neighbors"][0]["code"] == "8867-4"

    async def test_an_unauthenticated_caller_is_refused(
        self, anon_client: httpx.AsyncClient
    ) -> None:
        assert (await anon_client.post("/v1/bind", json={"query": "heart rate"})).status_code == 401

    async def test_an_unready_index_returns_503(
        self, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fhirbridge.terminology import nn_index

        nn_index.set_index(None)

        def _fail(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("encoder unavailable")

        monkeypatch.setattr(nn_index.TerminologyIndex, "load", _fail)

        response = await client.post("/v1/bind", json={"query": "heart rate"})

        assert response.status_code == 503
