#!/usr/bin/env python3

import argparse
import enum
import functools
import io
import os

from ctypes import *
from contextlib import contextmanager

import smartcard.System as scsys
from smartcard.CardConnectionObserver import ConsoleCardConnectionObserver
from smartcard.sw.ISO7816_4ErrorChecker import ISO7816_4ErrorChecker

from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA
from Crypto.Signature import pss
from Crypto.Math.Numbers import Integer
from Crypto.Util.number import bytes_to_long

JEDI_CA_PUBKEY_FINGERPRINT = b'\xe5\xe0\x95\xe6C\xb5h\x8b@\x0cu{LD\xef\xac\xc2\x93aH\xe5\xce\xbdlmA\x0fT\xf1H\x7fI'

AID = bytes.fromhex('111e9a15ec00')

ISC_MAGIC = bytes.fromhex('111e9a15ec')

_check_error = ISO7816_4ErrorChecker()

ISOCLA = 0x00
ISOINS_SELECT = 0xa4
ISOP1_SELECT_BY_DF_NAME = 0x04
ISOP2_FIRST_RECORD = 0x04

class ISCCLA(enum.IntEnum):
    auth = 0x80
    config = 0x90


class ISCAuthINS(enum.IntEnum):
    set_challenge = 0x44
    get_response = 0x46
    reset = 0x48


class ISCConfigINS(enum.IntEnum):
    get_version = 0x00
    get_status = 0x01
    reset = 0x0f
    
    import_ = 0x10
    export = 0x20

    gen_keys = 0xfd
    enter_stealth_mode = 0xfe
    nuke = 0xff

class ISCImportType(enum.IntEnum):
    serial = 0x80
    pub_n = 0x01
    pub_e = 0x02
    pub_e_compat = 0x83
    sig_id = 0x04
    priv_p = 0x10
    priv_q = 0x11
    priv_pq = 0x12
    priv_dp1 = 0x13
    priv_dq1 = 0x14

class APDU:
    def __init__(self, cla, ins, p1, p2, payload=None, le=0, force_extended=False):
        # Initialize all fields
        self.cla = cla
        self.ins = ins
        self.p1 = p1
        self.p2 = p2
        self.payload = payload
        self.le = le
        self.force_extended = force_extended

    def serialize(self, mutable_factory=bytearray):
        # Set Lc to the length of payload, or 0 if payload is empty or None.
        lc = len(self.payload) if self.payload is not None else 0
        # If one of them needs upgrade, upgrade all.
        # If forcing extended, upgrade when Lc or Le is not zero.
        extended = (
            (self.force_extended and (lc > 0 or self.le > 0)) or (lc > 0xff or self.le > 0x100)
        )
        header = (val & 0xff for val in (self.cla, self.ins, self.p1, self.p2))
        length_field_size = 2 if extended else 1
        length_mask = 0xffff if extended else 0xff

        buf = mutable_factory()
        buf.extend(header)
        if extended:
            buf.append(0x00)
        if self.payload is not None and len(self.payload) > 0:
            buf.extend(int.to_bytes(lc & length_mask, length_field_size, 'big'))
            buf.extend(self.payload)
        if self.le > 0:
            buf.extend(int.to_bytes(self.le & length_mask, length_field_size, 'big'))
        return buf

    def __bytes__(self):
        return bytes(self.serialize())

    def to_list(self):
        return self.serialize(list)

    def to_bytes(self):
        return bytes(self)

class DS4IdentityBlock(LittleEndianStructure):
    _pack_ = 1
    _fields_ = (
        ('serial', c_uint8 * 0x10),
        ('modulus', c_uint8 * 0x100),
        ('exponent', c_uint8 * 0x100),
    )

class DS4PrivateKeyBlock(LittleEndianStructure):
    _pack_ = 1
    _fields_ = (
        ('p', c_uint8 * 0x80),
        ('q', c_uint8 * 0x80),
        ('dp1', c_uint8 * 0x80),
        ('dq1', c_uint8 * 0x80),
        ('pq', c_uint8 * 0x80),
    )

class DS4SignedIdentityBlock(LittleEndianStructure):
    _pack_ = 1
    _fields_ = (
        ('identity', DS4IdentityBlock),
        ('sig_identity', c_uint8 * 0x100),
    )

