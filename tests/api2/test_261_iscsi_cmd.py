import contextlib
import enum
import errno
import ipaddress
import os
import random
import socket
import string
from time import sleep

import iscsi
import pyscsi
import pytest
import requests
from assets.websocket.iscsi import (alua_enabled, initiator, initiator_portal,
                                    portal, read_capacity16, target,
                                    target_extent_associate, verify_capacity,
                                    verify_luns)
from middlewared.service_exception import InstanceNotFound, ValidationError, ValidationErrors
from middlewared.test.integration.assets.iscsi import target_login_test
from middlewared.test.integration.assets.pool import dataset, snapshot
from middlewared.test.integration.utils import call, ssh
from middlewared.test.integration.utils.client import truenas_server
from pyscsi.pyscsi.scsi_sense import sense_ascq_dict, sense_key_dict
from pytest_dependency import depends

from auto_config import ha, hostname, isns_ip, password, pool_name, user
from functions import SSH_TEST
from protocols import (initiator_name_supported, iscsi_scsi_connection,
                       isns_connection)

# Setup some flags that will enable/disable tests based upon the capabilities of the
# python-scsi package in use
try:
    from pyscsi.pyscsi.scsi_cdb_persistentreservein import PR_SCOPE, PR_TYPE
    pyscsi_has_persistent_reservations = 'PersistentReserveOut' in dir(pyscsi.pyscsi.scsi)
    LU_SCOPE = PR_SCOPE.LU_SCOPE
except ImportError:
    pyscsi_has_persistent_reservations = False
    LU_SCOPE = 0
skip_persistent_reservations = pytest.mark.skipif(not pyscsi_has_persistent_reservations,
                                                  reason="PYSCSI does not support persistent reservations")

skip_multi_initiator = pytest.mark.skipif(not initiator_name_supported(),
                                          reason="PYSCSI does not support persistent reservations")

skip_ha_tests = pytest.mark.skipif(not (ha and "virtual_ip" in os.environ), reason="Skip HA tests")


skip_invalid_initiatorname = pytest.mark.skipif(not initiator_name_supported(),
                                                reason="Invalid initiatorname will be presented")

pyscsi_has_report_target_port_groups = 'ReportTargetPortGroups' in dir(pyscsi.pyscsi.scsi)

# See: https://github.com/python-scsi/cython-iscsi/pull/8
pyscsi_supports_check_condition = hasattr(iscsi.Task, 'raw_sense')
skip_no_check_condition = pytest.mark.skipif(not pyscsi_supports_check_condition, "PYSCSI does not support CHECK CONDITION")


# The following strings are taken from pyscsi/pyscsi/scsi_exception
class CheckType(enum.Enum):
    CHECK_CONDITION = "CheckCondition"
    CONDITIONS_MET = "ConditionsMet"
    BUSY_STATUS = "BusyStatus"
    RESERVATION_CONFLICT = "ReservationConflict"
    TASK_SET_FULL = "TaskSetFull"
    ACA_ACTIVE = "ACAActive"
    TASK_ABORTED = "TaskAborted"

    def __str__(self):
        return self.value


# Some constants
MB = 1024 * 1024
MB_100 = 100 * MB
MB_200 = 200 * MB
MB_256 = 256 * MB
MB_512 = 512 * MB
PR_KEY1 = 0xABCDEFAABBCCDDEE
PR_KEY2 = 0x00000000DEADBEEF
CONTROLLER_A_TARGET_PORT_GROUP_ID = 101
CONTROLLER_B_TARGET_PORT_GROUP_ID = 102

# Some variables
digit = ''.join(random.choices(string.digits, k=2))
file_mountpoint = f'/tmp/iscsi-file-{hostname}'
zvol_mountpoint = f'/tmp/iscsi-zvol-{hostname}'
target_name = f"target{digit}"
dataset_name = f"iscsids{digit}"
file_name = f"iscsi{digit}"
basename = "iqn.2005-10.org.freenas.ctl"
zvol_name = f"ds{digit}"
zvol = f'{pool_name}/{zvol_name}'


def snapshot_rollback(snapshot_id):
    call('zfs.snapshot.rollback', snapshot_id)


def other_node(node):
    if node == 'A':
        return 'B'
    if node == 'B':
        return 'A'
    raise ValueError("Invalid node supplied")


def get_ip_addr(ip):
    try:
        ipaddress.ip_address(ip)
        return ip
    except ValueError:
        actual_ip = socket.gethostbyname(ip)
        ipaddress.ip_address(actual_ip)
        return actual_ip


@contextlib.contextmanager
def iscsi_auth(tag, user, secret, peeruser=None, peersecret=None):
    payload = {
        'tag': tag,
        'user': user,
        'secret': secret,
    }
    if peeruser and peersecret:
        payload.update({
            'peeruser': peeruser,
            'peersecret': peersecret
        })
    auth_config = call('iscsi.auth.create', payload)

    try:
        yield auth_config
    finally:
        call('iscsi.auth.delete', auth_config['id'])


@contextlib.contextmanager
def file_extent(pool_name, dataset_name, file_name, filesize=MB_512, extent_name='extent', serial=None):
    payload = {
        'type': 'FILE',
        'name': extent_name,
        'filesize': filesize,
        'path': f'/mnt/{pool_name}/{dataset_name}/{file_name}'
    }
    # We want to allow any non-None serial to be specified (even '')
    if serial is not None:
        payload.update({'serial': serial})
    extent_config = call('iscsi.extent.create', payload)

    try:
        yield extent_config
    finally:
        call('iscsi.extent.delete', extent_config['id'], True, True)


@contextlib.contextmanager
def zvol_dataset(zvol, volsize=MB_512, recursive=False, force=False):
    payload = {
        'name': zvol,
        'type': 'VOLUME',
        'volsize': volsize,
        'volblocksize': '16K'
    }
    dataset_config = call('pool.dataset.create', payload)

    try:
        yield dataset_config
    finally:
        try:
            call('pool.dataset.delete', dataset_config['id'], {'recursive': recursive, 'force': force})
        except InstanceNotFound:
            pass


def modify_extent(ident, payload):
    call('iscsi.extent.update', ident, payload)


def file_extent_resize(ident, filesize):
    payload = {
        'filesize': filesize,
    }
    modify_extent(ident, payload)


def extent_disable(ident):
    modify_extent(ident, {'enabled': False})


def extent_enable(ident):
    modify_extent(ident, {'enabled': True})


def zvol_resize(zvol, volsize):
    payload = {
        'volsize': volsize,
    }
    call('pool.dataset.update', zvol, payload)


def _get_iscsi_sessions(filters=None):
    if filters:
        return call('iscsi.global.sessions', filters)
    else:
        return call('iscsi.global.sessions')


def get_iscsi_sessions(filters=None, check_length=None):
    if isinstance(check_length, int):
        for _ in range(10):
            data = _get_iscsi_sessions(filters)
            if len(data) == check_length:
                return data
            sleep(1)
        assert len(data) == check_length, data
    else:
        data = _get_iscsi_sessions(filters)
    return data


def get_client_count():
    return call('iscsi.global.client_count')


def get_zvol_property(zvolid, property_name):
    return call('zfs.dataset.query', [['id', '=', zvolid]], {'get': True})['properties'][property_name]['value']


def get_volthreading(zvolid):
    return get_zvol_property(zvolid, 'volthreading')


def get_readonly(zvolid):
    return get_zvol_property(zvolid, 'readonly')


def verify_client_count(count, retries=10):
    """Verify that the client count is the expected value, but include some
    retries to allow things to settle if necessary."""
    assert retries > 0
    while retries:
        if get_client_count() == count:
            # All is good
            return
        retries -= 1
        sleep(1)
    assert get_client_count() == count


@contextlib.contextmanager
def zvol_extent(zvol, extent_name='zvol_extent'):
    payload = {
        'type': 'DISK',
        'disk': f'zvol/{zvol}',
        'name': extent_name,
    }
    extent_config = call('iscsi.extent.create', payload)

    try:
        yield extent_config
    finally:
        try:
            call('iscsi.extent.delete', extent_config['id'], True, True)
        except InstanceNotFound:
            pass


@contextlib.contextmanager
def configured_target_to_file_extent(config, target_name, pool_name, dataset_name, file_name, alias=None, filesize=MB_512, extent_name='extent'):
    portal_id = config['portal']['id']
    with target(target_name, [{'portal': portal_id}], alias) as target_config:
        target_id = target_config['id']
        with dataset(dataset_name) as dataset_config:
            with file_extent(pool_name, dataset_name, file_name, filesize=filesize, extent_name=extent_name) as extent_config:
                extent_id = extent_config['id']
                with target_extent_associate(target_id, extent_id):
                    newconfig = config.copy()
                    newconfig.update({
                        'target': target_config,
                        'dataset': dataset_config,
                        'extent': extent_config,
                    })
                    yield newconfig


@contextlib.contextmanager
def add_file_extent_target_lun(config, lun, filesize=MB_512, extent_name=None):
    name = config['target']['name']
    target_id = config['target']['id']
    dataset_name = f"iscsids{name}"
    lun_file_name = f'{name}_lun{lun}'
    if not extent_name:
        extent_name = lun_file_name
    with file_extent(pool_name, dataset_name, lun_file_name, filesize=filesize, extent_name=extent_name) as extent_config:
        extent_id = extent_config['id']
        with target_extent_associate(target_id, extent_id, lun):
            newconfig = config.copy()
            newconfig.update({
                f'extent_lun{lun}': extent_config,
            })
            yield newconfig


@contextlib.contextmanager
def configured_target_to_zvol_extent(config, target_name, zvol, alias=None, extent_name='zvol_extent', volsize=MB_512):
    portal_id = config['portal']['id']
    with target(target_name, [{'portal': portal_id}], alias) as target_config:
        target_id = target_config['id']
        with zvol_dataset(zvol, volsize) as dataset_config:
            with zvol_extent(zvol, extent_name=extent_name) as extent_config:
                extent_id = extent_config['id']
                with target_extent_associate(target_id, extent_id) as associate_config:
                    newconfig = config.copy()
                    newconfig.update({
                        'associate': associate_config,
                        'target': target_config,
                        'dataset': dataset_config['id'],
                        'extent': extent_config,
                    })
                    yield newconfig


@contextlib.contextmanager
def add_zvol_extent_target_lun(config, lun, volsize=MB_512, extent_name=None):
    name = config['target']['name']
    zvol_name = f"ds{name}"
    zvol = f'{pool_name}/{zvol_name}_lun{lun}'
    target_id = config['target']['id']
    lun_file_name = f'{name}_lun{lun}'
    if not extent_name:
        extent_name = lun_file_name
        with zvol_dataset(zvol, volsize) as dataset_config:
            with zvol_extent(zvol, extent_name=extent_name) as extent_config:
                extent_id = extent_config['id']
                with target_extent_associate(target_id, extent_id, lun) as associate_config:
                    newconfig = config.copy()
                    newconfig.update({
                        f'dataset_lun{lun}': dataset_config,
                        f'associate_lun{lun}': associate_config,
                        f'extent_lun{lun}': extent_config,
                    })
                    yield newconfig


@contextlib.contextmanager
def configured_target(config, name, extent_type, alias=None, extent_size=MB_512):
    assert extent_type in ["FILE", "VOLUME"]
    if extent_type == "FILE":
        ds_name = f"iscsids{name}"
        with configured_target_to_file_extent(config, name, pool_name, ds_name, file_name, alias, extent_size, name) as newconfig:
            yield newconfig
    elif extent_type == "VOLUME":
        zvol_name = f"ds{name}"
        zvol = f'{pool_name}/{zvol_name}'
        with configured_target_to_zvol_extent(config, name, zvol, alias, name, extent_size) as newconfig:
            yield newconfig


@contextlib.contextmanager
def isns_enabled(delay=5):
    payload = {'isns_servers': [isns_ip]}
    call('iscsi.global.update', payload)
    try:
        yield
    finally:
        payload = {'isns_servers': []}
        call('iscsi.global.update', payload)
        if delay:
            print(f'Sleeping for {delay} seconds after turning off iSNS')
            sleep(delay)


def TUR(s):
    """
    Perform a TEST UNIT READY.

    :param s: a pyscsi.SCSI instance
    """
    s.testunitready()
    # try:
    #     s.testunitready()
    # except TypeError:
    #     s.testunitready()


def expect_check_condition(s, text=None, check_type=CheckType.CHECK_CONDITION):
    """
    Expect a CHECK CONDITION containing the specified text.

    :param s: a pyscsi.SCSI instance
    :param text: string expected as part of the CHECK CONDITION
    :param check_type: CheckType enum of the expected CHECK_CONDITION

    Issue a TEST UNIT READY and verify that the expected CHECK CONDITION is raised.

    If this version of pyscsi(/cython-iscsi) does not support CHECK CONDITION
    then just swallow the condition by issuing another TEST UNIT READY.
    """
    assert check_type in CheckType, f"Parameter '{check_type}' is not a CheckType"
    if pyscsi_supports_check_condition:
        with pytest.raises(Exception) as excinfo:
            s.testunitready()

        e = excinfo.value
        assert e.__class__.__name__ == str(check_type), f"Unexpected CHECK CONDITION type.  Got '{e.__class__.__name__}', expected {str(check_type)}"
        if text:
            assert text in str(e), f"Exception did not match: {text}"
    else:
        # If we cannot detect a CHECK CONDITION, then swallow it by retrying a TUR
        try:
            s.testunitready()
        except TypeError:
            s.testunitready()


@contextlib.contextmanager
def raises_check_condition(sense_key, ascq):
    with pytest.raises(Exception) as excinfo:
        yield
    e = excinfo.value
    assert f"Check Condition: {sense_key_dict[sense_key]}(0x{sense_key:02X}) ASC+Q:{sense_ascq_dict[ascq]}(0x{ascq:04X})" == str(e)


def _verify_inquiry(s):
    """
    Verify that the supplied SCSI has the expected INQUIRY response.

    :param s: a pyscsi.SCSI instance
    """
    TUR(s)
    r = s.inquiry()
    data = r.result
    assert data['t10_vendor_identification'].decode('utf-8').startswith("TrueNAS"), str(data)
    assert data['product_identification'].decode('utf-8').startswith("iSCSI Disk"), str(data)


