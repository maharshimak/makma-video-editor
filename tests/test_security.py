"""Security regression tests.

Everything here protects a property that is invisible when it works and
catastrophic when it breaks: a production instance anyone can sign into, a
media URL that never expires, a storage key that reaches outside the root.
"""
from __future__ import annotations

import time
from urllib.parse import quote

import pytest

from makma.config import Settings, UnsafeConfiguration
from makma.storage.local import LocalStorage, sign_key, verify_key


# --- production fails closed ----------------------------------------------


def test_production_refuses_to_start_with_development_defaults():
    settings = Settings(environment="production")
    with pytest.raises(UnsafeConfiguration) as caught:
        settings.enforce_production_safety()

    message = str(caught.value)
    assert "MAKMA_SECRET_KEY" in message
    assert "MAKMA_DEV_AUTOLOGIN" in message
    # The refusal has to say what to change, not just that it refused.
    assert "Set it to" in message


@pytest.mark.parametrize(
    "override,expected",
    [
        ({"secret_key": "dev-insecure-change-me"}, "MAKMA_SECRET_KEY"),
        ({"secret_key": "short"}, "MAKMA_SECRET_KEY"),
        ({"dev_autologin": True}, "MAKMA_DEV_AUTOLOGIN"),
        ({"debug": True}, "MAKMA_DEBUG"),
        ({"cors_origins": "*"}, "MAKMA_CORS_ORIGINS"),
    ],
)
def test_each_unsafe_default_is_caught_on_its_own(override, expected):
    safe = {
        "environment": "production",
        "secret_key": "k" * 48,
        "dev_autologin": False,
        "debug": False,
        "cors_origins": "https://example.com",
    }
    settings = Settings(**{**safe, **override})
    problems = " ".join(settings.unsafe_settings())
    assert expected in problems


def test_a_correctly_configured_production_instance_starts():
    Settings(
        environment="production", secret_key="k" * 48,
        dev_autologin=False, debug=False,
        cors_origins="https://example.com",
    ).enforce_production_safety()


def test_development_never_refuses_to_start():
    """Local work must not need ceremony; only production fails closed."""
    settings = Settings(environment="development")
    assert settings.unsafe_settings(), "defaults should still be reported"
    settings.enforce_production_safety()


# --- signed media URLs -----------------------------------------------------


def test_a_signature_is_bound_to_one_key():
    expiry, signature = sign_key("proxies/p/a/proxy.mp4")
    assert verify_key("proxies/p/a/proxy.mp4", expiry, signature)
    # Pointing the same signature at another file must fail.
    assert not verify_key("originals/p/a/secret.mp4", expiry, signature)


def test_a_signature_is_bound_to_its_expiry():
    """Extending the deadline in the URL must invalidate it."""
    expiry, signature = sign_key("proxies/p/a/proxy.mp4")
    later = str(int(expiry) + 86_400)
    assert not verify_key("proxies/p/a/proxy.mp4", later, signature)


def test_an_expired_link_stops_working():
    expiry, signature = sign_key("proxies/p/a/proxy.mp4", ttl_seconds=-1)
    assert not verify_key("proxies/p/a/proxy.mp4", expiry, signature)


@pytest.mark.parametrize("signature", ["", "0" * 32, "not-a-signature", None])
def test_a_forged_signature_is_rejected(signature):
    expiry = str(int(time.time()) + 600)
    assert not verify_key("proxies/p/a/proxy.mp4", expiry, signature)


def test_production_links_are_short_lived():
    from makma.storage import local as storage_module

    dev = Settings(environment="development")
    prod = Settings(environment="production", secret_key="k" * 48,
                    dev_autologin=False, debug=False)
    assert storage_module.PRODUCTION_URL_TTL < storage_module.DEFAULT_URL_TTL
    assert prod.is_production and not dev.is_production


# --- storage path safety ---------------------------------------------------