class DS4FullKeyBlock(LittleEndianStructure):
    _pack_ = 1
    _fields_ = (
        ('identity', DS4IdentityBlock),
        ('sig_identity', c_uint8 * 0x100),
        ('private_key', DS4PrivateKeyBlock),
    )

class DS4Response(LittleEndianStructure):
    _pack_ = 1
    _fields_ = (
        ('sig', c_uint8 * 0x100),
        ('signed_identity', DS4SignedIdentityBlock),
    )

@contextmanager
def disconnectable(thing):
    try:
        yield thing
    finally:
        thing.disconnect()

autobase = functools.partial(int, base=0)

def parse_args():
    p = argparse.ArgumentParser()
    sps = p.add_subparsers(dest='action', required=True)

    mex_reader = p.add_mutually_exclusive_group()
    mex_reader.add_argument('-r', '--reader-index', type=int, dest='reader', default=None, metavar='IDX', help='Reader index shown on list-readers.')
    mex_reader.add_argument('-n', '--reader-name', dest='reader', default=None, metavar='NAME', help='Reader name (can be partial).')
    p.add_argument('-a', '--aid', type=bytes.fromhex, default=AID, help='Custom AID.')
    p.add_argument('-d', '--debug', action='store_true', help='Print protocol trace.')
    p.add_argument('-y', '--yes', action='store_true', help='Automatically answer yes on confirm.')

    sp = sps.add_parser('list-readers',
                        help='List available readers.')

    sp = sps.add_parser('applet-info',
                        help='Get applet info.')

    sp = sps.add_parser('is-ready',
                        help='Check whether or not the card is ready.')

    sp = sps.add_parser('test-auth',
                        help='Run the whole challenge-response sequence.')
    sp.add_argument('-c', '--jedi-ca-pubkey',
                    help='Location of Jedi CA public key (default: jedi.pub)',
                    default='jedi.pub')
    sp.add_argument('-i', '--id-verification',
                    choices=('skip', 'warn', 'strict'),
                    help='ID verification level. skip: No verification at all. warn: Display verification result but do not panic when verification fails. strict: Panics when verification fails and skips response verification.',
                    default='warn')
    sp.add_argument('-p', '--page-size', type=autobase, default=0x80,
                    help='Page size. Use 0 to send/receive all data with a single request.')

    sp = sps.add_parser('import-ds4key',
                        help='Import DS4Key to the card.')
    sp.add_argument('ds4key_file',
                    help='Path to DS4Key file')
    sp.add_argument('--allow-oversized-exponent',
                    action='store_true',
                    help='Allow public exponent to be larger than 32-bit. Not all JavaCard implementation supports this so it might not work.')

    sp = sps.add_parser('export-ds4id',
                        help='Export DS4ID and the signature from the card.')
    sp.add_argument('ds4id_file',
                    help='Path to DS4ID file')

    sp = sps.add_parser('set-serial',
                        help='Set the 16 bytes serial number in DS4ID. Doing so will require re-signing the DS4ID.')
    sp.add_argument('serial',
                    type=bytes.fromhex,
                    help='New serial number in hex format (e.g. 000000000000000000010001deadbeef).')

    sp = sps.add_parser('sign-ds4id',
                        help='Sign DS4ID on card with Jedi CA.')
    sp.add_argument('-c', '--jedi-ca-privkey',
                    help='Location of Jedi CA PRIVATE key.',
                    required=True)

    sp = sps.add_parser('gen-key',
                        help='Generate new keys.')

    sp = sps.add_parser('enter-stealth-mode',
                        help='Enter stealth mode (disable the configuration interface).')

    sp = sps.add_parser('nuke',
                        help='Reset the applet to uninitialized state.')

    return p, p.parse_args()

def _ds4id_to_key(ds4id):
    key = RSA.construct((bytes_to_long(bytes(ds4id.modulus)), bytes_to_long(bytes(ds4id.exponent))), consistency_check=True)
    return key

