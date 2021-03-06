import os
import datetime
import binascii
import json
import argparse
import hid
import cryptography
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from mchp_aws_zt_kit import MchpAwsZTKitDevice
from sim_hid_device import SimMchpAwsZTHidDevice
from aws_kit_common import *

from serial import Serial
from time import sleep
import sys

def main():
    ############################################################################
    if len(sys.argv) != 2:
        print("You need to supply USB port as the first argument, example command: python manual_kit_provision.py /dev/tty.usbmodem1421. On Linux/MAC you can use this command to see list of available ports: ls /dev | grep 'tty.usb'")
        sys.exit()
    else:
        port = str(sys.argv[1])
    com = Serial(port, 115200)
    ############################################################################

    print('\nLoading root CA certificate')
    if not os.path.isfile(ROOT_CA_CERT_FILENAME):
        raise AWSZTKitError('Failed to find root CA certificate file, ' + ROOT_CA_CERT_FILENAME + '. Have you run ca_create_root first?')
    with open(ROOT_CA_CERT_FILENAME, 'rb') as f:
        print('    Loading from ' + f.name)
        root_ca_cert = x509.load_pem_x509_certificate(f.read(), crypto_be)

    print('\nLoading signer CA key')
    if not os.path.isfile(SIGNER_CA_KEY_FILENAME):
        raise AWSZTKitError('Failed to find signer CA key file, ' + SIGNER_CA_KEY_FILENAME + '. Have you run ca_create_signer_csr first?')
    with open(SIGNER_CA_KEY_FILENAME, 'rb') as f:
        print('    Loading from ' + f.name)
        signer_ca_priv_key = serialization.load_pem_private_key(
            data=f.read(),
            password=None,
            backend=crypto_be)

    print('\nLoading signer CA certificate')
    if not os.path.isfile(SIGNER_CA_CERT_FILENAME):
        raise AWSZTKitError('Failed to find signer CA certificate file, ' + SIGNER_CA_CERT_FILENAME + '. Have you run ca_create_signer first?')
    with open(SIGNER_CA_CERT_FILENAME, 'rb') as f:
        print('    Loading from ' + f.name)
        signer_ca_cert = x509.load_pem_x509_certificate(f.read(), crypto_be)

    print('\nRequesting device CSR')
    ############################################################################
    com.write("genCsr:\0".encode())
    res = com.read_until(b'\x00')
    csr = res[:-1].decode("utf-8")
    print('    Done.')
    ############################################################################
    device_csr = x509.load_der_x509_csr(binascii.a2b_hex(csr), crypto_be)
    if not device_csr.is_signature_valid:
        raise AWSZTKitError('Device CSR has invalid signature.')
    with open(DEVICE_CSR_FILENAME, 'wb') as f:
        print('    Saving to ' + f.name)
        f.write(device_csr.public_bytes(encoding=serialization.Encoding.PEM))

    print('\nGenerating device certificate from CSR')
    # Build certificate
    builder = x509.CertificateBuilder()
    builder = builder.issuer_name(signer_ca_cert.subject)
    builder = builder.not_valid_before(datetime.datetime.now(tz=pytz.utc).replace(minute=0,second=0)) # Device cert must have minutes and seconds set to 0
    builder = builder.not_valid_after(datetime.datetime(3000, 12, 31, 23, 59, 59)) # Should be year 9999, but this doesn't work on windows
    builder = builder.subject_name(device_csr.subject)
    builder = builder.public_key(device_csr.public_key())
    # Device certificate is generated from certificate dates and public key
    builder = builder.serial_number(device_cert_sn(16, builder))
    # Add in extensions specified by CSR
    for extension in device_csr.extensions:
        builder = builder.add_extension(extension.value, extension.critical)
    # Subject Key ID is used as the thing name and MQTT client ID and is required for this demo
    builder = builder.add_extension(
        x509.SubjectKeyIdentifier.from_public_key(builder._public_key),
        critical=False)
    issuer_ski = signer_ca_cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier)
    builder = builder.add_extension(
        x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(issuer_ski),
        critical=False)

    # Sign certificate
    device_cert = builder.sign(
        private_key=signer_ca_priv_key,
        algorithm=hashes.SHA256(),
        backend=crypto_be)

    for extension in device_cert.extensions:
        if extension.oid._name != 'subjectKeyIdentifier':
            continue # Not the extension we're looking for, skip
        thing_name = binascii.b2a_hex(extension.value.digest).decode('ascii')

    # Save certificate for reference
    with open(DEVICE_CERT_FILENAME, 'wb') as f:
        print('    Saving to ' + f.name)
        f.write(device_cert.public_bytes(encoding=serialization.Encoding.PEM))

    print('\nProvisioning device with AWS IoT credentials\n')
    pub_nums = root_ca_cert.public_key().public_numbers()
    pubkey =  pub_nums.x.to_bytes(32, byteorder='big', signed=False)
    pubkey += pub_nums.y.to_bytes(32, byteorder='big', signed=False)

    ############################################################################
    print("Sending Singer CA Pub Key")
    msg = "caPubKey:" + pubkey.hex() + '\0'
    com.write(msg.encode())
    res = com.read_until(b'\x00')
    print('    Done.\n')
    sleep(1)

    print("Sending Signer Certificate")
    msg = "caCert:" + signer_ca_cert.public_bytes(encoding=serialization.Encoding.DER).hex() + '\0'
    com.write(msg.encode())
    res = com.read_until(b'\x00')
    print('    Done.\n')
    sleep(1)

    print("Sending Device Certificate")
    msg = "deviceCert:" + device_cert.public_bytes(encoding=serialization.Encoding.DER).hex() + '\0'
    com.write(msg.encode())
    res = com.read_until(b'\x00')
    print('    Done.\n')
    sleep(1)

    print("Provisioning the WINC")
    com.write("transferCertificates:\0".encode())
    res = com.read_until(b'\x00')
    print('    Done.\n')
    ############################################################################

    print('Done provisioning thing {}'.format(thing_name))

try:
    main()
except AWSZTKitError as e:
    print(e)