def get_target(targetid):
    """
    Return target JSON data.
    """
    return call('iscsi.target.get_instance', int(targetid))


def get_targets():
    """
    Return a dictionary of target JSON data, keyed by target name.
    """
    return {target['name']: target for target in call('iscsi.target.query')}


def modify_target(targetid, payload):
    call('iscsi.target.update', targetid, payload)


def set_target_alias(targetid, newalias):
    modify_target(targetid, {'alias': newalias})


def set_target_initiator_id(targetid, initiatorid):
    target_data = get_target(targetid)

    assert 'groups' in target_data, target_data
    groups = target_data['groups']
    assert len(groups) == 1, target_data

    groups[0]['initiator'] = initiatorid
    modify_target(targetid, {'groups': groups})


def _get_service(service_name='iscsitarget'):
    return call('service.query', [['service', '=', service_name]], {'get': True})


@pytest.mark.dependency(name="iscsi_cmd_00")
def test_00_setup(request):
    # Enable iSCSI service
    payload = {"enable": True}
    call('service.update', 'iscsitarget', payload)
    # Start iSCSI service
    call('service.start', 'iscsitarget')
    sleep(1)
    # Verify running
    service = _get_service()
    assert service['state'] == "RUNNING", service


def test_01_inquiry(request):
    """
    This tests the Vendor and Product information in an INQUIRY response
    are 'TrueNAS' and 'iSCSI Disk' respectively.
    """
    depends(request, ["iscsi_cmd_00"], scope="session")
    with initiator():
        with portal() as portal_config:
            portal_id = portal_config['id']
            with target(target_name, [{'portal': portal_id}]) as target_config:
                target_id = target_config['id']
                with dataset(dataset_name):
                    with file_extent(pool_name, dataset_name, file_name) as extent_config:
                        extent_id = extent_config['id']
                        with target_extent_associate(target_id, extent_id):
                            iqn = f'{basename}:{target_name}'
                            with iscsi_scsi_connection(truenas_server.ip, iqn) as s:
                                _verify_inquiry(s)


def test_02_read_capacity16(request):
    """
    This tests that the target created returns the correct size to READ CAPACITY (16).

    It performs this test with a couple of sizes for both file & zvol based targets.
    """
    depends(request, ["iscsi_cmd_00"], scope="session")
    with initiator():
        with portal() as portal_config:
            portal_id = portal_config['id']
            with target(target_name, [{'portal': portal_id}]) as target_config:
                target_id = target_config['id']
                with dataset(dataset_name):
                    # 100 MB file extent
                    with file_extent(pool_name, dataset_name, file_name, MB_100) as extent_config:
                        extent_id = extent_config['id']
                        with target_extent_associate(target_id, extent_id):
                            iqn = f'{basename}:{target_name}'
                            with iscsi_scsi_connection(truenas_server.ip, iqn) as s:
                                verify_capacity(s, MB_100)
                    # 512 MB file extent
                    with file_extent(pool_name, dataset_name, file_name, MB_512) as extent_config:
                        extent_id = extent_config['id']
                        with target_extent_associate(target_id, extent_id):
                            iqn = f'{basename}:{target_name}'
                            with iscsi_scsi_connection(truenas_server.ip, iqn) as s:
                                verify_capacity(s, MB_512)
                # 100 MB zvol extent
                with zvol_dataset(zvol, MB_100):
                    with zvol_extent(zvol) as extent_config:
                        extent_id = extent_config['id']
                        with target_extent_associate(target_id, extent_id):
                            iqn = f'{basename}:{target_name}'
                            with iscsi_scsi_connection(truenas_server.ip, iqn) as s:
                                verify_capacity(s, MB_100)
                # 512 MB zvol extent
                with zvol_dataset(zvol):
                    with zvol_extent(zvol) as extent_config:
                        extent_id = extent_config['id']
                        with target_extent_associate(target_id, extent_id):
                            iqn = f'{basename}:{target_name}'
                            with iscsi_scsi_connection(truenas_server.ip, iqn) as s:
                                verify_capacity(s, MB_512)


def target_test_readwrite16(ip, iqn):
    """
    This tests WRITE SAME (16), READ (16) and WRITE (16)
    operations on the specified target.
    """
    zeros = bytearray(512)
    deadbeef = bytearray.fromhex('deadbeef') * 128
    deadbeef_lbas = [1, 5, 7]

    with iscsi_scsi_connection(ip, iqn) as s:
        TUR(s)

        # First let's write zeros to the first 12 blocks using WRITE SAME (16)
        s.writesame16(0, 12, zeros)

        # Check results using READ (16)
        for lba in range(0, 12):
            r = s.read16(lba, 1)
            assert r.datain == zeros, r.datain

        # Now let's write DEADBEEF to a few LBAs using WRITE (16)
        for lba in deadbeef_lbas:
            s.write16(lba, 1, deadbeef)

        # Check results using READ (16)
        for lba in range(0, 12):
            r = s.read16(lba, 1)
            if lba in deadbeef_lbas:
                assert r.datain == deadbeef, r.datain
            else:
                assert r.datain == zeros, r.datain

    # Drop the iSCSI connection and login again
    with iscsi_scsi_connection(ip, iqn) as s:
        TUR(s)

        # Check results using READ (16)
        for lba in range(0, 12):
            r = s.read16(lba, 1)
            if lba in deadbeef_lbas:
                assert r.datain == deadbeef, r.datain
            else:
                assert r.datain == zeros, r.datain

        # Do a WRITE for > 1 LBA
        s.write16(10, 2, deadbeef * 2)

        # Check results using READ (16)
        deadbeef_lbas.extend([10, 11])
        for lba in range(0, 12):
            r = s.read16(lba, 1)
            if lba in deadbeef_lbas:
                assert r.datain == deadbeef, r.datain
            else:
                assert r.datain == zeros, r.datain

        # Do a couple of READ (16) for > 1 LBA
        # At this stage we have written deadbeef to LBAs 1,5,7,10,11
        r = s.read16(0, 2)
        assert r.datain == zeros + deadbeef, r.datain
        r = s.read16(1, 2)
        assert r.datain == deadbeef + zeros, r.datain
        r = s.read16(2, 2)
        assert r.datain == zeros * 2, r.datain
        r = s.read16(10, 2)
        assert r.datain == deadbeef * 2, r.datain


def test_03_readwrite16_file_extent(request):
    """
    This tests WRITE SAME (16), READ (16) and WRITE (16) operations with
    a file extent based iSCSI target.
    """
    depends(request, ["iscsi_cmd_00"], scope="session")
    with initiator_portal() as config:
        with configured_target_to_file_extent(config, target_name, pool_name, dataset_name, file_name):
            iqn = f'{basename}:{target_name}'
            target_test_readwrite16(truenas_server.ip, iqn)


def test_04_readwrite16_zvol_extent(request):
    """
    This tests WRITE SAME (16), READ (16) and WRITE (16) operations with
    a zvol extent based iSCSI target.
    """
    depends(request, ["iscsi_cmd_00"], scope="session")
    with initiator_portal() as config:
        with configured_target_to_zvol_extent(config, target_name, zvol):
            iqn = f'{basename}:{target_name}'
            target_test_readwrite16(truenas_server.ip, iqn)


@skip_invalid_initiatorname
def test_05_chap(request):
    """
    This tests that CHAP auth operates as expected.
    """
    depends(request, ["iscsi_cmd_00"], scope="session")
    user = "user1"
    secret = 'sec1' + ''.join(random.choices(string.ascii_uppercase + string.ascii_lowercase + string.digits, k=10))
    with initiator():
        with portal() as portal_config:
            portal_id = portal_config['id']
            auth_tag = 1
            with iscsi_auth(auth_tag, user, secret):
                with target(target_name, [{'portal': portal_id, 'authmethod': 'CHAP', 'auth': auth_tag}]) as target_config:
                    target_id = target_config['id']
                    with dataset(dataset_name):
                        with file_extent(pool_name, dataset_name, file_name) as extent_config:
                            extent_id = extent_config['id']
                            with target_extent_associate(target_id, extent_id):
                                iqn = f'{basename}:{target_name}'

                                # Try and fail to connect without supplying CHAP creds
                                with pytest.raises(RuntimeError) as ve:
                                    with iscsi_scsi_connection(truenas_server.ip, iqn) as s:
                                        TUR(s)
                                        assert False, "Should not have been able to connect without CHAP credentials."
                                assert 'Unable to connect to' in str(ve), ve

                                # Try and fail to connect supplying incorrect CHAP creds
                                with pytest.raises(RuntimeError) as ve:
                                    with iscsi_scsi_connection(truenas_server.ip, iqn, 0, user, "WrongSecret") as s:
                                        TUR(s)
                                        assert False, "Should not have been able to connect without CHAP credentials."
                                assert 'Unable to connect to' in str(ve), ve

                                # Finally ensure we can connect with the right CHAP creds
                                with iscsi_scsi_connection(truenas_server.ip, iqn, 0, user, secret) as s:
                                    _verify_inquiry(s)


@skip_invalid_initiatorname
def test_06_mutual_chap(request):
    """
    This tests that Mutual CHAP auth operates as expected.
    """
    depends(request, ["iscsi_cmd_00"], scope="session")
    user = "user1"
    secret = 'sec1' + ''.join(random.choices(string.ascii_uppercase + string.ascii_lowercase + string.digits, k=10))
    peer_user = "user2"
    peer_secret = 'sec2' + ''.join(random.choices(string.ascii_uppercase + string.ascii_lowercase + string.digits, k=10))
    with initiator():
        with portal() as portal_config:
            portal_id = portal_config['id']
            auth_tag = 1
            with iscsi_auth(auth_tag, user, secret, peer_user, peer_secret):
                with target(target_name, [{'portal': portal_id, 'authmethod': 'CHAP_MUTUAL', 'auth': auth_tag}]) as target_config:
                    target_id = target_config['id']
                    with dataset(dataset_name):
                        with file_extent(pool_name, dataset_name, file_name) as extent_config:
                            extent_id = extent_config['id']
                            with target_extent_associate(target_id, extent_id):
                                iqn = f'{basename}:{target_name}'

                                # Try and fail to connect without supplying Mutual CHAP creds
                                with pytest.raises(RuntimeError) as ve:
                                    with iscsi_scsi_connection(truenas_server.ip, iqn) as s:
                                        TUR(s)
                                        assert False, "Should not have been able to connect without CHAP credentials."
                                assert 'Unable to connect to' in str(ve), ve

                                # Try and fail to connect supplying incorrect CHAP creds (not mutual)
                                with pytest.raises(RuntimeError) as ve:
                                    with iscsi_scsi_connection(truenas_server.ip, iqn, 0, user, "WrongSecret") as s:
                                        TUR(s)
                                        assert False, "Should not have been able to connect with incorrect CHAP credentials."
                                assert 'Unable to connect to' in str(ve), ve

                                # Ensure we can connect with the right CHAP creds, if we *choose* not
                                # to validate things.
                                with iscsi_scsi_connection(truenas_server.ip, iqn, 0, user, secret) as s:
                                    _verify_inquiry(s)

                                # Try and fail to connect supplying incorrect Mutual CHAP creds
                                with pytest.raises(RuntimeError) as ve:
                                    with iscsi_scsi_connection(truenas_server.ip, iqn, 0, user, secret, peer_user, "WrongSecret") as s:
                                        TUR(s)
                                        assert False, "Should not have been able to connect with incorrect Mutual CHAP credentials."
                                assert 'Unable to connect to' in str(ve), ve

                                # Finally ensure we can connect with the right Mutual CHAP creds
                                with iscsi_scsi_connection(truenas_server.ip, iqn, 0, user, secret, peer_user, peer_secret) as s:
                                    _verify_inquiry(s)


def test_07_report_luns(request):
    """
    This tests REPORT LUNS and accessing multiple LUNs on a target.
    """
    depends(request, ["iscsi_cmd_00"], scope="session")
    iqn = f'{basename}:{target_name}'
    with initiator():
        with portal() as portal_config:
            portal_id = portal_config['id']
            with target(target_name, [{'portal': portal_id}]) as target_config:
                target_id = target_config['id']
                with dataset(dataset_name):
                    # LUN 0 (100 MB file extent)
                    with file_extent(pool_name, dataset_name, file_name, MB_100) as extent_config:
                        extent_id = extent_config['id']
                        with target_extent_associate(target_id, extent_id):
                            with iscsi_scsi_connection(truenas_server.ip, iqn) as s:
                                verify_luns(s, [0])
                                verify_capacity(s, MB_100)
                            # Now create a 512 MB zvol and associate with LUN 1
                            with zvol_dataset(zvol):
                                with zvol_extent(zvol) as extent_config:
                                    extent_id = extent_config['id']
                                    with target_extent_associate(target_id, extent_id, 1):
                                        # Connect to LUN 0
                                        with iscsi_scsi_connection(truenas_server.ip, iqn, 0) as s0:
                                            verify_luns(s0, [0, 1])
                                            verify_capacity(s0, MB_100)
                                        # Connect to LUN 1
                                        with iscsi_scsi_connection(truenas_server.ip, iqn, 1) as s1:
                                            verify_luns(s1, [0, 1])
                                            verify_capacity(s1, MB_512)
                            # Check again now that LUN 1 has been removed again.
                            with iscsi_scsi_connection(truenas_server.ip, iqn) as s:
                                verify_luns(s, [0])
                                verify_capacity(s, MB_100)