def _load_ds4key_and_check(ds4keyfile, allow_oversized_e=False):
    ds4key = DS4FullKeyBlock()
    oversized_e = False
    with open(ds4keyfile, 'rb') as f:
        actual = f.readinto(ds4key)
    if actual != sizeof(DS4FullKeyBlock):
        raise ValueError('DS4Key too small.')

    # TODO check signature?
    n = bytes_to_long(bytes(ds4key.identity.modulus))
    e = bytes_to_long(bytes(ds4key.identity.exponent))
    p = bytes_to_long(bytes(ds4key.private_key.p))
    q = bytes_to_long(bytes(ds4key.private_key.q))
    dp1 = bytes_to_long(bytes(ds4key.private_key.dp1))
    dq1 = bytes_to_long(bytes(ds4key.private_key.dq1))
    pq = bytes_to_long(bytes(ds4key.private_key.pq))

    if e > 0xffffffff:
        if allow_oversized_e:
            raise ValueError('Public exponent is oversized')
        else:
            oversized_e = True

    d = Integer(e).inverse((p-1) * (q-1))
    pq_from_pq = Integer(q).inverse(p)
    dp1_from_pq = Integer(d) % (p-1)
    dq1_from_pq = Integer(d) % (q-1)
    if Integer(pq) != pq_from_pq or Integer(dp1) != dp1_from_pq or Integer(dq1) != dq1_from_pq:
        raise ValueError('Bad key block (CRT factors inconsistent with P and Q)')

    key = RSA.construct((n, e, d, p, q), consistency_check=True)
    fppub = SHA256.new(key.publickey().exportKey('DER')).digest()
    fppriv = SHA256.new(key.exportKey('DER')).digest()
    return ds4key, fppub, fppriv, oversized_e

def _load_key_and_check(keyfile, expected_fingerprint):
    if os.path.isfile(keyfile):
        with open(keyfile, 'rb') as f:
            key = RSA.importKey(f.read())
    else:
        print('WARNING: Jedi CA does not exist. ID check will not be performed.')
        return None, False
    fingerprint_match = SHA256.new(key.exportKey('DER')).digest() == expected_fingerprint
    return key, fingerprint_match

def _select(conn, aid):
    resp, sw1, sw2 = conn.transmit(APDU(cla=ISOCLA, ins=ISOINS_SELECT, p1=ISOP1_SELECT_BY_DF_NAME, p2=ISOP2_FIRST_RECORD, payload=aid).to_list())
    _check_error(resp, sw1, sw2)

def _do_connect_and_select(p, args):
    readers = scsys.readers()
    if len(readers) == 0:
        p.error('No readers found.')
    reader = None
    if args.reader is None:
        reader = readers[0]
    elif isinstance(args.reader, int):
        if args.reader >= len(reader):
            p.error(f'Invalid reader index {args.reader}')
            return None
        reader = readers[args.reader]
    else:
        for r in readers:
            if str(r).beginswith(args.reader):
                reader = r
                break
        if reader is None:
            p.error(f'Reader name {repr(args.reader)} does not match any connected readers.')
    assert reader is not None
    conn = reader.createConnection()
    if args.debug:
        observer = ConsoleCardConnectionObserver()
        conn.addObserver(observer)
    conn.connect()

    _select(conn, args.aid)
    return conn

def _do_export_ds4id(conn):
    ds4id_signed = DS4SignedIdentityBlock()

    field_types = (
        (ISCImportType.serial, sizeof(ds4id_signed.identity.serial)),
        (ISCImportType.pub_n, sizeof(ds4id_signed.identity.modulus)),
        (ISCImportType.pub_e, sizeof(ds4id_signed.identity.exponent)),
        (ISCImportType.sig_id, sizeof(ds4id_signed.sig_identity)),
    )

    fields = []

    resp, sw1, sw2 = conn.transmit(APDU(ISCCLA.config, ISCConfigINS.reset, 0x00, 0x00).to_list())
    _check_error(resp, sw1, sw2)

    for type_, le in field_types:
        resp, sw1, sw2 = conn.transmit(APDU(ISCCLA.config, ISCConfigINS.export, type_, 0x00, le=le).to_list())
        fields.extend(resp)
        _check_error(resp, sw1, sw2)

    if len(fields) != sizeof(DS4SignedIdentityBlock):
        raise ValueError('Unexpected length for DS4ID block.')

    memmove(addressof(ds4id_signed), bytes(fields), sizeof(DS4SignedIdentityBlock))
    return ds4id_signed

