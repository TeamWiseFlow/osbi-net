"""Isolated HTTPS integration/visual fixture. Never a production model provider."""
import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import ipaddress
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import sys
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
sys.path.insert(0, str(Path(__file__).parent))
from conftest import Model, material
from osbi_server.service import Service
from osbi_server.config import Settings
from osbi_server.app import create_app as server_app
from osbi_client.app import create_app as client_app
from osbi_client.daemon import Daemon


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def certificate(directory):
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,"OSBI isolated test")])
    now=datetime.now(timezone.utc)
    cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=1)).not_valid_after(now+timedelta(days=2))
          .add_extension(x509.BasicConstraints(ca=True,path_length=0),critical=True)
          .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1")),x509.DNSName("localhost")]),critical=False)
          .sign(key,hashes.SHA256()))
    cert_file=directory/"test-cert.pem";key_file=directory/"test-key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    key_file.chmod(0o600)
    return cert_file,key_file


def run(port=8781):
    root=Path(tempfile.mkdtemp(prefix="osbi-integration-"))
    cert,key=certificate(root)
    shared=Service(Settings(root/"server"),Model())
    token=shared.repo.issue_key("isolated-demo",time.time()+3600)["key"]
    server_port=free_port()
    server=uvicorn.Server(uvicorn.Config(server_app(service=shared),host="127.0.0.1",port=server_port,
                                        ssl_certfile=str(cert),ssl_keyfile=str(key),log_level="warning"))
    thread=threading.Thread(target=server.run,daemon=True);thread.start()
    deadline=time.monotonic()+10
    while not server.started and time.monotonic()<deadline:time.sleep(.05)
    if not server.started:raise RuntimeError("Isolated TLS server failed to start")
    client=Daemon(root/"client")
    client.state.save_config({"serverUrl":f"https://127.0.0.1:{server_port}","serverKey":token,"serverCA":str(cert),"headless":True,
                            "sources":[{"id":"example","url":"https://example.org","profile":"demo"}]})
    # Submit through the same HTTPS SDK/outbox as an acquired material.
    payload=material(request="preview-fixture")
    from osbi_contracts.storage import encode
    client.state.execute("INSERT INTO outbox(id,run_id,payload,state,server_url,server_ca,credential_generation) VALUES (?,?,?,'pending',?,?,1)",
                         ("preview-fixture","",encode(payload),f"https://127.0.0.1:{server_port}",str(cert)))
    local=uvicorn.Server(uvicorn.Config(client_app(daemon=client),host="127.0.0.1",port=port,log_level="warning"))
    print(f"Isolated fixture with simulated model: http://127.0.0.1:{port}/graph",flush=True)
    try:local.run()
    finally:
        server.should_exit=True;thread.join(timeout=10)


if __name__=="__main__":
    parser=argparse.ArgumentParser();parser.add_argument("--port",type=int,default=8781)
    run(parser.parse_args().port)