def target_test_snapshot_single_login(ip, iqn, dataset_id):
    """
    This tests snapshots with an iSCSI target using a single
    iSCSI session.
    """
    zeros = bytearray(512)
    deadbeef = bytearray.fromhex('deadbeef') * 128
    deadbeef_lbas = [1, 5, 7]
    all_deadbeef_lbas = [1, 5, 7, 10, 11]

    with iscsi_scsi_connection(ip, iqn) as s:
        TUR(s)

        # First let's write zeros to the first 12 blocks using WRITE SAME (16)
        s.writesame16(0, 12, zeros)

        # Check results using READ (16)
        for lba in range(0, 12):
            r = s.read16(lba, 1)
            assert r.datain == zeros, r.datain

        # Take snap0
        with snapshot(dataset_id, "snap0", get=True) as snap0_config:

            # Now let's write DEADBEEF to a few LBAs using WRITE (16)
            for lba in deadbeef_lbas:
                s.write16(lba, 1, deadbeef)

            # Check results using READ (16)
            for lba in range(0, 12):
                r = s.read16(lba, 1)
                if lba in deadbeef_lbas:
                    assert r.datain == deadbeef, r.datain
                else:
                    assert r.datain == zeros, r.datain

            # Take snap1
            with snapshot(dataset_id, "snap1", get=True) as snap1_config:

                # Do a WRITE for > 1 LBA
                s.write16(10, 2, deadbeef * 2)

                # Check results using READ (16)
                for lba in range(0, 12):
                    r = s.read16(lba, 1)
                    if lba in all_deadbeef_lbas:
                        assert r.datain == deadbeef, r.datain
                    else:
                        assert r.datain == zeros, r.datain

                # Now revert to snap1
                snapshot_rollback(snap1_config['id'])

                # Check results using READ (16)
                for lba in range(0, 12):
                    r = s.read16(lba, 1)
                    if lba in deadbeef_lbas:
                        assert r.datain == deadbeef, r.datain
                    else:
                        assert r.datain == zeros, r.datain

            # Now revert to snap0
            snapshot_rollback(snap0_config['id'])

            # Check results using READ (16)
            for lba in range(0, 12):
                r = s.read16(lba, 1)
                assert r.datain == zeros, r.datain


def target_test_snapshot_multiple_login(ip, iqn, dataset_id):
    """
    This tests snapshots with an iSCSI target using multiple
    iSCSI sessions.
    """
    zeros = bytearray(512)
    deadbeef = bytearray.fromhex('deadbeef') * 128
    deadbeef_lbas = [1, 5, 7]
    all_deadbeef_lbas = [1, 5, 7, 10, 11]

    with iscsi_scsi_connection(ip, iqn) as s:
        TUR(s)

        # First let's write zeros to the first 12 blocks using WRITE SAME (16)
        s.writesame16(0, 12, zeros)

        # Check results using READ (16)
        for lba in range(0, 12):
            r = s.read16(lba, 1)
            assert r.datain == zeros, r.datain

    # Take snap0
    with snapshot(dataset_id, "snap0", get=True) as snap0_config:

        with iscsi_scsi_connection(ip, iqn) as s:
            TUR(s)

            # Now let's write DEADBEEF to a few LBAs using WRITE (16)
            for lba in deadbeef_lbas:
                s.write16(lba, 1, deadbeef)

            # Check results using READ (16)
            for lba in range(0, 12):
                r = s.read16(lba, 1)
                if lba in deadbeef_lbas:
                    assert r.datain == deadbeef, r.datain
                else:
                    assert r.datain == zeros, r.datain

        # Take snap1
        with snapshot(dataset_id, "snap1", get=True) as snap1_config:

            with iscsi_scsi_connection(ip, iqn) as s:
                TUR(s)

                # Do a WRITE for > 1 LBA
                s.write16(10, 2, deadbeef * 2)

                # Check results using READ (16)
                for lba in range(0, 12):
                    r = s.read16(lba, 1)
                    if lba in all_deadbeef_lbas:
                        assert r.datain == deadbeef, r.datain
                    else:
                        assert r.datain == zeros, r.datain

                # Now revert to snap1
                snapshot_rollback(snap1_config['id'])

        with iscsi_scsi_connection(ip, iqn) as s:
            TUR(s)

            # Check results using READ (16)
            for lba in range(0, 12):
                r = s.read16(lba, 1)
                if lba in deadbeef_lbas:
                    assert r.datain == deadbeef, r.datain
                else:
                    assert r.datain == zeros, r.datain

        # Now revert to snap0
        snapshot_rollback(snap0_config['id'])

        with iscsi_scsi_connection(ip, iqn) as s:
            TUR(s)
            # Check results using READ (16)
            for lba in range(0, 12):
                r = s.read16(lba, 1)
                assert r.datain == zeros, r.datain


def test_08_snapshot_zvol_extent(request):
    """
    This tests snapshots with a zvol extent based iSCSI target.
    """
    depends(request, ["iscsi_cmd_00"], scope="session")
    iqn = f'{basename}:{target_name}'
    with initiator_portal() as config:
        with configured_target_to_zvol_extent(config, target_name, zvol) as iscsi_config:
            target_test_snapshot_single_login(truenas_server.ip, iqn, iscsi_config['dataset'])
        with configured_target_to_zvol_extent(config, target_name, zvol) as iscsi_config:
            target_test_snapshot_multiple_login(truenas_server.ip, iqn, iscsi_config['dataset'])


def test_09_snapshot_file_extent(request):
    """
    This tests snapshots with a file extent based iSCSI target.
    """
    depends(request, ["iscsi_cmd_00"], scope="session")
    iqn = f'{basename}:{target_name}'
    with initiator_portal() as config:
        with configured_target_to_file_extent(config, target_name, pool_name, dataset_name, file_name) as iscsi_config:
            target_test_snapshot_single_login(truenas_server.ip, iqn, iscsi_config['dataset'])
        with configured_target_to_zvol_extent(config, target_name, zvol) as iscsi_config:
            target_test_snapshot_multiple_login(truenas_server.ip, iqn, iscsi_config['dataset'])


def test_10_target_alias(request):
    """
    This tests iSCSI target alias.

    At the moment SCST does not use the alias usefully (e.g. TargetAlias in
    LOGIN response).  When this is rectified this test should be extended.
    """
    depends(request, ["iscsi_cmd_00"], scope="session")

    data = {}
    for t in ["A", "B"]:
        data[t] = {}
        data[t]['name'] = f"{target_name}{t.lower()}"
        data[t]['alias'] = f"{target_name}{t}_alias"
        data[t]['file'] = f"{target_name}{t}_file"

    A = data['A']
    B = data['B']
    with initiator_portal() as config:
        with configured_target_to_file_extent(config, A['name'], pool_name, dataset_name, A['file'], A['alias']) as iscsi_config:
            with target(B['name'], [{'portal': iscsi_config['portal']['id']}]) as targetB_config:
                with file_extent(pool_name, dataset_name, B['file'], extent_name="extentB") as extentB_config:
                    with target_extent_associate(targetB_config['id'], extentB_config['id']):
                        # Created two targets, one with an alias, one without.  Check them.
                        targets = get_targets()
                        assert targets[A['name']]['alias'] == A['alias'], targets[A['name']]['alias']
                        assert targets[B['name']]['alias'] is None, targets[B['name']]['alias']

                        # Update alias for B
                        set_target_alias(targets[B['name']]['id'], B['alias'])
                        targets = get_targets()
                        assert targets[A['name']]['alias'] == A['alias'], targets[A['name']]['alias']
                        assert targets[B['name']]['alias'] == B['alias'], targets[B['name']]['alias']

                        # Clear alias for A
                        set_target_alias(targets[A['name']]['id'], "")
                        targets = get_targets()
                        assert targets[A['name']]['alias'] is None, targets[A['name']]['alias']
                        assert targets[B['name']]['alias'] == B['alias'], targets[B['name']]['alias']

                        # Clear alias for B
                        set_target_alias(targets[B['name']]['id'], "")
                        targets = get_targets()
                        assert targets[A['name']]['alias'] is None, targets[A['name']]['alias']
                        assert targets[B['name']]['alias'] is None, targets[B['name']]['alias']


def test_11_modify_portal(request):
    """
    Test that we can modify a target portal.
    """
    depends(request, ["iscsi_cmd_00"], scope="session")
    with portal() as portal_config:
        assert portal_config['comment'] == 'Default portal', portal_config
        # First just change the comment
        payload = {'comment': 'New comment'}
        call('iscsi.portal.update', portal_config['id'], payload)
        new_config = call('iscsi.portal.get_instance', portal_config['id'])
        assert new_config['comment'] == 'New comment', new_config
        # Then try to reapply everything
        payload = {'comment': 'test1', 'discovery_authmethod': 'NONE', 'discovery_authgroup': None, 'listen': [{'ip': '0.0.0.0'}]}
        # payload = {'comment': 'test1', 'discovery_authmethod': 'NONE', 'discovery_authgroup': None, 'listen': [{'ip': '0.0.0.0'}, {'ip': '::'}]}
        call('iscsi.portal.update', portal_config['id'], payload)
        new_config = call('iscsi.portal.get_instance', portal_config['id'])
        assert new_config['comment'] == 'test1', new_config


def test_12_pblocksize_setting(request):
    """
    This tests whether toggling pblocksize has the desired result on READ CAPACITY 16, i.e.
    whether setting it results in LOGICAL BLOCKS PER PHYSICAL BLOCK EXPONENT being zero.
    """
    depends(request, ["iscsi_cmd_00"], scope="session")
    iqn = f'{basename}:{target_name}'
    with initiator_portal() as config:
        with configured_target_to_file_extent(config, target_name, pool_name, dataset_name, file_name) as iscsi_config:
            extent_config = iscsi_config['extent']
            with iscsi_scsi_connection(truenas_server.ip, iqn) as s:
                TUR(s)
                data = s.readcapacity16().result
                # By default 512 << 3 == 4096
                assert data['lbppbe'] == 3, data

                # First let's just change the blocksize to 2K
                payload = {'blocksize': 2048}
                call('iscsi.extent.update', extent_config['id'], payload)

                expect_check_condition(s, sense_ascq_dict[0x2900])  # "POWER ON, RESET, OR BUS DEVICE RESET OCCURRED"

                data = s.readcapacity16().result
                assert data['block_length'] == 2048, data
                assert data['lbppbe'] == 1, data

                # Now let's change it back to 512, but also set pblocksize
                payload = {'blocksize': 512, 'pblocksize': True}
                call('iscsi.extent.update', extent_config['id'], payload)

                expect_check_condition(s, sense_ascq_dict[0x2900])  # "POWER ON, RESET, OR BUS DEVICE RESET OCCURRED"

                data = s.readcapacity16().result
                assert data['block_length'] == 512, data
                assert data['lbppbe'] == 0, data

        with configured_target_to_zvol_extent(config, target_name, zvol) as iscsi_config:
            extent_config = iscsi_config['extent']
            with iscsi_scsi_connection(truenas_server.ip, iqn) as s:
                TUR(s)
                data = s.readcapacity16().result
                # We created a vol with volblocksize == 16K (512 << 5)
                assert data['lbppbe'] == 5, data

                # First let's just change the blocksize to 4K
                payload = {'blocksize': 4096}
                call('iscsi.extent.update', extent_config['id'], payload)

                expect_check_condition(s, sense_ascq_dict[0x2900])  # "POWER ON, RESET, OR BUS DEVICE RESET OCCURRED"

                data = s.readcapacity16().result
                assert data['block_length'] == 4096, data
                assert data['lbppbe'] == 2, data

                # Now let's also set pblocksize
                payload = {'pblocksize': True}
                call('iscsi.extent.update', extent_config['id'], payload)

                TUR(s)
                data = s.readcapacity16().result
                assert data['block_length'] == 4096, data
                assert data['lbppbe'] == 0, data


def generate_name(length, base="target"):
    result = f"{base}-{length}-"
    remaining = length - len(result)
    assert remaining >= 0, f"Function not suitable for such a short length: {length}"
    return result + ''.join(random.choices(string.ascii_lowercase + string.digits, k=remaining))


@pytest.mark.parametrize('extent_type', ["FILE", "VOLUME"])
def test_13_test_target_name(request, extent_type):
    """
    Test the user-supplied target name.
    """
    depends(request, ["iscsi_cmd_00"], scope="session")

    with initiator_portal() as config:
        name64 = generate_name(64)
        with configured_target(config, name64, extent_type):
            iqn = f'{basename}:{name64}'
            target_test_readwrite16(truenas_server.ip, iqn)

        name65 = generate_name(65)
        with pytest.raises(ValidationErrors) as ve:
            with configured_target(config, name65, extent_type):
                assert False, f"Should not have been able to create a target with name length {len(name65)}."
        assert ve.value.errors == [
            ValidationError('iscsi_extent_create.name', 'The value may not be longer than 64 characters', errno.EINVAL),
        ]


