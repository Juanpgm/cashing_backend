"""Tests for secop_service helper functions (no DB required)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.core.exceptions import ExternalServiceError


# ── Helper function tests ──────────────────────────────────────────────────


class TestParseDate:
    def test_parse_iso_datetime(self) -> None:
        from app.services.secop_service import _parse_date

        result = _parse_date("2024-01-15T00:00:00")
        assert result is not None
        assert result.year == 2024
        assert result.month == 1
        assert result.day == 15

    def test_parse_date_only(self) -> None:
        from app.services.secop_service import _parse_date

        result = _parse_date("2024-06-30")
        assert result is not None
        assert result.year == 2024
        assert result.month == 6

    def test_parse_none_returns_none(self) -> None:
        from app.services.secop_service import _parse_date

        assert _parse_date(None) is None

    def test_parse_empty_returns_none(self) -> None:
        from app.services.secop_service import _parse_date

        assert _parse_date("") is None

    def test_parse_invalid_returns_none(self) -> None:
        from app.services.secop_service import _parse_date

        assert _parse_date("not-a-date") is None


class TestParseFloat:
    def test_parse_int_string(self) -> None:
        from app.services.secop_service import _parse_float

        assert _parse_float("1000") == 1000.0

    def test_parse_float_string(self) -> None:
        from app.services.secop_service import _parse_float

        assert _parse_float("3500000.50") == 3500000.50

    def test_parse_none_returns_none(self) -> None:
        from app.services.secop_service import _parse_float

        assert _parse_float(None) is None

    def test_parse_int_value(self) -> None:
        from app.services.secop_service import _parse_float

        assert _parse_float(42) == 42.0

    def test_parse_invalid_returns_none(self) -> None:
        from app.services.secop_service import _parse_float

        assert _parse_float("abc") is None


class TestIsPrestacionServicios:
    def test_matches_exact(self) -> None:
        from app.services.secop_service import _is_prestacion_servicios

        assert _is_prestacion_servicios("Prestación de Servicios") is True

    def test_matches_case_insensitive(self) -> None:
        from app.services.secop_service import _is_prestacion_servicios

        assert _is_prestacion_servicios("PRESTACIÓN DE SERVICIOS") is True

    def test_no_match_other_type(self) -> None:
        from app.services.secop_service import _is_prestacion_servicios

        assert _is_prestacion_servicios("Compraventa") is False

    def test_none_returns_false(self) -> None:
        from app.services.secop_service import _is_prestacion_servicios

        assert _is_prestacion_servicios(None) is False

    def test_empty_returns_false(self) -> None:
        from app.services.secop_service import _is_prestacion_servicios

        assert _is_prestacion_servicios("") is False


class TestIsFresh:
    def test_fresh_timestamp(self) -> None:
        from app.services.secop_service import _is_fresh

        recent = datetime.now(tz=UTC) - timedelta(hours=1)
        assert _is_fresh(recent) is True

    def test_stale_timestamp(self) -> None:
        from app.services.secop_service import _is_fresh

        old = datetime.now(tz=UTC) - timedelta(hours=25)
        assert _is_fresh(old) is False

    def test_naive_datetime_treated_as_utc(self) -> None:
        from app.services.secop_service import _is_fresh

        naive = datetime.now() - timedelta(hours=1)  # no tzinfo
        assert _is_fresh(naive) is True


class TestQuerySocrata:
    @pytest.mark.asyncio
    async def test_returns_list_on_success(self) -> None:
        from app.services.secop_service import _query_socrata

        mock_response = MagicMock()
        mock_response.json.return_value = [{"id_contrato": "ABC"}]
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)

            result = await _query_socrata("jbjy-vk9h", "cedula='12345'")

        assert result == [{"id_contrato": "ABC"}]

    @pytest.mark.asyncio
    async def test_extracts_results_key_if_not_list(self) -> None:
        from app.services.secop_service import _query_socrata

        mock_response = MagicMock()
        mock_response.json.return_value = {"results": [{"id_contrato": "XYZ"}]}
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)

            result = await _query_socrata("jbjy-vk9h", "cedula='12345'")

        assert result == [{"id_contrato": "XYZ"}]

    @pytest.mark.asyncio
    async def test_raises_external_service_error_on_http_error(self) -> None:
        from app.services.secop_service import _query_socrata

        mock_request = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 500

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.side_effect = httpx.HTTPStatusError(
                "error", request=mock_request, response=mock_resp
            )
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)

            with pytest.raises(ExternalServiceError):
                await _query_socrata("jbjy-vk9h", "cedula='12345'")

    @pytest.mark.asyncio
    async def test_raises_external_service_error_on_request_error(self) -> None:
        from app.services.secop_service import _query_socrata

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.side_effect = httpx.RequestError("connection refused")
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)

            with pytest.raises(ExternalServiceError):
                await _query_socrata("jbjy-vk9h", "cedula='12345'")

    @pytest.mark.asyncio
    async def test_retries_on_429_then_succeeds(self) -> None:
        """429 on the first attempt, 200 on the second — retry recovers with no error."""
        from app.services.secop_service import _query_socrata

        mock_request = MagicMock()

        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.raise_for_status.side_effect = httpx.HTTPStatusError(
            "429", request=mock_request, response=resp_429
        )

        resp_200 = MagicMock()
        resp_200.raise_for_status = MagicMock()
        resp_200.json.return_value = [{"id_contrato": "OK"}]

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.side_effect = [resp_429, resp_200]
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)

            with patch("app.services.secop_service.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                result = await _query_socrata("jbjy-vk9h", "cedula='12345'")

        assert result == [{"id_contrato": "OK"}]
        assert mock_client.get.call_count == 2
        mock_sleep.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_exhausts_retries_and_raises_after_bounded_attempts(self) -> None:
        """429/5xx on every attempt: bounded retries, then raises — never hangs."""
        from app.services.secop_service import _query_socrata

        mock_request = MagicMock()
        resp_500 = MagicMock()
        resp_500.status_code = 500
        resp_500.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500", request=mock_request, response=resp_500
        )

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = resp_500
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)

            with (
                patch("app.services.secop_service.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
                pytest.raises(ExternalServiceError),
            ):
                await _query_socrata("jbjy-vk9h", "cedula='12345'")

        # Bounded attempts (max 3): 3 calls total, 2 sleeps in between — no hang.
        assert mock_client.get.call_count == 3
        assert mock_sleep.call_count == 2
