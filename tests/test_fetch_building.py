from unittest.mock import patch, MagicMock
import pytest
from scripts.fetch_building import is_lfs_pointer, fetch_ifc_content, compute_sha256


def test_is_lfs_pointer():
    lfs_content = (
        b"version https://git-lfs.github.com/spec/v1\n"
        b"oid sha256:b347a2c8aa8fff6db896a4417a9c50c22ac0ccd7c5cfc22b99b8d29336c606ed\n"
        b"size 2380763\n"
    )
    ifc_content = b"ISO-10303-21;\nHEADER;\nFILE_SCHEMA(('IFC2X3'));"

    assert is_lfs_pointer(lfs_content) is True
    assert is_lfs_pointer(ifc_content) is False


def test_fetch_ifc_content_detects_lfs_and_fetches_media_url():
    lfs_pointer = (
        b"version https://git-lfs.github.com/spec/v1\n"
        b"oid sha256:abc123\n"
        b"size 100\n"
    )
    actual_ifc = b"ISO-10303-21;\nHEADER;\nFILE_SCHEMA(('IFC2X3'));\nEND-ISO-10303-21;"

    def mock_get(url, timeout=30):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        if "raw.githubusercontent.com" in url:
            mock_resp.content = lfs_pointer
        elif "media.githubusercontent.com" in url:
            mock_resp.content = actual_ifc
        else:
            mock_resp.content = b""
        return mock_resp

    with patch("scripts.fetch_building.requests.get", side_effect=mock_get):
        content, resolved_url = fetch_ifc_content()
        assert content == actual_ifc
        assert "media.githubusercontent.com" in resolved_url


def test_fetch_ifc_content_falls_back_to_source_2():
    lfs_pointer = b"version https://git-lfs.github.com/spec/v1\n"
    fallback_ifc = b"ISO-10303-21;\nFALLBACK_SOURCE_2;"

    def mock_get(url, timeout=30):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        # Fail or return LFS on both primary raw and media
        if "buildingsmart-community" in url:
            mock_resp.content = lfs_pointer
        elif "MadsHolten" in url:
            mock_resp.content = fallback_ifc
        else:
            mock_resp.content = b""
        return mock_resp

    with patch("scripts.fetch_building.requests.get", side_effect=mock_get):
        content, resolved_url = fetch_ifc_content()
        assert content == fallback_ifc
        assert "MadsHolten" in resolved_url
