"""The compose secrets mount + container-visible key-path contract (t_adfb7881).

The failure class this gates: PR #35 shipped RS256 key families, but the deploy
had no volume to carry the PEMs into the api container and `.env` left the four
key paths empty — so the live api ran on EPHEMERAL in-process keypairs and every
restart (autoheal included) invalidated every access and session token.

Three things must stay true, and each pin here breaks if one drifts:

1. `docker-compose.yml` mounts the host `./secrets` dir READ-ONLY into the api
   service at `/run/secrets/jwt` — and only into the api service (mcp must
   never hold key material). The build context is `./backend`, so the mount
   source cannot leak into the image, and no Dockerfile COPY references it.
2. `.env.example` + docs/01 + docs/38 document the four key paths as
   CONTAINER-visible files under that same mount target — the api process
   cannot see host paths, so the docs and the compose file must agree.
3. A deployment that CONFIGURES paths whose files are unreadable inside the
   container (a bad or missing mount) hard-fails: `_load_family` raises, the
   boot preload (`preload_signing_keys`, called from `lifespan`) refuses to
   start, and NOTHING falls back to an ephemeral keypair — a silent fallback
   is the exact bug this provisioning eliminates.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.core.config import get_settings
from app.core.jwt_keys import clear_key_cache, get_family_keys, preload_signing_keys

REPO_ROOT = Path(__file__).resolve().parents[2]
MOUNT_SOURCE = "./secrets"
MOUNT_TARGET = "/run/secrets/jwt"
_LEGACY_SECRET = "t-adfb7881-legacy-secret-0123456789abcd"


def _compose_service_block(service: str) -> str:
    """The lines of docker-compose.yml belonging to one service (indent-scoped)."""
    lines = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8").splitlines()
    block: list[str] = []
    in_block = False
    for line in lines:
        if in_block and (re.match(r"^  \S", line) or re.match(r"^\S", line)):
            break  # next 2-space key = next service; 0-space key = top-level section
        if in_block:
            block.append(line)
        elif re.match(rf"^  {service}:\s*$", line):
            in_block = True
    assert block, f"service {service!r} not found in docker-compose.yml"
    return "\n".join(block)


# --- 1. the compose mount ----------------------------------------------------


def test_api_mounts_the_secrets_dir_read_only() -> None:
    block = _compose_service_block("api")
    mount_line = f"- {MOUNT_SOURCE}:{MOUNT_TARGET}:ro"
    assert mount_line in block, (
        f"the api service must mount the signing-key dir: `{mount_line}` — without "
        "it the JWT_*_KEY_PATH files never reach the container and the api falls "
        "back to ephemeral keys that die on every restart (t_adfb7881)"
    )
    assert "build: ./backend" in block, (
        "the api build context is ./backend, deliberately OUTSIDE the mount "
        "source — if the context ever moves to the repo root, key material would "
        "enter the docker build context"
    )


def test_only_the_api_service_mounts_the_secrets_dir() -> None:
    for service in ("db", "web", "mcp", "site"):
        block = _compose_service_block(service)
        assert MOUNT_TARGET not in block, f"the {service} service must never mount {MOUNT_TARGET}"
        assert MOUNT_SOURCE not in block, f"the {service} service must never mount {MOUNT_SOURCE}"


def test_key_material_stays_out_of_git_and_out_of_the_image() -> None:
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert re.search(r"^secrets/", gitignore, re.MULTILINE), ".gitignore must ignore secrets/"
    assert re.search(r"^\*_private\.pem$", gitignore, re.MULTILINE), (
        ".gitignore must ignore *_private.pem"
    )
    dockerfile = (REPO_ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")
    assert not re.search(r"^COPY\s+.*secret", dockerfile, re.IGNORECASE | re.MULTILINE), (
        "the api image must never COPY key material — keys arrive only via the "
        "read-only runtime mount"
    )


# --- 2. the documented container-visible path convention ---------------------


def test_env_example_documents_the_container_visible_paths() -> None:
    env_text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    for key in (
        "JWT_ACCESS_PRIVATE_KEY_PATH",
        "JWT_ACCESS_PUBLIC_KEY_PATH",
        "JWT_SESSION_PRIVATE_KEY_PATH",
        "JWT_SESSION_PUBLIC_KEY_PATH",
    ):
        documented = re.search(rf"^#\s+{key}=(\S+)$", env_text, re.MULTILINE)
        assert documented, f".env.example no longer documents a container path for {key}"
        assert documented.group(1).startswith(f"{MOUNT_TARGET}/"), (
            f"{key}'s documented path {documented.group(1)} must live under the "
            f"compose mount target {MOUNT_TARGET} — the api process cannot see "
            "host paths"
        )
        live = re.search(rf"^{key}=(.*)$", env_text, re.MULTILINE)
        assert live is not None, f".env.example lost the live {key} line"
        assert live.group(1) == "", (
            f"{key} must ship EMPTY in .env.example (the dev default is the "
            "ephemeral keypair; a deployment sets the container path)"
        )


def test_docs_name_the_mount_and_the_provisioning_step() -> None:
    doc38 = (REPO_ROOT / "docs" / "38-auth-hardening.md").read_text(encoding="utf-8")
    assert MOUNT_TARGET in doc38, "docs/38 must document the compose mount target"
    assert "generate_jwt_keys.py" in doc38, "docs/38 must name the provisioning script"
    doc01 = (REPO_ROOT / "docs" / "01-architecture.md").read_text(encoding="utf-8")
    assert MOUNT_TARGET in doc01, "docs/01 must document the compose mount target"


# --- 3. the hard-fail probe: bad mount breaks the gate, not production -------


def _apply_key_paths(monkeypatch, **paths: str) -> None:
    monkeypatch.setenv("JWT_SECRET", _LEGACY_SECRET)
    for name, value in paths.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    clear_key_cache()


@pytest.fixture
def key_env_teardown():
    """Restore settings + key caches after every path-mutating test."""
    yield
    get_settings.cache_clear()
    clear_key_cache()


def test_unreadable_configured_paths_hard_fail(monkeypatch, tmp_path, key_env_teardown) -> None:
    """Paths SET but files absent (a bad/missing mount) must raise, naming the
    path — never fall back to an ephemeral keypair whose tokens die on restart."""
    missing_priv = tmp_path / "jwt_access_private.pem"  # deliberately never created
    missing_pub = tmp_path / "jwt_access_public.pem"
    _apply_key_paths(
        monkeypatch,
        JWT_ACCESS_PRIVATE_KEY_PATH=str(missing_priv),
        JWT_ACCESS_PUBLIC_KEY_PATH=str(missing_pub),
    )
    with pytest.raises(RuntimeError, match="not readable"):
        get_family_keys("access")
    with pytest.raises(RuntimeError, match="not readable"):
        preload_signing_keys()


def test_container_convention_paths_are_probed_at_boot(monkeypatch, key_env_teardown) -> None:
    """The exact env a compose deployment sets (/run/secrets/jwt/...) must be
    probed by the boot preload: without the mount present, boot refuses."""
    if Path(MOUNT_TARGET, "jwt_access_private.pem").exists():
        pytest.skip("running inside a container with the mount provisioned")
    _apply_key_paths(
        monkeypatch,
        JWT_ACCESS_PRIVATE_KEY_PATH=f"{MOUNT_TARGET}/jwt_access_private.pem",
        JWT_ACCESS_PUBLIC_KEY_PATH=f"{MOUNT_TARGET}/jwt_access_public.pem",
        JWT_SESSION_PRIVATE_KEY_PATH=f"{MOUNT_TARGET}/jwt_session_private.pem",
        JWT_SESSION_PUBLIC_KEY_PATH=f"{MOUNT_TARGET}/jwt_session_public.pem",
    )
    with pytest.raises(RuntimeError, match=MOUNT_TARGET):
        preload_signing_keys()


def test_preload_reports_file_backed_and_ephemeral(monkeypatch, tmp_path, key_env_teardown) -> None:
    """The boot log contract PM verifies a deploy against: file-backed families
    report their path, unconfigured ones report EPHEMERAL."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = tmp_path / "jwt_access_private.pem"
    pub = tmp_path / "jwt_access_public.pem"
    priv.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    pub.write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    _apply_key_paths(
        monkeypatch,
        JWT_ACCESS_PRIVATE_KEY_PATH=str(priv),
        JWT_ACCESS_PUBLIC_KEY_PATH=str(pub),
    )
    sources = preload_signing_keys()
    assert sources["access"] == f"file-backed ({priv})"
    assert "EPHEMERAL" in sources["session"], sources


def test_lifespan_boots_with_the_key_preload() -> None:
    """The wiring pin: startup itself calls the preload, so an unreadable mount
    fails the BOOT (autoheal-visible, logs name the path) instead of serving a
    healthy-looking api whose first login 500s."""
    from app.main import lifespan

    source = inspect.getsource(inspect.unwrap(lifespan))
    # Pin the CALL (parens), not the bare name: the import line alone would
    # keep this green after the preload was removed from the boot path.
    assert "preload_signing_keys()" in source, (
        "app startup must eagerly load the signing keys (t_adfb7881): a bad "
        "secrets mount has to fail the boot, not the first login"
    )