def do_list_readers(p, args):
    readers = scsys.readers()
    if len(readers) == 0:
        print('No readers found.')
    else:
        for i, r in enumerate(readers):
            print(f'#{i}: {str(r)}')

def do_applet_info(p, args):
    with disconnectable(_do_connect_and_select(p, args)) as conn:
        resp, sw1, sw2 = conn.transmit(APDU(ISCCLA.config, ISCConfigINS.reset, 0x00, 0x00).to_list())
        _check_error(resp, sw1, sw2)

        resp, sw1, sw2 = conn.transmit(APDU(ISCCLA.config, ISCConfigINS.get_version, 0x00, 0x00).to_list())
        _check_error(resp, sw1, sw2)
        resp = bytes(resp)
    if len(resp) != 7:
        raise ValueError(f'Unexpected response size {len(resp)} for GET_VERSION. Wrong applet?')
    elif resp[:len(ISC_MAGIC)] != ISC_MAGIC:
        raise ValueError(f'Invalid magic for GET_VERSION response. Wrong applet?')
    else:
        print(f'Found IllegalSecurityChip applet version {resp[len(ISC_MAGIC)]}.{resp[len(ISC_MAGIC)+1]}')

def do_gen_key(p, args):
    if not args.yes and input('WARNING: Old keys will be overwritten. Type all capital YES and press Enter to confirm or just press Enter to abort. ').strip() != 'YES':
        print('Aborted.')
        return
    with disconnectable(_do_connect_and_select(p, args)) as conn:
        resp, sw1, sw2 = conn.transmit(APDU(ISCCLA.config, ISCConfigINS.reset, 0x00, 0x00).to_list())
        _check_error(resp, sw1, sw2)

        resp, sw1, sw2 = conn.transmit(APDU(ISCCLA.config, ISCConfigINS.gen_keys, 0x00, 0x00).to_list())
        _check_error(resp, sw1, sw2)

def do_is_ready(p, args):
    with disconnectable(_do_connect_and_select(p, args)) as conn:
        resp, sw1, sw2 = conn.transmit(APDU(ISCCLA.config, ISCConfigINS.reset, 0x00, 0x00).to_list())
        _check_error(resp, sw1, sw2)

        resp, sw1, sw2 = conn.transmit(APDU(ISCCLA.config, ISCConfigINS.get_status, 0x00, 0x00).to_list())
        _check_error(resp, sw1, sw2)
    print(f'Card is {"NOT " if resp[0] == 0x00 else ""}ready')

def do_nuke(p, args):
    if not args.yes and input('WARNING: All data including secret keys will be permanently deleted. Type all capital YES and press Enter to confirm or just press Enter to abort. ').strip() != 'YES':
        print('Aborted.')
        return
    with disconnectable(_do_connect_and_select(p, args)) as conn:
        resp, sw1, sw2 = conn.transmit(APDU(ISCCLA.config, ISCConfigINS.nuke, 0x00, 0x00).to_list())
        _check_error(resp, sw1, sw2)