@pytest.mark.parametrize('extent_type', ["FILE", "VOLUME"])
def test_14_target_lun_extent_modify(request, extent_type):
    """
    Perform some tests of the iscsi.targetextent.update API, including
    trying tp provide invalid
    """
    depends(request, ["iscsi_cmd_00"], scope="session")

    name1 = f'{target_name}1'
    name2 = f'{target_name}2'
    name3 = f'{target_name}3'
    name4 = f'{target_name}4'

    @contextlib.contextmanager
    def expect_lun_in_use_failure():
        with pytest.raises(ValidationErrors) as ve:
            yield
            assert False, "Should not be able to associate because LUN in use"
        assert "LUN ID is already being used for this target." in str(ve.value)

    @contextlib.contextmanager
    def expect_extent_in_use_failure():
        with pytest.raises(ValidationErrors) as ve:
            yield
            assert False, "Should not be able to associate because extent in use"
        assert "Extent is already in use" in str(ve.value)

    # The following will create the extents with the same name as the target.
    with initiator_portal() as config:
        with configured_target(config, name1, extent_type) as config1:
            with configured_target(config, name2, extent_type) as config2:
                with configured_target(config, name3, extent_type) as config3:
                    # Create an extra extent to 'play' with
                    with zvol_dataset(zvol):
                        with zvol_extent(zvol, extent_name=name4) as config4:
                            # First we will attempt some new, but invalid associations

                            # LUN in use
                            with expect_lun_in_use_failure():
                                payload = {
                                    'target': config1['target']['id'],
                                    'lunid': 0,
                                    'extent': config4['id']
                                }
                                call('iscsi.targetextent.create', payload)

                            # extent in use
                            with expect_extent_in_use_failure():
                                payload = {
                                    'target': config1['target']['id'],
                                    'lunid': 1,
                                    'extent': config2['extent']['id']
                                }
                                call('iscsi.targetextent.create', payload)

                            # Now succeed in creating a new target/lun/extent association
                            payload = {
                                'target': config1['target']['id'],
                                'lunid': 1,
                                'extent': config4['id']
                            }
                            call('iscsi.targetextent.create', payload)

                            # Get the current config
                            textents = call('iscsi.targetextent.query')

                            # Now perform some updates that will not succeed
                            textent4 = next(textent for textent in textents if textent['extent'] == config4['id'])

                            # Attempt some invalid updates
                            # LUN in use
                            with expect_lun_in_use_failure():
                                payload = {
                                    'target': textent4['target'],
                                    'lunid': 0,
                                    'extent': textent4['extent']
                                }
                                call('iscsi.targetextent.update', textent4['id'], payload)

                            # extent in use in another target
                            with expect_extent_in_use_failure():
                                payload = {
                                    'target': textent4['target'],
                                    'lunid': textent4['lunid'],
                                    'extent': config3['extent']['id']
                                }
                                call('iscsi.targetextent.update', textent4['id'], payload)

                            # extent in use in this target
                            with expect_extent_in_use_failure():
                                payload = {
                                    'target': textent4['target'],
                                    'lunid': textent4['lunid'],
                                    'extent': config1['extent']['id']
                                }
                                call('iscsi.targetextent.update', textent4['id'], payload)

                            # Move a target to LUN 1
                            textent2 = next(textent for textent in textents if textent['extent'] == config2['extent']['id'])
                            payload = {
                                'target': textent2['target'],
                                'lunid': 1,
                                'extent': textent2['extent']
                            }
                            call('iscsi.targetextent.update', textent2['id'], payload)

                            # Try to move it (to target1) just by changing the target, will clash
                            with expect_lun_in_use_failure():
                                payload = {
                                    'target': config1['target']['id'],
                                    'lunid': 1,
                                    'extent': textent2['extent']
                                }
                                call('iscsi.targetextent.update', textent2['id'], payload)

                            # But can move it elsewhere (target3)
                            payload = {
                                'target': config3['target']['id'],
                                'lunid': 1,
                                'extent': textent2['extent']
                            }
                            call('iscsi.targetextent.update', textent2['id'], payload)

                            # Delete textent4 association
                            call('iscsi.targetextent.delete', textent4['id'])

                            # Now can do the move that previously failed
                            payload = {
                                'target': config1['target']['id'],
                                'lunid': 1,
                                'extent': textent2['extent']
                            }
                            call('iscsi.targetextent.update', textent2['id'], payload)

                            # Restore it
                            payload = {
                                'target': config2['target']['id'],
                                'lunid': 0,
                                'extent': textent2['extent']
                            }
                            call('iscsi.targetextent.update', textent2['id'], payload)


def _isns_wait_for_iqn(isns_client, iqn, timeout=10):
    iqns = set(isns_client.list_targets())
    while timeout > 0 and iqn not in iqns:
        sleep(1)
        iqns = set(isns_client.list_targets())
    return iqns


def test_15_test_isns(request):
    """
    Test ability to register targets with iSNS.
    """
    # Will use a more unique target name than usual, just in case several test
    # runs are hitting the same iSNS server at the same time.
    depends(request, ["iscsi_cmd_00"], scope="session")
    _host = socket.gethostname()
    _rand = ''.join(random.choices(string.digits + string.ascii_lowercase, k=12))
    _name_base = f'isnstest:{_host}:{_rand}'
    _target1 = f'{_name_base}:1'
    _target2 = f'{_name_base}:2'
    _initiator = f'iqn.2005-10.org.freenas.ctl:isnstest:{_name_base}:initiator'
    _iqn1 = f'{basename}:{_target1}'
    _iqn2 = f'{basename}:{_target1}'

    with isns_connection(isns_ip, _initiator) as isns_client:
        # First let's ensure that the targets are not already present.
        base_iqns = set(isns_client.list_targets())
        for iqn in [_iqn1, _iqn2]:
            assert iqn not in base_iqns, iqn

        # Create target1 and ensure it is still not present (because we
        # haven't switched on iSNS yet).
        with initiator_portal() as config:
            with configured_target_to_file_extent(config,
                                                  _target1,
                                                  pool_name,
                                                  dataset_name,
                                                  file_name) as iscsi_config:
                iqns = set(isns_client.list_targets())
                assert _iqn1 not in iqns, _iqn1

                # Now turn on the iSNS server
                with isns_enabled():
                    iqns = _isns_wait_for_iqn(isns_client, _iqn1)
                    assert _iqn1 in iqns, _iqn1

                    # Create another target and ensure it shows up too
                    with target(_target2,
                                [{'portal': iscsi_config['portal']['id']}]
                                ) as target2_config:
                        target_id = target2_config['id']
                        with zvol_dataset(zvol):
                            with zvol_extent(zvol) as extent_config:
                                extent_id = extent_config['id']
                                with target_extent_associate(target_id, extent_id):
                                    iqns = _isns_wait_for_iqn(isns_client, _iqn2)
                                    for inq in [_iqn1, _iqn2]:
                                        assert iqn in iqns, iqn

                # Now that iSNS is disabled again, ensure that our target is
                # no longer advertised
                iqns = set(isns_client.list_targets())
                assert _iqn1 not in iqns, _iqn1

        # Finally let's ensure that neither target is present.
        base_iqns = set(isns_client.list_targets())
        for iqn in [_iqn1, _iqn2]:
            assert iqn not in base_iqns, iqn


class TestFixtureInitiatorName:
    """Fixture for test_16_invalid_initiator_name"""

    iqn = f'{basename}:{target_name}'

    @pytest.fixture(scope='class')
    def create_target(self):
        with initiator_portal() as config:
            with configured_target(config, target_name, "FILE"):
                yield

    params = [
        (None, True),
        ("iqn.1991-05.com.microsoft:fake-host", True),
        ("iqn.1991-05.com.microsoft:fake-/-host", False),
        ("iqn.1991-05.com.microsoft:fake-#-host", False),
        ("iqn.1991-05.com.microsoft:fake-%s-host", False),
        ("iqn.1991-05.com.microsoft:unicode-\u6d4b\u8bd5-ok", True),        # 测试
        ("iqn.1991-05.com.microsoft:unicode-\u30c6\u30b9\u30c8-ok", True),  # テスト
        ("iqn.1991-05.com.microsoft:unicode-\u180E-bad", False),            # Mongolian vowel separator
        ("iqn.1991-05.com.microsoft:unicode-\u2009-bad", False),            # Thin Space
        ("iqn.1991-05.com.microsoft:unicode-\uFEFF-bad", False),            # Zero width no-break space
    ]

    @pytest.mark.parametrize("initiator_name, expected", params)
    def test_16_invalid_initiator_name(self, request, create_target, initiator_name, expected):
        """
        Deliberately send SCST some invalid initiator names and ensure it behaves OK.
        """
        depends(request, ["iscsi_cmd_00"], scope="session")

        if expected:
            with iscsi_scsi_connection(truenas_server.ip, TestFixtureInitiatorName.iqn, initiator_name=initiator_name) as s:
                _verify_inquiry(s)
        else:
            with pytest.raises(RuntimeError) as ve:
                with iscsi_scsi_connection(truenas_server.ip, TestFixtureInitiatorName.iqn, initiator_name=initiator_name) as s:
                    assert False, "Should not have been able to connect with invalid initiator name."
                assert 'Unable to connect to' in str(ve), ve


def _pr_check_registered_keys(s, expected=[]):
    opcodes = s.device.opcodes
    data = s.persistentreservein(opcodes.PERSISTENT_RESERVE_IN.serviceaction.READ_KEYS)
    assert len(data.result['reservation_keys']) == len(expected), data.result
    if len(expected):
        expected_set = set(expected)
        received_set = set(data.result['reservation_keys'])
        assert expected_set == received_set, received_set
    return data.result


def _pr_check_reservation(s, expected={'reservation_key': None, 'scope': None, 'type': None}):
    opcodes = s.device.opcodes
    data = s.persistentreservein(opcodes.PERSISTENT_RESERVE_IN.serviceaction.READ_RESERVATION)
    for key, value in expected.items():
        actual_value = data.result.get(key)
        assert value == actual_value, data.result
    return data.result


def _pr_register_key(s, value):
    opcodes = s.device.opcodes
    s.persistentreserveout(opcodes.PERSISTENT_RESERVE_OUT.serviceaction.REGISTER,
                           service_action_reservation_key=value)


def _pr_unregister_key(s, value):
    opcodes = s.device.opcodes
    s.persistentreserveout(opcodes.PERSISTENT_RESERVE_OUT.serviceaction.REGISTER,
                           reservation_key=value,
                           service_action_reservation_key=0)


def _pr_reserve(s, pr_type, scope=LU_SCOPE, **kwargs):
    opcodes = s.device.opcodes
    s.persistentreserveout(opcodes.PERSISTENT_RESERVE_OUT.serviceaction.RESERVE,
                           scope=scope,
                           pr_type=pr_type,
                           **kwargs)


def _pr_release(s, pr_type, scope=LU_SCOPE, **kwargs):
    opcodes = s.device.opcodes
    s.persistentreserveout(opcodes.PERSISTENT_RESERVE_OUT.serviceaction.RELEASE,
                           scope=scope,
                           pr_type=pr_type,
                           **kwargs)


@contextlib.contextmanager
def _pr_registration(s, key):
    _pr_register_key(s, key)
    try:
        yield
    finally:
        _pr_unregister_key(s, key)
        # There is room for improvement here wrt SPC-5 5.14.11.2.3, but not urgent as
        # we are hygenic wrt releasing reservations before unregistering keys


@contextlib.contextmanager
def _pr_reservation(s, pr_type, scope=LU_SCOPE, other_connections=[], **kwargs):
    assert s not in other_connections, "Invalid parameter mix"
    _pr_reserve(s, pr_type, scope, **kwargs)
    try:
        yield
    finally:
        _pr_release(s, pr_type, scope, **kwargs)
        # Do processing as specified by SPC-5 5.14.11.2.2 Releasing
        # For the time being we will ignore the NUAR bit from SPC-5 7.5.11 Control mode page
        if pr_type in [PR_TYPE.WRITE_EXCLUSIVE_REGISTRANTS_ONLY,
                       PR_TYPE.EXCLUSIVE_ACCESS_REGISTRANTS_ONLY,
                       PR_TYPE.WRITE_EXCLUSIVE_ALL_REGISTRANTS,
                       PR_TYPE.EXCLUSIVE_ACCESS_ALL_REGISTRANTS]:
            sleep(5)
            for s2 in other_connections:
                expect_check_condition(s2, sense_ascq_dict[0x2A04])  # "RESERVATIONS RELEASED"


@skip_persistent_reservations
@pytest.mark.dependency(name="iscsi_basic_persistent_reservation")
def test_17_basic_persistent_reservation(request):
    depends(request, ["iscsi_cmd_00"], scope="session")
    with initiator_portal() as config:
        with configured_target_to_zvol_extent(config, target_name, zvol):
            iqn = f'{basename}:{target_name}'
            with iscsi_scsi_connection(truenas_server.ip, iqn) as s:
                TUR(s)

                _pr_check_registered_keys(s, [])
                _pr_check_reservation(s)

                with _pr_registration(s, PR_KEY1):
                    _pr_check_registered_keys(s, [PR_KEY1])
                    _pr_check_reservation(s)

                    with _pr_reservation(s, PR_TYPE.WRITE_EXCLUSIVE, reservation_key=PR_KEY1):
                        _pr_check_registered_keys(s, [PR_KEY1])
                        _pr_check_reservation(s, {'reservation_key': PR_KEY1, 'scope': LU_SCOPE, 'type': PR_TYPE.WRITE_EXCLUSIVE})

                    _pr_check_registered_keys(s, [PR_KEY1])
                    _pr_check_reservation(s)

                _pr_check_registered_keys(s, [])
                _pr_check_reservation(s)


@contextlib.contextmanager
def _pr_expect_reservation_conflict(s):
    try:
        yield
        assert False, "Failed to get expected PERSISTENT CONFLICT"
    except Exception as e:
        if e.__class__.__name__ != str(CheckType.RESERVATION_CONFLICT):
            raise e


def _check_target_rw_paths(s1, s2):
    """
    Check that the two supplied paths can read/write data, and they point at the same LUN.
    """
    zeros = bytearray(512)
    deadbeef = bytearray.fromhex('deadbeef') * 128
    abba = bytearray.fromhex('abbaabba') * 128

    # First let's write zeros to the first 12 blocks using WRITE SAME (16)
    s1.writesame16(0, 12, zeros)

    # Check results using READ (16)
    for s in (s1, s2):
        for lba in range(0, 12):
            r = s.read16(lba, 1)
            assert r.datain == zeros, r.datain

    # Update some blocks from each initiator using WRITE SAME
    s1.writesame16(0, 6, deadbeef)
    s2.writesame16(6, 6, abba)

    # Check results using READ (16)
    for s in (s1, s2):
        for lba in range(0, 6):
            r = s.read16(lba, 1)
            assert r.datain == deadbeef, r.datain
        for lba in range(6, 12):
            r = s.read16(lba, 1)
            assert r.datain == abba, r.datain


