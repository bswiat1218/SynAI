from __future__ import annotations

import asyncio
import datetime as dt
import ipaddress
import ssl
import tempfile
import unittest
from pathlib import Path
from socket import socket

import httpx
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from synai.broker.app import BrokerService, create_app
from tests.test_broker import config


class BrokerMutualTLSIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.broker_ca_key, self.broker_ca = self._make_ca("broker-service-ca")
        self.core_ca_one_key, self.core_ca_one = self._make_ca("core-service-ca-one")
        self.core_ca_two_key, self.core_ca_two = self._make_ca("core-service-ca-two")
        self.other_ca_key, self.other_ca = self._make_ca("untrusted-service-ca")
        self.broker_cert, self.broker_key = self._make_leaf(
            "broker", "server", self.broker_ca_key, self.broker_ca,
            ExtendedKeyUsageOID.SERVER_AUTH,
        )
        self.core_one_cert, self.core_one_key = self._make_leaf(
            "core-one", "client", self.core_ca_one_key, self.core_ca_one,
            ExtendedKeyUsageOID.CLIENT_AUTH,
        )
        self.core_two_cert, self.core_two_key = self._make_leaf(
            "core-two", "client", self.core_ca_two_key, self.core_ca_two,
            ExtendedKeyUsageOID.CLIENT_AUTH,
        )
        self.expired_cert, self.expired_key = self._make_leaf(
            "core-expired", "client", self.core_ca_one_key, self.core_ca_one,
            ExtendedKeyUsageOID.CLIENT_AUTH, expired=True,
        )
        self.untrusted_cert, self.untrusted_key = self._make_leaf(
            "other-service", "client", self.other_ca_key, self.other_ca,
            ExtendedKeyUsageOID.CLIENT_AUTH,
        )
        self.broker_ca_path = self._write_ca_bundle("broker-ca.pem", [self.broker_ca])
        self.core_ca_path = self._write_ca_bundle("core-ca.pem", [self.core_ca_one])
        self.port = self._free_port()
        self.server_task: asyncio.Task[None] | None = None
        self.server: uvicorn.Server | None = None
        self.loop = asyncio.get_running_loop()
        self.previous_exception_handler = self.loop.get_exception_handler()
        self.loop.set_exception_handler(self._expected_tls_failure)
        await self._start_server()

    async def asyncTearDown(self) -> None:
        await self._stop_server()
        self.loop.set_exception_handler(self.previous_exception_handler)

    async def test_mtls_rejects_missing_untrusted_and_expired_peer_and_accepts_rotation(self) -> None:
        self.assertEqual(
            await self._request(self.core_one_cert, self.core_one_key),
            {"status": "alive"},
        )

        with self.assertRaises(httpx.TransportError):
            await self._request()
        with self.assertRaises(httpx.TransportError):
            await self._request(self.untrusted_cert, self.untrusted_key)
        with self.assertRaises(httpx.TransportError):
            await self._request(self.expired_cert, self.expired_key)

        await self._stop_server()
        self.core_ca_path.write_bytes(self._pem(self.core_ca_one) + self._pem(self.core_ca_two))
        await self._start_server()
        self.assertEqual(
            await self._request(self.core_two_cert, self.core_two_key),
            {"status": "alive"},
        )
        self.assertEqual(
            await self._request(self.core_one_cert, self.core_one_key),
            {"status": "alive"},
        )

        await self._stop_server()
        self.core_ca_path.write_bytes(self._pem(self.core_ca_two))
        await self._start_server()
        with self.assertRaises(httpx.TransportError):
            await self._request(self.core_one_cert, self.core_one_key)
        self.assertEqual(
            await self._request(self.core_two_cert, self.core_two_key),
            {"status": "alive"},
        )

        context = ssl.create_default_context(cafile=str(self.broker_ca_path))
        context.load_cert_chain(self.core_two_cert, self.core_two_key)
        async with httpx.AsyncClient(verify=context, trust_env=False) as client:
            response = await client.get(f"https://127.0.0.1:{self.port}/health")
            self.assertEqual(response.status_code, 200)

    async def _request(
        self,
        certificate: Path | None = None,
        private_key: Path | None = None,
    ) -> dict[str, str]:
        context = ssl.create_default_context(cafile=str(self.broker_ca_path))
        if certificate is not None and private_key is not None:
            context.load_cert_chain(certificate, private_key)
        async with httpx.AsyncClient(verify=context, trust_env=False, timeout=2) as client:
            response = await client.get(f"https://127.0.0.1:{self.port}/health")
            response.raise_for_status()
            return response.json()

    async def _start_server(self) -> None:
        broker_config = config(self.root / "broker")
        service = BrokerService(broker_config)
        server_config = uvicorn.Config(
            create_app(service),
            host="127.0.0.1",
            port=self.port,
            ssl_certfile=str(self.broker_cert),
            ssl_keyfile=str(self.broker_key),
            ssl_ca_certs=str(self.core_ca_path),
            ssl_cert_reqs=ssl.CERT_REQUIRED,
            log_level="critical",
            access_log=False,
        )
        self.server = uvicorn.Server(server_config)
        self.server_task = asyncio.create_task(self.server.serve())
        for _ in range(100):
            if self.server.started:
                return
            if self.server_task.done():
                await self.server_task
            await asyncio.sleep(0.02)
        self.fail("mTLS test listener did not start")

    async def _stop_server(self) -> None:
        if self.server is None or self.server_task is None:
            return
        self.server.should_exit = True
        await asyncio.wait_for(self.server_task, timeout=5)
        self.server = None
        self.server_task = None

    def _expected_tls_failure(
        self,
        loop: asyncio.AbstractEventLoop,
        context: dict[str, object],
    ) -> None:
        error = context.get("exception")
        if isinstance(error, (ssl.SSLError, ConnectionResetError)):
            return
        if self.previous_exception_handler is not None:
            self.previous_exception_handler(loop, context)
        else:
            loop.default_exception_handler(context)

    @staticmethod
    def _free_port() -> int:
        with socket() as listener:
            listener.bind(("127.0.0.1", 0))
            return int(listener.getsockname()[1])

    def _make_ca(self, common_name: str) -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
        now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=1))
            .not_valid_after(now + dt.timedelta(days=2))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True, content_commitment=False,
                    key_encipherment=False, data_encipherment=False,
                    key_agreement=False, key_cert_sign=True, crl_sign=True,
                    encipher_only=None, decipher_only=None,
                ),
                critical=True,
            )
            .sign(key, hashes.SHA256())
        )
        return key, cert

    def _make_leaf(
        self,
        common_name: str,
        role: str,
        issuer_key: rsa.RSAPrivateKey,
        issuer: x509.Certificate,
        usage: x509.ObjectIdentifier,
        *,
        expired: bool = False,
    ) -> tuple[Path, Path]:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
        now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        not_before = now - dt.timedelta(days=2) if expired else now - dt.timedelta(minutes=1)
        not_after = now - dt.timedelta(days=1) if expired else now + dt.timedelta(days=1)
        builder = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(not_before)
            .not_valid_after(not_after)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()),
                critical=False,
            )
            .add_extension(x509.ExtendedKeyUsage([usage]), critical=False)
        )
        if role == "server":
            builder = builder.add_extension(
                x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                critical=False,
            )
        certificate = builder.sign(issuer_key, hashes.SHA256())
        cert_path = self.root / f"{common_name}.crt"
        key_path = self.root / f"{common_name}.key"
        cert_path.write_bytes(self._pem(certificate))
        key_path.write_bytes(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))
        cert_path.chmod(0o644)
        key_path.chmod(0o600)
        return cert_path, key_path

    def _write_ca_bundle(
        self,
        name: str,
        certificates: list[x509.Certificate],
    ) -> Path:
        path = self.root / name
        path.write_bytes(b"".join(self._pem(cert) for cert in certificates))
        path.chmod(0o644)
        return path

    @staticmethod
    def _pem(certificate: x509.Certificate) -> bytes:
        return certificate.public_bytes(serialization.Encoding.PEM)