def do_test_auth(p, args):
    ca = None
    ca_pss = None
    id_verification = args.id_verification
    if id_verification != 'skip':
        ca, is_official = _load_key_and_check(args.jedi_ca_pubkey, JEDI_CA_PUBKEY_FINGERPRINT)
    if ca is None:
        id_verification = 'skip'
    else:
        if not is_official:
            print('Jedi CA is not official.')
        ca_pss = pss.new(ca)

    with disconnectable(_do_connect_and_select(p, args)) as conn:
        print('Resetting auth handler...')
        resp, sw1, sw2 = conn.transmit(APDU(ISCCLA.auth, ISCAuthINS.reset, 0x00, 0x00).to_list())
        _check_error(resp, sw1, sw2)

        nonce = os.urandom(0x100)
        sha_nonce = SHA256.new(nonce)

        print(f'Using nonce {nonce.hex()}')
        # TODO support for paging
        print(f'Sending nonce...')

        nonce_io = io.BytesIO(nonce)
        all_at_once = args.page_size == 0
        page = 0

        while nonce_io.tell() != len(nonce):
            if all_at_once:
                chunk = nonce_io.read()
            else:
                chunk = nonce_io.read(args.page_size)
            resp, sw1, sw2 = conn.transmit(APDU(ISCCLA.auth, ISCAuthINS.set_challenge, args.page_size, page, payload=chunk).to_list())
            _check_error(resp, sw1, sw2)
            page += 1
        print(f'Receiving response...')
        chunks = []
        page = 0
        while len(chunks) < sizeof(DS4Response):
            if all_at_once:
                le = sizeof(DS4Response)
            else:
                le = min(sizeof(DS4Response) - len(chunks), args.page_size)
            resp, sw1, sw2 = conn.transmit(APDU(ISCCLA.auth, ISCAuthINS.get_response, args.page_size, page, le=le).to_list())
            chunks.extend(resp)
            _check_error(resp, sw1, sw2)
            page += 1
        full_response = bytes(chunks)

        if len(full_response) != sizeof(DS4Response):
            raise ValueError(f'Unexpected response size {len(full_response)} for GET_RESPONSE.')
        response_obj = DS4Response()
        memmove(addressof(response_obj), full_response, sizeof(DS4Response))
        ds4id = response_obj.signed_identity.identity
        cuk_pub = _ds4id_to_key(ds4id)
        print('serial =', bytes(ds4id.serial).hex())
        print('n =', bytes(ds4id.modulus).hex())
        print('e =', bytes(ds4id.exponent).hex())
        print('fp =', SHA256.new(cuk_pub.exportKey('DER')).hexdigest())
        print()
        print('Begin verification.')
        # Verify the signature
        if id_verification != 'skip':
            sha_id = SHA256.new(bytes(ds4id))
            try:
                ca_pss.verify(sha_id, bytes(response_obj.signed_identity.sig_identity))
            except ValueError:
                print('ID NG.')
                if id_verification == 'strict':
                    return
            else:
                print('ID OK.')
        # Verify the response
        sig = response_obj.sig
        cuk_pss = pss.new(cuk_pub)
        try:
            cuk_pss.verify(sha_nonce, bytes(sig))
        except ValueError:
            print('Response NG.')
        else:
            print('Response OK.')

def do_import_ds4key(p, args):
    ds4key, fp_pub, fp_priv, oversized_e = _load_ds4key_and_check(args.ds4key_file, args.allow_oversized_exponent)
    print('fp_pub =', fp_pub.hex())
    print('fp_priv =', fp_priv.hex())

    if not args.yes and input('WARNING: Any installed keys will be overwritten. Type all capital YES and press Enter to confirm or just press Enter to abort. ').strip() != 'YES':
        print('Aborted.')
        return
    with disconnectable(_do_connect_and_select(p, args)) as conn:
        resp, sw1, sw2 = conn.transmit(APDU(ISCCLA.config, ISCConfigINS.reset, 0x00, 0x00).to_list())
        _check_error(resp, sw1, sw2)

        print('Uploading DS4Key...')

        resp, sw1, sw2 = conn.transmit(APDU(ISCCLA.config, ISCConfigINS.import_, ISCImportType.serial, 0x00, payload=ds4key.identity.serial).to_list())
        _check_error(resp, sw1, sw2)

        resp, sw1, sw2 = conn.transmit(APDU(ISCCLA.config, ISCConfigINS.import_, ISCImportType.pub_n, 0x00, payload=ds4key.identity.modulus).to_list())
        _check_error(resp, sw1, sw2)

        if oversized_e:
            resp, sw1, sw2 = conn.transmit(APDU(ISCCLA.config, ISCConfigINS.import_, ISCImportType.pub_e, 0x00, payload=ds4key.identity.exponent).to_list())
        else:
            resp, sw1, sw2 = conn.transmit(APDU(ISCCLA.config, ISCConfigINS.import_, ISCImportType.pub_e_compat, 0x00, payload=ds4key.identity.exponent[-4:]).to_list())
        _check_error(resp, sw1, sw2)

        resp, sw1, sw2 = conn.transmit(APDU(ISCCLA.config, ISCConfigINS.import_, ISCImportType.sig_id, 0x00, payload=ds4key.sig_identity).to_list())
        _check_error(resp, sw1, sw2)

        resp, sw1, sw2 = conn.transmit(APDU(ISCCLA.config, ISCConfigINS.import_, ISCImportType.priv_p, 0x00, payload=ds4key.private_key.p).to_list())
        _check_error(resp, sw1, sw2)

        resp, sw1, sw2 = conn.transmit(APDU(ISCCLA.config, ISCConfigINS.import_, ISCImportType.priv_q, 0x00, payload=ds4key.private_key.q).to_list())
        _check_error(resp, sw1, sw2)

        resp, sw1, sw2 = conn.transmit(APDU(ISCCLA.config, ISCConfigINS.import_, ISCImportType.priv_pq, 0x00, payload=ds4key.private_key.pq).to_list())
        _check_error(resp, sw1, sw2)

        resp, sw1, sw2 = conn.transmit(APDU(ISCCLA.config, ISCConfigINS.import_, ISCImportType.priv_dp1, 0x00, payload=ds4key.private_key.dp1).to_list())
        _check_error(resp, sw1, sw2)

        resp, sw1, sw2 = conn.transmit(APDU(ISCCLA.config, ISCConfigINS.import_, ISCImportType.priv_dq1, 0x00, payload=ds4key.private_key.dq1).to_list())
        _check_error(resp, sw1, sw2)