def _check_persistent_reservations(s1, s2):
    #
    # First just do a some basic tests (register key, reserve, release, unregister key)
    #
    _pr_check_registered_keys(s1, [])
    _pr_check_reservation(s1)
    _pr_check_registered_keys(s2, [])
    _pr_check_reservation(s2)

    with _pr_registration(s1, PR_KEY1):
        _pr_check_registered_keys(s1, [PR_KEY1])
        _pr_check_reservation(s1)
        _pr_check_registered_keys(s2, [PR_KEY1])
        _pr_check_reservation(s2)

        with _pr_reservation(s1, PR_TYPE.WRITE_EXCLUSIVE, reservation_key=PR_KEY1, other_connections=[s2]):
            _pr_check_registered_keys(s1, [PR_KEY1])
            _pr_check_reservation(s1, {'reservation_key': PR_KEY1, 'scope': LU_SCOPE, 'type': PR_TYPE.WRITE_EXCLUSIVE})
            _pr_check_registered_keys(s2, [PR_KEY1])
            _pr_check_reservation(s2, {'reservation_key': PR_KEY1, 'scope': LU_SCOPE, 'type': PR_TYPE.WRITE_EXCLUSIVE})

        _pr_check_registered_keys(s1, [PR_KEY1])
        _pr_check_reservation(s1)
        _pr_check_registered_keys(s2, [PR_KEY1])
        _pr_check_reservation(s2)

        with _pr_registration(s2, PR_KEY2):
            _pr_check_registered_keys(s1, [PR_KEY1, PR_KEY2])
            _pr_check_reservation(s1)
            _pr_check_registered_keys(s2, [PR_KEY1, PR_KEY2])
            _pr_check_reservation(s2)

            with _pr_reservation(s1, PR_TYPE.WRITE_EXCLUSIVE, reservation_key=PR_KEY1, other_connections=[s2]):
                _pr_check_registered_keys(s1, [PR_KEY1, PR_KEY2])
                _pr_check_reservation(s1, {'reservation_key': PR_KEY1, 'scope': LU_SCOPE, 'type': PR_TYPE.WRITE_EXCLUSIVE})
                _pr_check_registered_keys(s2, [PR_KEY1, PR_KEY2])
                _pr_check_reservation(s2, {'reservation_key': PR_KEY1, 'scope': LU_SCOPE, 'type': PR_TYPE.WRITE_EXCLUSIVE})

            _pr_check_registered_keys(s1, [PR_KEY1, PR_KEY2])
            _pr_check_reservation(s1)
            _pr_check_registered_keys(s2, [PR_KEY1, PR_KEY2])
            _pr_check_reservation(s2)

            with _pr_reservation(s2, PR_TYPE.WRITE_EXCLUSIVE_REGISTRANTS_ONLY, reservation_key=PR_KEY2, other_connections=[s1]):
                _pr_check_registered_keys(s1, [PR_KEY1, PR_KEY2])
                _pr_check_reservation(s1, {'reservation_key': PR_KEY2, 'scope': LU_SCOPE, 'type': PR_TYPE.WRITE_EXCLUSIVE_REGISTRANTS_ONLY})
                _pr_check_registered_keys(s2, [PR_KEY1, PR_KEY2])
                _pr_check_reservation(s2, {'reservation_key': PR_KEY2, 'scope': LU_SCOPE, 'type': PR_TYPE.WRITE_EXCLUSIVE_REGISTRANTS_ONLY})

            _pr_check_registered_keys(s1, [PR_KEY1, PR_KEY2])
            _pr_check_reservation(s1)
            _pr_check_registered_keys(s2, [PR_KEY1, PR_KEY2])
            _pr_check_reservation(s2)

        _pr_check_registered_keys(s1, [PR_KEY1])
        _pr_check_reservation(s1)
        _pr_check_registered_keys(s2, [PR_KEY1])
        _pr_check_reservation(s2)

    _pr_check_registered_keys(s1, [])
    _pr_check_reservation(s1)
    _pr_check_registered_keys(s2, [])
    _pr_check_reservation(s2)

    #
    # Now let's fail some stuff
    # See:
    # - SPC-5 5.14 Table 66
    # - SBC-4 4.17 Table 13
    #
    zeros = bytearray(512)
    dancing_queen = bytearray.fromhex('00abba00') * 128
    deadbeef = bytearray.fromhex('deadbeef') * 128
    with _pr_registration(s1, PR_KEY1):
        with _pr_registration(s2, PR_KEY2):

            # With registrations only, both initiators can write
            s1.write16(0, 1, deadbeef)
            s2.write16(1, 1, dancing_queen)
            r = s1.read16(1, 1)
            assert r.datain == dancing_queen, r.datain
            r = s2.read16(0, 1)
            assert r.datain == deadbeef, r.datain

            with _pr_reservation(s1, PR_TYPE.WRITE_EXCLUSIVE, reservation_key=PR_KEY1, other_connections=[s2]):
                s1.writesame16(0, 2, zeros)
                r = s2.read16(0, 2)
                assert r.datain == zeros + zeros, r.datain

                with _pr_expect_reservation_conflict(s2):
                    s2.write16(1, 1, dancing_queen)

                r = s2.read16(0, 2)
                assert r.datain == zeros + zeros, r.datain

                with _pr_expect_reservation_conflict(s2):
                    with _pr_reservation(s2, PR_TYPE.WRITE_EXCLUSIVE, reservation_key=PR_KEY2):
                        pass

            with _pr_reservation(s1, PR_TYPE.EXCLUSIVE_ACCESS, reservation_key=PR_KEY1, other_connections=[s2]):
                with _pr_expect_reservation_conflict(s2):
                    r = s2.read16(0, 2)
                    assert r.datain == zeros + zeros, r.datain

            with _pr_reservation(s1, PR_TYPE.EXCLUSIVE_ACCESS_REGISTRANTS_ONLY, reservation_key=PR_KEY1, other_connections=[s2]):
                r = s2.read16(0, 2)
                assert r.datain == zeros + zeros, r.datain

        # s2 no longer is registered
        with _pr_reservation(s1, PR_TYPE.EXCLUSIVE_ACCESS_REGISTRANTS_ONLY, reservation_key=PR_KEY1):
            with _pr_expect_reservation_conflict(s2):
                r = s2.read16(0, 2)
                assert r.datain == zeros + zeros, r.datain

        with _pr_reservation(s1, PR_TYPE.WRITE_EXCLUSIVE_REGISTRANTS_ONLY, reservation_key=PR_KEY1):
            r = s2.read16(0, 2)
            assert r.datain == zeros + zeros, r.datain


@skip_persistent_reservations
@skip_multi_initiator
def test_18_persistent_reservation_two_initiators(request):
    depends(request, ["iscsi_cmd_00"], scope="session")
    with initiator_portal() as config:
        with configured_target_to_zvol_extent(config, target_name, zvol):
            iqn = f'{basename}:{target_name}'
            with iscsi_scsi_connection(truenas_server.ip, iqn) as s1:
                TUR(s1)
                initiator_name2 = f"iqn.2018-01.org.pyscsi:{socket.gethostname()}:second"
                with iscsi_scsi_connection(truenas_server.ip, iqn, initiator_name=initiator_name2) as s2:
                    TUR(s2)
                    _check_persistent_reservations(s1, s2)


def _serial_number(s):
    x = s.inquiry(evpd=1, page_code=0x80)
    return x.result['unit_serial_number'].decode('utf-8')


def _device_identification(s):
    result = {}
    x = s.inquiry(evpd=1, page_code=0x83)
    for desc in x.result['designator_descriptors']:
        if desc['designator_type'] == 4:
            result['relative_target_port_identifier'] = desc['designator']['relative_port']
        if desc['designator_type'] == 5:
            result['target_port_group'] = desc['designator']['target_portal_group']
        if desc['designator_type'] == 3 and desc['designator']['naa'] == 6:
            items = (desc['designator']['naa'],
                     desc['designator']['ieee_company_id'],
                     desc['designator']['vendor_specific_identifier'],
                     desc['designator']['vendor_specific_identifier_extension']
                     )
            result['naa'] = "0x{:01x}{:06x}{:09x}{:016x}".format(*items)
    return result


def _verify_ha_inquiry(s, serial_number, naa, tpgs=0,
                       vendor='TrueNAS', product_id='iSCSI Disk'):
    """
    Verify that the supplied SCSI has the expected INQUIRY response.

    :param s: a pyscsi.SCSI instance
    """
    TUR(s)
    inq = s.inquiry().result
    assert inq['t10_vendor_identification'].decode('utf-8').startswith(vendor)
    assert inq['product_identification'].decode('utf-8').startswith(product_id)
    assert inq['tpgs'] == tpgs
    assert serial_number == _serial_number(s)
    assert naa == _device_identification(s)['naa']


def _get_node(timeout=None):
    return call('failover.node')


def _get_ha_failover_status():
    # Make sure we're talking to the master
    return call('failover.status')


def _get_ha_remote_failover_status():
    return call('failover.call_remote', 'failover.status')


def _get_ha_failover_in_progress():
    # Make sure we're talking to the master
    return call('failover.in_progress')


def _check_master():
    status = _get_ha_failover_status()
    assert status == 'MASTER'


def _check_ha_node_configuration():
    both_nodes = ['A', 'B']
    # Let's perform some sanity checking wrt controller and IP address
    # First get node and calculate othernode
    node = _get_node()
    assert node in both_nodes
    _check_master()

    # Now let's get IPs and ensure that
    # - Node A has truenas_server.nodea_ip
    # - Node B has truenas_server.nodeb_ip
    # We will need this later when we start checking TPG, etc
    ips = {}
    for anode in both_nodes:
        ips[anode] = set()
        if anode == node:
            interfaces = call('interface.query')
        else:
            interfaces = call('failover.call_remote', 'interface.query')

        for i in interfaces:
            for alias in i['state']['aliases']:
                if alias.get('type') == 'INET':
                    ips[anode].add(alias['address'])
    # Ensure that truenas_server.nodea_ip and truenas_server.nodeb_ip are what we expect
    assert truenas_server.nodea_ip in ips['A']
    assert truenas_server.nodea_ip not in ips['B']
    assert truenas_server.nodeb_ip in ips['B']
    assert truenas_server.nodeb_ip not in ips['A']


def _verify_ha_device_identification(s, naa, relative_target_port_identifier, target_port_group):
    x = _device_identification(s)
    assert x['naa'] == naa, x
    assert x['relative_target_port_identifier'] == relative_target_port_identifier, x
    assert x['target_port_group'] == target_port_group, x


def _verify_ha_report_target_port_groups(s, tpgs, active_tpg):
    """
    Verify that the REPORT TARGET PORT GROUPS command returns the expected
    results.
    """
    x = s.reporttargetportgroups()
    for tpg_desc in x.result['target_port_group_descriptors']:
        tpg_id = tpg_desc['target_port_group']
        ids = set([x['relative_target_port_id'] for x in tpg_desc['target_ports']])
        assert ids == set(tpgs[tpg_id]), ids
        # See SPC-5 6.36 REPORT TARGET PORT GROUPS
        # Active/Optimized is 0
        # Active/Non-optimized is 1
        if tpg_id == active_tpg:
            assert tpg_desc['asymmetric_access_state'] == 0, tpg_desc
        else:
            assert tpg_desc['asymmetric_access_state'] == 1, tpg_desc


def _get_active_target_portal_group():
    _check_master()
    node = _get_node()
    if node == 'A':
        return CONTROLLER_A_TARGET_PORT_GROUP_ID
    elif node == 'B':
        return CONTROLLER_B_TARGET_PORT_GROUP_ID
    return None


def _wait_for_alua_settle(retries=20):
    print("Checking ALUA status...")
    while retries:
        if call('iscsi.alua.settled'):
            print("ALUA is settled")
            break
        retries -= 1
        print("Waiting for ALUA to settle")
        sleep(5)


def _ha_reboot_master(delay=900):
    """
    Reboot the MASTER node and wait for both the new MASTER
    and new BACKUP to become available.
    """
    get_node_timeout = 20
    orig_master_node = _get_node()
    new_master_node = other_node(orig_master_node)

    call('system.reboot')

    # First we'll loop until the node is no longer the orig_node
    new_master = False
    while not new_master:
        try:
            # There are times when we don't get a response at all (albeit
            # in a bhyte HA-VM pair), so add a timeout to catch this situation.
            if _get_node(timeout=get_node_timeout) == new_master_node:
                new_master = True
                break
        except requests.exceptions.Timeout:
            delay = delay - get_node_timeout
        except Exception:
            delay = delay - 1
        if delay <= 0:
            break
        print("Waiting for MASTER")
        sleep(1)

    if not new_master:
        raise RuntimeError('Did not switch to new controller.')

    # OK, we're on the new master, now wait for the other controller
    # to become BACKUP.
    new_backup = False
    while not new_backup:
        try:
            if _get_ha_remote_failover_status() == 'BACKUP':
                new_backup = True
                break
        except Exception:
            pass
        delay = delay - 5
        if delay <= 0:
            break
        print("Waiting for BACKUP")
        sleep(5)

    if not new_backup:
        raise RuntimeError('Backup controller did not surface.')

    # Ensure that a failover is still not in progress
    in_progress = True
    while in_progress:
        try:
            in_progress = _get_ha_failover_in_progress()
            if not in_progress:
                break
        except Exception:
            pass
        delay = delay - 5
        if delay <= 0:
            break
        print("Waiting while in progress")
        sleep(5)

    if in_progress:
        raise RuntimeError('Failover never completed.')

    # Finally check the ALUA status
    _wait_for_alua_settle()


def _ensure_alua_state(state):
    results = call('iscsi.global.config')
    assert results['alua'] == state, results


