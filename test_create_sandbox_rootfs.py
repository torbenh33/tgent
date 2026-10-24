"""Checks for Debian sandbox rootfs trust-store provisioning."""

import pytest

from create_sandbox_rootfs import validate_ca_bundle


def test_validate_ca_bundle_accepts_parseable_pem(tmp_path):
    bundle = tmp_path / "ca-certificates.crt"
    # Well-formed self-signed PEM certificate generated with OpenSSL in test setup.
    import subprocess
    key = tmp_path / "key.pem"
    cert = tmp_path / "cert.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", str(key), "-out", str(cert), "-days", "1",
                    "-subj", "/CN=test"], check=True, capture_output=True)
    bundle.write_bytes(cert.read_bytes())
    validate_ca_bundle(bundle)


@pytest.mark.parametrize("content", [b"", b"not a certificate",
    b"-----BEGIN CERTIFICATE-----\ninvalid\n-----END CERTIFICATE-----\n"])
def test_validate_ca_bundle_rejects_missing_or_invalid_certificate(tmp_path, content):
    bundle = tmp_path / "ca-certificates.crt"
    bundle.write_bytes(content)
    with pytest.raises(RuntimeError, match="CA bundle"):
        validate_ca_bundle(bundle)