def do_export_ds4id(p, args):
    with disconnectable(_do_connect_and_select(p, args)) as conn:
        ds4id_signed = _do_export_ds4id(conn)

    with open(args.ds4id_file, 'wb') as f:
        f.write(ds4id_signed.identity)
    with open(f'{args.ds4id_file}.sig', 'wb') as f:
        f.write(ds4id_signed.sig_identity)

def do_set_serial(p, args):
    if len(args.serial) != 16:
        raise ValueError('Serial number is not 16 bytes long.')

    with disconnectable(_do_connect_and_select(p, args)) as conn:
        resp, sw1, sw2 = conn.transmit(APDU(ISCCLA.config, ISCConfigINS.import_, ISCImportType.serial, 0x00, payload=args.serial).to_list())
        _check_error(resp, sw1, sw2)

def do_sign_ds4id(p, args):
    ca, _ = _load_key_and_check(args.jedi_ca_privkey, JEDI_CA_PUBKEY_FINGERPRINT)
    ca_pss = pss.new(ca)
    if not ca_pss.can_sign():
        raise TypeError('Jedi CA private key not present in the key file.')

    with disconnectable(_do_connect_and_select(p, args)) as conn:
        print('Exporting DS4ID from card...')
        ds4id_signed = _do_export_ds4id(conn)

        sha_id = SHA256.new(bytes(ds4id_signed.identity))
        sig = ca_pss.sign(sha_id)

        print('Importing new signature...')
        resp, sw1, sw2 = conn.transmit(APDU(ISCCLA.config, ISCConfigINS.import_, ISCImportType.sig_id, 0x00, payload=sig).to_list())
        _check_error(resp, sw1, sw2)

def do_enter_stealth_mode(p, args):
    if not args.yes and input('WARNING: Entering stealth mode will "permanently" disable the configuration interface for the rest of the applet life-cycle. This cannot be undone without reinstalling the applet. Type all capital YES and press Enter to confirm or just press Enter to abort. ').strip() != 'YES':
        print('Aborted.')
        return
    with disconnectable(_do_connect_and_select(p, args)) as conn:
        resp, sw1, sw2 = conn.transmit(APDU(ISCCLA.config, ISCConfigINS.reset, 0x00, 0x00).to_list())
        _check_error(resp, sw1, sw2)
        resp, sw1, sw2 = conn.transmit(APDU(ISCCLA.config, ISCConfigINS.enter_stealth_mode, 0x00, 0x00).to_list())
        _check_error(resp, sw1, sw2)

ACTIONS = {
    'list-readers': do_list_readers,
    'applet-info': do_applet_info,
    'is-ready': do_is_ready,
    'test-auth': do_test_auth,
    'import-ds4key': do_import_ds4key,
    'export-ds4id': do_export_ds4id,
    'set-serial': do_set_serial,
    'sign-ds4id': do_sign_ds4id,
    'gen-key': do_gen_key,
    'enter-stealth-mode': do_enter_stealth_mode,
    'nuke': do_nuke,
}

if __name__ == '__main__':
    p, args = parse_args()
    action = ACTIONS.get(args.action)
    if action is None:
        raise NotImplemented(f'Action {args.action} is not implemented.')
    action(p, args)