@pytest.mark.dependency(name="iscsi_alua_config")
@pytest.mark.timeout(900)
def test_19_alua_config(request):
    """
    Test various aspects of ALUA configuration.

    When run against a HA system this test will perform TWO reboots to
    test failover wrt iSCSI ALUA targets.

    The second reboot was added to return the system to the original ACTIVE
    node.  This means that subsequent tests will run on the same node that
    the previous tests started on, thereby simplifying log analysis.
    """
    # First ensure ALUA is off
    _ensure_alua_state(False)

    if ha:
        _check_ha_node_configuration()

    # Next create a target
    with initiator_portal() as config:
        with configured_target_to_file_extent(config,
                                              target_name,
                                              pool_name,
                                              dataset_name,
                                              file_name
                                              ) as iscsi_config:
            # Login to the target and ensure that things look reasonable.
            iqn = f'{basename}:{target_name}'
            api_serial_number = iscsi_config['extent']['serial']
            api_naa = iscsi_config['extent']['naa']
            with iscsi_scsi_connection(truenas_server.ip, iqn) as s:
                _verify_ha_inquiry(s, api_serial_number, api_naa)

            if ha:
                # Only perform this section on a HA system

                with alua_enabled():
                    _ensure_alua_state(True)
                    _wait_for_alua_settle()

                    # We will login to the target on BOTH controllers and make sure
                    # we see the same target.  Observe that we supply tpgs=1 as
                    # part of the check
                    with iscsi_scsi_connection(truenas_server.nodea_ip, iqn) as s1:
                        _verify_ha_inquiry(s1, api_serial_number, api_naa, 1)
                        with iscsi_scsi_connection(truenas_server.nodeb_ip, iqn) as s2:
                            _verify_ha_inquiry(s2, api_serial_number, api_naa, 1)

                            _verify_ha_device_identification(s1, api_naa, 1, CONTROLLER_A_TARGET_PORT_GROUP_ID)
                            _verify_ha_device_identification(s2, api_naa, 32001, CONTROLLER_B_TARGET_PORT_GROUP_ID)

                            tpgs = {
                                CONTROLLER_A_TARGET_PORT_GROUP_ID: [1],
                                CONTROLLER_B_TARGET_PORT_GROUP_ID: [32001]
                            }
                            active_tpg = _get_active_target_portal_group()
                            _verify_ha_report_target_port_groups(s1, tpgs, active_tpg)
                            _verify_ha_report_target_port_groups(s2, tpgs, active_tpg)

                # Ensure ALUA is off again
                _ensure_alua_state(False)

        # At this point we have no targets and ALUA is off
        if ha:
            # Now turn on ALUA again
            with alua_enabled():
                _ensure_alua_state(True)

                # Then create a target (with ALUA already enabled)
                with configured_target_to_file_extent(config,
                                                      target_name,
                                                      pool_name,
                                                      dataset_name,
                                                      file_name
                                                      ) as iscsi_config:
                    iqn = f'{basename}:{target_name}'
                    api_serial_number = iscsi_config['extent']['serial']
                    api_naa = iscsi_config['extent']['naa']
                    # Login to the target and ensure that things look reasonable.
                    with iscsi_scsi_connection(truenas_server.nodea_ip, iqn) as s1:
                        _verify_ha_inquiry(s1, api_serial_number, api_naa, 1)

                        with iscsi_scsi_connection(truenas_server.nodeb_ip, iqn) as s2:
                            _verify_ha_inquiry(s2, api_serial_number, api_naa, 1)

                            _verify_ha_device_identification(s1, api_naa, 1, CONTROLLER_A_TARGET_PORT_GROUP_ID)
                            _verify_ha_device_identification(s2, api_naa, 32001, CONTROLLER_B_TARGET_PORT_GROUP_ID)

                            # Use the tpgs & active_tpg from above
                            _verify_ha_report_target_port_groups(s1, tpgs, active_tpg)
                            _verify_ha_report_target_port_groups(s2, tpgs, active_tpg)

                            _check_target_rw_paths(s1, s2)

                            # Let's failover
                            _ha_reboot_master()
                            expect_check_condition(s1, sense_ascq_dict[0x2900])  # "POWER ON, RESET, OR BUS DEVICE RESET OCCURRED"
                            expect_check_condition(s2, sense_ascq_dict[0x2900])  # "POWER ON, RESET, OR BUS DEVICE RESET OCCURRED"

                            _check_ha_node_configuration()
                            new_active_tpg = _get_active_target_portal_group()
                            assert new_active_tpg != active_tpg

                            _verify_ha_device_identification(s1, api_naa, 1, CONTROLLER_A_TARGET_PORT_GROUP_ID)
                            _verify_ha_device_identification(s2, api_naa, 32001, CONTROLLER_B_TARGET_PORT_GROUP_ID)

                            _verify_ha_report_target_port_groups(s1, tpgs, new_active_tpg)
                            _verify_ha_report_target_port_groups(s2, tpgs, new_active_tpg)

                            _check_target_rw_paths(s1, s2)

                            # Create a new target
                            with configured_target_to_zvol_extent(config, f'{target_name}b', zvol) as iscsi_config2:
                                iqn2 = f'{basename}:{target_name}b'
                                api_serial_number2 = iscsi_config2['extent']['serial']
                                api_naa2 = iscsi_config2['extent']['naa']
                                tpgs2 = {
                                    CONTROLLER_A_TARGET_PORT_GROUP_ID: [1, 2],
                                    CONTROLLER_B_TARGET_PORT_GROUP_ID: [32001, 32002]
                                }
                                # Wait until ALUA settles, so that we know the target is available on the STANDBY node.
                                _wait_for_alua_settle()
                                # Login to the target on each controller
                                with iscsi_scsi_connection(truenas_server.nodea_ip, iqn2) as s3:
                                    _verify_ha_inquiry(s3, api_serial_number2, api_naa2, 1)
                                    initiator_name3 = f"iqn.2018-01.org.pyscsi:{socket.gethostname()}:third"
                                    with iscsi_scsi_connection(truenas_server.nodeb_ip, iqn2, initiator_name=initiator_name3) as s4:
                                        _verify_ha_inquiry(s4, api_serial_number2, api_naa2, 1)
                                        _verify_ha_device_identification(s3, api_naa2, 2, CONTROLLER_A_TARGET_PORT_GROUP_ID)
                                        _verify_ha_device_identification(s4, api_naa2, 32002, CONTROLLER_B_TARGET_PORT_GROUP_ID)
                                        _verify_ha_report_target_port_groups(s3, tpgs2, new_active_tpg)
                                        _verify_ha_report_target_port_groups(s4, tpgs2, new_active_tpg)
                                        _check_target_rw_paths(s3, s4)

                                        # Reboot again (to failback to the original ACTIVE node)
                                        _ha_reboot_master()
                                        for s in [s1, s2, s3, s4]:
                                            expect_check_condition(s, sense_ascq_dict[0x2900])  # "POWER ON, RESET, OR BUS DEVICE RESET OCCURRED"

                                        # After the 2nd reboot we will switch back to using the original active_tpg

                                        # Check the new target again
                                        _verify_ha_inquiry(s3, api_serial_number2, api_naa2, 1)
                                        _verify_ha_inquiry(s4, api_serial_number2, api_naa2, 1)
                                        _verify_ha_device_identification(s3, api_naa2, 2, CONTROLLER_A_TARGET_PORT_GROUP_ID)
                                        _verify_ha_device_identification(s4, api_naa2, 32002, CONTROLLER_B_TARGET_PORT_GROUP_ID)
                                        _verify_ha_report_target_port_groups(s3, tpgs2, active_tpg)
                                        _verify_ha_report_target_port_groups(s4, tpgs2, active_tpg)
                                        _check_target_rw_paths(s3, s4)

                                        # Check the original target
                                        _verify_ha_inquiry(s1, api_serial_number, api_naa, 1)
                                        _verify_ha_inquiry(s2, api_serial_number, api_naa, 1)
                                        _verify_ha_device_identification(s1, api_naa, 1, CONTROLLER_A_TARGET_PORT_GROUP_ID)
                                        _verify_ha_device_identification(s2, api_naa, 32001, CONTROLLER_B_TARGET_PORT_GROUP_ID)
                                        _verify_ha_report_target_port_groups(s1, tpgs2, active_tpg)
                                        _verify_ha_report_target_port_groups(s2, tpgs2, active_tpg)
                                        _check_target_rw_paths(s1, s2)
                            # Second target has been removed again
                            _wait_for_alua_settle()
                            _verify_ha_report_target_port_groups(s1, tpgs, active_tpg)
                            _verify_ha_report_target_port_groups(s2, tpgs, active_tpg)

            # Ensure ALUA is off again
            _ensure_alua_state(False)


@skip_persistent_reservations
@skip_multi_initiator
@skip_ha_tests
def test_20_alua_basic_persistent_reservation(request):
    # Don't need to specify "iscsi_cmd_00" here
    depends(request, ["iscsi_alua_config", "iscsi_basic_persistent_reservation"], scope="session")
    # Turn on ALUA
    with alua_enabled():
        with initiator_portal() as config:
            with configured_target_to_file_extent(config, target_name, pool_name, dataset_name, file_name):
                iqn = f'{basename}:{target_name}'
                # Login to the target on each controller
                with iscsi_scsi_connection(truenas_server.nodea_ip, iqn) as s1:
                    with iscsi_scsi_connection(truenas_server.nodeb_ip, iqn) as s2:
                        # Now we can do some basic tests
                        _pr_check_registered_keys(s1, [])
                        _pr_check_registered_keys(s2, [])
                        _pr_check_reservation(s1)
                        _pr_check_reservation(s2)

                        with _pr_registration(s1, PR_KEY1):
                            _pr_check_registered_keys(s1, [PR_KEY1])
                            _pr_check_registered_keys(s2, [PR_KEY1])
                            _pr_check_reservation(s1)
                            _pr_check_reservation(s2)

                            with _pr_reservation(s1, PR_TYPE.WRITE_EXCLUSIVE, reservation_key=PR_KEY1, other_connections=[s2]):
                                _pr_check_registered_keys(s1, [PR_KEY1])
                                _pr_check_registered_keys(s2, [PR_KEY1])
                                _pr_check_reservation(s1, {'reservation_key': PR_KEY1, 'scope': LU_SCOPE, 'type': PR_TYPE.WRITE_EXCLUSIVE})
                                _pr_check_reservation(s2, {'reservation_key': PR_KEY1, 'scope': LU_SCOPE, 'type': PR_TYPE.WRITE_EXCLUSIVE})

                            _pr_check_registered_keys(s1, [PR_KEY1])
                            _pr_check_registered_keys(s2, [PR_KEY1])
                            _pr_check_reservation(s1)
                            _pr_check_reservation(s2)

                        _pr_check_registered_keys(s1, [])
                        _pr_check_registered_keys(s2, [])
                        _pr_check_reservation(s1)
                        _pr_check_reservation(s2)

    # Ensure ALUA is off again
    _ensure_alua_state(False)


@skip_persistent_reservations
@skip_multi_initiator
@skip_ha_tests
def test_21_alua_persistent_reservation_two_initiators(request):
    depends(request, ["iscsi_alua_config", "iscsi_basic_persistent_reservation"], scope="session")
    with alua_enabled():
        with initiator_portal() as config:
            with configured_target_to_zvol_extent(config, target_name, zvol):
                iqn = f'{basename}:{target_name}'
                # Login to the target on each controller
                with iscsi_scsi_connection(truenas_server.nodea_ip, iqn) as s1:
                    TUR(s1)
                    initiator_name2 = f"iqn.2018-01.org.pyscsi:{socket.gethostname()}:second"
                    with iscsi_scsi_connection(truenas_server.nodeb_ip, iqn, initiator_name=initiator_name2) as s2:
                        TUR(s2)
                        _check_persistent_reservations(s1, s2)
                        # Do it all again, the other way around
                        _check_persistent_reservations(s2, s1)


def _get_designator(s, designator_type):
    x = s.inquiry(evpd=1, page_code=0x83)
    for designator in x.result["designator_descriptors"]:
        if designator["designator_type"] == designator_type:
            del designator["piv"]
            return designator


def _xcopy_test(s1, s2, adds1=None, adds2=None):
    zeros = bytearray(512)
    deadbeef = bytearray.fromhex("deadbeef") * 128

    def validate_blocks(s, start, end, beefy_list):
        for lba in range(start, end):
            r = s.read16(lba, 1)
            if lba in beefy_list:
                assert r.datain == deadbeef, r.datain
            else:
                assert r.datain == zeros, r.datain

    d1 = _get_designator(s1, 3)
    d2 = _get_designator(s2, 3)

    # First let's write zeros to the first 20 blocks using WRITE SAME (16)
    s1.writesame16(0, 20, zeros)
    s2.writesame16(0, 20, zeros)

    # Write some deadbeef
    s1.write16(1, 1, deadbeef)
    s1.write16(3, 1, deadbeef)
    s1.write16(4, 1, deadbeef)

    # Check that the blocks were written correctly
    validate_blocks(s1, 0, 20, [1, 3, 4])
    validate_blocks(s2, 0, 20, [])
    if adds1:
        validate_blocks(adds1, 0, 20, [1, 3, 4])
    if adds2:
        validate_blocks(adds2, 0, 20, [])

    # XCOPY
    s1.extendedcopy4(
        priority=1,
        list_identifier=0x34,
        target_descriptor_list=[
            {
                "descriptor_type_code": "Identification descriptor target descriptor",
                "peripheral_device_type": 0x00,
                "target_descriptor_parameters": d1,
                "device_type_specific_parameters": {"disk_block_length": 512},
            },
            {
                "descriptor_type_code": "Identification descriptor target descriptor",
                "peripheral_device_type": 0x00,
                "target_descriptor_parameters": d2,
                "device_type_specific_parameters": {"disk_block_length": 512},
            },
        ],
        segment_descriptor_list=[
            {
                "descriptor_type_code": "Copy from block device to block device",
                "dc": 1,
                "source_target_descriptor_id": 0,
                "destination_target_descriptor_id": 1,
                "block_device_number_of_blocks": 4,
                "source_block_device_logical_block_address": 1,
                "destination_block_device_logical_block_address": 10,
            }
        ],
    )

    validate_blocks(s1, 0, 20, [1, 3, 4])
    validate_blocks(s2, 0, 20, [10, 12, 13])
    if adds1:
        validate_blocks(adds1, 0, 20, [1, 3, 4])
    if adds2:
        validate_blocks(adds2, 0, 20, [10, 12, 13])


@pytest.mark.parametrize('extent2', ["FILE", "VOLUME"])
@pytest.mark.parametrize('extent1', ["FILE", "VOLUME"])
def test_22_extended_copy(request, extent1, extent2):
    # print(f"Extended copy {extent1} -> {extent2}")
    depends(request, ["iscsi_cmd_00"], scope="session")

    name1 = f"{target_name}x1"
    name2 = f"{target_name}x2"
    iqn1 = f'{basename}:{name1}'
    iqn2 = f'{basename}:{name2}'

    with initiator_portal() as config:
        with configured_target(config, name1, extent1):
            with configured_target(config, name2, extent2):
                with iscsi_scsi_connection(truenas_server.ip, iqn1) as s1:
                    with iscsi_scsi_connection(truenas_server.ip, iqn2) as s2:
                        s1.testunitready()
                        s2.testunitready()
                        _xcopy_test(s1, s2)


