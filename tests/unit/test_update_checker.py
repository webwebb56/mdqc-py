from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from mdqc.config.defaults import GITHUB_RELEASES_API
from mdqc.update_checker import UpdateChecker, UpdateInfo, _is_newer, _parse_version


def test_parse_version_strips_prefix() -> None:
    assert _parse_version("v1.2.3") == (1, 2, 3)
    assert _parse_version("0.7.0") == (0, 7, 0)


def test_parse_version_returns_none_on_garbage() -> None:
    assert _parse_version("not-a-version") is None
    assert _parse_version("") is None


def test_is_newer_basic() -> None:
    assert _is_newer("1.0.0", "0.9.0") is True
    assert _is_newer("0.6.1", "0.6.0") is True
    assert _is_newer("0.6.0", "0.6.0") is False
    assert _is_newer("0.5.0", "0.6.0") is False
    assert _is_newer("0.6", "0.6.0") is False
    assert _is_newer("garbage", "0.1.0") is None


@pytest.mark.asyncio
async def test_throttling_only_hits_network_once(tmp_data_dir: Path) -> None:
    state_path = tmp_data_dir / "update_state.json"

    with respx.mock(base_url="https://api.github.com") as mock:
        route = mock.get("/repos/webwebb56/mdqc-py/releases/latest").mock(
            return_value=httpx.Response(
                200,
                json={
                    "tag_name": "v9.9.9",
                    "html_url": "https://github.com/webwebb56/mdqc-py/releases/tag/v9.9.9",
                    "published_at": "2026-01-01T00:00:00Z",
                },
                headers={"Last-Modified": "Wed, 21 Oct 2026 07:28:00 GMT"},
            )
        )

        checker = UpdateChecker(
            current_version="0.1.0",
            state_path=state_path,
            check_interval_s=3600,
        )
        info1 = await checker.check()
        info2 = await checker.check()
        await checker.aclose()

    assert route.call_count == 1
    assert info1 is not None
    assert info1.version == "9.9.9"
    assert info2 is not None
    assert info2.version == "9.9.9"


@pytest.mark.asyncio
async def test_304_returns_cached_info(tmp_data_dir: Path) -> None:
    state_path = tmp_data_dir / "update_state.json"
    state_path.write_text(
        json.dumps(
            {
                "last_check": "2020-01-01T00:00:00+00:00",
                "last_modified_header": "Wed, 21 Oct 2026 07:28:00 GMT",
                "latest_known_version": "9.9.9",
            }
        ),
        encoding="utf-8",
    )

    with respx.mock(base_url="https://api.github.com") as mock:
        mock.get("/repos/webwebb56/mdqc-py/releases/latest").mock(
            return_value=httpx.Response(304)
        )

        checker = UpdateChecker(
            current_version="0.1.0",
            state_path=state_path,
            check_interval_s=0,
        )
        info = await checker.check()
        await checker.aclose()

    assert info is not None
    assert info.version == "9.9.9"


@pytest.mark.asyncio
async def test_200_with_newer_returns_update(tmp_data_dir: Path) -> None:
    state_path = tmp_data_dir / "update_state.json"

    with respx.mock(base_url="https://api.github.com") as mock:
        mock.get("/repos/webwebb56/mdqc-py/releases/latest").mock(
            return_value=httpx.Response(
                200,
                json={
                    "tag_name": "v2.0.0",
                    "html_url": "https://example.com/v2.0.0",
                    "published_at": "2026-01-01T00:00:00Z",
                },
            )
        )

        checker = UpdateChecker(
            current_version="1.0.0",
            state_path=state_path,
        )
        info = await checker.check()
        await checker.aclose()

    assert isinstance(info, UpdateInfo)
    assert info.version == "2.0.0"
    assert info.tag_name == "v2.0.0"
    assert info.release_url == "https://example.com/v2.0.0"


@pytest.mark.asyncio
async def test_200_with_same_or_older_returns_none(tmp_data_dir: Path) -> None:
    state_path = tmp_data_dir / "update_state.json"

    with respx.mock(base_url="https://api.github.com") as mock:
        mock.get("/repos/webwebb56/mdqc-py/releases/latest").mock(
            return_value=httpx.Response(
                200,
                json={
                    "tag_name": "v1.0.0",
                    "html_url": "https://example.com/v1.0.0",
                    "published_at": None,
                },
            )
        )

        checker = UpdateChecker(
            current_version="1.0.0",
            state_path=state_path,
        )
        info = await checker.check()
        await checker.aclose()

    assert info is None


@pytest.mark.asyncio
async def test_if_modified_since_sent_after_first_check(tmp_data_dir: Path) -> None:
    state_path = tmp_data_dir / "update_state.json"
    last_modified = "Wed, 21 Oct 2026 07:28:00 GMT"

    captured_headers: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_headers.append(dict(request.headers))
        return httpx.Response(
            200,
            json={
                "tag_name": "v2.0.0",
                "html_url": "https://example.com/v2.0.0",
                "published_at": "2026-01-01T00:00:00Z",
            },
            headers={"Last-Modified": last_modified},
        )

    with respx.mock(base_url="https://api.github.com") as mock:
        mock.get("/repos/webwebb56/mdqc-py/releases/latest").mock(side_effect=handler)

        checker = UpdateChecker(
            current_version="1.0.0",
            state_path=state_path,
            check_interval_s=0,
        )
        await checker.check()
        await checker.check()
        await checker.aclose()

    assert len(captured_headers) == 2
    assert "if-modified-since" not in {k.lower() for k in captured_headers[0]}
    assert captured_headers[1].get("if-modified-since") == last_modified


@pytest.mark.asyncio
async def test_state_persisted_across_instances(tmp_data_dir: Path) -> None:
    state_path = tmp_data_dir / "update_state.json"

    with respx.mock(base_url="https://api.github.com") as mock:
        route = mock.get("/repos/webwebb56/mdqc-py/releases/latest").mock(
            return_value=httpx.Response(
                200,
                json={
                    "tag_name": "v3.0.0",
                    "html_url": "https://example.com/v3.0.0",
                    "published_at": "2026-01-01T00:00:00Z",
                },
                headers={"Last-Modified": "Wed, 21 Oct 2026 07:28:00 GMT"},
            )
        )

        checker1 = UpdateChecker(
            current_version="1.0.0",
            state_path=state_path,
            check_interval_s=3600,
        )
        info = await checker1.check()
        await checker1.aclose()
        assert info is not None
        assert info.version == "3.0.0"

        checker2 = UpdateChecker(
            current_version="1.0.0",
            state_path=state_path,
            check_interval_s=3600,
        )
        info2 = await checker2.check()
        await checker2.aclose()

    assert info2 is not None
    assert info2.version == "3.0.0"
    assert route.call_count == 1


_ = GITHUB_RELEASES_API