TRAVERSALS = [
    "../secrets.txt",
    "..\\secrets.txt",
    "originals/../../secrets.txt",
    "originals/../../../../../../etc/passwd",
    "./../../secrets.txt",
    "originals/./../../secrets.txt",
    "a/b/../../../secrets.txt",
]


@pytest.mark.parametrize("key", TRAVERSALS)
def test_storage_rejects_traversal(tmp_path, key):
    storage = LocalStorage(tmp_path)
    with pytest.raises(ValueError):
        storage.local_path(key)


@pytest.mark.parametrize(
    "key",
    [
        "/etc/passwd",
        "//etc/passwd",
        "C:/Windows/System32/config/SAM",
        "C:\\Windows\\System32\\config\\SAM",
        "\\\\server\\share\\secret.txt",
    ],
)
def test_storage_rejects_absolute_and_unc_paths(tmp_path, key):
    """An absolute key must never escape the storage root."""
    storage = LocalStorage(tmp_path)
    try:
        resolved = storage.local_path(key)
    except ValueError:
        return                       # refused outright, which is fine
    assert str(resolved).startswith(str(tmp_path.resolve())), (
        f"{key!r} resolved outside the storage root to {resolved}"
    )


def test_a_legitimate_key_still_resolves(tmp_path):
    storage = LocalStorage(tmp_path)
    resolved = storage.local_path("originals/proj_1/asset_2/clip.mp4")
    assert str(resolved).startswith(str(tmp_path.resolve()))
    assert resolved.name == "clip.mp4"


# --- traversal through the HTTP layer --------------------------------------


@pytest.mark.parametrize("raw", TRAVERSALS + ["%2e%2e%2fsecrets.txt",
                                              "%252e%252e%252fsecrets.txt"])
def test_media_route_never_serves_a_file_outside_storage(client, raw):
    """Signed or not, a traversal key must not return file contents."""
    expiry, signature = sign_key(raw)
    response = client.get(
        f"/api/media/{quote(raw, safe='')}?e={expiry}&s={signature}"
    )
    assert response.status_code in (400, 403, 404, 422), (
        f"{raw!r} returned {response.status_code}"
    )


def test_media_route_requires_a_signature(client, uploaded):
    unsigned = uploaded["proxy_url"].split("?")[0]
    assert client.get(unsigned).status_code == 403


def test_one_users_project_is_invisible_to_another(client):
    """Not-yours and not-found must be indistinguishable."""
    from makma.db import SessionLocal
    from makma.models import Project, User

    db = SessionLocal()
    other = User(email="intruder-target@example.com", name="Other")
    db.add(other)
    db.commit()
    theirs = Project(owner_id=other.id, name="Private", document={})
    db.add(theirs)
    db.commit()
    project_id = theirs.id
    db.close()

    assert client.get(f"/api/projects/{project_id}").status_code == 404
    assert client.get(f"/api/projects/{project_id}/assets").status_code == 404
    assert client.post(
        f"/api/projects/{project_id}/ai/chat", json={"message": "hello"}
    ).status_code == 404


# --- configuration is not cwd-dependent ------------------------------------


def test_a_relative_data_dir_is_anchored_to_the_repository():
    """Otherwise where you start the process decides where your media lives.

    `MAKMA_DATA_DIR=./data` resolved against the current working directory
    meant uvicorn started from apps/api used a different database and a
    different storage tree from the one uploads went into. The media was not
    lost; it was in the other install.
    """
    from pathlib import Path

    from makma.config import REPO_ROOT, Settings

    settings_ = Settings(data_dir=Path("./data"))
    assert settings_.data_dir.is_absolute()
    assert settings_.data_dir == (REPO_ROOT / "data").resolve()
    assert str(REPO_ROOT.resolve()) in settings_.database_url.replace("/", "\\") \
        or REPO_ROOT.as_posix() in settings_.database_url


def test_an_absolute_data_dir_is_left_alone(tmp_path):
    from makma.config import Settings

    settings_ = Settings(data_dir=tmp_path)
    assert settings_.data_dir == tmp_path