@skip_ha_tests
@pytest.mark.parametrize('extent2', ["FILE", "VOLUME"])
@pytest.mark.parametrize('extent1', ["FILE", "VOLUME"])
def test_23_ha_extended_copy(request, extent1, extent2):
    depends(request, ["iscsi_alua_config"], scope="session")

    name1 = f"{target_name}x1"
    name2 = f"{target_name}x2"
    iqn1 = f'{basename}:{name1}'
    iqn2 = f'{basename}:{name2}'

    with alua_enabled():
        with initiator_portal() as config:
            with configured_target(config, name1, extent1):
                with configured_target(config, name2, extent2):
                    with iscsi_scsi_connection(truenas_server.nodea_ip, iqn1) as sa1:
                        with iscsi_scsi_connection(truenas_server.nodea_ip, iqn2) as sa2:
                            with iscsi_scsi_connection(truenas_server.nodeb_ip, iqn1) as sb1:
                                with iscsi_scsi_connection(truenas_server.nodeb_ip, iqn2) as sb2:
                                    sa1.testunitready()
                                    sa2.testunitready()
                                    sb1.testunitready()
                                    sb2.testunitready()
                                    _xcopy_test(sa1, sa2, sb1, sb2)
                                    # Now re-run the test using the other controller
                                    _xcopy_test(sb1, sb2, sa1, sa2)


def test_24_iscsi_target_disk_login(request):
    """
    Tests whether a logged in iSCSI target shows up in disks.
    """
    depends(request, ["iscsi_cmd_00"], scope="session")
    iqn = f'{basename}:{target_name}'

    def fetch_disk_data(fetch_remote=False):
        data = {}
        if fetch_remote:
            data['failover.get_disks_local'] = set(call('failover.call_remote', 'failover.get_disks_local'))
            data['disk.get_unused'] = set([d['devname'] for d in call('failover.call_remote', 'disk.get_unused')])
        else:
            data['failover.get_disks_local'] = set(call('failover.get_disks_local'))
            data['disk.get_unused'] = set([d['devname'] for d in call('disk.get_unused')])
        return data

    def check_disk_data(old, new, whenstr, internode_check=False):
        # There are some items that we can't compare between 2 HA nodes
        SINGLE_NODE_COMPARE_ONLY = ['disk.get_unused']
        for key in old:
            if internode_check and key in SINGLE_NODE_COMPARE_ONLY:
                continue
            assert old[key] == new[key], f"{key} does not match {whenstr}: {old[key]} {new[key]}"

    if ha:
        # In HA we will create an ALUA target and check the STANDBY node
        data_before_l = fetch_disk_data()
        data_before_r = fetch_disk_data(True)
        check_disk_data(data_before_l, data_before_r, "initially", True)
        with alua_enabled():
            with initiator_portal() as config:
                with configured_target_to_zvol_extent(config, target_name, zvol):
                    sleep(5)
                    data_after_l = fetch_disk_data()
                    data_after_r = fetch_disk_data(True)
                    check_disk_data(data_before_l, data_after_l, "after iSCSI ALUA target creation (Active)")
                    check_disk_data(data_before_r, data_after_r, "after iSCSI ALUA target creation (Standby)")
    else:
        # In non-HA we will create a target and login to it from the same TrueNAS system
        # Just in case IP was supplied as a hostname use actual_ip
        actual_ip = get_ip_addr(truenas_server.ip)
        data_before = fetch_disk_data()
        with initiator_portal() as config:
            with configured_target_to_zvol_extent(config, target_name, zvol):
                data_after = fetch_disk_data()
                check_disk_data(data_before, data_after, "after iSCSI target creation")

                # Discover the target (loopback)
                results = SSH_TEST(f"iscsiadm -m discovery -t st -p {actual_ip}", user, password)
                assert results['result'] is True, f'out: {results["output"]}, err: {results["stderr"]}'
                # Make SURE we find the target at the ip we expect
                found_iqn = False
                for line in results['stdout'].split('\n'):
                    if not line.startswith(f'{actual_ip}:'):
                        continue
                    if line.split()[1] == iqn:
                        found_iqn = True
                assert found_iqn, f'Failed to find IQN {iqn}: out: {results["output"]}'

                # Login the target
                results = SSH_TEST(f"iscsiadm -m node -T {iqn} -p {actual_ip}:3260 --login", user, password)
                assert results['result'] is True, f'out: {results["output"]}, err: {results["stderr"]}'
                # Allow some time for the disk to surface
                sleep(5)
                # Then check that everything looks OK
                try:
                    data_after = fetch_disk_data()
                    check_disk_data(data_before, data_after, "after iSCSI target login")
                finally:
                    results = SSH_TEST(f"iscsiadm -m node -T {iqn} -p {actual_ip}:3260 --logout", user, password)
                    assert results['result'] is True, f'out: {results["output"]}, err: {results["stderr"]}'


def test_25_resize_target_zvol(request):
    """
    Verify that an iSCSI client is notified when the size of a ZVOL underlying
    an iSCSI extent is modified.
    """
    depends(request, ["iscsi_cmd_00"], scope="session")

    with initiator_portal() as config:
        with configured_target_to_zvol_extent(config, target_name, zvol, volsize=MB_100) as config:
            iqn = f'{basename}:{target_name}'
            with iscsi_scsi_connection(truenas_server.ip, iqn) as s:
                TUR(s)
                assert MB_100 == read_capacity16(s)
                # Have checked using tcpdump/wireshark that a SCSI Asynchronous Event Notification
                # gets sent 0x2A09: "CAPACITY DATA HAS CHANGED"
                zvol_resize(zvol, MB_256)
                assert MB_256 == read_capacity16(s)
                # But we can do better (in terms of test) ... turn AEN off,
                # which means we will get a CHECK CONDITION on the next resize
                SSH_TEST(f"echo 1 > /sys/kernel/scst_tgt/targets/iscsi/{iqn}/aen_disabled", user, password)
                zvol_resize(zvol, MB_512)
                expect_check_condition(s, sense_ascq_dict[0x2A09])  # "CAPACITY DATA HAS CHANGED"
                assert MB_512 == read_capacity16(s)
                # Try to shrink the ZVOL again.  Expect an error
                with pytest.raises(ValidationErrors):
                    zvol_resize(zvol, MB_256)
                assert MB_512 == read_capacity16(s)


def test_26_resize_target_file(request):
    """
    Verify that an iSCSI client is notified when the size of a file-based
    iSCSI extent is modified.
    """
    depends(request, ["iscsi_cmd_00"], scope="session")

    with initiator_portal() as config:
        with configured_target_to_file_extent(config,
                                              target_name,
                                              pool_name,
                                              dataset_name,
                                              file_name,
                                              filesize=MB_100) as config:
            iqn = f'{basename}:{target_name}'
            with iscsi_scsi_connection(truenas_server.ip, iqn) as s:
                extent_id = config['extent']['id']
                TUR(s)
                assert MB_100 == read_capacity16(s)
                file_extent_resize(extent_id, MB_256)
                assert MB_256 == read_capacity16(s)
                # Turn AEN off so that we will get a CHECK CONDITION on the next resize
                SSH_TEST(f"echo 1 > /sys/kernel/scst_tgt/targets/iscsi/{iqn}/aen_disabled", user, password)
                file_extent_resize(extent_id, MB_512)
                expect_check_condition(s, sense_ascq_dict[0x2A09])  # "CAPACITY DATA HAS CHANGED"
                assert MB_512 == read_capacity16(s)
                # Try to shrink the file again.  Expect an error
                with pytest.raises(ValidationErrors):
                    file_extent_resize(extent_id, MB_256)
                assert MB_512 == read_capacity16(s)


@skip_multi_initiator
def test_27_initiator_group(request):
    depends(request, ["iscsi_cmd_00"], scope="session")

    initiator_base = f"iqn.2018-01.org.pyscsi:{socket.gethostname()}"
    initiator_iqn1 = f"{initiator_base}:one"
    initiator_iqn2 = f"{initiator_base}:two"
    initiator_iqn3 = f"{initiator_base}:three"

    # First create a target without an initiator group specified
    with initiator_portal() as config1:
        with configured_target_to_zvol_extent(config1, target_name, zvol) as config:
            iqn = f'{basename}:{target_name}'

            # Ensure we can access from all initiators
            for initiator_iqn in [initiator_iqn1, initiator_iqn2, initiator_iqn3]:
                with iscsi_scsi_connection(truenas_server.ip, iqn, initiator_name=initiator_iqn) as s:
                    TUR(s)

            # Now set the initiator id to the empty (Allow All Initiators) one
            # that we created above.  Then ensure we can still read access the
            # target from all initiators
            set_target_initiator_id(config['target']['id'], config['initiator']['id'])
            for initiator_iqn in [initiator_iqn1, initiator_iqn2, initiator_iqn3]:
                with iscsi_scsi_connection(truenas_server.ip, iqn, initiator_name=initiator_iqn) as s:
                    TUR(s)

            # Now create another initiator group, which contains the first two
            # initiators only and modify the target to use it
            with initiator("two initiators only", [initiator_iqn1, initiator_iqn2]) as twoinit_config:
                set_target_initiator_id(config['target']['id'], twoinit_config['id'])
                # First two initiators can connect to the target
                for initiator_iqn in [initiator_iqn1, initiator_iqn2]:
                    with iscsi_scsi_connection(truenas_server.ip, iqn, initiator_name=initiator_iqn) as s:
                        TUR(s)
                # Third initiator cannot connect to the target
                with pytest.raises(RuntimeError) as ve:
                    with iscsi_scsi_connection(truenas_server.ip, iqn, initiator_name=initiator_iqn3) as s:
                        TUR(s)
                assert 'Unable to connect to' in str(ve), ve
                # Clear it again
                set_target_initiator_id(config['target']['id'], None)

            for initiator_iqn in [initiator_iqn1, initiator_iqn2, initiator_iqn3]:
                with iscsi_scsi_connection(truenas_server.ip, iqn, initiator_name=initiator_iqn) as s:
                    TUR(s)


def test_28_portal_access(request):
    """
    Verify that an iSCSI client can access a target on the specified
    portal.

    For a HA ALUA target, check the constituent interfaces.
    """
    iqn = f'{basename}:{target_name}'
    with initiator() as initiator_config:
        with portal(listen=[{'ip': get_ip_addr(truenas_server.ip)}]) as portal_config:
            config1 = {'initiator': initiator_config, 'portal': portal_config}
            with configured_target_to_zvol_extent(config1, target_name, zvol, volsize=MB_100):
                with iscsi_scsi_connection(truenas_server.ip, iqn) as s:
                    TUR(s)
                    assert MB_100 == read_capacity16(s)
                # Now, if we are in a HA config turn on ALUA and test
                # the specific IP addresses
                if ha:
                    with alua_enabled():
                        _ensure_alua_state(True)

                        with pytest.raises(RuntimeError) as ve:
                            with iscsi_scsi_connection(truenas_server.ip, iqn) as s:
                                TUR(s)
                        assert 'Unable to connect to' in str(ve), ve

                        with iscsi_scsi_connection(truenas_server.nodea_ip, iqn) as s:
                            TUR(s)
                            assert MB_100 == read_capacity16(s)

                        with iscsi_scsi_connection(truenas_server.nodeb_ip, iqn) as s:
                            TUR(s)
                            assert MB_100 == read_capacity16(s)


def test_29_multiple_extents():
    """
    Verify that an iSCSI client can access multiple target LUNs
    when multiple extents are configured.

    Also validate that an extent serial number cannot be reused, and
    that supplying an empty string serial number means one gets
    generated.
    """
    iqn = f'{basename}:{target_name}'
    with initiator_portal() as config:
        portal_id = config['portal']['id']
        with target(target_name, [{'portal': portal_id}]) as target_config:
            target_id = target_config['id']
            with dataset(dataset_name):
                with file_extent(pool_name, dataset_name, "target.extent1", filesize=MB_100, extent_name="extent1") as extent1_config:
                    with file_extent(pool_name, dataset_name, "target.extent2", filesize=MB_256, extent_name="extent2") as extent2_config:
                        with target_extent_associate(target_id, extent1_config['id'], 0):
                            with target_extent_associate(target_id, extent2_config['id'], 1):
                                with iscsi_scsi_connection(truenas_server.ip, iqn, 0) as s:
                                    TUR(s)
                                    assert MB_100 == read_capacity16(s)
                                with iscsi_scsi_connection(truenas_server.ip, iqn, 1) as s:
                                    TUR(s)
                                    assert MB_256 == read_capacity16(s)

                                # Now try to create another extent using the same serial number
                                # We expect this to fail.
                                with pytest.raises(ValidationErrors) as ve:
                                    with file_extent(pool_name, dataset_name, "target.extent3", filesize=MB_512,
                                                     extent_name="extent3", serial=extent1_config['serial']):
                                        pass
                                assert ve.value.errors == [
                                    ValidationError('iscsi_extent_create.serial', 'Serial number must be unique', errno.EINVAL)
                                ]

                                with file_extent(pool_name, dataset_name, "target.extent3", filesize=MB_512,
                                                 extent_name="extent3", serial='') as extent3_config:
                                    # We expect this to complete, but generate a serial number
                                    assert len(extent3_config['serial']) == 15, extent3_config['serial']


def check_inq_enabled_state(iqn, expected):
    """Check the current enabled state of the specified SCST IQN directly from /sys
    is as expected."""
    results = SSH_TEST(f"cat /sys/kernel/scst_tgt/targets/iscsi/{iqn}/enabled", user, password)
    assert results['result'] is True, f'out: {results["output"]}, err: {results["stderr"]}'
    for line in results["output"].split('\n'):
        if line.startswith('Warning: Permanently added'):
            continue
        if line:
            actual = int(line)
    assert actual == expected, f'IQN {iqn} has an unexpected enabled state - was {actual}, expected {expected}'


