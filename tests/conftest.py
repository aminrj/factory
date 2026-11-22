"""Test-wide guard: no test may send a real push. A test that wants one patches orchestrate.notify itself."""

import pytest


@pytest.fixture(autouse=True)
def no_real_push(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(req: object, *a: object, **k: object) -> object:
        url = getattr(req, "full_url", str(req))
        if "ntfy" in url or "/v1/" not in url and not url.startswith("http://127.0.0.1"):
            raise AssertionError(f"a test tried a real network call: {url[:80]}")
        return real(req, *a, **k)

    import urllib.request
    real = urllib.request.urlopen
    monkeypatch.setattr(urllib.request, "urlopen", refuse)


@pytest.fixture(autouse=True)
def single_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """The foreman lease (scripts/lease.py) is off unless a test turns it on: fakes of Gitea do not store variables."""
    monkeypatch.setenv("FACTORY_LEASE", "off")
    monkeypatch.setenv("FACTORY_NIGHT", "off")  # the night shift would act on its own at night; tests turn it on