def test_30_target_without_active_extent(request):
    """Validate that a target will not be enabled if it does not have
    and enabled associated extents"""
    depends(request, ["iscsi_cmd_00"], scope="session")

    name1 = f"{target_name}x1"
    name2 = f"{target_name}x2"
    iqn1 = f'{basename}:{name1}'
    iqn2 = f'{basename}:{name2}'

    with initiator_portal() as config:
        with configured_target(config, name1, 'VOLUME') as target1_config:
            with configured_target(config, name2, 'VOLUME') as target2_config:
                # OK, we've configured two separate targets, ensure all looks good
                check_inq_enabled_state(iqn1, 1)
                check_inq_enabled_state(iqn2, 1)
                with iscsi_scsi_connection(truenas_server.ip, iqn1) as s1:
                    TUR(s1)
                with iscsi_scsi_connection(truenas_server.ip, iqn2) as s2:
                    TUR(s2)

                # Disable an extent and ensure things are as expected
                extent_disable(target2_config['extent']['id'])
                check_inq_enabled_state(iqn1, 1)
                check_inq_enabled_state(iqn2, 0)
                with iscsi_scsi_connection(truenas_server.ip, iqn1) as s1:
                    TUR(s1)
                with pytest.raises(RuntimeError) as ve:
                    with iscsi_scsi_connection(truenas_server.ip, iqn2) as s2:
                        TUR(s2)
                assert 'Unable to connect to' in str(ve), ve

                # Reenable the extent
                extent_enable(target2_config['extent']['id'])
                check_inq_enabled_state(iqn1, 1)
                check_inq_enabled_state(iqn2, 1)
                with iscsi_scsi_connection(truenas_server.ip, iqn1) as s1:
                    TUR(s1)
                with iscsi_scsi_connection(truenas_server.ip, iqn2) as s2:
                    TUR(s2)

                # Move the extent from target2 to target1
                #
                # Doing this by updating the existing association rather
                # than deleting the old association and creating a new one,
                # because want to avoid breakage wrt yield ... finally cleanup
                payload = {
                    'target': target1_config['target']['id'],
                    'lunid': 1,
                    'extent': target2_config['extent']['id']
                }
                call('iscsi.targetextent.update', target2_config['associate']['id'], payload)

                check_inq_enabled_state(iqn1, 1)
                check_inq_enabled_state(iqn2, 0)
                with iscsi_scsi_connection(truenas_server.ip, iqn1) as s1:
                    TUR(s1)
                # We should now have a LUN 1
                with iscsi_scsi_connection(truenas_server.ip, iqn1, 1) as s1b:
                    TUR(s1b)
                with pytest.raises(RuntimeError) as ve:
                    with iscsi_scsi_connection(truenas_server.ip, iqn2) as s2:
                        TUR(s2)
                assert 'Unable to connect to' in str(ve), ve


def test_31_iscsi_sessions(request):
    """Validate that we can get a list of currently running iSCSI sessions."""
    depends(request, ["iscsi_cmd_00"], scope="session")

    name1 = f"{target_name}x1"
    name2 = f"{target_name}x2"
    name3 = f"{target_name}x3"
    iqn1 = f'{basename}:{name1}'
    iqn2 = f'{basename}:{name2}'
    iqn3 = f'{basename}:{name3}'
    initiator_base = f"iqn.2018-01.org.pyscsi:{socket.gethostname()}"
    initiator_iqn1 = f"{initiator_base}:one"
    initiator_iqn2 = f"{initiator_base}:two"
    initiator_iqn3 = f"{initiator_base}:three"

    with initiator_portal() as config:
        with configured_target(config, name1, 'VOLUME'):
            with configured_target(config, name2, 'FILE'):
                with configured_target(config, name3, 'VOLUME'):
                    verify_client_count(0)
                    with iscsi_scsi_connection(truenas_server.ip, iqn1, initiator_name=initiator_iqn1):
                        verify_client_count(1)
                        with iscsi_scsi_connection(truenas_server.ip, iqn2, initiator_name=initiator_iqn2):
                            # Client count checks the number of different IPs attached, not sessions
                            verify_client_count(1)
                            # Validate that the two sessions are reported correctly
                            data = get_iscsi_sessions(check_length=2)
                            for sess in data:
                                if sess['target'] == iqn1:
                                    assert sess['initiator'] == initiator_iqn1, data
                                elif sess['target'] == iqn2:
                                    assert sess['initiator'] == initiator_iqn2, data
                                else:
                                    # Unknown target!
                                    assert False, data
                            # Filter by target
                            data = get_iscsi_sessions([['target', '=', iqn1]], 1)
                            assert data[0]['initiator'] == initiator_iqn1, data
                            data = get_iscsi_sessions([['target', '=', iqn2]], 1)
                            assert data[0]['initiator'] == initiator_iqn2, data
                            data = get_iscsi_sessions([['target', '=', iqn3]], 0)
                            # Filter by initiator
                            data = get_iscsi_sessions([['initiator', '=', initiator_iqn1]], 1)
                            assert data[0]['target'] == iqn1, data
                            data = get_iscsi_sessions([['initiator', '=', initiator_iqn2]], 1)
                            assert data[0]['target'] == iqn2, data
                            data = get_iscsi_sessions([['initiator', '=', initiator_iqn3]], 0)
                            # Now login to target2 with initiator1
                            with iscsi_scsi_connection(truenas_server.ip, iqn2, initiator_name=initiator_iqn1):
                                verify_client_count(1)
                                get_iscsi_sessions(check_length=3)
                                # Filter by target
                                data = get_iscsi_sessions([['target', '=', iqn1]], 1)
                                assert data[0]['initiator'] == initiator_iqn1, data
                                data = get_iscsi_sessions([['target', '=', iqn2]], 2)
                                assert set([sess['initiator'] for sess in data]) == {initiator_iqn1, initiator_iqn2}, data
                                data = get_iscsi_sessions([['target', '=', iqn3]], 0)
                                # Filter by initiator
                                data = get_iscsi_sessions([['initiator', '=', initiator_iqn1]], 2)
                                assert set([sess['target'] for sess in data]) == {iqn1, iqn2}, data
                                data = get_iscsi_sessions([['initiator', '=', initiator_iqn2]], 1)
                                assert data[0]['target'] == iqn2, data
                                data = get_iscsi_sessions([['initiator', '=', initiator_iqn3]], 0)
                            # Logout of target, ensure sessions get updated.
                            verify_client_count(1)
                            data = get_iscsi_sessions(check_length=2)
                            for sess in data:
                                if sess['target'] == iqn1:
                                    assert sess['initiator'] == initiator_iqn1, data
                                elif sess['target'] == iqn2:
                                    assert sess['initiator'] == initiator_iqn2, data
                                else:
                                    # Unknown target!
                                    assert False, data
                        # Client count checks the number of different IPs attached, not sessions
                        verify_client_count(1)
                        get_iscsi_sessions(check_length=1)
                    verify_client_count(0)
                    get_iscsi_sessions(check_length=0)


def test_32_multi_lun_targets(request):
    """Validate that we can create and access multi-LUN targets."""
    depends(request, ["iscsi_cmd_00"], scope="session")

    name1 = f"{target_name}x1"
    name2 = f"{target_name}x2"
    iqn1 = f'{basename}:{name1}'
    iqn2 = f'{basename}:{name2}'

    def test_target_sizes(ipaddr):
        with iscsi_scsi_connection(ipaddr, iqn1, 0) as s:
            verify_capacity(s, MB_100)
        with iscsi_scsi_connection(ipaddr, iqn1, 1) as s:
            verify_capacity(s, MB_200)
        with iscsi_scsi_connection(ipaddr, iqn2, 0) as s:
            verify_capacity(s, MB_256)
        with iscsi_scsi_connection(ipaddr, iqn2, 1) as s:
            verify_capacity(s, MB_512)

    with initiator_portal() as config:
        with configured_target(config, name1, 'FILE', extent_size=MB_100) as config1:
            with add_file_extent_target_lun(config1, 1, MB_200):
                with configured_target(config, name2, 'VOLUME', extent_size=MB_256) as config1:
                    with add_zvol_extent_target_lun(config1, 1, volsize=MB_512):
                        # Check that we can connect to each LUN and that it has the expected capacity
                        test_target_sizes(truenas_server.ip)
                        if ha:
                            # Only perform this section on a HA system
                            with alua_enabled():
                                test_target_sizes(truenas_server.nodea_ip)
                                test_target_sizes(truenas_server.nodeb_ip)


def test_33_no_lun_zero():
    """
    Verify that an iSCSI client can login to a target that is missing LUN 0 (and LUN 1)
    and that report LUNs works as expected.
    """
    iqn = f'{basename}:{target_name}'
    with initiator_portal() as config:
        portal_id = config['portal']['id']
        with target(target_name, [{'portal': portal_id}]) as target_config:
            target_id = target_config['id']
            with dataset(dataset_name):
                with file_extent(pool_name, dataset_name, "target.extent1", filesize=MB_100, extent_name="extent1") as extent1_config:
                    with file_extent(pool_name, dataset_name, "target.extent2", filesize=MB_256, extent_name="extent2") as extent2_config:
                        with target_extent_associate(target_id, extent1_config['id'], 100):
                            with target_extent_associate(target_id, extent2_config['id'], 101):
                                # libiscsi sends a TUR to the lun on connect, so cannot properly test using it.
                                # Let's actually login and check that the expected LUNs surface.
                                assert target_login_test(get_ip_addr(truenas_server.ip), iqn, {100, 101})

                                # With libiscsi we can also check that the expected LUNs are there
                                with iscsi_scsi_connection(truenas_server.ip, iqn, 100) as s:
                                    verify_luns(s, [100, 101])


def test_34_zvol_extent_volthreading():
    """
    Ensure that volthreading is on for regular zvols and off when they are being
    used an iSCSI extent.
    """
    zvol_name = f"zvol_volthreading_test{digit}"
    zvol = f'{pool_name}/{zvol_name}'
    with zvol_dataset(zvol, MB_100, True, True):
        assert get_volthreading(zvol) == 'on'
        with zvol_extent(zvol, extent_name='zvolextent1'):
            assert get_volthreading(zvol) == 'off'
        assert get_volthreading(zvol) == 'on'


@pytest.mark.parametrize('extent_type', ["FILE", "VOLUME"])
def test_35_delete_extent_no_dataset(extent_type):
    """
    Verify that even if a dataset that contains an extent has been deleted from
    the command line, can still use the webui/API to delete the extent.
    """
    dataset_name = f'iscsids_{extent_type}_{digit}'
    with dataset(dataset_name) as dspath:
        DESTROY_CMD = f'zfs destroy -r {dspath}'
        match extent_type:
            case 'FILE':
                with file_extent(pool_name, dataset_name, 'testfile', extent_name='fileextent1'):
                    ssh(DESTROY_CMD)
            case 'VOLUME':
                zvol = f'{dspath}/zvol{digit}'
                with zvol_dataset(zvol, MB_100, True, True):
                    with zvol_extent(zvol, extent_name='zvolextent1'):
                        ssh(DESTROY_CMD)

def test_36_target_readonly_extent():
    """Validate a target that is made RO - either by modifying the extent
    setting, or the underlying ZVOL - behaves correctly."""
    name1 = f"{target_name}x1"
    iqn = f'{basename}:{name1}'

    zeros = bytearray(512)
    deadbeef = bytearray.fromhex('deadbeef') * 128

    def lba_data(flipped):
        if flipped:
            return deadbeef, zeros
        else:
            return zeros, deadbeef

    def write_lbas(s, flipped=False):
        lba0, lba1 = lba_data(flipped)
        s.write16(0, 1, lba0)
        s.write16(1, 1, lba1)

    def read_lbas(s, flipped=False):
        lba0, lba1 = lba_data(flipped)
        r = s.read16(0, 1)
        assert r.datain == lba0, r.datain
        r = s.read16(1, 1)
        assert r.datain == lba1, r.datain

    def check_readonly_state(zvolid, extentid, readonly):
        if readonly:
            assert get_readonly(zvolid) == 'on'
            assert call('iscsi.extent.get_instance', extentid)['ro'] is True
        else:
            assert get_readonly(zvolid) == 'off'
            assert call('iscsi.extent.get_instance', extentid)['ro'] is False

    with initiator_portal() as config:
        with configured_target(config, name1, 'VOLUME') as target_config:
            with iscsi_scsi_connection(truenas_server.ip, iqn) as s:
                zvolid = target_config['dataset']
                extentid = target_config['extent']['id']

                # Ensure that we can read and write
                write_lbas(s)
                read_lbas(s)
                check_readonly_state(zvolid, extentid, False)

                # Set RO by updating the extent
                call('iscsi.extent.update', extentid, {'ro': True})
                expect_check_condition(s, sense_ascq_dict[0x2900])  # "POWER ON, RESET, OR BUS DEVICE RESET OCCURRED"
                check_readonly_state(zvolid, extentid, True)

                # Ensure that we can only READ
                read_lbas(s)
                # Write => Check Condition Sense key = 7 for Data Protect, ASCQ == 0
                with raises_check_condition(7, 0):
                    write_lbas(s, True)
                read_lbas(s)

                # Set RW by updating the extent
                call('iscsi.extent.update', extentid, {'ro': False})
                expect_check_condition(s, sense_ascq_dict[0x2900])  # "POWER ON, RESET, OR BUS DEVICE RESET OCCURRED"
                check_readonly_state(zvolid, extentid, False)

                # Ensure that we can read and write
                read_lbas(s)
                write_lbas(s, True)
                read_lbas(s, True)

                # Set RO by updating the ZVOL
                call('pool.dataset.update', zvolid, {'readonly': 'ON'})
                expect_check_condition(s, sense_ascq_dict[0x2900])  # "POWER ON, RESET, OR BUS DEVICE RESET OCCURRED"
                check_readonly_state(zvolid, extentid, True)

                # Ensure that we can only READ
                read_lbas(s, True)
                with raises_check_condition(7, 0):
                    write_lbas(s)
                read_lbas(s, True)

                # Set RW by updating the ZVOL
                call('pool.dataset.update', zvolid, {'readonly': 'OFF'})
                expect_check_condition(s, sense_ascq_dict[0x2900])  # "POWER ON, RESET, OR BUS DEVICE RESET OCCURRED"
                check_readonly_state(zvolid, extentid, False)

                # Ensure that we can read and write
                read_lbas(s, True)
                write_lbas(s)
                read_lbas(s)


def test_99_teardown(request):
    # Disable iSCSI service
    depends(request, ["iscsi_cmd_00"])
    payload = {'enable': False}
    call('service.update', 'iscsitarget', payload)
    # Stop iSCSI service.
    call('service.stop', 'iscsitarget')
    sleep(1)
    # Verify stopped
    service = _get_service()
    assert service['state'] == "STOPPED", service
